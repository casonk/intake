"""Unit tests for intake.ai (merge_ai_result field normalisation)."""

from __future__ import annotations

from datetime import date

import pytest

from intake.ai import merge_ai_result, purchaser_was_blacklisted
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
    r = merge_ai_result(_receipt(receipt_date=date(2026, 4, 1)), {"receipt_date": "not-a-date"})
    assert r.receipt_date == date(2026, 4, 1)


# ── purchaser normalisation ───────────────────────────────────────────────────


def test_merge_ai_result_lowercases_purchaser():
    r = merge_ai_result(_receipt(), {"purchaser": "John Smith"})
    assert r.purchaser == "john smith"


def test_merge_ai_result_canonicalizes_purchaser_alias():
    r = merge_ai_result(_receipt(), {"purchaser": "casonkonzer@gmail.com"})
    assert r.purchaser == "cason konzer"


def test_merge_ai_result_skips_empty_purchaser():
    r = merge_ai_result(_receipt(), {"purchaser": ""})
    assert r.purchaser is None


@pytest.mark.parametrize(
    "bad_value",
    [
        "account",
        "Account",
        "ACCOUNT",
        "cardholder",
        "customer",
        "member",
        "guest",
        "user",
        "name",
        "n/a",
        "na",
        "none",
        "unknown",
    ],
)
def test_merge_ai_result_rejects_blacklisted_purchaser(bad_value):
    r = merge_ai_result(_receipt(), {"purchaser": bad_value})
    assert r.purchaser is None


def test_merge_ai_result_accepts_real_purchaser_name():
    r = merge_ai_result(_receipt(), {"purchaser": "Jane Doe"})
    assert r.purchaser == "jane doe"


def test_merge_ai_result_skips_probable_cashier_name_from_counter_line():
    r = merge_ai_result(
        _receipt(raw_text="Counter #: 862305 Sal\nMC 5653 42.38"),
        {"purchaser": "Sam"},
    )
    assert r.purchaser is None


def test_merge_ai_result_clears_unpaid_notice_total():
    r = merge_ai_result(
        _receipt(
            total=15.0,
            raw_text="Status: Unpaid\nTotal Amount Paid: $0.00\nTotal Amount Due: $15.00",
        ),
        {"total": 15.0},
    )
    assert r.total is None


def test_merge_ai_result_prefers_total_amount_paid_for_paid_notice():
    r = merge_ai_result(
        _receipt(
            total=None,
            raw_text="Status: Paid\nTotal Amount Paid: $18.95\nTotal Amount Due: $0.00",
        ),
        {"total": 15.0},
    )
    assert r.total == pytest.approx(18.95)


def test_merge_ai_result_populates_itemized_lines():
    r = merge_ai_result(
        _receipt(),
        {
            "items": [
                {"description": "Bananas", "amount": 2.99, "quantity": 1, "unit_price": 2.99},
                {"description": "Whole Milk", "amount": 4.39, "quantity": None, "unit_price": None},
            ]
        },
    )
    assert len(r.items) == 2
    assert r.items[0].description == "Bananas"
    assert r.items[0].amount == pytest.approx(2.99)


def test_merge_ai_result_merges_certifications():
    r = merge_ai_result(
        _receipt(),
        {
            "certifications": [
                {
                    "issuer": "AAA",
                    "label": "AAA certified",
                    "source_text": "Certified by AAA",
                }
            ]
        },
    )
    assert len(r.certifications) == 1
    assert r.certifications[0].issuer == "AAA"
    assert r.certifications[0].label == "AAA certified"


# ── no-op when AI returns nothing useful ─────────────────────────────────────


def test_merge_ai_result_returns_unchanged_on_empty_dict():
    original = _receipt(merchant="shop", total=5.0)
    result = merge_ai_result(original, {})
    assert result is original


# ── purchaser_was_blacklisted ─────────────────────────────────────────────────


def test_purchaser_was_blacklisted_true_for_account():
    assert purchaser_was_blacklisted({"purchaser": "account"}) is True


def test_purchaser_was_blacklisted_true_case_insensitive():
    assert purchaser_was_blacklisted({"purchaser": "ACCOUNT"}) is True


def test_purchaser_was_blacklisted_true_for_footer_artifact():
    assert purchaser_was_blacklisted({"purchaser": "THANK at FOR SHOPPING"}) is True


def test_purchaser_was_blacklisted_false_for_null():
    assert purchaser_was_blacklisted({"purchaser": None}) is False


def test_purchaser_was_blacklisted_false_for_missing_key():
    assert purchaser_was_blacklisted({}) is False


def test_purchaser_was_blacklisted_false_for_known_alias_email():
    assert purchaser_was_blacklisted({"purchaser": "casonkonzer@gmail.com"}) is False


def test_purchaser_was_blacklisted_false_for_real_name():
    assert purchaser_was_blacklisted({"purchaser": "John Smith"}) is False
