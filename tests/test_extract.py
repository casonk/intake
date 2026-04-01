"""Unit tests for intake.extract (quality check, model rank, extract_receipt)."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch


from intake.extract import (
    DEFAULT_ESCALATION_PATTERNS,
    OCR_MODEL,
    _compile_patterns,
    _extraction_tier,
    _merchant_escalates,
    _ocr_quality_ok,
    extract_receipt,
    model_rank,
    should_reprocess,
)
from intake.parse import Receipt


# ── helpers ───────────────────────────────────────────────────────────────────


def _receipt(merchant="Acme Store", total=9.99, raw_text="x" * 200) -> Receipt:
    return Receipt(
        filename="20260401_1000.pdf",
        scan_date=date(2026, 4, 1),
        receipt_date=date(2026, 4, 1),
        receipt_time=None,
        merchant=merchant,
        total=total,
        raw_text=raw_text,
    )


# ── model rank ────────────────────────────────────────────────────────────────


def test_model_rank_ordering():
    assert model_rank("tesseract") < model_rank("gpt-4o-mini") < model_rank("gpt-4o")


def test_model_rank_unknown_is_negative():
    assert model_rank("some-future-model") == -1


def test_should_reprocess_when_better_model():
    assert should_reprocess("tesseract", "gpt-4o-mini") is True
    assert should_reprocess("gpt-4o-mini", "gpt-4o") is True


def test_should_not_reprocess_same_or_lower_model():
    assert should_reprocess("tesseract", "tesseract") is False
    assert should_reprocess("gpt-4o-mini", "tesseract") is False
    assert should_reprocess("gpt-4o", "gpt-4o") is False


# ── OCR quality ───────────────────────────────────────────────────────────────


def test_quality_ok_for_good_receipt():
    r = _receipt()
    assert _ocr_quality_ok("A" * 200, r) is True


def test_quality_fails_short_text():
    r = _receipt()
    assert _ocr_quality_ok("short", r) is False


def test_quality_fails_unknown_merchant():
    r = _receipt(merchant="unknown")
    assert _ocr_quality_ok("A" * 200, r) is False


def test_quality_fails_missing_total():
    r = _receipt(total=None)
    assert _ocr_quality_ok("A" * 200, r) is False


def test_quality_fails_high_noise():
    noisy = "\xff\xfe\x80" * 100  # lots of non-ASCII
    r = _receipt()
    assert _ocr_quality_ok(noisy, r) is False


# ── extract_receipt ───────────────────────────────────────────────────────────


def _fake_good_text():
    return "Acme Store\nItem 1   $5.00\nItem 2   $4.99\nTotal $9.99\n" * 5


@patch("intake.extract.extract_text")
@patch("intake.extract.parse_receipt")
def test_extract_receipt_returns_tesseract_when_quality_ok(
    mock_parse, mock_ocr, tmp_path
):
    good_text = _fake_good_text()
    mock_ocr.return_value = good_text
    mock_parse.return_value = _receipt(raw_text=good_text)

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    receipt, model, tier = extract_receipt(pdf)
    assert model == OCR_MODEL


@patch("intake.extract.extract_text")
@patch("intake.extract.parse_receipt")
def test_extract_receipt_no_ai_fallback_without_key(mock_parse, mock_ocr, tmp_path):
    mock_ocr.return_value = "bad"
    mock_parse.return_value = _receipt(merchant="unknown", total=None, raw_text="bad")

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    receipt, model, tier = extract_receipt(pdf, openai_api_key=None)
    assert model == OCR_MODEL


@patch("intake.extract.extract_text")
@patch("intake.extract.parse_receipt")
@patch("intake.ai.merge_ai_result")
@patch("intake.ai.extract_with_openai")
def test_extract_receipt_uses_ai_when_quality_low(
    mock_ai, mock_merge, mock_parse, mock_ocr, tmp_path
):
    mock_ocr.return_value = "bad"
    base_receipt = _receipt(merchant="unknown", total=None, raw_text="bad")
    improved = _receipt(merchant="Real Store", total=12.50, raw_text="bad")
    mock_parse.return_value = base_receipt
    mock_ai.return_value = {"merchant": "Real Store", "total": 12.50}
    mock_merge.return_value = improved

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    receipt, model, tier = extract_receipt(
        pdf, openai_api_key="sk-test", openai_model_low="gpt-4o-mini"
    )
    assert model == "gpt-4o-mini"
    assert receipt.merchant == "Real Store"


@patch("intake.extract.extract_text")
@patch("intake.extract.parse_receipt")
@patch("intake.ai.extract_with_openai", side_effect=Exception("API error"))
def test_extract_receipt_degrades_gracefully_on_ai_failure(
    mock_ai, mock_parse, mock_ocr, tmp_path
):
    mock_ocr.return_value = "bad"
    mock_parse.return_value = _receipt(merchant="unknown", total=None, raw_text="bad")

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    receipt, model, tier = extract_receipt(pdf, openai_api_key="sk-test")
    assert model == OCR_MODEL  # fell back gracefully


# ── escalation tier ───────────────────────────────────────────────────────────


def test_merchant_escalates_on_date_fragment():
    patterns = _compile_patterns(DEFAULT_ESCALATION_PATTERNS)
    assert _merchant_escalates("03/15 Receipt", patterns) is True


def test_merchant_escalates_on_page_number():
    patterns = _compile_patterns(DEFAULT_ESCALATION_PATTERNS)
    assert _merchant_escalates("Page 1 of 3", patterns) is True


def test_merchant_escalates_on_receipt_number():
    patterns = _compile_patterns(DEFAULT_ESCALATION_PATTERNS)
    assert _merchant_escalates("#00123", patterns) is True


def test_merchant_does_not_escalate_clean_name():
    patterns = _compile_patterns(DEFAULT_ESCALATION_PATTERNS)
    assert _merchant_escalates("Kroger", patterns) is False


def test_extraction_tier_ok_for_good_receipt():
    patterns = _compile_patterns(DEFAULT_ESCALATION_PATTERNS)
    r = _receipt()
    assert _extraction_tier(_fake_good_text(), r, patterns) == "ok"


def test_extraction_tier_high_for_escalated_merchant():
    patterns = _compile_patterns(DEFAULT_ESCALATION_PATTERNS)
    r = _receipt(merchant="03/15 Store")
    assert _extraction_tier(_fake_good_text(), r, patterns) == "high"


def test_extraction_tier_low_for_poor_quality():
    patterns = _compile_patterns(DEFAULT_ESCALATION_PATTERNS)
    r = _receipt(merchant="unknown", total=None)
    assert _extraction_tier("short", r, patterns) == "low"
