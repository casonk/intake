"""Unit tests for intake.email (HTML stripping, dedup, safe-save, receipt heuristic)."""

from __future__ import annotations

from datetime import date

import pytest

from intake.db import init_db, is_email_processed, record_email_receipt
from intake.email import (
    FetchResult,
    _configured_receipt_sender_hints,
    _email_was_handled,
    _extract_credit_card_payment_email,
    _extract_paypal_receipt_email,
    _extract_progressive_payment_email,
    _is_internal_intake_notification,
    _looks_like_receipt,
    _preferred_payment_category,
    _safe_save,
    _safe_save_receipt_pdf,
    html_to_text,
)

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


@pytest.mark.parametrize(
    ("subject", "text"),
    [
        (
            "We've received your payment",
            "Thanks for your payment\nPayment amount:\n$69.69\nPosted date:\nApril 16, 2026",
        ),
        (
            "Your Scheduled Payment",
            "Payment Info\nAmount: $20.26\nPayment Post Date: April 16, 2026",
        ),
    ],
)
def test_looks_like_receipt_true_for_credit_card_payment_emails(subject, text):
    assert _looks_like_receipt(f"{subject}\n{text}") is True


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


def test_extract_credit_card_payment_email_discover_scheduled():
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
    assert parsed == {
        "merchant": "discover",
        "total": 69.69,
        "receipt_date": date(2026, 4, 16),
        "purchaser": "cason konzer",
        "card_last4": "7875",
        "category": "finance/credit-card-payment",
    }


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


# ── _safe_save ────────────────────────────────────────────────────────────────


def test_safe_save_writes_file(tmp_path):
    dest = _safe_save(tmp_path, "receipt.pdf", b"%PDF-1.4")
    assert dest.exists()
    assert dest.read_bytes() == b"%PDF-1.4"


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


# ── FetchResult ───────────────────────────────────────────────────────────────


def test_fetch_result_defaults():
    r = FetchResult()
    assert r.found == 0
    assert r.skipped == 0
    assert r.attachments_saved == 0
    assert r.html_processed == 0
    assert r.errors == 0
    assert r.saved_files == []
