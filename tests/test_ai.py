"""Unit tests for intake.ai (merge_ai_result field normalisation)."""

from __future__ import annotations

from datetime import date

import pytest

from intake.ai import merge_ai_result
from intake.parse import Receipt


def _receipt(**kwargs) -> Receipt:
    defaults = dict(
        filename="20260401_1000.pdf",
        scan_date=date(2026, 4, 1),
        receipt_date=None,
        receipt_time=None,
        merchant=None,
        total=None,
        raw_text="",
    )
    defaults.update(kwargs)
    return Receipt(**defaults)


# ── merchant normalisation ────────────────────────────────────────────────────


def test_merge_ai_result_lowercases_merchant():
    r = merge_ai_result(_receipt(), {"merchant": "Whole Foods Market"})
    assert r.merchant == "whole foods market"


def test_merge_ai_result_strips_merchant():
    r = merge_ai_result(_receipt(), {"merchant": "  Target  "})
    assert r.merchant == "target"


def test_merge_ai_result_truncates_merchant_at_60():
    long_name = "A" * 70
    r = merge_ai_result(_receipt(), {"merchant": long_name})
    assert r.merchant == "a" * 60


def test_merge_ai_result_skips_empty_merchant():
    r = merge_ai_result(_receipt(merchant="original"), {"merchant": ""})
    assert r.merchant == "original"


def test_merge_ai_result_skips_whitespace_merchant():
    r = merge_ai_result(_receipt(merchant="original"), {"merchant": "   "})
    assert r.merchant == "original"


# ── total normalisation ───────────────────────────────────────────────────────


def test_merge_ai_result_sets_total_float():
    r = merge_ai_result(_receipt(), {"total": 42.99})
    assert r.total == pytest.approx(42.99)


def test_merge_ai_result_coerces_string_total():
    r = merge_ai_result(_receipt(), {"total": "19.95"})
    assert r.total == pytest.approx(19.95)


def test_merge_ai_result_ignores_negative_total():
    r = merge_ai_result(_receipt(total=10.0), {"total": -1.0})
    assert r.total == pytest.approx(10.0)


def test_merge_ai_result_ignores_zero_total():
    r = merge_ai_result(_receipt(total=10.0), {"total": 0.0})
    assert r.total == pytest.approx(10.0)


def test_merge_ai_result_ignores_non_numeric_total():
    r = merge_ai_result(_receipt(total=5.0), {"total": "N/A"})
    assert r.total == pytest.approx(5.0)


# ── date normalisation ────────────────────────────────────────────────────────


def test_merge_ai_result_parses_receipt_date():
    r = merge_ai_result(_receipt(), {"receipt_date": "2026-01-15"})
    assert r.receipt_date == date(2026, 1, 15)


def test_merge_ai_result_ignores_invalid_date():
    r = merge_ai_result(
        _receipt(receipt_date=date(2026, 4, 1)), {"receipt_date": "not-a-date"}
    )
    assert r.receipt_date == date(2026, 4, 1)


# ── purchaser normalisation ───────────────────────────────────────────────────


def test_merge_ai_result_lowercases_purchaser():
    r = merge_ai_result(_receipt(), {"purchaser": "John Smith"})
    assert r.purchaser == "john smith"


def test_merge_ai_result_skips_empty_purchaser():
    r = merge_ai_result(_receipt(), {"purchaser": ""})
    assert r.purchaser is None


# ── no-op when AI returns nothing useful ─────────────────────────────────────


def test_merge_ai_result_returns_unchanged_on_empty_dict():
    original = _receipt(merchant="shop", total=5.0)
    result = merge_ai_result(original, {})
    assert result is original
