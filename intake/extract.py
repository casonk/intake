"""High-level receipt extraction: OCR → parse → quality check → optional AI fallback.

This module is the single entry-point for turning a PDF into a Receipt.
It returns both the Receipt and the name of the model that produced it,
allowing callers to store that provenance and make model-aware skip decisions
on future re-runs (e.g. backfill only receipts processed with an older model).
"""

from __future__ import annotations

from pathlib import Path

from intake.ocr import extract_text
from intake.parse import Receipt, parse_receipt

# Canonical name for the local Tesseract pipeline
OCR_MODEL = "tesseract"

# Higher rank = more capable / preferred model.
# Any model not listed here gets rank -1 (unknown / legacy).
_MODEL_RANK: dict[str, int] = {
    "tesseract": 0,
    "gpt-4o-mini": 1,
    "gpt-4o": 2,
}


def model_rank(model: str) -> int:
    """Return a numeric capability rank for *model* (higher is better)."""
    return _MODEL_RANK.get(model, -1)


def should_reprocess(sidecar_model: str, current_model: str) -> bool:
    """Return True if the receipt should be reprocessed with *current_model*.

    Reprocessing is warranted when the current model is strictly more capable
    than the one that produced the existing sidecar.
    """
    return model_rank(current_model) > model_rank(sidecar_model)


# ── quality heuristics ────────────────────────────────────────────────────────


def _ocr_quality_ok(text: str, receipt: Receipt) -> bool:
    """Return True if OCR output looks reliable enough to use without AI help.

    Criteria (any failure → low quality):
    - Fewer than 100 non-whitespace characters extracted
    - Merchant could not be identified (fell back to "Unknown")
    - Total could not be parsed
    - More than 10 % of characters are non-printable / non-ASCII noise
    """
    if len(text.strip()) < 100:
        return False
    if receipt.merchant == "Unknown":
        return False
    if receipt.total is None:
        return False
    noise = sum(
        1 for c in text
        if ord(c) > 127 or (not c.isprintable() and c not in "\n\t ")
    )
    if noise / max(len(text), 1) > 0.10:
        return False
    return True


# ── public API ────────────────────────────────────────────────────────────────


def extract_receipt(
    pdf_path: Path,
    openai_api_key: str | None = None,
    openai_model: str = "gpt-4o-mini",
) -> tuple[Receipt, str]:
    """Extract a Receipt from *pdf_path*, with optional AI fallback.

    Returns ``(receipt, model_name)`` where *model_name* is either
    ``OCR_MODEL`` (``"tesseract"``) or the OpenAI model string used.

    AI fallback is attempted when:
    - ``openai_api_key`` is provided, AND
    - OCR quality heuristics flag the output as low-confidence

    If the AI call itself fails, the function degrades gracefully and
    returns the best Tesseract result with ``model_name = "tesseract"``.
    """
    raw_text = extract_text(pdf_path)
    receipt = parse_receipt(pdf_path.name, raw_text)

    if _ocr_quality_ok(raw_text, receipt) or not openai_api_key:
        return receipt, OCR_MODEL

    try:
        from intake.ai import extract_with_openai, merge_ai_result

        ai_data = extract_with_openai(pdf_path, raw_text, openai_api_key, openai_model)
        receipt = merge_ai_result(receipt, ai_data)
        return receipt, openai_model
    except Exception:  # noqa: BLE001 — degrade gracefully, never crash the pipeline
        return receipt, OCR_MODEL
