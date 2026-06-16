"""Parse reference documents such as manuals, specs, and warranties."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class Document:
    filename: str
    scan_date: date
    document_date: date | None
    document_kind: str
    title: str | None
    issuer: str | None
    subject: str | None
    model_number: str | None
    serial_number: str | None
    body_excerpt: str
    raw_text: str


def parse_document(filename: str, raw_text: str, pdf_path: Path | None = None) -> Document:
    """Parse OCR text into a generic reference document."""
    from intake.parse import _parse_receipt_datetime, _parse_scan_date

    scan_date = _parse_scan_date(filename, pdf_path)
    document_date, _ = _parse_receipt_datetime(raw_text)
    document_kind = classify_document(raw_text)

    return Document(
        filename=filename,
        scan_date=scan_date,
        document_date=document_date,
        document_kind=document_kind,
        title=_parse_title(raw_text),
        issuer=_parse_issuer(raw_text),
        subject=_parse_subject(raw_text),
        model_number=_parse_model_number(raw_text),
        serial_number=_parse_serial_number(raw_text),
        body_excerpt=_parse_body_excerpt(raw_text),
        raw_text=raw_text,
    )


_MODEL_RE = re.compile(
    r"\b(?:model\s+number|model\s+no\.?|part\s+no\.?|model|pn|p/n|sku)\s*[:#]?\s*([A-Z0-9][A-Z0-9._/-]{2,})\b",
    re.IGNORECASE,
)
_SERIAL_RE = re.compile(
    r"\b(?:serial\s+number|serial\s+no\.?|serial|s/n|sn)\s*[:#]?\s*([A-Z0-9][A-Z0-9._/-]{3,})\b",
    re.IGNORECASE,
)
_TITLE_LABEL_RE = re.compile(r"^(?:title|document|product)[:\s]+(.+)$", re.IGNORECASE)
_HEADER_NOISE_RE = re.compile(
    r"^(?:page\s+\d+|table\s+of\s+contents|contents|important\s+safety|warning|caution)$",
    re.IGNORECASE,
)

_MANUAL_TERMS = (
    "user manual",
    "user guide",
    "owner's manual",
    "owners manual",
    "instruction manual",
    "installation guide",
    "operating instructions",
)
_SPEC_TERMS = (
    "datasheet",
    "data sheet",
    "technical specifications",
    "specification sheet",
    "hardware specifications",
    "product specification",
)
_WARRANTY_TERMS = (
    "limited warranty",
    "warranty",
    "service contract",
    "protection plan",
    "extended service plan",
    "terms and conditions",
)


def classify_document(raw_text: str) -> str:
    """Return a document category path for manuals/specs/warranties."""
    lowered = raw_text.lower()
    if any(term in lowered for term in _WARRANTY_TERMS):
        return "documents/warranty"
    if any(term in lowered for term in _SPEC_TERMS):
        return "documents/specs"
    if any(term in lowered for term in _MANUAL_TERMS):
        return "documents/manuals"
    return "documents/reference"


def _parse_title(raw_text: str) -> str | None:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for line in lines[:8]:
        match = _TITLE_LABEL_RE.match(line)
        if match:
            return match.group(1).strip()[:160] or None
    for line in lines[:8]:
        if _HEADER_NOISE_RE.match(line):
            continue
        if len(line) < 4:
            continue
        return line[:160]
    return None


def _parse_issuer(raw_text: str) -> str | None:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for line in lines[:6]:
        lowered = line.lower()
        if any(term in lowered for term in (*_MANUAL_TERMS, *_SPEC_TERMS, *_WARRANTY_TERMS)):
            continue
        if _HEADER_NOISE_RE.match(line):
            continue
        if 2 <= len(line.split()) <= 6:
            return line[:120]
    return None


def _parse_subject(raw_text: str) -> str | None:
    model = _parse_model_number(raw_text)
    title = _parse_title(raw_text)
    if model and title:
        return f"{title} ({model})"[:180]
    return title


def _parse_model_number(raw_text: str) -> str | None:
    match = _MODEL_RE.search(raw_text)
    return match.group(1).strip()[:80] if match else None


def _parse_serial_number(raw_text: str) -> str | None:
    match = _SERIAL_RE.search(raw_text)
    return match.group(1).strip()[:80] if match else None


def _parse_body_excerpt(raw_text: str) -> str:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    return " ".join(lines)[:500]
