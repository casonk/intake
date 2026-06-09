"""Unit tests for intake.language."""

from intake.language import detect_receipt_locale


def test_detect_receipt_locale_german_terms():
    hint = detect_receipt_locale(
        "\n".join(
            [
                "Bäckerei Müller",
                "Zwischensumme 3,50 EUR",
                "MwSt 0,56",
                "Gesamt 4,06",
                "Rückgeld 0,94",
            ]
        )
    )

    assert hint.language == "de"
    assert hint.currency is None
    assert hint.confidence >= 0.8


def test_detect_receipt_locale_french_terms():
    hint = detect_receipt_locale(
        "\n".join(
            [
                "Boulangerie du Marché",
                "Sous-total 8,20 €",
                "TVA 0,80",
                "Total TTC 9,00",
                "Espèces 10,00",
                "Rendu 1,00",
            ]
        )
    )

    assert hint.language == "fr"
    assert hint.currency is None
    assert hint.confidence >= 0.8
