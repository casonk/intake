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
from typing import Any

SIDECAR_SUFFIX = ".intake.json"
OCR_DEBUG_SUFFIX = ".ocr.txt"
AI_DEBUG_SUFFIX = ".ai.json"

MODEL_FIDELITY_WEIGHTS = {
    "tesseract": ("ocr", 0),
    "gpt-4o-mini": ("low", 1),
    "gpt-4o": ("mid", 2),
    "gpt-4.1": ("high", 3),
    "o4-mini": ("high", 3),
}


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

    written_at = datetime.now(UTC).isoformat()
    ai_path = ai_debug_path(pdf)
    existing_payload = _read_ai_debug_payload(ai_path)
    existing_attempts = _normalize_ai_attempts(existing_payload.get("attempts", []))
    new_attempts = _normalize_ai_attempts(ai_attempts or [], recorded_at=written_at)
    attempts = [*existing_attempts, *new_attempts]
    payload = {
        "filename": pdf.name,
        "processed_by": processed_by,
        "tier": tier,
        "used_ai": bool(attempts),
        "latest_used_ai": bool(new_attempts),
        "attempt_count": len(attempts),
        "latest_attempt_count": len(new_attempts),
        "attempts": attempts,
        "models": _summarize_models(attempts),
        "written_at": written_at,
    }
    ai_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return ocr_path, ai_path


def read_sidecar(pdf: Path) -> dict | None:
    """Return sidecar data for *pdf*, or None if the sidecar is absent."""
    p = sidecar_path(pdf)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _read_ai_debug_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_ai_attempts(
    attempts: list[dict] | tuple[dict, ...] | Any,
    *,
    recorded_at: str | None = None,
) -> list[dict]:
    if not isinstance(attempts, list | tuple):
        return []
    normalized: list[dict] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        item = dict(attempt)
        model = str(item.get("model") or "")
        fidelity, weight = _model_fidelity_weight(model)
        item["model"] = model
        item["fidelity"] = item.get("fidelity") or fidelity
        item["weight"] = int(item.get("weight") if item.get("weight") is not None else weight)
        if recorded_at and "recorded_at" not in item:
            item["recorded_at"] = recorded_at
        normalized.append(item)
    return normalized


def _model_fidelity_weight(model: str) -> tuple[str, int]:
    if model in MODEL_FIDELITY_WEIGHTS:
        return MODEL_FIDELITY_WEIGHTS[model]
    lowered = model.lower()
    if "mini" in lowered:
        return "low", 1
    if any(token in lowered for token in ("4.1", "o4", "high")):
        return "high", 3
    if any(token in lowered for token in ("4o", "mid")):
        return "mid", 2
    return "unknown", -1


def _summarize_models(attempts: list[dict]) -> list[dict]:
    by_model: dict[str, dict] = {}
    for attempt in attempts:
        model = str(attempt.get("model") or "")
        if not model:
            continue
        fidelity, weight = _model_fidelity_weight(model)
        summary = by_model.setdefault(
            model,
            {
                "model": model,
                "fidelity": str(attempt.get("fidelity") or fidelity),
                "weight": int(attempt.get("weight") or weight),
                "attempt_count": 0,
                "latest_attempt_at": None,
            },
        )
        summary["attempt_count"] += 1
        recorded_at = attempt.get("recorded_at")
        if isinstance(recorded_at, str) and (
            summary["latest_attempt_at"] is None or recorded_at > summary["latest_attempt_at"]
        ):
            summary["latest_attempt_at"] = recorded_at
    return sorted(by_model.values(), key=lambda item: (-item["weight"], item["model"]))
