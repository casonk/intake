"""Unit tests for intake.parse."""

from datetime import date

import pytest

from intake.parse import _parse_scan_date, parse_receipt, receipt_items_from_dicts


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


def test_merchant_strips_welcome_to_prefix():
    r = parse_receipt("20260506_2125.jpg", "Welcome To Loves#\nsaa US Hwy 16 E\nTOTAL $14.96")
    assert r.merchant == "loves"


def test_merchant_welcome_to_with_store_number_suffix():
    r = parse_receipt(
        "20260520_0932.jpg",
        "WELCOME TO ROSAUERS #16\n1, My nate is Elizabe\nTOTAL 36.75",
    )
    assert r.merchant == "rosauers #16"


def test_merchant_welcome_to_alone_falls_through_to_next_line():
    r = parse_receipt(
        "19990211_0930.jpg",
        "WELCOME TO\n[GHTS FUEL STOP\nHICD RIVER, OREGON\nTOTAL 12.00",
    )
    assert r.merchant == "ghts fuel stop"


def test_merchant_store_number_suffix_not_mistaken_for_amount_line():
    # A bare "#123" store-number tail shouldn't be treated as a price line
    # and skipped when looking for the merchant name.
    r = parse_receipt("20260401_0900.pdf", "ROSAUERS #16\nTOTAL 36.75")
    assert r.merchant == "rosauers #16"


def test_total_from_total_line():
    r = parse_receipt("20260401_0900.pdf", "Some Store\nItems...\nTOTAL $47.23")
    assert r.total == pytest.approx(47.23)


def test_total_prefers_final_total_over_hyphenated_sub_total():
    # "Sub-Total" (hyphenated) must be excluded the same as "Sub Total" /
    # "Subtotal" — it was previously winning the fallback pass whenever the
    # real Total line's amount got OCR-garbled.
    r = parse_receipt(
        "20260401_0900.pdf",
        "Store\nSub-Total 44.53\nSales Tax 2.67\nTotal 47.20",
    )
    assert r.total == pytest.approx(47.20)


def test_total_ignores_job_subtotal_lines_on_multi_job_invoice():
    # A per-job "JOB# N TOTAL" subtotal must never be mistaken for the
    # invoice's real grand total.
    r = parse_receipt(
        "20260401_0900.pdf",
        "Dealer\nJOB# 1 TOTAL 217.95\nJOB# 2 TOTAL 378.00\nTOTAL INVOICE $ 2583.55",
    )
    assert r.total == pytest.approx(2583.55)


def test_total_returns_none_rather_than_a_noisy_job_line_match():
    # When the real grand total's amount is unrecoverable (e.g. split from
    # its label by column-based PDF extraction), only JOB# subtotals remain
    # as candidates — returning None (flagged for review) beats silently
    # reporting a wrong job-line subtotal as the total.
    r = parse_receipt(
        "20260401_0900.pdf",
        "Dealer\nJOB# 1 TOTAL 217.95\nJOB# 2 TOTAL 378.00\nTOTAL INVOICE $\n2583.55",
    )
    assert r.total is None


def test_total_rejects_noisy_label_merged_onto_total_line():
    # OCR sometimes merges adjacent text onto the same line as a real total
    # label. Here "PARTS TOTAL CHARGE 1233087" is a garbled "PARTS TOTAL
    # CHARGE" line fused with the real total "1233.37" (decimal point lost) —
    # the amount regex's 6-digit cap leaves a stray "1" stuck in the label
    # ("...charge 1", amount "233087"), which must not be read as the total.
    r = parse_receipt(
        "20260401_0900.pdf",
        "Dealer\nSUB-TOTAL 1163.56\nPARTS TOTAL CHARGE 1233087\nTOTAL CHARGE\n1233.37",
    )
    assert r.total is None


def test_total_accepts_known_continuation_word_after_term():
    # "Total Invoice Amount" is a real, common phrasing — the extra trailing
    # word "amount" must not disqualify the match the way genuine noise would.
    r = parse_receipt(
        "20260401_0900.pdf",
        "Vendor\nSubtotal 1,600.00\nTotal Invoice Amount 1,716.01 USD",
    )
    assert r.total == pytest.approx(1716.01)


def test_total_rejects_sub_component_total_with_real_word_suffix():
    # "Total - Parts" / "Total - Labor" name a job's parts/labor sub-total on
    # multi-job repair invoices, not the grand total — even though "parts"
    # and "labor" are themselves perfectly ordinary trailing words.
    r = parse_receipt(
        "20260401_0900.pdf",
        "Dealer\nJOB# 1 TOTAL 217.95\nTOTAL - PARTS 28.95\nTOTAL - LABOR 189.00",
    )
    assert r.total is None


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


# ── currency detection ────────────────────────────────────────────────────────


def test_currency_defaults_to_usd():
    r = parse_receipt("20260401_0900.pdf", "Some Store\nTOTAL $47.23")
    assert r.currency == "USD"


# ISO-4217 code adjacent to amount
def test_currency_iso_code_after_amount():
    for code in ("CAD", "EUR", "JPY", "GBP", "AUD", "CHF", "MXN", "KRW"):
        r = parse_receipt("20260401_0900.pdf", f"Store\nTotal: 47.23 {code}")
        assert r.currency == code, f"expected {code}"


def test_currency_iso_code_before_amount():
    r = parse_receipt("20260401_0900.pdf", "Store\nGBP 99.50\nTotal 99.50")
    assert r.currency == "GBP"


# Unicode currency symbols
def test_currency_euro_symbol():
    r = parse_receipt("20260401_0900.pdf", "Store\n€47.23\nTotal 47.23")
    assert r.currency == "EUR"


def test_currency_pound_symbol():
    r = parse_receipt("20260401_0900.pdf", "Store\n£99.50\nTotal 99.50")
    assert r.currency == "GBP"


def test_currency_yen_symbol():
    r = parse_receipt("20260401_0900.pdf", "Store\n¥5000\nTotal 5000")
    assert r.currency == "JPY"


def test_currency_rupee_symbol():
    r = parse_receipt("20260401_0900.pdf", "Store\n₹1500.00\nTotal 1500.00")
    assert r.currency == "INR"


def test_currency_won_symbol():
    r = parse_receipt("20260401_0900.pdf", "Store\n₩50000\nTotal 50000")
    assert r.currency == "KRW"


def test_currency_yuan_character():
    r = parse_receipt("20260401_0900.pdf", "店铺\n元200.00\nTotal 200.00")
    assert r.currency == "CNY"


# Non-ISO text aliases
def test_currency_cdn_dollar_alias():
    r = parse_receipt("20260401_0900.pdf", "Store\nCDN$ 22.50\nTotal 22.50")
    assert r.currency == "CAD"


def test_currency_ca_dollar_alias():
    r = parse_receipt("20260401_0900.pdf", "Store\nCA$ 15.00\nTotal 15.00")
    assert r.currency == "CAD"


def test_currency_canadian_dollar_text():
    r = parse_receipt("20260401_0900.pdf", "Store\nAll prices in Canadian dollar\nTotal 15.00")
    assert r.currency == "CAD"


def test_currency_euro_word():
    r = parse_receipt("20260401_0900.pdf", "Store\nTotal in Euro: 47.23")
    assert r.currency == "EUR"


def test_currency_yen_word():
    r = parse_receipt("20260401_0900.pdf", "Store\nTotal in Yen: 5000")
    assert r.currency == "JPY"


def test_currency_sterling_word():
    r = parse_receipt("20260401_0900.pdf", "Store\nPrices in Sterling\nTotal 99.50")
    assert r.currency == "GBP"


# Explicit Currency: label
def test_currency_explicit_label():
    r = parse_receipt("20260401_0900.pdf", "Store\nCurrency: AUD\nTotal: 88.00")
    assert r.currency == "AUD"


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


def test_masked_card_last4_is_not_payment_tender_total():
    text = "\n".join(
        [
            "Rogers Tavern",
            "Subtotal $8.00",
            "GST $0.40",
            "LCT $0.80",
            "Tip $1.84",
            "Total $11.04",
            "MASTERCARD XXXXXXXX5653",
        ]
    )
    r = parse_receipt("20260413_0547.jpg", text)
    assert r.total == pytest.approx(11.04)


def test_ocr_noisy_total_label_beats_masked_card_fragment():
    text = "\n".join(
        [
            "Rogers Tavern",
            "Subtotal $8.00",
            "GST $0.40",
            "LCT $0.80",
            "Tip $1.84",
            "Tota| $11.04",
            "MASTERCARD XXXXXXXXHH53",
        ]
    )
    r = parse_receipt("20260413_0548.jpg", text)
    assert r.total == pytest.approx(11.04)


def test_masked_card_ocr_fragment_does_not_override_clean_total():
    text = "\n".join(
        [
            "Rogers Tavern",
            "subtota | $10.00",
            "GST $0.50",
            "LCT $1.00",
            "Tip $2.30",
            "Total $13.80",
            "Mastercard XXXXXXXXH9BH3",
        ]
    )
    r = parse_receipt("20260416_0929.jpg", text)
    assert r.total == pytest.approx(13.80)


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


def test_japanese_receipt_keeps_merchant_and_prefers_total_over_cash_tender():
    text = "\n".join(
        [
            "ローソン",
            "合計 ￥1,280",
            "お預り 2000",
            "お釣り 720",
        ]
    )
    r = parse_receipt("20260401_0900.pdf", text)
    assert r.merchant == "ローソン"
    assert r.total == pytest.approx(1280.0)
    assert r.currency == "JPY"
    assert r.language == "ja"


def test_german_receipt_detected_as_german_and_parses_total():
    text = "\n".join(
        [
            "Bäckerei Müller",
            "Zwischensumme 3,50 EUR",
            "MwSt 0,56",
            "Gesamt 4,06",
            "Rückgeld 0,94",
        ]
    )
    r = parse_receipt("20260401_0910.pdf", text)
    assert r.merchant == "bäckerei müller"
    assert r.total == pytest.approx(4.06)
    assert r.currency == "EUR"
    assert r.language == "de"


def test_french_receipt_detected_as_french_and_parses_total():
    text = "\n".join(
        [
            "Boulangerie du Marché",
            "Sous-total 8,20 €",
            "TVA 0,80",
            "Total TTC 9,00",
            "Espèces 10,00",
            "Rendu 1,00",
        ]
    )
    r = parse_receipt("20260401_0915.pdf", text)
    assert r.merchant == "boulangerie du marché"
    assert r.total == pytest.approx(9.0)
    assert r.currency == "EUR"
    assert r.language == "fr"


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


def test_itemization_extracts_simple_line_items():
    text = "\n".join(
        [
            "Kroger",
            "Bananas 2.99",
            "Whole Milk 4.39",
            "Total 7.38",
        ]
    )
    r = parse_receipt("20260401_0900.pdf", text)
    assert [item.description for item in r.items] == ["Bananas", "Whole Milk"]
    assert [item.amount for item in r.items] == [pytest.approx(2.99), pytest.approx(4.39)]


def test_itemization_extracts_quantity_and_unit_price_lines():
    text = "\n".join(
        [
            "Hardware Store",
            "Bolts 3 @ 1.50 4.50",
            "2 x Washers 1.20",
            "Total 5.70",
        ]
    )
    r = parse_receipt("20260401_0900.pdf", text)
    assert len(r.items) == 2
    assert r.items[0].description == "Bolts"
    assert r.items[0].quantity == pytest.approx(3.0)
    assert r.items[0].unit_price == pytest.approx(1.5)
    assert r.items[0].amount == pytest.approx(4.5)
    assert r.items[1].description == "Washers"
    assert r.items[1].quantity == pytest.approx(2.0)
    assert r.items[1].amount == pytest.approx(1.2)


def test_itemization_ignores_total_and_tender_lines():
    text = "\n".join(
        [
            "Shop",
            "Apples 3.25",
            "MASTERCARD 3.25",
            "Card Number: ************5653",
            "Total 3.25",
        ]
    )
    r = parse_receipt("20260401_0900.pdf", text)
    assert len(r.items) == 1
    assert r.items[0].description == "Apples"


def test_itemization_preserves_ai_negative_adjustments():
    items = receipt_items_from_dicts(
        [
            {"description": "Bottle Refund", "amount": -1.50},
            {"description": "Change", "amount": 0.00},
        ]
    )
    assert len(items) == 1
    assert items[0].description == "Bottle Refund"
    assert items[0].amount == pytest.approx(-1.50)


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


def test_card_last4_from_ocr_noisy_card_number_line():
    text = "\n".join(
        [
            "Boyne Mountain Resort",
            "Total Due: 25.00",
            "MasterCard 25.00",
            "Card Number: XXXXXXXXKXXKKKAKAXKK S653",
        ]
    )
    r = parse_receipt("20260329_1155.pdf", text)
    assert r.card_last4 == "5653"


def test_purchaser_extracted():
    r = parse_receipt("20260401_0900.pdf", "Store\nCardholder: JOHN DOE\nTotal $5.00")
    assert r.purchaser is not None
    assert "john" in r.purchaser


def test_purchaser_alias_normalized_by_ocr_parser():
    r = parse_receipt("20260401_0900.pdf", "Store\nCardholder: JANE\nTotal $5.00")
    assert r.purchaser == "jane sample"


def test_purchaser_extracted_from_bill_to_email_block():
    text = "\n".join(
        [
            "Credit Note",
            "Anthropic, PBC Bill to",
            "548 Market Street janesample@gmail.com's Organization",
            "PMB 90375 4350 Bald Eagle Lake Road",
            "support@anthropic.com janesample@gmail.com",
            "Refund issued - Visa - 9369 $200.00",
        ]
    )
    r = parse_receipt("20260322_2241.pdf", text)
    assert r.purchaser == "jane sample"


def test_purchaser_extracted_from_rewards_greeting():
    # McDonald's app receipts print a loyalty greeting rather than a
    # cardholder/customer-name label: "Thank You Rewards Customer\nHi jordan".
    text = "McDonald's Restaurant #10001\nThank You Rewards Customer\nHi jordan\nTotal $5.00"
    r = parse_receipt("20260505_2054.jpg", text)
    assert r.purchaser == "jordan"


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


def test_member_savings_footer_not_treated_as_purchaser():
    # A loyalty-program "Member Savings" footer line matched the generic
    # cardholder/member regex and got captured as a name.
    text = "Safeway\nMember Savings\nTotal $5.00"
    r = parse_receipt("20260401_0900.pdf", text)
    assert r.purchaser is None


def test_member_sign_in_prompt_not_treated_as_purchaser():
    text = "Experian\nMember Sign In\nSee your credit report"
    r = parse_receipt("20260401_0900.pdf", text)
    assert r.purchaser is None


def test_counter_staff_name_not_treated_as_purchaser():
    text = "Store\nCounter #: 862305 Sal\nCardholder: Sam\nMC 5653 42.38\nTotal $42.38"
    r = parse_receipt("20260226_0730.pdf", text)
    assert r.purchaser is None


def test_staff_is_extracted_from_receipt_text():
    r = parse_receipt(
        "20260421_1842.jpg",
        "Smokin Outlaws BBQ\nServer: Isabel J\nTotal 9.20",
    )

    assert [staff.role for staff in r.staff] == ["server"]
    assert [staff.name for staff in r.staff] == ["isabel j"]


def test_two_word_staff_name_not_treated_as_purchaser():
    r = parse_receipt(
        "20260418_2158.jpg",
        "Rogers Tavern\nserver: Patricia W\nName: Patricia W\nTotal 13.80",
    )

    assert r.purchaser is None
    assert r.staff[0].name == "patricia w"


def test_certification_extracted_from_aaa_line():
    text = "\n".join(
        [
            "Heritage Collision",
            "Certified by AAA",
            "Customer: Michael J. Harrington Jr.",
        ]
    )
    r = parse_receipt("20110830_1149.pdf", text)
    assert len(r.certifications) == 1
    assert r.certifications[0].issuer == "AAA"
    assert r.certifications[0].label == "AAA certified"
    assert r.certifications[0].source_text == "Certified by AAA"
