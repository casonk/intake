"""Merchant name normalisation and fuzzy correction.

After OCR extraction, merchant names are often garbled (OCR noise, font
artefacts, truncation). This module provides merchant cleanup so names like
``"Kr0ger #1234"`` can be corrected to ``"kroger"``.
"""

from __future__ import annotations

import difflib
import re

# Keywords shorter than this are skipped for fuzzy matching to avoid
# false positives (e.g. "bp" matching "bp" in "apple"). Exact token matching
# can still safely use 3-character merchants like "cvs".
_MIN_KEYWORD_LEN = 3
_FUZZY_MIN_KEYWORD_LEN = 4

_BLACKLISTED_MERCHANT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^gmail\s*-\s*order confirmation https://mail\.google\.com/mail/u/0/?$", re.IGNORECASE
    ),
)

_KNOWN_MERCHANT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bautozone\b|orders@em\.autozone\.com", re.IGNORECASE), "autozone"),
    (
        re.compile(
            r"\b(?:bass\s*pro|basspro|bassepro|outdoor\s+world)\b|basspro\.(?:com|con)",
            re.IGNORECASE,
        ),
        "bass pro",
    ),
    (
        re.compile(r"\bmi basic driver improvement course\b", re.IGNORECASE),
        "mi basic driver improvement course",
    ),
    (
        re.compile(r"\banthropic\b|\bclaude\s+pro\b|support@anthropic\.com", re.IGNORECASE),
        "anthropic",
    ),
    (
        re.compile(r"\bmcdonald(?:'s|s)\b(?:\s+restaurant(?:\s*#?\d+)?)?", re.IGNORECASE),
        "mcdonald's",
    ),
    (
        re.compile(r"\bkroger(?:\s+fuel(?:\s+center)?)\b", re.IGNORECASE),
        "kroger fuel",
    ),
    (re.compile(r"\bvodafone\b", re.IGNORECASE), "vodafone"),
    (
        re.compile(r"\b(?:kroger|kreager)(?:\.com)?\b|\bpoger\b(?:\s+plus\b)?", re.IGNORECASE),
        "kroger",
    ),
    (
        re.compile(
            r"(?:o['`]?r[ei1l][ei1l]+y|orei[il1y]+|reiliy).{0,12}(?:auto|auro).{0,12}parts?",
            re.IGNORECASE,
        ),
        "o'reilly",
    ),
    (re.compile(r"\bwalmart(?:\.com)?\b", re.IGNORECASE), "walmart"),
    (re.compile(r"\bcvs\b(?:\s+pharmacy)?\b", re.IGNORECASE), "cvs"),
)


def build_keyword_list(rules: dict) -> list[str]:
    """Return a flat list of all keywords from a nested category rules dict.

    Only includes keywords with at least ``_MIN_KEYWORD_LEN`` normalized characters.
    """
    keywords: list[str] = []
    for cat_cfg in rules.values():
        if not isinstance(cat_cfg, dict):
            continue
        keywords.extend(
            kw for kw in cat_cfg.get("keywords", []) if len(_normalise(kw)) >= _MIN_KEYWORD_LEN
        )
        for val in cat_cfg.values():
            if isinstance(val, dict) and "keywords" in val:
                keywords.extend(
                    kw for kw in val["keywords"] if len(_normalise(kw)) >= _MIN_KEYWORD_LEN
                )
    return keywords


def _normalise(text: str) -> str:
    """Lowercase and strip non-alpha characters for comparison."""
    return re.sub(r"[^a-z]", "", text.lower())


def clean_merchant_text(text: str) -> str:
    """Strip surrounding OCR noise while preserving the merchant words."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = re.sub(r"^[^\w]+", "", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"[^\w]+$", "", cleaned, flags=re.UNICODE)
    return cleaned.strip("_")


def is_blacklisted_merchant(text: str) -> bool:
    """Return True when *text* is a known recurring non-merchant header."""
    cleaned = clean_merchant_text(text)
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in _BLACKLISTED_MERCHANT_PATTERNS)


def _tokens(text: str) -> list[str]:
    return [
        _normalise(part)
        for part in re.split(r"[^A-Za-z0-9']+", text.lower())
        if len(_normalise(part)) >= _MIN_KEYWORD_LEN
    ]


def _candidate_chunks(text: str) -> set[str]:
    tokens = _tokens(text)
    candidates = set(tokens)
    for start in range(len(tokens)):
        for end in range(start + 2, min(len(tokens), start + 4) + 1):
            candidates.add("".join(tokens[start:end]))
    full = _normalise(text)
    if full:
        candidates.add(full)
    return {candidate for candidate in candidates if candidate}


def keyword_hint(text: str, keywords: list[str]) -> str | None:
    """Return a merchant keyword found exactly in *text* when possible."""
    if not text or not keywords:
        return None

    for pattern, canonical in _KNOWN_MERCHANT_PATTERNS:
        if pattern.search(text):
            return canonical

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    prepared = sorted(
        (
            (_normalise(keyword), keyword.lower(), _tokens(keyword))
            for keyword in keywords
            if _normalise(keyword)
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )

    for line in lines[:30]:
        line_tokens = _tokens(line)
        if not line_tokens:
            continue
        line_norm = _normalise(line)
        for norm_kw, keyword, keyword_tokens in prepared:
            if len(keyword_tokens) == 1:
                if norm_kw in line_tokens:
                    return keyword
            elif len(norm_kw) >= _FUZZY_MIN_KEYWORD_LEN and norm_kw in line_norm:
                return keyword

    return None


def consolidate_merchant(merchant: str, aliases: dict[str, list[str]]) -> str:
    """Return the canonical merchant name if *merchant* matches a known alias.

    Compares the normalised form of *merchant* against each alias list entry
    in *aliases* (``{canonical: [alias, ...]}``) using non-alpha-stripped
    lowercase matching.  Also accepts merchants that already match a canonical
    name (handles capitalisation variants like ``"O'Reilly"`` → ``"o'reilly"``).

    Returns *merchant* unchanged when no match is found.
    """
    if not merchant:
        return merchant

    merchant_clean = clean_merchant_text(merchant)
    if not merchant_clean:
        return ""
    merchant_clean = merchant_clean[:60].lower()

    if is_blacklisted_merchant(merchant_clean):
        return "unknown"

    for pattern, canonical in _KNOWN_MERCHANT_PATTERNS:
        if pattern.search(merchant_clean):
            return canonical

    norm_merchant = _normalise(merchant_clean)
    if not norm_merchant:
        return merchant_clean

    for canonical, alias_list in aliases.items():
        canonical_clean = clean_merchant_text(canonical)[:60].lower()
        if norm_merchant == _normalise(canonical_clean):
            return canonical_clean
        for alias in alias_list:
            if norm_merchant == _normalise(alias):
                return canonical_clean

    return merchant_clean


def fuzzy_correct(
    merchant: str,
    keywords: list[str],
    threshold: float = 0.82,
) -> str:
    """Return a corrected merchant name if a fuzzy keyword match is found.

    Compares the leading portion of *merchant* against every keyword in
    *keywords* using ``difflib.SequenceMatcher``.  If the best match ratio
    meets *threshold*, the keyword (title-cased) is returned as the
    corrected name.  Otherwise *merchant* is returned unchanged.

    The comparison uses only the first meaningful word of the merchant to
    avoid being confused by trailing store numbers or addresses.
    """
    if not merchant or not keywords:
        return merchant

    merchant_clean = clean_merchant_text(merchant)
    if not merchant_clean:
        return merchant

    for pattern, canonical in _KNOWN_MERCHANT_PATTERNS:
        if pattern.search(merchant_clean):
            return canonical

    candidates = _candidate_chunks(merchant_clean)
    if not candidates:
        return merchant_clean or merchant

    full_norm = _normalise(merchant_clean)
    best_ratio = 0.0
    best_kw: str | None = None

    for kw in keywords:
        norm_kw = _normalise(kw)
        if len(norm_kw) < _FUZZY_MIN_KEYWORD_LEN:
            continue
        if norm_kw in full_norm:
            return kw.lower()
        for candidate in candidates:
            ratio = difflib.SequenceMatcher(None, candidate, norm_kw).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_kw = kw

    if best_ratio >= threshold and best_kw:
        return best_kw.lower()
    return merchant_clean
