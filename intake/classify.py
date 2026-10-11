"""Classify a merchant string into a receipt category using configurable rules."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_CONFIG_LOCAL = Path(__file__).parent.parent / "config" / "categories.toml"
_CONFIG_EXAMPLE = Path(__file__).parent.parent / "config" / "categories.toml.example"


def _config_path(override: Path | None) -> Path:
    if override:
        return override
    return _CONFIG_LOCAL if _CONFIG_LOCAL.exists() else _CONFIG_EXAMPLE


def _is_subcategory(value: object) -> bool:
    """Return True if *value* is a dict representing a subcategory entry."""
    return isinstance(value, dict) and "keywords" in value


def _normalise_phrase(text: str) -> str:
    """Return a lowercase, punctuation-light phrase for boundary-aware matching.

    Folds Latin accents (NFKD + drop combining marks) and full-width forms to
    half-width, but otherwise keeps non-Latin scripts intact — ASCII-encoding
    everything (the previous approach) silently dropped Japanese/CJK merchant
    names to an empty string, so they could never match any keyword.
    """
    folded = unicodedata.normalize("NFKD", text)
    # Only strip Latin combining diacritics (U+0300-U+036F, e.g. the acute
    # accent NFKD splits off of "é"). Other scripts also decompose under
    # NFKD — notably voiced/semi-voiced Japanese kana (dakuten/handakuten,
    # e.g. "ば" -> "は" + U+3099) — and stripping those combining marks
    # would silently change the character read, not just its accent.
    stripped = "".join(
        ch for ch in folded if not (unicodedata.combining(ch) and ord(ch) <= 0x036F)
    )
    stripped = unicodedata.normalize("NFC", stripped)
    stripped = stripped.replace("'", "").replace("’", "")
    return " ".join(re.findall(r"[^\W_]+", stripped.lower(), re.UNICODE))


def _keyword_matches(search_target: str, keyword: str) -> bool:
    """Return True when *keyword* matches *search_target* as a token/phrase."""
    norm_keyword = _normalise_phrase(keyword)
    if not norm_keyword:
        return False
    norm_target = _normalise_phrase(search_target)
    if not norm_target:
        return False
    return f" {norm_keyword} " in f" {norm_target} "


def _match_score(keyword: str, category_path: str, target_index: int) -> tuple[int, int, int, int]:
    """Return a tuple where higher values mean a more specific match."""
    norm_keyword = _normalise_phrase(keyword)
    token_count = len(norm_keyword.split())
    char_count = len(norm_keyword)
    depth = category_path.count("/") + 1
    # Merchant matches (target_index=0) win tie-breaks over raw-text matches.
    return (token_count, char_count, depth, -target_index)


def load_rules(config_path: Path | None = None) -> dict:
    """Load category rules from a TOML file.

    Returns a nested dict:
      { "category": { "label": str, "keywords": [...],
                      "subcat": { "label": str, "keywords": [...] }, ... } }

    Subcategories are any child dict that contains a ``keywords`` key.
    """
    import toml

    data = toml.load(_config_path(config_path))
    return data.get("categories", {})


def load_merchant_aliases(config_path: Path | None = None) -> dict[str, list[str]]:
    """Load the merchant alias map from the TOML config.

    Returns a dict mapping each canonical merchant name to a list of alias
    strings: ``{ "o'reilly": ["o'reilly auto parts", ...], ... }``.

    Defined under ``[merchants]`` in ``categories.toml``.
    """
    import toml

    data = toml.load(_config_path(config_path))
    raw = data.get("merchants", {})
    return {canonical: list(aliases) for canonical, aliases in raw.items()}


def load_labels(config_path: Path | None = None) -> dict[str, str]:
    """Return a flat mapping of category (and category/subcategory) to display label.

    Falls back to the key name when no label is defined.
    """
    rules = load_rules(config_path)
    labels: dict[str, str] = {}
    for cat, cat_cfg in rules.items():
        labels[cat] = cat_cfg.get("label", cat)
        for key, val in cat_cfg.items():
            if _is_subcategory(val):
                labels[f"{cat}/{key}"] = val.get("label", key)
    return labels


def classify(merchant: str, rules: dict, full_text: str = "") -> str:
    """Return the most specific category for *merchant*.

    Checks merchant name first, then falls back to full OCR text.
    Subcategory keywords are evaluated before parent keywords so the
    most specific match always wins.  Returns ``"general"`` if nothing
    matches.

    Return value is ``"category"`` or ``"category/subcategory"``.
    """
    best_category = "general"
    best_score: tuple[int, int, int, int] | None = None

    for target_index, search_target in enumerate((merchant, full_text)):
        if not search_target:
            continue
        for cat, cat_cfg in rules.items():
            if cat == "general":
                continue
            # Check subcategories first (most specific)
            for key, val in cat_cfg.items():
                if not _is_subcategory(val):
                    continue
                for kw in val.get("keywords", []):
                    if _keyword_matches(search_target, kw):
                        category_path = f"{cat}/{key}"
                        score = _match_score(kw, category_path, target_index)
                        if best_score is None or score > best_score:
                            best_score = score
                            best_category = category_path
            # Fall back to top-level category keywords
            for kw in cat_cfg.get("keywords", []):
                if _keyword_matches(search_target, kw):
                    score = _match_score(kw, cat, target_index)
                    if best_score is None or score > best_score:
                        best_score = score
                        best_category = cat
    return best_category
