"""Tests for receipt filename canonicalization helpers."""

from __future__ import annotations

import os
from datetime import date, datetime

from intake.naming import (
    allocate_canonical_path,
    canonical_filename_for_datetime,
    infer_artifact_datetime,
    is_canonical_receipt_filename,
)
from intake.parse import Receipt


def test_is_canonical_receipt_filename():
    assert is_canonical_receipt_filename("20260409_1707.pdf") is True
    assert is_canonical_receipt_filename("20260409.pdf") is False
    assert is_canonical_receipt_filename("20260409-1.pdf") is False


def test_canonical_filename_for_datetime():
    assert (
        canonical_filename_for_datetime(datetime(2026, 4, 9, 17, 7), ".PDF") == "20260409_1707.pdf"
    )


def test_infer_artifact_datetime_uses_name_date_and_mtime_time(tmp_path):
    pdf = tmp_path / "20260409.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ts = datetime(2026, 4, 10, 18, 34).timestamp()
    os.utime(pdf, (ts, ts))

    inferred = infer_artifact_datetime(filename=pdf.name, path=pdf)

    assert inferred == datetime(2026, 4, 9, 18, 34)


def test_allocate_canonical_path_advances_by_minute_on_collision(tmp_path):
    first = tmp_path / "20260409_1707.pdf"
    first.write_bytes(b"%PDF-1.4\n")

    allocated = allocate_canonical_path(
        tmp_path,
        base_dt=datetime(2026, 4, 9, 17, 7),
        suffix=".pdf",
    )

    assert allocated.name == "20260409_1708.pdf"


def test_receipt_suggested_filename_uses_receipt_time():
    receipt = Receipt(
        filename="scan.pdf",
        scan_date=date(2026, 4, 9),
        receipt_date=date(2026, 4, 8),
        receipt_time=datetime(2026, 4, 8, 13, 45).time(),
        merchant="kroger",
        total=1.0,
        raw_text="",
    )

    assert receipt.suggested_filename() == "20260408_1345.pdf"


def test_receipt_suggested_filename_uses_source_time_when_receipt_time_missing(tmp_path):
    pdf = tmp_path / "20260409.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ts = datetime(2026, 4, 10, 18, 34).timestamp()
    os.utime(pdf, (ts, ts))

    receipt = Receipt(
        filename=pdf.name,
        scan_date=date(2026, 4, 9),
        receipt_date=date(2026, 4, 9),
        receipt_time=None,
        merchant="kroger",
        total=1.0,
        raw_text="",
    )

    assert receipt.suggested_filename(source_path=pdf) == "20260409_1834.pdf"
