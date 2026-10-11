"""Tests for intake.report."""

from __future__ import annotations

from pathlib import Path

from intake.db import init_db, upsert_receipt
from intake.report import _format_item_summary, generate_reports


def _browser_uri(*parts: str, is_dir: bool = False) -> str:
    from urllib.parse import quote

    path = "/".join(quote(part, safe="") for part in ("files", "receipt", *parts))
    uri = f"https://files.snowbridge.internal/{path}"
    if is_dir:
        uri += "/"
    return uri


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
            "staff": [
                {"role": "cashier", "name": "chec 510", "source_text": "Your cashier was CHEC 510"}
            ],
            "items": [
                {"description": "Bananas", "amount": 2.99},
                {"description": "Whole Milk", "amount": 4.39},
            ],
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


def test_format_item_summary_keeps_table_preview_short():
    label, markdown, full_label = _format_item_summary(
        [
            {
                "description": (
                    "KeePassium (KeePass passwords) Version 2.5 Premium "
                    "(iOS+Mac) In-App Purchase iPhone"
                )
            },
            {"description": "Family Sharing renewal for a very long product name"},
            {"description": "Tax adjustment"},
        ]
    )

    assert len(label) <= 90
    assert label == "3: KeePassium (KeePass passwords)..., Family Sharing renewal for a ve..., ..."
    assert markdown == label
    assert "In-App Purchase iPhone" in full_label


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
    assert output_dir / "merchants" / "index.md" in written
    assert output_dir / "merchants" / "index.html" in written
    assert output_dir / "merchants" / "kroger.md" in written
    assert output_dir / "merchants" / "kroger.html" in written

    md = (output_dir / "intake-report.md").read_text(encoding="utf-8")
    html = (output_dir / "intake-report.html").read_text(encoding="utf-8")
    merchant_index_md = (output_dir / "merchants" / "index.md").read_text(encoding="utf-8")
    merchant_index_html = (output_dir / "merchants" / "index.html").read_text(encoding="utf-8")
    merchant_detail_md = (output_dir / "merchants" / "kroger.md").read_text(encoding="utf-8")
    merchant_detail_html = (output_dir / "merchants" / "kroger.html").read_text(encoding="utf-8")

    assert "| Category | Count | Total Spend | % of Total | Chart |" in md
    assert "| Month | Count | Total Spend | Chart |" in md
    assert "| Model | Receipt Count | Share | Chart |" in md
    assert "| Itemized Receipts | 1 / 5 (20.0%) |" in md
    assert "| Total Line Items | 2 |" in md
    assert "########################" in md

    assert "Monthly Spend Trend" in html
    assert "Top Categories by Spend" in html
    assert "Top Merchants by Spend" in html
    assert "Merchant Spend Over Time" in html
    assert "Merchant Month Breakdown" in html
    assert "Extraction Model Mix" in html
    assert "Recent Receipts (last 50)" in html
    assert 'aria-label="Section menu"' in html
    assert 'href="merchants/index.html"' in html
    assert 'href="#merchant-spend-over-time"' in html
    assert 'href="#merchant-month-breakdown"' in html
    assert 'href="#recent-receipts"' in html
    assert "Itemized Receipts" in html
    assert "Total Line Items" in html
    assert '<svg class="line-chart"' in html
    assert "bar-fill fill-category" in html
    assert "bar-fill fill-merchant" in html
    assert "bar-fill fill-model" in html
    assert "Receipt activity at a glance." in html
    assert _browser_uri("grocery", "warehouse", is_dir=True) in html
    assert _browser_uri("gas-station", "20260318_1834.pdf") in html
    assert _browser_uri(is_dir=True) in html
    assert _browser_uri("intake", is_dir=True) in html
    assert "[gas-station]" in md
    assert _browser_uri("gas-station", is_dir=True) in md
    assert _browser_uri("gas-station", "20260318_1834.pdf") in md
    assert _browser_uri("grocery", "kroger", "20260331_1914.pdf") in md
    assert "## Merchant Spend Over Time" in md
    assert "## Merchant Month Breakdown" in md
    assert "## Recent Receipts (last 50)" in md
    assert "[Merchant Directory](merchants/index.md)" in md
    assert "[Visual Overview](#visual-overview)" in md
    assert "[Merchant Trends](#merchant-spend-over-time)" in md
    assert _browser_uri("sw", "dev", is_dir=True) in md
    assert _browser_uri("sw", "dev", is_dir=True) in html
    assert "email_1.txt" in md
    assert f"[email_1.txt]({_browser_uri('sw', 'dev', is_dir=True)})" not in md
    assert f"[receipt/]({_browser_uri(is_dir=True)})" in md
    assert f"[receipt/intake/]({_browser_uri('intake', is_dir=True)})" in md
    assert "2: Bananas, Whole Milk" in md
    assert "2: Bananas, Whole Milk" in html
    assert "cashier: chec 510" in md
    assert "cashier: chec 510" in html
    assert "Jan 2026 $45.67 (1); Mar 2026 $53.81 (1)" in md
    assert "Jan 2026 $45.67 (1)<br>Mar 2026 $53.81 (1)" in html
    assert "| [kroger](merchants/kroger.md) | Jan 2026 | 1 | $45.67 |" in md
    assert "| [kroger](merchants/kroger.md) | Mar 2026 | 1 | $53.81 |" in md
    assert '<a href="merchants/kroger.html">kroger</a>' in html

    assert "# Merchant Directory" in merchant_index_md
    assert "[Main Report](../intake-report.md)" in merchant_index_md
    assert "| [kroger](kroger.md) | 2 | $99.48 | 2 | 2026-01-10 → 2026-03-31 |" in merchant_index_md
    assert "Browse every tracked merchant." in merchant_index_html
    assert "data-merchant-row" in merchant_index_html
    assert 'href="kroger.html"' in merchant_index_html
    assert 'id="merchant-filter"' in merchant_index_html

    assert "# Merchant: kroger" in merchant_detail_md
    assert (
        "[Main Report](../intake-report.md) | [Merchant Directory](index.md)" in merchant_detail_md
    )
    assert "## Monthly Spend" in merchant_detail_md
    assert "| Jan 2026 | 1 | $45.67 |" in merchant_detail_md
    assert "| Mar 2026 | 1 | $53.81 |" in merchant_detail_md
    assert "| [grocery/supermarket]" in merchant_detail_md
    assert "| 2026-03-31 | [grocery]" in merchant_detail_md

    assert "<title>kroger · Intake Merchant History</title>" in merchant_detail_html
    assert 'href="../intake-report.html"' in merchant_detail_html
    assert 'href="index.html"' in merchant_detail_html
    assert "Every tracked receipt for this merchant bucket." in merchant_detail_html
    assert "Category Mix" in merchant_detail_html
    assert "Monthly Spend" in merchant_detail_html
    assert _browser_uri("grocery", "kroger", is_dir=True) in merchant_detail_html
    assert _browser_uri("grocery", "kroger", "20260331_1914.pdf") in merchant_detail_html


def test_generate_reports_links_extraction_model_breakdown_to_detail_pages(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"
    receipt_root = tmp_path / "receipts"
    _seed_receipts(db_path)
    _seed_receipt_files(receipt_root)

    conn = init_db(db_path)
    written = generate_reports(conn, output_dir, fmt="both", receipt_root=receipt_root)
    conn.close()

    assert output_dir / "models" / "index.md" in written
    assert output_dir / "models" / "index.html" in written
    assert output_dir / "models" / "tesseract.md" in written
    assert output_dir / "models" / "tesseract.html" in written

    html = (output_dir / "intake-report.html").read_text(encoding="utf-8")
    md = (output_dir / "intake-report.md").read_text(encoding="utf-8")
    model_index_html = (output_dir / "models" / "index.html").read_text(encoding="utf-8")
    model_index_md = (output_dir / "models" / "index.md").read_text(encoding="utf-8")
    tesseract_html = (output_dir / "models" / "tesseract.html").read_text(encoding="utf-8")
    tesseract_md = (output_dir / "models" / "tesseract.md").read_text(encoding="utf-8")

    # The main report's Extraction Model Breakdown table links each model.
    assert '<a href="models/tesseract.html">tesseract</a>' in html
    assert "[tesseract](models/tesseract.md)" in md

    assert "Extraction Model Directory" in model_index_html
    assert 'href="tesseract.html"' in model_index_html
    assert "[tesseract](tesseract.md)" in model_index_md

    assert "<title>tesseract · Intake Extraction Model History</title>" in tesseract_html
    assert 'href="../intake-report.html"' in tesseract_html
    assert 'href="index.html"' in tesseract_html
    assert "Category Mix" in tesseract_html
    assert "kroger" in tesseract_html
    assert 'href="../merchants/kroger.html"' in tesseract_html
    assert _browser_uri("grocery", "supermarket", "20260110_0900.pdf") in tesseract_html

    assert "# Extraction Model: tesseract" in tesseract_md
    assert "[Main Report](../intake-report.md) | [Model Directory](index.md)" in tesseract_md
    assert "[kroger](../merchants/kroger.md)" in tesseract_md


def test_generate_reports_links_model_column_on_audit_page(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"
    receipt_root = tmp_path / "receipts"
    _seed_receipts(db_path)
    _seed_receipt_files(receipt_root)

    conn = init_db(db_path)
    written = generate_reports(conn, output_dir, fmt="both", receipt_root=receipt_root)
    conn.close()

    assert output_dir / "audit.html" in written
    audit_html = (output_dir / "audit.html").read_text(encoding="utf-8")
    assert '"model_href": "models/tesseract.html"' in audit_html
    assert '"model_href": "models/gpt-4o-mini.html"' in audit_html


def test_generate_reports_links_purchaser_breakdown_to_detail_pages(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"
    receipt_root = tmp_path / "receipts"
    _seed_receipts(db_path)
    _seed_receipt_files(receipt_root)

    conn = init_db(db_path)
    written = generate_reports(conn, output_dir, fmt="both", receipt_root=receipt_root)
    conn.close()

    assert output_dir / "purchasers" / "index.md" in written
    assert output_dir / "purchasers" / "index.html" in written
    assert output_dir / "purchasers" / "john.md" in written
    assert output_dir / "purchasers" / "john.html" in written
    assert output_dir / "purchasers" / "unknown.html" in written

    html = (output_dir / "intake-report.html").read_text(encoding="utf-8")
    md = (output_dir / "intake-report.md").read_text(encoding="utf-8")
    purchaser_index_html = (output_dir / "purchasers" / "index.html").read_text(encoding="utf-8")
    purchaser_index_md = (output_dir / "purchasers" / "index.md").read_text(encoding="utf-8")
    john_html = (output_dir / "purchasers" / "john.html").read_text(encoding="utf-8")
    john_md = (output_dir / "purchasers" / "john.md").read_text(encoding="utf-8")

    # The main report's Purchaser Breakdown table and Recent Receipts rows link each purchaser.
    assert '<a href="purchasers/john.html">john</a>' in html
    assert "[john](purchasers/john.md)" in md

    assert "Purchaser Directory" in purchaser_index_html
    assert 'href="john.html"' in purchaser_index_html
    assert "[john](john.md)" in purchaser_index_md

    assert "<title>john · Intake Purchaser History</title>" in john_html
    assert 'href="../intake-report.html"' in john_html
    assert 'href="index.html"' in john_html
    assert "Category Mix" in john_html
    assert "kroger" in john_html
    assert 'href="../merchants/kroger.html"' in john_html
    assert "$107.77" in john_html
    assert _browser_uri("grocery", "supermarket", "20260110_0900.pdf") in john_html

    assert "# Purchaser: john" in john_md
    assert "[Main Report](../intake-report.md) | [Purchaser Directory](index.md)" in john_md
    assert "[kroger](../merchants/kroger.md)" in john_md


def test_generate_reports_links_purchaser_column_on_audit_page(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"
    receipt_root = tmp_path / "receipts"
    _seed_receipts(db_path)
    _seed_receipt_files(receipt_root)

    conn = init_db(db_path)
    generate_reports(conn, output_dir, fmt="both", receipt_root=receipt_root)
    conn.close()

    audit_html = (output_dir / "audit.html").read_text(encoding="utf-8")
    assert '"purchaser_href": "purchasers/john.html"' in audit_html


def test_generate_reports_handles_empty_dataset(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"

    conn = init_db(db_path)
    written = generate_reports(conn, output_dir, fmt="both", receipt_root=tmp_path / "receipts")
    conn.close()

    html = (output_dir / "intake-report.html").read_text(encoding="utf-8")
    md = (output_dir / "intake-report.md").read_text(encoding="utf-8")
    merchant_index_html = (output_dir / "merchants" / "index.html").read_text(encoding="utf-8")
    merchant_index_md = (output_dir / "merchants" / "index.md").read_text(encoding="utf-8")

    assert "No spend data yet." in html
    assert "No category data yet." in html
    assert "No merchant data yet." in html
    assert "No extraction data yet." in html
    assert "| Total Receipts | 0 |" in md
    assert output_dir / "merchants" / "index.html" in written
    assert output_dir / "merchants" / "index.md" in written
    assert "No merchant data yet." in merchant_index_html
    assert "No merchant data yet." in merchant_index_md

    model_index_html = (output_dir / "models" / "index.html").read_text(encoding="utf-8")
    model_index_md = (output_dir / "models" / "index.md").read_text(encoding="utf-8")
    assert output_dir / "models" / "index.html" in written
    assert output_dir / "models" / "index.md" in written
    assert "No model data yet." in model_index_html
    assert "No model data yet." in model_index_md

    purchaser_index_html = (output_dir / "purchasers" / "index.html").read_text(encoding="utf-8")
    purchaser_index_md = (output_dir / "purchasers" / "index.md").read_text(encoding="utf-8")
    assert output_dir / "purchasers" / "index.html" in written
    assert output_dir / "purchasers" / "index.md" in written
    assert "No purchaser data yet." in purchaser_index_html
    assert "No purchaser data yet." in purchaser_index_md


def test_generate_reports_resolves_non_pdf_receipt_links(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"
    receipt_root = tmp_path / "receipts"

    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "20260402_1707.jpg",
            "scan_date": "2026-04-02",
            "merchant": "target",
            "total": 23.45,
            "category": "general",
            "receipt_dir": "general/20260402_1707.jpg",
            "raw_text": "target",
            "processed_by": "tesseract",
        },
    )
    written = generate_reports(conn, output_dir, fmt="both", receipt_root=receipt_root)
    conn.close()

    jpg = receipt_root / "general" / "20260402_1707.jpg"
    jpg.parent.mkdir(parents=True, exist_ok=True)
    jpg.write_text("image placeholder", encoding="utf-8")

    conn = init_db(db_path)
    written = generate_reports(conn, output_dir, fmt="both", receipt_root=receipt_root)
    conn.close()

    html = (output_dir / "intake-report.html").read_text(encoding="utf-8")
    md = (output_dir / "intake-report.md").read_text(encoding="utf-8")

    assert output_dir / "intake-report.html" in written
    assert _browser_uri("general", "20260402_1707.jpg") in html
    assert _browser_uri("general", "20260402_1707.jpg") in md


def test_generate_reports_links_existing_email_text_artifact(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"
    receipt_root = tmp_path / "receipts"
    artifact = receipt_root / "email" / "sw" / "subscription" / "email_31077_INBOX.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("KeePassium receipt body", encoding="utf-8")

    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "email_31077_INBOX.txt",
            "scan_date": "2026-04-26",
            "merchant": "apple",
            "total": 31.79,
            "category": "sw/subscription",
            "receipt_dir": "email/sw/subscription",
            "raw_text": "KeePassium App Store",
            "processed_by": "gpt-4.1",
        },
    )
    generate_reports(conn, output_dir, fmt="both", receipt_root=receipt_root)
    conn.close()

    html = (output_dir / "intake-report.html").read_text(encoding="utf-8")
    md = (output_dir / "intake-report.md").read_text(encoding="utf-8")

    assert _browser_uri("email", "sw", "subscription", "email_31077_INBOX.txt") in html
    assert _browser_uri("email", "sw", "subscription", "email_31077_INBOX.txt") in md


def test_generate_reports_distinguishes_unknown_totals_from_true_zero(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"

    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "20260101_0558.pdf",
            "scan_date": "2026-01-01",
            "merchant": "keepgo",
            "total": None,
            "category": "phone/keepgo",
            "receipt_dir": "phone/keepgo/20260101_0558.pdf",
            "raw_text": "keepgo",
            "processed_by": "gpt-4.1",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20210327_1740.pdf",
            "scan_date": "2021-03-27",
            "merchant": "bloomberg for education",
            "total": 0.0,
            "category": "sw",
            "receipt_dir": "sw/20210327_1740.pdf",
            "raw_text": "bloomberg for education",
            "processed_by": "gpt-4.1",
        },
    )
    generate_reports(conn, output_dir, fmt="both", receipt_root=tmp_path / "receipts")
    conn.close()

    md = (output_dir / "intake-report.md").read_text(encoding="utf-8")
    merchant_index_md = (output_dir / "merchants" / "index.md").read_text(encoding="utf-8")
    keepgo_md = (output_dir / "merchants" / "keepgo.md").read_text(encoding="utf-8")
    bloomberg_md = (output_dir / "merchants" / "bloomberg-for-education.md").read_text(
        encoding="utf-8"
    )

    assert "| [keepgo](merchants/keepgo.md) | Jan 2026 | 1 | — |" in md
    assert (
        "| [bloomberg for education](merchants/bloomberg-for-education.md) | Mar 2021 | 1 | $0.00 |"
        in md
    )
    assert "| [keepgo](keepgo.md) | 1 | — | 1 | 2026-01-01 → 2026-01-01 |" in merchant_index_md
    assert (
        "| [bloomberg for education](bloomberg-for-education.md) | 1 | $0.00 | 1 | 2021-03-27 → 2021-03-27 |"
        in merchant_index_md
    )
    assert "| Total Spend | — |" in keepgo_md
    assert "| Jan 2026 | 1 | — |" in keepgo_md
    assert "| Total Spend | $0.00 |" in bloomberg_md
    assert "| Mar 2021 | 1 | $0.00 |" in bloomberg_md


def test_generate_reports_formats_jpy_rows_without_adding_to_usd_rollups(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"

    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "20260401_0900.pdf",
            "scan_date": "2026-04-01",
            "merchant": "kroger",
            "total": 10.00,
            "currency": "USD",
            "category": "grocery",
            "receipt_dir": "grocery/20260401_0900.pdf",
            "raw_text": "kroger",
            "processed_by": "tesseract",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260402_0900.pdf",
            "scan_date": "2026-04-02",
            "merchant": "ローソン",
            "total": 1280.0,
            "currency": "JPY",
            "category": "general",
            "receipt_dir": "general/20260402_0900.pdf",
            "raw_text": "ローソン\n合計 1280円",
            "processed_by": "tesseract",
        },
    )
    generate_reports(conn, output_dir, fmt="both", receipt_root=tmp_path / "receipts")
    conn.close()

    md = (output_dir / "intake-report.md").read_text(encoding="utf-8")
    html = (output_dir / "intake-report.html").read_text(encoding="utf-8")

    assert "| Total Spend | $10.00 |" in md
    assert "¥1,280" in md
    assert "¥1,280" in html
    assert "$1,280.00" not in md
    assert "$1,280.00" not in html


def test_generate_reports_render_certifications_on_merchant_page(tmp_path):
    db_path = tmp_path / "intake.db"
    output_dir = tmp_path / "reports"
    receipt_root = tmp_path / "receipts"

    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "20110830_1149.pdf",
            "scan_date": "2011-08-30",
            "merchant": "heritage collision",
            "total": None,
            "category": "auto/heritage-collision",
            "receipt_dir": "auto/heritage-collision/20110830_1149.pdf",
            "raw_text": "Certified by AAA",
            "processed_by": "gpt-4.1",
            "certifications": [
                {
                    "issuer": "AAA",
                    "label": "AAA certified",
                    "source_text": "Certified by AAA",
                }
            ],
        },
    )
    conn.close()

    pdf = receipt_root / "auto" / "heritage-collision" / "20110830_1149.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_text("pdf placeholder", encoding="utf-8")

    generate_reports(conn := init_db(db_path), output_dir, fmt="both", receipt_root=receipt_root)
    conn.close()

    merchant_detail_md = (output_dir / "merchants" / "heritage-collision.md").read_text(
        encoding="utf-8"
    )
    merchant_detail_html = (output_dir / "merchants" / "heritage-collision.html").read_text(
        encoding="utf-8"
    )

    assert "## Certifications" in merchant_detail_md
    assert (
        "| AAA certified | AAA | 1 | Certified by AAA on [20110830_1149.pdf]" in merchant_detail_md
    )
    assert "| 2011-08-30 | [auto/heritage-collision]" in merchant_detail_md
    assert "| AAA certified |" in merchant_detail_md

    assert (
        "Structured merchant certifications and claims captured from receipt evidence."
        in merchant_detail_html
    )
    assert "AAA certified" in merchant_detail_html
    assert "Certified by AAA" in merchant_detail_html
    assert _browser_uri("auto", "heritage-collision", "20110830_1149.pdf") in merchant_detail_html
