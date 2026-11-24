from __future__ import annotations

from email.message import Message
from types import SimpleNamespace

import pytest

from intake.db import init_db
from intake.inbox_triage import classify_headers, run_triage

OWN = frozenset({"me@example.com"})


@pytest.fixture()
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def hdr(sender: str, subject: str, **extra: str) -> Message:
    msg = Message()
    msg["From"] = sender
    msg["Subject"] = subject
    for key, value in extra.items():
        msg[key.replace("_", "-")] = value
    return msg


def decide(sender: str, subject: str, **extra: str):
    return classify_headers(hdr(sender, subject, **extra), own_addresses=OWN)


@pytest.mark.parametrize(
    ("sender", "subject", "extra", "kind"),
    [
        # bulk mail is filed as promos
        (
            "Booking <email.campaign@sg.booking.com>",
            "Rate Moment: hotels",
            {"List_Unsubscribe": "<x>"},
            "promo",
        ),
        ("D-Structure <online@d-structure.com>", "Winter sale", {"List_Id": "<l.x>"}, "promo"),
        ("a@b.com", "Newsletter", {"Precedence": "bulk"}, "promo"),
        # automated no-reply with no bulk headers is a notice
        ("noreply@spotify.com", "Your account data is ready to download", {}, "notice"),
        ("google-gemini-noreply@google.com", "Updates to Gemini apps", {}, "notice"),
        ("a@b.com", "Daily summary", {"Auto_Submitted": "auto-generated"}, "notice"),
        # portfolio-generated mail, by header or by subject prefix
        (
            "me@example.com",
            "[portfolio] Digest: 1 notification",
            {"X_Portfolio_Service": "shock-relay"},
            "notify",
        ),
        ("me@example.com", "Magneto complete: Some Movie", {}, "notify"),
        ("me@example.com", "Job guard: 1 dropped", {}, "notify"),
    ],
)
def test_files_clearly_non_actionable_mail(sender, subject, extra, kind):
    decision = decide(sender, subject, **extra)
    assert decision.kind == kind and decision.label


@pytest.mark.parametrize(
    ("sender", "subject", "extra"),
    [
        # transactional or alerting subjects stay even from bulk senders
        (
            "seasonpass@hakubavalley.com",
            "WEB SHOP QR code for Your Ordered Ticket",
            {"List_Unsubscribe": "<x>"},
        ),
        ("noreply@github.com", "[GitHub] You have used 100% of the Actions minutes", {}),
        ("a@bank.com", "Your verification code", {"List_Unsubscribe": "<x>"}),
        ("noreply@x.com", "Security alert: new sign-in", {}),
        ("noreply@x.com", "Payment failed", {"Precedence": "bulk"}),
        # people and notes to self stay
        ("ckonzer@umich.edu", "Japan 2027!", {}),
        ("cason.konzer@bosch.com", "routing", {}),
        ("me@example.com", "AI APPS", {}),
    ],
)
def test_keeps_anything_that_could_need_a_human(sender, subject, extra):
    decision = decide(sender, subject, **extra)
    assert decision.kind == "keep" and decision.label is None


def test_newsletter_pipeline_senders_are_left_alone():
    decision = classify_headers(
        hdr("Pulse <pulse@mail.tokenmetrics.com>", "Issue", List_Unsubscribe="<x>"),
        managed_senders=frozenset({"pulse@mail.tokenmetrics.com"}),
    )
    assert decision.kind == "keep"


def test_custom_labels_override_defaults():
    decision = classify_headers(
        hdr("a@b.com", "Sale", List_Unsubscribe="<x>"), labels={"promo": "Mail/Promos"}
    )
    assert decision.label == "Mail/Promos"


# -- run_triage ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, messages):
        self.messages = messages
        self.expunged = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def select(self, mailbox, readonly=False):
        return ("OK", [b"1"])

    def uid(self, command, *args):
        if command == "search":
            return ("OK", [b" ".join(str(u).encode() for u in self.messages)])
        return ("OK", [self.messages[int(args[0])]])

    def expunge(self):
        self.expunged = True
        return ("OK", [])


def _raw(sender, subject, **extra):
    msg = hdr(sender, subject, Message_ID=f"<{subject}@x>", **extra)
    return msg.as_bytes()


def _env(messages, moved, read):
    conn = _Conn(messages)
    sr = SimpleNamespace(
        open_imap_connection=lambda _c: conn,
        build_search_terms=lambda **kw: ["ALL"],
        parse_int_uid_list=lambda data: [int(x) for x in data[0].split()],
        extract_fetch_payload=lambda fetched: fetched[0],
        decode_mime_header=lambda v: str(v or ""),
        mark_message_read=lambda _c, uid: read.append(uid),
        move_message=lambda _c, uid, label: moved.append((uid, label)),
    )
    config = SimpleNamespace(
        imap=SimpleNamespace(mailboxes=["INBOX"], search_charset=None, username="me@example.com")
    )
    return sr, config, conn


def test_run_triage_moves_unread_records_moved_mail_only(db):
    from intake.db import is_email_processed

    moved, read = [], []
    messages = {
        1: _raw("a@promo.com", "Winter sale", List_Unsubscribe="<x>"),
        2: _raw("noreply@spotify.com", "Data ready"),
        3: _raw("friend@example.org", "Japan 2027!"),
    }
    sr, config, conn = _env(messages, moved, read)
    result = run_triage(sr, config, {"email": {}}, db, verbose=False)

    assert dict(result.by_kind) == {"promo": 1, "notice": 1, "keep": 1}
    assert sorted(moved) == [(1, "intake/promos"), (2, "intake/notices")]
    assert read == [], "triage must keep mail unread"
    assert conn.expunged
    assert is_email_processed(db, 1, "INBOX") and is_email_processed(db, 2, "INBOX")
    # A kept message is not recorded, so the receipt loop can still process it later.
    assert not is_email_processed(db, 3, "INBOX")


def test_run_triage_dry_run_changes_nothing(db):
    from intake.db import is_email_processed

    moved, read = [], []
    sr, config, conn = _env({1: _raw("a@promo.com", "Sale", List_Unsubscribe="<x>")}, moved, read)
    result = run_triage(sr, config, {"email": {}}, db, dry_run=True, verbose=False)
    assert result.by_kind["promo"] == 1 and moved == [] and not conn.expunged
    assert not is_email_processed(db, 1, "INBOX")


def test_run_triage_skips_messages_intake_already_judged(db):
    from intake.db import record_email_receipt

    record_email_receipt(db, 1, "INBOX", "<m>", "Sale", "a@promo.com", "", 0, status="unmatched")
    moved, read = [], []
    sr, config, _ = _env({1: _raw("a@promo.com", "Sale", List_Unsubscribe="<x>")}, moved, read)
    result = run_triage(sr, config, {"email": {}}, db, verbose=False)
    assert result.examined == 0 and moved == []
