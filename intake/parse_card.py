"""Parse raw OCR text into a structured Card dataclass."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class Card:
    filename: str
    scan_date: date
    card_date: date | None
    occasion: str | None
    sender: str | None
    recipient: str | None
    message_excerpt: str
    raw_text: str


_OCCASION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bhappy\s+birthday\b", re.IGNORECASE), "birthday"),
    (re.compile(r"\bcongratulations?\b", re.IGNORECASE), "congratulations"),
    (re.compile(r"\b(merry\s+christmas|happy\s+holidays?|season.?s\s+greetings|happy\s+new\s+year)\b", re.IGNORECASE), "holiday"),
    (re.compile(r"\bhappy\s+anniversary\b", re.IGNORECASE), "anniversary"),
    (re.compile(r"\bget\s+well\s+soon\b", re.IGNORECASE), "get-well"),
    (re.compile(r"\b(with\s+sympathy|thinking\s+of\s+you|our\s+deepest\s+sympathy)\b", re.IGNORECASE), "sympathy"),
    (re.compile(r"\bthank\s+you\b", re.IGNORECASE), "thank-you"),
    (re.compile(r"\bbon\s+voyage\b", re.IGNORECASE), "farewell"),
]

_TO_RE = re.compile(r"^(?:to|dear)\b[,:\s]+(.+)$", re.IGNORECASE | re.MULTILINE)
_FROM_RE = re.compile(
    r"^(?:from|love(?:\s+from)?|signed?|xo+)[,:\s]+(.{1,60})$",
    re.IGNORECASE | re.MULTILINE,
)
# Year pattern used to exclude lines that look like dates from being sender names
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def parse_card(filename: str, raw_text: str, pdf_path: Path | None = None) -> Card:
    """Parse filename and raw OCR text into a Card."""
    from intake.parse import _parse_receipt_datetime, _parse_scan_date

    scan_date = _parse_scan_date(filename, pdf_path)
    card_date, _ = _parse_receipt_datetime(raw_text)

    return Card(
        filename=filename,
        scan_date=scan_date,
        card_date=card_date,
        occasion=_parse_occasion(raw_text),
        sender=_parse_card_sender(raw_text),
        recipient=_parse_card_recipient(raw_text),
        message_excerpt=raw_text.strip()[:300],
        raw_text=raw_text,
    )


def _parse_occasion(raw_text: str) -> str | None:
    for pattern, label in _OCCASION_PATTERNS:
        if pattern.search(raw_text):
            return label
    return None


def _parse_card_sender(raw_text: str) -> str | None:
    m = _FROM_RE.search(raw_text)
    if m:
        return m.group(1).strip()[:60] or None
    # Heuristic: last short non-date line is likely the signature
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for line in reversed(lines):
        if len(line) < 40 and not _YEAR_RE.search(line) and re.search(r"[A-Za-z]", line):
            return line[:60]
    return None


def _parse_card_recipient(raw_text: str) -> str | None:
    m = _TO_RE.search(raw_text)
    if m:
        return m.group(1).strip()[:60] or None
    return None


# ── category classification ───────────────────────────────────────────────────


def classify_card(card: Card) -> str:
    """Return a category path for a Card based on its occasion."""
    return f"cards/{card.occasion or 'general'}"
