"""Parse debt-payment documents: credit card, loan, mortgage, auto, utility payments."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class Payment:
    filename: str
    scan_date: date
    payment_date: date | None
    amount: float | None
    account_name: str | None
    account_last4: str | None
    payment_type: str | None  # credit-card | loan | mortgage | auto | utility | other
    confirmation_number: str | None
    payer: str | None
    raw_text: str


_AMOUNT_RE = re.compile(r"\$\s*(\d{1,6}(?:,\d{3})*\.\d{2})\b")
_ACCOUNT_LAST4_RE = re.compile(r"\b(?:ending|ending\s+in|x{2,}|card)\s*(\d{4})\b", re.IGNORECASE)
_CONFIRMATION_RE = re.compile(
    r"\b(?:confirmation|reference|conf|ref|auth(?:orization)?)\s*(?:#|no\.?|number|:)?\s*:?\s*([A-Z0-9\-]{5,24})\b",
    re.IGNORECASE,
)

_MORTGAGE_TERMS = frozenset(["mortgage", "home loan", "escrow", "servicer"])
_LOAN_TERMS = frozenset(["auto loan", "personal loan", "student loan", "installment loan"])
_AUTO_TERMS = frozenset(["vehicle payment", "car payment", "auto payment"])
_UTILITY_TERMS = frozenset(
    ["electric", "gas bill", "water bill", "utility", "internet bill", "phone bill"]
)
_CREDIT_CARD_TERMS = frozenset(
    [
        "credit card",
        "visa",
        "mastercard",
        "amex",
        "american express",
        "discover",
        "sapphire",
        "freedom",
        "cash back",
        "rewards card",
        "statement balance",
        "minimum payment",
        "available credit",
    ]
)


def _infer_payment_type(text: str) -> str:
    t = text.lower()
    for term in _MORTGAGE_TERMS:
        if term in t:
            return "mortgage"
    for term in _AUTO_TERMS:
        if term in t:
            return "auto"
    for term in _LOAN_TERMS:
        if term in t:
            return "loan"
    for term in _UTILITY_TERMS:
        if term in t:
            return "utility"
    for term in _CREDIT_CARD_TERMS:
        if term in t:
            return "credit-card"
    return "other"


def parse_payment(filename: str, raw_text: str, *, pdf_path: Path | None = None) -> Payment:
    from intake.parse import _parse_receipt_datetime, _parse_scan_date

    scan_date = _parse_scan_date(filename, pdf_path=pdf_path)

    payment_date: date | None = None
    parsed_date, _ = _parse_receipt_datetime(raw_text)
    if parsed_date:
        payment_date = parsed_date

    # Use the largest dollar amount as the payment amount.
    amounts = []
    for raw in _AMOUNT_RE.findall(raw_text):
        with contextlib.suppress(ValueError):
            amounts.append(float(raw.replace(",", "")))
    amount = max(amounts) if amounts else None

    m = _ACCOUNT_LAST4_RE.search(raw_text)
    account_last4 = m.group(1) if m else None

    m = _CONFIRMATION_RE.search(raw_text)
    confirmation_number = m.group(1) if m else None

    return Payment(
        filename=filename,
        scan_date=scan_date,
        payment_date=payment_date,
        amount=amount,
        account_name=None,
        account_last4=account_last4,
        payment_type=_infer_payment_type(raw_text),
        confirmation_number=confirmation_number,
        payer=None,
        raw_text=raw_text,
    )


def classify_payment(payment: Payment) -> str:
    """Return the category path for a payment (e.g. ``payments/credit-card``)."""
    return f"payments/{payment.payment_type or 'other'}"
