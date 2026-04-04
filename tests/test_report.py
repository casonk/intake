"""Tests for intake.report."""

from __future__ import annotations

from pathlib import Path

from intake.db import init_db, upsert_receipt
from intake.report import generate_reports


def _seed_receipts(db_path: Path) -> None:
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "20260110_0900.pdf",
            "scan_date": "2026-01-10",
            "merchant": "kroger",
            "total": 45.67,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260110_0900.pdf",
            "raw_text": "kroger",
            "processed_by": "tesseract",
            "purchaser": "john",
            "card_last4": "1111",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260211_1400.pdf",
            "scan_date": "2026-02-11",
            "merchant": "costco",
            "total": 125.00,
            "category": "grocery/warehouse",
            "receipt_dir": "grocery/warehouse/20260211_1400.pdf",
            "raw_text": "costco",
            "processed_by": "gpt-4o-mini",
            "purchaser": None,
            "card_last4": None,
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260318_1834.pdf",
            "scan_date": "2026-03-18",
            "merchant": "shell",
            "total": 62.10,
            "category": "gas-station",
            "receipt_dir": "gas-station/20260318_1834.pdf",
            "raw_text": "shell",
            "processed_by": "gpt-4.1",
            "purchaser": "john",
            "card_last4": "4321",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "email_1.txt",
            "scan_date": "2026-03-19",
            "merchant": "github",
            "total": 9.99,
            "category": "sw/dev",
            "receipt_dir": "email/sw/dev",
            "raw_text": "github",
            "processed_by": "email-parse",
            "purchaser": None,
            "card_last4": None,
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260331_1914.pdf",
            "scan_date": "2026-03-31",
            "merchant": "kroger",
            "total": 53.81,
            "category": "grocery",
            "receipt_dir": "grocery/20260331_1914.pdf",
            "raw_text": "kroger",
            "processed_by": "gpt-4.1",
            "purchaser": None,
            "card_last4": "3553",
        },
    )
    conn.close()


def _seed_receipt_files(receipt_root: Path) -> None:
    for relative in (
        "grocery/supermarket/20260110_0900.pdf",
        "grocery/warehouse/20260211_1400.pdf",
        "gas-station/20260318_1834.pdf",
        "grocery/kroger/20260331_1914.pdf",
    ):
        path = receipt_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pdf placeholder", encoding="utf-8")
    (receipt_root / "sw" / "dev").mkdir(parents=True, exist_ok=True)


def test_generate_reports_renders_chart_sections(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"
    receipt_root = tmp_path / "receipts"
    _seed_receipts(db_path)
    _seed_receipt_files(receipt_root)

    conn = init_db(db_path)
    written = generate_reports(conn, output_dir, fmt="both", receipt_root=receipt_root)
    conn.close()

    assert output_dir / "intake-report.md" in written
    assert output_dir / "intake-report.html" in written

    md = (output_dir / "intake-report.md").read_text(encoding="utf-8")
    html = (output_dir / "intake-report.html").read_text(encoding="utf-8")

    assert "| Category | Count | Total Spend | % of Total | Chart |" in md
    assert "| Month | Count | Total Spend | Chart |" in md
    assert "| Model | Receipt Count | Share | Chart |" in md
    assert "########################" in md

    assert "Monthly Spend Trend" in html
    assert "Top Categories by Spend" in html
    assert "Top Merchants by Spend" in html
    assert "Extraction Model Mix" in html
    assert "Recent Receipts (last 50)" in html
    assert '<svg class="line-chart"' in html
    assert "bar-fill fill-category" in html
    assert "bar-fill fill-merchant" in html
    assert "bar-fill fill-model" in html
    assert "Receipt activity at a glance." in html
    assert receipt_root.joinpath("grocery", "warehouse").as_uri() in html
    assert receipt_root.joinpath("gas-station", "20260318_1834.pdf").as_uri() in html
    assert "[gas-station]" in md
    assert receipt_root.joinpath("gas-station").as_uri() in md
    assert receipt_root.joinpath("gas-station", "20260318_1834.pdf").as_uri() in md
    assert receipt_root.joinpath("grocery", "kroger", "20260331_1914.pdf").as_uri() in md
    assert "## Recent Receipts (last 50)" in md
    assert receipt_root.joinpath("sw", "dev").as_uri() in md
    assert receipt_root.joinpath("sw", "dev").as_uri() in html
    assert "email_1.txt" in md
    assert f"[email_1.txt]({receipt_root.joinpath('sw', 'dev').as_uri()})" in md


def test_generate_reports_handles_empty_dataset(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"

    conn = init_db(db_path)
    generate_reports(conn, output_dir, fmt="both", receipt_root=tmp_path / "receipts")
    conn.close()

    html = (output_dir / "intake-report.html").read_text(encoding="utf-8")
    md = (output_dir / "intake-report.md").read_text(encoding="utf-8")

    assert "No spend data yet." in html
    assert "No category data yet." in html
    assert "No merchant data yet." in html
    assert "No extraction data yet." in html
    assert "| Total Receipts | 0 |" in md
