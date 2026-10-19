"""Local email triage must not turn payment plans into spending."""

import io
import json

import pytest

from intake.db import init_db
from intake.email import _looks_like_receipt, _upsert_html_receipt
from intake.email_local_classifier import (
    classify_ambiguous_email,
    is_legal_payment_correspondence,
)

_BODY = (
    "Mr. Reader,\nI can make time at 1pm on the 21st or 22nd, "
    "or bank transfer would be fine. Our office does not support invoices.\n"
    "弁護士 佐藤\n差出人: Reader\nI would like to deliver a cash payment for the invoice."
)


@pytest.fixture()
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def test_legal_payment_discussion_blocks_receipt_heuristic():
    assert is_legal_payment_correspondence("Re: cash payment", _BODY)
    assert _looks_like_receipt(_BODY)
    assert not is_legal_payment_correspondence(
        "Payment confirmation", "Your payment of $25 was received by the bank."
    )
    assert not is_legal_payment_correspondence(
        "Law office payment", "The attorney confirms your payment was received."
    )


def test_local_classifier_only_routes_unpaid_legal_correspondence(monkeypatch):
    results = iter(
        [
            {"kind": "legal_correspondence", "payment_completed": False},
            {"kind": "legal_correspondence", "payment_completed": True},
            {"kind": "financial_notice", "payment_completed": False},
        ]
    )

    def fake_urlopen(_request, timeout):
        assert timeout == 45
        return io.BytesIO(json.dumps({"response": json.dumps(next(results))}).encode())

    monkeypatch.setattr("intake.email_local_classifier.urlopen", fake_urlopen)
    settings = {"email": {}}
    args = ("Re: cash payment", "Counsel <counsel@example.com>", _BODY, settings)
    document = classify_ambiguous_email(*args)
    assert document["document_kind"] == "letters/legal"
    assert document["merchant"] == "Counsel"
    assert "bank transfer" in document["body_excerpt"]
    assert classify_ambiguous_email(*args) is None
    assert classify_ambiguous_email(*args) is None


def test_legal_payment_thread_is_stored_as_document_without_spend(db, tmp_path, monkeypatch):
    monkeypatch.setattr("intake.classify.load_rules", lambda: {})
    monkeypatch.setattr("intake.classify.load_merchant_aliases", lambda: {})
    document = {
        "document_kind": "letters/legal",
        "title": "Re: cash payment",
        "merchant": "Counsel",
        "subject": "Re: cash payment",
        "body_excerpt": "Bank transfer would be fine; no invoice was issued.",
        "processed_by": "email-local-classifier:llama3.2:3b-instruct-q8_0",
    }
    _upsert_html_receipt(
        db,
        34872,
        "INBOX",
        document["title"],
        "Counsel <counsel@example.com>",
        "Sun, 18 Oct 2026 05:25:10 +0900",
        _BODY,
        {"receipt_dir": str(tmp_path)},
        structured_email=document,
    )
    row = db.execute("SELECT * FROM documents WHERE filename = 'email_34872_INBOX.txt'").fetchone()
    assert row is not None
    assert row["document_kind"] == "letters/legal"
    assert (
        db.execute(
            "SELECT count(*) FROM receipts WHERE filename = 'email_34872_INBOX.txt'"
        ).fetchone()[0]
        == 0
    )
