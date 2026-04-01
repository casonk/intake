"""Merchant name normalisation and fuzzy correction.

After OCR extraction, merchant names are often garbled (OCR noise, font
artefacts, truncation).  This module provides fuzzy matching against the
known keyword lists defined in ``config/categories.toml`` so that names
like ``"Kr0ger #1234"`` can be corrected to ``"Kroger"``.
"""

from __future__ import annotations

import difflib
import re

# Keywords shorter than this are skipped for fuzzy matching to avoid
# false positives (e.g. "bp" matching "bp" in "apple")
_MIN_KEYWORD_LEN = 4


def build_keyword_list(rules: dict) -> list[str]:
    """Return a flat list of all keywords from a nested category rules dict.

    Only includes keywords with at least ``_MIN_KEYWORD_LEN`` characters.
    """
    keywords: list[str] = []
    for cat_cfg in rules.values():
        if not isinstance(cat_cfg, dict):
            continue
        keywords.extend(
            kw for kw in cat_cfg.get("keywords", []) if len(kw) >= _MIN_KEYWORD_LEN
        )
        for val in cat_cfg.values():
            if isinstance(val, dict) and "keywords" in val:
                keywords.extend(
                    kw for kw in val["keywords"] if len(kw) >= _MIN_KEYWORD_LEN
                )
    return keywords


def _normalise(text: str) -> str:
    """Lowercase and strip non-alpha characters for comparison."""
    return re.sub(r"[^a-z]", "", text.lower())


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

    # Use the first "word" that is at least 3 chars and not all digits
    first_word = ""
    for part in re.split(r"[\s#,\.]+", merchant):
        cleaned = re.sub(r"[^a-zA-Z]", "", part)
        if len(cleaned) >= 3:
            first_word = cleaned
            break

    if not first_word:
        return merchant

    norm_merchant = _normalise(first_word)
    best_ratio = 0.0
    best_kw: str | None = None

    for kw in keywords:
        norm_kw = _normalise(kw)
        if not norm_kw:
            continue
        ratio = difflib.SequenceMatcher(None, norm_merchant, norm_kw).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_kw = kw

    if best_ratio >= threshold and best_kw:
        return best_kw.lower()
    return merchant
