"""Tests for purchaser normalization helpers."""

from intake.purchaser import (
    billed_to_purchaser_fallback,
    is_blacklisted_purchaser,
    is_probable_staff_name,
    looks_like_business_name,
    normalize_purchaser,
)


def test_normalize_purchaser_canonicalizes_known_aliases():
    assert normalize_purchaser("jane") == "jane sample"
    assert normalize_purchaser("Jane Sample") == "jane sample"
    assert normalize_purchaser("janesample@gmail.com") == "jane sample"
    assert normalize_purchaser("Sample / Jane A") == "jane sample"


def test_normalize_purchaser_canonicalizes_family_aliases():
    assert normalize_purchaser("Al Thornwood") == "alaric g thornwood"
    assert normalize_purchaser("Michael J. Harrington Jr.") == "michael harrington"
    assert normalize_purchaser("Mike Harringtonn") == "michael harrington"


def test_normalize_purchaser_reorders_comma_name():
    assert normalize_purchaser("Sample, Jane") == "jane sample"


def test_normalize_purchaser_blacklists_footer_and_phone_artifacts():
    assert normalize_purchaser("Phone") is None
    assert normalize_purchaser("THANK at FOR SHOPPING") is None
    assert normalize_purchaser("PAY SHOP CHARGE FOR REPAIR ORDER") is None


def test_normalize_purchaser_blacklists_ui_and_account_phrases():
    # "Member Savings" / "Member Sign In" on a loyalty-program footer matched
    # the generic cardholder/member regex and got captured as a name.
    assert normalize_purchaser("Savings") is None
    assert normalize_purchaser("Sign In") is None


def test_is_blacklisted_purchaser_matches_artifacts_only():
    assert is_blacklisted_purchaser("Phone") is True
    assert is_blacklisted_purchaser("THANK at FOR SHOPPING") is True
    assert is_blacklisted_purchaser("HEREBY ACKNOWLEDGES RECEIVING") is True
    assert is_blacklisted_purchaser("Jane Doe") is False


def test_is_probable_staff_name_matches_counter_line_fuzzily():
    raw_text = "Counter #: 862305 Sal\nDate: 02/26/2026 05:42 PM"
    assert is_probable_staff_name("sam", raw_text) is True
    assert is_probable_staff_name("jane sample", raw_text) is False


def test_is_probable_staff_name_matches_server_line():
    # A bare "Server" label (not just "served by") should still be recognized
    # so the server's name isn't mistaken for the purchaser.
    raw_text = "Server 100000001/Robin S\nCHK 12957\nTable 14"
    assert is_probable_staff_name("robin s", raw_text) is True
    assert is_probable_staff_name("jordan avery", raw_text) is False


def test_is_probable_staff_name_matches_salesperson_line():
    # Auto-parts and repair-shop receipts commonly print a Salesperson/
    # Advisor/Manager field; that name is staff, not the purchaser.
    assert is_probable_staff_name("christian", "Salesperson: Christian\nStore #8650") is True
    assert is_probable_staff_name("jordan avery", "Salesperson: Christian") is False


def test_is_probable_staff_name_matches_full_multi_word_name_on_staff_line():
    # A full two-word name (not a single word or "name + initial") is exempt
    # from the fuzzy-match guard when every word independently appears on a
    # staff-labeled line — exact whole-name membership carries none of the
    # false-positive risk that guard exists for.
    assert is_probable_staff_name("sandra kim", "Advisor: Sandra Kim") is True
    assert is_probable_staff_name("marcus reyes", "SOLD BY: MARCUS REYES") is True
    assert is_probable_staff_name("michael harrington", "Advisor: Sandra Kim") is False


def test_is_probable_staff_name_does_not_bridge_separate_lines():
    # A documented gap, not a guarantee: when the label and handwritten name
    # land on different OCR lines (a common layout for "Sales Rep:" fields
    # followed by a signature line below), this line-scoped matcher can't
    # associate them. Such cases still need a manual correction.
    assert is_probable_staff_name("tracy", "Sales Rep: 0, Salesman\nTRACY") is False


def test_looks_like_business_name():
    assert looks_like_business_name("Lakewood Parts & Equip.") is True
    assert looks_like_business_name("Acme Mobile Repair") is True
    assert looks_like_business_name("Michael Harrington") is False


def test_billed_to_purchaser_fallback_finds_customer_behind_staff_name():
    text = (
        "SOLD BY: MARCUS REYES\n"
        "BILL TO: MICHAEL HARRINGTON\n"
        "100 EXAMPLE TRL E LOT 1"
    )
    assert billed_to_purchaser_fallback("marcus reyes", text) == "MICHAEL HARRINGTON"


def test_billed_to_purchaser_fallback_rejects_business_bill_to():
    text = "SOLD BY: MARCUS REYES\nBILL TO: ACME MOBILE REPAIR\n200 EXAMPLE RD"
    assert billed_to_purchaser_fallback("marcus reyes", text) is None


def test_billed_to_purchaser_fallback_none_when_bill_to_is_same_staff_name():
    text = "SOLD BY: MARCUS REYES\nBILL TO: MARCUS REYES"
    assert billed_to_purchaser_fallback("marcus reyes", text) is None


def test_billed_to_purchaser_fallback_none_when_no_bill_to_field():
    assert billed_to_purchaser_fallback("marcus reyes", "SOLD BY: MARCUS REYES") is None
