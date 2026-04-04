"""Parse raw OCR text into a structured Receipt dataclass."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path


@dataclass
class Receipt:
    filename: str
    scan_date: date
    receipt_date: date | None  # date printed on the receipt (from OCR)
    receipt_time: time | None  # time printed on the receipt (from OCR), if found
    merchant: str
    total: float | None
    raw_text: str
    purchaser: str | None = None  # cardholder name if found on receipt
    card_last4: str | None = None  # last 4 digits of payment card if found

    def suggested_filename(self) -> str:
        """Return a filename derived from the receipt date/time, falling back to scan_date."""
        d = self.receipt_date or self.scan_date
        if self.receipt_time:
            return f"{d.strftime('%Y%m%d')}_{self.receipt_time.strftime('%H%M')}.pdf"
        return f"{d.strftime('%Y%m%d')}.pdf"


def parse_receipt(filename: str, raw_text: str, pdf_path: Path | None = None) -> Receipt:
    """Parse filename and raw OCR text into a Receipt."""
    scan_date = _parse_scan_date(filename, pdf_path)
    merchant = _parse_merchant(raw_text)
    total = _parse_total(raw_text)
    receipt_date, receipt_time = _parse_receipt_datetime(raw_text)
    purchaser = _parse_purchaser(raw_text)
    card_last4 = _parse_card_last4(raw_text)
    return Receipt(
        filename=filename,
        scan_date=scan_date,
        receipt_date=receipt_date,
        receipt_time=receipt_time,
        merchant=merchant,
        total=total,
        raw_text=raw_text,
        purchaser=purchaser,
        card_last4=card_last4,
    )


# ── filename scan-date ────────────────────────────────────────────────────────

# YYYYMMDD (8 digits), YYYYMM (6 digits) — tried in order, longest first
_DATE8_RE = re.compile(r"(\d{8})")
_DATE6_RE = re.compile(r"(\d{6})")


def _parse_scan_date(filename: str, pdf_path: Path | None = None) -> date:
    """Extract a scan date from the filename, falling back to file mtime.

    Tries (in order):
    1. YYYYMMDD (8-digit run) — validated, skips runs that produce invalid dates
    2. YYYYMM   (6-digit run) — day defaults to 1
    3. File mtime via *pdf_path*
    4. Today's date as last resort
    """
    stem = Path(filename).stem

    for m in _DATE8_RE.finditer(stem):
        raw = m.group(1)
        try:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError:
            continue

    for m in _DATE6_RE.finditer(stem):
        raw = m.group(1)
        try:
            return date(int(raw[:4]), int(raw[4:6]), 1)
        except ValueError:
            continue

    if pdf_path and pdf_path.exists():
        return date.fromtimestamp(pdf_path.stat().st_mtime)

    return date.today()


# ── receipt date/time from OCR text ──────────────────────────────────────────

# Match M/D/YY, MM/DD/YY, M/D/YYYY, MM/DD/YYYY with / or -
_MDY_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b")
# Match YYYY-MM-DD or YYYY/MM/DD
_ISO_RE = re.compile(r"\b(20\d{2})[/\-](\d{2})[/\-](\d{2})\b")
# Match time: H:MM AM/PM or HH:MM (24-hr)
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|am|pm)?\b")

_CURRENT_YEAR = datetime.now().year


def _normalise_year(y: int) -> int:
    """Expand 2-digit year: 00-29 -> 2000-2029, 30-99 -> 1930-1999."""
    if y < 100:
        return 2000 + y if y < 30 else 1900 + y
    return y


def _candidate_date(m_str: str, d_str: str, y_str: str) -> date | None:
    try:
        month, day, year = int(m_str), int(d_str), _normalise_year(int(y_str))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        if not (1990 <= year <= _CURRENT_YEAR + 1):
            return None
        return date(year, month, day)
    except ValueError:
        return None


def _parse_receipt_datetime(raw_text: str) -> tuple[date | None, time | None]:
    """Extract the earliest plausible receipt date and optional time from OCR text."""
    candidates: list[date] = []

    for m in _ISO_RE.finditer(raw_text):
        d = _candidate_date(m.group(2), m.group(3), m.group(1))
        if d:
            candidates.append(d)

    for m in _MDY_RE.finditer(raw_text):
        d = _candidate_date(m.group(1), m.group(2), m.group(3))
        if d:
            candidates.append(d)

    receipt_date = min(candidates) if candidates else None

    receipt_time: time | None = None
    for m in _TIME_RE.finditer(raw_text):
        hour, minute = int(m.group(1)), int(m.group(2))
        meridiem = (m.group(4) or "").upper()
        if meridiem == "PM" and hour != 12:
            hour += 12
        elif meridiem == "AM" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            receipt_time = time(hour, minute)
            break  # take the first plausible time

    return receipt_date, receipt_time


# ── merchant ──────────────────────────────────────────────────────────────────


def _parse_merchant(raw_text: str) -> str:
    from intake.merchant import clean_merchant_text  # noqa: PLC0415

    for line in raw_text.splitlines():
        stripped = clean_merchant_text(line)
        if stripped:
            return stripped[:60].lower()
    return "unknown"


# ── total ─────────────────────────────────────────────────────────────────────

_LINE_AMOUNT_RE = re.compile(
    r"^\s*(?P<label>[^0-9$][^\n$]{0,120}?)\s*[:\-]?\s*\$?\s*(?P<amount>-?\d{1,6}(?:[.,]\d{1,2})?)\s*(?:[A-Z]{3})?\s*$",
    re.IGNORECASE,
)

_TOTAL_LABEL_TERMS = ("grand total", "order total", "amount due", "balance due", "balance", "total")
_TOTAL_EXCLUDE_TERMS = (
    "subtotal",
    "sub total",
    "loyalty",
    "points",
    "reward",
    "rewards",
    "item",
    "items",
    "tax",
    "discount",
    "savings",
    "coupon",
    "promo",
    "promotion",
    "change",
    "customer",
    "payment",
    "cash",
)
_SUBTOTAL_TERMS = ("subtotal", "sub total")
_TAX_TOTAL_TERMS = ("total tax", "tax total")
_DISCOUNT_TOTAL_TERMS = ("total discount", "discount total", "total savings", "savings total")
_ADDITION_TOTAL_TERMS = ("tip", "gratuity", "service charge", "delivery", "fee", "fees", "surcharge")
_DISCOUNT_TERMS = ("discount", "savings", "coupon", "promo", "promotion", "rebate")


def _parse_amount(raw: str) -> float | None:
    cleaned = raw.strip()
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _iter_labeled_amounts(raw_text: str) -> list[tuple[str, float]]:
    entries: list[tuple[str, float]] = []
    for line in raw_text.splitlines():
        match = _LINE_AMOUNT_RE.match(line)
        if not match:
            continue
        amount = _parse_amount(match.group("amount"))
        if amount is None:
            continue
        label = " ".join(match.group("label").strip().lower().split())
        if label:
            entries.append((label, amount))
    return entries


def _last_amount_with_terms(
    entries: list[tuple[str, float]],
    include_terms: tuple[str, ...],
    *,
    exclude_terms: tuple[str, ...] = (),
) -> float | None:
    match_value: float | None = None
    for label, amount in entries:
        if any(term in label for term in include_terms) and not any(
            term in label for term in exclude_terms
        ):
            match_value = amount
    return match_value


def _sum_amounts_with_terms(
    entries: list[tuple[str, float]],
    include_terms: tuple[str, ...],
    *,
    exclude_terms: tuple[str, ...] = (),
) -> float | None:
    matches = [
        amount
        for label, amount in entries
        if any(term in label for term in include_terms)
        and not any(term in label for term in exclude_terms)
    ]
    if not matches:
        return None
    return round(sum(matches), 2)


def _parse_total(raw_text: str) -> float | None:
    entries = _iter_labeled_amounts(raw_text)
    if not entries:
        return None

    explicit_total = _last_amount_with_terms(
        entries,
        _TOTAL_LABEL_TERMS,
        exclude_terms=_TOTAL_EXCLUDE_TERMS,
    )
    if explicit_total is not None and explicit_total > 0:
        return explicit_total

    subtotal = _last_amount_with_terms(entries, _SUBTOTAL_TERMS)
    if subtotal is None or subtotal <= 0:
        return None

    tax_total = _last_amount_with_terms(entries, _TAX_TOTAL_TERMS)
    if tax_total is None:
        tax_total = _sum_amounts_with_terms(
            entries,
            ("tax",),
            exclude_terms=("total tax", "tax total"),
        )

    discount_total = _last_amount_with_terms(entries, _DISCOUNT_TOTAL_TERMS)
    if discount_total is None:
        discount_total = _sum_amounts_with_terms(
            entries,
            _DISCOUNT_TERMS,
            exclude_terms=_DISCOUNT_TOTAL_TERMS,
        )

    additions = _sum_amounts_with_terms(entries, _ADDITION_TOTAL_TERMS) or 0.0
    tax_total = tax_total or 0.0
    discount_total = abs(discount_total or 0.0)

    if not any((tax_total, discount_total, additions)):
        return None

    return round(subtotal + tax_total + additions - discount_total, 2)


# ── purchaser / card ──────────────────────────────────────────────────────────

# Masked card patterns: "VISA xxxx1234", "MC **** 5678", "xxxx-xxxx-xxxx-9012"
_CARD_BRAND_RE = re.compile(
    r"(?:visa|v\.?i\.?s\.?a|mastercard|mc|amex|american\s*express|discover)"
    r"[^0-9]{0,20}(?:[xX*]{2,}[-\s]?)*(\d{4})",
    re.IGNORECASE,
)
_MASKED_RE = re.compile(r"[xX*]{4}[-\s]?[xX*]{0,4}[-\s]?[xX*]{0,4}[-\s]?(\d{4})")


def _parse_card_last4(raw_text: str) -> str | None:
    """Extract the last 4 digits of the payment card from OCR text."""
    m = _CARD_BRAND_RE.search(raw_text)
    if m:
        return m.group(1)
    m = _MASKED_RE.search(raw_text)
    if m:
        return m.group(1)
    return None


# Cardholder name often follows "cardholder:", "member:", "approved for:", etc.
_PURCHASER_RE = re.compile(
    r"(?:card\s*holder|cardholder|member(?:\s*name)?|customer\s*name|customer(?=\s*:)|approved\s*for|name(?=\s*:))"
    r"[:\s]+([A-Z][A-Za-z\s'\-]{2,40}?)(?:\s{2,}|\n|$)",
    re.IGNORECASE,
)


def _parse_purchaser(raw_text: str) -> str | None:
    """Extract the cardholder / customer name from OCR text if present."""
    from intake.ai import _PURCHASER_BLACKLIST  # noqa: PLC0415

    m = _PURCHASER_RE.search(raw_text)
    if m:
        name = m.group(1).strip().lower()
        if name and not name.isdigit() and name not in _PURCHASER_BLACKLIST:
            return name[:60]
    return None
