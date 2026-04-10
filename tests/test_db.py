"""Tests for receipt update audit helpers."""

from intake.db import (
    get_receipt,
    get_receipt_by_file_hash,
    infer_purchaser_from_card_last4,
    init_db,
    query_receipt_updates,
    update_receipt_fields,
    upsert_receipt,
)


def test_update_receipt_fields_records_audit_entry(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260409_1707.pdf",
            "scan_date": "2026-04-09",
            "merchant": "kroger",
            "total": 86.05,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260409_1707.pdf",
            "raw_text": "BALANCE 86.05\nMASTERCARD 88.05",
            "processed_by": "tesseract",
        },
    )

    before = get_receipt(conn, "20260409_1707.pdf")
    assert before is not None

    updated, changes = update_receipt_fields(
        conn,
        "20260409_1707.pdf",
        {"total": 88.05},
        source="manual-correct",
        note="card charge wins over balance line",
    )

    assert updated["total"] == 88.05
    assert changes["total"] == {"old": 86.05, "new": 88.05}
    assert "processed_at" in changes
    assert updated["processed_at"] != before["processed_at"]

    history = query_receipt_updates(conn, filename="20260409_1707.pdf")
    assert len(history) == 1
    assert history[0]["source"] == "manual-correct"
    assert history[0]["note"] == "card charge wins over balance line"
    assert history[0]["changes"]["total"] == {"old": 86.05, "new": 88.05}


def test_update_receipt_fields_noop_does_not_record_audit(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260409_1707.pdf",
            "scan_date": "2026-04-09",
            "merchant": "kroger",
            "total": 88.05,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260409_1707.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
        },
    )

    updated, changes = update_receipt_fields(
        conn,
        "20260409_1707.pdf",
        {"merchant": "kroger"},
        source="manual-correct",
        note="noop",
    )

    assert updated["merchant"] == "kroger"
    assert changes == {}
    assert query_receipt_updates(conn, filename="20260409_1707.pdf") == []


def test_get_receipt_by_file_hash_returns_matching_row(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260409_1707.pdf",
            "scan_date": "2026-04-09",
            "merchant": "kroger",
            "total": 88.05,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260409_1707.pdf",
            "file_hash": "abc123",
            "raw_text": "",
            "processed_by": "tesseract",
        },
    )

    row = get_receipt_by_file_hash(conn, "abc123")

    assert row is not None
    assert row["filename"] == "20260409_1707.pdf"
    assert row["file_hash"] == "abc123"


def test_infer_purchaser_from_card_last4_returns_most_recent_known_holder(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20250711_1814.pdf",
            "scan_date": "2025-07-11",
            "merchant": "autozone",
            "total": 10.0,
            "category": "auto/az",
            "receipt_dir": "auto/az/20250711_1814.pdf",
            "card_last4": "5653",
            "purchaser": "cason konzer",
            "raw_text": "",
            "processed_by": "tesseract",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260406_0902.pdf",
            "scan_date": "2026-04-09",
            "merchant": "mcdonald's",
            "total": 6.13,
            "category": "dining/fast-food",
            "receipt_dir": "dining/fast-food/20260406_0902.pdf",
            "card_last4": "5653",
            "purchaser": "cason",
            "raw_text": "",
            "processed_by": "gpt-4.1",
        },
    )

    purchaser = infer_purchaser_from_card_last4(conn, "5653")

    assert purchaser == "cason konzer"


def test_update_receipt_fields_normalizes_purchaser_alias(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260409_1707.pdf",
            "scan_date": "2026-04-09",
            "merchant": "kroger",
            "total": 88.05,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260409_1707.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
        },
    )

    updated, changes = update_receipt_fields(
        conn,
        "20260409_1707.pdf",
        {"purchaser": "casonkonzer@gmail.com"},
        source="manual-correct",
        note="normalize purchaser alias",
    )

    assert updated["purchaser"] == "cason konzer"
    assert changes["purchaser"] == {"old": None, "new": "cason konzer"}
