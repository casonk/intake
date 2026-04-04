"""Unit tests for intake.merchant (fuzzy correction)."""

from intake.merchant import build_keyword_list, clean_merchant_text, consolidate_merchant, fuzzy_correct, keyword_hint

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
    assert "cvs" in kws


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


def test_clean_merchant_text_strips_leading_ocr_noise():
    assert clean_merchant_text("¥ CVS pharmacy’") == "CVS pharmacy"


def test_keyword_hint_finds_exact_merchant_token_in_later_line():
    kws = build_keyword_list(_RULES)
    raw_text = "wrleger\nHometown Grocer.\nKROGER PLUS CARD\nTOTAL 21.14\n"
    assert keyword_hint(raw_text, kws) == "kroger"


def test_keyword_hint_finds_kroger_from_noisy_ocr_variants():
    kws = build_keyword_list(_RULES)
    raw_text = ". a | i\nPOGER PLUS CUSTOMS>\nGo to www kreager.com/feedbacs\nZ TOTAL: 29.10\n"
    assert keyword_hint(raw_text, kws) == "kroger"


def test_keyword_hint_finds_autozone_from_gmail_rendered_receipt():
    kws = ["autozone", "o'reilly", "advance auto"]
    raw_text = (
        "Gmail - Here's your receipt. Thanks for shopping! https://mail.google.com/...\n"
        "AutoZone <orders@em.autozone.com>\n"
        "THANKS FOR STOPPING IN!\n"
    )
    assert keyword_hint(raw_text, kws) == "autozone"


def test_keyword_hint_finds_bass_pro_from_outdoor_world_receipt():
    kws = ["bass pro", "cabela"]
    raw_text = "World\nVisit basspro.com/feedback.\nCustomer Copy\nTOTAL $82.91\n"
    assert keyword_hint(raw_text, kws) == "bass pro"


def test_keyword_hint_finds_walmart_in_url_header():
    kws = build_keyword_list(_RULES)
    raw_text = "Order details - Walmart.com https://www.walmart.com/orders/0\nMar 14, 2026\n"
    assert keyword_hint(raw_text, kws) == "walmart"


def test_keyword_hint_collapses_vodafone_global_connect():
    kws = ["vodafone", "verizon"]
    raw_text = "Vodafone Global Connect S.à r.l.\nService Summary\nTotal 13.2 USD\n"
    assert keyword_hint(raw_text, kws) == "vodafone"


def test_fuzzy_correct_handles_multiword_ocr_noise():
    kws = ["o'reilly", "walmart", "kroger"]
    assert fuzzy_correct("Reiliy/auro parts", kws) == "o'reilly"


# ── consolidate_merchant ──────────────────────────────────────────────────────

_ALIASES = {
    "o'reilly": ["o'reilly auto parts", "oreilly auto parts"],
    "walmart": ["walmart supercenter", "walmart neighborhood market"],
    "kroger": ["kroger plus", "kroger marketplace"],
}


def test_consolidate_merchant_alias_match():
    assert consolidate_merchant("o'reilly auto parts", _ALIASES) == "o'reilly"


def test_consolidate_merchant_case_insensitive():
    assert consolidate_merchant("O'Reilly Auto Parts", _ALIASES) == "o'reilly"


def test_consolidate_merchant_canonical_already():
    assert consolidate_merchant("o'reilly", _ALIASES) == "o'reilly"


def test_consolidate_merchant_canonical_capitalized():
    assert consolidate_merchant("O'Reilly", _ALIASES) == "o'reilly"


def test_consolidate_merchant_no_match_returns_original():
    assert consolidate_merchant("advance auto parts", _ALIASES) == "advance auto parts"


def test_consolidate_merchant_empty_aliases():
    assert consolidate_merchant("o'reilly auto parts", {}) == "o'reilly auto parts"


def test_consolidate_merchant_empty_merchant():
    assert consolidate_merchant("", _ALIASES) == ""


def test_consolidate_merchant_walmart_alias():
    assert consolidate_merchant("walmart supercenter", _ALIASES) == "walmart"


def test_consolidate_merchant_does_not_partial_match():
    # "kroger plus #1234" normalises to "krogerplus" which matches the "kroger plus" alias
    # — store numbers are stripped, so this correctly consolidates to the canonical
    assert consolidate_merchant("kroger plus #1234", _ALIASES) == "kroger"
