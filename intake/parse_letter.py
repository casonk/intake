"""Parse raw OCR text into a structured Letter dataclass."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class Letter:
    filename: str
    scan_date: date
    letter_date: date | None
    sender: str | None
    recipient: str | None
    subject: str | None
    salutation: str | None
    body_excerpt: str
    raw_text: str


def parse_letter(filename: str, raw_text: str, pdf_path: Path | None = None) -> Letter:
    """Parse filename and raw OCR text into a Letter."""
    from intake.parse import _parse_receipt_datetime, _parse_scan_date

    scan_date = _parse_scan_date(filename, pdf_path)
    letter_date, _ = _parse_receipt_datetime(raw_text)

    return Letter(
        filename=filename,
        scan_date=scan_date,
        letter_date=letter_date,
        sender=_parse_sender(raw_text),
        recipient=_parse_recipient(raw_text),
        subject=_parse_subject(raw_text),
        salutation=_parse_salutation(raw_text),
        body_excerpt=_parse_body_excerpt(raw_text),
        raw_text=raw_text,
    )


_FROM_RE = re.compile(r"^(?:from|sender)[:\s]+(.+)$", re.IGNORECASE | re.MULTILINE)
_TO_RE = re.compile(r"^(?:to|recipient)[:\s]+(.+)$", re.IGNORECASE | re.MULTILINE)
_SUBJECT_RE = re.compile(r"^(?:re|subject|subj)[:\s]+(.+)$", re.IGNORECASE | re.MULTILINE)
_SALUTATION_RE = re.compile(
    r"^(dear\b.{0,60}[,:]|to\s+whom\s+it\s+may\s+concern[,:]?)",
    re.IGNORECASE | re.MULTILINE,
)
_HEADER_RE = re.compile(
    r"^(?:from|to|date|subject|re|dear\b|sincerely|regards|yours|best\b)",
    re.IGNORECASE,
)


def _parse_sender(raw_text: str) -> str | None:
    m = _FROM_RE.search(raw_text)
    if m:
        return m.group(1).strip()[:80] or None
    return None


def _parse_recipient(raw_text: str) -> str | None:
    m = _TO_RE.search(raw_text)
    if m:
        return m.group(1).strip()[:80] or None
    return None


def _parse_subject(raw_text: str) -> str | None:
    m = _SUBJECT_RE.search(raw_text)
    if m:
        return m.group(1).strip()[:120] or None
    return None


def _parse_salutation(raw_text: str) -> str | None:
    m = _SALUTATION_RE.search(raw_text)
    if m:
        return m.group(1).strip()[:80] or None
    return None


def _parse_body_excerpt(raw_text: str) -> str:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    body_lines = [line for line in lines if not _HEADER_RE.match(line)]
    return " ".join(body_lines)[:500]


# ── category classification ───────────────────────────────────────────────────

_LEGAL_TERMS = frozenset(
    {
        "hereby",
        "pursuant",
        "defendant",
        "plaintiff",
        "attorney",
        "counsel",
        "court",
        "subpoena",
        "lawsuit",
        "litigation",
        "deposition",
        "affidavit",
    }
)
_MEDICAL_TERMS = frozenset(
    {
        "diagnosis",
        "prescription",
        "patient",
        "physician",
        "hospital",
        "clinic",
        "treatment",
        "medication",
        "referral",
        "copay",
        "healthcare",
    }
)
_FINANCIAL_TERMS = frozenset(
    {
        "account",
        "statement",
        "balance",
        "mortgage",
        "loan",
        "tax",
        "refund",
        "credit",
        "debit",
        "investment",
        "portfolio",
        "dividend",
        "escrow",
    }
)


def classify_letter(letter: Letter) -> str:
    """Return a category path for a Letter based on content."""
    words = set(re.findall(r"\b\w+\b", letter.raw_text.lower()))
    if words & _LEGAL_TERMS:
        return "letters/legal"
    if words & _MEDICAL_TERMS:
        return "letters/medical"
    if words & _FINANCIAL_TERMS:
        return "letters/financial"
    return "letters/personal"
