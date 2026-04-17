"""Unit tests for intake.sidecar."""

import json
from pathlib import Path

import pytest

from intake.sidecar import (
    AI_DEBUG_SUFFIX,
    OCR_DEBUG_SUFFIX,
    SIDECAR_SUFFIX,
    ai_debug_path,
    has_sidecar,
    ocr_debug_path,
    read_sidecar,
    sidecar_path,
    write_debug_artifacts,
    write_sidecar,
)


@pytest.fixture()
def tmp_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")
    return pdf


def test_sidecar_path_suffix(tmp_pdf):
    assert sidecar_path(tmp_pdf).name == "20260401_1000.intake.json"


def test_has_sidecar_false_when_absent(tmp_pdf):
    assert not has_sidecar(tmp_pdf)


def test_write_and_read_sidecar(tmp_pdf):
    data = {
        "filename": tmp_pdf.name,
        "merchant": "Acme",
        "total": 9.99,
        "category": "general",
        "items": [{"description": "Widget", "amount": 9.99}],
    }
    path = write_sidecar(tmp_pdf, data)

    assert path.exists()
    assert path.name.endswith(SIDECAR_SUFFIX)
    assert has_sidecar(tmp_pdf)

    payload = read_sidecar(tmp_pdf)
    assert payload["merchant"] == "Acme"
    assert payload["total"] == 9.99
    assert payload["items"] == [{"description": "Widget", "amount": 9.99}]
    assert "processed_at" in payload


def test_write_sidecar_sets_processed_at(tmp_pdf):
    write_sidecar(tmp_pdf, {"filename": tmp_pdf.name})
    payload = json.loads(sidecar_path(tmp_pdf).read_text())
    assert payload["processed_at"].startswith("20")  # ISO timestamp


def test_read_sidecar_returns_none_when_absent(tmp_pdf):
    assert read_sidecar(tmp_pdf) is None


def test_write_sidecar_does_not_include_raw_text(tmp_pdf):
    """Callers filter out raw_text before passing to write_sidecar."""
    data = {"filename": tmp_pdf.name, "merchant": "Shop"}
    write_sidecar(tmp_pdf, data)
    payload = read_sidecar(tmp_pdf)
    assert "raw_text" not in payload


def test_debug_paths_suffixes(tmp_pdf):
    assert ocr_debug_path(tmp_pdf).name.endswith(OCR_DEBUG_SUFFIX)
    assert ai_debug_path(tmp_pdf).name.endswith(AI_DEBUG_SUFFIX)


def test_write_debug_artifacts(tmp_pdf):
    ocr_path, ai_path = write_debug_artifacts(
        tmp_pdf,
        raw_text="Store\nTOTAL 10.00",
        processed_by="gpt-4o-mini",
        tier="low",
        ai_attempts=[{"model": "gpt-4o-mini", "response": {"merchant": "store"}}],
    )

    assert ocr_path.read_text(encoding="utf-8") == "Store\nTOTAL 10.00"
    payload = json.loads(ai_path.read_text(encoding="utf-8"))
    assert payload["processed_by"] == "gpt-4o-mini"
    assert payload["tier"] == "low"
    assert payload["used_ai"] is True
    assert payload["attempts"][0]["model"] == "gpt-4o-mini"
