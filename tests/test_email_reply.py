"""Reply handling for intake's "Receipt processed" emails.

A reply can change a receipt (merchant, total, purchaser, card) or ask for a file link, so what
matters is what it is allowed to change, that intake never acts on its own mail, that a message is
never applied twice, and that a bad message is counted and skipped rather than aborting the run.
The IMAP side and shock-relay are fakes, the sender of link replies is stubbed, and the receipt
database is a real SQLite file under ``tmp_path``.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from types import SimpleNamespace

import pytest

from intake import email_reply as er
from intake.db import (
    get_receipt,
    init_db,
    is_email_message_id_processed,
    query_receipt_updates,
    record_email_receipt,
    record_receipt_update,
    upsert_receipt,
)

FILENAME = "email_100_INBOX.txt"


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    upsert_receipt(
        conn,
        {
            "filename": FILENAME,
            "scan_date": "2026-04-24",
            "merchant": "costco",
            "total": 66.00,
            "category": "general",
            "receipt_dir": "email/general",
            "raw_text": "raw",
            "processed_by": "email-parse",
        },
    )
    yield conn
    conn.close()


def make_message(
    subject="Re: Receipt processed: Costco",
    body="hello",
    *,
    message_id="<reply1@example.com>",
    in_reply_to="<orig@example.com>",
    references=None,
    sender="Me <me@example.com>",
    headers=None,
    html=None,
):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 01 Jan 2026 10:00:00 +0000"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return msg


def parsed(**kwargs):
    return message_from_bytes(make_message(**kwargs).as_bytes(), policy=default_policy)


class SR:
    """The slice of shock-relay the reply code uses."""

    def __init__(self, conn=None):
        self.conn = conn

    @staticmethod
    def decode_mime_header(value):
        return str(value or "")

    @staticmethod
    def extract_message_bodies(msg):
        plain = msg.get_body(preferencelist=("plain",))
        html = msg.get_body(preferencelist=("html",))
        return (
            plain.get_content() if plain is not None else None,
            html.get_content() if html is not None else None,
        )

    @staticmethod
    def extract_fetch_payload(data):
        return data  # the fetch stubs below return the raw bytes directly

    def open_imap_connection(self, config):
        @contextmanager
        def cm():
            yield self.conn

        return cm()


# --- mailbox list -------------------------------------------------------------------------


def test_configured_mailboxes_come_first_then_defaults_without_duplicates():
    got = er._configured_reply_mailboxes({"reply_mailboxes": ["Custom", " Sent ", "Custom"]})
    assert got[:2] == ["Custom", "Sent"]
    assert got.count("Sent") == 1
    assert "[Gmail]/All Mail" in got
    assert er._configured_reply_mailboxes({})[0] == "[Gmail]/All Mail"


# --- is this a reply to us? ---------------------------------------------------------------


def test_a_reply_needs_the_subject_fragment():
    assert er._is_intake_reply_message(parsed(subject="Re: RECEIPT PROCESSED: x"), {})
    assert not er._is_intake_reply_message(parsed(subject="Re: lunch plans"), {})


@pytest.mark.parametrize("subject", ["Re: Receipt processed: a", "FWD: Receipt processed: a"])
def test_a_reply_prefix_is_enough(subject):
    msg = parsed(subject=subject, in_reply_to=None)
    assert er._is_intake_reply_message(msg, {})


def test_without_a_prefix_a_thread_header_is_needed():
    plain = parsed(subject="Receipt processed: a", in_reply_to=None)
    assert not er._is_intake_reply_message(plain, {})
    assert er._is_intake_reply_message(parsed(subject="Receipt processed: a"), {})
    threaded = parsed(subject="Receipt processed: a", in_reply_to=None, references="<r@x>")
    assert er._is_intake_reply_message(threaded, {})


@pytest.mark.parametrize("intent", ["notify", "reply-link", " NOTIFY "])
def test_intake_never_processes_its_own_generated_mail(intent):
    own = parsed(
        headers={"X-Portfolio-Service": "intake", "X-Crew-Chief-Intent": intent},
    )
    assert er._is_intake_generated_message(own)
    assert not er._is_intake_reply_message(own, {})


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Portfolio-Service": "intake", "X-Crew-Chief-Intent": "digest"},
        {"X-Portfolio-Service": "clockwork", "X-Crew-Chief-Intent": "notify"},
        {"X-Portfolio-Service": "intake"},
        {},
    ],
)
def test_only_the_exact_service_and_intent_pair_counts_as_generated(headers):
    msg = parsed(headers=headers)
    assert not er._is_intake_generated_message(msg)
    assert er._is_intake_reply_message(msg, {})


def test_a_quoted_notification_is_recognised_in_the_body():
    quoted = "Receipt processed\nmerchant: costco\n"
    assert er._contains_intake_notification_quote(quoted)
    assert er._contains_intake_notification_quote("Receipt processed\nfilename: a.txt")
    assert not er._contains_intake_notification_quote("Receipt processed, thanks")
    assert not er._contains_intake_notification_quote("merchant: costco")
    assert not er._contains_intake_notification_quote("   ")
    assert not er._contains_intake_notification_quote(None)


# --- reading the message ------------------------------------------------------------------


def test_message_text_joins_plain_and_html_parts():
    msg = parsed(body="plain part", html="<p>html <b>part</b></p>")
    text = er._message_text(msg, SR())
    assert "plain part" in text
    assert "html" in text and "part" in text


def test_combining_body_parts_skips_blank_ones():
    assert er._combine_message_body_text("  ", None) == ""
    assert er._combine_message_body_text(" a ", "") == "a"
    assert er._combine_message_body_text(None, "<p>only html</p>") == "only html"


def test_the_new_reply_text_stops_at_the_quoted_original():
    cases = {
        "total: 5\n\n> quoted line\nmore": "total: 5",
        "total: 5\nOn Mon, 1 Jan 2026, Me <me@x> wrote:\nold": "total: 5",
        "total: 5\n-----Original Message-----\nold": "total: 5",
        "total: 5\nFrom: someone@x\nSent: today\nold": "total: 5",
        "\n\n  keep me  \n\n": "keep me",
        "a\r\nb\rc": "a\nb\nc",
        "   \n  ": "",
        "> only a quote": "",
    }
    for given, expected in cases.items():
        assert er._trim_reply_note_text(given) == expected, given


def test_the_filename_comes_from_a_filename_line_with_directories_stripped():
    assert er._extract_receipt_filename_from_text("hi\nfilename: a_b.txt\nbye") == "a_b.txt"
    assert er._extract_receipt_filename_from_text("filename: ../../etc/passwd") == "passwd"
    assert er._extract_receipt_filename_from_text("FILENAME:   x.txt  ") == "x.txt"


def test_a_file_line_needs_an_extension():
    assert er._extract_receipt_filename_from_text("file: receipt_1.txt") == "receipt_1.txt"
    assert er._extract_receipt_filename_from_text("file: /var/x/receipt_1.txt") == "receipt_1.txt"
    assert er._extract_receipt_filename_from_text("file: noextension") is None
    assert er._extract_receipt_filename_from_text("file: dir/") is None
    assert er._extract_receipt_filename_from_text("nothing here") is None


def test_a_quoted_filename_line_is_not_used():
    assert er._extract_receipt_filename_from_text("> filename: a.txt") is None


def test_the_filename_prefers_the_header_then_the_receipt_dir_then_the_body():
    sr = SR()
    header = parsed(
        headers={"X-Intake-Receipt-Filename": "/x/y/from_header.txt"}, body="file: b.txt"
    )
    assert er._extract_receipt_filename_from_message(header, sr) == "from_header.txt"

    by_dir = parsed(headers={"X-Intake-Receipt-Dir": "email/general/receipt.txt"})
    assert er._extract_receipt_filename_from_message(by_dir, sr) == "receipt.txt"

    dir_without_extension = parsed(
        headers={"X-Intake-Receipt-Dir": "email/general"}, body="filename: from_body.txt"
    )
    assert er._extract_receipt_filename_from_message(dir_without_extension, sr) == "from_body.txt"

    assert er._extract_receipt_filename_from_message(parsed(body="nothing"), sr) is None


def test_reply_message_ids_are_deduplicated_in_header_order():
    msg = parsed(in_reply_to="<a@x>", references="<b@x> <a@x> <c@x>")
    assert er._extract_reply_message_ids(msg) == ["<a@x>", "<b@x>", "<c@x>"]
    assert er._extract_reply_message_ids(parsed(in_reply_to=None)) == []


def test_the_references_header_keeps_the_thread_and_adds_this_message_once():
    assert (
        er._reply_references_header(parsed(references="<a@x>   <b@x>", message_id="<m@x>"))
        == "<a@x> <b@x> <m@x>"
    )
    assert (
        er._reply_references_header(parsed(references="<a@x> <m@x>", message_id="<m@x>"))
        == "<a@x> <m@x>"
    )
    assert er._reply_references_header(parsed(message_id="<m@x>")) == "<m@x>"
    bare = EmailMessage()
    assert er._reply_references_header(bare) is None


def test_the_thread_token_is_the_replied_to_id_or_else_the_message_id():
    assert er._reply_thread_token(parsed(in_reply_to="<orig@x>"), "<mine@x>") == "<orig@x>"
    assert er._reply_thread_token(parsed(in_reply_to=None), "<mine@x>") == "<mine@x>"


# --- structured updates -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Card Last4", "card_last4"),
        ("card", "card_last4"),
        ("card_last4", "card_last4"),
        ("MERCHANT", "merchant"),
        (" Total ", "total"),
    ],
)
def test_field_labels_are_normalised(label, expected):
    assert er._normalize_reply_field(label) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("66.40", 66.40),
        ("$1,234.56", 1234.56),
        ("1234", 1234.0),
        ("-5.50", -5.5),
        # a total typed with one decimal place is still the amount the person meant
        ("12.5", 12.5),
        ("0.5", 0.5),
        ("-5.5", -5.5),
        ("  7.1  ", 7.1),
        ("12.345", 12.35),
    ],
)
def test_a_total_keeps_every_cent_that_was_typed(raw, expected):
    assert er._parse_reply_update_value("total", raw) == expected


def test_an_unparseable_total_is_rejected_not_zeroed():
    assert er._parse_reply_update_value("total", "about ten") is ...


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        ("merchant", "  Rock Creek Liquor ", "rock creek liquor"),
        ("purchaser", " Alice ", "Alice"),
        ("purchaser", "NONE", None),
        ("purchaser", "clear", None),
        ("card_last4", "Blank", None),
        ("card_last4", "**** 1234", "1234"),
        ("card_last4", "12345", ...),
        ("card_last4", "12", ...),
        ("currency", "usd", "USD"),
        ("currency", "U.S.D.", "USD"),
        ("currency", "dollars", ...),
        ("merchant", "none", "none"),
        ("category", "food", ...),
        ("total", "   ", None),
    ],
)
def test_structured_values(field, raw, expected):
    assert er._parse_reply_update_value(field, raw) == expected


def test_updates_are_split_from_the_free_text_that_remains():
    note = "Merchant = Costco\nthis is wrong\n\ntotal -> 12.5\ncard last4: 4242\nthanks!"
    updates, remaining = er._extract_reply_updates(note)
    assert updates == {"merchant": "costco", "total": 12.5, "card_last4": "4242"}
    assert remaining == "this is wrong\n\nthanks!"


def test_clear_lines_blank_a_field_and_are_not_free_text():
    updates, remaining = er._extract_reply_updates("clear purchaser\nClear Card Last4\n")
    assert updates == {"purchaser": None, "card_last4": None}
    assert remaining == ""


def test_a_line_with_a_bad_value_stays_as_free_text_and_changes_nothing():
    updates, remaining = er._extract_reply_updates("card: 12\ntotal: lots")
    assert updates == {}
    assert remaining == "card: 12\ntotal: lots"


def test_only_known_fields_are_ever_updated():
    updates, remaining = er._extract_reply_updates(
        "category: tax\nfilename: other.txt\nprocessed_by: me\nmerchant: ok"
    )
    assert updates == {"merchant": "ok"}
    assert "category: tax" in remaining


def test_blank_lines_collapse_and_edges_are_trimmed():
    _, remaining = er._extract_reply_updates("\n\nfirst\n\n\n\nsecond\n\n")
    assert remaining == "first\n\nsecond"


# --- asking for the file ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("Please attach the OCR and Image", True),
        ("send me the receipt file", True),
        ("can you share the photo?", True),
        ("link the image", True),
        ("attach it", False),
        ("the image is blurry", False),
        ("", False),
        (None, False),
    ],
)
def test_a_file_request_needs_an_action_word_and_a_target_word(note, expected):
    assert er._reply_requests_file_link(note) is expected


GENERATED = "Snowbridge receipt link requested\nReceipt: a.txt\nLink: https://files.example/a"


def test_the_link_reply_intake_sends_is_not_treated_as_a_new_request():
    assert er._looks_like_generated_file_link_reply(GENERATED)
    assert not er._reply_requests_file_link(GENERATED + "\nplease send the file")
    assert not er._looks_like_generated_file_link_reply("Snowbridge receipt link requested\nx")
    assert not er._looks_like_generated_file_link_reply("")
    assert not er._looks_like_generated_file_link_reply("hello\nReceipt: a\nLink: b")


def test_a_link_already_sent_for_this_thread_is_remembered(db):
    assert not er._reply_link_already_recorded(db, FILENAME, "<t@x>", "<m@x>")
    assert not er._reply_link_already_recorded(db, FILENAME, "", "")
    record_receipt_update(
        db, FILENAME, source="email-reply-link", note="<t@x>", changes={"snowbridge_link": {}}
    )
    assert er._reply_link_already_recorded(db, FILENAME, "<t@x>", "<m@x>")
    assert er._reply_link_already_recorded(db, FILENAME, "", "<t@x>")
    assert not er._reply_link_already_recorded(db, "other.txt", "<t@x>", "")
    assert not er._reply_link_already_recorded(db, FILENAME, "<other@x>", "<m@x>")


@pytest.fixture
def link_sender(monkeypatch):
    sent = []

    def fake(settings, **kwargs):
        sent.append(kwargs)
        return "https://files.example/receipt"

    monkeypatch.setattr("intake.notify.send_receipt_file_link_gmail_reply", fake)
    return sent


def call_link(db, **overrides):
    args = dict(
        filename=FILENAME,
        note_text="please send the receipt file",
        recipient="me@example.com",
        subject="Re: Receipt processed",
        reply_token="<t@x>",
        reply_message_id="<m@x>",
        references="<a@x> <m@x>",
    )
    args.update(overrides)
    return er._maybe_send_reply_link(db, {}, **args)


def test_no_file_request_sends_nothing(db, link_sender):
    assert call_link(db, note_text="thanks") is None
    assert link_sender == []


def test_a_file_request_sends_one_link_and_records_it(db, link_sender):
    assert call_link(db) == "link-sent"
    assert len(link_sender) == 1
    assert link_sender[0]["recipient"] == "me@example.com"
    assert link_sender[0]["in_reply_to"] == "<m@x>"
    assert link_sender[0]["references"] == "<a@x> <m@x>"
    history = query_receipt_updates(db, filename=FILENAME)
    assert history[0]["source"] == "email-reply-link"
    assert history[0]["changes"]["snowbridge_link"]["new"] == "https://files.example/receipt"
    assert history[0]["changes"]["reply_to"]["new"] == "me@example.com"


def test_the_same_request_is_never_answered_twice(db, link_sender):
    assert call_link(db) == "link-sent"
    assert call_link(db) == "link-already-sent"
    assert len(link_sender) == 1


def test_a_link_for_an_unknown_receipt_is_refused(db, link_sender):
    with pytest.raises(KeyError):
        call_link(db, filename="missing.txt")
    assert link_sender == []


def test_a_blank_message_id_sends_no_in_reply_to(db, link_sender):
    call_link(db, reply_message_id="")
    assert link_sender[0]["in_reply_to"] is None


# --- applying a reply ---------------------------------------------------------------------


def test_applying_to_an_unknown_receipt_raises_key_error(db):
    with pytest.raises(KeyError):
        er._apply_reply_to_receipt(db, {}, filename="missing.txt", note_text="total: 1")


def test_a_free_text_reply_to_an_unknown_receipt_is_refused_and_leaves_no_audit_row(db):
    with pytest.raises(KeyError):
        er._apply_reply_to_receipt(db, {}, filename="missing.txt", note_text="just a comment")
    assert query_receipt_updates(db, filename="missing.txt") == []


def test_an_empty_note_changes_and_records_nothing(db):
    assert er._apply_reply_to_receipt(db, {}, filename=FILENAME, note_text="") == "reply-empty"
    assert query_receipt_updates(db, filename=FILENAME) == []


def test_structured_updates_are_applied_and_audited(db):
    status = er._apply_reply_to_receipt(
        db, {}, filename=FILENAME, note_text="total: 12.5\nmerchant: Trader Joe's"
    )
    row = get_receipt(db, FILENAME)
    assert status == "reply-applied"
    assert row["total"] == 12.5
    assert row["merchant"] == "trader joe's"
    assert query_receipt_updates(db, filename=FILENAME)[0]["source"] == "email-reply"


def test_free_text_is_recorded_for_review_without_touching_the_receipt(db):
    status = er._apply_reply_to_receipt(
        db, {}, filename=FILENAME, note_text="this looks wrong to me"
    )
    assert status == "reply-recorded"
    assert get_receipt(db, FILENAME)["total"] == 66.0
    history = query_receipt_updates(db, filename=FILENAME)
    assert history[0]["source"] == "email-reply-note"
    assert history[0]["changes"]["email_reply"]["new"] == "this looks wrong to me"


def test_a_requested_change_that_changes_nothing_is_recorded_not_applied(db):
    status = er._apply_reply_to_receipt(db, {}, filename=FILENAME, note_text="merchant: costco")
    assert status == "reply-recorded"
    history = query_receipt_updates(db, filename=FILENAME)
    assert history[0]["changes"]["requested_updates"]["new"] == {"merchant": "costco"}


def test_updates_and_free_text_in_one_reply_are_both_kept(db):
    status = er._apply_reply_to_receipt(
        db, {}, filename=FILENAME, note_text="total: 70\nplease double check"
    )
    assert status == "reply-applied"
    sources = {h["source"] for h in query_receipt_updates(db, filename=FILENAME)}
    assert sources == {"email-reply", "email-reply-note"}


def test_recording_a_note_with_nothing_to_record_is_a_no_op(db):
    er._record_reply_note(db, filename=FILENAME, note_text="")
    assert query_receipt_updates(db, filename=FILENAME) == []


def test_the_sidecar_is_synced_only_when_a_receipt_root_exists(db, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("intake.cli._sync_receipt_sidecar", lambda root, row: calls.append(root))
    er._apply_reply_to_receipt(db, {}, filename=FILENAME, note_text="total: 1")
    er._apply_reply_to_receipt(
        db, {"receipt_dir": str(tmp_path / "missing")}, filename=FILENAME, note_text="total: 2"
    )
    assert calls == []

    root = tmp_path / "receipts"
    root.mkdir()
    er._apply_reply_to_receipt(
        db, {"receipt_dir": str(root)}, filename=FILENAME, note_text="total: 3"
    )
    assert calls == [root]


def test_a_failing_sidecar_sync_never_undoes_the_update(db, tmp_path, monkeypatch):
    root = tmp_path / "receipts"
    root.mkdir()

    def boom(root, row):
        raise OSError("disk full")

    monkeypatch.setattr("intake.cli._sync_receipt_sidecar", boom)
    status = er._apply_reply_to_receipt(
        db, {"receipt_dir": str(root)}, filename=FILENAME, note_text="total: 9"
    )
    assert status == "reply-applied"
    assert get_receipt(db, FILENAME)["total"] == 9.0


# --- resolving which receipt a reply is about ---------------------------------------------


def test_the_filename_in_the_reply_text_wins_without_any_lookup(monkeypatch):
    monkeypatch.setattr(
        er, "_find_message_in_mailboxes", lambda *a, **k: pytest.fail("must not search")
    )
    got = er._resolve_reply_receipt_filename(
        None, None, SR(), {}, parsed(), "total: 1\nfilename: direct.txt"
    )
    assert got == "direct.txt"


def original_bytes(filename="orig_receipt.txt"):
    return make_message(
        subject="Receipt processed: Costco",
        in_reply_to=None,
        headers={"X-Intake-Receipt-Filename": filename},
    ).as_bytes()


@pytest.fixture
def mailbox_lookup(monkeypatch):
    state = SimpleNamespace(found={}, payload=original_bytes(), searched=[], fetched=[])

    def find(conn, sr, mailboxes, message_id, *, recent_limit, search_charset):
        state.searched.append((list(mailboxes), message_id, recent_limit, search_charset))
        return state.found.get(message_id)

    def fetch(conn, mailbox, uid, *, readonly):
        state.fetched.append((mailbox, uid, readonly))
        return state.payload

    monkeypatch.setattr(er, "_find_message_in_mailboxes", find)
    monkeypatch.setattr(er, "_fetch_message_by_uid", fetch)
    return state


CONFIG = SimpleNamespace(imap=SimpleNamespace(search_charset="UTF-8"))


def test_the_receipt_is_found_through_the_original_notification(mailbox_lookup):
    mailbox_lookup.found["<orig@example.com>"] = {"mailbox": "Sent", "uid": 7}
    got = er._resolve_reply_receipt_filename(
        None, CONFIG, SR(), {"email": {"limit": 500}}, parsed(), "total: 1"
    )
    assert got == "orig_receipt.txt"
    assert mailbox_lookup.fetched == [("Sent", 7, True)]
    mailboxes, message_id, recent_limit, charset = mailbox_lookup.searched[0]
    assert message_id == "<orig@example.com>"
    assert recent_limit == 100  # clamped to at most 100 recent messages
    assert charset == "UTF-8"
    assert "[Gmail]/All Mail" in mailboxes


@pytest.mark.parametrize(("configured", "expected"), [(5, 10), (50, 50), (5000, 100)])
def test_the_search_window_is_clamped(mailbox_lookup, configured, expected):
    er._resolve_reply_receipt_filename(
        None, CONFIG, SR(), {"email": {"limit": configured}}, parsed(), "x"
    )
    assert mailbox_lookup.searched[0][2] == expected


def test_each_referenced_message_is_tried_until_one_resolves(mailbox_lookup):
    mailbox_lookup.found["<second@x>"] = {"mailbox": "Sent", "uid": 3}
    msg = parsed(in_reply_to="<first@x>", references="<first@x> <second@x>")
    got = er._resolve_reply_receipt_filename(None, CONFIG, SR(), {}, msg, "x")
    assert got == "orig_receipt.txt"
    assert [s[1] for s in mailbox_lookup.searched] == ["<first@x>", "<second@x>"]


def test_an_original_without_a_payload_or_filename_resolves_nothing(mailbox_lookup):
    mailbox_lookup.found["<orig@example.com>"] = {"mailbox": "Sent", "uid": 7}
    mailbox_lookup.payload = b""
    assert er._resolve_reply_receipt_filename(None, CONFIG, SR(), {}, parsed(), "x") is None

    mailbox_lookup.payload = make_message(in_reply_to=None, body="no file mentioned").as_bytes()
    assert er._resolve_reply_receipt_filename(None, CONFIG, SR(), {}, parsed(), "x") is None


def test_a_reply_that_references_nothing_findable_resolves_nothing(mailbox_lookup):
    assert er._resolve_reply_receipt_filename(None, CONFIG, SR(), {}, parsed(), "x") is None
    assert (
        er._resolve_reply_receipt_filename(None, CONFIG, SR(), {}, parsed(in_reply_to=None), "x")
        is None
    )


# --- searching the mailbox ----------------------------------------------------------------


def test_the_reply_search_returns_the_newest_uids_up_to_the_limit(monkeypatch):
    seen = {}

    def build_search_terms(*, subject_contains, since_days):
        seen["terms"] = (subject_contains, since_days)
        return ["SUBJECT", subject_contains]

    monkeypatch.setitem(
        sys.modules,
        "common",
        SimpleNamespace(
            build_search_terms=build_search_terms,
            parse_int_uid_list=lambda data: [int(x) for x in data[0].split()],
        ),
    )

    class Conn:
        def uid(self, command, charset, *terms):
            seen["call"] = (command, charset, terms)
            return "OK", [b"3 9 1 7"]

    got = er._search_reply_uids(
        Conn(), CONFIG, subject_fragment="Receipt processed", since_days=14, limit=2
    )
    assert got == [9, 7]
    assert seen["terms"] == ("Receipt processed", 14)
    assert seen["call"] == ("search", "UTF-8", ("SUBJECT", "Receipt processed"))


def test_a_failed_search_finds_nothing(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "common",
        SimpleNamespace(build_search_terms=lambda **k: [], parse_int_uid_list=lambda d: [1]),
    )

    class Conn:
        def uid(self, *a):
            return "NO", []

    assert er._search_reply_uids(Conn(), CONFIG, subject_fragment="x", since_days=1, limit=5) == []


# --- the whole run ------------------------------------------------------------------------


class Result:
    def __init__(self):
        self.errors = 0
        self.skipped = 0
        self.reply_unmatched = 0
        self.reply_applied = 0
        self.reply_processed = 0

    def counts(self):
        return dict(vars(self))


class World:
    """Mailboxes of raw messages, indexed by (mailbox, uid)."""

    def __init__(self, mailboxes):
        self.mailboxes = mailboxes
        self.fetched = []
        self.selected = []


@pytest.fixture
def run_replies(db, monkeypatch, link_sender):
    def run(mailboxes, *, settings=None, since_days=7, limit=50, verbose=False, select_fails=()):
        world = World(mailboxes)
        sr = SR(conn=object())

        def select(conn, mailbox, *, readonly):
            world.selected.append(mailbox)
            if mailbox in select_fails:
                raise OSError("imap down")
            return ("OK", []) if mailbox in mailboxes else ("NO", [])

        def search(conn, config, *, subject_fragment, since_days, limit):
            return sorted(mailboxes.get(world.selected[-1], {}), reverse=True)[:limit]

        def fetch(conn, mailbox, uid, *, readonly):
            world.fetched.append((mailbox, uid))
            payload = mailboxes[mailbox][uid]
            if isinstance(payload, BaseException):
                raise payload
            return payload

        monkeypatch.setattr(er, "_load_shock_relay", lambda path: sr)
        monkeypatch.setattr(er, "_select_mailbox", select)
        monkeypatch.setattr(er, "_search_reply_uids", search)
        monkeypatch.setattr(er, "_fetch_message_by_uid", fetch)
        monkeypatch.setattr(er, "_find_message_in_mailboxes", lambda *a, **k: None)
        result = Result()
        settings = settings or {"email": {"reply_mailboxes": ["INBOX"]}}
        email_cfg = {"allowed_addresses": ["me@example.com"], **settings.get("email", {})}
        er._process_reply_messages(
            CONFIG,
            {**settings, "email": email_cfg},
            db,
            result,
            since_days=since_days,
            limit=limit,
            verbose=verbose,
        )
        return result, world

    return run


def reply(body, **kw):
    kw.setdefault("message_id", "<r1@example.com>")
    return make_message(body=body, **kw).as_bytes()


def email_status(db, uid, mailbox="INBOX"):
    row = db.execute(
        "SELECT status FROM email_receipts WHERE uid = ? AND mailbox = ?", (uid, mailbox)
    ).fetchone()
    return row[0] if row else None


def test_a_reply_with_updates_is_applied_and_recorded(db, run_replies):
    result, _ = run_replies({"INBOX": {5: reply(f"total: 12.5\nfilename: {FILENAME}")}})

    assert get_receipt(db, FILENAME)["total"] == 12.5
    assert email_status(db, 5) == "reply-applied"
    assert (result.reply_applied, result.reply_processed, result.errors) == (1, 1, 0)
    assert is_email_message_id_processed(db, "<r1@example.com>")


def test_a_free_text_reply_is_recorded_not_applied(db, run_replies):
    result, _ = run_replies({"INBOX": {5: reply(f"looks off\nfilename: {FILENAME}")}})
    assert email_status(db, 5) == "reply-recorded"
    assert (result.reply_applied, result.reply_processed) == (0, 1)
    assert get_receipt(db, FILENAME)["total"] == 66.0


def test_a_reply_that_names_no_receipt_is_kept_as_unmatched(db, run_replies):
    result, _ = run_replies({"INBOX": {5: reply("total: 1")}})
    assert email_status(db, 5) == "reply-unmatched"
    assert result.reply_unmatched == 1
    assert get_receipt(db, FILENAME)["total"] == 66.0


def test_a_reply_naming_a_receipt_that_is_not_in_the_database_is_unmatched(db, run_replies):
    result, _ = run_replies({"INBOX": {5: reply("total: 1\nfilename: ghost.txt")}})
    assert email_status(db, 5) == "reply-unmatched"
    assert result.reply_unmatched == 1


def test_messages_that_are_not_replies_to_intake_are_ignored_entirely(db, run_replies):
    unrelated = reply("hi", subject="Re: lunch")
    own = reply(
        f"total: 1\nfilename: {FILENAME}",
        headers={"X-Portfolio-Service": "intake", "X-Crew-Chief-Intent": "notify"},
    )
    result, _ = run_replies({"INBOX": {5: unrelated, 6: own}})
    assert email_status(db, 5) is None and email_status(db, 6) is None
    assert get_receipt(db, FILENAME)["total"] == 66.0
    assert result.counts() == Result().counts()


def test_an_empty_payload_is_skipped(db, run_replies):
    result, _ = run_replies({"INBOX": {5: b""}})
    assert result.skipped == 1


def test_a_message_id_already_processed_is_not_applied_again(db, run_replies):
    record_email_receipt(db, 1, "INBOX", "<r1@example.com>", "s", "me", "d", 0, "reply-applied")
    result, _ = run_replies({"INBOX": {5: reply(f"total: 99\nfilename: {FILENAME}")}})
    assert get_receipt(db, FILENAME)["total"] == 66.0
    assert result.skipped == 1
    assert email_status(db, 5) is None


def test_a_uid_already_processed_is_not_applied_again(db, run_replies):
    record_email_receipt(db, 5, "INBOX", "<older@example.com>", "s", "me", "d", 0, "reply-applied")
    result, _ = run_replies({"INBOX": {5: reply(f"total: 99\nfilename: {FILENAME}")}})
    assert get_receipt(db, FILENAME)["total"] == 66.0
    assert result.skipped == 1


def test_an_already_processed_reply_can_still_get_its_requested_link(db, run_replies, link_sender):
    record_email_receipt(db, 1, "INBOX", "<r1@example.com>", "s", "me", "d", 0, "reply-applied")
    run_replies({"INBOX": {5: reply(f"please send the receipt file\nfilename: {FILENAME}")}})
    assert len(link_sender) == 1


def test_a_failing_link_reply_is_counted_and_does_not_stop_the_run(db, run_replies, monkeypatch):
    def boom(settings, **kwargs):
        raise RuntimeError("gmail down")

    monkeypatch.setattr("intake.notify.send_receipt_file_link_gmail_reply", boom)
    result, _ = run_replies(
        {"INBOX": {5: reply(f"please send the receipt file\nfilename: {FILENAME}")}}
    )
    assert result.errors == 1
    assert email_status(db, 5) == "reply-recorded"


def test_a_sent_link_is_reflected_in_the_status(db, run_replies, link_sender):
    result, _ = run_replies(
        {"INBOX": {5: reply(f"total: 20\nplease attach the receipt file\nfilename: {FILENAME}")}}
    )
    assert email_status(db, 5) == "reply-applied-link-sent"
    assert result.reply_applied == 1 and result.reply_processed == 1
    assert len(link_sender) == 1


def test_one_bad_message_is_counted_and_the_rest_are_still_processed(db, run_replies):
    mailboxes = {
        "INBOX": {
            5: OSError("connection reset"),
            6: reply(f"total: 8\nfilename: {FILENAME}"),
        }
    }
    result, world = run_replies(mailboxes)
    assert result.errors == 1
    assert get_receipt(db, FILENAME)["total"] == 8.0
    assert email_status(db, 6) == "reply-applied"
    assert [uid for _, uid in world.fetched] == [6, 5]  # newest first, both attempted


def test_a_failing_message_is_reported_when_verbose(db, run_replies, capsys):
    run_replies({"INBOX": {5: OSError("connection reset")}}, verbose=True)
    assert "ERROR reply uid=5 mailbox=INBOX: connection reset" in capsys.readouterr().out


def test_an_unreachable_mailbox_is_counted_and_the_next_one_is_tried(db, run_replies):
    result, world = run_replies(
        {
            "Broken": {1: b""},
            "INBOX": {5: reply(f"total: 31\nfilename: {FILENAME}")},
        },
        settings={"email": {"reply_mailboxes": ["Broken", "INBOX"]}},
        select_fails={"Broken"},
    )
    assert result.errors == 1
    assert get_receipt(db, FILENAME)["total"] == 31.0


def test_a_mailbox_that_cannot_be_selected_or_is_empty_is_skipped(db, run_replies):
    result, world = run_replies(
        {"Empty": {}, "INBOX": {5: reply(f"total: 4\nfilename: {FILENAME}")}},
        settings={"email": {"reply_mailboxes": ["Missing", "Empty", "INBOX"]}},
    )
    assert result.errors == 0
    assert get_receipt(db, FILENAME)["total"] == 4.0
    assert world.fetched == [("INBOX", 5)]


def test_the_search_stops_after_the_all_mail_folder(db, run_replies):
    result, world = run_replies(
        {
            "[Gmail]/All Mail": {1: reply(f"total: 2\nfilename: {FILENAME}")},
            "Sent": {2: reply(f"total: 3\nfilename: {FILENAME}", message_id="<r2@x>")},
        },
        settings={"email": {"reply_mailboxes": ["[Gmail]/All Mail", "Sent"]}},
    )
    assert "Sent" not in world.selected
    assert get_receipt(db, FILENAME)["total"] == 2.0


def test_verbose_mode_reports_progress_without_changing_results(db, run_replies, capsys):
    result, _ = run_replies({"INBOX": {5: reply(f"total: 6\nfilename: {FILENAME}")}}, verbose=True)
    out = capsys.readouterr().out
    assert "Found 1 reply candidate(s) in INBOX" in out
    assert "reply → reply-applied" in out
    assert result.reply_applied == 1


def test_verbose_mode_explains_each_skip(db, run_replies, capsys):
    record_email_receipt(db, 1, "INBOX", "<r1@example.com>", "s", "me", "d", 0, "reply-applied")
    run_replies(
        {
            "Broken": {1: b""},
            "INBOX": {
                5: reply(f"total: 9\nfilename: {FILENAME}"),
                4: reply("no receipt named", message_id="<r4@x>"),
                3: reply("x", subject="Re: lunch", message_id="<r3@x>"),
            },
        },
        settings={"email": {"reply_mailboxes": ["Broken", "INBOX"]}},
        select_fails={"Broken"},
        verbose=True,
    )
    out = capsys.readouterr().out
    assert "WARNING reply mailbox Broken" in out
    assert "skip (reply message-id already processed)" in out
    assert "could not resolve receipt filename" in out


def test_uid_already_processed_is_reported_when_verbose(db, run_replies, capsys):
    record_email_receipt(db, 5, "INBOX", "<older@x>", "s", "me", "d", 0, "reply-applied")
    run_replies({"INBOX": {5: reply(f"total: 9\nfilename: {FILENAME}")}}, verbose=True)
    assert "skip (reply already processed)" in capsys.readouterr().out


def test_verbose_mode_reports_a_receipt_missing_from_the_database(db, run_replies, capsys):
    run_replies({"INBOX": {5: reply("total: 1\nfilename: ghost.txt")}}, verbose=True)
    assert "reply skipped: receipt not found in DB: ghost.txt" in capsys.readouterr().out


def test_a_second_run_does_not_resend_a_link_and_says_so_when_verbose(
    db, run_replies, link_sender, capsys
):
    body = f"please send the receipt file\nfilename: {FILENAME}"
    run_replies({"INBOX": {5: reply(body)}}, verbose=True)
    first = capsys.readouterr().out
    assert "reply → reply-recorded-link-sent" in first

    run_replies({"INBOX": {6: reply(body)}}, verbose=True)
    second = capsys.readouterr().out
    assert "skip (reply link already sent)" in second
    assert len(link_sender) == 1


def test_an_already_processed_reply_that_sends_its_link_says_so_when_verbose(
    db, run_replies, link_sender, capsys
):
    record_email_receipt(db, 1, "INBOX", "<r1@example.com>", "s", "me", "d", 0, "reply-applied")
    run_replies({"INBOX": {5: reply(f"send the receipt file\nfilename: {FILENAME}")}}, verbose=True)
    assert "reply → link-sent for " + FILENAME in capsys.readouterr().out


def test_a_link_failure_is_reported_when_verbose(db, run_replies, monkeypatch, capsys):
    def boom(settings, **kwargs):
        raise RuntimeError("gmail down")

    monkeypatch.setattr("intake.notify.send_receipt_file_link_gmail_reply", boom)
    body = f"send the receipt file\nfilename: {FILENAME}"
    run_replies({"INBOX": {5: reply(body)}}, verbose=True)
    assert f"WARNING reply-link for {FILENAME}: gmail down" in capsys.readouterr().out

    record_email_receipt(db, 9, "INBOX", "<r7@example.com>", "s", "me", "d", 0, "reply-applied")
    run_replies({"INBOX": {7: reply(body, message_id="<r7@example.com>")}}, verbose=True)
    assert f"WARNING reply-link for {FILENAME}: gmail down" in capsys.readouterr().out


def test_a_file_line_with_only_a_slash_names_nothing():
    assert er._extract_receipt_filename_from_text("file: /") is None


def test_a_sidecar_module_that_cannot_be_imported_does_not_undo_the_update(
    db, tmp_path, monkeypatch
):
    root = tmp_path / "receipts"
    root.mkdir()
    monkeypatch.setitem(sys.modules, "intake.cli", None)  # makes `from intake.cli import ...` fail
    status = er._apply_reply_to_receipt(
        db, {"receipt_dir": str(root)}, filename=FILENAME, note_text="total: 11"
    )
    assert status == "reply-applied"
    assert get_receipt(db, FILENAME)["total"] == 11.0


# --- only the configured addresses may drive replies ---------------------------------------


def only(*addresses):
    return {"email": {"reply_mailboxes": ["INBOX"], "allowed_addresses": list(addresses)}}


def test_a_reply_from_an_address_that_is_not_listed_changes_nothing_and_sends_nothing(
    db, run_replies, link_sender
):
    body = f"total: 1\nplease send the receipt file\nfilename: {FILENAME}"
    result, _ = run_replies(
        {"INBOX": {5: reply(body, sender="Stranger <stranger@example.net>")}},
        settings=only("me@example.com"),
    )
    assert get_receipt(db, FILENAME)["total"] == 66.0
    assert link_sender == []
    assert email_status(db, 5) == "reply-untrusted"
    assert (result.skipped, result.reply_applied, result.reply_processed) == (1, 0, 0)
    assert query_receipt_updates(db, filename=FILENAME) == []


def test_a_reply_from_a_listed_address_is_acted_on_whatever_its_display_name(db, run_replies):
    result, _ = run_replies(
        {
            "INBOX": {
                5: reply(f"total: 9\nfilename: {FILENAME}", sender="Anyone At All <ME@Example.com>")
            }
        },
        settings=only("me@example.com"),
    )
    assert get_receipt(db, FILENAME)["total"] == 9.0 and result.reply_applied == 1


def test_with_no_list_configured_no_reply_is_acted_on(db, run_replies):
    result, _ = run_replies(
        {"INBOX": {5: reply(f"total: 9\nfilename: {FILENAME}")}}, settings=only()
    )
    assert get_receipt(db, FILENAME)["total"] == 66.0
    assert email_status(db, 5) == "reply-untrusted" and result.skipped == 1


def test_a_look_alike_sender_is_not_trusted(db, run_replies):
    run_replies(
        {"INBOX": {5: reply(f"total: 9\nfilename: {FILENAME}", sender="x@example.com.evil.test")}},
        settings=only("me@example.com", "x@example.com"),
    )
    assert get_receipt(db, FILENAME)["total"] == 66.0


def test_an_untrusted_reply_is_recorded_once_and_reported_when_verbose(db, run_replies, capsys):
    mailboxes = {"INBOX": {5: reply(f"total: 9\nfilename: {FILENAME}", sender="s@example.net")}}
    run_replies(mailboxes, settings=only("me@example.com"), verbose=True)
    assert "reply sender is not in [email].allowed_addresses" in capsys.readouterr().out
    first = db.execute("SELECT processed_at FROM email_receipts WHERE uid = 5").fetchone()[0]
    run_replies(mailboxes, settings=only("me@example.com"))
    assert (
        db.execute("SELECT processed_at FROM email_receipts WHERE uid = 5").fetchone()[0] == first
    )


def test_the_sender_check_comes_before_the_receipt_is_even_looked_up(db, run_replies, link_sender):
    result, _ = run_replies(
        {"INBOX": {5: reply("total: 1\nfilename: ghost.txt", sender="s@example.net")}},
        settings=only("me@example.com"),
    )
    assert email_status(db, 5) == "reply-untrusted"  # not "reply-unmatched"
    assert result.reply_unmatched == 0
