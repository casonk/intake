"""Classify a merchant string into a receipt category using configurable rules."""

from __future__ import annotations

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
        lower = search_target.lower()
        for cat, cat_cfg in rules.items():
            if cat == "general":
                continue
            # Check subcategories first (most specific)
            for key, val in cat_cfg.items():
                if not _is_subcategory(val):
                    continue
                for kw in val.get("keywords", []):
                    if kw.lower() in lower:
                        return f"{cat}/{key}"
            # Fall back to top-level category keywords
            for kw in cat_cfg.get("keywords", []):
                if kw.lower() in lower:
                    return cat
    return "general"
