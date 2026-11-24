"""Email review pages, served by ``intake serve`` under ``/mail``.

A small server-rendered app over the local mail index (:mod:`intake.mailindex`): a "Needs you"
queue first, then everything triage filed, plus full-text search and a message view.  It never
talks to the mailbox itself in this module, and never renders mail as HTML: every field is
escaped and the body is shown as plain text.

Security model (the page exposes private mail and, later, accepts actions):

* only reachable through the server's existing Host-header guard (DNS-rebinding defence);
* every response carries a strict Content-Security-Policy and ``Cache-Control: no-store``;
* every POST needs a per-process CSRF token in a ``SameSite=Strict`` cookie *and* the same
  token in the form, plus an ``Origin`` that matches the ``Host``;
* remote content can never load (``default-src 'none'``), so a tracking pixel in a message
  cannot phone home even if its text were somehow rendered.
"""

from __future__ import annotations

import hmac
import html
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from intake import mailindex

PAGE_SIZE = 40
MAX_FORM_BYTES = 16 * 1024
CSRF_COOKIE = "intake_csrf"
CSP = (
    "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
_MSGID_RE = r"[0-9]{1,24}"


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _when(iso: str | None) -> str:
    return (iso or "")[:16].replace("T", " ")


def gmail_url(thread_id: str) -> str | None:
    """Link that opens the conversation in Gmail (the thread id, in hex)."""
    try:
        return f"https://mail.google.com/mail/u/0/#all/{int(thread_id):x}"
    except (TypeError, ValueError):
        return None


_STYLE = """
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 var(--font,system-ui,sans-serif)}
main{max-width:980px;margin:0 auto;padding:16px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
nav.queues{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}
nav.queues a{padding:5px 10px;border:1px solid var(--border);border-radius:999px;background:var(--surface);color:var(--text)}
nav.queues a.on{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
nav.queues b{color:var(--accent);font-weight:600}
form.search{display:flex;gap:8px;margin:0 0 14px}
form.search input[type=search]{flex:1;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text)}
button,.btn{padding:7px 12px;border:1px solid var(--border);border-radius:8px;background:var(--surface2);color:var(--text);cursor:pointer;font:inherit}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
ul.rows{list-style:none;margin:0;padding:0;border:1px solid var(--border);border-radius:10px;overflow:hidden}
ul.rows li{padding:10px 12px;border-bottom:1px solid var(--border);background:var(--surface)}
ul.rows li:last-child{border-bottom:0}ul.rows li.unread .subj{font-weight:700}
.top{display:flex;gap:8px;align-items:baseline;justify-content:space-between}
.from{color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%}
.date,.muted{color:var(--muted);font-size:13px}
.subj{display:block;margin-top:2px}.snippet{color:var(--muted);font-size:13px;margin-top:2px}
.chip{display:inline-block;padding:1px 8px;margin:2px 4px 2px 0;border:1px solid var(--border);border-radius:999px;font-size:12px;color:var(--muted)}
.chip.q{border-color:var(--accent);color:var(--accent)}
pre.body{white-space:pre-wrap;word-break:break-word;padding:12px;border:1px solid var(--border);border-radius:10px;background:var(--surface);font:13px/1.5 var(--mono,monospace)}
h1{font-size:20px;margin:0 0 12px}.card{padding:12px;border:1px solid var(--border);border-radius:10px;background:var(--surface);margin:0 0 12px}
.pager{display:flex;justify-content:space-between;margin:12px 0}.empty{padding:24px;text-align:center;color:var(--muted)}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
"""


class MailApp:
    """Routes ``/mail`` requests against the mail index."""

    def __init__(self, db_path: Path | str, *, actions=None) -> None:
        self.db_path = Path(db_path)
        self.csrf_token = secrets.token_urlsafe(32)
        self.actions = actions  # mailbox-changing actions; None = read-only (+ local review flag)

    # -- plumbing ------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return mailindex.connect(self.db_path)

    def _respond(
        self, status: int, body: str, *, headers: dict[str, str] | None = None
    ) -> Response:
        return Response(
            status,
            body.encode("utf-8"),
            headers={
                "Content-Security-Policy": CSP,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                **(headers or {}),
            },
        )

    def _cookie(self) -> dict[str, str]:
        return {
            "Set-Cookie": f"{CSRF_COOKIE}={self.csrf_token}; Path=/mail; HttpOnly; SameSite=Strict"
        }

    def _page(
        self, title: str, inner: str, conn: sqlite3.Connection, *, active: str = "", q: str = ""
    ) -> str:
        counts = mailindex.queue_counts(conn)
        chips = []
        for queue in mailindex.QUEUES:
            total = counts[queue]["total"]
            if queue in ("reviewed", "archive", "sent") and not total and queue != active:
                continue
            unread = counts[queue]["unread"]
            badge = f" <b>{unread}</b>" if unread else ""
            on = ' class="on"' if queue == active else ""
            chips.append(
                f'<a{on} href="/mail/q/{queue}">{esc(mailindex.QUEUE_TITLES[queue])} '
                f'<span class="muted">{total}</span>{badge}</a>'
            )
        last = mailindex.get_meta(conn, "last_sync")
        return (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta name=viewport content="width=device-width,initial-scale=1">'
            f"<title>{esc(title)} · Mail review</title>"
            '<link rel=stylesheet href="/_portfolio/theme.css">'
            f"<style>{_STYLE}</style>"
            '<script src="/_portfolio/chrome.js" defer></script></head><body><main>'
            f'<nav class="queues"><a href="/mail">Overview</a>{"".join(chips)}</nav>'
            '<form class="search" method="get" action="/mail/search">'
            f'<input type="search" name="q" value="{esc(q)}" placeholder="Search all mail" '
            'autocomplete="off"><button class="primary">Search</button></form>'
            f"{inner}"
            f'<p class="muted">Index last synced {esc(_when(last) or "never")} UTC · '
            "read-only copy of your mailbox</p></main></body></html>"
        )

    # -- rendering -----------------------------------------------------------------------

    @staticmethod
    def _row(row: sqlite3.Row, *, show_queue: bool = False) -> str:
        who = row["sender_name"] or row["sender"] or "(unknown sender)"
        snippet = row["summary"] or " ".join((row["preview"] or "").split())[:180]
        queue = (
            f'<span class="chip q">{esc(mailindex.QUEUE_TITLES[mailindex.row_queue(row)])}</span>'
            if show_queue
            else ""
        )
        cls = ' class="unread"' if row["unread"] else ""
        return (
            f"<li{cls}><div class=top><span class=from>{esc(who)}</span>"
            f"<span class=date>{esc(_when(row['sent_at']))}</span></div>"
            f'<a class=subj href="/mail/m/{esc(row["gm_msgid"])}">{queue}'
            f"{esc(row['subject'] or '(no subject)')}</a>"
            f"<div class=snippet>{esc(snippet)}</div></li>"
        )

    def _list(self, rows: list[sqlite3.Row], *, show_queue: bool = False) -> str:
        if not rows:
            return '<div class="card empty">Nothing here.</div>'
        return f'<ul class="rows">{"".join(self._row(r, show_queue=show_queue) for r in rows)}</ul>'

    @staticmethod
    def _pager(base: str, page: int, has_next: bool) -> str:
        sep = "&" if "?" in base else "?"
        prev = (
            f'<a href="{esc(base)}{sep}page={page - 1}">&larr; Newer</a>'
            if page > 1
            else "<span></span>"
        )
        nxt = (
            f'<a href="{esc(base)}{sep}page={page + 1}">Older &rarr;</a>'
            if has_next
            else "<span></span>"
        )
        return f'<div class="pager">{prev}{nxt}</div>' if page > 1 or has_next else ""

    # -- routes --------------------------------------------------------------------------

    def get(self, raw_path: str) -> Response:
        parts = urlsplit(raw_path)
        path, query = parts.path.rstrip("/") or "/mail", parse_qs(parts.query)
        raw_page = (query.get("page") or ["1"])[0]
        page = max(1, int(raw_page)) if raw_page.isdigit() and len(raw_page) < 6 else 1
        conn = self._connect()
        try:
            if path == "/mail":
                return self._overview(conn)
            if path.startswith("/mail/q/"):
                queue = path.removeprefix("/mail/q/")
                if queue not in mailindex.QUEUES:
                    return self._not_found(conn)
                return self._queue(conn, queue, page)
            if path == "/mail/search":
                return self._search(conn, (query.get("q") or [""])[0][:200], page)
            if path.startswith("/mail/m/"):
                return self._message(conn, path.removeprefix("/mail/m/"))
            return self._not_found(conn)
        finally:
            conn.close()

    def _not_found(self, conn: sqlite3.Connection) -> Response:
        return self._respond(
            404, self._page("Not found", '<div class="card empty">Not found.</div>', conn)
        )

    def _overview(self, conn: sqlite3.Connection) -> Response:
        counts = mailindex.queue_counts(conn)
        needs = mailindex.list_messages(conn, queue="needs", limit=PAGE_SIZE)
        cards = "".join(
            f'<a class="card" href="/mail/q/{q}" style="display:block"><b>{esc(mailindex.QUEUE_TITLES[q])}</b>'
            f'<div class="muted">{counts[q]["total"]} message(s), {counts[q]["unread"]} unread</div></a>'
            for q in mailindex.QUEUES
            if q not in ("needs", "reviewed") and counts[q]["total"]
        )
        inner = (
            f'<h1>Needs you <span class="muted">({counts["needs"]["total"]})</span></h1>'
            f"{self._list(needs)}"
            f"{self._pager('/mail/q/needs', 1, counts['needs']['total'] > PAGE_SIZE)}"
            f'<h1 style="margin-top:22px">Filed</h1>{cards or self._list([])}'
        )
        return self._respond(
            200, self._page("Overview", inner, conn, active=""), headers=self._cookie()
        )

    def _queue(self, conn: sqlite3.Connection, queue: str, page: int) -> Response:
        rows = mailindex.list_messages(
            conn, queue=queue, limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE
        )
        inner = (
            f"<h1>{esc(mailindex.QUEUE_TITLES[queue])}</h1>{self._list(rows[:PAGE_SIZE])}"
            f"{self._pager(f'/mail/q/{queue}', page, len(rows) > PAGE_SIZE)}"
        )
        return self._respond(
            200,
            self._page(mailindex.QUEUE_TITLES[queue], inner, conn, active=queue),
            headers=self._cookie(),
        )

    def _search(self, conn: sqlite3.Connection, text: str, page: int) -> Response:
        rows = (
            mailindex.list_messages(
                conn, query=text, limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE
            )
            if text.strip()
            else []
        )
        label = f"Results for “{esc(text)}”" if text.strip() else "Search"
        base = f"/mail/search?q={quote(text)}"
        inner = (
            f"<h1>{label}</h1>{self._list(rows[:PAGE_SIZE], show_queue=True)}"
            f"{self._pager(base, page, len(rows) > PAGE_SIZE)}"
        )
        return self._respond(200, self._page("Search", inner, conn, q=text), headers=self._cookie())

    def _message(self, conn: sqlite3.Connection, msgid: str) -> Response:
        row = mailindex.get_message(conn, msgid) if re.fullmatch(_MSGID_RE, msgid) else None
        if row is None or row["gone"]:
            return self._not_found(conn)
        queue = mailindex.row_queue(row)
        labels = "".join(
            f'<span class="chip">{esc(x)}</span>'
            for x in _json_list(row["labels"])
            if not x.startswith("\\")
        )
        link = gmail_url(row["thread_id"])
        reviewed = row["reviewed_at"] is not None
        review_form = (
            f'<form method="post" action="/mail/m/{esc(msgid)}/review">'
            f'<input type="hidden" name="csrf" value="{esc(self.csrf_token)}">'
            f'<input type="hidden" name="reviewed" value="{0 if reviewed else 1}">'
            f'<button class="primary">{"Mark as not reviewed" if reviewed else "Mark reviewed"}</button></form>'
        )
        inner = (
            f'<p><a href="/mail/q/{queue}">&larr; {esc(mailindex.QUEUE_TITLES[queue])}</a></p>'
            f'<div class="card"><h1>{esc(row["subject"] or "(no subject)")}</h1>'
            f"<div><b>{esc(row['sender_name'] or row['sender'])}</b> "
            f'<span class="muted">&lt;{esc(row["sender"])}&gt;</span></div>'
            f'<div class="muted">To: {esc(row["recipients"])}</div>'
            f'<div class="muted">{esc(_when(row["sent_at"]))} UTC · {esc(row["size"])} bytes'
            f"{' · unread' if row['unread'] else ''}{' · reviewed' if reviewed else ''}</div>"
            f'<div><span class="chip q">{esc(mailindex.QUEUE_TITLES[queue])}</span>{labels}</div></div>'
            + (
                f'<div class="card"><b>Summary</b><div>{esc(row["summary"])}</div></div>'
                if row["summary"]
                else ""
            )
            + f'<div class="actions">{review_form}'
            + (
                f'<a class="btn" href="{esc(link)}" target="_blank" rel="noopener noreferrer">Open in Gmail</a>'
                if link
                else ""
            )
            + "</div>"
            + (
                f'<pre class="body">{esc(row["excerpt"])}</pre>'
                if row["excerpt"]
                else '<div class="card empty">No text indexed (large or attachment-only message).</div>'
            )
            + '<p class="muted">Showing the indexed text excerpt only; open the message in Gmail for '
            "the original, attachments and images.</p>"
        )
        return self._respond(
            200, self._page(row["subject"] or "Message", inner, conn), headers=self._cookie()
        )

    # -- POST ----------------------------------------------------------------------------

    def post(self, raw_path: str, body: bytes, *, headers, cookie_header: str = "") -> Response:
        """Handle a form POST: CSRF and Origin are checked before anything is touched."""
        conn = self._connect()
        try:
            refusal = self._check_post(headers, cookie_header, body)
            if refusal is not None:
                return self._respond(
                    refusal[0],
                    self._page("Refused", f'<div class="card">{esc(refusal[1])}</div>', conn),
                )
            form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            path = urlsplit(raw_path).path.rstrip("/")
            match = re.fullmatch(rf"/mail/m/({_MSGID_RE})/(review)", path)
            if not match:
                return self._not_found(conn)
            msgid, action = match.groups()
            if mailindex.get_message(conn, msgid) is None:
                return self._not_found(conn)
            if action == "review":
                mailindex.mark_reviewed(conn, msgid, form.get("reviewed") == "1")
                conn.commit()
            return Response(
                303, b"", headers={"Location": f"/mail/m/{msgid}", "Cache-Control": "no-store"}
            )
        finally:
            conn.close()

    def _check_post(self, headers, cookie_header: str, body: bytes) -> tuple[int, str] | None:
        if len(body) > MAX_FORM_BYTES:
            return 413, "Request too large."
        if (headers.get("Content-Type") or "").split(";")[
            0
        ].strip() != "application/x-www-form-urlencoded":
            return 415, "Unsupported content type."
        origin = headers.get("Origin") or ""
        host = headers.get("Host") or ""
        if not origin or urlsplit(origin).netloc != host:
            return 403, "Cross-origin request refused."
        jar = SimpleCookie()
        try:
            jar.load(cookie_header)
        except Exception:  # noqa: BLE001
            return 403, "Bad cookie."
        cookie_token = jar[CSRF_COOKIE].value if CSRF_COOKIE in jar else ""
        form_token = (parse_qs(body.decode("utf-8", "replace")).get("csrf") or [""])[0]
        ok = all(
            hmac.compare_digest(candidate.encode(), self.csrf_token.encode())
            for candidate in (cookie_token, form_token)
        )
        return (
            None
            if ok
            else (403, "Missing or invalid security token. Reload the page and try again.")
        )


def _json_list(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []
