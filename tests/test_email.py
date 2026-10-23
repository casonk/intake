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
    _extract_amex_payment_email,
    _extract_credit_card_payment_email,
    _extract_credit_reporting_email,
    _extract_irs_payment_email,
    _extract_nelnet_billing_email,
    _extract_paypal_receipt_email,
    _extract_receipt_filename_from_text,
    _extract_reply_updates,
    _extract_venmo_payment_email,
    _extract_wise_payment_email,
    _extract_xanterra_reservation_email,
    _file_message_to_processed_label,
    _has_google_apple_relay_authentication,
    _is_intake_generated_message,
    _is_intake_reply_message,
    _is_internal_intake_notification,
    _is_known_non_receipt_notice,
    _looks_like_receipt,
    _mailbox_select_candidates,
    _preferred_credit_reporting_category,
    _preferred_payment_category,
    _reply_requests_file_link,
    _reply_thread_token,
    _safe_save,
    _safe_save_receipt_pdf,
    _trim_reply_note_text,
    _upsert_html_receipt,
    _write_inline_email_receipt_artifact,
    _write_processed_email_artifact,
    fetch_receipts,
    html_to_text,
)
from intake.parse import ReceiptItem
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
        (
            "You paid Mirjam Zinner $20.00",
            "Hi Alex,\nYou paid Mirjam Zinner $20.00\nFor: dinner",
        ),
        (
            "Mirjam Zinner paid you $15.00",
            "Hi Alex,\nMirjam Zinner paid you $15.00",
        ),
        (
            "Alex charged you $50.00",
            "Hi Alex,\nAlex charged you $50.00",
        ),
        (
            "Money added to your account",
            "Hello Alex,\nYou added 123.45 USD to your account.",
        ),
        (
            "Your Reservation Confirmation",
            "Yellowstone National Park Lodges\nReservation Total $43.88",
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
            "ALEX SAMPLE, your online Discover card payment is scheduled\n"
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
    ALEX SAMPLE,
    Payment amount:
    $69.69
    Posted date:
    April 16, 2026
    This email was sent to alexsample@gmail.com
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
        "purchaser": "alex sample",
        "card_last4": "5653",
        "category": "finance/credit-card-payment",
    }


def test_extract_credit_card_payment_email_discover_scheduled_is_ignored():
    body = """
    Account ending in 7875
    ALEX SAMPLE, your online Discover card payment is scheduled
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


def test_preferred_credit_reporting_category_prefers_credit_reporting_subcategory():
    rules = {
        "finance": {
            "label": "Finance & Transfers",
            "keywords": [],
            "credit-reporting": {"label": "Credit Reporting", "keywords": []},
        }
    }
    assert _preferred_credit_reporting_category(rules) == "finance/credit-reporting"


def test_extract_credit_reporting_email_equifax():
    body = """
    Credit Monitoring Alert
    JORDAN, you have a new credit report and score available.
    Sign into your myEquifax account for details.
    """
    parsed = _extract_credit_reporting_email(
        "New Equifax Credit Report and Score Available",
        "no-reply@equifax.com",
        body,
    )
    assert parsed == {
        "merchant": "equifax",
        "total": None,
        "receipt_date": None,
        "purchaser": "jordan",
        "card_last4": None,
        "category": "finance/credit-reporting",
        "processed_by": "email-credit-reporting",
    }


def test_extract_credit_reporting_email_experian():
    body = """
    Keep it up, Jordan—here’s what’s new
    YOUR FICO(R) SCORE
    INCREASED
    See your alerts
    """
    parsed = _extract_credit_reporting_email(
        "Your FICO® Score went up. Nice work!",
        "Experian Alerts <support@s.usa.experian.com>",
        body,
    )
    assert parsed == {
        "merchant": "experian",
        "total": None,
        "receipt_date": None,
        "purchaser": "jordan",
        "card_last4": None,
        "category": "finance/credit-reporting",
        "processed_by": "email-credit-reporting",
    }


def test_extract_credit_reporting_email_capital_one_creditwise():
    body = """
    Confirming your brand-new credit score.
    Jordan,
    Here's a quick look at your credit score activity monitored by CreditWise from Capital One.
    """
    parsed = _extract_credit_reporting_email(
        "Jordan, your credit score has improved",
        "Capital One <capitalone@notification.capitalone.com>",
        body,
    )
    assert parsed == {
        "merchant": "capital one creditwise",
        "total": None,
        "receipt_date": None,
        "purchaser": "jordan",
        "card_last4": None,
        "category": "finance/credit-reporting",
        "processed_by": "email-credit-reporting",
    }


@pytest.mark.parametrize(
    "subject",
    [
        "Progressive policy renewal reminder",
        "Reader, your auto policy is ready to renew.",
        "Reader, a reminder to pay your auto policy.",
        "You have new Progressive policy documents",
    ],
)
def test_progressive_policy_notices_are_non_spend_documents(subject):
    from intake.email import _extract_structured_finance_email

    parsed = _extract_structured_finance_email(
        subject,
        "Progressive <customerservice@e.progressive.com>",
        "Your bill is ready. Total premium: $786.00. Minimum installment: $137.03."
        " Next payment due 11/01/2026.",
    )
    assert parsed == {
        "merchant": "progressive",
        "document_kind": "documents/financial",
        "title": subject,
    }
    assert (
        _extract_structured_finance_email(
            subject, "unrelated@example.com", "Your bill is ready. $137.03 due."
        )
        is None
    )


def test_fanduel_dormancy_requires_matching_sender_and_subject():
    from intake.email import _extract_structured_finance_email

    body = "Your account remains inactive. Log in before 10/20."
    assert (
        _extract_structured_finance_email(
            "Final Warning: Account Dormancy", "other@example.com", body
        )
        is None
    )
    assert (
        _extract_structured_finance_email(
            "Weekend sportsbook offers", "FanDuel <email@t.sb.fanduel.com>", body
        )
        is None
    )


def test_extract_paypal_receipt_email_transaction_receipt():
    body = """
    Alex Sample,

    You paid $12.99 USD to Spotify USA Inc

    Merchant
    Spotify USA Inc
    support@spotify.com

    Transaction date
    Apr 23, 2026

    Total
    $12.99 USD

    Your payment was sent from alexsample@gmail.com
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
        "purchaser": "alex sample",
        "card_last4": None,
        "category": None,
    }


def test_extract_paypal_receipt_email_crypto_purchase():
    body = """
    You bought 0.02027474 Bitcoin Cash
    Jordan Avery, here's your receipt.
    Hello, Jordan Avery
    You bought 0.02027474 Bitcoin Cash (BCH) for $5.11 USD
    See Your Crypto
    Transaction details
    Transaction Id: 00A00000AA000000A
    Sep 2, 2026 4:05 PM
    You bought
    0.02027474 BCH
    Exchange rate
    $246.62 USD = 1 BCH
    Price
    $5.00 USD
    Fee
    $0.11 USD
    You paid
    $5.11 USD
    Paid with HUNTINGTON BANK xxxx0000
    """
    parsed = _extract_paypal_receipt_email(
        "You bought 0.02027474 Bitcoin Cash",
        "service@paypal.com",
        body,
    )
    assert parsed == {
        "merchant": "PayPal Crypto (BCH)",
        "total": 5.11,
        "receipt_date": date(2026, 9, 2),
        "purchaser": "jordan avery",
        "card_last4": None,
        "category": "finance",
    }


def test_extract_paypal_receipt_email_unrecognized_shape_returns_none():
    parsed = _extract_paypal_receipt_email(
        "Your account statement",
        "service@paypal.com",
        "You paid your bill this month. Thanks for banking with us.",
    )
    assert parsed is None


def test_extract_venmo_payment_email_crypto_purchase():
    body = """
    You bought 0.01369143 Bitcoin Cash (BCH) for $5.11 USD We added 0.01369143 Bitcoin Cash (BCH) to your crypto balance.
    Bitcoin Cash Purchase
    STATUS
    Complete
    PAYMENT METHOD
    Venmo balance
    TRANSACTION DETAILS
    May 22, 2026, 11:28 PM PDT
    """
    parsed = _extract_venmo_payment_email(
        "You bought Bitcoin Cash",
        "Venmo venmo@venmo.com",
        body,
    )
    assert parsed == {
        "merchant": "Venmo Crypto (BCH)",
        "total": 5.11,
        "receipt_date": date(2026, 5, 22),
        "purchaser": None,
        "card_last4": None,
        "category": "finance",
    }


def test_extract_venmo_payment_email_paid_you_repeated_line():
    # Some Venmo emails render the summary line twice back-to-back (an
    # image-alt-text + body-text artifact); the old end-anchored regex would
    # capture everything up to the *second* occurrence as the merchant name.
    body = """
    Bruce ostler paid you $25.00 Bruce ostler paid you $25.00
    Bruce ostler paid you
    $
    25
    .00
    Shafta
    See transaction
    """
    parsed = _extract_venmo_payment_email(
        "Bruce ostler paid you $25.00",
        "Venmo venmo@venmo.com",
        body,
    )
    assert parsed["merchant"] == "Bruce ostler"
    assert parsed["total"] == 25.00
    assert parsed["is_income"] is True


def test_extract_venmo_payment_email_you_paid():
    body = """
    You paid Joe Smith $25.00

    Note: dinner

    May 10, 2026

    View on Venmo
    """
    parsed = _extract_venmo_payment_email(
        "You paid Joe Smith $25.00",
        "Venmo venmo@venmo.com",
        body,
    )
    assert parsed == {
        "merchant": "Joe Smith",
        "total": 25.00,
        "receipt_date": date(2026, 5, 10),
        "purchaser": None,
        "card_last4": None,
        "category": None,
        "is_income": False,
    }


def test_extract_venmo_payment_email_paid_you():
    body = """
    Jane Doe paid you $50.00

    Note: rent split

    May 9, 2026

    View on Venmo
    """
    parsed = _extract_venmo_payment_email(
        "Jane Doe paid you $50.00",
        "Venmo venmo@venmo.com",
        body,
    )
    assert parsed == {
        "merchant": "Jane Doe",
        "total": 50.00,
        "receipt_date": date(2026, 5, 9),
        "purchaser": None,
        "card_last4": None,
        "category": None,
        "is_income": True,
    }


def test_extract_venmo_payment_email_charged_you():
    body = """
    Alex Kim charged you $15.00

    Note: pizza

    May 8, 2026

    View on Venmo
    """
    parsed = _extract_venmo_payment_email(
        "Alex Kim charged you $15.00",
        "Venmo venmo@venmo.com",
        body,
    )
    assert parsed == {
        "merchant": "Alex Kim",
        "total": 15.00,
        "receipt_date": date(2026, 5, 8),
        "purchaser": None,
        "card_last4": None,
        "category": None,
        "is_income": False,
    }


def test_extract_venmo_payment_email_unrecognized_shape_returns_none():
    parsed = _extract_venmo_payment_email(
        "Your weekly Venmo summary",
        "Venmo venmo@venmo.com",
        "Here's what happened on Venmo this week.",
    )
    assert parsed is None


def test_extract_venmo_payment_email_wrong_sender_returns_none():
    parsed = _extract_venmo_payment_email(
        "You paid Joe Smith $25.00",
        "Scammer scammer@notvenmo.com",
        "You paid Joe Smith $25.00\nMay 10, 2026",
    )
    assert parsed is None


def test_extract_wise_payment_email_money_added():
    body = """
    Hello Alex Sample,

    You added 123.45 USD to your account.

    And it's ready to be used.
    """
    parsed = _extract_wise_payment_email(
        "Money added to your account",
        "Wise <noreply@wise.com>",
        body,
    )
    assert parsed == {
        "merchant": "Wise",
        "total": 123.45,
        "receipt_date": None,
        "purchaser": None,
        "card_last4": None,
        "category": "finance",
    }


def test_extract_wise_payment_email_money_added_with_thousands_separator():
    body = """
    Hello Jordan Lee,

    You added 10,000 JPY to your account.

    And it's ready to be used.
    """
    parsed = _extract_wise_payment_email(
        "Money added to your account",
        "Wise <noreply@wise.com>",
        body,
    )
    assert parsed == {
        "merchant": "Wise",
        "total": 10000.0,
        "receipt_date": None,
        "purchaser": None,
        "card_last4": None,
        "category": "finance",
    }


def test_extract_wise_payment_email_wrong_sender_returns_none():
    parsed = _extract_wise_payment_email(
        "Money added to your account",
        "Wise <noreply@notwise.example>",
        "You added 123.45 USD to your account.",
    )
    assert parsed is None


def test_extract_xanterra_reservation_email_confirmation():
    body = """
    Welcome, Alex Sample

    Yellowstone National Park Lodges
    Bridge Bay Campground
    Arriving Sunday, May 24, 2026
    Check-In Sunday 5/24/2026 after 1:00 p.m.
    Check-Out Monday 5/25/2026 by 11:00 a.m.
    Nightly Rate $38.00
    Taxes/Fees $5.88
    Reservation Total $43.88
    Deposit Required $43.88
    Deposit Paid By American Express Card
    """
    parsed = _extract_xanterra_reservation_email(
        "Your Reservation Confirmation",
        "Xanterra Reservations <Reserve-NoReply@xanterra.com>",
        body,
    )
    assert parsed == {
        "merchant": "Yellowstone National Park Lodges",
        "total": 43.88,
        "receipt_date": None,
        "purchaser": "alex sample",
        "card_last4": None,
        "category": "outdoor/park",
        "processed_by": "email-reservation",
        "items": [
            {
                "description": "Bridge Bay Campground lodging",
                "amount": 38.0,
                "quantity": None,
                "unit_price": None,
            },
            {
                "description": "Taxes/Fees",
                "amount": 5.88,
                "quantity": None,
                "unit_price": None,
            },
        ],
    }


def test_extract_xanterra_reservation_email_wrong_sender_returns_none():
    parsed = _extract_xanterra_reservation_email(
        "Your Reservation Confirmation",
        "Reservations <reserve@example.com>",
        "Yellowstone National Park Lodges\nReservation Total $43.88",
    )
    assert parsed is None


def test_extract_irs_payment_email_confirmation():
    body = """
CONFIRMATION OF PAYMENT TRANSACTION

You've successfully submitted your payment(s).

Total Amount:
$108.10

Payment Amount:
$108.10

Date:
May 12, 2026

Tax Year & Type:
2025 Income Tax
    """
    parsed = _extract_irs_payment_email(
        "IRS Account Confirmation of Scheduled Transaction",
        "Internal Revenue Service <DoNotReply@account.irs.gov>",
        body,
    )
    assert parsed == {
        "merchant": "IRS",
        "total": 108.10,
        "receipt_date": date(2026, 5, 12),
        "purchaser": None,
        "card_last4": None,
        "category": None,
    }


def test_extract_irs_payment_email_generic_notification_returns_none():
    body = """
Internal Revenue Service
You Have a New Notification in Your Online Account
Dear Taxpayer,
Sign in to your account to view your notification.
    """
    parsed = _extract_irs_payment_email(
        "Notification from the IRS",
        "Internal Revenue Service <DoNotReply@account.irs.gov>",
        body,
    )
    assert parsed is None


def test_extract_irs_payment_email_wrong_sender_returns_none():
    parsed = _extract_irs_payment_email(
        "IRS Account Confirmation of Scheduled Transaction",
        "scammer@notirs.com",
        "You've successfully submitted your payment(s).\nTotal Amount:\n$100.00\nDate:\nMay 12, 2026",
    )
    assert parsed is None


def test_extract_nelnet_billing_email_statement():
    body = """
ALEX
Account:\xa0EXXXXX6972
Due Date:\xa06/1/2026
Amount Due:\xa0$0.00
Your Nelnet monthly billing statement is now available.
This email was sent to: ALEXSAMPLE@GMAIL.COM
    """
    parsed = _extract_nelnet_billing_email(
        "Your student loan billing statement is now available",
        "Nelnet <nelnetnoreply@nelnet.studentaid.gov>",
        body,
    )
    assert parsed == {
        "merchant": "Nelnet",
        "total": 0.00,
        "receipt_date": date(2026, 6, 1),
        "purchaser": None,
        "card_last4": None,
        "category": None,
    }


def test_extract_nelnet_billing_email_wrong_sender_returns_none():
    parsed = _extract_nelnet_billing_email(
        "Your student loan billing statement is now available",
        "scammer@notnel.net",
        "Amount Due:\xa0$0.00\nDue Date:\xa06/1/2026",
    )
    assert parsed is None


def test_is_known_non_receipt_notice_irs_generic():
    body = """
Internal Revenue Service
You Have a New Notification in Your Online Account
Dear Taxpayer,
Sign in to your account to view your notification.
    """
    from intake.email import _is_known_non_receipt_notice

    assert (
        _is_known_non_receipt_notice(
            "Notification from the IRS",
            "Internal Revenue Service <DoNotReply@account.irs.gov>",
            body,
        )
        is True
    )


def test_is_known_non_receipt_notice_irs_payment_not_ignored():
    body = """
CONFIRMATION OF PAYMENT TRANSACTION
You've successfully submitted your payment(s).
Total Amount:
$108.10
Date:
May 12, 2026
    """
    from intake.email import _is_known_non_receipt_notice

    assert (
        _is_known_non_receipt_notice(
            "IRS Account Confirmation of Scheduled Transaction",
            "Internal Revenue Service <DoNotReply@account.irs.gov>",
            body,
        )
        is False
    )


def test_is_known_non_receipt_notice_venmo_transaction_history():
    from intake.email import _is_known_non_receipt_notice

    assert (
        _is_known_non_receipt_notice(
            "Your April 2026 transaction history",
            "Venmo <venmo@venmo.com>",
            "Here is a summary of your Venmo transaction history for April 2026.",
        )
        is True
    )


def test_is_known_non_receipt_notice_venmo_payment_not_ignored():
    from intake.email import _is_known_non_receipt_notice

    assert (
        _is_known_non_receipt_notice(
            "You paid John Doe $25.00",
            "Venmo <venmo@venmo.com>",
            "You paid John Doe $25.00",
        )
        is False
    )


def test_is_known_non_receipt_notice_binance_staking_ignored():
    from intake.email import _is_known_non_receipt_notice

    body = """
Binance.US | Staking order received
Your staking order has been received.
Staking Amount:
0.1280 SOL
    """
    assert (
        _is_known_non_receipt_notice(
            "Binance.US | Staking order received",
            "do-not-reply@post.binance.us",
            body,
        )
        is True
    )


def test_is_known_non_receipt_notice_binance_non_staking_not_ignored():
    from intake.email import _is_known_non_receipt_notice

    assert (
        _is_known_non_receipt_notice(
            "Your Binance.US deposit of $100.00 was confirmed",
            "do-not-reply@post.binance.us",
            "Your deposit of $100.00 USD has been confirmed.",
        )
        is False
    )


def test_configured_receipt_sender_hints_include_paypal_default():
    hints = _configured_receipt_sender_hints({"receipt_senders": ["merchant@example.com"]})
    assert hints == [
        "merchant@example.com",
        "service@paypal.com",
        "service.paypal.com",
        "venmo@venmo.com",
        "account.irs.gov",
        "nelnet.studentaid.gov",
        "noreply@wise.com",
        "reserve-noreply@xanterra.com",
        "info@smbctb.co.jp",
        "americanexpress.com",
        "no-reply@equifax.com",
        "support@s.usa.experian.com",
        "no-reply@revolut.com",
        "noreply@gotinder.com",
        "hello@binance.us",
        "post.binance.us",
        "donotreply@mail.schwab.com",
        "email.huntington.com",
        "customerservice@e.progressive.com",
        "email@t.sb.fanduel.com",
    ]


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
            "alexsample@gmail.com",
            settings,
        )
        is True
    )


def test_intake_reply_message_is_detected_from_re_subject():
    msg = EmailMessage()
    msg["Subject"] = "Re: [intake] Receipt processed: oooo rr rr or rk ee ff $66.00"

    assert _is_intake_reply_message(msg, {}) is True


def test_intake_generated_reply_link_is_not_treated_as_user_reply():
    msg = EmailMessage()
    msg["Subject"] = "Re: [intake] Receipt processed: ac tire & service center unknown total"
    msg["X-Portfolio-Service"] = "intake"
    msg["X-Crew-Chief-Intent"] = "reply-link"
    msg["In-Reply-To"] = "<reply@example.com>"
    msg.set_content(
        "Snowbridge receipt link requested\n"
        "Receipt: 20260427_1121.pdf\n"
        "Merchant: ac tire & service center\n"
        "Total: unknown\n"
        "Link: https://files.snowbridge.internal/files/receipt/general/20260427_1121.pdf"
    )

    assert _is_intake_generated_message(msg) is True
    assert _is_intake_reply_message(msg, {}) is False


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


def test_reply_requests_file_link_ignores_generated_link_reply_body():
    note = (
        "Snowbridge receipt link requested\n"
        "Receipt: 20260427_1121.pdf\n"
        "Merchant: ac tire & service center\n"
        "Total: unknown\n"
        "Link: https://files.snowbridge.internal/files/receipt/general/20260427_1121.pdf"
    )
    assert _reply_requests_file_link(note) is False


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
    labeled_uids: list[tuple[int, str]] = []

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
        label_message=lambda _conn, uid, label: labeled_uids.append((uid, label)),
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
    assert read_uids == []
    assert moved_uids == []
    assert labeled_uids == []
    assert is_email_processed(db, uid=30999, mailbox="INBOX") is True
    row = db.execute(
        "SELECT status FROM email_receipts WHERE uid = ? AND mailbox = ?",
        (30999, "INBOX"),
    ).fetchone()
    assert row["status"] == "internal"

    with (
        patch("intake.email._load_shock_relay", return_value=sr),
        patch("intake.email._search_receipt_uids", return_value=[30999]),
        patch("intake.email._process_reply_messages"),
    ):
        result = fetch_receipts(settings, db, dry_run=False, verbose=False)

    assert result.skipped == 1
    assert labeled_uids == []
    assert moved_uids == []

    db.execute(
        """
        UPDATE email_receipts
        SET processed_at = ?
        WHERE uid = ? AND mailbox = ?
        """,
        ("2026-05-01T09:30:00+00:00", 30999, "INBOX"),
    )
    db.commit()

    with (
        patch("intake.email._load_shock_relay", return_value=sr),
        patch("intake.email._search_receipt_uids", return_value=[30999]),
        patch("intake.email._process_reply_messages"),
    ):
        result = fetch_receipts(settings, db, dry_run=False, verbose=False)

    assert result.skipped == 1
    assert read_uids == []
    assert moved_uids == [(30999, "intake/notify")]
    row = db.execute(
        "SELECT status FROM email_receipts WHERE uid = ? AND mailbox = ?",
        (30999, "INBOX"),
    ).fetchone()
    assert row["status"] == "internal-filed"


def test_fetch_receipts_files_internal_reply_link_to_notify_label(db, tmp_path):
    receipt_root = tmp_path / "receipts"
    message = EmailMessage()
    message["Message-ID"] = "<reply-link@example.com>"
    message["Subject"] = "Re: [intake] Receipt processed: kroger $32.98"
    message["From"] = "intake@example.com"
    message["Date"] = "Fri, 01 May 2026 09:31:00 -0400"
    message["X-Portfolio-Service"] = "intake"
    message["X-Crew-Chief-Intent"] = "reply-link"
    message["In-Reply-To"] = "<human-reply@example.com>"
    message.set_content(
        "Snowbridge receipt link requested\n"
        "Receipt: 20260409_1707.pdf\n"
        "Merchant: kroger\n"
        "Total: $32.98\n"
        "Link: https://files.snowbridge.internal/files/receipt/grocery/kroger/20260409_1707.pdf"
    )
    payload = message.as_bytes()
    read_uids: list[int] = []
    moved_uids: list[tuple[int, str]] = []
    labeled_uids: list[tuple[int, str]] = []

    class _ReplyLinkConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def select(self, mailbox, readonly=False):
            return ("OK", [b"1"])

        def uid(self, command, uid, *args):
            assert command == "fetch"
            assert uid == "31000"
            return ("OK", [b"payload"])

        def expunge(self):
            return ("OK", [b"0"])

    def fail_extract_attachments(_payload):
        raise AssertionError("internal reply-link messages must not inspect attachments")

    sr = SimpleNamespace(
        load_config=lambda _path: SimpleNamespace(
            imap=SimpleNamespace(mailboxes=["INBOX"], search_charset=None)
        ),
        validate_imap=lambda _config: None,
        open_imap_connection=lambda _config: _ReplyLinkConnection(),
        extract_fetch_payload=lambda _msg_data: payload,
        decode_mime_header=lambda value: str(value or ""),
        extract_attachments=fail_extract_attachments,
        mark_message_read=lambda _conn, uid: read_uids.append(uid),
        move_message=lambda _conn, uid, label: moved_uids.append((uid, label)),
        label_message=lambda _conn, uid, label: labeled_uids.append((uid, label)),
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
        patch("intake.email._search_receipt_uids", return_value=[31000]),
        patch("intake.email._process_reply_messages"),
    ):
        result = fetch_receipts(settings, db, dry_run=False, verbose=False)

    assert result.skipped == 1
    assert read_uids == []
    assert moved_uids == []
    assert labeled_uids == []
    assert is_email_processed(db, uid=31000, mailbox="INBOX") is True
    row = db.execute(
        "SELECT status FROM email_receipts WHERE uid = ? AND mailbox = ?",
        (31000, "INBOX"),
    ).fetchone()
    assert row["status"] == "internal"


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
        return []

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


def test_fetch_receipts_classifies_an_unmatched_candidate_only_once(db, tmp_path):
    message = EmailMessage()
    message["Message-ID"] = "<conversation@example.com>"
    message["Subject"] = "RE: Alex - cash payment"
    message["From"] = "Pat <pat@example.com>"
    message["Date"] = "Fri, 01 May 2026 09:32:35 +0000"
    message.set_content("Thanks, let's talk about it on Monday.")
    payload = message.as_bytes()
    read_uids: list[int] = []
    moved_uids: list[tuple[int, str]] = []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def select(self, mailbox, readonly=False):
            return ("OK", [b"1"])

        def uid(self, command, uid, *args):
            assert command == "fetch"
            return ("OK", [b"payload"])

        def expunge(self):
            return ("OK", [b"0"])

    sr = SimpleNamespace(
        load_config=lambda _path: SimpleNamespace(
            imap=SimpleNamespace(mailboxes=["INBOX"], search_charset=None)
        ),
        validate_imap=lambda _config: None,
        open_imap_connection=lambda _config: _Connection(),
        extract_fetch_payload=lambda _msg_data: payload,
        decode_mime_header=lambda value: str(value or ""),
        extract_message_bodies=lambda _msg: ("Thanks, let's talk about it on Monday.", ""),
        extract_attachments=lambda _payload: [],
        mark_message_read=lambda _conn, uid: read_uids.append(uid),
        move_message=lambda _conn, uid, label: moved_uids.append((uid, label)),
    )
    settings = {
        "receipt_dir": str(tmp_path / "receipts"),
        "email": {"shock_relay_config": "/tmp/config.local.yaml"},
    }

    with (
        patch("intake.email._load_shock_relay", return_value=sr),
        patch("intake.email._search_receipt_uids", return_value=[42001]),
        patch("intake.email._process_reply_messages"),
        patch("intake.email.classify_ambiguous_email", return_value=None) as classify,
    ):
        first = fetch_receipts(settings, db, dry_run=False, verbose=False)
        second = fetch_receipts(settings, db, dry_run=False, verbose=False)

    assert classify.call_count == 1, "a known non-receipt must not be classified again"
    assert (first.skipped, second.skipped) == (1, 1)
    assert moved_uids == [] and read_uids == [], "an unmatched message stays in the inbox"
    row = db.execute(
        "SELECT status FROM email_receipts WHERE uid = ? AND mailbox = ?",
        (42001, "INBOX"),
    ).fetchone()
    assert row["status"] == "unmatched"


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


def test_upsert_html_receipt_routes_venmo_paid_you_to_income_document(db, tmp_path, monkeypatch):
    # "X paid you $Y" is money credited to the account, not spend — it should
    # land in the documents table (non-spend), not the receipts table.
    receipt_root = tmp_path / "receipts"
    triggered = []

    monkeypatch.setattr(
        "intake.notify.send_processed_receipt_notifications", lambda *a, **k: ({}, {})
    )
    monkeypatch.setattr(
        "intake.personal_finance.trigger_personal_finance",
        lambda *a, **k: triggered.append(True),
    )

    _upsert_html_receipt(
        db,
        40001,
        "INBOX",
        "Jane Doe paid you $50.00",
        "Venmo venmo@venmo.com",
        "Wed, 20 May 2026 19:01:20 -0600",
        "Jane Doe paid you $50.00\nMoney credited to your Venmo account.",
        {"receipt_dir": str(receipt_root)},
    )

    assert get_receipt(db, "email_40001_INBOX.txt") is None
    document = db.execute(
        "SELECT * FROM documents WHERE filename = ?", ("email_40001_INBOX.txt",)
    ).fetchone()
    assert document is not None
    assert document["document_kind"] == "documents/income"
    assert document["category"] == "documents/income"
    assert document["issuer"] == "Jane Doe"

    artifact = receipt_root / "email" / "documents" / "income" / "email_40001_INBOX.txt"
    assert artifact.exists()
    # Income documents don't participate in spend tracking.
    assert triggered == []


def test_upsert_html_receipt_handles_xanterra_reservation(db, tmp_path, monkeypatch):
    import intake.classify as classify_mod

    receipt_root = tmp_path / "receipts"
    captured_events = []

    monkeypatch.setattr(
        classify_mod,
        "load_rules",
        lambda: {"outdoor": {"park": {"label": "Parks & Public Lands", "keywords": []}}},
    )
    monkeypatch.setattr(classify_mod, "load_merchant_aliases", lambda: {})
    monkeypatch.setattr(
        "intake.notify.send_processed_receipt_notifications", lambda *a, **k: ({}, {})
    )
    monkeypatch.setattr(
        "intake.personal_finance.trigger_personal_finance",
        lambda _settings, events, *, trigger: captured_events.append((trigger, events)),
    )

    _upsert_html_receipt(
        db,
        31936,
        "INBOX",
        "Your Reservation Confirmation",
        "Xanterra Reservations <Reserve-NoReply@xanterra.com>",
        "Wed, 20 May 2026 19:01:20 -0600",
        """
        Welcome, Alex Sample
        Yellowstone National Park Lodges
        Bridge Bay Campground
        Nightly Rate $38.00
        Taxes/Fees $5.88
        Reservation Total $43.88
        Deposit Required $43.88
        """,
        {"receipt_dir": str(receipt_root)},
    )

    row = get_receipt(db, "email_31936_INBOX.txt")
    artifact = receipt_root / "email" / "outdoor" / "park" / "email_31936_INBOX.txt"
    sidecar = read_sidecar(artifact)

    assert row is not None
    assert row["scan_date"] == "2026-05-20"
    assert row["merchant"] == "yellowstone national park lodges"
    assert row["total"] == 43.88
    assert row["category"] == "outdoor/park"
    assert row["receipt_dir"] == "email/outdoor/park"
    assert row["processed_by"] == "email-reservation"
    assert row["purchaser"] == "alex sample"
    assert sidecar is not None
    assert sidecar["items"] == [
        {
            "description": "Bridge Bay Campground lodging",
            "amount": 38.0,
            "quantity": None,
            "unit_price": None,
        },
        {
            "description": "Taxes/Fees",
            "amount": 5.88,
            "quantity": None,
            "unit_price": None,
        },
    ]
    assert captured_events[0][0] == "email"
    assert captured_events[0][1][0]["scan_date"] == "2026-05-20"


def test_upsert_html_receipt_handles_credit_reporting_email(db, tmp_path, monkeypatch):
    import intake.classify as classify_mod

    receipt_root = tmp_path / "receipts"

    monkeypatch.setattr(
        classify_mod,
        "load_rules",
        lambda: {"finance": {"credit-reporting": {"label": "Credit Reporting", "keywords": []}}},
    )
    monkeypatch.setattr(classify_mod, "load_merchant_aliases", lambda: {})
    monkeypatch.setattr(
        "intake.notify.send_processed_receipt_notifications", lambda *a, **k: ({}, {})
    )
    monkeypatch.setattr("intake.personal_finance.trigger_personal_finance", lambda *a, **k: None)

    _upsert_html_receipt(
        db,
        32602,
        "INBOX",
        "New Equifax Credit Report and Score Available",
        "no-reply@equifax.com",
        "Wed, 10 Jun 2026 20:08:58 +0000",
        """
        Credit Monitoring Alert
        JORDAN, you have a new credit report and score available.
        Sign into your myEquifax account for details.
        """,
        {"receipt_dir": str(receipt_root)},
    )

    row = get_receipt(db, "email_32602_INBOX.txt")
    artifact = receipt_root / "email" / "finance" / "credit-reporting" / "email_32602_INBOX.txt"
    sidecar = read_sidecar(artifact)

    assert row is not None
    assert row["scan_date"] == "2026-06-10"
    assert row["merchant"] == "equifax"
    assert row["total"] is None
    assert row["category"] == "finance/credit-reporting"
    assert row["receipt_dir"] == "email/finance/credit-reporting"
    assert row["processed_by"] == "email-credit-reporting"
    assert row["purchaser"] == "jordan"
    assert sidecar is not None
    assert sidecar["merchant"] == "equifax"
    assert sidecar["category"] == "finance/credit-reporting"


def test_write_inline_email_artifact_serializes_items_in_sidecar(tmp_path):
    receipt_root = tmp_path / "receipts"
    artifact = _write_inline_email_receipt_artifact(
        {"receipt_dir": str(receipt_root)},
        category="gas-station",
        filename="email_31485_INBOX.txt",
        plain_text="Shell receipt",
        row={
            "filename": "email_31485_INBOX.txt",
            "scan_date": "2026-05-05",
            "merchant": "shell oil",
            "total": 2.11,
            "category": "gas-station",
            "receipt_dir": "email/gas-station",
            "processed_by": "gpt-4.1",
            "items": [ReceiptItem("Coffee Refill", amount=1.99, quantity=1.0, unit_price=1.99)],
            "certifications": [],
            "staff": [],
        },
    )

    assert artifact == receipt_root / "email" / "gas-station" / "email_31485_INBOX.txt"
    sidecar = read_sidecar(artifact)
    assert sidecar is not None
    assert sidecar["items"] == [
        {
            "description": "Coffee Refill",
            "amount": 1.99,
            "quantity": 1.0,
            "unit_price": 1.99,
        }
    ]


# ── _extract_amex_payment_email ───────────────────────────────────────────────

_AMEX_PAYMENT_BODY = """\
Thanks for your payment processed on Jun 5, 2026
ALEX J SAMPLE
Account Ending: 01002
Thank you for your payment
Your scheduled payment was processed.
You're all set. You can view your updated balances online.
Payment amount:
$69.69
Processed on:
Jun 5, 2026
"""


def test_extract_amex_payment_email_confirmation():
    parsed = _extract_amex_payment_email(
        "We processed your payment",
        "AmericanExpress@welcome.americanexpress.com",
        _AMEX_PAYMENT_BODY,
    )
    assert parsed is not None
    assert parsed["merchant"] == "american express"
    assert parsed["total"] == 69.69
    assert parsed["receipt_date"] == date(2026, 6, 5)
    assert parsed["purchaser"] == "alex j sample"
    assert parsed["card_last4"] == "01002"
    assert parsed["category"] == "finance/credit-card-payment"
    assert parsed["processed_by"] == "email-payment-amex"


def test_extract_amex_payment_email_wrong_sender_returns_none():
    parsed = _extract_amex_payment_email(
        "We processed your payment",
        "noreply@othersender.com",
        _AMEX_PAYMENT_BODY,
    )
    assert parsed is None


def test_extract_amex_payment_email_non_payment_subject_returns_none():
    parsed = _extract_amex_payment_email(
        "Your account statement is ready",
        "AmericanExpress@welcome.americanexpress.com",
        _AMEX_PAYMENT_BODY,
    )
    assert parsed is None


def test_card_scheduled_notice_re_does_not_match_amex_was_processed():
    from intake.email import _CARD_SCHEDULED_NOTICE_RE

    assert not _CARD_SCHEDULED_NOTICE_RE.search("Your scheduled payment was processed.")


def test_card_scheduled_notice_re_still_matches_future_scheduled():
    from intake.email import _CARD_SCHEDULED_NOTICE_RE

    assert _CARD_SCHEDULED_NOTICE_RE.search("Your scheduled payment is being processed")
    assert _CARD_SCHEDULED_NOTICE_RE.search("payment is scheduled for Jun 7, 2026")


def test_amex_cardholder_re_does_not_capture_preceding_lines():
    from intake.email import _AMEX_CARDHOLDER_RE

    body = "THANK YOU\nALEX J SAMPLE\nAccount Ending: 01002"
    m = _AMEX_CARDHOLDER_RE.search(body)
    assert m is not None
    assert m.group(1) == "ALEX J SAMPLE"


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


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("subject", "sender", "body", "pdf", "expected_inline"),
    [
        (
            "Thank you for your payment to Example",
            "billing@example.com",
            "Receipt attached",
            True,
            False,
        ),
        ("Your Revolut account T&Cs", "no-reply@revolut.com", "Terms attached", True, False),
        ("Your receipt", "shop@example.com", "Total: $12.34", False, True),
        ("Your receipt", "shop@example.com", "Total: $12.34", True, False),
        (
            "Your payment of $12 is scheduled for Oct. 12, 2026",
            "capitalone@notification.capitalone.com",
            "Please review your payment details.",
            True,
            False,
        ),
    ],
)
def test_fetch_finance_discovery_plain_text_and_notice_attachments(
    db, tmp_path, subject, sender, body, pdf, expected_inline, dry_run
):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Message-ID"] = "<finance@example.com>"
    message["Date"] = "Sun, 11 Oct 2026 09:30:00 -0400"
    message.set_content(body)
    payload = message.as_bytes()
    moved = []

    class Connection(_NoopImapConnection):
        def select(self, mailbox, readonly=False):
            assert readonly is dry_run
            return "OK", [b"1"]

        def uid(self, command, uid, *args):
            assert command == "fetch"
            assert args == ("(BODY.PEEK[])",)
            return "OK", [b"payload"]

        def expunge(self):
            return "OK", []

    sr = SimpleNamespace(
        load_config=lambda _: SimpleNamespace(imap=SimpleNamespace(mailboxes=["INBOX"])),
        validate_imap=lambda _: None,
        open_imap_connection=lambda _: Connection(),
        extract_fetch_payload=lambda _: payload,
        decode_mime_header=lambda value: str(value or ""),
        extract_message_bodies=lambda _: (body, ""),
        extract_attachments=lambda _: (
            [("notice.pdf", "application/pdf", b"%PDF-1.4\n")] if pdf else []
        ),
        mark_message_read=lambda *_: None,
        move_message=lambda _conn, uid, label: moved.append((uid, label)),
    )
    settings = {"receipt_dir": str(tmp_path), "email": {"shock_relay_config": "/tmp/example"}}
    with (
        patch("intake.email._load_shock_relay", return_value=sr),
        patch("intake.email._search_receipt_uids", return_value=[42]) as search,
        patch("intake.email._process_reply_messages"),
        patch("intake.email._upsert_html_receipt") as inline,
    ):
        result = fetch_receipts(settings, db, verbose=False, dry_run=dry_run)
    assert result.errors == 0
    if dry_run:
        assert result.attachments_saved == 0
        assert inline.called is False
        assert moved == []
        assert db.execute("select count(*) from email_receipts").fetchone()[0] == 0
        assert list(tmp_path.rglob("*.pdf")) == []
        return
    assert result.attachments_saved == int(pdf)
    assert inline.called is expected_inline
    assert moved == [(42, "intake/processed")]
    assert "thank you for your payment" in search.call_args.args[2]
    assert "statement" in search.call_args.args[2]
    assert "no-reply@revolut.com" in search.call_args.kwargs["sender_hints"]


def test_amex_mycredit_guide_alert_is_non_spend():
    result = _extract_credit_reporting_email(
        "Your FICO® Score* has increased",
        "American Express MyCredit Guide <noreply@notificationmycredit-guide.americanexpress.com>",
        "Your FICO score increased to 750.",
    )
    assert result["merchant"] == "american express mycredit guide"
    assert result["total"] is None
    assert result["processed_by"] == "email-credit-reporting"


@pytest.mark.parametrize(
    ("subject", "sender", "category"),
    [
        ("Your statement is ready", "service@paypal.com", "documents/financial"),
        (
            "Reader, your September account statement is available.",
            "PayPal <noreply@service.paypal.com>",
            "documents/financial",
        ),
        (
            "Statement Available",
            "Huntington Bank <HuntingtonOnline@email.huntington.com>",
            "documents/financial",
        ),
        (
            "Reader, a reminder to pay your auto policy.",
            "Progressive <customerservice@e.progressive.com>",
            "documents/financial",
        ),
        (
            "Final Warning: Account Dormancy",
            "FanDuel Sportsbook <email@t.sb.fanduel.com>",
            "documents/financial",
        ),
        (
            "Reminder: Warning Of Account Dormancy",
            "FanDuel Sportsbook <email@t.sb.fanduel.com>",
            "documents/financial",
        ),
        (
            "You have a new statement online",
            "discover@services.discover.com",
            "documents/financial",
        ),
        ("We reviewed your credit report", "geico@et.geico.com", "documents/financial"),
        ("You’ve received your monthly interest", "noreply@wise.com", "documents/income"),
    ],
)
def test_finance_notices_are_documents_without_spend(db, tmp_path, subject, sender, category):
    settings = {"receipt_dir": str(tmp_path)}
    _upsert_html_receipt(
        db,
        42,
        "INBOX",
        subject,
        sender,
        "Sun, 11 Oct 2026 09:30:00 -0400",
        "Account notice: $500 balance. Payment Due Date: October 20, 2026. Minimum Payment Due: $35.00.",
        settings,
    )
    assert db.execute("select count(*) from receipts").fetchone()[0] == 0
    row = dict(db.execute("select * from documents").fetchone())
    assert row["category"] == category
    assert row["title"] == subject
    assert row["document_date"] == "2026-10-11"
    assert (tmp_path / row["receipt_dir"] / row["filename"]).exists()


def test_tinder_renewal_uses_total_instead_of_future_price():
    from intake.email import _extract_structured_finance_email

    result = _extract_structured_finance_email(
        "Your Tinder Gold Subscription Has Been Renewed!",
        "noreply@gotinder.com",
        "Your payment details\nTotal: $43.99\nExpiration Date: 11/01/2026\n"
        "Your subscription will automatically continue at $39.99 until you cancel.",
    )
    assert result["total"] == 43.99
    assert result["merchant"] == "tinder"
    assert result["receipt_date"] is None


@pytest.mark.parametrize("total,currency", [(12, "USD"), (25, "USD"), (30, "EUR")])
def test_binance_bundle_uses_fiat_total_not_asset_prices(total, currency):
    from intake.email import _extract_structured_finance_email

    result = _extract_structured_finance_email(
        "Binance.US | You’ve successfully purchased Example bundle",
        "hello@binance.us",
        "Status: Successful\nDate & time: 2026-10-10 22:14:27 (UTC)\n"
        f"Asset: BTC\nAsset cost: 85000.123\nAsset amount: 0.00001\nTotal: {total} {currency}\n",
    )
    assert result["total"] == total
    assert result["currency"] == currency
    assert result["receipt_date"] == date(2026, 10, 10)
    assert result["category"] == "finance/crypto"


@pytest.mark.parametrize(
    "subject,sender,body",
    [
        ("Now Live in Binance.US Wallet", "hello@binance.us", "Total: 25 USD"),
        (
            "Binance.US | You’ve successfully purchased BTC",
            "hello@binance.us",
            "Status: Failed\nTotal: 25 USD",
        ),
        ("Meet new people on Tinder", "noreply@gotinder.com", "Gold costs $39.99"),
    ],
)
def test_provider_promotions_and_failed_orders_are_not_structured_purchases(subject, sender, body):
    from intake.email import _extract_structured_finance_email

    assert _extract_structured_finance_email(subject, sender, body) is None


def test_tinder_renewal_stores_message_date_not_expiration(db, tmp_path):
    _upsert_html_receipt(
        db,
        42,
        "INBOX",
        "Your Tinder Gold Subscription Has Been Renewed!",
        "noreply@gotinder.com",
        "Mon, 12 Oct 2026 07:04:55 +0000",
        "Your payment details\nTotal: $43.99\nExpiration Date: 11/12/2026\n"
        "Future renewals will cost $39.99.",
        {"receipt_dir": str(tmp_path)},
    )
    row = dict(db.execute("select * from receipts").fetchone())
    assert row["merchant"] == "tinder"
    assert row["total"] == 43.99
    assert row["scan_date"] == "2026-10-12"
    assert row["processed_by"] == "email-tinder-renewal"


@pytest.mark.parametrize("trade_date", ["20261009", "10/09/2026"])
def test_schwab_trade_notice_is_investment_document_without_spend(db, tmp_path, trade_date):
    from intake.email import _extract_structured_finance_email

    subject = "Schwab Trade Confirmation eNotification account ending in 123"
    sender = "Schwab Alerts <donotreply@mail.schwab.com>"
    body = (
        f"Your trade confirmation(s) for {trade_date} is available at schwab.com/reports.\n"
        "Log in to view your report.\nSymbol:\nABC\nSecurity Description:\nExample Fund\n"
        "Symbol:\nXYZ\nSymbol:\nABC\n"
    )
    fields = _extract_structured_finance_email(subject, sender, body)
    assert fields["document_kind"] == "documents/investments"
    assert fields["document_date"] == "2026-10-09"
    assert fields["subject"] == "Securities: ABC, XYZ"
    with patch("intake.personal_finance.trigger_personal_finance") as downstream:
        _upsert_html_receipt(
            db,
            42,
            "INBOX",
            subject,
            sender,
            "Sat, 10 Oct 2026 06:27:28 -0600",
            body,
            {"receipt_dir": str(tmp_path)},
        )
    assert not downstream.called
    assert db.execute("select count(*) from receipts").fetchone()[0] == 0
    row = dict(db.execute("select * from documents").fetchone())
    assert row["document_date"] == "2026-10-09"
    assert row["scan_date"] == "2026-10-10"
    assert row["category"] == "documents/investments"
    assert row["processed_by"] == "email-schwab-trade-notice"
    artifact = tmp_path / row["receipt_dir"] / row["filename"]
    assert artifact.read_text() == body
    assert read_sidecar(artifact)["subject"] == "Securities: ABC, XYZ"


@pytest.mark.parametrize(
    "subject,sender",
    [
        ("Schwab Market Update", "donotreply@mail.schwab.com"),
        ("Schwab Trade Confirmation", "news@example.com"),
    ],
)
def test_schwab_promotions_and_other_senders_do_not_match(subject, sender):
    from intake.email import _extract_structured_finance_email

    assert _extract_structured_finance_email(subject, sender, "Symbol:\nABC") is None


def test_schwab_invalid_trade_date_uses_notification_date(db, tmp_path):
    _upsert_html_receipt(
        db,
        42,
        "INBOX",
        "Schwab Trade Confirmation eNotification",
        "donotreply@mail.schwab.com",
        "Sat, 10 Oct 2026 06:27:28 -0600",
        "Your trade confirmation(s) for 20261340 is available online.",
        {"receipt_dir": str(tmp_path)},
    )
    assert db.execute("select document_date from documents").fetchone()[0] == "2026-10-10"


def test_kalshi_relay_liquidation_is_investment_document_without_spend(db, tmp_path):
    from intake.email import _extract_structured_finance_email

    subject = "Your COPPER PERP position was liquidated"
    sender = "Kalshi <no-reply_at_kalshi_com_abc123_def456@privaterelay.appleid.com>"
    body = (
        "Your position was liquidated at $1234.56.\n"
        "LIQUIDATION EXECUTED\nCOPPER PERP - Long 4.0x\n"
        "Amount 7 contracts\nLiquidation price $1234.56\n"
    )
    fields = _extract_structured_finance_email(subject, sender, body)
    assert fields["document_kind"] == "documents/investments"
    assert fields["subject"] == "COPPER PERP"
    assert "7 contracts" in fields["body_excerpt"]
    assert "liquidation price $1234.56" in fields["body_excerpt"]
    _upsert_html_receipt(
        db,
        42,
        "[Gmail]/Spam",
        subject,
        sender,
        "Sun, 18 Oct 2026 22:00:16 +0000",
        body,
        {"receipt_dir": str(tmp_path)},
        structured_email=fields,
    )
    assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    row = dict(db.execute("SELECT * FROM documents").fetchone())
    assert row["document_kind"] == "documents/investments"
    assert row["processed_by"] == "email-kalshi-liquidation"
    assert row["document_date"] == "2026-10-18"


def test_amex_cash_advance_pin_ready_is_nonspend_document(db, tmp_path):
    from intake.email import _extract_structured_finance_email

    subject = "Your Cash Advance PIN is Ready"
    sender = "American Express <AmericanExpress@welcome.americanexpress.com>"
    body = (
        "Your PIN is ready below.\nLog into your account to view your secure PIN.\n"
        "View PIN\nCash Advance Fees and APRs will apply if you withdraw cash."
    )
    fields = _extract_structured_finance_email(subject, sender, body)
    assert fields["document_kind"] == "documents/financial"
    assert fields["merchant"] == "american express"
    assert "No cash withdrawal" in fields["body_excerpt"]
    assert "americanexpress.com" in _configured_receipt_sender_hints({})
    _upsert_html_receipt(
        db,
        43,
        "INBOX",
        subject,
        sender,
        "Thu, 15 Oct 2026 16:19:07 -0700",
        body,
        {"receipt_dir": str(tmp_path)},
        structured_email=fields,
    )
    assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    row = dict(db.execute("SELECT * FROM documents").fetchone())
    assert row["document_kind"] == "documents/financial"
    assert row["document_date"] == "2026-10-15"


@pytest.mark.parametrize(
    "subject,sender,body",
    [
        (
            "Your Cash Advance PIN is Ready",
            "American Express <other@example.com>",
            "Your PIN is ready. View PIN",
        ),
        (
            "Your Cash Advance PIN is Ready",
            "AmericanExpress@welcome.americanexpress.com",
            "Apply for cash advance today. View PIN",
        ),
        (
            "Your cash advance was completed",
            "AmericanExpress@welcome.americanexpress.com",
            "Your PIN is ready. View PIN",
        ),
    ],
)
def test_amex_cash_advance_pin_notice_rejects_other_messages(subject, sender, body):
    from intake.email import _extract_structured_finance_email

    assert _extract_structured_finance_email(subject, sender, body) is None


@pytest.mark.parametrize(
    "subject,sender,body",
    [
        (
            "Your COPPER PERP position was liquidated",
            "Kalshi <attacker@example.com>",
            "LIQUIDATION EXECUTED Amount 7 contracts Liquidation price $1234.56",
        ),
        (
            "New COPPER PERP offer",
            "no-reply_at_kalshi_com_abc123_def456@privaterelay.appleid.com",
            "LIQUIDATION EXECUTED Amount 7 contracts Liquidation price $1234.56",
        ),
        (
            "Your COPPER PERP position was liquidated",
            "no-reply_at_kalshi_com_abc123_def456@privaterelay.appleid.com",
            "Complete your payment at https://example.com",
        ),
    ],
)
def test_kalshi_liquidation_rejects_other_messages(subject, sender, body):
    from intake.email import _extract_structured_finance_email

    assert _extract_structured_finance_email(subject, sender, body) is None


def test_spam_recovery_requires_google_apple_relay_authentication():
    message = EmailMessage()
    message["Authentication-Results"] = (
        "mx.google.com; dkim=pass header.i=@privaterelay.appleid.com; "
        "spf=pass; dmarc=pass header.from=appleid.com"
    )
    assert _has_google_apple_relay_authentication(message)
    spoofed = EmailMessage()
    spoofed["Authentication-Results"] = (
        "other.example; dkim=pass header.i=@privaterelay.appleid.com; dmarc=pass"
    )
    assert not _has_google_apple_relay_authentication(spoofed)


def test_smbc_pending_cash_award_is_nonspend_document(db, tmp_path):
    from intake.email import _extract_structured_finance_email

    subject = "GLOBAL PASS Start Program Cash Award Notice"
    sender = "SMBC Trust Bank <info@smbctb.co.jp>"
    body = (
        "Your GLOBAL PASS usage amount has met the required condition, and we will "
        "deposit 10 US Dollar into your PRESTIA MultiMoney USD Savings Deposit account "
        "in approximately a week. The provision of cash award may be delayed."
    )
    parsed = _extract_structured_finance_email(subject, sender, body)
    assert parsed["document_kind"] == "documents/financial"
    assert parsed["merchant"] == "SMBC Trust Bank"
    assert "will deposit 10 US Dollar" in parsed["body_excerpt"]
    _upsert_html_receipt(
        db,
        34841,
        "INBOX",
        subject,
        sender,
        "Thu, 15 Oct 2026 23:00:50 -0600",
        body,
        {"receipt_dir": str(tmp_path)},
        structured_email=parsed,
    )
    row = db.execute("SELECT * FROM documents WHERE filename='email_34841_INBOX.txt'").fetchone()
    assert row["document_kind"] == "documents/financial"
    assert (
        db.execute(
            "SELECT count(*) FROM receipts WHERE filename='email_34841_INBOX.txt'"
        ).fetchone()[0]
        == 0
    )
    assert "info@smbctb.co.jp" in _configured_receipt_sender_hints({})


@pytest.mark.parametrize(
    "subject,sender,body",
    [
        (
            "GLOBAL PASS Start Program Cash Award Notice",
            "news@example.com",
            "We will deposit 10 US Dollar.",
        ),
        ("SMBC newsletter", "info@smbctb.co.jp", "We will deposit 10 US Dollar."),
        (
            "GLOBAL PASS Start Program Cash Award Notice",
            "info@smbctb.co.jp",
            "Apply now for an award.",
        ),
    ],
)
def test_smbc_award_parser_rejects_other_messages(subject, sender, body):
    from intake.email import _extract_structured_finance_email

    assert _extract_structured_finance_email(subject, sender, body) is None


def test_wise_transfer_setup_is_pending_nonspend_document(db, tmp_path):
    from intake.email import _extract_structured_finance_email

    subject = "Transfer set up (#1234567890)"
    sender = "Wise <noreply@wise.com>"
    body = (
        "Communication code: test\nHello Reader,\nYour transfer of 250 USD is set up. "
        "Your money will get to us in 3 working days. As soon as it gets here, "
        "we'll convert it. Then we'll send it out to your account."
    )
    parsed = _extract_structured_finance_email(subject, sender, body)
    assert parsed["document_kind"] == "documents/financial"
    assert parsed["merchant"] == "Wise"
    assert parsed["body_excerpt"].startswith("Your transfer of 250 USD is set up")
    _upsert_html_receipt(
        db,
        44,
        "INBOX",
        subject,
        sender,
        "Fri, 16 Oct 2026 04:31:28 +0000",
        body,
        {"receipt_dir": str(tmp_path)},
        structured_email=parsed,
    )
    row = db.execute("SELECT * FROM documents WHERE filename='email_44_INBOX.txt'").fetchone()
    assert row["document_kind"] == "documents/financial"
    assert (
        db.execute("SELECT count(*) FROM receipts WHERE filename='email_44_INBOX.txt'").fetchone()[
            0
        ]
        == 0
    )


@pytest.mark.parametrize(
    "subject,sender,body",
    [
        (
            "Transfer set up (#1234567890)",
            "news@example.com",
            "Your transfer of 250 USD is set up.",
        ),
        (
            "Transfer complete (#1234567890)",
            "noreply@wise.com",
            "Your transfer of 250 USD is set up.",
        ),
        (
            "Transfer set up (#1234567890)",
            "noreply@wise.com",
            "Your transfer of 250 USD arrived.",
        ),
    ],
)
def test_wise_setup_parser_requires_pending_status(subject, sender, body):
    from intake.email import _extract_structured_finance_email

    assert _extract_structured_finance_email(subject, sender, body) is None
