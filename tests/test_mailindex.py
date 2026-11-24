from __future__ import annotations

import json
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from intake import mailindex as mi


@pytest.fixture()
def conn(tmp_path):
    c = mi.connect(tmp_path / "data" / "mailindex.db")
    yield c
    c.close()


# -- parsing -------------------------------------------------------------------------------


def test_parse_label_list_handles_atoms_quotes_and_escapes():
    assert mi.parse_label_list('\\Inbox GitHub/CI "intake/promos"') == [
        "\\Inbox",
        "GitHub/CI",
        "intake/promos",
    ]
    assert mi.parse_label_list('"My \\"quoted\\" label" Plain') == ['My "quoted" label', "Plain"]
    assert mi.parse_label_list("") == []
    assert mi.parse_label_list("&AOk-t&AOk-") == ["été"], "modified UTF-7 label names decode"


def test_parse_attrs_reads_gmail_extensions_and_read_state():
    line = (
        b"314 (X-GM-THRID 1882743624092861033 X-GM-MSGID 1882743624092861034 "
        b'X-GM-LABELS (\\Inbox GitHub/Codecov "a (b)") UID 90785 RFC822.SIZE 10335 '
        b'INTERNALDATE "24-Nov-2026 12:43:58 +0000" FLAGS (\\Seen))'
    )
    a = mi.parse_attrs(line)
    assert (a.uid, a.gm_msgid, a.thread_id, a.size) == (
        90785,
        "1882743624092861034",
        "1882743624092861033",
        10335,
    )
    assert a.labels == ["\\Inbox", "GitHub/Codecov", "a (b)"]
    assert a.unread is False
    unread = mi.parse_attrs(b"1 (X-GM-MSGID 5 X-GM-LABELS () UID 9 FLAGS ())")
    assert unread.unread is True and unread.labels == []
    assert mi.parse_attrs(b"garbage") is None


def test_queue_of_uses_first_matching_label_and_inbox_wins():
    assert mi.queue_of(["intake/promos"], False) == "promos"
    assert mi.queue_of(["intake/promos", "intake/processed"], False) == "promos"
    assert mi.queue_of(["GitHub/Codecov"], False) == "ci"
    assert mi.queue_of(["\\Sent"], False) == "sent"
    assert mi.queue_of(["Career/Job alerts"], False) == "archive"
    assert mi.queue_of(["intake/promos"], True) == "needs", "anything still in the Inbox needs you"


# -- storage and search --------------------------------------------------------------------


def _put(conn, gm, **kw):
    rec = {"gm_msgid": gm, "uid": int(gm), "sender": "a@b.example", "subject": "s", "labels": []}
    rec.update(kw)
    mi.upsert(conn, rec)


def test_search_finds_words_prefixes_senders_and_summaries(conn):
    _put(
        conn,
        "1",
        subject="Hakuba ski trip",
        excerpt="lift tickets for the season",
        sent_at="2026-11-02T00:00:00+00:00",
    )
    _put(
        conn,
        "2",
        subject="Invoice",
        sender="billing@shop.example",
        sent_at="2026-11-03T00:00:00+00:00",
    )
    mi.set_summary(conn, "2", "Quarterly hosting charge")
    assert [r["gm_msgid"] for r in mi.list_messages(conn, query="hakuba")] == ["1"]
    assert [r["gm_msgid"] for r in mi.list_messages(conn, query="tick")] == ["1"], "prefix"
    assert [r["gm_msgid"] for r in mi.list_messages(conn, query="billing@shop.example")] == ["2"]
    assert [r["gm_msgid"] for r in mi.list_messages(conn, query="hosting charge")] == ["2"]
    assert [r["gm_msgid"] for r in mi.list_messages(conn)] == ["2", "1"], "newest first"


@pytest.mark.parametrize(
    "hostile", ['"', "AND OR NOT", "a* NEAR(b c)", "'; DROP TABLE messages;--", "()", "-", ""]
)
def test_hostile_search_text_never_raises(conn, hostile):
    _put(conn, "1", subject="hello")
    mi.list_messages(conn, query=hostile)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_queues_partition_messages_and_counts_match(conn):
    _put(conn, "1", labels=["\\Inbox"], unread=True)
    _put(conn, "2", labels=["\\Inbox"])
    mi.mark_reviewed(conn, "2")
    _put(conn, "3", labels=["intake/promos"], unread=True)
    _put(conn, "4", labels=["intake/notices"])
    _put(conn, "5", labels=["GitHub/CI"])
    _put(conn, "6", labels=["intake/processed"])
    _put(conn, "7", labels=["\\Sent"])
    _put(conn, "8", labels=["Career/Alerts"])
    _put(conn, "9", labels=["intake/promos", "intake/processed"])
    ids = lambda q: sorted(r["gm_msgid"] for r in mi.list_messages(conn, queue=q))  # noqa: E731
    assert ids("needs") == ["1"] and ids("reviewed") == ["2"]
    assert ids("promos") == ["3", "9"] and ids("notices") == ["4"] and ids("ci") == ["5"]
    assert ids("receipts") == ["6"] and ids("sent") == ["7"] and ids("archive") == ["8"]
    counts = mi.queue_counts(conn)
    assert sum(c["total"] for c in counts.values()) == 9, "every message is in exactly one queue"
    assert counts["promos"] == {"total": 2, "unread": 1}


def test_upsert_keeps_local_review_state_summary_and_better_text(conn):
    _put(conn, "1", subject="Original", excerpt="body text", labels=["\\Inbox"])
    mi.mark_reviewed(conn, "1")
    mi.set_summary(conn, "1", "A summary")
    _put(conn, "1", subject="", excerpt="", labels=["intake/promos"])  # header-only refresh
    row = mi.get_message(conn, "1")
    assert (row["subject"], row["excerpt"], row["summary"]) == (
        "Original",
        "body text",
        "A summary",
    )
    assert row["reviewed_at"] is not None
    assert json.loads(row["labels"]) == ["intake/promos"] and row["in_inbox"] == 0


# -- sync against a fake Gmail IMAP -------------------------------------------------------


class FakeImap:
    def __init__(self, messages, validity=b"11"):
        self.messages = messages  # uid -> dict(msgid, labels, seen, raw, date)
        self.validity = validity
        self.fetched_bodies: list[int] = []
        self.commands: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def select(self, mailbox, readonly=False):
        self.commands.append(("select", mailbox, readonly))
        return ("OK", [b"1"])

    def response(self, name):
        return (name, [self.validity])

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "search":
            return ("OK", [b" ".join(str(u).encode() for u in sorted(self.messages))])
        uids = [int(x) for x in str(args[0]).split(",")]
        spec = args[1]
        if "X-GM-MSGID" in spec:
            out = []
            for u in uids:
                m = self.messages.get(u)
                if m:
                    labels = " ".join(
                        f'"{x}"' if any(c in x for c in ' ()"') else x for x in m["labels"]
                    )
                    flags = "\\Seen" if m["seen"] else ""
                    out.append(
                        f"1 (X-GM-THRID {m['msgid']} X-GM-MSGID {m['msgid']} X-GM-LABELS ({labels}) "
                        f"UID {u} RFC822.SIZE {len(m['raw']) if 'size' not in m else m['size']} "
                        f'INTERNALDATE "22-Nov-2026 10:00:00 +0000" FLAGS ({flags}))'.encode()
                    )
            return ("OK", out)
        self.fetched_bodies.append(uids[0])
        return ("OK", [self.messages[uids[0]]["raw"]])


def _raw(
    subject,
    sender="Shop <shop@example.com>",
    to="me@example.org",
    body="Body text here for the test.",
):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, to, subject
    m["Date"] = "Sun, 22 Nov 2026 10:00:00 +0000"
    m["Message-ID"] = f"<{subject}@x>"
    m.set_content(body)
    return m.as_bytes()


def _sr(fake):
    from intake.email_text import html_to_text  # noqa: F401

    return SimpleNamespace(
        open_imap_connection=lambda _c: fake,
        extract_fetch_payload=lambda fetched: fetched[0],
        extract_message_bodies=lambda msg: (msg.get_body(("plain",)).get_content(), ""),
        decode_mime_header=lambda v: str(v or ""),
    )


def _msg(msgid, labels, raw, seen=False, **kw):
    return {"msgid": str(msgid), "labels": labels, "seen": seen, "raw": raw, **kw}


def test_sync_indexes_new_mail_once_and_is_read_only(conn):
    fake = FakeImap(
        {
            101: _msg(1001, ["\\Inbox"], _raw("Japan 2027")),
            102: _msg(1002, ["intake/promos"], _raw("Winter sale"), seen=True),
        }
    )
    r = mi.sync(_sr(fake), SimpleNamespace(), conn, since_days=30)
    assert (r.seen, r.new) == (2, 2)
    row = mi.get_message(conn, "1001")
    assert (row["subject"], row["sender"], row["sender_name"], row["in_inbox"], row["unread"]) == (
        "Japan 2027",
        "shop@example.com",
        "Shop",
        1,
        1,
    )
    assert row["recipients"] == "me@example.org" and "Body text" in row["excerpt"]
    assert row["sent_at"].startswith("2026-11-22")
    assert ("select", mi.ALL_MAIL, True) in fake.commands, "All Mail must be opened read-only"
    assert not [c for c in fake.commands if c[0] in ("store", "copy", "expunge")]
    # second run: nothing new, no bodies refetched
    fake.fetched_bodies.clear()
    r2 = mi.sync(_sr(fake), SimpleNamespace(), conn, since_days=30)
    assert r2.new == 0 and fake.fetched_bodies == []


def test_sync_tracks_label_moves_read_state_and_vanished_mail(conn):
    fake = FakeImap(
        {
            101: _msg(1001, ["\\Inbox"], _raw("A")),
            102: _msg(1002, ["\\Inbox"], _raw("B")),
        }
    )
    mi.sync(_sr(fake), SimpleNamespace(), conn, since_days=30)
    mi.mark_reviewed(conn, "1001")
    # triage files A under a label and the user reads it; B is deleted on the server
    fake.messages[101]["labels"] = ["intake/promos"]
    fake.messages[101]["seen"] = True
    del fake.messages[102]
    r = mi.sync(_sr(fake), SimpleNamespace(), conn, since_days=30)
    a = mi.get_message(conn, "1001")
    assert json.loads(a["labels"]) == ["intake/promos"] and a["in_inbox"] == 0 and a["unread"] == 0
    assert a["reviewed_at"] is not None, "local review state survives a label move"
    assert mi.get_message(conn, "1002")["gone"] == 1 and r.gone == 1
    assert [m["gm_msgid"] for m in mi.list_messages(conn)] == ["1001"], "gone mail is hidden"


def test_large_messages_are_fetched_headers_only(conn):
    fake = FakeImap({101: _msg(1001, ["\\Inbox"], _raw("Big"), size=mi.MAX_FULL_FETCH_BYTES + 1)})
    r = mi.sync(_sr(fake), SimpleNamespace(), conn, since_days=30)
    assert r.skipped_large == 1
    spec = [c for c in fake.commands if c[0] == "fetch" and "HEADER.FIELDS" in str(c)]
    assert spec, "a large message must not be pulled in full"


def test_sync_honours_limit_and_time_budget(conn):
    fake = FakeImap({u: _msg(u * 10, ["\\Inbox"], _raw(f"M{u}")) for u in range(101, 109)})
    assert mi.sync(_sr(fake), SimpleNamespace(), conn, since_days=30, limit=3).new == 3
    assert mi.sync(
        _sr(fake), SimpleNamespace(), conn, since_days=30, time_budget_s=-1
    ).stopped_early


def test_uidvalidity_change_reindexes_without_losing_review_state(conn):
    fake = FakeImap({101: _msg(1001, ["\\Inbox"], _raw("A"))}, validity=b"11")
    mi.sync(_sr(fake), SimpleNamespace(), conn, since_days=30)
    mi.mark_reviewed(conn, "1001")
    fake2 = FakeImap({555: _msg(1001, ["\\Inbox"], _raw("A"))}, validity=b"12")
    mi.sync(_sr(fake2), SimpleNamespace(), conn, since_days=30)
    row = mi.get_message(conn, "1001")
    assert row["uid"] == 555 and row["reviewed_at"] is not None
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
