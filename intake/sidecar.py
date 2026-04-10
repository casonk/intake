"""Sidecar JSON files written alongside processed receipt PDFs.

Each processed PDF gets a companion ``<stem>.intake.json`` file recording
key extraction results.  The presence of a sidecar is the canonical
filesystem signal that a PDF has already been ingested — letting
``intake backfill`` skip it without requiring a live database connection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

SIDECAR_SUFFIX = ".intake.json"
OCR_DEBUG_SUFFIX = ".ocr.txt"
AI_DEBUG_SUFFIX = ".ai.json"


def sidecar_path(pdf: Path) -> Path:
    """Return the expected sidecar path for *pdf*."""
    return pdf.with_suffix(SIDECAR_SUFFIX)


def ocr_debug_path(pdf: Path) -> Path:
    """Return the OCR debug artifact path for *pdf*."""
    return pdf.with_suffix(OCR_DEBUG_SUFFIX)


def ai_debug_path(pdf: Path) -> Path:
    """Return the AI debug artifact path for *pdf*."""
    return pdf.with_suffix(AI_DEBUG_SUFFIX)


def has_sidecar(pdf: Path) -> bool:
    """Return True if *pdf* already has a companion sidecar file."""
    return sidecar_path(pdf).exists()


def write_sidecar(pdf: Path, data: dict) -> Path:
    """Write a sidecar JSON next to *pdf* and return its path.

    *data* should contain the fields stored in the DB row (excluding raw_text).
    ``processed_at`` is set automatically to now (UTC).
    """
    payload = {**data, "processed_at": datetime.now(UTC).isoformat()}
    path = sidecar_path(pdf)
    path.write_text(json.dumps(payload, indent=2))
    return path


def write_debug_artifacts(
    pdf: Path,
    *,
    raw_text: str,
    processed_by: str,
    tier: str,
    ai_attempts: list[dict] | None = None,
) -> tuple[Path, Path]:
    """Write OCR and AI debug artifacts next to *pdf*."""
    ocr_path = ocr_debug_path(pdf)
    ocr_path.write_text(raw_text or "", encoding="utf-8")

    payload = {
        "filename": pdf.name,
        "processed_by": processed_by,
        "tier": tier,
        "used_ai": bool(ai_attempts),
        "attempts": ai_attempts or [],
        "written_at": datetime.now(UTC).isoformat(),
    }
    ai_path = ai_debug_path(pdf)
    ai_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return ocr_path, ai_path


def read_sidecar(pdf: Path) -> dict | None:
    """Return sidecar data for *pdf*, or None if the sidecar is absent."""
    p = sidecar_path(pdf)
    if not p.exists():
        return None
    return json.loads(p.read_text())
