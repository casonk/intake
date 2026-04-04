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
    """Return a lowercase, punctuation-light phrase for boundary-aware matching."""
    folded = unicodedata.normalize("NFKD", text)
    ascii_text = folded.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("'", "").replace("’", "")
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text.lower()))


def _keyword_matches(search_target: str, keyword: str) -> bool:
    """Return True when *keyword* matches *search_target* as a token/phrase."""
    norm_keyword = _normalise_phrase(keyword)
    if not norm_keyword:
        return False
    norm_target = _normalise_phrase(search_target)
    if not norm_target:
        return False
    return f" {norm_keyword} " in f" {norm_target} "


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
    for search_target in (merchant, full_text):
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
                        return f"{cat}/{key}"
            # Fall back to top-level category keywords
            for kw in cat_cfg.get("keywords", []):
                if _keyword_matches(search_target, kw):
                    return cat
    return "general"
