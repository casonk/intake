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


def sidecar_path(pdf: Path) -> Path:
    """Return the expected sidecar path for *pdf*."""
    return pdf.with_suffix(SIDECAR_SUFFIX)


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


def read_sidecar(pdf: Path) -> dict | None:
    """Return sidecar data for *pdf*, or None if the sidecar is absent."""
    p = sidecar_path(pdf)
    if not p.exists():
        return None
    return json.loads(p.read_text())
