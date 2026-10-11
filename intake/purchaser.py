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
        "cash customer",
        "paypal",
        "venmo",
        "zelle",
        "cash app",
        "cashapp",
        "apple pay",
        "google pay",
        "savings",
        "sign in",
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
    r"\b(?:cashier|counter|associate|attendant|clerk|employee|operator|drawer|"
    r"server|waiter|waitress|bartender|served\s+by|"
    r"salesperson|sales\s*rep(?:resentative)?|salesman|advisor|mgr|manager|"
    r"tech(?:nician)?|sold\s+by|received\s+by)\b",
    re.IGNORECASE,
)
# "Robin S" — first name + last-initial, a common wait-staff privacy
# convention. Matched separately from a bare single word so the fuzzy-match
# guard below can still reject genuinely different multi-word names.
_NAME_LAST_INITIAL_RE = re.compile(r"^([a-z]+)\s+[a-z]\.?$")
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
        "server",
        "waiter",
        "waitress",
        "bartender",
        "salesperson",
        "sales",
        "rep",
        "representative",
        "salesman",
        "advisor",
        "mgr",
        "manager",
        "tech",
        "technician",
        "sold",
        "received",
    }
)

_KNOWN_PURCHASER_ALIASES: dict[str, tuple[str, ...]] = {}


def register_purchaser_aliases(aliases: dict[str, list[str]]) -> None:
    """Populate the alias table from settings (call once at startup).

    Each key is the canonical name; the value is a list of alternate spellings,
    OCR variants, email addresses, and comma-inverted forms that should resolve
    to that canonical name.
    """
    _KNOWN_PURCHASER_ALIASES.clear()
    _KNOWN_PURCHASER_ALIASES.update(
        {canonical: tuple(variants) for canonical, variants in aliases.items()}
    )


_BILL_TO_LABEL_RE = re.compile(r"\b(?:bill\s*to|invoice\s*to|ship\s*to)\b", re.IGNORECASE)
_NUMERIC_ONLY_LINE_RE = re.compile(r"^[\d\-#\s]+$")
_MIN_BILLED_TO_LEN = 6


def _looks_like_name_or_business(candidate: str) -> bool:
    """Reject fragments this heuristic tends to pick up by accident: bare
    labels ("Counter", "Invoice"), addresses/phone numbers (digit-heavy), and
    anything too short to be a real name."""
    if len(candidate) < _MIN_BILLED_TO_LEN:
        return False
    if not re.search(r"[ ,]", candidate):
        return False  # single bare word, e.g. "COUNTER" / "INVOICE"
    digits = sum(ch.isdigit() for ch in candidate)
    letters = sum(ch.isalpha() for ch in candidate)
    return letters > digits


_BUSINESS_SUFFIX_RE = re.compile(
    r"\b(?:inc\.?|llc|corp\.?|co\.?|ltd\.?|company|repair|equip(?:ment)?\.?|"
    r"automotive|auto\s+parts|center|shop|towing|garage|dealership)\b",
    re.IGNORECASE,
)


def looks_like_business_name(candidate: str) -> bool:
    """True when *candidate* reads as a business, not a person — used to keep
    the Bill To/Ship To fallback (see billed_to_purchaser_fallback) from
    substituting a company name in for the purchaser."""
    return bool(_BUSINESS_SUFFIX_RE.search(candidate))


def billed_to_purchaser_fallback(rejected_name: str, raw_text: str) -> str | None:
    """Return a Bill To/Ship To name to use instead of *rejected_name*, when
    an AI-suggested purchaser was rejected as a staff name (advisor,
    salesperson, technician, ...) but the document also names a real
    customer elsewhere — e.g. "SOLD BY: MARCUS REYES" (staff) on an
    invoice whose "BILL TO: MICHAEL HARRINGTON" names the actual customer.

    Returns None when there's no Bill To name, it's the same rejected staff
    name, it also looks like staff, or it reads as a business rather than a
    person — any of which means there's nothing safe to substitute.
    """
    billed_to = extract_billed_to_name(raw_text)
    if not billed_to:
        return None
    if _collapse_spaces(billed_to).lower() == _collapse_spaces(rejected_name).lower():
        return None
    if looks_like_business_name(billed_to):
        return None
    if is_probable_staff_name(billed_to, raw_text):
        return None
    return billed_to


def extract_billed_to_name(raw_text: str) -> str | None:
    """Return the name following a "Bill To" / "Invoice To" / "Ship To"
    label, skipping a leading bare customer-number line where present.

    Used to flag receipts addressed to someone else entirely (a different
    person or a business) — worth a second look before counting as personal
    spend, e.g. a wholesale parts invoice billed to a repair shop rather than
    to the account holder. Deliberately conservative: returns None rather
    than a fragment for anything that doesn't look like a real name.
    """
    lines = (raw_text or "").splitlines()
    for i, line in enumerate(lines):
        if not _BILL_TO_LABEL_RE.search(line):
            continue
        after_label = _BILL_TO_LABEL_RE.sub("", line).strip(" :-\t")
        for candidate in (after_label, *lines[i + 1 : i + 4]):
            candidate = candidate.strip()
            if not candidate or _NUMERIC_ONLY_LINE_RE.match(candidate):
                continue
            if not _looks_like_name_or_business(candidate):
                continue
            return candidate[:80]
        return None
    return None


def has_registered_purchaser_aliases() -> bool:
    """Return True once register_purchaser_aliases has been given a non-empty
    table — used to gate the "billed to a different party" review flag so it
    stays silent for anyone who hasn't configured purchaser aliases."""
    return bool(_KNOWN_PURCHASER_ALIASES)


def is_known_purchaser_name(name: str) -> bool:
    """Return True when *name* matches a registered canonical purchaser or
    one of its aliases (case/punctuation-insensitive, comma-inversion aware —
    "Harrington, Michael" matches an alias of "Michael Harrington")."""
    keys = {_lookup_key(name), _lookup_key(_reorder_comma_name(name))}
    keys.discard("")
    if not keys:
        return False
    for canonical, variants in _KNOWN_PURCHASER_ALIASES.items():
        if _lookup_key(canonical) in keys:
            return True
        if any(_lookup_key(variant) in keys for variant in variants):
            return True
    return False


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
    if not cleaned:
        return False
    try:
        from intake.staff import extract_staff  # noqa: PLC0415

        for staff in extract_staff(raw_text):
            staff_name = _collapse_spaces(staff.name).lower()
            if staff_name == cleaned:
                return True
    except Exception:
        pass

    tokens = _staff_name_tokens(raw_text)

    # A full multi-word name (e.g. "marcus reyes", "sandra kim") where
    # every word independently appears on a staff-labeled line is safe to
    # accept on an exact basis — unlike the fuzzy single-word match below,
    # requiring every word to match removes the false-positive risk that
    # match is guarded against, so it isn't restricted to one word.
    words = cleaned.split()
    if len(words) > 1 and all(word in tokens for word in words):
        return True

    last_initial_match = _NAME_LAST_INITIAL_RE.match(cleaned)
    if last_initial_match:
        cleaned = last_initial_match.group(1)
    elif " " in cleaned:
        return False
    for token in tokens:
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
