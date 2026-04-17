"""Tests for receipt update audit helpers."""

from intake.db import (
    get_receipt,
    get_receipt_by_file_hash,
    infer_purchaser_from_card_last4,
    init_db,
    query_flagged,
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


def test_upsert_receipt_round_trips_items(tmp_path):
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
            "raw_text": "Bananas 2.99\nMilk 4.39",
            "processed_by": "tesseract",
            "items": [
                {"description": "Bananas", "amount": 2.99},
                {"description": "Milk", "amount": 4.39},
            ],
        },
    )

    row = get_receipt(conn, "20260409_1707.pdf")

    assert row is not None
    assert row["items"] == [
        {"description": "Bananas", "amount": 2.99, "quantity": None, "unit_price": None},
        {"description": "Milk", "amount": 4.39, "quantity": None, "unit_price": None},
    ]


def test_update_receipt_fields_records_itemization_attempt(tmp_path):
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
        {"items": []},
        source="itemize-backfill",
        note="attempted itemized breakdown from stored OCR",
    )

    assert updated["items"] == []
    assert changes["items"] == {"old": None, "new": []}


def test_upsert_receipt_round_trips_certifications(tmp_path):
    conn = init_db(tmp_path / "intake.db")
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

    row = get_receipt(conn, "20110830_1149.pdf")

    assert row is not None
    assert row["certifications"] == [
        {"issuer": "AAA", "label": "AAA certified", "source_text": "Certified by AAA"}
    ]


def test_update_receipt_fields_records_certification_changes(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20110830_1149.pdf",
            "scan_date": "2011-08-30",
            "merchant": "heritage collision",
            "total": None,
            "category": "auto/heritage-collision",
            "receipt_dir": "auto/heritage-collision/20110830_1149.pdf",
            "raw_text": "",
            "processed_by": "gpt-4.1",
        },
    )

    updated, changes = update_receipt_fields(
        conn,
        "20110830_1149.pdf",
        {
            "certifications": [
                {
                    "issuer": "AAA",
                    "label": "AAA certified",
                    "source_text": "Certified by AAA",
                }
            ]
        },
        source="manual-correct",
        note="record aaa certification",
    )

    assert updated["certifications"] == [
        {"issuer": "AAA", "label": "AAA certified", "source_text": "Certified by AAA"}
    ]
    assert changes["certifications"] == {
        "old": None,
        "new": [{"issuer": "AAA", "label": "AAA certified", "source_text": "Certified by AAA"}],
    }


def test_query_flagged_includes_reason_codes_for_basic_issues(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260409_1707.pdf",
            "scan_date": "2026-04-09",
            "merchant": "ab",
            "total": None,
            "category": "general",
            "receipt_dir": "general/20260409_1707.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
        },
    )

    flagged = query_flagged(conn)

    assert len(flagged) == 1
    assert flagged[0]["filename"] == "20260409_1707.pdf"
    assert flagged[0]["review_reasons"] == ["short_merchant", "missing_total"]
    assert flagged[0]["review_reason_summary"] == "short_merchant, missing_total"


def test_query_flagged_adds_recent_novelty_and_outlier_reasons(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    historical_rows = [
        ("20260301_0900.pdf", "2026-03-01", "kroger", 10.0, "5653"),
        ("20260305_0900.pdf", "2026-03-05", "kroger", 11.0, "5653"),
        ("20260310_0900.pdf", "2026-03-10", "kroger", 12.0, "5653"),
    ]
    for filename, scan_date, merchant, total, card_last4 in historical_rows:
        upsert_receipt(
            conn,
            {
                "filename": filename,
                "scan_date": scan_date,
                "merchant": merchant,
                "total": total,
                "category": "grocery/supermarket",
                "receipt_dir": f"grocery/supermarket/{filename}",
                "raw_text": "",
                "processed_by": "tesseract",
                "card_last4": card_last4,
            },
        )

    upsert_receipt(
        conn,
        {
            "filename": "20260411_1200.pdf",
            "scan_date": "2026-04-11",
            "merchant": "new shop",
            "total": 55.0,
            "category": "general",
            "receipt_dir": "general/20260411_1200.pdf",
            "raw_text": "",
            "processed_by": "gpt-4.1",
            "card_last4": "7464",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260412_1200.pdf",
            "scan_date": "2026-04-12",
            "merchant": "kroger",
            "total": 400.0,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260412_1200.pdf",
            "raw_text": "",
            "processed_by": "gpt-4.1",
            "card_last4": "5653",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20240101_0900.pdf",
            "scan_date": "2024-01-01",
            "merchant": "old unique merchant",
            "total": 20.0,
            "category": "general",
            "receipt_dir": "general/20240101_0900.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
            "card_last4": "9999",
        },
    )

    flagged = {row["filename"]: row for row in query_flagged(conn)}

    assert "20260411_1200.pdf" in flagged
    assert flagged["20260411_1200.pdf"]["review_reasons"] == [
        "new_merchant",
        "new_card_last4",
        "new_merchant_card_pair",
    ]
    assert "20260412_1200.pdf" in flagged
    assert flagged["20260412_1200.pdf"]["review_reasons"] == [
        "total_over_250",
        "amount_outlier_for_merchant (prior median $11.00)",
    ]
    assert "20240101_0900.pdf" not in flagged


def test_query_flagged_adds_total_guard_threshold_reasons(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20240101_0900.pdf",
            "scan_date": "2024-01-01",
            "merchant": "travel co",
            "total": 1200.0,
            "category": "general",
            "receipt_dir": "general/20240101_0900.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
            "card_last4": "5653",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260412_1200.pdf",
            "scan_date": "2026-04-12",
            "merchant": "baseline shop",
            "total": 10.0,
            "category": "general",
            "receipt_dir": "general/20260412_1200.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
            "card_last4": "1111",
        },
    )

    flagged = {row["filename"]: row for row in query_flagged(conn)}

    assert flagged["20240101_0900.pdf"]["review_reasons"] == [
        "total_over_250",
        "total_over_500",
        "total_over_750",
        "total_over_1000",
    ]
