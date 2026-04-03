"""Unit tests for intake.email (HTML stripping, dedup, safe-save, receipt heuristic)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from intake.email import (
    FetchResult,
    _looks_like_receipt,
    _safe_save,
    html_to_text,
)
from intake.db import init_db, is_email_processed, record_email_receipt

# ── html_to_text ──────────────────────────────────────────────────────────────


def test_html_to_text_strips_tags():
    html = "<p>Hello <b>world</b></p>"
    assert html_to_text(html) == "Hello world"


def test_html_to_text_drops_script_and_style():
    html = "<p>Keep this</p><script>alert(1)</script><style>.x{}</style><p>And this</p>"
    text = html_to_text(html)
    assert "Keep this" in text
    assert "And this" in text
    assert "alert" not in text
    assert ".x{}" not in text


def test_html_to_text_preserves_newlines_at_block_boundaries():
    html = "<div>Line A</div><div>Line B</div>"
    text = html_to_text(html)
    assert "Line A" in text
    assert "Line B" in text
    # They must appear on separate lines
    assert text.index("Line A") < text.index("Line B")


def test_html_to_text_collapses_whitespace():
    html = "<p>  too   many   spaces  </p>"
    assert html_to_text(html) == "too many spaces"


def test_html_to_text_empty_string():
    assert html_to_text("") == ""


def test_html_to_text_plain_text_passthrough():
    # No tags — should come back clean
    assert html_to_text("Hello world") == "Hello world"


# ── _looks_like_receipt ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Order total: $42.00",
        "Amount Due: $9.99",
        "Invoice #1234",
        "Receipt #007",
        "You have been charged $15.00",
        "Subtotal: $5.00",
        "Order Total $100.00",
    ],
)
def test_looks_like_receipt_true(text):
    assert _looks_like_receipt(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Hi there, just checking in!",
        "Your account has been updated.",
        "Newsletter: 10 tips for better sleep",
    ],
)
def test_looks_like_receipt_false(text):
    assert _looks_like_receipt(text) is False


# ── _safe_save ────────────────────────────────────────────────────────────────


def test_safe_save_writes_file(tmp_path):
    dest = _safe_save(tmp_path, "receipt.pdf", b"%PDF-1.4")
    assert dest.exists()
    assert dest.read_bytes() == b"%PDF-1.4"


def test_safe_save_sanitises_filename(tmp_path):
    dest = _safe_save(tmp_path, "re ce:ipt?.pdf", b"data")
    # No colon or question mark in filename
    assert ":" not in dest.name
    assert "?" not in dest.name
    assert dest.exists()


def test_safe_save_avoids_clobber(tmp_path):
    first = _safe_save(tmp_path, "receipt.pdf", b"first")
    second = _safe_save(tmp_path, "receipt.pdf", b"second")
    assert first != second
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"


def test_safe_save_empty_filename_fallback(tmp_path):
    # Empty string sanitises to "" which is falsy → falls back to "attachment.pdf"
    dest = _safe_save(tmp_path, "", b"data")
    assert dest.name == "attachment.pdf"
    assert dest.exists()


# ── DB deduplication helpers ──────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def test_is_email_processed_false_initially(db):
    assert is_email_processed(db, uid=12345, mailbox="INBOX") is False


def test_record_and_check_email_processed(db):
    record_email_receipt(
        db,
        uid=12345,
        mailbox="INBOX",
        message_id="<abc@mail.example.com>",
        subject="Your receipt",
        sender="shop@example.com",
        received_at="Thu, 01 Jan 2026 12:00:00 +0000",
        attachment_count=1,
    )
    assert is_email_processed(db, uid=12345, mailbox="INBOX") is True


def test_different_uid_not_processed(db):
    record_email_receipt(
        db,
        uid=12345,
        mailbox="INBOX",
        message_id="<abc@mail.example.com>",
        subject="Your receipt",
        sender="shop@example.com",
        received_at="",
        attachment_count=0,
    )
    assert is_email_processed(db, uid=99999, mailbox="INBOX") is False


def test_different_mailbox_not_processed(db):
    record_email_receipt(
        db,
        uid=12345,
        mailbox="INBOX",
        message_id="<abc@mail.example.com>",
        subject="Your receipt",
        sender="shop@example.com",
        received_at="",
        attachment_count=0,
    )
    assert is_email_processed(db, uid=12345, mailbox="[Gmail]/Sent") is False


def test_duplicate_record_does_not_raise(db):
    kwargs = dict(
        uid=12345,
        mailbox="INBOX",
        message_id="<abc@mail.example.com>",
        subject="Your receipt",
        sender="shop@example.com",
        received_at="",
        attachment_count=0,
    )
    record_email_receipt(db, **kwargs)
    record_email_receipt(db, **kwargs)  # second call must not raise
    assert is_email_processed(db, uid=12345, mailbox="INBOX") is True


# ── FetchResult ───────────────────────────────────────────────────────────────


def test_fetch_result_defaults():
    r = FetchResult()
    assert r.found == 0
    assert r.skipped == 0
    assert r.attachments_saved == 0
    assert r.html_processed == 0
    assert r.errors == 0
    assert r.saved_files == []
