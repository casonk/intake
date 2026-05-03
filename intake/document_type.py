"""Document type classification: receipt, letter, card, or unknown.

Runs immediately after OCR so the pipeline can dispatch to the correct
extractor before doing any receipt-specific parsing.
"""

from __future__ import annotations

import re
from typing import Literal

DocumentType = Literal["receipt", "letter", "card", "payment", "unknown"]

# ── receipt signals ───────────────────────────────────────────────────────────

_TOTAL_SIGNAL_RE = re.compile(
    r"\b(total|amount\s+due|amount\s+paid|grand\s+total|balance\s+due"
    r"|subtotal|invoice\s+total|order\s+total|sales\s+tax|freight"
    r"|parts\s+total|labor\s+total|invoice\s+no|invoice\s+number"
    r"|withdrawal|deposit|transaction|atm\s+cash)\b",
    re.IGNORECASE,
)
_PRICE_LINE_RE = re.compile(r"\$\s*\d{1,6}[.,]\d{2}\b")
# Two adjacent bare decimal amounts (e.g. tabular price columns without $)
_TABULAR_PRICE_RE = re.compile(r"\b\d{1,6}\.\d{2}\s+\d{1,6}\.\d{2}\b")

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

# ── payment signals ──────────────────────────────────────────────────────────
# Require two independent signals so "amount paid" on a receipt doesn't trigger.

_PAYMENT_STRONG_RE = re.compile(
    r"\b(payment\s+received|payment\s+confirm|payment\s+process"
    r"|payment\s+applied|payment\s+post|payment\s+accepted"
    r"|minimum\s+payment|statement\s+balance|available\s+credit"
    r"|credit\s+card\s+payment|loan\s+payment|mortgage\s+payment"
    r"|auto\s+loan\s+payment|autopay|auto\s+pay)\b",
    re.IGNORECASE,
)

# ── card signals ──────────────────────────────────────────────────────────────

_CARD_OCCASION_RE = re.compile(
    r"\b(happy\s+birthday|congratulations?|merry\s+christmas|happy\s+holidays?"
    r"|happy\s+anniversary|get\s+well\s+soon|with\s+sympathy"
    r"|thinking\s+of\s+you|bon\s+voyage|happy\s+new\s+year|season.?s\s+greetings"
    r"|best\s+wishes|wishing\s+you)\b",
    re.IGNORECASE,
)


def classify_document_type(raw_text: str, filename: str = "") -> DocumentType:
    """Classify OCR text as receipt, letter, card, payment, or unknown."""
    text = raw_text.strip()
    if not text:
        return "unknown"

    payment_score = _payment_score(text)
    if payment_score >= 2:
        return "payment"

    receipt_score = _receipt_score(text)
    if receipt_score >= 2:
        return "receipt"

    card_score = _card_score(text)
    letter_score = _letter_score(text)

    if card_score >= 2 and receipt_score == 0:
        return "card"
    if letter_score >= 2:
        return "letter"
    if receipt_score >= 1:
        return "receipt"
    if payment_score >= 1:
        return "payment"
    if card_score >= 1:
        return "card"
    if letter_score >= 1:
        return "letter"
    return "unknown"


def _payment_score(text: str) -> int:
    return len(_PAYMENT_STRONG_RE.findall(text))


def _receipt_score(text: str) -> int:
    score = 0
    if _TOTAL_SIGNAL_RE.search(text):
        score += 1
    if len(_PRICE_LINE_RE.findall(text)) >= 2 or _TABULAR_PRICE_RE.search(text):
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
    return score


def _card_score(text: str) -> int:
    occasions = _CARD_OCCASION_RE.findall(text)
    score = len(occasions) + (1 if occasions else 0)
    if len(text) < 250:
        score += 1
    return score
