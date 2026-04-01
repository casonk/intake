"""High-level receipt extraction: OCR → parse → quality check → optional AI fallback.

This module is the single entry-point for turning a PDF into a Receipt.
It returns both the Receipt and the name of the model that produced it,
allowing callers to store that provenance and make model-aware skip decisions
on future re-runs (e.g. backfill only receipts processed with an older model).

Extraction tiers
----------------
ok    → Tesseract output is reliable; no AI call needed.
low   → Quality heuristics failed (short text, missing fields, high noise);
        use the low-fidelity model (default: gpt-4o-mini).
high  → Merchant name matches an escalation pattern (date fragment, page
        number, etc.) indicating OCR produced junk for the first line;
        use the high-fidelity model (default: gpt-4o).
"""

from __future__ import annotations

import re
from pathlib import Path

from intake.ocr import extract_text
from intake.parse import Receipt, parse_receipt

OCR_MODEL = "tesseract"

_MODEL_RANK: dict[str, int] = {
    "tesseract": 0,
    "gpt-4o-mini": 1,
    "gpt-4o": 2,
    "gpt-4.1": 3,
    "o4-mini": 3,
}

# Patterns that, when matched against the extracted merchant name, force
# escalation to the high-fidelity model.
DEFAULT_ESCALATION_PATTERNS: list[str] = [
    r"\d{1,2}[/\-]\d{1,2}",    # date fragment: "03/15" or "3-15"
    r"\bpages?\s*\.?\s*\d+\b",  # "page 1", "pages 2"
    r"\bpg\.?\s*\d+\b",         # "pg. 1"
    r"#\d{3,}",                  # receipt/order number in name: "#00123"
    r"^\d+$",                    # name is entirely digits
    r"^.{0,2}$",                 # name too short (0–2 chars)
]


def model_rank(model: str) -> int:
    """Return a numeric capability rank for *model* (higher is better)."""
    return _MODEL_RANK.get(model, -1)


def should_reprocess(sidecar_model: str, current_model: str) -> bool:
    """Return True if the receipt should be reprocessed with *current_model*.

    Reprocessing is warranted when the current model is strictly more capable
    than the one that produced the existing sidecar.
    """
    return model_rank(current_model) > model_rank(sidecar_model)


# ── quality / escalation heuristics ──────────────────────────────────────────


def _ocr_quality_ok(text: str, receipt: Receipt) -> bool:
    """Return True if OCR output looks reliable enough without AI help."""
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


def _merchant_escalates(merchant: str, patterns: list[re.Pattern]) -> bool:
    """Return True if *merchant* matches any compiled escalation pattern."""
    m = merchant.strip()
    return any(p.search(m) for p in patterns)


def _compile_patterns(raw: list[str]) -> list[re.Pattern]:
    compiled = []
    for p in raw:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error:
            pass
    return compiled


def _extraction_tier(
    text: str,
    receipt: Receipt,
    escalation_compiled: list[re.Pattern],
) -> str:
    """Return ``'ok'``, ``'low'``, or ``'high'`` for the extraction tier needed."""
    if _merchant_escalates(receipt.merchant, escalation_compiled):
        return "high"
    if not _ocr_quality_ok(text, receipt):
        return "low"
    return "ok"


# ── public API ────────────────────────────────────────────────────────────────


def extract_receipt(
    pdf_path: Path,
    openai_api_key: str | None = None,
    openai_model_low: str = "gpt-4o-mini",
    openai_model_high: str = "gpt-4o",
    escalation_patterns: list[str] | None = None,
    fuzzy_rules: dict | None = None,
    fuzzy_threshold: float = 0.82,
) -> tuple[Receipt, str]:
    """Extract a Receipt from *pdf_path*, with optional tiered AI fallback.

    Returns ``(receipt, model_name)`` where *model_name* identifies the
    extraction pipeline used (``"tesseract"``, ``openai_model_low``, or
    ``openai_model_high``).

    Pipeline
    --------
    1. Tesseract OCR + parse
    2. Fuzzy-correct merchant name against *fuzzy_rules* keywords
    3. Determine extraction tier (ok / low / high)
    4. If tier != ok and *openai_api_key* set → call AI with appropriate model
    5. Merge AI result; degrade gracefully on any API error
    """
    raw_text = extract_text(pdf_path)
    receipt = parse_receipt(pdf_path.name, raw_text)

    # Fuzzy-correct garbled merchant name
    if fuzzy_rules:
        from intake.merchant import build_keyword_list, fuzzy_correct
        keywords = build_keyword_list(fuzzy_rules)
        corrected = fuzzy_correct(receipt.merchant, keywords, fuzzy_threshold)
        if corrected != receipt.merchant:
            from dataclasses import replace
            receipt = replace(receipt, merchant=corrected)

    if not openai_api_key:
        return receipt, OCR_MODEL

    patterns = _compile_patterns(
        escalation_patterns if escalation_patterns is not None else DEFAULT_ESCALATION_PATTERNS
    )
    tier = _extraction_tier(raw_text, receipt, patterns)

    if tier == "ok":
        return receipt, OCR_MODEL

    model = openai_model_high if tier == "high" else openai_model_low

    try:
        from intake.ai import extract_with_openai, merge_ai_result
        ai_data = extract_with_openai(pdf_path, raw_text, openai_api_key, model)
        receipt = merge_ai_result(receipt, ai_data)
        return receipt, model
    except Exception:  # noqa: BLE001
        return receipt, OCR_MODEL

