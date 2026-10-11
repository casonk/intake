"""Unit tests for intake.reference_ids."""

from __future__ import annotations

from intake.reference_ids import extract_reference_ids


def test_extracts_eft_number():
    text = "Payment date May 12, 2026\nEFT number 900000000000015\nAmount $108.10"
    assert extract_reference_ids(text) == {"eft_number": "900000000000015"}


def test_extracts_transaction_id():
    text = "Transaction ID: 2B462505U85115351\nYou sent a payment of $217.30 USD"
    assert extract_reference_ids(text) == {"transaction_id": "2B462505U85115351"}


def test_extracts_confirmation_number():
    text = "Confirmation Number:\nB12282351431220125\nTotal Amount:\n$108.10"
    assert extract_reference_ids(text) == {"confirmation_number": "B12282351431220125"}


def test_extracts_order_number_without_hash_or_word():
    text = "Order 1000000001\nlaaltindows Windows 10 Pro for Workstations $205.00"
    assert extract_reference_ids(text) == {"order_number": "1000000001"}


def test_extracts_invoice_number():
    text = "INVOICE #\nS-PK944EGZGJ-2606\nSERVICE\nFOR\nJune 2026"
    assert extract_reference_ids(text) == {"invoice_number": "S-PK944EGZGJ-2606"}


def test_does_not_match_order_total_line():
    assert extract_reference_ids("Order Total 47.20") == {}


def test_does_not_match_prose_mentioning_invoice():
    assert extract_reference_ids("Invoice paid for June 2026") == {}


def test_no_match_returns_empty_dict():
    assert extract_reference_ids("Just a regular receipt with no IDs") == {}


def test_empty_text_returns_empty_dict():
    assert extract_reference_ids("") == {}
