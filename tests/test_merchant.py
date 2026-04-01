"""Unit tests for intake.merchant (fuzzy correction)."""

from intake.merchant import build_keyword_list, fuzzy_correct


_RULES = {
    "grocery": {
        "label": "Grocery",
        "keywords": [],
        "supermarket": {
            "label": "Supermarkets",
            "keywords": ["kroger", "walmart", "meijer"],
        },
        "organic": {"label": "Organic", "keywords": ["whole foods", "trader joes"]},
    },
    "pharmacy": {
        "label": "Pharmacy",
        "keywords": ["walgreens", "cvs"],
    },
    "general": {"label": "General", "keywords": []},
}


def test_build_keyword_list_flattens_subcategories():
    kws = build_keyword_list(_RULES)
    assert "kroger" in kws
    assert "walgreens" in kws
    assert "whole foods" in kws


def test_build_keyword_list_excludes_short_keywords():
    kws = build_keyword_list(_RULES)
    # "cvs" is 3 chars, below the 4-char minimum
    assert "cvs" not in kws


def test_fuzzy_correct_exact_match():
    kws = build_keyword_list(_RULES)
    assert fuzzy_correct("Kroger #123", kws) == "kroger"


def test_fuzzy_correct_ocr_noise():
    kws = build_keyword_list(_RULES)
    # "Kr0ger" — OCR substituted '0' for 'o'
    result = fuzzy_correct("Kr0ger Store", kws)
    assert result == "kroger"


def test_fuzzy_correct_no_match_returns_original():
    kws = build_keyword_list(_RULES)
    original = "Completely Unknown Shop"
    assert fuzzy_correct(original, kws) == original


def test_fuzzy_correct_below_threshold_returns_original():
    kws = ["walgreens"]
    # "xyz" is not close to "walgreens"
    assert fuzzy_correct("xyz", kws, threshold=0.82) == "xyz"


def test_fuzzy_correct_empty_merchant():
    kws = build_keyword_list(_RULES)
    assert fuzzy_correct("", kws) == ""


def test_fuzzy_correct_empty_keywords():
    assert fuzzy_correct("Kroger", []) == "Kroger"
