"""Unit tests for intake.notify."""

from __future__ import annotations

import sys
from datetime import date, time

import pytest

from intake.notify import (
    format_processed_receipt_message,
    format_processed_receipt_subject,
    gmail_notifications_enabled,
    send_processed_receipt_gmail_notification,
    send_processed_receipt_notification,
    send_processed_receipt_notifications,
    signal_notifications_enabled,
)
from intake.parse import Receipt


def _receipt() -> Receipt:
    return Receipt(
        filename="20260401_1000.pdf",
        scan_date=date(2026, 4, 1),
        receipt_date=date(2026, 3, 18),
        receipt_time=time(18, 34),
        merchant="kroger",
        total=4.33,
        raw_text="raw",
        purchaser="john doe",
        card_last4="4321",
    )


def test_signal_notifications_enabled_with_minimal_config():
    settings = {
        "signal": {
            "shock_relay_config": "/tmp/config.local.yaml",
            "recipient": "+15551234567",
        }
    }
    assert signal_notifications_enabled(settings) is True


def test_signal_notifications_disabled_without_config():
    assert signal_notifications_enabled({}) is False
    assert signal_notifications_enabled({"signal": {"enabled": False}}) is False


def test_signal_notifications_enabled_for_note_to_self():
    settings = {
        "signal": {
            "shock_relay_config": "/tmp/config.local.yaml",
            "note_to_self": True,
        }
    }
    assert signal_notifications_enabled(settings) is True


def test_gmail_notifications_enabled_with_email_config_fallback():
    settings = {
        "email": {
            "shock_relay_config": "/tmp/gmail-imap/config.local.yaml",
        },
        "gmail": {
            "recipient": "me@example.com",
        },
    }
    assert gmail_notifications_enabled(settings) is True


def test_gmail_notifications_disabled_without_recipient():
    assert gmail_notifications_enabled({}) is False
    assert gmail_notifications_enabled({"gmail": {"enabled": False}}) is False


def test_format_processed_receipt_message_includes_key_fields():
    message = format_processed_receipt_message(
        _receipt(),
        category="grocery/supermarket",
        receipt_dir="grocery/supermarket/20260318_1834.pdf",
        filename="20260318_1834.pdf",
        processed_by="tesseract",
    )

    assert "Receipt processed" in message
    assert "Merchant: kroger" in message
    assert "Total: $4.33" in message
    assert "Category: grocery/supermarket" in message
    assert "When: 2026-03-18 18:34" in message
    assert "File: grocery/supermarket/20260318_1834.pdf" in message
    assert "Model: tesseract" in message
    assert "Purchaser: john doe" in message
    assert "Card: ****4321" in message


def test_format_processed_receipt_subject_includes_prefix_and_total():
    subject = format_processed_receipt_subject(_receipt(), subject_prefix="[intake] ")
    assert subject == "[intake] Receipt processed: kroger $4.33"


def test_send_processed_receipt_notification_invokes_signal_sender(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("signal_cli:\n  account: +15550001111\n", encoding="utf-8")
    send_script = tmp_path / "send_message.py"
    send_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output, text):
        calls.append((cmd, capture_output, text))
        return Result()

    monkeypatch.setattr("intake.notify.subprocess.run", fake_run)

    settings = {
        "signal": {
            "shock_relay_config": str(config_path),
            "recipient": "+15551234567",
        }
    }

    sent = send_processed_receipt_notification(
        settings,
        _receipt(),
        category="grocery/supermarket",
        receipt_dir="grocery/supermarket/20260318_1834.pdf",
        filename="20260318_1834.pdf",
        processed_by="tesseract",
    )

    assert sent == 1
    assert len(calls) == 1
    cmd, capture_output, text = calls[0]
    assert cmd[:4] == [sys.executable, str(send_script), "--config", str(config_path)]
    assert cmd[4] == "+15551234567"
    assert "Receipt processed" in cmd[5]
    assert capture_output is True
    assert text is True


def test_send_processed_receipt_gmail_notification_invokes_sender(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("imap:\n  username: me@example.com\n", encoding="utf-8")
    send_script = tmp_path / "send_email.py"
    send_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output, text):
        calls.append((cmd, capture_output, text))
        return Result()

    monkeypatch.setattr("intake.notify.subprocess.run", fake_run)

    settings = {
        "email": {
            "shock_relay_config": str(config_path),
        },
        "gmail": {
            "recipient": "me@example.com",
            "subject_prefix": "[intake] ",
        },
    }

    sent = send_processed_receipt_gmail_notification(
        settings,
        _receipt(),
        category="grocery/supermarket",
        receipt_dir="grocery/supermarket/20260318_1834.pdf",
        filename="20260318_1834.pdf",
        processed_by="tesseract",
    )

    assert sent == 1
    assert len(calls) == 1
    cmd, capture_output, text = calls[0]
    assert cmd[:3] == [sys.executable, str(send_script), "me@example.com"]
    assert cmd[3] == "[intake] Receipt processed: kroger $4.33"
    assert "Receipt processed" in cmd[4]
    assert cmd[5:] == ["--config", str(config_path)]
    assert capture_output is True
    assert text is True


def test_send_processed_receipt_notification_note_to_self(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        'signal_cli:\n  account: "+15550001111"\n  bus_name: "fedora-cli"\n',
        encoding="utf-8",
    )

    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output, text):
        calls.append((cmd, capture_output, text))
        return Result()

    monkeypatch.setattr("intake.notify.subprocess.run", fake_run)

    settings = {
        "signal": {
            "shock_relay_config": str(config_path),
            "note_to_self": True,
        }
    }

    sent = send_processed_receipt_notification(
        settings,
        _receipt(),
        category="grocery/supermarket",
        receipt_dir="grocery/supermarket/20260318_1834.pdf",
        filename="20260318_1834.pdf",
        processed_by="tesseract",
    )

    assert sent == 1
    cmd, capture_output, text = calls[0]
    assert cmd == [
        "signal-cli",
        "-a",
        "+15550001111",
        "--bus-name",
        "fedora-cli",
        "send",
        "--note-to-self",
        "-m",
        cmd[-1],
    ]
    assert "Receipt processed" in cmd[-1]
    assert capture_output is True
    assert text is True


def test_send_processed_receipt_notifications_keeps_other_channels_running(monkeypatch):
    def fail_signal(*args, **kwargs):
        raise RuntimeError("signal boom")

    monkeypatch.setattr("intake.notify.send_processed_receipt_notification", fail_signal)
    monkeypatch.setattr(
        "intake.notify.send_processed_receipt_gmail_notification", lambda *a, **k: 1
    )

    sent, errors = send_processed_receipt_notifications(
        {"gmail": {"recipient": "me@example.com"}},
        _receipt(),
        category="grocery/supermarket",
        receipt_dir="grocery/supermarket/20260318_1834.pdf",
        filename="20260318_1834.pdf",
        processed_by="tesseract",
    )

    assert sent == {"gmail": 1}
    assert errors == {"signal": "signal boom"}


def test_send_processed_receipt_notification_raises_on_sender_error(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("signal_cli:\n  account: +15550001111\n", encoding="utf-8")
    (tmp_path / "send_message.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    class Result:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr("intake.notify.subprocess.run", lambda *args, **kwargs: Result())

    settings = {
        "signal": {
            "shock_relay_config": str(config_path),
            "recipient": "+15551234567",
        }
    }

    with pytest.raises(RuntimeError, match="Signal notification failed"):
        send_processed_receipt_notification(
            settings,
            _receipt(),
            category="grocery/supermarket",
            receipt_dir="grocery/supermarket/20260318_1834.pdf",
            filename="20260318_1834.pdf",
            processed_by="tesseract",
        )


def test_send_processed_receipt_gmail_notification_raises_on_sender_error(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("imap:\n  username: me@example.com\n", encoding="utf-8")
    (tmp_path / "send_email.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    class Result:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr("intake.notify.subprocess.run", lambda *args, **kwargs: Result())

    settings = {
        "gmail": {
            "shock_relay_config": str(config_path),
            "recipient": "me@example.com",
        }
    }

    with pytest.raises(RuntimeError, match="Gmail notification failed"):
        send_processed_receipt_gmail_notification(
            settings,
            _receipt(),
            category="grocery/supermarket",
            receipt_dir="grocery/supermarket/20260318_1834.pdf",
            filename="20260318_1834.pdf",
            processed_by="tesseract",
        )
