"""Structured finance and payment email extraction helpers."""

from __future__ import annotations

import re
from contextlib import suppress
from datetime import date, datetime
from email.utils import parseaddr

from intake.purchaser import normalize_purchaser

_RECEIPT_PATTERNS = re.compile(
    r"\b(total|amount\s+due|order\s+total|charged|subtotal|invoice|receipt\s+#)\b",
    re.IGNORECASE,
)

_PAYMENT_EMAIL_PATTERNS = re.compile(
    r"\b("
    r"thanks\s+for\s+(?:scheduling\s+)?your\s+payment|"
    r"we'?ve\s+received\s+your\s+payment|"
    r"your\s+scheduled\s+payment|"
    r"payment\s+amount|"
    r"payment\s+post\s+date|"
    r"scheduled\s+date|"
    r"posted\s+date|"
    r"payment\s+of\s+\$|"
    r"you\s+paid\s+\$|"
    r"you\s+paid\s+\w+|"  # Venmo: "You paid Name $N" (name precedes $)
    r"paid\s+you\s+\$|"
    r"\w+\s+paid\s+you\s+\$|"  # Venmo: "Name paid you $N"
    r"charged\s+you\s+\$|"
    r"\w+\s+charged\s+you\s+\$|"  # Venmo: "Name charged you $N"
    r"money\s+added\s+to\s+your\s+account|"
    r"you\s+added\s+\d+(?:\.\d{2})?\s+[A-Z]{3}\s+to\s+your\s+account"
    r")\b",
    re.IGNORECASE,
)

_CARD_SCHEDULED_NOTICE_RE = re.compile(
    r"\b("
    r"thanks\s+for\s+scheduling\s+your\s+payment|"
    r"your\s+scheduled\s+payment(?!\s+was\b)|"
    r"(?:payment|card\s+payment)\s+is\s+scheduled|"
    r"is\s+scheduled\s+for"
    r")\b",
    re.IGNORECASE,
)

_PAYPAL_UPCOMING_PURCHASE_RE = re.compile(
    r"\byou\s+have\s+an\s+upcoming\b(?=.*\bscheduled\b)(?=.*\bpurchase\b)",
    re.IGNORECASE | re.DOTALL,
)

_ANY_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)

_PAYMENT_AMOUNT_RE = re.compile(
    r"\bpayment\s+amount\s*:\s*\$?\s*(\d+(?:\.\d{2})?)\b", re.IGNORECASE
)

_PAYMENT_OF_RE = re.compile(r"\bpayment\s+of\s+\$?\s*(\d+(?:\.\d{2})?)\b", re.IGNORECASE)

_PAYMENT_INLINE_AMOUNT_RE = re.compile(r"\bamount\s*:\s*\$?\s*(\d+(?:\.\d{2})?)\b", re.IGNORECASE)

_ACCOUNT_ENDING_RE = re.compile(
    r"\baccount(?:\s+number)?\s+ending\s+(?:in|with)\s+(\d{4})\b",
    re.IGNORECASE,
)

_LAST4_RE = re.compile(r"\blast\s*4\s*#?\s*:\s*(\d{4})\b", re.IGNORECASE)

_CAPITAL_ONE_CARD_RE = re.compile(r"\bcredit\s+card\.\.\.(\d{4})\b", re.IGNORECASE)

_PAYMENT_GREETING_RE = re.compile(
    r"(?:^|\n)\s*([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)\s*,", re.MULTILINE
)

_PAYMENT_DATE_TEXT_PATTERN = (
    r"(?:"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r")\.?\s+\d{1,2},\s+\d{4}"
)

_PAYMENT_DATE_RE = re.compile(
    rf"\b{_PAYMENT_DATE_TEXT_PATTERN}\b",
    re.IGNORECASE,
)

_PAYMENT_MDY_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")

_POSTED_TO_ACCOUNT_DATE_RE = re.compile(
    rf"\bposted\s+to\s+your\s+account\s+on\s+({_PAYMENT_DATE_TEXT_PATTERN})\b",
    re.IGNORECASE,
)

_SCHEDULED_FOR_DATE_RE = re.compile(
    rf"\bscheduled\s+for\s+({_PAYMENT_DATE_TEXT_PATTERN})\b",
    re.IGNORECASE,
)

_DUE_ON_DATE_RE = re.compile(r"\bdue\s+on\s+(\d{2}/\d{2}/\d{4})\b", re.IGNORECASE)

_CAPITAL_ONE_SENDER_HINT = "capitalone@notification.capitalone.com"

_DISCOVER_SENDER_HINT = "discover@services.discover.com"

_AMEX_SENDER_HINT = "americanexpress.com"

_EQUIFAX_SENDER_HINT = "no-reply@equifax.com"

_EXPERIAN_SENDER_HINT = "support@s.usa.experian.com"

_TRANSUNION_SENDER_HINT = "transunion"

_CREDIT_KARMA_SENDER_HINT = "creditkarma"

_AMEX_ACCOUNT_ENDING_RE = re.compile(r"\baccount\s+ending\s*:\s*(\d{4,5})\b", re.IGNORECASE)

_AMEX_CARDHOLDER_RE = re.compile(
    r"([A-Z][A-Z ]{3,}[A-Z])\s*\n\s*Account\s+Ending\b",
    re.MULTILINE,
)

_PROGRESSIVE_SENDER_HINT = "customerservice@e.progressive.com"

_PAYPAL_SENDER_HINT = "service@paypal.com"

_PAYPAL_SUBJECT_AMOUNT_RE = re.compile(r":\s*\$?\s*(\d+(?:\.\d{2})?)\s+[A-Z]{3}\b")

_VENMO_SENDER_HINT = "venmo@venmo.com"

_VENMO_YOU_PAID_RE = re.compile(
    r"^you\s+paid\s+(.+?)\s+\$\s*(\d+(?:\.\d{2})?)",
    re.IGNORECASE | re.MULTILINE,
)

_VENMO_PAID_YOU_RE = re.compile(
    r"^(.+?)\s+paid\s+you\s+\$\s*(\d+(?:\.\d{2})?)",
    re.IGNORECASE | re.MULTILINE,
)

_VENMO_CHARGED_YOU_RE = re.compile(
    r"^(.+?)\s+charged\s+you\s+\$\s*(\d+(?:\.\d{2})?)",
    re.IGNORECASE | re.MULTILINE,
)

_IRS_SENDER_HINT = "account.irs.gov"

_NELNET_SENDER_HINT = "nelnet.studentaid.gov"

_NELNET_MDY_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")

_WISE_SENDER_HINT = "noreply@wise.com"

_WISE_MONEY_ADDED_RE = re.compile(
    r"\byou\s+added\s+(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s+([A-Z]{3})\s+to\s+your\s+account\b",
    re.IGNORECASE,
)

_XANTERRA_SENDER_HINT = "xanterra.com"

_XANTERRA_WELCOME_RE = re.compile(
    r"(?im)^\s*Welcome,\s*([A-Z][A-Za-z]+(?:[ \t]+[A-Z][A-Za-z]+){1,3})\s*$"
)

_CREDIT_REPORTING_ALLCAPS_GREETING_RE = re.compile(
    r"(?m)^\s*([A-Z]{3,}(?:\s+[A-Z]{2,}){0,2})\s*,\s*$"
)

_CREDIT_REPORTING_INLINE_GREETING_RE = re.compile(
    r"\b(?:keep it up|hi|hello)\s*,?\s*([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})\b",
    re.IGNORECASE,
)

_BINANCE_SENDER_HINT = "post.binance.us"

_PAYPAL_YOU_PAID_RE = re.compile(
    r"\byou paid\s+\$?\s*(\d+(?:\.\d{2})?)\s+[A-Z]{3}\s+to\s+(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

# PayPal and Venmo both send this exact phrasing for in-app crypto purchases,
# e.g. "You bought 0.02027474 Bitcoin Cash (BCH) for $5.11 USD". Neither the
# "paid ... to <merchant>" nor the peer-to-peer "paid you"/"charged you"
# patterns above match this shape, so it needs its own extractor.
_CRYPTO_PURCHASE_RE = re.compile(
    r"\byou\s+bought\s+[\d,.]+\s+.+?\(([A-Z]{2,6})\)\s+for\s+\$?\s*(\d+(?:\.\d{2})?)\s*USD\b",
    re.IGNORECASE,
)


def _extract_crypto_purchase(plain_text: str) -> tuple[str, float] | None:
    """Return (ticker, amount) for a "you bought ... (TICKER) for $N USD" line."""
    match = _CRYPTO_PURCHASE_RE.search(plain_text)
    if not match:
        return None
    with suppress(ValueError):
        return match.group(1).upper(), float(match.group(2))
    return None


def _is_known_non_receipt_notice(subject: str, sender: str, plain_text: str) -> bool:
    """Return True for payment notices that should be filed but not ingested."""
    combined = f"{subject}\n{plain_text or ''}"
    sender_lower = str(sender or "").lower()
    if "you paid" not in combined.lower() and _PAYPAL_UPCOMING_PURCHASE_RE.search(combined):
        return True
    if (
        _CAPITAL_ONE_SENDER_HINT in sender_lower
        or _DISCOVER_SENDER_HINT in sender_lower
        or _CARD_SCHEDULED_NOTICE_RE.search(combined)
    ):
        return bool(_CARD_SCHEDULED_NOTICE_RE.search(combined))
    if _IRS_SENDER_HINT in sender_lower and not re.search(r"\$\s*\d+", combined):
        return True
    if _VENMO_SENDER_HINT in sender_lower and re.search(
        r"transaction\s+history", combined, re.IGNORECASE
    ):
        return True
    return bool(
        _BINANCE_SENDER_HINT in sender_lower and re.search(r"\bstaking\b", combined, re.IGNORECASE)
    )


def _category_exists(rules: dict, category_path: str) -> bool:
    """Return True when *category_path* exists in the configured rule tree."""
    parts = category_path.split("/")
    if len(parts) == 1:
        return parts[0] in rules
    if len(parts) != 2:
        return False
    category, subcategory = parts
    return category in rules and isinstance(rules[category].get(subcategory), dict)


def _preferred_payment_category(rules: dict) -> str:
    """Return the best configured category for card-payment notification emails."""
    for candidate in ("finance/credit-card-payment", "general/credit-card-payment", "finance"):
        if _category_exists(rules, candidate):
            return candidate
    return "general"


def _preferred_credit_reporting_category(rules: dict) -> str:
    """Return the best configured category for credit-reporting notification emails."""
    for candidate in ("finance/credit-reporting", "general/credit-reporting", "finance"):
        if _category_exists(rules, candidate):
            return candidate
    return "general"


def _parse_payment_date_text(text: str) -> date | None:
    """Parse a month-name payment date used in issuer notification emails."""
    cleaned = " ".join(text.replace("\n", " ").split())
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _parse_numeric_payment_date(text: str) -> date | None:
    """Parse an ``MM/DD/YYYY`` payment date used in billing reminders."""
    cleaned = " ".join(text.replace("\n", " ").split())
    try:
        return datetime.strptime(cleaned, "%m/%d/%Y").date()
    except ValueError:
        return None


def _extract_labeled_block_value(plain_text: str, labels: tuple[str, ...]) -> str | None:
    """Return the value following one of *labels* on the same or next line."""
    lines = [line.strip() for line in plain_text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if not any(label in lowered for label in labels):
            continue
        if ":" in line:
            _, after = line.split(":", 1)
            after = after.strip()
            if after:
                return after
        for candidate in lines[idx + 1 : idx + 3]:
            if candidate:
                return candidate
    return None


def _extract_labeled_money_amount(plain_text: str, labels: tuple[str, ...]) -> float | None:
    """Return the first money amount printed after one of *labels*."""
    flattened = " ".join(plain_text.replace("\xa0", " ").split())
    for label in labels:
        label_pattern = r"\s+".join(re.escape(part) for part in label.split())
        match = re.search(
            rf"\b{label_pattern}\s*:?\s*\$?\s*([0-9,]+(?:\.\d{{2}})?)\b",
            flattened,
            re.IGNORECASE,
        )
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _extract_payment_amount(subject: str, plain_text: str) -> float | None:
    """Extract the payment amount from a card-payment notification email."""
    for pattern in (_PAYMENT_AMOUNT_RE, _PAYMENT_OF_RE, _PAYMENT_INLINE_AMOUNT_RE):
        match = pattern.search(plain_text)
        if match:
            return float(match.group(1))
    match = _PAYMENT_OF_RE.search(subject)
    if match:
        return float(match.group(1))
    return None


def _extract_payment_card_last4(plain_text: str) -> str | None:
    """Extract the card account last4 from a card-payment notification email."""
    for pattern in (_CAPITAL_ONE_CARD_RE, _ACCOUNT_ENDING_RE, _LAST4_RE):
        match = pattern.search(plain_text)
        if match:
            return match.group(1)
    return None


def _extract_payment_purchaser(plain_text: str) -> str | None:
    """Extract the purchaser from a card-payment notification email."""
    for match in _ANY_EMAIL_RE.finditer(plain_text):
        normalized = normalize_purchaser(match.group(0))
        if normalized is not None:
            return normalized
    match = _PAYMENT_GREETING_RE.search(plain_text)
    if match:
        normalized = normalize_purchaser(match.group(1))
        if normalized is not None:
            return normalized
    return None


def _extract_credit_reporting_purchaser(plain_text: str) -> str | None:
    """Extract the purchaser from a credit-reporting alert email."""
    purchaser = _extract_payment_purchaser(plain_text)
    if purchaser is not None:
        return purchaser

    for pattern in (_CREDIT_REPORTING_ALLCAPS_GREETING_RE, _CREDIT_REPORTING_INLINE_GREETING_RE):
        match = pattern.search(plain_text)
        if not match:
            continue
        normalized = normalize_purchaser(match.group(1))
        if normalized is not None:
            return normalized
    return None


def _extract_payment_effective_date(subject: str, plain_text: str) -> date | None:
    """Extract the posted/scheduled payment date from a notification email."""
    for labels in (
        ("posted date", "payment post date"),
        ("scheduled date",),
    ):
        value = _extract_labeled_block_value(plain_text, labels)
        if value is None:
            continue
        match = _PAYMENT_DATE_RE.search(value)
        if match:
            parsed = _parse_payment_date_text(match.group(0))
            if parsed is not None:
                return parsed

    for pattern in (_POSTED_TO_ACCOUNT_DATE_RE, _SCHEDULED_FOR_DATE_RE):
        match = pattern.search(f"{subject}\n{plain_text}")
        if match:
            parsed = _parse_payment_date_text(match.group(1))
            if parsed is not None:
                return parsed
    match = _DUE_ON_DATE_RE.search(f"{subject}\n{plain_text}")
    if match:
        return _parse_numeric_payment_date(match.group(1))
    return None


def _extract_credit_card_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for known credit-card payment-notification emails."""
    sender_lower = sender.lower()
    if _CAPITAL_ONE_SENDER_HINT in sender_lower:
        merchant = "capital one"
    elif _DISCOVER_SENDER_HINT in sender_lower:
        merchant = "discover"
    else:
        return None

    if not _PAYMENT_EMAIL_PATTERNS.search(f"{subject}\n{plain_text}"):
        return None
    if _is_known_non_receipt_notice(subject, sender, plain_text):
        return None

    return {
        "merchant": merchant,
        "total": _extract_payment_amount(subject, plain_text),
        "receipt_date": _extract_payment_effective_date(subject, plain_text),
        "purchaser": _extract_payment_purchaser(plain_text),
        "card_last4": _extract_payment_card_last4(plain_text),
        "category": "finance/credit-card-payment",
    }


def _extract_paypal_receipt_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for PayPal merchant receipt emails."""
    if _PAYPAL_SENDER_HINT not in sender.lower():
        return None

    crypto = _extract_crypto_purchase(plain_text)
    if crypto is not None:
        ticker, amount = crypto
        date_match = _PAYMENT_DATE_RE.search(plain_text)
        receipt_date = _parse_payment_date_text(date_match.group(0)) if date_match else None
        return {
            "merchant": f"PayPal Crypto ({ticker})",
            "total": amount,
            "receipt_date": receipt_date,
            "purchaser": _extract_payment_purchaser(plain_text),
            "card_last4": None,
            "category": "finance",
        }

    if "you paid" not in plain_text.lower():
        return None

    merchant = _extract_labeled_block_value(plain_text, ("merchant",))
    if not merchant and ":" in subject:
        merchant = subject.split(":", 1)[0].strip()
    if not merchant:
        match = _PAYPAL_YOU_PAID_RE.search(plain_text)
        if match:
            merchant = match.group(2).strip()

    total = None
    total_value = _extract_labeled_block_value(plain_text, ("total",))
    if total_value:
        match = re.search(r"\$?\s*(\d+(?:\.\d{2})?)", total_value)
        if match:
            total = float(match.group(1))
    if total is None:
        match = _PAYPAL_YOU_PAID_RE.search(plain_text)
        if match:
            total = float(match.group(1))
        else:
            match = _PAYPAL_SUBJECT_AMOUNT_RE.search(subject)
            if match:
                total = float(match.group(1))

    date_value = _extract_labeled_block_value(plain_text, ("transaction date",))
    receipt_date = _parse_payment_date_text(date_value) if date_value else None

    if merchant is None and total is None:
        # Sender/phrase hints matched, but nothing was actually extracted —
        # this is some PayPal email shape this function doesn't recognize.
        # Returning None lets the AI fallback (or generic parsing) have a
        # shot instead of locking in an empty stub.
        return None

    return {
        "merchant": merchant,
        "total": total,
        "receipt_date": receipt_date,
        "purchaser": _extract_payment_purchaser(plain_text),
        "card_last4": None,
        "category": None,
    }


def _extract_irs_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for IRS online payment confirmation emails."""
    if _IRS_SENDER_HINT not in sender.lower():
        return None
    if not re.search(
        r"confirmation\s+of\s+(?:scheduled\s+)?(?:payment\s+)?transaction"
        r"|you.?ve\s+successfully\s+submitted\s+your\s+payment",
        plain_text,
        re.IGNORECASE,
    ):
        return None

    total = None
    total_value = _extract_labeled_block_value(plain_text, ("total amount",))
    if total_value:
        match = re.search(r"\$?\s*(\d+(?:\.\d{2})?)", total_value)
        if match:
            total = float(match.group(1))

    receipt_date = None
    date_value = _extract_labeled_block_value(plain_text, ("date",))
    if date_value:
        receipt_date = _parse_payment_date_text(date_value)

    return {
        "merchant": "IRS",
        "total": total,
        "receipt_date": receipt_date,
        "purchaser": None,
        "card_last4": None,
        "category": None,
    }


def _extract_nelnet_billing_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for Nelnet student loan billing statement emails."""
    if _NELNET_SENDER_HINT not in sender.lower():
        return None
    if "billing statement" not in subject.lower() and "billing statement" not in plain_text.lower():
        return None

    total = None
    amount_value = _extract_labeled_block_value(plain_text, ("amount due",))
    if amount_value:
        match = re.search(r"\$?\s*(\d+(?:\.\d{2})?)", amount_value)
        if match:
            total = float(match.group(1))

    receipt_date = None
    date_value = _extract_labeled_block_value(plain_text, ("due date",))
    if date_value:
        date_match = _NELNET_MDY_DATE_RE.search(date_value)
        if date_match:
            receipt_date = _parse_numeric_payment_date(date_match.group(1))

    return {
        "merchant": "Nelnet",
        "total": total,
        "receipt_date": receipt_date,
        "purchaser": _extract_payment_purchaser(plain_text),
        "card_last4": None,
        "category": None,
    }


def _extract_venmo_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for Venmo payment notification emails."""
    if _VENMO_SENDER_HINT not in sender.lower():
        return None

    combined = f"{subject}\n{plain_text}"

    crypto = _extract_crypto_purchase(plain_text)
    if crypto is not None:
        ticker, amount = crypto
        date_match = _PAYMENT_DATE_RE.search(plain_text)
        receipt_date = _parse_payment_date_text(date_match.group(0)) if date_match else None
        return {
            "merchant": f"Venmo Crypto ({ticker})",
            "total": amount,
            "receipt_date": receipt_date,
            "purchaser": _extract_payment_purchaser(plain_text),
            "card_last4": None,
            "category": "finance",
        }

    merchant: str | None = None
    total: float | None = None

    is_income = False
    for pattern in (_VENMO_YOU_PAID_RE, _VENMO_PAID_YOU_RE, _VENMO_CHARGED_YOU_RE):
        match = pattern.search(combined)
        if match:
            merchant = match.group(1).strip()
            with suppress(ValueError, IndexError):
                total = float(match.group(2))
            # "<name> paid you $X" is money credited to the account (income),
            # unlike "you paid <name>" / "<name> charged you" (both spend).
            is_income = pattern is _VENMO_PAID_YOU_RE
            break

    if merchant is None and total is None:
        # Sender hint matched, but none of the peer-to-peer patterns fired —
        # let the AI fallback (or generic parsing) have a shot instead of
        # locking in an empty stub.
        return None

    receipt_date: date | None = None
    date_match = _PAYMENT_DATE_RE.search(plain_text)
    if date_match:
        receipt_date = _parse_payment_date_text(date_match.group(0))

    return {
        "merchant": merchant,
        "total": total,
        "receipt_date": receipt_date,
        "purchaser": _extract_payment_purchaser(plain_text),
        "card_last4": None,
        "category": None,
        "is_income": is_income,
    }


def _extract_wise_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for Wise money-movement notification emails."""
    if _WISE_SENDER_HINT not in sender.lower():
        return None
    if "money added to your account" not in subject.lower():
        return None

    match = _WISE_MONEY_ADDED_RE.search(plain_text)
    total = float(match.group(1).replace(",", "")) if match else None

    return {
        "merchant": "Wise",
        "total": total,
        "receipt_date": None,
        "purchaser": None,
        "card_last4": None,
        "category": "finance",
    }


def _extract_xanterra_reservation_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for Xanterra/Yellowstone reservation confirmations."""
    combined = f"{subject}\n{plain_text}"
    if _XANTERRA_SENDER_HINT not in sender.lower():
        return None
    if not re.search(r"\breservation\s+confirmation\b", subject, re.IGNORECASE):
        return None
    if "yellowstone national park" not in combined.lower():
        return None

    total = _extract_labeled_money_amount(
        plain_text,
        (
            "deposit paid",
            "deposit required",
            "reservation total",
            "lodging total",
        ),
    )
    booking_date = None
    booking_value = _extract_labeled_block_value(plain_text, ("original booking date",))
    if booking_value:
        booking_date = _parse_numeric_payment_date(booking_value)

    lodging_amount = _extract_labeled_money_amount(
        plain_text,
        ("lodging total", "lodging", "nightly rate"),
    )
    taxes_amount = _extract_labeled_money_amount(plain_text, ("taxes/fees", "taxes & fees"))
    items = []
    if lodging_amount is not None:
        items.append(
            {
                "description": "Bridge Bay Campground lodging",
                "amount": lodging_amount,
                "quantity": None,
                "unit_price": None,
            }
        )
    if taxes_amount is not None:
        items.append(
            {
                "description": "Taxes/Fees",
                "amount": taxes_amount,
                "quantity": None,
                "unit_price": None,
            }
        )

    purchaser = None
    match = _XANTERRA_WELCOME_RE.search(plain_text)
    if match:
        purchaser = normalize_purchaser(match.group(1))

    return {
        "merchant": "Yellowstone National Park Lodges",
        "total": total,
        "receipt_date": booking_date,
        "purchaser": purchaser,
        "card_last4": None,
        "category": "outdoor/park",
        "processed_by": "email-reservation",
        "items": items,
    }


def _extract_amex_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for American Express payment confirmation emails."""
    if _AMEX_SENDER_HINT not in sender.lower():
        return None
    if "payment" not in subject.lower():
        return None

    amount = _extract_labeled_money_amount(plain_text, ("payment amount",))

    receipt_date = None
    date_value = _extract_labeled_block_value(plain_text, ("processed on",))
    if date_value:
        m = _PAYMENT_DATE_RE.search(date_value)
        if m:
            receipt_date = _parse_payment_date_text(m.group(0))
    if receipt_date is None:
        m = re.search(
            r"processed\s+on\s+(" + _PAYMENT_DATE_TEXT_PATTERN + r")\b",
            f"{subject}\n{plain_text}",
            re.IGNORECASE,
        )
        if m:
            receipt_date = _parse_payment_date_text(m.group(1))

    purchaser = None
    m = _AMEX_CARDHOLDER_RE.search(plain_text)
    if m:
        purchaser = normalize_purchaser(m.group(1).strip())

    card_last4 = None
    m = _AMEX_ACCOUNT_ENDING_RE.search(plain_text)
    if m:
        card_last4 = m.group(1)

    return {
        "merchant": "american express",
        "total": amount,
        "receipt_date": receipt_date,
        "purchaser": purchaser,
        "card_last4": card_last4,
        "category": "finance/credit-card-payment",
        "processed_by": "email-payment-amex",
    }


def _extract_credit_reporting_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for known credit-reporting notification emails."""
    sender_lower = sender.lower()
    combined = f"{subject}\n{plain_text}"
    combined_lower = combined.lower()

    merchant: str | None = None
    if _EQUIFAX_SENDER_HINT in sender_lower and (
        "credit report" in combined_lower or "credit score" in combined_lower
    ):
        merchant = "equifax"
    elif _EXPERIAN_SENDER_HINT in sender_lower and (
        "fico" in combined_lower
        or "credit score" in combined_lower
        or "see your alerts" in combined_lower
    ):
        merchant = "experian"
    elif _TRANSUNION_SENDER_HINT in sender_lower and (
        "credit report" in combined_lower
        or "credit score" in combined_lower
        or "credit monitoring" in combined_lower
    ):
        merchant = "transunion"
    elif _CREDIT_KARMA_SENDER_HINT in sender_lower and (
        "credit report" in combined_lower
        or "credit score" in combined_lower
        or "credit monitoring" in combined_lower
    ):
        merchant = "credit karma"
    elif _CAPITAL_ONE_SENDER_HINT in sender_lower and (
        "creditwise" in combined_lower and "credit score" in combined_lower
    ):
        merchant = "capital one creditwise"
    elif (
        "americanexpress.com" in sender_lower
        and ("mycredit" in sender_lower or "mycredit guide" in combined_lower)
        and ("fico" in combined_lower or "credit score" in combined_lower)
    ):
        merchant = "american express mycredit guide"

    if merchant is None:
        return None

    return {
        "merchant": merchant,
        "total": None,
        "receipt_date": None,
        "purchaser": _extract_credit_reporting_purchaser(plain_text),
        "card_last4": None,
        "category": "finance/credit-reporting",
        "processed_by": "email-credit-reporting",
    }


def _extract_structured_finance_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for known finance/payment notification emails."""
    return (
        _extract_kalshi_liquidation_email(subject, sender, plain_text)
        or _extract_finance_notice_email(subject, sender, plain_text)
        or _extract_schwab_trade_notice_email(subject, sender, plain_text)
        or _extract_tinder_renewal_email(subject, sender, plain_text)
        or _extract_binance_purchase_email(subject, sender, plain_text)
        or _extract_credit_card_payment_email(subject, sender, plain_text)
        or _extract_amex_payment_email(subject, sender, plain_text)
        or _extract_paypal_receipt_email(subject, sender, plain_text)
        or _extract_venmo_payment_email(subject, sender, plain_text)
        or _extract_irs_payment_email(subject, sender, plain_text)
        or _extract_nelnet_billing_email(subject, sender, plain_text)
        or _extract_wise_payment_email(subject, sender, plain_text)
        or _extract_xanterra_reservation_email(subject, sender, plain_text)
        or _extract_credit_reporting_email(subject, sender, plain_text)
    )


def _extract_kalshi_liquidation_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Keep executed perpetual liquidations as investment notices, not spending."""
    address = parseaddr(sender)[1].lower()
    if not re.fullmatch(
        r"no-reply_at_kalshi_com_[a-z0-9]+_[a-z0-9]+@privaterelay\.appleid\.com",
        address,
    ):
        return None
    match = re.fullmatch(
        r"Your ([A-Z0-9][A-Z0-9 .-]{0,35}) PERP position was liquidated",
        subject.strip(),
        re.I,
    )
    if not match or not re.search(r"\bLIQUIDATION EXECUTED\b", plain_text, re.I):
        return None
    quantity = re.search(r"\bAmount\s+(\d+(?:\.\d+)?)\s+contracts?\b", plain_text, re.I)
    price = re.search(r"\bLiquidation price\s+\$([\d,]+(?:\.\d{2})?)\b", plain_text, re.I)
    if not quantity or not price:
        return None
    asset = match.group(1).upper()
    excerpt = f"{asset} PERP liquidation executed: {quantity.group(1)} contracts at liquidation price ${price.group(1)}."
    return {
        "merchant": "Kalshi",
        "document_kind": "documents/investments",
        "title": subject,
        "subject": f"{asset} PERP",
        "body_excerpt": excerpt,
        "processed_by": "email-kalshi-liquidation",
    }


def _extract_schwab_trade_notice_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Keep online brokerage confirmations without inventing spend or trade amounts."""
    if "donotreply@mail.schwab.com" not in sender.lower() or not re.search(
        r"\btrade\s+confirmations?\b", subject, re.IGNORECASE
    ):
        return None
    document_date = None
    dated = re.search(
        r"trade confirmation\(s\)\s+for\s+(\d{8}|\d{1,2}/\d{1,2}/\d{4})\b",
        plain_text,
        re.IGNORECASE,
    )
    if dated:
        with suppress(ValueError):
            value = dated.group(1)
            document_date = (
                datetime.strptime(value, "%m/%d/%Y" if "/" in value else "%Y%m%d")
                .date()
                .isoformat()
            )
    symbols = list(
        dict.fromkeys(
            re.findall(r"^Symbol:\s*\n\s*([A-Z][A-Z0-9.^/-]*)\s*$", plain_text, re.MULTILINE)
        )
    )
    security_summary = "Securities: " + ", ".join(symbols) if symbols else None
    return {
        "merchant": "charles schwab",
        "document_kind": "documents/investments",
        "title": subject,
        "document_date": document_date,
        "subject": security_summary,
        "body_excerpt": security_summary,
        "processed_by": "email-schwab-trade-notice",
    }


def _extract_tinder_renewal_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Extract a completed subscription renewal, excluding future pricing."""
    if "noreply@gotinder.com" not in sender.lower() or not re.search(
        r"\bsubscription\b.*\b(?:renewed|receipt)\b", subject, re.IGNORECASE
    ):
        return None
    total = _extract_labeled_money_amount(plain_text, ("total",))
    if total is None:
        return None
    return {
        "merchant": "tinder",
        "total": total,
        "receipt_date": None,
        "purchaser": None,
        "card_last4": None,
        "category": "sw",
        "processed_by": "email-tinder-renewal",
    }


def _extract_binance_purchase_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Extract a successful bundle purchase from its final fiat total."""
    if not any(hint in sender.lower() for hint in ("hello@binance.us", "post.binance.us")):
        return None
    if not re.search(r"successfully purchased", subject, re.IGNORECASE) or not re.search(
        r"^Status:\s*Successful\s*$", plain_text, re.IGNORECASE | re.MULTILINE
    ):
        return None
    match = re.search(
        r"^Total:\s*([\d,]+(?:\.\d+)?)\s+([A-Z]{3})\s*$",
        plain_text,
        re.MULTILINE,
    )
    if match is None:
        return None
    receipt_date = None
    timestamp = re.search(r"^Date & time:\s*(\d{4}-\d{2}-\d{2})\b", plain_text, re.MULTILINE)
    if timestamp:
        with suppress(ValueError):
            receipt_date = date.fromisoformat(timestamp.group(1))
    return {
        "merchant": "binance.us",
        "total": float(match.group(1).replace(",", "")),
        "currency": match.group(2),
        "receipt_date": receipt_date,
        "purchaser": _extract_payment_purchaser(plain_text),
        "card_last4": None,
        "category": "finance/crypto",
        "processed_by": "email-binance-purchase",
    }


def _extract_finance_notice_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Keep supported account notices as documents without inventing spending."""
    sender_lower = sender.lower()
    subject_lower = subject.lower()
    merchant = None
    kind = "documents/financial"
    excerpt = None
    if (
        any(hint in sender_lower for hint in ("service@paypal.com", "service.paypal.com"))
        and "statement" in subject_lower
    ):
        merchant = "paypal"
    elif "statement" in subject_lower:
        for provider, hint in (
            ("discover", "discover.com"),
            ("capital one", "capitalone.com"),
            ("american express", "americanexpress.com"),
            ("revolut", "revolut.com"),
            ("huntington bank", "email.huntington.com"),
        ):
            if hint in sender_lower:
                merchant = provider
                break
    elif _PROGRESSIVE_SENDER_HINT in sender_lower and re.search(
        r"\b(?:reminder to pay|ready to renew|policy renewal reminder|policy documents)\b",
        subject_lower,
    ):
        merchant = "progressive"
    elif (
        parseaddr(sender)[1].lower() == "americanexpress@welcome.americanexpress.com"
        and subject_lower.strip() == "your cash advance pin is ready"
        and re.search(r"\byour pin is ready\b", plain_text, re.I)
        and re.search(r"\bview pin\b", plain_text, re.I)
    ):
        merchant = "american express"
        excerpt = (
            "Cash Advance PIN is available after signing in to American Express. "
            "No cash withdrawal or advance is reported in this notice."
        )
    elif (
        parseaddr(sender)[1].lower() == "info@smbctb.co.jp"
        and "global pass" in subject_lower
        and "cash award notice" in subject_lower
        and re.search(r"\bwill deposit\b", plain_text, re.I)
    ):
        merchant = "SMBC Trust Bank"
        excerpt = re.sub(r"\s+", " ", plain_text).strip()[:500]
    elif "email@t.sb.fanduel.com" in sender_lower and "account dormancy" in subject_lower:
        merchant = "fanduel sportsbook"
    elif "geico.com" in sender_lower and "credit report" in subject_lower:
        merchant = "geico"
    elif (
        parseaddr(sender)[1].lower() == "noreply@wise.com"
        and re.fullmatch(r"transfer set up \(#\d+\)", subject.strip(), re.I)
        and re.search(r"\byour transfer of [\d,]+(?:\.\d+)? [A-Z]{3} is set up\b", plain_text, re.I)
    ):
        merchant = "Wise"
        cleaned = re.sub(r"\s+", " ", plain_text.replace("\u200c", "")).strip()
        start = re.search(r"\byour transfer of\b", cleaned, re.I)
        excerpt = cleaned[start.start() : start.start() + 220] if start else cleaned[:220]
    elif "noreply@wise.com" in sender_lower and "monthly interest" in subject_lower:
        merchant = "wise"
        kind = "documents/income"
    if merchant is None:
        return None
    result = {"merchant": merchant, "document_kind": kind, "title": subject}
    if excerpt:
        result["body_excerpt"] = excerpt
    return result
