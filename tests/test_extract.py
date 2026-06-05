"""Unit tests for intake.extract (quality check, model rank, extract_receipt)."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from intake.extract import (
    DEFAULT_ESCALATION_PATTERNS,
    DEFAULT_TOTAL_ESCALATION_THRESHOLD,
    OCR_GARBAGE_THRESHOLD,
    OCR_MODEL,
    _compile_patterns,
    _extraction_tier,
    _merchant_escalates,
    _ocr_garbage_score,
    _ocr_quality_ok,
    extract_receipt,
    model_rank,
    should_reprocess,
)
from intake.parse import Receipt, ReceiptItem

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
def test_extract_receipt_returns_tesseract_when_quality_ok(mock_parse, mock_ocr, tmp_path):
    good_text = _fake_good_text()
    mock_ocr.return_value = good_text
    mock_parse.return_value = _receipt(raw_text=good_text)

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    receipt, model, tier = extract_receipt(pdf)
    assert model == OCR_MODEL
    assert receipt._debug_ai_attempts == []  # type: ignore[attr-defined]


@patch("intake.extract.extract_text")
@patch("intake.extract.parse_receipt")
@patch("intake.ai.merge_ai_result")
@patch("intake.ai.extract_with_openai")
def test_extract_receipt_attempts_ai_when_items_missing(
    mock_ai, mock_merge, mock_parse, mock_ocr, tmp_path
):
    good_text = _fake_good_text()
    base_receipt = _receipt(raw_text=good_text)
    improved = _receipt(raw_text=good_text)
    improved.items = [ReceiptItem(description="Bananas", amount=2.99)]
    mock_ocr.return_value = good_text
    mock_parse.return_value = base_receipt
    mock_ai.return_value = {
        "items": [{"description": "Bananas", "amount": 2.99, "quantity": None, "unit_price": None}]
    }
    mock_merge.return_value = improved

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    receipt, model, tier = extract_receipt(
        pdf, openai_api_key="sk-test", openai_model_low="gpt-4o-mini"
    )

    assert model == "gpt-4o-mini"
    assert tier == "low"
    assert mock_ai.call_count == 1
    assert receipt.items[0].description == "Bananas"


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
def test_extract_receipt_uses_high_model_for_suspicious_total(
    mock_ai, mock_merge, mock_parse, mock_ocr, tmp_path
):
    good_text = _fake_good_text()
    base_receipt = _receipt(merchant="Kroger", total=965.00, raw_text=good_text)
    base_receipt.items = [ReceiptItem(description="Groceries", amount=32.96)]
    improved = _receipt(merchant="Kroger", total=32.96, raw_text=good_text)
    improved.items = base_receipt.items
    mock_ocr.return_value = good_text
    mock_parse.return_value = base_receipt
    mock_ai.return_value = {"merchant": "Kroger", "total": 32.96}
    mock_merge.return_value = improved

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    receipt, model, tier = extract_receipt(
        pdf,
        openai_api_key="sk-test",
        openai_model_low="gpt-low",
        openai_model_high="gpt-high",
        total_escalation_threshold=DEFAULT_TOTAL_ESCALATION_THRESHOLD,
    )

    assert tier == "high"
    assert model == "gpt-high"
    mock_ai.assert_called_once_with(pdf, good_text, "sk-test", "gpt-high")
    assert receipt.total == 32.96


@patch("intake.extract.extract_text")
@patch("intake.extract.parse_receipt")
def test_extract_receipt_preserves_high_total_tier_without_key(mock_parse, mock_ocr, tmp_path):
    good_text = _fake_good_text()
    mock_ocr.return_value = good_text
    mock_parse.return_value = _receipt(merchant="Kroger", total=965.00, raw_text=good_text)

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    _receipt_obj, model, tier = extract_receipt(
        pdf,
        openai_api_key=None,
        total_escalation_threshold=DEFAULT_TOTAL_ESCALATION_THRESHOLD,
    )

    assert model == OCR_MODEL
    assert tier == "high"


# ── mid-tier upgrade ──────────────────────────────────────────────────────────


@patch("intake.extract.extract_text")
@patch("intake.extract.parse_receipt")
@patch("intake.ai.merge_ai_result")
@patch("intake.ai.extract_with_openai")
def test_mid_tier_upgrade_when_low_model_returns_blacklisted_purchaser(
    mock_ai, mock_merge, mock_parse, mock_ocr, tmp_path
):
    """When low-tier AI returns a blacklisted purchaser, extract_receipt upgrades to mid."""
    mock_ocr.return_value = "bad"
    base = _receipt(merchant="unknown", total=None, raw_text="bad")
    after_low = _receipt(merchant="some shop", total=10.0, raw_text="bad")
    after_mid = _receipt(merchant="some shop", total=10.0, raw_text="bad")
    mock_parse.return_value = base
    # First call (low model) returns blacklisted purchaser in raw dict
    # Second call (mid model) returns a real name
    mock_ai.side_effect = [
        {"merchant": "some shop", "total": 10.0, "purchaser": "account"},
        {"merchant": "some shop", "total": 10.0, "purchaser": "Jane Doe"},
    ]
    mock_merge.side_effect = [after_low, after_mid]

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    receipt, model, tier = extract_receipt(
        pdf,
        openai_api_key="sk-test",
        openai_model_low="gpt-4o-mini",
        openai_model_mid="gpt-4o",
        openai_model_high="gpt-4.1",
    )

    assert tier == "mid"
    assert model == "gpt-4o"
    assert mock_ai.call_count == 2  # low then mid


@patch("intake.extract.extract_text")
@patch("intake.extract.parse_receipt")
@patch("intake.ai.merge_ai_result")
@patch("intake.ai.extract_with_openai")
def test_no_mid_tier_when_low_model_returns_null_purchaser(
    mock_ai, mock_merge, mock_parse, mock_ocr, tmp_path
):
    """When low-tier AI returns null purchaser (not blacklisted), stay at low tier."""
    mock_ocr.return_value = "bad"
    base = _receipt(merchant="unknown", total=None, raw_text="bad")
    after_low = _receipt(merchant="some shop", total=10.0, raw_text="bad")
    mock_parse.return_value = base
    mock_ai.return_value = {"merchant": "some shop", "total": 10.0, "purchaser": None}
    mock_merge.return_value = after_low

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    receipt, model, tier = extract_receipt(
        pdf, openai_api_key="sk-test", openai_model_low="gpt-4o-mini", openai_model_mid="gpt-4o"
    )

    assert tier == "low"
    assert model == "gpt-4o-mini"
    assert mock_ai.call_count == 1  # no upgrade


@patch("intake.extract.extract_text")
@patch("intake.extract.parse_receipt")
@patch("intake.ai.merge_ai_result")
@patch("intake.ai.extract_with_openai")
def test_extract_receipt_uses_ai_when_quality_low(
    mock_ai, mock_merge, mock_parse, mock_ocr, tmp_path
):
    mock_ocr.return_value = "bad"
    base_receipt = _receipt(merchant="unknown", total=None, raw_text="bad")
    improved = _receipt(merchant="real store", total=12.50, raw_text="bad")
    mock_parse.return_value = base_receipt
    mock_ai.return_value = {"merchant": "Real Store", "total": 12.50}
    mock_merge.return_value = improved

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    receipt, model, tier = extract_receipt(
        pdf, openai_api_key="sk-test", openai_model_low="gpt-4o-mini"
    )
    assert model == "gpt-4o-mini"
    assert receipt.merchant == "real store"  # merge_ai_result normalises to lowercase
    assert receipt._debug_ai_attempts == [  # type: ignore[attr-defined]
        {"model": "gpt-4o-mini", "response": {"merchant": "Real Store", "total": 12.50}}
    ]


@patch("intake.extract.extract_text")
@patch("intake.extract.parse_receipt")
@patch("intake.ai.merge_ai_result")
@patch("intake.ai.extract_with_openai")
def test_extract_receipt_consolidates_post_ai_merchant(
    mock_ai, mock_merge, mock_parse, mock_ocr, tmp_path
):
    mock_ocr.return_value = "bad"
    base_receipt = _receipt(merchant="unknown", total=None, raw_text="bad")
    improved = _receipt(merchant="McDonald's Restaurant #36549", total=6.13, raw_text="bad")
    mock_parse.return_value = base_receipt
    mock_ai.return_value = {"merchant": "McDonald's Restaurant #36549", "total": 6.13}
    mock_merge.return_value = improved

    pdf = tmp_path / "20260401_1000.pdf"
    pdf.write_bytes(b"%PDF fake")

    receipt, model, tier = extract_receipt(
        pdf, openai_api_key="sk-test", openai_model_low="gpt-4o-mini"
    )

    assert model == "gpt-4o-mini"
    assert receipt.merchant == "mcdonald's"


@patch("intake.extract.extract_text")
@patch("intake.extract.parse_receipt")
@patch("intake.ai.extract_with_openai", side_effect=Exception("API error"))
def test_extract_receipt_degrades_gracefully_on_ai_failure(mock_ai, mock_parse, mock_ocr, tmp_path):
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


def test_extraction_tier_high_for_suspicious_total():
    patterns = _compile_patterns(DEFAULT_ESCALATION_PATTERNS)
    r = _receipt(merchant="Kroger", total=DEFAULT_TOTAL_ESCALATION_THRESHOLD + 0.01)
    assert _extraction_tier(_fake_good_text(), r, patterns) == "high"


def test_extraction_tier_does_not_escalate_total_when_disabled():
    patterns = _compile_patterns(DEFAULT_ESCALATION_PATTERNS)
    r = _receipt(merchant="Kroger", total=965.00)
    assert _extraction_tier(_fake_good_text(), r, patterns, total_escalation_threshold=0) == "ok"


def test_extraction_tier_low_for_poor_quality():
    patterns = _compile_patterns(DEFAULT_ESCALATION_PATTERNS)
    r = _receipt(merchant="unknown", total=None)
    assert _extraction_tier("short", r, patterns) == "low"


# ── _ocr_garbage_score ────────────────────────────────────────────────────────

_GARBAGE_BODY = " ".join(
    [
        "JJJJJJJJJJJ",
        "SSSSSSSSSSS",
        "***********",
        "uzpfuxdydcu",
        "nebxwjtmnsi",
        "xwllsiczpuf",
        "axmchzcsdni",
        "1jnq2plwlcy",
        "4ilvc0rylnx",
        "azh1boabydc",
    ]
)


def test_ocr_garbage_score_high_for_garbage_text():
    score = _ocr_garbage_score(_GARBAGE_BODY)
    assert score >= OCR_GARBAGE_THRESHOLD


def test_ocr_garbage_score_low_for_clean_receipt():
    clean = (
        "Kroger\n123 Main St\nAnytown ST 12345\n"
        "Cashier: John\nDate: 2026-04-01\n"
        "Bananas $1.99\nMilk $3.49\nBread $2.99\n"
        "Subtotal $8.47\nTax $0.51\nTotal $8.98\n"
        "Thank you for shopping with us today"
    )
    assert _ocr_garbage_score(clean) < OCR_GARBAGE_THRESHOLD


def test_ocr_garbage_score_zero_for_empty():
    assert _ocr_garbage_score("") == 0.0


def test_extraction_tier_high_for_garbage_ocr():
    patterns = _compile_patterns(DEFAULT_ESCALATION_PATTERNS)
    r = _receipt()
    assert _extraction_tier(_GARBAGE_BODY, r, patterns) == "high"
