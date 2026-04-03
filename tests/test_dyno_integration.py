"""Integration tests for intake using dyno-lab utilities."""

import json
import textwrap
from datetime import date
from pathlib import Path

import pytest

from dyno_lab.env import EnvPatch
from dyno_lab.fs import TempWorkdir

from intake.classify import classify, load_rules
from intake.parse import parse_receipt
from intake.sidecar import (
    has_sidecar,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)

# ---------------------------------------------------------------------------
# Classify — TempWorkdir for custom TOML rules files
# ---------------------------------------------------------------------------


def test_classify_custom_rules_from_tempworkdir():
    """Classify uses rules loaded from a file created inside TempWorkdir."""
    cfg = textwrap.dedent("""
        [categories.coffee]
        label    = "Coffee Shops"
        keywords = ["starbucks", "dunkin"]

        [categories.general]
        label    = "General"
        keywords = []
    """)
    with TempWorkdir() as tmp:
        rules_file = tmp.write("categories.toml", cfg)
        rules = load_rules(rules_file)

    assert classify("Starbucks Reserve", rules) == "coffee"
    assert classify("Dunkin Donuts", rules) == "coffee"
    assert classify("Grocery Store", rules) == "general"


def test_classify_subcategory_wins_in_tempworkdir_rules():
    cfg = textwrap.dedent("""
        [categories.food]
        label    = "Food"
        keywords = ["mcdonalds"]

        [categories.food.fastfood]
        label    = "Fast Food"
        keywords = ["mcdonalds fast food"]

        [categories.general]
        label    = "General"
        keywords = []
    """)
    with TempWorkdir() as tmp:
        rules_file = tmp.write("cats.toml", cfg)
        rules = load_rules(rules_file)

    assert classify("McDonalds Fast Food #12", rules) == "food/fastfood"


# ---------------------------------------------------------------------------
# Parse — TempWorkdir for realistic file-system context
# ---------------------------------------------------------------------------


def test_parse_receipt_scan_date_from_filename_in_tempworkdir():
    """parse_receipt reads scan date from filename regardless of working dir."""
    with TempWorkdir() as tmp:
        pdf = tmp.path / "20260715_0930.pdf"
        pdf.write_bytes(b"%PDF fake")
        receipt = parse_receipt(pdf.name, "Demo Store\nTOTAL $5.00", pdf_path=pdf)

    assert receipt.scan_date == date(2026, 7, 15)
    assert receipt.total == pytest.approx(5.00)


def test_parse_receipt_falls_back_to_file_mtime_when_no_date_in_name():
    """When filename has no date digits, mtime is used as fallback."""
    with TempWorkdir() as tmp:
        pdf = tmp.path / "RECEIPT.pdf"
        pdf.write_bytes(b"%PDF fake")
        receipt = parse_receipt(
            pdf.name, "Unknown Store\nAmount Due: 9.99", pdf_path=pdf
        )

    assert isinstance(receipt.scan_date, date)
    assert receipt.total == pytest.approx(9.99)


# ---------------------------------------------------------------------------
# Sidecar — TempWorkdir for isolated per-test file trees
# ---------------------------------------------------------------------------


def test_sidecar_roundtrip_in_tempworkdir():
    """write_sidecar + read_sidecar round-trip within an isolated TempWorkdir."""
    with TempWorkdir() as tmp:
        pdf = tmp.path / "20260401_1000.pdf"
        pdf.write_bytes(b"%PDF fake")

        data = {
            "filename": pdf.name,
            "merchant": "dyno store",
            "total": 42.00,
            "category": "grocery/supermarket",
        }
        write_sidecar(pdf, data)

        assert has_sidecar(pdf)
        payload = read_sidecar(pdf)

    assert payload["merchant"] == "dyno store"
    assert payload["total"] == pytest.approx(42.00)
    assert payload["category"] == "grocery/supermarket"
    assert "processed_at" in payload


def test_sidecar_path_sibling_to_pdf_in_tempworkdir():
    with TempWorkdir() as tmp:
        pdf = tmp.path / "20260101_0800.pdf"
        pdf.write_bytes(b"%PDF fake")
        sp = sidecar_path(pdf)

    assert sp.name == "20260101_0800.intake.json"
    assert sp.parent == tmp.path


def test_write_sidecar_adds_processed_at_timestamp():
    with TempWorkdir() as tmp:
        pdf = tmp.path / "20260301_1200.pdf"
        pdf.write_bytes(b"%PDF fake")
        write_sidecar(pdf, {"filename": pdf.name, "merchant": "Test"})
        raw = json.loads(sidecar_path(pdf).read_text())

    assert "processed_at" in raw
    assert raw["processed_at"].startswith("20")  # ISO datetime starts with year


# ---------------------------------------------------------------------------
# EnvPatch — verify intake classify/parse are env-agnostic and demonstrate
# that irrelevant env vars do not corrupt results
# ---------------------------------------------------------------------------


def test_classify_unaffected_by_spurious_env_vars(tmp_path):
    """Unrelated environment variables must not bleed into classification logic."""
    cfg_path = Path(__file__).parent / "fixtures" / "categories.toml"
    rules = load_rules(cfg_path)

    with EnvPatch({"INTAKE_OPENAI_KEY": "sk-fake", "SOME_DEBUG_FLAG": "1"}):
        result = classify("Kroger #99", rules)

    assert result == "grocery/supermarket"


def test_parse_unaffected_by_env_overrides():
    """parse_receipt must produce consistent results regardless of env state."""
    with EnvPatch({"HOME": "/nonexistent", "TMPDIR": "/nonexistent"}):
        receipt = parse_receipt("20260510_1500.pdf", "Acme Co\nTOTAL $7.77")

    assert receipt.scan_date == date(2026, 5, 10)
    assert receipt.total == pytest.approx(7.77)


def test_envpatch_clears_and_restores_vars():
    """EnvPatch with clear=True removes all env vars for the duration of the block."""
    import os

    original_path = os.environ.get("PATH", "")

    with EnvPatch({"CUSTOM_VAR": "hello"}, clear=True):
        assert os.environ.get("CUSTOM_VAR") == "hello"
        assert "PATH" not in os.environ

    assert os.environ.get("PATH") == original_path
    assert "CUSTOM_VAR" not in os.environ
