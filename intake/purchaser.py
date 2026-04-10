"""Purchaser normalization and blacklist helpers."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_BLACKLISTED_PURCHASERS: frozenset[str] = frozenset(
    {
        "account",
        "cardholder",
        "customer",
        "member",
        "guest",
        "user",
        "name",
        "copy",
        "customer copy",
        "signature verified",
        "visa",
        "mastercard",
        "master card",
        "amex",
        "american express",
        "discover",
        "debit",
        "credit",
        "n/a",
        "na",
        "none",
        "unknown",
        "null",
        "phone",
        "pickup",
        "signature",
        "signature please pay",
        "signature please pay e",
        "date",
        "concern",
        "support team",
        "bmw club",
    }
)

_BLACKLISTED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bthank\b.*\bshopping\b", re.IGNORECASE),
    re.compile(
        r"\b(?:repair|agreement|acknowledg(?:e|es|ed|ing)|receiv(?:e|es|ed|ing)|"
        r"states?|truck|leak(?:s|ing)?|coolant|click(?:s|ing)?|materials?|charge|"
        r"mobile|club|news|call|date)\b",
        re.IGNORECASE,
    ),
)

_STAFF_LABEL_RE = re.compile(
    r"\b(?:cashier|counter|associate|attendant|clerk|employee|operator|drawer|served\s+by)\b",
    re.IGNORECASE,
)
_STAFF_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{1,20}")
_STAFF_LABEL_TOKENS = frozenset(
    {
        "cashier",
        "counter",
        "associate",
        "attendant",
        "clerk",
        "employee",
        "operator",
        "drawer",
        "served",
        "by",
    }
)

_KNOWN_PURCHASER_ALIASES: dict[str, tuple[str, ...]] = {
    "cason konzer": (
        "cason",
        "cason konzer",
        "casonkonzer",
        "casonkonzer@gmail.com",
        "cason konzep",
    ),
}


def _collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _lookup_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _reorder_comma_name(text: str) -> str:
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) == 2 and all(re.fullmatch(r"[A-Za-z .'\-]+", part) for part in parts):
        return f"{parts[1]} {parts[0]}"
    return text


def _staff_name_tokens(raw_text: str) -> set[str]:
    tokens: set[str] = set()
    for line in raw_text.splitlines():
        if not _STAFF_LABEL_RE.search(line):
            continue
        for token in _STAFF_TOKEN_RE.findall(line):
            lowered = token.lower()
            if lowered not in _STAFF_LABEL_TOKENS:
                tokens.add(lowered)
    return tokens


def is_blacklisted_purchaser(text: str | None) -> bool:
    """Return True if *text* is a known invalid purchaser artifact."""
    if text is None:
        return False
    cleaned = _collapse_spaces(text)
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if lowered in _BLACKLISTED_PURCHASERS:
        return True
    if "@" in lowered:
        return True
    return any(pattern.search(lowered) for pattern in _BLACKLISTED_PATTERNS)


def is_probable_staff_name(text: str | None, raw_text: str | None) -> bool:
    """Return True when *text* looks like a cashier / counter name, not a purchaser."""
    if text is None or raw_text is None:
        return False
    cleaned = _collapse_spaces(text).lower()
    if not cleaned or " " in cleaned:
        return False
    for token in _staff_name_tokens(raw_text):
        if token == cleaned:
            return True
        if SequenceMatcher(None, token, cleaned).ratio() >= 0.6:
            return True
    return False


def normalize_purchaser(text: str | None) -> str | None:
    """Return the canonical purchaser name, or None when the value is unusable."""
    if text is None:
        return None

    cleaned = _collapse_spaces(text)
    if not cleaned:
        return None

    reordered = _reorder_comma_name(cleaned)
    lookup = _lookup_key(reordered)
    for canonical, aliases in _KNOWN_PURCHASER_ALIASES.items():
        all_values = (canonical, *aliases)
        if any(lookup == _lookup_key(value) for value in all_values):
            return canonical

    if is_blacklisted_purchaser(reordered):
        return None

    return reordered[:60].lower()
