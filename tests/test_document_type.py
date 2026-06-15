from __future__ import annotations

from datetime import date

from intake.document_type import classify_document_type, is_identity_document_text
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
