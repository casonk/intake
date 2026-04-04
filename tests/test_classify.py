"""Unit tests for intake.classify."""

from pathlib import Path

from intake.classify import classify, load_labels, load_rules

_FIXTURE = Path(__file__).parent / "fixtures" / "categories.toml"
_RULES = load_rules(_FIXTURE)


def test_kroger_is_grocery_supermarket():
    assert classify("Kroger #123", _RULES) == "grocery/supermarket"


def test_shell_is_fuel_station():
    assert classify("Shell Gas Station", _RULES) == "fuel/station"


def test_unknown_falls_back_to_general():
    assert classify("Unknown Shop LLC", _RULES) == "general"


def test_case_insensitive_walmart():
    assert classify("WALMART SUPERCENTER", _RULES) == "grocery/supermarket"


def test_cvs_pharmacy_top_level():
    assert classify("CVS Pharmacy #8821", _RULES) == "pharmacy"


def test_autozone_is_auto_parts():
    assert classify("AutoZone #1234", _RULES) == "auto/parts"


def test_newegg_is_pc():
    assert classify("Newegg.com", _RULES) == "pc"


def test_steam_is_sw():
    assert classify("Steam Game Purchase", _RULES) == "sw"


def test_empty_merchant_is_general():
    assert classify("", _RULES) == "general"


def test_subcategory_beats_parent_keywords():
    """Subcategory match must win even when parent also has matching keywords."""
    import tempfile
    import textwrap
    from pathlib import Path

    from intake.classify import load_rules as lr

    cfg = textwrap.dedent("""
        [categories.shop]
        label    = "Shop"
        keywords = ["acme"]

        [categories.shop.online]
        label    = "Online Shop"
        keywords = ["acme online"]

        [categories.general]
        label    = "General"
        keywords = []
    """)
    with tempfile.NamedTemporaryFile(suffix=".toml", mode="w", delete=False) as f:
        f.write(cfg)
        tmp = Path(f.name)
    rules = lr(tmp)
    assert classify("Acme Online Store", rules) == "shop/online"
    tmp.unlink()


def test_load_labels_includes_subcategories():
    labels = load_labels(_FIXTURE)
    assert labels["grocery"] == "Grocery & Supermarkets"
    assert labels["grocery/supermarket"] == "Supermarket Chains"
    assert labels["fuel/station"] == "Gas Stations"


def test_mobile_text_does_not_match_mobil_keyword():
    rules = {
        "gas-station": {"label": "Gas Stations", "keywords": ["mobil"]},
        "general": {"label": "General", "keywords": []},
    }
    assert classify("Unknown Shop", rules, "mobile data plan") == "general"


def test_vodafone_classifies_as_phone_carrier():
    rules = {
        "gas-station": {"label": "Gas Stations", "keywords": ["mobil"]},
        "phone": {
            "label": "Telecommunications",
            "keywords": [],
            "carrier": {"label": "Telecom Carriers", "keywords": ["vodafone"]},
        },
        "general": {"label": "General", "keywords": []},
    }
    assert (
        classify(
            "vodafone global connect s.à r.l.",
            rules,
            "mobile data plan and roaming service",
        )
        == "phone/carrier"
    )
