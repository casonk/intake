"""Classify a merchant string into a receipt category using configurable rules."""

from __future__ import annotations

from pathlib import Path

_DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "categories.toml"


def load_rules(config_path: Path | None = None) -> dict[str, list[str]]:
    """Load category rules from a TOML file.

    Returns a dict mapping category name -> list of keyword strings.
    """
    import toml

    path = config_path or _DEFAULT_CONFIG
    data = toml.load(path)
    return data.get("categories", {})


def classify(merchant: str, rules: dict[str, list[str]], full_text: str = "") -> str:
    """Return the category for *merchant* by substring matching rules.

    Checks the merchant string first. If no match, falls back to searching
    the full OCR text so garbled first-lines don't prevent categorisation.
    Falls back to 'general'.
    """
    for search_target in (merchant, full_text):
        if not search_target:
            continue
        lower = search_target.lower()
        for category, keywords in rules.items():
            if category == "general":
                continue
            for kw in keywords:
                if kw.lower() in lower:
                    return category
    return "general"
