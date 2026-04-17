"""Structured merchant certification extraction and normalization helpers."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ReceiptCertification:
    """One structured certification or membership claim found on a receipt."""

    issuer: str
    label: str
    source_text: str | None = None


_AAA_ISSUER_RE = re.compile(
    r"\b(?:aaa|american\s+automobile\s+association)\b",
    re.IGNORECASE,
)
_CERTIFIED_BY_AAA_RE = re.compile(r"\bcertified\s+by\s+aaa\b", re.IGNORECASE)
_AAA_APPROVED_RE = re.compile(r"\baaa\s+approved\s+auto\s+repair\b", re.IGNORECASE)
_APPROVED_AUTO_REPAIR_RE = re.compile(r"\bapproved\s+auto\s+repair\b", re.IGNORECASE)


def _normalize_issuer(value: object | None) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    if _AAA_ISSUER_RE.search(text):
        return "AAA"
    return text[:40]


def _normalize_label(value: object | None, *, issuer: str | None = None) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" -:;,.")
    if not text:
        return None
    text_lower = text.casefold()
    if issuer == "AAA":
        if _CERTIFIED_BY_AAA_RE.search(text) or "aaa certified" in text_lower:
            return "AAA certified"
        if _AAA_APPROVED_RE.search(text) or (
            _APPROVED_AUTO_REPAIR_RE.search(text) and "aaa" in text_lower
        ):
            return "AAA approved auto repair"
    return text[:80]


def normalize_certification(
    *,
    issuer: object | None,
    label: object | None,
    source_text: object | None = None,
) -> ReceiptCertification | None:
    """Return a normalized certification record or None when unusable."""
    normalized_issuer = _normalize_issuer(issuer or label)
    normalized_label = _normalize_label(label or issuer, issuer=normalized_issuer)
    if normalized_issuer is None or normalized_label is None:
        return None

    source = re.sub(r"\s+", " ", str(source_text or "")).strip()
    return ReceiptCertification(
        issuer=normalized_issuer,
        label=normalized_label,
        source_text=source[:200] or None,
    )


def receipt_certifications_to_dicts(
    certifications: list[ReceiptCertification] | list[dict] | None,
) -> list[dict]:
    """Render certifications as JSON-friendly dictionaries."""
    rendered: list[dict] = []
    seen: set[tuple[str, str, str | None]] = set()
    for cert in certifications or []:
        normalized = (
            cert
            if isinstance(cert, ReceiptCertification)
            else normalize_certification(
                issuer=cert.get("issuer") if isinstance(cert, dict) else None,
                label=cert.get("label") if isinstance(cert, dict) else None,
                source_text=cert.get("source_text") if isinstance(cert, dict) else None,
            )
        )
        if normalized is None:
            continue
        key = (normalized.issuer, normalized.label, normalized.source_text)
        if key in seen:
            continue
        seen.add(key)
        rendered.append(asdict(normalized))
    return rendered


def receipt_certifications_from_dicts(value: object) -> list[ReceiptCertification]:
    """Return normalized certifications from a decoded JSON payload."""
    if not isinstance(value, list):
        return []
    certifications: list[dict] = []
    for item in value:
        if isinstance(item, ReceiptCertification):
            certifications.append(asdict(item))
            continue
        if not isinstance(item, dict):
            continue
        normalized = normalize_certification(
            issuer=item.get("issuer"),
            label=item.get("label"),
            source_text=item.get("source_text"),
        )
        if normalized is not None:
            certifications.append(asdict(normalized))
    return [
        ReceiptCertification(**item) for item in receipt_certifications_to_dicts(certifications)
    ]


def merge_certifications(
    existing: list[ReceiptCertification] | list[dict] | None,
    incoming: list[ReceiptCertification] | list[dict] | None,
) -> list[ReceiptCertification]:
    """Merge two certification lists, preferring richer source_text when duplicated."""
    merged: dict[tuple[str, str], ReceiptCertification] = {}
    for collection in (existing, incoming):
        for cert in receipt_certifications_from_dicts(collection):
            key = (cert.issuer, cert.label)
            current = merged.get(key)
            if current is None or (not current.source_text and cert.source_text):
                merged[key] = cert
    return list(merged.values())


def extract_certifications(raw_text: str) -> list[ReceiptCertification]:
    """Extract structured certifications from OCR text."""
    certifications: list[ReceiptCertification] = []
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for line in lines:
        lowered = line.casefold()
        cert: ReceiptCertification | None = None
        if _CERTIFIED_BY_AAA_RE.search(line):
            cert = normalize_certification(
                issuer="AAA",
                label="AAA certified",
                source_text=line,
            )
        elif _AAA_APPROVED_RE.search(line) or (
            "aaa" in lowered and _APPROVED_AUTO_REPAIR_RE.search(line)
        ):
            cert = normalize_certification(
                issuer="AAA",
                label="AAA approved auto repair",
                source_text=line,
            )
        if cert is not None:
            certifications.append(cert)
    return receipt_certifications_from_dicts(receipt_certifications_to_dicts(certifications))


def parse_manual_certification(spec: str) -> ReceiptCertification | None:
    """Parse a manual CLI certification spec.

    Supported shapes:
    - ``AAA=AAA certified``
    - ``AAA:AAA approved auto repair``
    - ``AAA certified`` (issuer inferred)
    """
    text = spec.strip()
    if not text:
        return None
    if "=" in text:
        issuer, label = text.split("=", 1)
    elif ":" in text:
        issuer, label = text.split(":", 1)
    else:
        issuer, label = text, text
    return normalize_certification(issuer=issuer, label=label, source_text=f"manual: {text}")
