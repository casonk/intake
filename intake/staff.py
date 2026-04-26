"""Structured staff extraction for receipt service/cashier fields."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ReceiptStaff:
    """One staff/service person or register identifier found on a receipt."""

    role: str
    name: str
    source_text: str


_ROLE_ALIASES: dict[str, str] = {
    "cashier": "cashier",
    "clerk": "clerk",
    "counter": "counter",
    "employee": "employee",
    "operator": "operator",
    "server": "server",
    "served by": "server",
    "waiter": "server",
    "waitress": "server",
}

_ROLE_PATTERN = "|".join(re.escape(role) for role in sorted(_ROLE_ALIASES, key=len, reverse=True))
_LINE_START_STAFF_RE = re.compile(
    r"^\s*(?P<role>server|waiter|waitress)\b\s*(?:#|no\.?|number)?\s*(?:was|:|-)?\s+(?P<name>.+)",
    re.IGNORECASE,
)
_EXPLICIT_STAFF_RE = re.compile(
    rf"\b(?P<role>{_ROLE_PATTERN})\b\s*(?:#|no\.?|number)?\s*(?:was|:|-)\s*(?P<name>.+)",
    re.IGNORECASE,
)
_YOUR_CASHIER_RE = re.compile(
    r"\byour\s+cashier\s+was\s+(?P<name>[A-Za-z0-9][A-Za-z0-9 .#'\-]{1,40})",
    re.IGNORECASE,
)
_TRAILING_STOP_RE = re.compile(
    r"\b(?:station|terminal|batch|table|tab|check|date|time|total|amount|method|please|"
    r"customer|service|associate|receipt)\b",
    re.IGNORECASE,
)
_REJECT_LINE_RE = re.compile(
    r"(?:please\s+pay|microsoft\s+windows\s+server|windows\s+server|customer\s+service\s+associate|"
    r"not\s+responsible|courtesy\s+cashier|invoice/trans)",
    re.IGNORECASE,
)


def _collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_role(role: str) -> str:
    return _ROLE_ALIASES.get(role.lower(), role.lower())


def _clean_staff_name(value: str) -> str | None:
    text = value.replace("]", "l")
    text = re.split(r"[;|]", text, maxsplit=1)[0]
    marker = _TRAILING_STOP_RE.search(text)
    if marker:
        text = text[: marker.start()]
    text = re.sub(r"^\s*(?:#|:|-)+", "", text)
    text = re.sub(r"^\s*\d+\s+(?=[A-Za-z])", "", text)
    text = _collapse_spaces(text.strip(" -*:;,.[]()$"))
    if not text:
        return None
    if not re.search(r"[A-Za-z0-9]", text):
        return None
    if len(text) > 40:
        return None
    lowered = text.lower()
    if lowered in {"pay", "please", "your", "table", "receipt", "copy"}:
        return None
    return lowered


def normalize_receipt_staff(
    *,
    role: object | None,
    name: object | None,
    source_text: object | None = None,
) -> ReceiptStaff | None:
    """Return normalized staff metadata, or None when the payload is unusable."""
    if role is None or name is None:
        return None
    role_text = _collapse_spaces(str(role)).lower()
    if not role_text:
        return None
    normalized_role = _normalize_role(role_text)
    cleaned_name = _clean_staff_name(str(name))
    if cleaned_name is None:
        return None
    if cleaned_name.isdigit() and normalized_role not in {"cashier", "employee"}:
        return None
    source = _collapse_spaces(str(source_text or f"{normalized_role}: {cleaned_name}"))
    return ReceiptStaff(
        role=normalized_role[:24],
        name=cleaned_name[:60],
        source_text=source[:160],
    )


def receipt_staff_to_dicts(staff: list[ReceiptStaff] | list[dict] | None) -> list[dict]:
    """Render staff metadata as JSON-friendly dictionaries."""
    rendered: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in staff or []:
        normalized = (
            item
            if isinstance(item, ReceiptStaff)
            else normalize_receipt_staff(
                role=item.get("role") if isinstance(item, dict) else None,
                name=item.get("name") if isinstance(item, dict) else None,
                source_text=item.get("source_text") if isinstance(item, dict) else None,
            )
        )
        if normalized is None:
            continue
        key = (normalized.role, normalized.name)
        if key in seen:
            continue
        replaced = False
        for idx, existing in enumerate(rendered):
            if existing["role"] != normalized.role:
                continue
            existing_name = str(existing["name"])
            if normalized.name.startswith(f"{existing_name} "):
                seen.discard((existing["role"], existing_name))
                rendered[idx] = asdict(normalized)
                seen.add(key)
                replaced = True
                break
            if existing_name.startswith(f"{normalized.name} "):
                replaced = True
                break
        if replaced:
            continue
        seen.add(key)
        rendered.append(asdict(normalized))
    return rendered


def receipt_staff_from_dicts(value: object) -> list[ReceiptStaff]:
    """Return normalized staff objects from a decoded JSON payload."""
    if not isinstance(value, list):
        return []
    normalized: list[ReceiptStaff] = []
    for item in value:
        if isinstance(item, ReceiptStaff):
            normalized.append(item)
            continue
        if not isinstance(item, dict):
            continue
        parsed = normalize_receipt_staff(
            role=item.get("role"),
            name=item.get("name"),
            source_text=item.get("source_text"),
        )
        if parsed is not None:
            normalized.append(parsed)
    return normalized


def extract_staff(raw_text: str) -> list[ReceiptStaff]:
    """Extract server/cashier/clerk metadata from OCR text."""
    found: list[ReceiptStaff] = []
    for raw_line in raw_text.splitlines():
        line = _collapse_spaces(raw_line)
        if not line or _REJECT_LINE_RE.search(line):
            continue

        cashier_match = _YOUR_CASHIER_RE.search(line)
        if cashier_match:
            staff = normalize_receipt_staff(
                role="cashier",
                name=cashier_match.group("name"),
                source_text=line,
            )
            if staff is not None:
                found.append(staff)
            continue

        match = _EXPLICIT_STAFF_RE.search(line) or _LINE_START_STAFF_RE.search(line)
        if not match:
            continue
        staff = normalize_receipt_staff(
            role=match.group("role"),
            name=match.group("name"),
            source_text=line,
        )
        if staff is not None:
            found.append(staff)

    return receipt_staff_from_dicts(receipt_staff_to_dicts(found))
