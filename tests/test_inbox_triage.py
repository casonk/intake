from __future__ import annotations

import json
from datetime import UTC, datetime
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

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
        ("a@bank.com", "Your verification code", {"List_Unsubscribe": "<x>"}),
        ("noreply@x.com", "Security alert: new sign-in", {}),
        ("noreply@x.com", "Payment failed", {"Precedence": "bulk"}),
        # people and notes to self stay
        ("ckonzer@umich.edu", "Japan 2027!", {}),
        ("cason.konzer@bosch.com", "routing", {}),
    ],
)
def test_keeps_anything_that_could_need_a_human(sender, subject, extra):
    decision = decide(sender, subject, **extra)
    assert decision.kind == "keep" and decision.label is None


NOW = datetime(2026, 11, 24, tzinfo=UTC)
PULSE = frozenset({"pulse@mail.tokenmetrics.com"})


def test_newsletter_pipeline_senders_are_left_alone_while_in_its_window():
    recent = hdr(
        "Pulse <pulse@mail.tokenmetrics.com>",
        "Issue",
        List_Unsubscribe="<x>",
        Date="Sun, 22 Nov 2026 10:00:00 +0000",
    )
    assert classify_headers(recent, managed_senders=PULSE, now=NOW).kind == "keep"


def test_newsletter_issue_older_than_its_lookback_is_filed_like_bulk_mail():
    # The newsletter job only looks back 7 days, so an older issue can never be digested.
    old = hdr(
        "Pulse <pulse@mail.tokenmetrics.com>",
        "Issue",
        List_Unsubscribe="<x>",
        Date="Tue, 03 Nov 2026 10:00:00 +0000",
    )
    decision = classify_headers(old, managed_senders=PULSE, now=NOW)
    assert decision.kind == "promo" and decision.label


def test_unreadable_date_on_a_managed_sender_is_kept_to_be_safe():
    odd = hdr("Pulse <pulse@mail.tokenmetrics.com>", "Issue", List_Unsubscribe="<x>", Date="??")
    assert classify_headers(odd, managed_senders=PULSE, now=NOW).kind == "keep"


def test_skip_senders_are_always_kept_regardless_of_age():
    old = hdr(
        "list@example.org", "Sale", List_Unsubscribe="<x>", Date="Tue, 03 Nov 2025 10:00:00 +0000"
    )
    assert (
        classify_headers(old, skip_senders=frozenset({"list@example.org"}), now=NOW).kind == "keep"
    )


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


def test_github_actions_minutes_notices_are_filed_not_kept():
    # CI falls back to other runners, so these are informational despite "minutes"/"usage".
    for pct in (90, 100):
        decision = decide(
            "noreply@github.com",
            f"[GitHub] You have used {pct}% of the Actions minutes included for the account",
        )
        assert decision.kind == "notice" and decision.label


def test_notice_subject_regexes_are_configurable_and_win_over_keep_keywords():
    headers = hdr("a@b.com", "Your quarterly usage summary")
    assert classify_headers(headers).kind == "keep"
    custom = classify_headers(headers, notice_subject_res=(r"quarterly usage summary",))
    assert custom.kind == "notice"


def test_triage_prints_or_logs_each_decision_once(db, capsys, caplog):
    sr, config, _ = _env({1: _raw("a@promo.com", "Sale", List_Unsubscribe="<x>")}, [], [])
    with caplog.at_level("INFO", logger="intake.triage"):
        run_triage(sr, config, {"email": {}}, db, verbose=True)
    assert capsys.readouterr().out.count("uid=1") == 1
    assert not [r for r in caplog.records if "uid=1" in r.getMessage()], "printed lines are teed"
    caplog.clear()
    sr, config, _ = _env({2: _raw("a@promo.com", "Sale2", List_Unsubscribe="<x>")}, [], [])
    with caplog.at_level("INFO", logger="intake.triage"):
        run_triage(sr, config, {"email": {}}, db, verbose=False)
    assert len([r for r in caplog.records if "uid=2" in r.getMessage()]) == 1


@pytest.mark.parametrize(
    "subject",
    [
        "Konzer, interested in extending your Hakuba trip?",
        "144 resorts, no lift ticket.",
        "Important updates: your weekend promo code inside",
        "Offer expires tonight - limit one per customer",
    ],
)
def test_bulk_headers_override_soft_keywords_so_marketing_is_filed(subject):
    decision = decide("campaign@shop.example", subject, List_Unsubscribe="<x>")
    assert decision.kind == "promo" and decision.label


@pytest.mark.parametrize(
    "subject",
    [
        "Your order has shipped",
        "Booking confirmation for your stay",
        "Your itinerary for Tokyo",
        "Receipt for your purchase",
        "Payment failed - action required",
        "Your verification code",
    ],
)
def test_bulk_headers_never_override_real_transaction_or_security_subjects(subject):
    decision = decide("campaign@shop.example", subject, List_Unsubscribe="<x>")
    assert decision.kind == "keep", subject


def test_soft_keywords_still_keep_non_bulk_mail():
    assert decide("someone@example.org", "Your trip next week").kind == "keep"
    assert (
        decide("google-gemini-noreply@google.com", "Important updates in Gemini Apps").kind
        == "keep"
    )


def test_a_note_to_yourself_is_filed_unless_notes_are_turned_off():
    note = decide("me@example.com", "AI APPS", To="me@example.com")
    assert note.kind == "note" and note.label == "intake/notes"
    kept = classify_headers(
        hdr("me@example.com", "AI APPS", To="me@example.com"), own_addresses=OWN, file_notes=False
    )
    assert kept.kind == "keep"


def test_a_forward_to_yourself_is_a_note_but_a_reply_or_mail_to_others_is_not():
    own = frozenset({"me@example.com", "work@example.org"})

    def run(sender, subject, **extra):
        return classify_headers(hdr(sender, subject, **extra), own_addresses=own).kind

    assert run("work@example.org", "Fw: an article", To="me@example.com") == "note"
    # addressed to someone else too: a real message in a conversation, not a note
    assert run("work@example.org", "Plan", To="me@example.com, boss@corp.example") == "keep"
    assert run("work@example.org", "Plan", To="me@example.com", Cc="boss@corp.example") == "keep"
    assert run("work@example.org", "Re: Plan", To="me@example.com") == "keep"
    assert run("work@example.org", "Plan", To="me@example.com", In_Reply_To="<a@b>") == "keep"
    assert run("work@example.org", "Plan") == "keep", "no recipients: cannot prove it is a note"


# -- second stage: local model on what the header rules could not settle -------------------


def _model(kind: str, confidence: str = "high", summary: str = "A summary."):
    from intake.triage_llm import Verdict

    return Verdict(kind, confidence == "high", summary)


def _run_with_model(db, messages, verdict, **settings_extra):
    moved, read = [], []
    sr, config, conn = _env(messages, moved, read)
    sr.extract_message_bodies = lambda _m: ("Body text of the message that is long enough.", "")
    settings = {"email": {"triage": {"llm": True, **settings_extra}}}
    with patch("intake.triage_llm.judge", return_value=verdict) as judge:
        result = run_triage(sr, config, settings, db, verbose=False)
    return result, moved, judge


def test_model_may_file_a_clear_notice_or_marketing_message(db):
    msgs = {1: _raw("noreply@service.example", "Important: terms are changing")}
    result, moved, _ = _run_with_model(db, msgs, _model("notice"))
    assert moved == [(1, "intake/notices")] and result.by_kind["notice"] == 1
    msgs = {2: _raw("Airline@o.airline.example", "You earned status")}
    result, moved, _ = _run_with_model(db, msgs, _model("marketing"))
    assert moved == [(2, "intake/promos")]


@pytest.mark.parametrize(
    "verdict",
    [_model("personal"), _model("transactional"), _model("other"), _model("notice", "low"), None],
)
def test_model_never_files_transactions_or_unsure_or_missing_answers(db, verdict):
    msgs = {1: _raw("noreply@service.example", "Important changes to your account")}
    result, moved, _ = _run_with_model(db, msgs, verdict)
    assert moved == [] and result.by_kind["keep"] == 1


def test_model_is_not_asked_about_threads_or_money_or_security_subjects(db):
    threaded = {1: _raw("friend@example.org", "Re: plans", In_Reply_To="<a@b>")}
    _, moved, judge = _run_with_model(db, threaded, _model("marketing"))
    assert moved == [] and judge.call_count == 0
    hard = {2: _raw("billing@service.example", "Payment failed")}
    _, moved, judge = _run_with_model(db, hard, _model("marketing"))
    assert moved == [] and judge.call_count == 0


def test_model_is_off_by_default(db):
    moved, read = [], []
    sr, config, _ = _env({1: _raw("friend@example.org", "Japan 2027!")}, moved, read)
    with patch("intake.triage_llm.judge") as judge:
        run_triage(sr, config, {"email": {}}, db, verbose=False)
    assert judge.call_count == 0


def test_a_dead_model_is_asked_once_per_run_and_nothing_is_recorded(db, tmp_path):
    state = tmp_path / "verdicts.json"
    moved, read = [], []
    msgs = {
        1: _raw("noreply@x.example", "Important hello there"),
        2: _raw("noreply@y.example", "Important hello again"),
    }
    sr, config, _ = _env(msgs, moved, read)
    sr.extract_message_bodies = lambda _m: ("some body text here", "")
    settings = {"email": {"triage": {"llm": True}}}
    with patch("intake.triage_llm.judge", return_value=None) as judge:
        run_triage(sr, config, settings, db, verbose=False, state_path=state)
    assert judge.call_count == 1, "an unreachable model is tried once, not per message"
    assert not state.exists(), "no verdict is stored, so the next poll asks again"


def test_kept_verdicts_are_remembered_so_the_model_is_asked_once_per_message(db, tmp_path):
    state = tmp_path / "verdicts.json"
    moved, read = [], []
    msgs = {1: _raw("noreply@service.example", "Important changes to your account")}
    sr, config, _ = _env(msgs, moved, read)
    sr.extract_message_bodies = lambda _m: ("some body text here", "")
    settings = {"email": {"triage": {"llm": True}}}
    with patch("intake.triage_llm.judge", return_value=_model("personal")) as judge:
        run_triage(sr, config, settings, db, verbose=False, state_path=state)
        run_triage(sr, config, settings, db, verbose=False, state_path=state)
    assert judge.call_count == 1
    assert json.loads(state.read_text())["INBOX:1"]["kind"] == "personal"


def test_github_notification_mail_is_never_filed_as_a_promo_even_with_bulk_headers():
    # GitHub sends List-Unsubscribe; a PR comment needs you and must stay in the Inbox.
    from intake.inbox_triage import DEFAULT_SKIP_SENDERS

    headers = hdr(
        "Reviewer <notifications@github.com>",
        "Re: [casonk/auto-pass] fix: installer verifies the PIN gate (PR #12)",
        List_Unsubscribe="<x>",
    )
    assert classify_headers(headers, skip_senders=DEFAULT_SKIP_SENDERS).kind == "keep"


def test_run_triage_applies_the_default_skip_list(db):
    moved, read = [], []
    msgs = {
        1: _raw("notifications@github.com", "Re: [a/b] a change (PR #1)", List_Unsubscribe="<x>")
    }
    sr, config, _ = _env(msgs, moved, read)
    result = run_triage(sr, config, {"email": {}}, db, verbose=False)
    assert moved == [] and result.by_kind["keep"] == 1


# -- regression: shapes a year-wide dry run showed the model would have misfiled ------------


@pytest.mark.parametrize(
    ("sender", "subject", "extra"),
    [
        # a banker's personal replies
        ("Cindy.M.Sullivan@huntington.com", "RE: Follow up from Huntington, Cason Konzer.", {}),
        ("cindy.m.sullivan@huntington.com", "Following up", {}),
        # relocation agents and colleagues, threaded or not
        ("hiromi.shibata@ysm-intl.co.jp", "RE: Mr Konzer : your upcoming shipment to Japan", {}),
        ("kristine.walker@sirva.com", "return shipment", {"In_Reply_To": "<a@b>"}),
        ("yankun.li3@cn.bosch.com", "RE: Bosch - STX assignee service", {}),
        # role accounts answered by people
        ("support@openai.com", "Your request", {}),
        ("customerservice@email.progressive.com", "RE: Progressive Claims Service", {}),
        ("claims@insurer.example", "Your claim", {}),
        # a personal gmail address and a title/closing company
        ("janhayssold@gmail.com", "Hello", {}),
        ("lmallett@title-connect.com", "Closing documents", {}),
        ("secure@authentisign.com", "Please sign", {}),
        # an automated-looking sender that is replying inside a thread
        ("noreply@service.example", "Re: your ticket", {}),
        ("noreply@service.example", "Fw: forwarded", {}),
        ("noreply@service.example", "Update", {"References": "<a@b>"}),
    ],
)
def test_model_is_never_allowed_to_review_people_roles_or_threads(sender, subject, extra):
    from intake.inbox_triage import model_may_review

    assert not model_may_review(hdr(sender, subject, **extra)), sender


@pytest.mark.parametrize(
    "sender",
    [
        "DeltaAirLines@o.delta.com",
        "google-gemini-noreply@google.com",
        "google-api-dev-verification-reply+061750w7bqxmq1e@google.com",
        "noreply@news.bloomberg.com",
        "info@news.bydfi.com",
        "notifications@service.example",
    ],
)
def test_model_may_review_plainly_automated_senders(sender):
    from intake.inbox_triage import model_may_review

    assert model_may_review(hdr(sender, "Some automated update"))


def test_a_misjudging_model_cannot_file_a_banker_reply_or_a_support_thread(db):
    # Even a confident "marketing"/"notice" verdict is ignored for mail it may not review.
    msgs = {
        1: _raw("Cindy.M.Sullivan@huntington.com", "RE: Follow up from Huntington"),
        2: _raw("support@openai.com", "Your request"),
        3: _raw("hiromi.shibata@ysm-intl.co.jp", "RE: Mr Konzer : your upcoming shipment"),
    }
    result, moved, judge = _run_with_model(db, msgs, _model("marketing"))
    assert moved == [] and judge.call_count == 0
    assert result.by_kind["keep"] == 3


@pytest.mark.parametrize(
    "sender",
    ["support@openai.com", "team@startup.example", "hello@person.example", "info@bank.example"],
)
def test_role_accounts_without_bulk_headers_are_kept_not_filed_as_automated(sender):
    assert decide(sender, "Regarding your request").kind == "keep"


@pytest.mark.parametrize(
    "subject",
    ["Your 2025-2026 Mount Bohemia Tickets", "Your receipts are ready", "Booking confirmations"],
)
def test_plural_transaction_words_keep_mail_too(subject):
    # "Tickets" (a purchase) used to slip past a singular-only keyword and be filed as a notice.
    assert decide("noreply@interactiveticketing.example", subject).kind == "keep"


def test_a_message_returned_to_the_inbox_is_not_filed_again(db):
    # The review page's "Return to Inbox" gives the message a new UID, but triage already
    # recorded its Message-ID when it first filed it; that user choice must stand.
    from intake.db import record_email_receipt

    record_email_receipt(
        db, 1, "INBOX", "<Sale@x>", "Sale", "a@promo.example", "", 0, status="triage-promo"
    )
    moved, read = [], []
    msgs = {77: _raw("a@promo.example", "Sale", List_Unsubscribe="<x>")}  # new UID, same Message-ID
    sr, config, _ = _env(msgs, moved, read)
    result = run_triage(sr, config, {"email": {}}, db, verbose=False)
    assert moved == [] and result.by_kind["promo"] == 0
