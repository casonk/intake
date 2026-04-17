"""Unit tests for intake.ocr media dispatch."""

from __future__ import annotations

from intake import ocr


def test_extract_text_dispatches_to_image_ocr(monkeypatch, tmp_path):
    image = tmp_path / "receipt.JPG"
    image.write_bytes(b"image")

    monkeypatch.setattr(ocr, "_load_image", lambda path: ("image", path.name))
    monkeypatch.setattr(ocr, "_ocr_image", lambda image_obj: f"ocr:{image_obj[1]}")

    assert ocr.extract_text(image) == "ocr:receipt.JPG"


def test_extract_text_dispatches_to_video_thumbnail(monkeypatch, tmp_path):
    video = tmp_path / "receipt.MOV"
    video.write_bytes(b"video")

    monkeypatch.setattr(ocr, "_video_thumbnail_image", lambda path: ("video", path.name))
    monkeypatch.setattr(ocr, "_ocr_image", lambda image_obj: f"ocr:{image_obj[1]}")

    assert ocr.extract_text(video) == "ocr:receipt.MOV"
