from __future__ import annotations

from datetime import date

from intake.document_type import (
    classify_document_type,
    is_identity_document_text,
    is_reference_document_text,
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
