"""Tests for purchaser normalization helpers."""

from intake.purchaser import (
    is_blacklisted_purchaser,
    is_probable_staff_name,
    normalize_purchaser,
)


def test_normalize_purchaser_canonicalizes_known_aliases():
    assert normalize_purchaser("jane") == "jane sample"
    assert normalize_purchaser("Jane Sample") == "jane sample"
    assert normalize_purchaser("janesample@gmail.com") == "jane sample"
    assert normalize_purchaser("Sample / Jane A") == "jane sample"


def test_normalize_purchaser_canonicalizes_family_aliases():
    assert normalize_purchaser("Al Thornwood") == "Alaric g Thornwood"
    assert normalize_purchaser("Michael J. Harrington Jr.") == "Michael Harrington"
    assert normalize_purchaser("Mike Harringtonn") == "Michael Harrington"


def test_normalize_purchaser_reorders_comma_name():
    assert normalize_purchaser("Sample, Jane") == "jane sample"


def test_normalize_purchaser_blacklists_footer_and_phone_artifacts():
    assert normalize_purchaser("Phone") is None
    assert normalize_purchaser("THANK at FOR SHOPPING") is None
    assert normalize_purchaser("PAY SHOP CHARGE FOR REPAIR ORDER") is None


def test_is_blacklisted_purchaser_matches_artifacts_only():
    assert is_blacklisted_purchaser("Phone") is True
    assert is_blacklisted_purchaser("THANK at FOR SHOPPING") is True
    assert is_blacklisted_purchaser("HEREBY ACKNOWLEDGES RECEIVING") is True
    assert is_blacklisted_purchaser("Jane Doe") is False


def test_is_probable_staff_name_matches_counter_line_fuzzily():
    raw_text = "Counter #: 862305 Sal\nDate: 02/26/2026 05:42 PM"
    assert is_probable_staff_name("sam", raw_text) is True
    assert is_probable_staff_name("jane sample", raw_text) is False
