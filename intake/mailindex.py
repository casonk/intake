"""Local, searchable index of the mailbox for the review UI.

One row per Gmail message (keyed by ``X-GM-MSGID``, which is the same across every label), with
sender, subject, date, labels, read state, a cleaned body excerpt, and an optional one-line
summary.  An FTS5 table makes it searchable.  The index lives in its own SQLite file under the
gitignored ``data/`` (mail text is more sensitive than receipt rows, so it is kept separate),
and is a *cache*: the mailbox stays the source of truth, and ``intake mail sync`` can rebuild it.

Sync reads ``[Gmail]/All Mail`` read-only (``BODY.PEEK``, readonly select), so a message is
indexed once however many labels it carries, and label moves made by triage show up as label
changes rather than as new mail.  Nothing here writes to the mailbox.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

log = logging.getLogger("intake.mailindex")

ALL_MAIL = "[Gmail]/All Mail"
EXCERPT_CHARS = 3000
MAX_FULL_FETCH_BYTES = 400_000  # larger messages: headers only, so attachments are never pulled
UID_CHUNK = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    gm_msgid TEXT PRIMARY KEY,
    uid INTEGER NOT NULL,
    thread_id TEXT,
    message_id TEXT,
    sender TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    recipients TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    labels TEXT NOT NULL DEFAULT '[]',
    in_inbox INTEGER NOT NULL DEFAULT 0,
    unread INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    excerpt TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    first_indexed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    gone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_uid ON messages(uid);
CREATE INDEX IF NOT EXISTS messages_sent ON messages(sent_at DESC);
CREATE INDEX IF NOT EXISTS messages_inbox ON messages(in_inbox, reviewed_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, sender, sender_name, excerpt, summary,
    content='messages', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, sender, sender_name, excerpt, summary)
    VALUES (new.rowid, new.subject, new.sender, new.sender_name, new.excerpt, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, sender, sender_name, excerpt, summary)
    VALUES ('delete', old.rowid, old.subject, old.sender, old.sender_name, old.excerpt, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, sender, sender_name, excerpt, summary)
    VALUES ('delete', old.rowid, old.subject, old.sender, old.sender_name, old.excerpt, old.summary);
    INSERT INTO messages_fts(rowid, subject, sender, sender_name, excerpt, summary)
    VALUES (new.rowid, new.subject, new.sender, new.sender_name, new.excerpt, new.summary);
END;
"""

# Primary queue of a message, from its Gmail labels.  First match wins.
QUEUE_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("promos", ("intake/promos",)),
    ("notices", ("intake/notices",)),
    ("notes", ("intake/notes",)),
    ("notify", ("intake/notify",)),
    ("ci", ("GitHub/CI", "GitHub/Codecov")),
    ("receipts", ("intake/processed",)),
)
QUEUES = (
    "needs",
    "reviewed",
    "promos",
    "notices",
    "notes",
    "notify",
    "ci",
    "receipts",
    "sent",
    "archive",
)
QUEUE_TITLES = {
    "needs": "Needs you",
    "reviewed": "Reviewed (still in Inbox)",
    "promos": "Promotions",
    "notices": "Notices",
    "notes": "Notes to self",
    "notify": "Portfolio notifications",
    "ci": "CI & coverage",
    "receipts": "Processed",
    "sent": "Sent",
    "archive": "Everything else",
}


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


def queue_of(labels: list[str], in_inbox: bool) -> str:
    """Which review queue a message belongs in."""
    if in_inbox:
        return "needs"
    lowered = {label.lower() for label in labels}
    for queue, names in QUEUE_LABELS:
        if any(name.lower() in lowered for name in names):
            return queue
    if "\\sent" in lowered:
        return "sent"
    return "archive"


# -- IMAP response parsing -----------------------------------------------------------------


def parse_label_list(raw: str) -> list[str]:
    """Parse the parenthesised value of ``X-GM-LABELS`` (atoms and quoted strings)."""
    labels: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            j, out = i + 1, []
            while j < len(raw) and raw[j] != '"':
                if raw[j] == "\\" and j + 1 < len(raw):
                    j += 1
                out.append(raw[j])
                j += 1
            labels.append("".join(out))
            i = j + 1
        elif ch in " \t":
            i += 1
        else:
            j = i
            while j < len(raw) and raw[j] not in " \t":
                j += 1
            labels.append(raw[i:j])
            i = j
    return [_decode_mutf7(label) for label in labels if label]


def _decode_mutf7(value: str) -> str:
    """Decode IMAP modified UTF-7 (used for non-ASCII label names)."""
    if "&" not in value:
        return value

    def repl(match: re.Match) -> str:
        chunk = match.group(1)
        if not chunk:
            return "&"
        try:
            padded = chunk.replace(",", "/") + "=" * (-len(chunk) % 4)
            import base64  # noqa: PLC0415

            return base64.b64decode(padded).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return match.group(0)

    return re.sub(r"&([^-]*)-", repl, value)


_ATTR_RE = {
    "uid": re.compile(rb"\bUID (\d+)"),
    "msgid": re.compile(rb"X-GM-MSGID (\d+)"),
    "thrid": re.compile(rb"X-GM-THRID (\d+)"),
    "size": re.compile(rb"RFC822\.SIZE (\d+)"),
    "flags": re.compile(rb"FLAGS \(([^)]*)\)"),
    "date": re.compile(rb'INTERNALDATE "([^"]+)"'),
}


@dataclass
class Attrs:
    uid: int
    gm_msgid: str
    thread_id: str = ""
    size: int = 0
    unread: bool = False
    labels: list[str] = field(default_factory=list)
    internal_date: str = ""


def parse_attrs(line: bytes) -> Attrs | None:
    """Parse one batch ``FETCH (X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS ...)`` response line."""
    uid, msgid = _ATTR_RE["uid"].search(line), _ATTR_RE["msgid"].search(line)
    if not uid or not msgid:
        return None
    thr, size = _ATTR_RE["thrid"].search(line), _ATTR_RE["size"].search(line)
    flags, date = _ATTR_RE["flags"].search(line), _ATTR_RE["date"].search(line)
    labels_at = line.find(b"X-GM-LABELS (")
    labels: list[str] = []
    if labels_at >= 0:
        start = labels_at + len(b"X-GM-LABELS (")
        depth, end = 1, start
        in_quote = False
        while end < len(line) and depth:
            c = line[end : end + 1]
            if c == b'"' and line[end - 1 : end] != b"\\":
                in_quote = not in_quote
            elif not in_quote and c == b"(":
                depth += 1
            elif not in_quote and c == b")":
                depth -= 1
            end += 1
        labels = parse_label_list(line[start : end - 1].decode("utf-8", "replace"))
    return Attrs(
        uid=int(uid.group(1)),
        gm_msgid=msgid.group(1).decode(),
        thread_id=thr.group(1).decode() if thr else "",
        size=int(size.group(1)) if size else 0,
        unread=b"\\Seen" not in (flags.group(1) if flags else b""),
        labels=labels,
        internal_date=date.group(1).decode() if date else "",
    )


# -- storage -------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def upsert(conn: sqlite3.Connection, rec: dict) -> None:
    """Insert or update by ``gm_msgid``, never clobbering a local ``reviewed_at`` or summary."""
    now = _now()
    labels = rec.get("labels", [])
    values = {
        "gm_msgid": rec["gm_msgid"],
        "uid": rec["uid"],
        "thread_id": rec.get("thread_id", ""),
        "message_id": rec.get("message_id", ""),
        "sender": rec.get("sender", ""),
        "sender_name": rec.get("sender_name", ""),
        "recipients": rec.get("recipients", ""),
        "subject": rec.get("subject", ""),
        "sent_at": rec.get("sent_at"),
        "labels": json.dumps(labels),
        "in_inbox": int("\\inbox" in {x.lower() for x in labels}),
        "unread": int(bool(rec.get("unread"))),
        "size": int(rec.get("size", 0)),
        "excerpt": rec.get("excerpt", ""),
        "now": now,
    }
    conn.execute(
        """INSERT INTO messages (gm_msgid, uid, thread_id, message_id, sender, sender_name,
               recipients, subject, sent_at, labels, in_inbox, unread, size, excerpt,
               first_indexed_at, updated_at)
           VALUES (:gm_msgid, :uid, :thread_id, :message_id, :sender, :sender_name, :recipients,
               :subject, :sent_at, :labels, :in_inbox, :unread, :size, :excerpt, :now, :now)
           ON CONFLICT(gm_msgid) DO UPDATE SET uid=excluded.uid, labels=excluded.labels,
               in_inbox=excluded.in_inbox, unread=excluded.unread, gone=0,
               updated_at=excluded.updated_at,
               thread_id=COALESCE(NULLIF(excluded.thread_id, ''), thread_id),
               excerpt=CASE WHEN excluded.excerpt != '' THEN excluded.excerpt ELSE excerpt END,
               subject=CASE WHEN excluded.subject != '' THEN excluded.subject ELSE subject END,
               sender=CASE WHEN excluded.sender != '' THEN excluded.sender ELSE sender END,
               sender_name=CASE WHEN excluded.sender_name != ''
                                THEN excluded.sender_name ELSE sender_name END""",
        values,
    )


def update_state(conn: sqlite3.Connection, attrs: Attrs) -> bool:
    """Refresh labels/read state of a known message; returns True when it exists."""
    labels = json.dumps(attrs.labels)
    in_inbox = int("\\inbox" in {x.lower() for x in attrs.labels})
    cur = conn.execute(
        "UPDATE messages SET uid=?, labels=?, in_inbox=?, unread=?, gone=0, updated_at=? "
        "WHERE gm_msgid=? AND (labels != ? OR unread != ? OR gone != 0 OR uid != ?)",
        (
            attrs.uid,
            labels,
            in_inbox,
            int(attrs.unread),
            _now(),
            attrs.gm_msgid,
            labels,
            int(attrs.unread),
            attrs.uid,
        ),
    )
    return cur.rowcount > 0


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def mark_reviewed(conn: sqlite3.Connection, gm_msgid: str, reviewed: bool = True) -> bool:
    cur = conn.execute(
        "UPDATE messages SET reviewed_at=? WHERE gm_msgid=?",
        (_now() if reviewed else None, gm_msgid),
    )
    return cur.rowcount > 0


def set_summary(conn: sqlite3.Connection, gm_msgid: str, summary: str) -> None:
    conn.execute("UPDATE messages SET summary=? WHERE gm_msgid=?", (summary[:300], gm_msgid))


# -- queries -------------------------------------------------------------------------------


def row_queue(row: sqlite3.Row) -> str:
    """The queue a row belongs to (a reviewed message still in the Inbox is ``reviewed``)."""
    queue = queue_of(json.loads(row["labels"] or "[]"), bool(row["in_inbox"]))
    if queue == "needs" and "reviewed_at" in row.keys() and row["reviewed_at"]:  # noqa: SIM118
        return "reviewed"
    return queue


def fts_query(text: str) -> str:
    """Turn user text into a safe FTS5 query: every word a quoted prefix term, ANDed."""
    words = re.findall(r"[\w@.\-]+", text, flags=re.UNICODE)
    return " ".join(f'"{w.replace(chr(34), "")}"*' for w in words if w.strip(".-"))


def _has_label(names: tuple[str, ...], *, negate: bool = False) -> tuple[str, list[str]]:
    """SQL (and params) for 'carries any of these labels', matched exactly via json_each."""
    marks = ",".join("?" for _ in names)
    sql = f"EXISTS (SELECT 1 FROM json_each(messages.labels) WHERE lower(value) IN ({marks}))"  # noqa: S608
    return (f"NOT {sql}" if negate else sql), [n.lower() for n in names]


def queue_sql(queue: str) -> tuple[str, list[str]]:
    """The WHERE clause (and params) that selects one queue; mirrors :func:`queue_of`."""
    if queue == "needs":
        return "in_inbox = 1 AND reviewed_at IS NULL", []
    if queue == "reviewed":
        return "in_inbox = 1 AND reviewed_at IS NOT NULL", []
    clauses, params = ["in_inbox = 0"], []
    earlier: list[str] = []
    for name, labels in QUEUE_LABELS:
        if name == queue:
            sql, p = _has_label(labels)
            clauses.append(sql)
            params += p
            break
        earlier += list(labels)
    else:
        none_sql, none_p = _has_label(tuple(earlier))
        if queue == "sent":
            sent_sql, sent_p = _has_label(("\\sent",))
            return " AND ".join([*clauses, f"NOT {none_sql}", sent_sql]), [*none_p, *sent_p]
        # archive: nothing above, and not sent
        sent_sql, sent_p = _has_label(("\\sent",))
        return (
            " AND ".join([*clauses, f"NOT {none_sql}", f"NOT {sent_sql}"]),
            [*none_p, *sent_p],
        )
    if earlier:
        sql, p = _has_label(tuple(earlier))
        clauses.append(f"NOT {sql}")
        params += p
    return " AND ".join(clauses), params


_LIST_COLUMNS = (
    "messages.gm_msgid, messages.uid, messages.thread_id, messages.sender, "
    "messages.sender_name, messages.subject, messages.sent_at, messages.labels, "
    "messages.in_inbox, messages.unread, messages.size, messages.summary, "
    "messages.reviewed_at, substr(messages.excerpt, 1, 280) AS preview"
)


def list_messages(
    conn: sqlite3.Connection,
    *,
    queue: str | None = None,
    query: str = "",
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
) -> list[sqlite3.Row]:
    """Messages newest first, in one queue and/or matching a search (list columns only)."""
    where, params = ["messages.gone = 0"], []
    join = ""
    if query.strip():
        match = fts_query(query)
        if not match:
            return []
        join = "JOIN messages_fts ON messages_fts.rowid = messages.rowid"
        where.append("messages_fts MATCH ?")
        params.append(match)
    if queue:
        sql, p = queue_sql(queue)
        where.append(f"({sql})")
        params += p
    if unread_only:
        where.append("messages.unread = 1")
    return conn.execute(
        f"SELECT {_LIST_COLUMNS} FROM messages {join} WHERE {' AND '.join(where)} "  # noqa: S608
        "ORDER BY messages.sent_at DESC, messages.uid DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()


def queue_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """``{queue: {"total": n, "unread": n}}`` over live messages."""
    counts: dict[str, dict[str, int]] = {}
    for queue in QUEUES:
        sql, params = queue_sql(queue)
        row = conn.execute(
            f"SELECT COUNT(*) AS total, COALESCE(SUM(unread), 0) AS unread FROM messages "  # noqa: S608
            f"WHERE gone = 0 AND ({sql})",
            params,
        ).fetchone()
        counts[queue] = {"total": row["total"], "unread": int(row["unread"])}
    return counts


def get_message(conn: sqlite3.Connection, gm_msgid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM messages WHERE gm_msgid=?", (gm_msgid,)).fetchone()


# -- sync ----------------------------------------------------------------------------------


@dataclass
class SyncResult:
    seen: int = 0
    new: int = 0
    refreshed: int = 0
    gone: int = 0
    skipped_large: int = 0
    stopped_early: bool = False


def _imap_date(days_ago: int) -> str:
    when = datetime.now(UTC) - timedelta(days=days_ago)
    return when.strftime("%d-%b-%Y")


def _parse_message(payload: bytes, sr) -> dict:
    from intake.email_text import html_to_text  # noqa: PLC0415
    from intake.promo_digest import clean_body  # noqa: PLC0415

    msg = message_from_bytes(payload, policy=default_policy)
    name, addr = parseaddr(str(msg.get("From", "")))
    try:
        sent = (
            parsedate_to_datetime(str(msg.get("Date")))
            .astimezone(UTC)
            .isoformat(timespec="seconds")
        )
    except (TypeError, ValueError):
        sent = None
    recipients = ", ".join(
        a.lower() for _, a in getaddresses([*msg.get_all("To", []), *msg.get_all("Cc", [])]) if a
    )
    plain, html = sr.extract_message_bodies(msg)
    text = plain or (html_to_text(html) if html else "")
    return {
        "message_id": str(msg.get("Message-ID", "") or ""),
        "sender": addr.lower(),
        "sender_name": sr.decode_mime_header(name) if name else "",
        "recipients": recipients,
        "subject": sr.decode_mime_header(msg.get("Subject")),
        "sent_at": sent,
        "excerpt": "\n".join(clean_body(text))[:EXCERPT_CHARS],
    }


def _fetch_attrs(conn_imap, uids: list[int]) -> list[Attrs]:
    out: list[Attrs] = []
    for i in range(0, len(uids), UID_CHUNK):
        chunk = ",".join(str(u) for u in uids[i : i + UID_CHUNK])
        status, data = conn_imap.uid(
            "fetch", chunk, "(X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS RFC822.SIZE INTERNALDATE)"
        )
        if status != "OK":
            continue
        for item in data:
            line = item[0] if isinstance(item, tuple) else item
            if isinstance(line, bytes):
                attrs = parse_attrs(line)
                if attrs:
                    out.append(attrs)
    return out


def sync(
    sr,
    config,
    conn: sqlite3.Connection,
    *,
    since_days: int = 365,
    limit: int = 500,
    refresh_days: int = 45,
    time_budget_s: float | None = None,
) -> SyncResult:
    """Index new mail in All Mail and refresh labels/read state of recent mail (read-only)."""
    from intake.email_provider import select_mailbox  # noqa: PLC0415

    result = SyncResult()
    started = time.monotonic()
    with sr.open_imap_connection(config) as imap:
        status, _ = select_mailbox(imap, ALL_MAIL, readonly=True)
        if status != "OK":
            raise RuntimeError(f"cannot open {ALL_MAIL!r} read-only")
        _, validity = imap.response("UIDVALIDITY")
        validity_s = (validity[0] or b"").decode() if validity else ""
        if get_meta(conn, "uidvalidity") not in ("", validity_s):
            log.warning("UIDVALIDITY changed; re-indexing from scratch (reviewed flags are kept)")
            conn.execute("UPDATE messages SET uid = 0")
        set_meta(conn, "uidvalidity", validity_s)

        status, data = imap.uid("search", None, "SINCE", _imap_date(since_days))
        if status != "OK":
            raise RuntimeError("All Mail search failed")
        server_uids = sorted(int(x) for x in data[0].split())
        result.seen = len(server_uids)
        known = {
            r["uid"]: r["gm_msgid"] for r in conn.execute("SELECT uid, gm_msgid FROM messages")
        }
        new_uids = [u for u in reversed(server_uids) if u not in known][:limit]

        # 1. labels and read state for recent known mail (cheap: no bodies)
        floor = _recent_floor(server_uids, refresh_days, imap)
        recent = [u for u in server_uids if u in known and u >= floor]
        for attrs in _fetch_attrs(imap, recent[-2000:]):
            result.refreshed += int(update_state(conn, attrs))

        # 2. new mail: attributes in batches, then each body once
        for attrs in _fetch_attrs(imap, new_uids):
            if time_budget_s is not None and time.monotonic() - started > time_budget_s:
                result.stopped_early = True
                break
            rec = {
                "gm_msgid": attrs.gm_msgid, "uid": attrs.uid, "thread_id": attrs.thread_id,
                "labels": attrs.labels, "unread": attrs.unread, "size": attrs.size,
            }  # fmt: skip
            spec = (
                "(BODY.PEEK[])"
                if attrs.size <= MAX_FULL_FETCH_BYTES
                else ("(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])")
            )
            result.skipped_large += int(attrs.size > MAX_FULL_FETCH_BYTES)
            status, fetched = imap.uid("fetch", str(attrs.uid), spec)
            payload = sr.extract_fetch_payload(fetched) if status == "OK" else None
            if payload:
                rec.update(_parse_message(payload, sr))
            if not rec.get("sent_at") and attrs.internal_date:
                with contextlib.suppress(ValueError):
                    rec["sent_at"] = (
                        datetime.strptime(attrs.internal_date, "%d-%b-%Y %H:%M:%S %z")
                        .astimezone(UTC)
                        .isoformat(timespec="seconds")
                    )
            upsert(conn, rec)
            result.new += 1
            if result.new % 50 == 0:
                conn.commit()

        # 3. mail that vanished from the server inside the window
        live = set(server_uids)
        window_floor = min(server_uids) if server_uids else 0
        for uid, gm_msgid in known.items():
            if uid and uid >= window_floor and uid not in live:
                conn.execute(
                    "UPDATE messages SET gone=1, updated_at=? WHERE gm_msgid=?", (_now(), gm_msgid)
                )
                result.gone += 1
    set_meta(conn, "last_sync", _now())
    conn.commit()
    return result


def _recent_floor(server_uids: list[int], refresh_days: int, imap) -> int:
    """Lowest UID received within ``refresh_days`` (All Mail UIDs rise with arrival)."""
    status, data = imap.uid("search", None, "SINCE", _imap_date(refresh_days))
    if status != "OK" or not data or not data[0]:
        return server_uids[-1] if server_uids else 0
    return min(int(x) for x in data[0].split())
