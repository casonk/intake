"""Unit tests for intake.personal_finance."""

from __future__ import annotations

import json
import sys
from datetime import date, time

import pytest

from intake.parse import Receipt
from intake.personal_finance import (
    build_receipt_event,
    personal_finance_enabled,
    trigger_personal_finance,
)


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


def test_personal_finance_enabled_requires_repo_root():
    assert personal_finance_enabled({}) is False
    assert personal_finance_enabled({"personal_finance": {"enabled": False}}) is False
    assert personal_finance_enabled({"personal_finance": {"repo_root": "/tmp/pf"}}) is True


def test_build_receipt_event_includes_expected_fields():
    event = build_receipt_event(
        _receipt(),
        category="grocery/supermarket",
        receipt_dir="grocery/supermarket/20260318_1834.pdf",
        filename="20260318_1834.pdf",
        processed_by="tesseract",
        tier="ok",
    )
    assert event["merchant"] == "kroger"
    assert event["total"] == 4.33
    assert event["category"] == "grocery/supermarket"
    assert event["receipt_time"] == "18:34"
    assert event["tier"] == "ok"


def test_trigger_personal_finance_writes_json_and_runs_command(monkeypatch, tmp_path):
    repo_root = tmp_path / "finance-pipeline"
    repo_root.mkdir()
    event_dir = tmp_path / "events"

    calls = []

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(cmd, cwd, env, capture_output, text, check, timeout):
        calls.append(
            {
                "cmd": cmd,
                "cwd": cwd,
                "env": env,
                "capture_output": capture_output,
                "text": text,
                "check": check,
                "timeout": timeout,
            }
        )
        return Result()

    monkeypatch.setattr("intake.personal_finance.subprocess.run", fake_run)

    settings = {
        "personal_finance": {
            "repo_root": str(repo_root),
            "event_dir": str(event_dir),
            "command": [
                sys.executable,
                "scripts/intake_ingest.py",
                "--receipts-json",
                "{receipts_json}",
                "--count",
                "{receipt_count}",
            ],
            "timeout_seconds": 90,
        }
    }
    receipts = [
        build_receipt_event(
            _receipt(),
            category="grocery/supermarket",
            receipt_dir="grocery/supermarket/20260318_1834.pdf",
            filename="20260318_1834.pdf",
            processed_by="tesseract",
            tier="ok",
        )
    ]

    json_path = trigger_personal_finance(settings, receipts, trigger="process")

    assert json_path is not None
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["trigger"] == "process"
    assert payload["receipt_count"] == 1
    assert payload["receipts"][0]["merchant"] == "kroger"
    assert json.loads((event_dir / "latest.json").read_text(encoding="utf-8"))["receipt_count"] == 1

    call = calls[0]
    assert call["cwd"] == str(repo_root)
    assert call["cmd"][:3] == [sys.executable, "scripts/intake_ingest.py", "--receipts-json"]
    assert call["cmd"][3] == str(json_path)
    assert call["cmd"][-1] == "1"
    assert call["env"]["INTAKE_RECEIPTS_JSON"] == str(json_path)
    assert call["env"]["INTAKE_RECEIPT_COUNT"] == "1"
    assert call["env"]["INTAKE_TRIGGER"] == "process"
    assert call["capture_output"] is True
    assert call["text"] is True
    assert call["check"] is False
    assert call["timeout"] == 90


def test_trigger_personal_finance_raises_for_failed_command(monkeypatch, tmp_path):
    repo_root = tmp_path / "finance-pipeline"
    repo_root.mkdir()

    class Result:
        returncode = 2
        stdout = ""
        stderr = "bad run"

    monkeypatch.setattr("intake.personal_finance.subprocess.run", lambda *args, **kwargs: Result())

    settings = {"personal_finance": {"repo_root": str(repo_root)}}
    receipts = [
        build_receipt_event(
            _receipt(),
            category="grocery/supermarket",
            receipt_dir="grocery/supermarket/20260318_1834.pdf",
            filename="20260318_1834.pdf",
            processed_by="tesseract",
            tier="ok",
        )
    ]

    with pytest.raises(RuntimeError, match="finance-pipeline trigger failed"):
        trigger_personal_finance(settings, receipts, trigger="process")
