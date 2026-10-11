from __future__ import annotations

from datetime import date

from intake.document_type import (
    classify_document_type,
    is_check_payable_to_known_purchaser,
    is_identity_document_text,
    is_reference_document_text,
    is_tax_wage_document_text,
)
from intake.parse_document import classify_document, parse_document
from intake.parse_letter import Letter, classify_letter


def test_japan_residence_card_routes_as_letter() -> None:
    raw_text = """
    MINISTRY OF JUSTICE
    RESIDENCE CARD
    NATIONALITY/REGION USA
    PERIOD OF STAY 1 YEAR
    """

    assert is_identity_document_text(raw_text)
    assert classify_document_type(raw_text, "20260615_0715.pdf") == "letter"


def test_japan_identity_documents_route_to_legal_identity_letters() -> None:
    letter = Letter(
        filename="20260615_0716.pdf",
        scan_date=date(2026, 6, 15),
        letter_date=None,
        sender=None,
        recipient=None,
        subject=None,
        salutation=None,
        body_excerpt="",
        raw_text="個人番号カード 住民票 横浜市長",
    )

    assert classify_letter(letter) == "letters/legal/identity"


def test_identity_guard_does_not_catch_generic_records_letters() -> None:
    letter = Letter(
        filename="20260615_0900.pdf",
        scan_date=date(2026, 6, 15),
        letter_date=None,
        sender=None,
        recipient=None,
        subject=None,
        salutation=None,
        body_excerpt="",
        raw_text="Dear resident, please update your account records.",
    )

    assert not is_identity_document_text(letter.raw_text)
    assert classify_letter(letter) == "letters/financial"


def test_user_manual_routes_as_reference_document() -> None:
    raw_text = """
    Acme Router XR500
    User Manual
    Model: XR500
    Important safety instructions
    """

    assert is_reference_document_text(raw_text)
    assert classify_document_type(raw_text, "20260616_1000.pdf") == "document"
    assert classify_document(raw_text) == "documents/manuals"


def test_good_luck_card_routes_as_card_not_receipt() -> None:
    raw_text = """
    Jordan - all the best on your new role overseas.
    Good Luck
    Because you ARE great!
    Best of luck, Joe
    """

    assert classify_document_type(raw_text, "20260601_0749.pdf") == "card"


def test_w2_wage_statement_routes_as_tax_document_not_receipt() -> None:
    raw_text = """
    2025 W-2 and EARNINGS SUMMARY
    Wage and Tax Statement
    c Employer's name, address, and ZIP code
    ACME MANUFACTURING LLC
    123 EXAMPLE PKWY STE 100
    ANYTOWN, IL 00000
    b Employer's FED ID number
    00-0000000
    a Employee's SSA number
    XXX-XX-9628
    1 Wages, tips, other comp. 2 Federal income tax withheld
    89249.51 11224.73
    3 Social security wages 4 Social security tax withheld
    107829.91 6685.45
    5 Medicare wages and tips 6 Medicare tax withheld
    107829.91 1563.53
    """

    document = parse_document("20250101_0749.pdf", raw_text)

    assert is_tax_wage_document_text(raw_text)
    assert classify_document_type(raw_text, "20250101_0749.pdf") == "document"
    assert document.document_kind == "documents/tax"


def test_check_payable_to_known_purchaser_routes_as_income_document() -> None:
    raw_text = """
    JOHN Q TRUSTEE
    U/A DTD 01/01/2020
    SAMPLE FAMILY LIVING TR
    123 EXAMPLE ST
    ANYTOWN MI 00000-0000

    Pay to the order of Jane Sample $ 275.00
    Two hundred seventy-five and 00/100 Dollars
    Payable at:
    SomeBroker
    """

    document = parse_document("20260601_0730.pdf", raw_text)

    assert is_check_payable_to_known_purchaser(raw_text)
    assert classify_document_type(raw_text, "20260601_0730.pdf") == "document"
    assert document.document_kind == "documents/income"


def test_check_payable_to_other_party_is_not_flagged_as_income() -> None:
    # A check the household WROTE to pay a contractor is spend, not income —
    # only checks payable TO a registered purchaser should be redirected.
    raw_text = "Pay to the order of ABC Plumbing LLC $ 400.00"

    assert not is_check_payable_to_known_purchaser(raw_text)


def test_hardware_specs_route_as_specs_document() -> None:
    raw_text = """
    Product Specification
    NVMe Controller ABC-12
    Technical Specifications
    Model Number: ABC-12
    """

    document = parse_document("20260616_1001.pdf", raw_text)

    assert classify_document_type(raw_text, "20260616_1001.pdf") == "document"
    assert document.document_kind == "documents/specs"
    assert document.model_number == "ABC-12"


def test_product_brief_routes_as_specs_document() -> None:
    raw_text = """
    9th Gen Intel Core Desktop Processors
    Product Brief
    Platform features and specifications
    """

    assert classify_document_type(raw_text, "20260616_1003.pdf") == "document"
    assert classify_document(raw_text) == "documents/specs"


def test_datasheet_with_warranty_period_stays_specs_document() -> None:
    raw_text = """
    Data Sheet
    Samsung SSD 870 EVO
    TECHNICAL SPECIFICATIONS
    Density Model Name Box Contents Model Code
    TBW 2,400TB
    Warranty Period 5 year limited
    Warranty Statement
    """

    document = parse_document("20260616_1004.pdf", raw_text)

    assert classify_document_type(raw_text, "20260616_1004.pdf") == "document"
    assert classify_document(raw_text) == "documents/specs"
    assert document.document_kind == "documents/specs"
    assert document.model_number is None


def test_warranty_routes_as_warranty_document() -> None:
    raw_text = """
    Limited Warranty
    Product: Shop Vacuum 20L
    Serial Number: SN-123456
    This warranty covers defects in materials for two years.
    """

    document = parse_document("20260616_1002.pdf", raw_text)

    assert classify_document_type(raw_text, "20260616_1002.pdf") == "document"
    assert document.document_kind == "documents/warranty"
    assert document.serial_number == "SN-123456"
