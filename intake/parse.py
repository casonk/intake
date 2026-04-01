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

    def suggested_filename(self) -> str:
        """Return a filename derived from the receipt date/time, falling back to scan_date."""
        d = self.receipt_date or self.scan_date
        if self.receipt_time:
            return f"{d.strftime('%Y%m%d')}_{self.receipt_time.strftime('%H%M')}.pdf"
        return f"{d.strftime('%Y%m%d')}.pdf"


def parse_receipt(filename: str, raw_text: str) -> Receipt:
    """Parse filename and raw OCR text into a Receipt."""
    scan_date = _parse_scan_date(filename)
    merchant = _parse_merchant(raw_text)
    total = _parse_total(raw_text)
    receipt_date, receipt_time = _parse_receipt_datetime(raw_text)
    return Receipt(
        filename=filename,
        scan_date=scan_date,
        receipt_date=receipt_date,
        receipt_time=receipt_time,
        merchant=merchant,
        total=total,
        raw_text=raw_text,
    )


# ── filename scan-date ────────────────────────────────────────────────────────

_SCANDATE_RE = re.compile(r"(\d{8})")


def _parse_scan_date(filename: str) -> date:
    stem = Path(filename).stem if "." in filename else filename
    m = _SCANDATE_RE.search(stem)
    if m:
        raw = m.group(1)
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    raise ValueError(f"Cannot parse date from filename: {filename!r}")


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
    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:60]
    return "Unknown"


# ── total ─────────────────────────────────────────────────────────────────────

_TOTAL_RE = re.compile(
    r"(?:total\s*(?:due)?|amount\s+due|balance(?:\s+due)?)\s*[:\$]?\s*\$?\s*(\d{1,6}[.,]\d{2})",
    re.IGNORECASE,
)


def _parse_total(raw_text: str) -> float | None:
    matches = _TOTAL_RE.findall(raw_text)
    if not matches:
        return None
    raw = matches[-1].replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None
