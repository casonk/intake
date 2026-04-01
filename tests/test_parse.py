"""Unit tests for intake.parse."""

from datetime import date

import pytest

from intake.parse import parse_receipt


def test_scan_date_from_filename():
    r = parse_receipt("20260401_1146.pdf", "Merchant Name\nTOTAL $10.00")
    assert r.scan_date == date(2026, 4, 1)


def test_total_from_total_line():
    r = parse_receipt("20260401_0900.pdf", "Some Store\nItems...\nTOTAL $47.23")
    assert r.total == pytest.approx(47.23)


def test_total_from_amount_due():
    r = parse_receipt("20260401_0900.pdf", "Another Store\nAmount Due: 12.50")
    assert r.total == pytest.approx(12.50)


def test_total_none_when_missing():
    r = parse_receipt("20260401_0900.pdf", "Mystery Shop\nItem 1   5.00\nItem 2   3.00")
    assert r.total is None


def test_merchant_from_first_line():
    r = parse_receipt(
        "20260401_0900.pdf", "Acme Supply Co\nSome item  9.99\nTOTAL $9.99"
    )
    assert r.merchant == "Acme Supply Co"


def test_merchant_truncated_to_60_chars():
    long_name = "A" * 80
    r = parse_receipt("20260401_0900.pdf", f"{long_name}\nTOTAL $1.00")
    assert len(r.merchant) == 60


def test_total_last_match_wins():
    """Grand total is the last matching line, not an intermediate subtotal."""
    text = "Shop\nSubtotal $10.00\nTax $1.00\nTotal $11.00"
    r = parse_receipt("20260401_0900.pdf", text)
    assert r.total == pytest.approx(11.00)


def test_balance_due_parsed():
    r = parse_receipt("20260315_1400.pdf", "Dentist Office\nBalance Due $350.00")
    assert r.total == pytest.approx(350.00)
