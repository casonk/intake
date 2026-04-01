"""Parse raw OCR text into a structured Receipt dataclass."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


@dataclass
class Receipt:
    filename: str
    scan_date: date
    merchant: str
    total: float | None
    raw_text: str


def parse_receipt(filename: str, raw_text: str) -> Receipt:
    """Parse filename and raw OCR text into a Receipt.

    - scan_date: extracted from YYYYMMDD prefix of the filename stem
    - merchant: first non-empty trimmed line of the OCR text (up to 60 chars)
    - total: last occurrence of a total-like line (Total / Amount Due / Balance Due)
    """
    scan_date = _parse_date(filename)
    merchant = _parse_merchant(raw_text)
    total = _parse_total(raw_text)
    return Receipt(
        filename=filename,
        scan_date=scan_date,
        merchant=merchant,
        total=total,
        raw_text=raw_text,
    )


_DATE_RE = re.compile(r"(\d{8})")
_TOTAL_RE = re.compile(
    r"(?:total\s*(?:due)?|amount\s+due|balance(?:\s+due)?)\s*[:\$]?\s*\$?\s*(\d{1,6}[.,]\d{2})",
    re.IGNORECASE,
)


def _parse_date(filename: str) -> date:
    stem = Path(filename).stem if "." in filename else filename
    m = _DATE_RE.search(stem)
    if m:
        raw = m.group(1)
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    raise ValueError(f"Cannot parse date from filename: {filename!r}")


def _parse_merchant(raw_text: str) -> str:
    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:60]
    return "Unknown"


def _parse_total(raw_text: str) -> float | None:
    matches = _TOTAL_RE.findall(raw_text)
    if not matches:
        return None
    raw = matches[-1].replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


# Lazy import kept local so ocr.py import is not required in unit tests
from pathlib import Path  # noqa: E402 (already imported implicitly via _parse_date)
