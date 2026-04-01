"""Unit tests for intake.classify."""

from intake.classify import classify, load_rules

_RULES = load_rules()


def test_kroger_is_grocery():
    assert classify("Kroger #123", _RULES) == "grocery"


def test_shell_is_gas_station():
    assert classify("Shell Gas Station", _RULES) == "gas-station"


def test_unknown_falls_back_to_general():
    assert classify("Unknown Shop LLC", _RULES) == "general"


def test_case_insensitive_walmart():
    assert classify("WALMART SUPERCENTER", _RULES) == "grocery"


def test_cvs_pharmacy():
    assert classify("CVS Pharmacy #8821", _RULES) == "cvs"


def test_autozone():
    assert classify("AutoZone #1234", _RULES) == "auto"


def test_newegg_is_pc():
    assert classify("Newegg.com", _RULES) == "PC"


def test_steam_is_sw():
    assert classify("Steam Game Purchase", _RULES) == "sw"


def test_empty_merchant_is_general():
    assert classify("", _RULES) == "general"
