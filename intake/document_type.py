"""Document type classification: receipt, letter, card, or unknown.

Runs immediately after OCR so the pipeline can dispatch to the correct
extractor before doing any receipt-specific parsing.
"""

from __future__ import annotations

import re
from typing import Literal

DocumentType = Literal["receipt", "letter", "card", "unknown"]

# ── receipt signals ───────────────────────────────────────────────────────────

_TOTAL_SIGNAL_RE = re.compile(
    r"\b(total|amount\s+due|amount\s+paid|grand\s+total|balance\s+due"
    r"|subtotal|invoice\s+total|order\s+total)\b",
    re.IGNORECASE,
)
_PRICE_LINE_RE = re.compile(r"\$\s*\d{1,6}[.,]\d{2}\b")

# ── letter signals ────────────────────────────────────────────────────────────

_SALUTATION_RE = re.compile(
    r"^(dear\b.{0,60}[,:]|to\s+whom\s+it\s+may\s+concern)",
    re.IGNORECASE | re.MULTILINE,
)
_CLOSING_RE = re.compile(
    r"\b(sincerely|regards|best\s+regards|yours\s+truly|kind\s+regards"
    r"|respectfully|warm\s+regards|with\s+appreciation|cordially)\b",
    re.IGNORECASE,
)
_ADDRESS_BLOCK_RE = re.compile(
    r"\d{1,5}\s+[A-Za-z][\w\s]{3,40}(?:st|ave|rd|blvd|dr|ln|ct|way|pl|cir)\.?\s*[\n,]",
    re.IGNORECASE,
)

# ── card signals ──────────────────────────────────────────────────────────────

_CARD_OCCASION_RE = re.compile(
    r"\b(happy\s+birthday|congratulations?|merry\s+christmas|happy\s+holidays?"
    r"|happy\s+anniversary|get\s+well\s+soon|thank\s+you|with\s+sympathy"
    r"|thinking\s+of\s+you|bon\s+voyage|happy\s+new\s+year|season.?s\s+greetings"
    r"|best\s+wishes|wishing\s+you)\b",
    re.IGNORECASE,
)


def classify_document_type(raw_text: str, filename: str = "") -> DocumentType:
    """Classify OCR text as receipt, letter, card, or unknown.

    Uses heuristic scoring: receipt signals take priority (a receipt with a
    greeting phrase is still a receipt), then card (short + occasion keywords),
    then letter (salutation + closing).
    """
    text = raw_text.strip()
    if not text:
        return "unknown"

    receipt_score = _receipt_score(text)
    if receipt_score >= 2:
        return "receipt"

    card_score = _card_score(text)
    letter_score = _letter_score(text)

    if card_score >= 2:
        return "card"
    if letter_score >= 2:
        return "letter"
    if receipt_score >= 1:
        return "receipt"
    if card_score >= 1:
        return "card"
    if letter_score >= 1:
        return "letter"
    return "unknown"


def _receipt_score(text: str) -> int:
    score = 0
    if _TOTAL_SIGNAL_RE.search(text):
        score += 1
    if len(_PRICE_LINE_RE.findall(text)) >= 2:
        score += 1
    return score


def _letter_score(text: str) -> int:
    score = 0
    if _SALUTATION_RE.search(text):
        score += 2
    if _CLOSING_RE.search(text):
        score += 1
    if _ADDRESS_BLOCK_RE.search(text):
        score += 1
    if len(text) > 400:
        score += 1
    return score


def _card_score(text: str) -> int:
    occasions = _CARD_OCCASION_RE.findall(text)
    score = len(occasions) + (1 if occasions else 0)
    if len(text) < 250:
        score += 1
    return score
