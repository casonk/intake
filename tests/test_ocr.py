"""Unit tests for intake.ocr media dispatch."""

from __future__ import annotations

from intake import ocr


def test_extract_text_dispatches_to_image_ocr(monkeypatch, tmp_path):
    image = tmp_path / "receipt.JPG"
    image.write_bytes(b"image")

    monkeypatch.setattr(ocr, "_load_image", lambda path: ("image", path.name))
    monkeypatch.setattr(ocr, "_ocr_image", lambda image_obj: f"ocr:{image_obj[1]}")

    assert ocr.extract_text(image) == "ocr:receipt.JPG"


def test_undouble_glyph_runs_recovers_bold_pdf_labels():
    text = (
        "IInnvvooiiccee ##\n"
        "Example Inc. Jordan Avery 06.30.26\n"
        "TToottaall $0.50\n"
        "AAmmoouunntt dduuee:: $0.50\n"
    )

    fixed = ocr._undouble_glyph_runs(text)

    assert "Total $0.50" in fixed
    assert "Amount due: $0.50" in fixed
    # Untouched, non-doubled text stays exactly as-is.
    assert "Example Inc. Jordan Avery 06.30.26" in fixed


def test_undouble_glyph_runs_leaves_short_tokens_alone():
    # "AA" and "##" coincidentally satisfy the pairwise-duplicate check but
    # are below the minimum length, so they must not be collapsed.
    text = "12d AA\nInvoice ##"

    assert ocr._undouble_glyph_runs(text) == text


def test_extract_text_dispatches_to_video_thumbnail(monkeypatch, tmp_path):
    video = tmp_path / "receipt.MOV"
    video.write_bytes(b"video")

    monkeypatch.setattr(ocr, "_video_thumbnail_image", lambda path: ("video", path.name))
    monkeypatch.setattr(ocr, "_ocr_image", lambda image_obj: f"ocr:{image_obj[1]}")

    assert ocr.extract_text(video) == "ocr:receipt.MOV"
