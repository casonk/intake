"""Tests for receipt update audit helpers."""

from intake.db import (
    dismiss_reference_match,
    get_receipt,
    get_receipt_by_file_hash,
    infer_purchaser_from_card_last4,
    init_db,
    query_all_receipts,
    query_dismissed_reference_matches,
    query_flagged,
    query_receipt_updates,
    query_reference_duplicate_groups,
    record_receipt_update,
    undismiss_reference_match,
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


def test_query_all_receipts_includes_currency(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260621_1522.jpg",
            "scan_date": "2026-06-21",
            "merchant": "adidas japan",
            "total": 14300.0,
            "category": "general",
            "receipt_dir": "general/20260621_1522.jpg",
            "raw_text": "",
            "processed_by": "gpt-4.1",
            "currency": "JPY",
        },
    )

    rows = query_all_receipts(conn)

    assert len(rows) == 1
    assert rows[0]["currency"] == "JPY"


def test_query_reference_duplicate_groups_finds_matching_eft_number(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260511_1243.pdf",
            "scan_date": "2026-05-11",
            "merchant": "irs",
            "total": 108.10,
            "category": "general",
            "receipt_dir": "general/20260511_1243.pdf",
            "raw_text": "Payment date May 12, 2026\nEFT number 900000000000015",
            "processed_by": "gpt-4.1",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "email_10001_INBOX.txt",
            "scan_date": "2026-05-12",
            "merchant": "irs",
            "total": 108.10,
            "category": "general",
            "receipt_dir": "email/general/email_10001_INBOX.txt",
            "raw_text": "Confirmation of Payment Transaction\nEFT#:\n900000000000015",
            "processed_by": "email-parse",
        },
    )
    # An unrelated receipt with no shared reference ID must not appear.
    upsert_receipt(
        conn,
        {
            "filename": "20260601_0900.pdf",
            "scan_date": "2026-06-01",
            "merchant": "kroger",
            "total": 12.34,
            "category": "grocery",
            "receipt_dir": "grocery/20260601_0900.pdf",
            "raw_text": "Kroger\nTotal 12.34",
            "processed_by": "tesseract",
        },
    )

    groups = query_reference_duplicate_groups(conn)

    assert len(groups) == 1
    kind, ref_id, rows = groups[0]
    assert kind == "eft_number"
    assert ref_id == "900000000000015"
    assert {r["filename"] for r in rows} == {"20260511_1243.pdf", "email_10001_INBOX.txt"}


def test_dismissed_reference_match_is_excluded_from_default_query(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20010108_1600.pdf",
            "scan_date": "2001-01-08",
            "merchant": "tamiami ford inc.",
            "total": 267.46,
            "category": "auto/dealer/ford",
            "receipt_dir": "auto/dealer/ford/20010108_1600.pdf",
            "raw_text": "INVOICE DATE INVOICE NO.\n11489 WILLIAM BENNET",
            "processed_by": "tesseract",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20031128_1233.pdf",
            "scan_date": "2003-11-28",
            "merchant": "tamiami ford inc.",
            "total": 2497.02,
            "category": "auto/dealer/ford",
            "receipt_dir": "auto/dealer/ford/20031128_1233.pdf",
            "raw_text": "INVOICE DATE INVOICE NO.\n11489 WILLIAM BENNET",
            "processed_by": "tesseract",
        },
    )

    groups_before = query_reference_duplicate_groups(conn)
    assert len(groups_before) == 1
    kind, ref_id, _rows = groups_before[0]
    assert (kind, ref_id) == ("invoice_number", "11489")

    dismiss_reference_match(conn, kind, ref_id, note="recurring tag number, not a real invoice ID")

    assert query_reference_duplicate_groups(conn) == []
    assert query_reference_duplicate_groups(conn, include_dismissed=True) == groups_before

    dismissed = query_dismissed_reference_matches(conn)
    assert len(dismissed) == 1
    assert dismissed[0]["kind"] == "invoice_number"
    assert dismissed[0]["ref_id"] == "11489"
    assert dismissed[0]["note"] == "recurring tag number, not a real invoice ID"

    undismiss_reference_match(conn, kind, ref_id)
    assert query_reference_duplicate_groups(conn) == groups_before


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
            "purchaser": "jane sample",
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
            "purchaser": "jane",
            "raw_text": "",
            "processed_by": "gpt-4.1",
        },
    )

    purchaser = infer_purchaser_from_card_last4(conn, "5653")

    assert purchaser == "jane sample"


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
        {"purchaser": "janesample@gmail.com"},
        source="manual-correct",
        note="normalize purchaser alias",
    )

    assert updated["purchaser"] == "jane sample"
    assert changes["purchaser"] == {"old": None, "new": "jane sample"}


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


def test_upsert_receipt_round_trips_language_and_translation(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260409_1707.pdf",
            "scan_date": "2026-04-09",
            "merchant": "ローソン",
            "total": 1280.0,
            "category": "general",
            "receipt_dir": "general/20260409_1707.pdf",
            "raw_text": "ローソン\n合計 1280円",
            "processed_by": "gpt-4o-mini",
            "currency": "JPY",
            "language": "ja",
            "translated_text": "Lawson\nTotal 1,280 yen",
        },
    )

    row = get_receipt(conn, "20260409_1707.pdf")

    assert row is not None
    assert row["language"] == "ja"
    assert row["translated_text"] == "Lawson\nTotal 1,280 yen"


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


def test_upsert_receipt_round_trips_staff(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260421_1842.jpg",
            "scan_date": "2026-04-21",
            "merchant": "smokin outlaws bbq",
            "total": 9.2,
            "category": "dining",
            "receipt_dir": "dining/20260421_1842.jpg",
            "raw_text": "Server: Isabel J",
            "processed_by": "gpt-4o-mini",
            "staff": [{"role": "Server", "name": "Isabel J", "source_text": "Server: Isabel J"}],
        },
    )

    row = get_receipt(conn, "20260421_1842.jpg")

    assert row is not None
    assert row["staff"] == [
        {"role": "server", "name": "isabel j", "source_text": "Server: Isabel J"}
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


def test_update_receipt_fields_records_staff_changes(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260421_1842.jpg",
            "scan_date": "2026-04-21",
            "merchant": "smokin outlaws bbq",
            "total": 9.2,
            "category": "dining",
            "receipt_dir": "dining/20260421_1842.jpg",
            "raw_text": "",
            "processed_by": "gpt-4o-mini",
        },
    )

    updated, changes = update_receipt_fields(
        conn,
        "20260421_1842.jpg",
        {"staff": [{"role": "server", "name": "Isabel J", "source_text": "Server: Isabel J"}]},
        source="staff-backfill",
        note="extract staff",
    )

    assert updated["staff"] == [
        {"role": "server", "name": "isabel j", "source_text": "Server: Isabel J"}
    ]
    assert changes["staff"] == {
        "old": None,
        "new": [{"role": "server", "name": "isabel j", "source_text": "Server: Isabel J"}],
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


def test_query_flagged_flags_receipt_billed_to_other_party(tmp_path):
    from intake.purchaser import register_purchaser_aliases

    register_purchaser_aliases({"jordan avery": ["jordan", "jordan lee avery"]})
    try:
        conn = init_db(tmp_path / "intake.db")
        upsert_receipt(
            conn,
            {
                "filename": "20110714_1356.pdf",
                "scan_date": "2011-07-14",
                "merchant": "keystone automotive industries",
                "total": 98.58,
                "category": "auto",
                "receipt_dir": "auto/20110714_1356.pdf",
                "raw_text": (
                    "BILL TO\n100200\nSAMPLE AUTO REPAIR (KK)\n"
                    "100 EXAMPLE RD\nANYTOWN, MI 48000-0000"
                ),
                "processed_by": "tesseract",
            },
        )

        flagged = query_flagged(conn)

        assert len(flagged) == 1
        assert "billed_to_other_party (SAMPLE AUTO REPAIR (KK))" in flagged[0]["review_reasons"]
        assert "raw_text" not in flagged[0]
    finally:
        register_purchaser_aliases({})


def test_query_flagged_skips_self_referential_bill_to(tmp_path):
    from intake.purchaser import register_purchaser_aliases

    register_purchaser_aliases({"jordan avery": ["jordan"]})
    try:
        conn = init_db(tmp_path / "intake.db")
        for filename, scan_date in (
            ("20260222_2241.pdf", "2026-02-22"),
            ("20260322_2241.pdf", "2026-03-22"),
        ):
            upsert_receipt(
                conn,
                {
                    "filename": filename,
                    "scan_date": scan_date,
                    "merchant": "anthropic",
                    "total": 200.0,
                    "category": "sw/dev",
                    "receipt_dir": f"sw/dev/{filename}",
                    "raw_text": "Bill To\nAnthropic, PBC\n548 Market Street",
                    "processed_by": "tesseract",
                },
            )

        flagged = query_flagged(conn)

        all_reasons = [reason for row in flagged for reason in row["review_reasons"]]
        assert not any(reason.startswith("billed_to_other_party") for reason in all_reasons)
    finally:
        register_purchaser_aliases({})


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


def test_query_flagged_ignores_resolved_email_reply_note(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260401_1200.jpg",
            "scan_date": "2026-04-01",
            "merchant": "the keg restaurant",
            "total": 44.0,
            "category": "dining/sit-down",
            "receipt_dir": "dining/sit-down/20260401_1200.jpg",
            "raw_text": "",
            "processed_by": "tesseract",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260420_2202.jpg",
            "scan_date": "2026-04-20",
            "merchant": "the keg",
            "total": 66.0,
            "category": "dining/sit-down",
            "receipt_dir": "dining/sit-down/20260420_2202.jpg",
            "raw_text": "",
            "processed_by": "tesseract",
        },
    )
    record_receipt_update(
        conn,
        "20260420_2202.jpg",
        source="email-reply-note",
        note="Merchant is invalid",
        changes={"email_reply": {"old": None, "new": "Merchant is invalid"}},
        updated_at="2026-04-24T23:46:03+00:00",
    )
    update_receipt_fields(
        conn,
        "20260420_2202.jpg",
        {"merchant": "the keg restaurant"},
        source="manual-correct",
        note="resolved reply request",
    )

    flagged = {row["filename"]: row for row in query_flagged(conn)}

    assert "20260420_2202.jpg" not in flagged


def test_query_flagged_keeps_unresolved_email_reply_note(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260401_1200.jpg",
            "scan_date": "2026-04-01",
            "merchant": "the keg",
            "total": 44.0,
            "category": "dining/sit-down",
            "receipt_dir": "dining/sit-down/20260401_1200.jpg",
            "raw_text": "",
            "processed_by": "tesseract",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260420_2202.jpg",
            "scan_date": "2026-04-20",
            "merchant": "the keg",
            "total": 66.0,
            "category": "dining/sit-down",
            "receipt_dir": "dining/sit-down/20260420_2202.jpg",
            "raw_text": "",
            "processed_by": "tesseract",
        },
    )
    record_receipt_update(
        conn,
        "20260420_2202.jpg",
        source="email-reply-note",
        note="Merchant is invalid",
        changes={"email_reply": {"old": None, "new": "Merchant is invalid"}},
    )

    flagged = {row["filename"]: row for row in query_flagged(conn)}

    assert (
        "email_reply_request (Merchant is invalid)"
        in flagged["20260420_2202.jpg"]["review_reasons"]
    )


def test_currency_stored_and_retrieved(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260401_1000.pdf",
            "scan_date": "2026-04-01",
            "merchant": "canadian tire",
            "total": 47.23,
            "category": "auto",
            "receipt_dir": "auto/20260401_1000.pdf",
            "raw_text": "Canadian Tire\nTotal: 47.23 CAD",
            "processed_by": "tesseract",
            "currency": "CAD",
        },
    )
    row = get_receipt(conn, "20260401_1000.pdf")
    assert row is not None
    assert row["currency"] == "CAD"


def test_currency_not_usd_flagged_in_review(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260401_1000.pdf",
            "scan_date": "2026-04-01",
            "merchant": "canadian tire",
            "total": 47.23,
            "category": "auto",
            "receipt_dir": "auto/20260401_1000.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
            "currency": "CAD",
        },
    )
    flagged = {row["filename"]: row for row in query_flagged(conn)}
    assert "20260401_1000.pdf" in flagged
    assert any("currency_not_usd" in r for r in flagged["20260401_1000.pdf"]["review_reasons"])


def test_currency_usd_not_flagged_for_currency(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260401_1000.pdf",
            "scan_date": "2026-04-01",
            "merchant": "target",
            "total": 23.50,
            "category": "grocery",
            "receipt_dir": "grocery/20260401_1000.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
            "currency": "USD",
        },
    )
    flagged = {row["filename"]: row for row in query_flagged(conn)}
    if "20260401_1000.pdf" in flagged:
        reasons = flagged["20260401_1000.pdf"]["review_reasons"]
        assert not any("currency_not_usd" in r for r in reasons)


def test_currency_can_be_corrected(tmp_path):
    conn = init_db(tmp_path / "intake.db")
    upsert_receipt(
        conn,
        {
            "filename": "20260401_1000.pdf",
            "scan_date": "2026-04-01",
            "merchant": "some shop",
            "total": 30.00,
            "category": "general",
            "receipt_dir": "general/20260401_1000.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
            "currency": "CAD",
        },
    )
    updated, changes = update_receipt_fields(
        conn, "20260401_1000.pdf", {"currency": "USD"}, source="manual-correct"
    )
    assert updated["currency"] == "USD"
    assert "currency" in changes
