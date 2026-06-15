"""Unit tests for intake.notify."""

from __future__ import annotations

import sys
from datetime import date, time

import pytest

from intake.notify import (
    format_processed_receipt_message,
    format_processed_receipt_subject,
    format_receipt_file_link_reply_message,
    gmail_digest_enabled,
    gmail_notifications_enabled,
    gmail_reply_available,
    send_corrupt_intake_gmail_notification,
    send_corrupt_intake_notifications,
    send_corrupt_intake_signal_notification,
    send_processed_receipt_gmail_notification,
    send_processed_receipt_notification,
    send_processed_receipt_notifications,
    send_receipt_file_link_gmail_reply,
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


def _jpy_receipt() -> Receipt:
    receipt = _receipt()
    receipt.merchant = "ローソン"
    receipt.total = 1280.0
    receipt.currency = "JPY"
    return receipt


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


def test_gmail_reply_available_with_email_config_fallback():
    settings = {
        "email": {
            "shock_relay_config": "/tmp/gmail-imap/config.local.yaml",
        }
    }
    assert gmail_reply_available(settings) is True


def test_gmail_digest_enabled():
    assert gmail_digest_enabled({}) is False
    assert gmail_digest_enabled({"gmail": {"digest": True}}) is True
    assert gmail_digest_enabled({"gmail": {"digest_enabled": True}}) is True


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


def test_format_processed_receipt_message_includes_optional_snowbridge_link():
    message = format_processed_receipt_message(
        _receipt(),
        category="grocery/supermarket",
        receipt_dir="grocery/supermarket/20260318_1834.pdf",
        filename="20260318_1834.pdf",
        processed_by="tesseract",
        snowbridge_link="https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.pdf",
        artifact_links={
            "ocr": "https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.ocr.txt",
            "sidecar_json": "https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.intake.json",
            "ai_json": "https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.ai.json",
        },
    )

    assert (
        "Link: https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.pdf"
        in message
    )
    assert (
        "OCR: https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.ocr.txt"
        in message
    )
    assert (
        "Sidecar JSON: https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.intake.json"
        in message
    )
    assert (
        "AI JSON: https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.ai.json"
        in message
    )


def test_format_processed_receipt_subject_includes_prefix_and_total():
    subject = format_processed_receipt_subject(_receipt(), subject_prefix="[intake] ")
    assert subject == "[intake] Receipt processed: kroger $4.33"


def test_format_processed_receipt_notification_uses_receipt_currency():
    message = format_processed_receipt_message(
        _jpy_receipt(),
        category="general",
        receipt_dir="general/20260401_1000.pdf",
        filename="20260401_1000.pdf",
        processed_by="tesseract",
    )
    subject = format_processed_receipt_subject(_jpy_receipt(), subject_prefix="[intake] ")

    assert "Total: ¥1,280" in message
    assert subject == "[intake] Receipt processed: ローソン ¥1,280"


def test_format_receipt_file_link_reply_message_includes_link():
    message = format_receipt_file_link_reply_message(
        {
            "filename": "20260419_2018.jpg",
            "merchant": "dewdney liquor store",
            "total": 39.07,
        },
        snowbridge_link="https://files.snowbridge.internal/files/receipt/general/20260419_2018.jpg",
        artifact_links={
            "ocr": "https://files.snowbridge.internal/files/receipt/general/20260419_2018.ocr.txt",
            "sidecar_json": "https://files.snowbridge.internal/files/receipt/general/20260419_2018.intake.json",
            "ai_json": "https://files.snowbridge.internal/files/receipt/general/20260419_2018.ai.json",
        },
    )
    assert "Snowbridge receipt link requested" in message
    assert "Receipt: 20260419_2018.jpg" in message
    assert "Merchant: dewdney liquor store" in message
    assert "Total: $39.07" in message
    assert "https://files.snowbridge.internal/files/receipt/general/20260419_2018.jpg" in message
    assert (
        "https://files.snowbridge.internal/files/receipt/general/20260419_2018.ocr.txt" in message
    )
    assert (
        "https://files.snowbridge.internal/files/receipt/general/20260419_2018.intake.json"
        in message
    )
    assert (
        "https://files.snowbridge.internal/files/receipt/general/20260419_2018.ai.json" in message
    )


def test_format_receipt_file_link_reply_message_uses_row_currency():
    message = format_receipt_file_link_reply_message(
        {
            "filename": "20260401_1000.pdf",
            "merchant": "ローソン",
            "total": 1280,
            "currency": "JPY",
        },
        snowbridge_link="https://files.snowbridge.internal/files/receipt/general/20260401_1000.pdf",
    )
    assert "Total: ¥1,280" in message


def test_send_processed_receipt_notification_invokes_signal_sender(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("signal_cli:\n  account: +15550001111\n", encoding="utf-8")
    send_script = tmp_path / "send_message.py"
    send_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    receipt_root = tmp_path / "receipts"
    receipt_file = receipt_root / "grocery" / "supermarket" / "20260318_1834.pdf"
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_bytes(b"%PDF-1.4")
    receipt_file.with_suffix(".ocr.txt").write_text("ocr", encoding="utf-8")
    receipt_file.with_suffix(".intake.json").write_text("{}", encoding="utf-8")
    receipt_file.with_suffix(".ai.json").write_text("{}", encoding="utf-8")

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
        "receipt_dir": str(receipt_root),
        "signal": {
            "shock_relay_config": str(config_path),
            "recipient": "+15551234567",
        },
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
    assert cmd[:8] == [
        sys.executable,
        str(send_script),
        "--meta",
        "cc-service: intake",
        "--meta",
        "cc-intent: notify",
        "--config",
        str(config_path),
    ]
    assert cmd[8] == "+15551234567"
    assert "Receipt processed" in cmd[9]
    assert (
        "Link: https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.pdf"
        in cmd[9]
    )
    assert capture_output is True
    assert text is True


def test_send_processed_receipt_gmail_notification_invokes_sender(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("imap:\n  username: me@example.com\n", encoding="utf-8")
    send_script = tmp_path / "send_email.py"
    send_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    receipt_root = tmp_path / "receipts"
    receipt_file = receipt_root / "grocery" / "supermarket" / "20260318_1834.pdf"
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_bytes(b"%PDF-1.4")
    receipt_file.with_suffix(".ocr.txt").write_text("ocr", encoding="utf-8")
    receipt_file.with_suffix(".intake.json").write_text("{}", encoding="utf-8")
    receipt_file.with_suffix(".ai.json").write_text("{}", encoding="utf-8")

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
        "receipt_dir": str(receipt_root),
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
    assert cmd[:11] == [
        sys.executable,
        str(send_script),
        "--header",
        "X-Portfolio-Service: intake",
        "--header",
        "X-Crew-Chief-Intent: notify",
        "--header",
        "X-Intake-Receipt-Filename: 20260318_1834.pdf",
        "--header",
        "X-Intake-Receipt-Dir: grocery/supermarket/20260318_1834.pdf",
        "me@example.com",
    ]
    assert cmd[11] == "[intake] Receipt processed: kroger $4.33"
    assert "Receipt processed" in cmd[12]
    assert (
        "Link: https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.pdf"
        in cmd[12]
    )
    assert (
        "OCR: https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.ocr.txt"
        in cmd[12]
    )
    assert (
        "Sidecar JSON: https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.intake.json"
        in cmd[12]
    )
    assert (
        "AI JSON: https://files.snowbridge.internal/files/receipt/grocery/supermarket/20260318_1834.ai.json"
        in cmd[12]
    )
    assert cmd[13:] == ["--config", str(config_path)]
    assert capture_output is True
    assert text is True


def test_send_processed_receipt_gmail_notification_queues_digest(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("imap:\n  username: me@example.com\n", encoding="utf-8")
    queue_script = tmp_path / "queue_digest.py"
    queue_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

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
        "gmail": {
            "shock_relay_config": str(config_path),
            "recipient": "me@example.com",
            "subject_prefix": "[intake] ",
            "digest": True,
        }
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
    cmd, capture_output, text = calls[0]
    assert cmd[:11] == [
        sys.executable,
        str(queue_script),
        "--header",
        "X-Portfolio-Service: intake",
        "--header",
        "X-Crew-Chief-Intent: notify",
        "--header",
        "X-Intake-Receipt-Filename: 20260318_1834.pdf",
        "--header",
        "X-Intake-Receipt-Dir: grocery/supermarket/20260318_1834.pdf",
        "--service",
    ]
    assert cmd[11:16] == [
        "intake",
        "--kind",
        "receipt-processed",
        "--summary",
        "[intake] Receipt processed: kroger $4.33",
    ]
    assert cmd[16] == "me@example.com"
    assert cmd[17] == "[intake] Receipt processed: kroger $4.33"
    assert "Receipt processed" in cmd[18]
    assert cmd[19:] == ["--config", str(config_path)]
    assert capture_output is True
    assert text is True


def test_send_receipt_file_link_gmail_reply_invokes_sender(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("imap:\n  username: me@example.com\n", encoding="utf-8")
    send_script = tmp_path / "send_email.py"
    send_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    receipt_root = tmp_path / "receipts"
    receipt_file = receipt_root / "general" / "20260419_2018.jpg"
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_bytes(b"jpg")
    receipt_file.with_suffix(".ocr.txt").write_text("ocr", encoding="utf-8")
    receipt_file.with_suffix(".intake.json").write_text("{}", encoding="utf-8")
    receipt_file.with_suffix(".ai.json").write_text("{}", encoding="utf-8")

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
        "receipt_dir": str(receipt_root),
        "email": {
            "shock_relay_config": str(config_path),
        },
    }

    link = send_receipt_file_link_gmail_reply(
        settings,
        recipient="Alex Sample <alexsample@gmail.com>",
        subject="Re: [intake] Receipt processed: dewdney liquor store $39.07",
        receipt_row={
            "filename": "20260419_2018.jpg",
            "merchant": "dewdney liquor store",
            "total": 39.07,
            "receipt_dir": "general/20260419_2018.jpg",
        },
        in_reply_to="<reply@example.com>",
        references="<orig@example.com> <reply@example.com>",
    )

    assert link == "https://files.snowbridge.internal/files/receipt/general/20260419_2018.jpg"
    assert len(calls) == 1
    cmd, capture_output, text = calls[0]
    assert cmd[:15] == [
        sys.executable,
        str(send_script),
        "--header",
        "X-Portfolio-Service: intake",
        "--header",
        "X-Crew-Chief-Intent: reply-link",
        "--header",
        "X-Intake-Receipt-Filename: 20260419_2018.jpg",
        "--header",
        "X-Intake-Receipt-Dir: general/20260419_2018.jpg",
        "--header",
        "In-Reply-To: <reply@example.com>",
        "--header",
        "References: <orig@example.com> <reply@example.com>",
        "alexsample@gmail.com",
    ]
    assert cmd[15] == "Re: [intake] Receipt processed: dewdney liquor store $39.07"
    assert "https://files.snowbridge.internal/files/receipt/general/20260419_2018.jpg" in cmd[16]
    assert (
        "OCR: https://files.snowbridge.internal/files/receipt/general/20260419_2018.ocr.txt"
        in cmd[16]
    )
    assert (
        "Sidecar JSON: https://files.snowbridge.internal/files/receipt/general/20260419_2018.intake.json"
        in cmd[16]
    )
    assert (
        "AI JSON: https://files.snowbridge.internal/files/receipt/general/20260419_2018.ai.json"
        in cmd[16]
    )
    assert cmd[17:] == ["--config", str(config_path)]
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
    assert cmd[-1].startswith("cc-service: intake\ncc-intent: notify\n\n")
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


# ---------------------------------------------------------------------------
# Corrupt-intake notifications
# ---------------------------------------------------------------------------


def test_send_corrupt_intake_signal_notification_sends_to_recipient(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("signal_cli:\n  account: +15550001111\n", encoding="utf-8")
    send_script = tmp_path / "send_message.py"
    send_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    captured: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **_kwargs):
        captured.append(cmd)
        return Result()

    monkeypatch.setattr("intake.notify.subprocess.run", fake_run)

    settings = {
        "signal": {
            "shock_relay_config": str(config_path),
            "recipient": "+15551234567",
        }
    }

    count = send_corrupt_intake_signal_notification(settings, ["IMG_2365.JPG"])
    assert count == 1
    assert len(captured) == 1
    assert "IMG_2365.JPG" in captured[0][-1]
    assert "corrupt" in captured[0][-1].lower() or "zero bytes" in captured[0][-1].lower()


def test_send_corrupt_intake_signal_notification_noop_when_disabled():
    count = send_corrupt_intake_signal_notification({}, ["IMG_2365.JPG"])
    assert count == 0


def test_send_corrupt_intake_gmail_notification_sends_to_recipient(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("imap:\n  username: me@example.com\n", encoding="utf-8")
    send_script = tmp_path / "send_email.py"
    send_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    captured: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **_kwargs):
        captured.append(cmd)
        return Result()

    monkeypatch.setattr("intake.notify.subprocess.run", fake_run)

    settings = {
        "gmail": {
            "shock_relay_config": str(config_path),
            "recipient": "me@example.com",
        }
    }

    count = send_corrupt_intake_gmail_notification(settings, ["IMG_2365.JPG", "IMG_2381.JPG"])
    assert count == 1
    assert len(captured) == 1
    subject_arg = captured[0][captured[0].index("me@example.com") + 1]
    assert "2 file" in subject_arg


def test_send_corrupt_intake_gmail_notification_noop_when_disabled():
    count = send_corrupt_intake_gmail_notification({}, ["IMG_2365.JPG"])
    assert count == 0


def test_send_corrupt_intake_notifications_returns_sent_and_errors(monkeypatch, tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("signal_cli:\n  account: +15550001111\n", encoding="utf-8")
    (tmp_path / "send_message.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("intake.notify.subprocess.run", lambda *args, **kwargs: Result())

    settings = {
        "signal": {
            "shock_relay_config": str(config_path),
            "recipient": "+15551234567",
        }
    }

    sent, errors = send_corrupt_intake_notifications(settings, ["IMG_2365.JPG"])
    assert sent.get("signal") == 1
    assert errors == {}
