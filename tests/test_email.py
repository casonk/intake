"""Unit tests for intake.email (HTML stripping, dedup, safe-save, receipt heuristic)."""

from __future__ import annotations

from datetime import date
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from intake.db import (
    get_receipt,
    init_db,
    is_email_message_id_processed,
    is_email_processed,
    query_flagged,
    query_receipt_updates,
    record_email_receipt,
    upsert_receipt,
)
from intake.email import (
    FetchResult,
    _apply_reply_to_receipt,
    _configured_receipt_sender_hints,
    _email_was_handled,
    _extract_credit_card_payment_email,
    _extract_paypal_receipt_email,
    _extract_progressive_payment_email,
    _extract_receipt_filename_from_text,
    _extract_reply_updates,
    _file_message_to_processed_label,
    _is_intake_reply_message,
    _is_internal_intake_notification,
    _is_known_non_receipt_notice,
    _looks_like_receipt,
    _mailbox_select_candidates,
    _preferred_payment_category,
    _reply_requests_file_link,
    _reply_thread_token,
    _safe_save,
    _safe_save_receipt_pdf,
    _trim_reply_note_text,
    _upsert_html_receipt,
    _write_processed_email_artifact,
    fetch_receipts,
    html_to_text,
)
from intake.sidecar import read_sidecar

# ── html_to_text ──────────────────────────────────────────────────────────────


def test_html_to_text_strips_tags():
    html = "<p>Hello <b>world</b></p>"
    assert html_to_text(html) == "Hello world"


def test_html_to_text_drops_script_and_style():
    html = "<p>Keep this</p><script>alert(1)</script><style>.x{}</style><p>And this</p>"
    text = html_to_text(html)
    assert "Keep this" in text
    assert "And this" in text
    assert "alert" not in text
    assert ".x{}" not in text


def test_html_to_text_preserves_newlines_at_block_boundaries():
    html = "<div>Line A</div><div>Line B</div>"
    text = html_to_text(html)
    assert "Line A" in text
    assert "Line B" in text
    # They must appear on separate lines
    assert text.index("Line A") < text.index("Line B")


def test_html_to_text_collapses_whitespace():
    html = "<p>  too   many   spaces  </p>"
    assert html_to_text(html) == "too many spaces"


def test_html_to_text_empty_string():
    assert html_to_text("") == ""


def test_html_to_text_plain_text_passthrough():
    # No tags — should come back clean
    assert html_to_text("Hello world") == "Hello world"


# ── _looks_like_receipt ───────────────────────────────────────────────────────

# TODO: Add broader scheduled-payment regression fixtures as more live examples
# are collected for Capital One, Discover, and Progressive variants.


@pytest.mark.parametrize(
    "text",
    [
        "Order total: $42.00",
        "Amount Due: $9.99",
        "Invoice #1234",
        "Receipt #007",
        "You have been charged $15.00",
        "Subtotal: $5.00",
        "Order Total $100.00",
    ],
)
def test_looks_like_receipt_true(text):
    assert _looks_like_receipt(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Hi there, just checking in!",
        "Your account has been updated.",
        "Newsletter: 10 tips for better sleep",
    ],
)
def test_looks_like_receipt_false(text):
    assert _looks_like_receipt(text) is False


def test_looks_like_receipt_false_for_quoted_intake_notification():
    text = """
    Merchant is invalid

    On Mon, Apr 20, 2026 at 07:48 wrote:
    Receipt processed
    Merchant: oooo rr rr or rk ee ff
    Total: $66.00
    Category: general
    File: general/20260420_2202.jpg
    Model: tesseract
    """

    assert _looks_like_receipt(text) is False


@pytest.mark.parametrize(
    ("subject", "text"),
    [
        (
            "We've received your payment",
            "Thanks for your payment\nPayment amount:\n$69.69\nPosted date:\nApril 16, 2026",
        ),
    ],
)
def test_looks_like_receipt_true_for_credit_card_payment_emails(subject, text):
    assert _looks_like_receipt(f"{subject}\n{text}") is True


@pytest.mark.parametrize(
    ("subject", "sender", "text"),
    [
        (
            "Your payment of $69.69 is scheduled for May. 2, 2026",
            "Capital One | VentureOne <capitalone@notification.capitalone.com>",
            "Please review your payment details.",
        ),
        (
            "Your Scheduled Payment",
            "Discover Card <discover@services.discover.com>",
            "CASON KONZER, your online Discover card payment is scheduled\n"
            "Payment Info\nAmount: $69.69",
        ),
        (
            "You have an upcoming Litecoin purchase",
            "service@paypal.com",
            "You have an upcoming scheduled Litecoin (LTC) purchase on May 2.",
        ),
    ],
)
def test_known_non_receipt_payment_notices_are_not_receipts(subject, sender, text):
    assert _is_known_non_receipt_notice(subject, sender, text) is True
    assert _looks_like_receipt(f"{subject}\n{text}") is False


def test_extract_credit_card_payment_email_capital_one_posted():
    body = """
    VentureOne Credit Card...5653
    Thanks for your payment of $69.69
    CASON,
    Payment amount:
    $69.69
    Posted date:
    April 16, 2026
    This email was sent to casonkonzer@gmail.com
    """
    parsed = _extract_credit_card_payment_email(
        "We've received your payment",
        '"Capital One | VentureOne" capitalone@notification.capitalone.com',
        body,
    )
    assert parsed == {
        "merchant": "capital one",
        "total": 69.69,
        "receipt_date": date(2026, 4, 16),
        "purchaser": "cason konzer",
        "card_last4": "5653",
        "category": "finance/credit-card-payment",
    }


def test_extract_credit_card_payment_email_discover_scheduled_is_ignored():
    body = """
    Account ending in 7875
    CASON KONZER, your online Discover card payment is scheduled
    Payment Info
    Amount: $69.69
    Payment Post Date: April 16, 2026
    Confirmation Number: 9N00-2CHP-2P8D
    """
    parsed = _extract_credit_card_payment_email(
        "Your Scheduled Payment",
        "Discover Card discover@services.discover.com",
        body,
    )
    assert parsed is None


def test_preferred_payment_category_prefers_credit_card_subcategory():
    rules = {
        "finance": {
            "label": "Finance & Transfers",
            "keywords": [],
            "credit-card-payment": {"label": "Credit Card Payments", "keywords": []},
        }
    }
    assert _preferred_payment_category(rules) == "finance/credit-card-payment"


def test_extract_progressive_payment_email_renewal_reminder():
    body = """
    Your auto policy renewal payment is due on 05/01/2026 by 12:01 a.m. EST.
    Your Payment Options
    Total Premium
    $1,125.00
    Pay In Full
    $1,028.00
    Minimum Installment
    $193.55
    Cason, your rate went down by $48!
    """
    parsed = _extract_progressive_payment_email(
        "Progressive policy renewal reminder",
        "Progressive customerservice@e.progressive.com",
        body,
    )
    assert parsed == {
        "merchant": "progressive",
        "total": 193.55,
        "receipt_date": date(2026, 5, 1),
        "purchaser": "cason konzer",
        "card_last4": None,
        "category": "auto/insurance",
    }


def test_extract_paypal_receipt_email_transaction_receipt():
    body = """
    Hello, Cason Konzer

    You paid $12.99 USD to Spotify USA Inc

    Merchant
    Spotify USA Inc
    support@spotify.com

    Transaction date
    Apr 23, 2026

    Total
    $12.99 USD

    Your payment was sent from casonkonzer@gmail.com
    """
    parsed = _extract_paypal_receipt_email(
        "Spotify USA Inc: $12.99 USD",
        "PayPal service@paypal.com",
        body,
    )
    assert parsed == {
        "merchant": "Spotify USA Inc",
        "total": 12.99,
        "receipt_date": date(2026, 4, 23),
        "purchaser": "cason konzer",
        "card_last4": None,
        "category": None,
    }


def test_configured_receipt_sender_hints_include_paypal_default():
    hints = _configured_receipt_sender_hints({"receipt_senders": ["merchant@example.com"]})
    assert hints == ["merchant@example.com", "service@paypal.com"]


@pytest.mark.parametrize(
    ("saved_count", "inline_processed", "handled"),
    [
        (0, False, False),
        (1, False, True),
        (0, True, True),
    ],
)
def test_email_was_handled_requires_attachment_or_inline_processing(
    saved_count, inline_processed, handled
):
    assert _email_was_handled(saved_count, inline_processed) is handled


def test_file_message_to_processed_label_skips_move_when_already_in_label():
    read_uids: list[int] = []
    moved_uids: list[tuple[int, str]] = []
    sr = SimpleNamespace(
        mark_message_read=lambda _conn, uid: read_uids.append(uid),
        move_message=lambda _conn, uid, label: moved_uids.append((uid, label)),
    )

    needs_expunge = _file_message_to_processed_label(
        sr,
        object(),
        30999,
        source_mailbox="intake/processed",
        processed_label="intake/processed",
    )

    assert needs_expunge is False
    assert read_uids == [30999]
    assert moved_uids == []


def test_internal_intake_notification_is_skipped():
    settings = {"gmail": {"subject_prefix": "[intake] "}}
    assert (
        _is_internal_intake_notification(
            "[intake] Receipt processed: capital one $69.69",
            "casonkonzer@gmail.com",
            settings,
        )
        is True
    )


def test_intake_reply_message_is_detected_from_re_subject():
    msg = EmailMessage()
    msg["Subject"] = "Re: [intake] Receipt processed: oooo rr rr or rk ee ff $66.00"

    assert _is_intake_reply_message(msg, {}) is True


def test_extract_receipt_filename_from_text_prefers_explicit_filename():
    text = """
    Receipt processed
    File: email/general
    Filename: email_30999_INBOX.txt
    """
    assert _extract_receipt_filename_from_text(text) == "email_30999_INBOX.txt"


def test_extract_receipt_filename_from_text_falls_back_to_file_basename():
    text = "Receipt processed\nFile: grocery/supermarket/20260409_1707.pdf"
    assert _extract_receipt_filename_from_text(text) == "20260409_1707.pdf"


def test_trim_reply_note_text_drops_quoted_original_message():
    text = """
    Merchant is invalid
    Please attach the OCR and Image

    On Thu, Apr 24, 2026 at 1:12 PM wrote:
    > Receipt processed
    > Filename: email_30999_INBOX.txt
    """
    assert _trim_reply_note_text(text) == "Merchant is invalid\nPlease attach the OCR and Image"


def test_extract_reply_updates_parses_safe_structured_fields():
    updates, remaining = _extract_reply_updates(
        "merchant: Spotify USA Inc\nTOTAL = $12.99\ncard last4: 5653\ncurrency: usd\nPlease review"
    )
    assert updates == {
        "merchant": "spotify usa inc",
        "total": 12.99,
        "card_last4": "5653",
        "currency": "USD",
    }
    assert remaining == "Please review"


def test_reply_requests_file_link_matches_attach_ocr_image_note():
    assert _reply_requests_file_link("Please attach the OCR and Image") is True


def test_reply_requests_file_link_ignores_plain_correction_note():
    assert _reply_requests_file_link("Merchant is invalid") is False


def test_reply_thread_token_prefers_in_reply_to_message_id():
    message = EmailMessage()
    message["Message-ID"] = "<reply@example.com>"
    message["In-Reply-To"] = "<original@example.com>"
    assert _reply_thread_token(message, "<reply@example.com>") == "<original@example.com>"


def test_mailbox_select_candidates_include_quoted_variant_for_spaces():
    assert _mailbox_select_candidates("[Gmail]/Sent Mail") == [
        "[Gmail]/Sent Mail",
        '"[Gmail]/Sent Mail"',
    ]


# ── _safe_save ────────────────────────────────────────────────────────────────


def test_safe_save_writes_file(tmp_path):
    dest = _safe_save(tmp_path, "receipt.pdf", b"%PDF-1.4")
    assert dest.exists()
    assert dest.read_bytes() == b"%PDF-1.4"


def test_write_processed_email_artifact_archives_raw_message(tmp_path):
    receipt_root = tmp_path / "receipts"
    payload = (
        b"From: shop@example.com\r\n"
        b"Subject: Receipt\r\n"
        b"Date: Fri, 01 May 2026 09:30:00 -0400\r\n"
        b"\r\n"
        b"Receipt body\r\n"
    )

    archived = _write_processed_email_artifact(
        {"receipt_dir": str(receipt_root)},
        uid=30999,
        mailbox="INBOX",
        message_id="<receipt@example.com>",
        subject="Receipt",
        sender="shop@example.com",
        received_at="Fri, 01 May 2026 09:30:00 -0400",
        payload=payload,
        attachment_count=1,
        inline_processed=False,
    )

    assert archived == receipt_root / "email" / "messages" / "20260501_0930_INBOX_30999.eml"
    assert archived.read_bytes() == payload
    sidecar = read_sidecar(archived)
    assert sidecar["document_type"] == "email-message"
    assert sidecar["message_id"] == "<receipt@example.com>"
    assert sidecar["receipt_dir"] == "email/messages/20260501_0930_INBOX_30999.eml"


def test_safe_save_sanitises_filename(tmp_path):
    dest = _safe_save(tmp_path, "re ce:ipt?.pdf", b"data")
    # No colon or question mark in filename
    assert ":" not in dest.name
    assert "?" not in dest.name
    assert dest.exists()


def test_safe_save_avoids_clobber(tmp_path):
    first = _safe_save(tmp_path, "receipt.pdf", b"first")
    second = _safe_save(tmp_path, "receipt.pdf", b"second")
    assert first != second
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"


def test_safe_save_empty_filename_fallback(tmp_path):
    # Empty string sanitises to "" which is falsy → falls back to "attachment.pdf"
    dest = _safe_save(tmp_path, "", b"data")
    assert dest.name == "attachment.pdf"
    assert dest.exists()


def test_safe_save_receipt_pdf_uses_canonical_name(tmp_path):
    dest = _safe_save_receipt_pdf(
        tmp_path,
        "vendor-receipt.pdf",
        b"%PDF-1.4",
        received_at="Thu, 10 Apr 2026 17:07:00 +0000",
    )
    assert dest.name == "20260410_1707.pdf"
    assert dest.exists()


# ── DB deduplication helpers ──────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def test_is_email_processed_false_initially(db):
    assert is_email_processed(db, uid=12345, mailbox="INBOX") is False


def test_record_and_check_email_processed(db):
    record_email_receipt(
        db,
        uid=12345,
        mailbox="INBOX",
        message_id="<abc@mail.example.com>",
        subject="Your receipt",
        sender="shop@example.com",
        received_at="Thu, 01 Jan 2026 12:00:00 +0000",
        attachment_count=1,
    )
    assert is_email_processed(db, uid=12345, mailbox="INBOX") is True


def test_is_email_message_id_processed_matches_across_mailboxes(db):
    record_email_receipt(
        db,
        uid=12345,
        mailbox="INBOX",
        message_id="<abc@mail.example.com>",
        subject="Your receipt",
        sender="shop@example.com",
        received_at="Thu, 01 Jan 2026 12:00:00 +0000",
        attachment_count=1,
    )
    assert is_email_message_id_processed(db, "<abc@mail.example.com>") is True


def test_different_uid_not_processed(db):
    record_email_receipt(
        db,
        uid=12345,
        mailbox="INBOX",
        message_id="<abc@mail.example.com>",
        subject="Your receipt",
        sender="shop@example.com",
        received_at="",
        attachment_count=0,
    )
    assert is_email_processed(db, uid=99999, mailbox="INBOX") is False


def test_different_mailbox_not_processed(db):
    record_email_receipt(
        db,
        uid=12345,
        mailbox="INBOX",
        message_id="<abc@mail.example.com>",
        subject="Your receipt",
        sender="shop@example.com",
        received_at="",
        attachment_count=0,
    )
    assert is_email_processed(db, uid=12345, mailbox="[Gmail]/Sent") is False


def test_duplicate_record_does_not_raise(db):
    kwargs = dict(
        uid=12345,
        mailbox="INBOX",
        message_id="<abc@mail.example.com>",
        subject="Your receipt",
        sender="shop@example.com",
        received_at="",
        attachment_count=0,
    )
    record_email_receipt(db, **kwargs)
    record_email_receipt(db, **kwargs)  # second call must not raise
    assert is_email_processed(db, uid=12345, mailbox="INBOX") is True


def test_apply_reply_to_receipt_applies_structured_updates(db, tmp_path):
    upsert_receipt(
        db,
        {
            "filename": "email_30999_INBOX.txt",
            "scan_date": "2026-04-24",
            "merchant": "oooo rr rr or rk ee ff",
            "total": 66.00,
            "category": "general",
            "receipt_dir": "email/general",
            "raw_text": "raw",
            "processed_by": "email-parse",
        },
    )

    status = _apply_reply_to_receipt(
        db,
        {"receipt_dir": str(tmp_path / "receipt-root")},
        filename="email_30999_INBOX.txt",
        note_text="merchant: rock creek liquor\ntotal: 66.40",
    )

    row = get_receipt(db, "email_30999_INBOX.txt")
    history = query_receipt_updates(db, filename="email_30999_INBOX.txt")

    assert status == "reply-applied"
    assert row is not None
    assert row["merchant"] == "rock creek liquor"
    assert row["total"] == 66.40
    assert history[0]["source"] == "email-reply"
    assert history[0]["changes"]["merchant"] == {
        "old": "oooo rr rr or rk ee ff",
        "new": "rock creek liquor",
    }


def test_apply_reply_to_receipt_records_freeform_note_and_flags_review(db, tmp_path):
    upsert_receipt(
        db,
        {
            "filename": "email_30999_INBOX.txt",
            "scan_date": "2026-04-24",
            "merchant": "spotify usa inc",
            "total": 12.99,
            "category": "general",
            "receipt_dir": "email/general",
            "raw_text": "raw",
            "processed_by": "email-parse",
        },
    )

    status = _apply_reply_to_receipt(
        db,
        {"receipt_dir": str(tmp_path / "receipt-root")},
        filename="email_30999_INBOX.txt",
        note_text="Merchant is invalid\nPlease attach the OCR and Image",
    )

    history = query_receipt_updates(db, filename="email_30999_INBOX.txt")
    flagged = {row["filename"]: row for row in query_flagged(db)}

    assert status == "reply-recorded"
    assert history[0]["source"] == "email-reply-note"
    assert history[0]["note"] == "Merchant is invalid\nPlease attach the OCR and Image"
    assert "email_30999_INBOX.txt" in flagged
    assert "email_reply_request" in flagged["email_30999_INBOX.txt"]["review_reason_summary"]


class _NoopImapConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def select(self, mailbox, readonly=False):
        return ("OK", [b"0"])

    def expunge(self):
        return ("OK", [b"0"])


def test_fetch_receipts_treats_reply_processing_as_best_effort(db, tmp_path):
    settings = {
        "receipt_dir": str(tmp_path / "receipts"),
        "email": {"shock_relay_config": "/tmp/config.local.yaml"},
    }
    sr = SimpleNamespace(
        load_config=lambda _path: SimpleNamespace(
            imap=SimpleNamespace(mailboxes=["INBOX"], search_charset=None)
        ),
        validate_imap=lambda _config: None,
        open_imap_connection=lambda _config: _NoopImapConnection(),
    )

    with (
        patch("intake.email._load_shock_relay", return_value=sr),
        patch("intake.email._search_receipt_uids", return_value=[]),
        patch("intake.email._process_reply_messages", side_effect=RuntimeError("reply degraded")),
    ):
        result = fetch_receipts(settings, db, dry_run=False, verbose=False)

    assert result.found == 0
    assert result.errors == 1


def test_fetch_receipts_archives_processed_email_message(db, tmp_path):
    receipt_root = tmp_path / "receipts"
    message = EmailMessage()
    message["Message-ID"] = "<receipt@example.com>"
    message["Subject"] = "Your receipt"
    message["From"] = "shop@example.com"
    message["Date"] = "Fri, 01 May 2026 09:30:00 -0400"
    message.set_content("Receipt attached")
    payload = message.as_bytes()
    read_uids: list[int] = []
    moved_uids: list[tuple[int, str]] = []

    class _OneMessageConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def select(self, mailbox, readonly=False):
            return ("OK", [b"1"])

        def uid(self, command, uid, *args):
            assert command == "fetch"
            assert uid == "30999"
            return ("OK", [b"payload"])

        def expunge(self):
            return ("OK", [b"0"])

    sr = SimpleNamespace(
        load_config=lambda _path: SimpleNamespace(
            imap=SimpleNamespace(mailboxes=["INBOX"], search_charset=None)
        ),
        validate_imap=lambda _config: None,
        open_imap_connection=lambda _config: _OneMessageConnection(),
        extract_fetch_payload=lambda _msg_data: payload,
        decode_mime_header=lambda value: str(value or ""),
        extract_attachments=lambda _payload: [
            ("receipt.pdf", "application/pdf", b"%PDF-1.4\nreceipt\n")
        ],
        extract_message_bodies=lambda _msg: ("", ""),
        mark_message_read=lambda _conn, uid: read_uids.append(uid),
        move_message=lambda _conn, uid, label: moved_uids.append((uid, label)),
    )

    settings = {
        "receipt_dir": str(receipt_root),
        "email": {"shock_relay_config": "/tmp/config.local.yaml"},
    }

    with (
        patch("intake.email._load_shock_relay", return_value=sr),
        patch("intake.email._search_receipt_uids", return_value=[30999]),
        patch("intake.email._process_reply_messages"),
    ):
        result = fetch_receipts(settings, db, dry_run=False, verbose=False)

    archived = receipt_root / "email" / "messages" / "20260501_0930_INBOX_30999.eml"
    sidecar = read_sidecar(archived)

    assert result.attachments_saved == 1
    assert archived.read_bytes() == payload
    assert sidecar["attachment_count"] == 1
    assert sidecar["inline_processed"] is False
    assert is_email_processed(db, uid=30999, mailbox="INBOX") is True
    assert read_uids == [30999]
    assert moved_uids == [(30999, "intake/processed")]


def test_fetch_receipts_files_already_processed_candidate(db, tmp_path):
    receipt_root = tmp_path / "receipts"
    record_email_receipt(
        db,
        uid=30999,
        mailbox="INBOX",
        message_id="<receipt@example.com>",
        subject="Your receipt",
        sender="shop@example.com",
        received_at="Fri, 01 May 2026 09:30:00 -0400",
        attachment_count=1,
    )
    message = EmailMessage()
    message["Message-ID"] = "<receipt@example.com>"
    message["Subject"] = "Your receipt"
    message["From"] = "shop@example.com"
    message["Date"] = "Fri, 01 May 2026 09:30:00 -0400"
    message.set_content("Receipt attached")
    payload = message.as_bytes()
    read_uids: list[int] = []
    moved_uids: list[tuple[int, str]] = []

    class _AlreadyProcessedConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def select(self, mailbox, readonly=False):
            return ("OK", [b"1"])

        def uid(self, command, uid, *args):
            assert command == "fetch"
            assert uid == "30999"
            return ("OK", [b"payload"])

        def expunge(self):
            return ("OK", [b"0"])

    def fail_extract_attachments(_payload):
        raise AssertionError("already-processed messages must not be reprocessed")

    sr = SimpleNamespace(
        load_config=lambda _path: SimpleNamespace(
            imap=SimpleNamespace(mailboxes=["INBOX"], search_charset=None)
        ),
        validate_imap=lambda _config: None,
        open_imap_connection=lambda _config: _AlreadyProcessedConnection(),
        extract_fetch_payload=lambda _msg_data: payload,
        decode_mime_header=lambda value: str(value or ""),
        extract_attachments=fail_extract_attachments,
        mark_message_read=lambda _conn, uid: read_uids.append(uid),
        move_message=lambda _conn, uid, label: moved_uids.append((uid, label)),
    )

    settings = {
        "receipt_dir": str(receipt_root),
        "email": {"shock_relay_config": "/tmp/config.local.yaml"},
    }

    with (
        patch("intake.email._load_shock_relay", return_value=sr),
        patch("intake.email._search_receipt_uids", return_value=[30999]),
        patch("intake.email._process_reply_messages"),
    ):
        result = fetch_receipts(settings, db, dry_run=False, verbose=False)

    assert result.skipped == 1
    assert read_uids == [30999]
    assert moved_uids == [(30999, "intake/processed")]


def test_fetch_receipts_files_internal_notification_to_notify_label(db, tmp_path):
    receipt_root = tmp_path / "receipts"
    message = EmailMessage()
    message["Message-ID"] = "<notify@example.com>"
    message["Subject"] = "[intake] Receipt processed: kroger $32.98"
    message["From"] = "intake@example.com"
    message["Date"] = "Fri, 01 May 2026 09:30:00 -0400"
    message.set_content("Receipt processed\nMerchant: kroger")
    payload = message.as_bytes()
    read_uids: list[int] = []
    moved_uids: list[tuple[int, str]] = []

    class _NotificationConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def select(self, mailbox, readonly=False):
            return ("OK", [b"1"])

        def uid(self, command, uid, *args):
            assert command == "fetch"
            assert uid == "30999"
            return ("OK", [b"payload"])

        def expunge(self):
            return ("OK", [b"0"])

    def fail_extract_attachments(_payload):
        raise AssertionError("internal notifications must not be reprocessed")

    sr = SimpleNamespace(
        load_config=lambda _path: SimpleNamespace(
            imap=SimpleNamespace(mailboxes=["INBOX"], search_charset=None)
        ),
        validate_imap=lambda _config: None,
        open_imap_connection=lambda _config: _NotificationConnection(),
        extract_fetch_payload=lambda _msg_data: payload,
        decode_mime_header=lambda value: str(value or ""),
        extract_attachments=fail_extract_attachments,
        mark_message_read=lambda _conn, uid: read_uids.append(uid),
        move_message=lambda _conn, uid, label: moved_uids.append((uid, label)),
    )

    settings = {
        "receipt_dir": str(receipt_root),
        "email": {
            "shock_relay_config": "/tmp/config.local.yaml",
            "notification_label": "intake/notify",
        },
    }

    with (
        patch("intake.email._load_shock_relay", return_value=sr),
        patch("intake.email._search_receipt_uids", return_value=[30999]),
        patch("intake.email._process_reply_messages"),
    ):
        result = fetch_receipts(settings, db, dry_run=False, verbose=False)

    assert result.skipped == 1
    assert read_uids == [30999]
    assert moved_uids == [(30999, "intake/notify")]
    assert is_email_processed(db, uid=30999, mailbox="INBOX") is False


def test_fetch_receipts_files_known_non_receipt_notice_as_ignored(db, tmp_path):
    receipt_root = tmp_path / "receipts"
    message = EmailMessage()
    message["Message-ID"] = "<scheduled-payment@example.com>"
    message["Subject"] = "Your payment of $69.69 is scheduled for May. 2, 2026"
    message["From"] = "Capital One | VentureOne <capitalone@notification.capitalone.com>"
    message["Date"] = "Fri, 01 May 2026 09:32:35 +0000"
    message.set_content("Please review your payment details.")
    payload = message.as_bytes()
    read_uids: list[int] = []
    moved_uids: list[tuple[int, str]] = []

    class _NoticeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def select(self, mailbox, readonly=False):
            return ("OK", [b"1"])

        def uid(self, command, uid, *args):
            assert command == "fetch"
            assert uid == "41001"
            return ("OK", [b"payload"])

        def expunge(self):
            return ("OK", [b"0"])

    def fail_extract_attachments(_payload):
        raise AssertionError("known non-receipt notices must not inspect attachments")

    sr = SimpleNamespace(
        load_config=lambda _path: SimpleNamespace(
            imap=SimpleNamespace(mailboxes=["INBOX"], search_charset=None)
        ),
        validate_imap=lambda _config: None,
        open_imap_connection=lambda _config: _NoticeConnection(),
        extract_fetch_payload=lambda _msg_data: payload,
        decode_mime_header=lambda value: str(value or ""),
        extract_message_bodies=lambda _msg: ("Please review your payment details.", ""),
        extract_attachments=fail_extract_attachments,
        mark_message_read=lambda _conn, uid: read_uids.append(uid),
        move_message=lambda _conn, uid, label: moved_uids.append((uid, label)),
    )

    settings = {
        "receipt_dir": str(receipt_root),
        "email": {"shock_relay_config": "/tmp/config.local.yaml"},
    }

    with (
        patch("intake.email._load_shock_relay", return_value=sr),
        patch("intake.email._search_receipt_uids", return_value=[41001]),
        patch("intake.email._process_reply_messages"),
    ):
        result = fetch_receipts(settings, db, dry_run=False, verbose=False)

    assert result.skipped == 1
    assert read_uids == [41001]
    assert moved_uids == [(41001, "intake/processed")]
    assert is_email_processed(db, uid=41001, mailbox="INBOX") is True
    row = db.execute(
        "SELECT status FROM email_receipts WHERE uid = ? AND mailbox = ?",
        (41001, "INBOX"),
    ).fetchone()
    assert row["status"] == "ignored"
    assert get_receipt(db, "email_41001_INBOX.txt") is None


def test_upsert_html_receipt_writes_snowbridge_email_artifact(db, tmp_path, monkeypatch):
    import intake.classify as classify_mod

    receipt_root = tmp_path / "receipts"
    sent_links: list[str | None] = []

    def fake_notify(settings, _receipt, *, category, receipt_dir, filename, processed_by):
        from intake.file_links import build_receipt_filebrowser_link

        sent_links.append(
            build_receipt_filebrowser_link(
                settings,
                {
                    "filename": filename,
                    "receipt_dir": receipt_dir,
                    "category": category,
                },
            )
        )
        return {"gmail": 1}, {}

    monkeypatch.setattr(classify_mod, "load_rules", lambda: {})
    monkeypatch.setattr(classify_mod, "load_merchant_aliases", lambda: {})
    monkeypatch.setattr(classify_mod, "classify", lambda *_args, **_kwargs: "general")
    monkeypatch.setattr("intake.notify.send_processed_receipt_notifications", fake_notify)
    monkeypatch.setattr("intake.personal_finance.trigger_personal_finance", lambda *a, **k: None)

    _upsert_html_receipt(
        db,
        30999,
        "INBOX",
        "Receipt from Example",
        "shop@example.com",
        "",
        "Example Shop\nTotal $12.99",
        {"receipt_dir": str(receipt_root)},
    )

    artifact = receipt_root / "email" / "general" / "email_30999_INBOX.txt"
    sidecar = receipt_root / "email" / "general" / "email_30999_INBOX.intake.json"
    row = get_receipt(db, "email_30999_INBOX.txt")

    assert artifact.read_text(encoding="utf-8") == "Example Shop\nTotal $12.99"
    assert sidecar.exists()
    assert row is not None
    assert row["receipt_dir"] == "email/general"
    assert sent_links == [
        "https://files.snowbridge.internal/files/receipt/email/general/email_30999_INBOX.txt"
    ]


# ── FetchResult ───────────────────────────────────────────────────────────────


def test_fetch_result_defaults():
    r = FetchResult()
    assert r.found == 0
    assert r.skipped == 0
    assert r.attachments_saved == 0
    assert r.html_processed == 0
    assert r.reply_processed == 0
    assert r.reply_applied == 0
    assert r.reply_unmatched == 0
    assert r.errors == 0
    assert r.saved_files == []
