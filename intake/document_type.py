"""Document type classification: receipt, letter, card, document, or unknown.

Runs immediately after OCR so the pipeline can dispatch to the correct
extractor before doing any receipt-specific parsing.
"""

from __future__ import annotations

import re
from typing import Literal

DocumentType = Literal["receipt", "letter", "card", "payment", "document", "unknown"]

# ── identity / legal document signals ─────────────────────────────────────────

_IDENTITY_DOCUMENT_RE = re.compile(
    r"(?:\b(?:residence\s+card|certificate\s+of\s+residence|resident\s+record"
    r"|certified\s+copy\s+of\s+resident\s+record|my\s+number\s+card"
    r"|individual\s+number\s+card|personal\s+number\s+card"
    r"|ministry\s+of\s+justice|period\s+of\s+stay|nationality\s*/\s*region"
    r")\b|在留カード|住民票|住民基本台帳|個人番号|在留期間|横浜市長)",
    re.IGNORECASE,
)

# ── reference document signals ────────────────────────────────────────────────

_REFERENCE_DOCUMENT_RE = re.compile(
    r"\b(?:user\s+manual|user\s+guide|owner'?s\s+manual|instruction\s+manual"
    r"|installation\s+guide|installation\s+manual|setup\s+guide|quick\s+start\s+guide"
    r"|operating\s+instructions|datasheet|data\s+sheet|product\s+brief"
    r"|technical\s+specifications|specification\s+sheet|hardware\s+specifications"
    r"|product\s+specification|limited\s+warranty|warranty\s+certificate"
    r"|service\s+contract|protection\s+plan|extended\s+service\s+plan)\b",
    re.IGNORECASE,
)

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

    if is_identity_document_text(text):
        return "letter"

    if is_reference_document_text(text):
        return "document"

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


def is_identity_document_text(text: str) -> bool:
    """Return True when text points to an identity/residence legal document."""
    return bool(_IDENTITY_DOCUMENT_RE.search(text or ""))


def is_reference_document_text(text: str) -> bool:
    """Return True when text points to a manual/spec/warranty-style document."""
    return bool(_REFERENCE_DOCUMENT_RE.search(text or ""))


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
