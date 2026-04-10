"""Unit tests for intake.parse."""

from datetime import date

import pytest

from intake.parse import _parse_scan_date, parse_receipt


def test_scan_date_from_filename():
    r = parse_receipt("20260401_1146.pdf", "Merchant Name\nTOTAL $10.00")
    assert r.scan_date == date(2026, 4, 1)


def test_scan_date_yyyymm_fallback():
    assert _parse_scan_date("202601_eSIM.pdf") == date(2026, 1, 1)


def test_scan_date_invalid_8digit_skipped():
    # Stata_Order_2000412779 — 20004127 has month=41 (invalid), falls to 6-digit
    assert _parse_scan_date("Stata_Order_2000412779.pdf").year >= 2000


def test_scan_date_no_digits_returns_date(tmp_path):
    # Non-date filename with no digits: falls back to file mtime or today
    f = tmp_path / "ASME.pdf"
    f.write_bytes(b"")
    result = _parse_scan_date("ASME.pdf", pdf_path=f)
    assert isinstance(result, date)


def test_scan_date_no_file_returns_today():
    result = _parse_scan_date("NoDate.pdf")
    assert result == date.today()


def test_total_from_total_line():
    r = parse_receipt("20260401_0900.pdf", "Some Store\nItems...\nTOTAL $47.23")
    assert r.total == pytest.approx(47.23)


def test_total_from_amount_due():
    r = parse_receipt("20260401_0900.pdf", "Another Store\nAmount Due: 12.50")
    assert r.total == pytest.approx(12.50)


def test_receipt_datetime_parses_month_name_date_and_time():
    r = parse_receipt(
        "20260401_0900.pdf",
        "Anthropic\nDate of issue March 22, 2026\nRefund issued at 9:38 PM\nTotal credit $200.00",
    )
    assert r.receipt_date == date(2026, 3, 22)
    assert r.receipt_time is not None
    assert r.receipt_time.hour == 21
    assert r.receipt_time.minute == 38


def test_total_from_usd_line_with_single_decimal():
    r = parse_receipt("20260401_0900.pdf", "Vodafone\nTotal 13.2 USD")
    assert r.total == pytest.approx(13.2)


def test_total_none_when_missing():
    r = parse_receipt("20260401_0900.pdf", "Mystery Shop\nItem 1   5.00\nItem 2   3.00")
    assert r.total is None


def test_merchant_from_first_line():
    r = parse_receipt("20260401_0900.pdf", "Acme Supply Co\nSome item  9.99\nTOTAL $9.99")
    assert r.merchant == "acme supply co"


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


def test_payment_tender_total_beats_balance_line():
    text = "\n".join(
        [
            "Kroger",
            "Balance 86.05",
            "MASTERCARD 88.05",
            "Total Number Of Items Sold = 14",
        ]
    )
    r = parse_receipt("20260409_1707.pdf", text)
    assert r.total == pytest.approx(88.05)


def test_unpaid_notice_total_is_not_treated_as_spend():
    text = "\n".join(
        [
            "Violation Receipt",
            "Kalamazoo, MI",
            "Date: 04/09/2026 08:29 am Payment Type: N/A Status: Unpaid",
            "Total Amount Paid: $0.00",
            "Total Amount Due: $15.00",
        ]
    )
    r = parse_receipt("20260409_0829.pdf", text)
    assert r.total is None


def test_paid_notice_prefers_total_amount_paid_over_amount_due():
    text = "\n".join(
        [
            "Violation Receipt",
            "Kalamazoo, MI",
            "Date: 04/09/2026 08:32 am Payment Type: Web Status: Paid",
            "Total Amount Paid: $18.95",
            "Total Amount Due: $0.00",
        ]
    )
    r = parse_receipt("20260409_0832.pdf", text)
    assert r.total == pytest.approx(18.95)


def test_loyalty_points_total_ignored():
    text = "\n".join(
        [
            "Shop",
            "Subtotal: $10.00",
            "Tax: $0.60",
            "Loyalty Points Earned: 55.00",
            "Loyalty Points Total: 800.69",
        ]
    )
    r = parse_receipt("20260401_0900.pdf", text)
    assert r.total == pytest.approx(10.60)


def test_total_computed_from_subtotal_tax_and_discount():
    text = "\n".join(
        [
            "Moses Roses - Waterford REC",
            "Subtotal: $195.00",
            "Michigan Cannabis Excise Tax: $15.50",
            "Michigan State Tax: $10.23",
            "Total Tax: $25.73",
            "Total Discount: $39.99",
            "Payment (Cash): $200.00",
            "Due Customer: $19.00",
            "Loyalty Points Total: 800.69",
        ]
    )
    r = parse_receipt("20260307.pdf", text)
    assert r.total == pytest.approx(180.74)


def test_card_last4_from_visa():
    r = parse_receipt("20260401_0900.pdf", "Store\nVISA xxxx xxxx xxxx 4321\nTotal $5.00")
    assert r.card_last4 == "4321"


def test_card_last4_from_masked_pattern():
    r = parse_receipt("20260401_0900.pdf", "Store\nCard: XXXX-XXXX-XXXX-7890\nTotal $5.00")
    assert r.card_last4 == "7890"


def test_card_last4_absent_when_not_present():
    r = parse_receipt("20260401_0900.pdf", "Store\nTotal $5.00")
    assert r.card_last4 is None


def test_card_last4_from_discover_with_nearby_line_digits():
    text = "\n".join(
        [
            "W. BLOOMFIELD THNSHP WI",
            "DISCOVER CHIP READ",
            "Entry Type: CONTACT",
            "eeaxgeanvcecae 7875 gal ur",
            "TOTAL ANT: usd $37.10",
        ]
    )
    r = parse_receipt("20260213_1231.pdf", text)
    assert r.card_last4 == "7875"


def test_purchaser_extracted():
    r = parse_receipt("20260401_0900.pdf", "Store\nCardholder: JOHN DOE\nTotal $5.00")
    assert r.purchaser is not None
    assert "john" in r.purchaser


def test_purchaser_alias_normalized_by_ocr_parser():
    r = parse_receipt("20260401_0900.pdf", "Store\nCardholder: CASON\nTotal $5.00")
    assert r.purchaser == "cason konzer"


def test_purchaser_absent_when_not_present():
    r = parse_receipt("20260401_0900.pdf", "Store\nTotal $5.00")
    assert r.purchaser is None


def test_purchaser_blacklist_rejected_by_ocr_parser():
    """Blacklisted tokens like 'account' must not be stored as purchaser."""
    r = parse_receipt("20260401_0900.pdf", "Store\nCardholder: account\nTotal $5.00")
    assert r.purchaser is None


def test_purchaser_blacklist_case_insensitive_ocr():
    r = parse_receipt("20260401_0900.pdf", "Store\nCardholder: ACCOUNT\nTotal $5.00")
    assert r.purchaser is None


def test_customer_satisfaction_not_treated_as_purchaser():
    text = "Store\n~-customer satisfaction per randa S\nSubtotal: $10.00\nTax: $0.60"
    r = parse_receipt("20260401_0900.pdf", text)
    assert r.purchaser is None


def test_cardholder_signature_verified_not_treated_as_purchaser():
    text = "Store\nCardholder Signature Verified\nTotal $5.00"
    r = parse_receipt("20260401_0900.pdf", text)
    assert r.purchaser is None


def test_app_name_discover_not_treated_as_purchaser():
    text = "Store\nApp Name: Discover\nTotal $5.00"
    r = parse_receipt("20260401_0900.pdf", text)
    assert r.purchaser is None


def test_thank_you_footer_not_treated_as_purchaser():
    text = "Store\nCardholder: THANK at FOR SHOPPING\nTotal $5.00"
    r = parse_receipt("20260401_0900.pdf", text)
    assert r.purchaser is None


def test_counter_staff_name_not_treated_as_purchaser():
    text = "Store\nCounter #: 862305 Sal\nCardholder: Sam\nMC 5653 42.38\nTotal $42.38"
    r = parse_receipt("20260226_0730.pdf", text)
    assert r.purchaser is None
