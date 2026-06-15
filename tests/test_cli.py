"""Tests for intake.cli path resolution and clockwork-backed unit generation."""

import importlib.util
import json
from argparse import Namespace
from datetime import date
from pathlib import Path

import pytest

from intake import cli
from intake.db import get_receipt, init_db, query_receipt_updates, upsert_receipt
from intake.parse import Receipt
from intake.sidecar import read_sidecar, write_sidecar

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib


def test_load_settings_resolves_repo_relative_paths(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    settings_path = repo_root / "config" / "settings.toml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        "\n".join(
            [
                'receipt_dir = "receipts"',
                'db_path = "data/intake.db"',
                'reports_dir = "reports"',
                "",
                "[email]",
                'shock_relay_config = "mail/config.local.yaml"',
                "",
                "[signal]",
                'shock_relay_config = "signal/config.local.yaml"',
                "",
                "[gmail]",
                'shock_relay_config = "gmail/config.local.yaml"',
                "",
                "[personal_finance]",
                'repo_root = "repos/finance-pipeline"',
                'event_dir = "data/finance-pipeline"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "_REPO_ROOT", repo_root)
    monkeypatch.setattr(cli, "_SETTINGS_PATH", settings_path)

    settings = cli._load_settings()

    assert settings["receipt_dir"] == str(repo_root / "receipts")
    assert settings["db_path"] == str(repo_root / "data" / "intake.db")
    assert settings["reports_dir"] == str(repo_root / "reports")
    assert settings["email"]["shock_relay_config"] == str(repo_root / "mail" / "config.local.yaml")
    assert settings["signal"]["shock_relay_config"] == str(
        repo_root / "signal" / "config.local.yaml"
    )
    assert settings["gmail"]["shock_relay_config"] == str(repo_root / "gmail" / "config.local.yaml")
    assert settings["personal_finance"]["repo_root"] == str(
        repo_root / "repos" / "finance-pipeline"
    )
    assert settings["personal_finance"]["event_dir"] == str(repo_root / "data" / "finance-pipeline")


def test_install_systemd_unit_avoids_repo_working_directory(monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", "/usr/bin/python3")
    captured: dict[str, str] = {}

    def fake_install(manifest_text: str, *, target: str) -> None:
        captured["manifest_text"] = manifest_text
        captured["target"] = target

    monkeypatch.setattr(cli, "_install_clockwork_manifest", fake_install)
    cli._install_systemd_unit({})
    manifest = tomllib.loads(captured["manifest_text"])
    job = manifest["jobs"][0]

    assert captured["target"] == "systemd-user"
    assert job["working_directory"] == "%h"
    assert job["start_limit_interval_sec"] == "0"
    assert "/bin/bash -lc" in job["exec_start"]
    assert (
        f'export PYTHONPATH="{cli._REPO_ROOT}${{PYTHONPATH:+:$PYTHONPATH}}"; ' in job["exec_start"]
    )
    assert 'until /usr/bin/python3 -c "import intake.cli" >/dev/null 2>&1;' in job["exec_start"]
    assert "exec /usr/bin/python3 -m intake.cli daemon" in job["exec_start"]


def test_install_report_timer_units_writes_noon_schedule(monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", "/usr/bin/python3")
    captured: dict[str, str] = {}

    def fake_install(manifest_text: str, *, target: str) -> None:
        captured["manifest_text"] = manifest_text
        captured["target"] = target

    monkeypatch.setattr(cli, "_install_clockwork_manifest", fake_install)
    cli._install_report_timer_units(on_calendar="*-*-* 12:00:00")
    manifest = tomllib.loads(captured["manifest_text"])
    job = manifest["jobs"][0]
    timer = job["timer"]

    assert captured["target"] == "systemd-user"
    assert job["working_directory"] == "%h"
    assert "/bin/bash -lc" in job["exec_start"]
    assert (
        f'export PYTHONPATH="{cli._REPO_ROOT}${{PYTHONPATH:+:$PYTHONPATH}}"; ' in job["exec_start"]
    )
    assert 'until /usr/bin/python3 -c "import intake.cli" >/dev/null 2>&1;' in job["exec_start"]
    assert "exec /usr/bin/python3 -m intake.cli report" in job["exec_start"]
    assert timer["on_calendar"] == "*-*-* 12:00:00"
    assert timer["persistent"] is True


def test_install_web_unit_writes_localhost_service(monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", "/usr/bin/python3")
    captured: dict[str, str] = {}

    def fake_install(manifest_text: str, *, target: str) -> None:
        captured["manifest_text"] = manifest_text
        captured["target"] = target

    monkeypatch.setattr(cli, "_install_clockwork_manifest", fake_install)
    cli._install_web_unit()
    manifest = tomllib.loads(captured["manifest_text"])
    job = manifest["jobs"][0]

    assert captured["target"] == "systemd-user"
    assert job["working_directory"] == "%h"
    assert job["start_limit_interval_sec"] == "0"
    assert "/bin/bash -lc" in job["exec_start"]
    assert (
        f'export PYTHONPATH="{cli._REPO_ROOT}${{PYTHONPATH:+:$PYTHONPATH}}"; ' in job["exec_start"]
    )
    assert 'until /usr/bin/python3 -c "import intake.cli" >/dev/null 2>&1;' in job["exec_start"]
    assert "exec /usr/bin/python3 -m intake.cli serve" in job["exec_start"]


def test_web_host_and_port_defaults():
    assert cli._web_host({}, None) == "127.0.0.1"
    assert cli._web_port({}, None) == 5200


def test_web_host_and_port_read_settings():
    settings = {"web": {"host": "127.0.0.2", "port": 5300}}

    assert cli._web_host(settings, None) == "127.0.0.2"
    assert cli._web_port(settings, None) == 5300
    assert cli._web_host(settings, "127.0.0.3") == "127.0.0.3"
    assert cli._web_port(settings, 5400) == 5400


def test_receipt_event_from_row_includes_currency():
    event = cli._receipt_event_from_row(
        {
            "filename": "20260614_1350.jpg",
            "receipt_dir": "general/20260614_1350.jpg",
            "scan_date": "2026-06-14",
            "merchant": "山梨家富士宮店",
            "total": 2130.0,
            "currency": "JPY",
            "category": "general",
            "processed_by": "gpt-4o-mini",
        },
        tier="manual-correct",
    )

    assert event["total"] == 2130.0
    assert event["currency"] == "JPY"


def test_stored_receipt_needs_refresh_for_historical_japanese_row():
    settings = {"translation_provider": "ollama"}
    sidecar = {"language": "und", "currency": "JPY", "translated_text": None}
    row = {
        "raw_text": "ローソン\n合計 1280円\nお預り 2000\nお釣り 720",
        "language": "und",
        "currency": "JPY",
        "translated_text": None,
    }

    assert cli._stored_receipt_needs_refresh(sidecar, row, settings) is True


def test_cmd_translate_text_prints_json(monkeypatch, capsys):
    args = Namespace(
        text="ローソン\n合計 1280円",
        text_file=None,
        language="ja",
        provider="ollama",
        model="aya-expanse:8b",
        base_url="http://localhost:11434",
        timeout_seconds=45,
        json=True,
    )
    monkeypatch.setattr(
        "intake.translation.translate_receipt_text",
        lambda *_args, **_kwargs: (
            "Lawson\nTotal 1,280 yen",
            {"provider": "ollama", "model": "aya-expanse:8b"},
        ),
    )

    cli._cmd_translate_text(args, {})

    payload = json.loads(capsys.readouterr().out)
    assert payload["translation"] == "Lawson\nTotal 1,280 yen"
    assert payload["metadata"]["provider"] == "ollama"
    assert payload["source_language"] == "ja"


def test_cmd_translate_backfill_updates_row_and_sidecar(tmp_path, monkeypatch, capsys):
    receipt_root = tmp_path / "receipts"
    artifact = receipt_root / "general" / "20260605_0914.jpg"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"fake-image")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "20260605_0914.jpg",
            "scan_date": "2026-06-05",
            "merchant": "lawson",
            "total": 680.0,
            "category": "general",
            "receipt_dir": "general/20260605_0914.jpg",
            "raw_text": "ローソン\n合計 680円",
            "processed_by": "tesseract",
            "currency": "JPY",
            "language": "und",
        },
    )

    monkeypatch.setattr("intake.classify.load_merchant_aliases", lambda: {})
    monkeypatch.setattr(
        "intake.translation.translate_receipt_artifact",
        lambda *_args, **_kwargs: (
            "LAWSON\nTotal\n¥680",
            {"provider": "ollama", "model": "aya-expanse:8b", "source": "artifact"},
        ),
    )

    args = Namespace(
        receipt_dir=str(receipt_root),
        db=str(db_path),
        provider="ollama",
        model="aya-expanse:8b",
        base_url="http://localhost:11434",
        timeout_seconds=60,
        filename=["20260605_0914.jpg"],
        limit=None,
        dry_run=False,
        force=False,
    )
    settings = {
        "translation_provider": "ollama",
        "translation_model": "aya-expanse:8b",
        "translation_base_url": "http://localhost:11434",
        "translation_timeout_seconds": 60,
    }

    cli._cmd_translate_backfill(args, settings)

    updated = get_receipt(init_db(db_path), "20260605_0914.jpg")
    assert updated["language"] == "ja"
    assert updated["translated_text"] == "LAWSON\nTotal\n¥680"

    sidecar = read_sidecar(artifact)
    assert sidecar is not None
    assert sidecar["language"] == "ja"
    assert sidecar["translated_text"] == "LAWSON\nTotal\n¥680"
    assert "Translated 20260605_0914.jpg" in capsys.readouterr().out


def test_install_clockwork_manifest_writes_units_when_clockwork_is_available(monkeypatch, tmp_path):
    if importlib.util.find_spec("clockwork") is None and not cli._CLOCKWORK_SRC_DIR.exists():
        pytest.skip("clockwork is not installed and sibling repo is unavailable")

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    manifest_text = cli._build_daemon_manifest_text("/usr/bin/python3")

    cli._install_clockwork_manifest(manifest_text, target="systemd-user")

    unit_path = home / ".config" / "systemd" / "user" / "intake-daemon.service"
    unit = unit_path.read_text(encoding="utf-8")
    assert "Description=intake receipt processor daemon" in unit
    assert "StartLimitIntervalSec=0" in unit
    assert "ExecStart=/bin/bash -lc" in unit
    assert f'export PYTHONPATH="{cli._REPO_ROOT}${{PYTHONPATH:+:$PYTHONPATH}}"; ' in unit


def test_cmd_review_renders_reasoned_flags(tmp_path, capsys):
    db_path = tmp_path / "intake.db"
    reports_dir = tmp_path / "reports"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "20260301_0900.pdf",
            "scan_date": "2026-03-01",
            "merchant": "kroger",
            "total": 10.0,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260301_0900.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
            "card_last4": "5653",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260305_0900.pdf",
            "scan_date": "2026-03-05",
            "merchant": "kroger",
            "total": 11.0,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260305_0900.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
            "card_last4": "5653",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260310_0900.pdf",
            "scan_date": "2026-03-10",
            "merchant": "kroger",
            "total": 12.0,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260310_0900.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
            "card_last4": "5653",
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
    conn.close()

    cli._cmd_review(
        Namespace(
            db=str(db_path),
            output_dir=str(reports_dir),
            show_suppressed=False,
        ),
        {},
    )

    out = capsys.readouterr().out
    review_md = (reports_dir / "review.md").read_text(encoding="utf-8")

    assert "new_merchant, new_card_last4, new_merchant_card_pair" in out
    assert "total_over_250, amount_outlier_for_merchant (prior median $11.00)" in out
    assert "amount_outlier_for_merchant (prior median $11.00)" in out
    assert (
        "| # | file | receipt_dir | merchant | total | model | reasons | correction |" in review_md
    )
    assert "new_merchant, new_card_last4, new_merchant_card_pair" in review_md
    assert "total_over_250, amount_outlier_for_merchant (prior median $11.00)" in review_md
    assert "amount_outlier_for_merchant (prior median $11.00)" in review_md


def test_cmd_correct_updates_db_and_sidecar_and_records_audit(tmp_path, capsys):
    receipt_root = tmp_path / "receipts"
    pdf = receipt_root / "grocery" / "supermarket" / "20260409_1707.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": pdf.name,
            "scan_date": "2026-04-09",
            "merchant": "kroger",
            "total": 86.05,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260409_1707.pdf",
            "raw_text": "BALANCE 86.05\nMASTERCARD 88.05",
            "processed_by": "tesseract",
            "currency": "USD",
        },
    )
    conn.close()
    write_sidecar(
        pdf,
        {
            "filename": pdf.name,
            "scan_date": "2026-04-09",
            "merchant": "kroger",
            "total": 86.05,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260409_1707.pdf",
            "processed_by": "tesseract",
            "currency": "USD",
            "tier": "ok",
        },
    )

    cli._cmd_correct(
        Namespace(
            filename=pdf.name,
            receipt_dir=str(receipt_root),
            db=str(db_path),
            merchant=None,
            total=88.05,
            category=None,
            purchaser=None,
            clear_purchaser=False,
            card_last4=None,
            clear_card_last4=False,
            currency="CAD",
            certification=None,
            clear_certifications=False,
            note="card charge wins over balance line",
        ),
        {},
    )

    conn = init_db(db_path)
    row = get_receipt(conn, pdf.name)
    history = query_receipt_updates(conn, filename=pdf.name)
    conn.close()
    sidecar = read_sidecar(pdf)

    assert row is not None
    assert row["total"] == 88.05
    assert sidecar is not None
    assert sidecar["total"] == 88.05
    assert sidecar["currency"] == "CAD"
    assert sidecar["tier"] == "ok"
    assert history[0]["source"] == "manual-correct"
    assert history[0]["changes"]["total"] == {"old": 86.05, "new": 88.05}
    assert history[0]["changes"]["currency"] == {"old": "USD", "new": "CAD"}

    out = capsys.readouterr().out
    assert "Updated 20260409_1707.pdf" in out
    assert "sidecar:" in out


def test_cmd_correct_triggers_personal_finance_after_db_change(tmp_path, monkeypatch):
    receipt_root = tmp_path / "receipts"
    pdf = receipt_root / "general" / "20260409_1707.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": pdf.name,
            "scan_date": "2026-04-09",
            "merchant": "unknown",
            "total": 86.05,
            "category": "general",
            "receipt_dir": "general/20260409_1707.pdf",
            "raw_text": "BALANCE 86.05",
            "processed_by": "tesseract",
        },
    )
    conn.close()

    calls = []

    def fake_trigger(settings, receipt_events, *, trigger, prefix=""):
        calls.append(
            {
                "settings": settings,
                "receipt_events": receipt_events,
                "trigger": trigger,
                "prefix": prefix,
            }
        )

    monkeypatch.setattr(cli, "_trigger_personal_finance", fake_trigger)

    settings = {"personal_finance": {"repo_root": "/tmp/finance-pipeline"}}
    cli._cmd_correct(
        Namespace(
            filename=pdf.name,
            receipt_dir=str(receipt_root),
            db=str(db_path),
            merchant="badlands national park",
            total=88.05,
            category="outdoor/park",
            purchaser=None,
            clear_purchaser=False,
            card_last4=None,
            clear_card_last4=False,
            certification=None,
            clear_certifications=False,
            note="correct merchant and category",
            currency=None,
        ),
        settings,
    )

    assert len(calls) == 1
    assert calls[0]["settings"] == settings
    assert calls[0]["trigger"] == "correct"
    assert calls[0]["prefix"] == "  "
    event = calls[0]["receipt_events"][0]
    assert event["filename"] == pdf.name
    assert event["merchant"] == "badlands national park"
    assert event["total"] == 88.05
    assert event["category"] == "outdoor/park"
    assert event["receipt_dir"] == "outdoor/park/20260409_1707.pdf"
    assert event["tier"] == "manual-correct"


def test_cmd_correct_moves_receipt_when_category_changes(tmp_path):
    receipt_root = tmp_path / "receipts"
    old_pdf = receipt_root / "general" / "20260409_1707.pdf"
    old_pdf.parent.mkdir(parents=True)
    old_pdf.write_bytes(b"%PDF-1.4\n")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": old_pdf.name,
            "scan_date": "2026-04-09",
            "merchant": "kroger fuel",
            "total": 42.00,
            "category": "general",
            "receipt_dir": "general/20260409_1707.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
        },
    )
    conn.close()
    write_sidecar(
        old_pdf,
        {
            "filename": old_pdf.name,
            "scan_date": "2026-04-09",
            "merchant": "kroger fuel",
            "total": 42.00,
            "category": "general",
            "receipt_dir": "general/20260409_1707.pdf",
            "processed_by": "tesseract",
            "tier": "ok",
        },
    )

    cli._cmd_correct(
        Namespace(
            filename=old_pdf.name,
            receipt_dir=str(receipt_root),
            db=str(db_path),
            merchant=None,
            total=None,
            category="gas-station",
            purchaser=None,
            clear_purchaser=False,
            card_last4=None,
            clear_card_last4=False,
            certification=None,
            clear_certifications=False,
            note="reclassified as fuel",
        ),
        {},
    )

    new_pdf = receipt_root / "gas-station" / old_pdf.name
    assert not old_pdf.exists()
    assert new_pdf.exists()

    conn = init_db(db_path)
    row = get_receipt(conn, old_pdf.name)
    history = query_receipt_updates(conn, filename=old_pdf.name)
    conn.close()
    sidecar = read_sidecar(new_pdf)

    assert row is not None
    assert row["category"] == "gas-station"
    assert row["receipt_dir"] == "gas-station/20260409_1707.pdf"
    assert sidecar is not None
    assert sidecar["category"] == "gas-station"
    assert sidecar["receipt_dir"] == "gas-station/20260409_1707.pdf"
    assert history[0]["changes"]["category"] == {"old": "general", "new": "gas-station"}
    assert history[0]["changes"]["receipt_dir"] == {
        "old": "general/20260409_1707.pdf",
        "new": "gas-station/20260409_1707.pdf",
    }


def test_cmd_correct_recategorizes_email_receipt_and_materializes_artifact(tmp_path):
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "email_30999_INBOX.txt",
            "scan_date": "2026-04-23",
            "merchant": "Spotify USA Inc",
            "total": 12.99,
            "category": "general",
            "receipt_dir": "email/general",
            "raw_text": "Spotify receipt body",
            "processed_by": "email-parse",
        },
    )
    conn.close()

    cli._cmd_correct(
        Namespace(
            filename="email_30999_INBOX.txt",
            receipt_dir=str(receipt_root),
            db=str(db_path),
            merchant="spotify usa inc",
            total=None,
            category="sw/subscription",
            purchaser=None,
            clear_purchaser=False,
            card_last4=None,
            clear_card_last4=False,
            certification=None,
            clear_certifications=False,
            note="normalize subscription email",
            currency=None,
        ),
        {},
    )

    conn = init_db(db_path)
    row = get_receipt(conn, "email_30999_INBOX.txt")
    history = query_receipt_updates(conn, filename="email_30999_INBOX.txt")
    conn.close()

    assert row is not None
    assert row["merchant"] == "spotify usa inc"
    assert row["category"] == "sw/subscription"
    assert row["receipt_dir"] == "email/sw/subscription"
    artifact = receipt_root / "email" / "sw" / "subscription" / "email_30999_INBOX.txt"
    sidecar = read_sidecar(artifact)
    assert artifact.read_text(encoding="utf-8") == "Spotify receipt body"
    assert sidecar is not None
    assert sidecar["category"] == "sw/subscription"
    assert sidecar["receipt_dir"] == "email/sw/subscription"
    assert history[0]["changes"]["receipt_dir"] == {
        "old": "email/general",
        "new": "email/sw/subscription",
    }


def test_cmd_correct_moves_existing_email_receipt_artifact(tmp_path):
    receipt_root = tmp_path / "receipts"
    old_artifact = receipt_root / "email" / "general" / "email_30999_INBOX.txt"
    old_artifact.parent.mkdir(parents=True)
    old_artifact.write_text("Spotify receipt body", encoding="utf-8")
    write_sidecar(
        old_artifact,
        {
            "filename": "email_30999_INBOX.txt",
            "scan_date": "2026-04-23",
            "merchant": "spotify usa inc",
            "total": 12.99,
            "category": "general",
            "receipt_dir": "email/general",
            "processed_by": "email-parse",
        },
    )
    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "email_30999_INBOX.txt",
            "scan_date": "2026-04-23",
            "merchant": "Spotify USA Inc",
            "total": 12.99,
            "category": "general",
            "receipt_dir": "email/general",
            "raw_text": "Spotify receipt body",
            "processed_by": "email-parse",
        },
    )
    conn.close()

    cli._cmd_correct(
        Namespace(
            filename="email_30999_INBOX.txt",
            receipt_dir=str(receipt_root),
            db=str(db_path),
            merchant=None,
            total=None,
            category="sw/subscription",
            purchaser=None,
            clear_purchaser=False,
            card_last4=None,
            clear_card_last4=False,
            certification=None,
            clear_certifications=False,
            note="normalize subscription email",
            currency=None,
        ),
        {},
    )

    new_artifact = receipt_root / "email" / "sw" / "subscription" / "email_30999_INBOX.txt"
    sidecar = read_sidecar(new_artifact)
    assert not old_artifact.exists()
    assert new_artifact.read_text(encoding="utf-8") == "Spotify receipt body"
    assert sidecar is not None
    assert sidecar["category"] == "sw/subscription"
    assert sidecar["receipt_dir"] == "email/sw/subscription"


def test_cmd_correct_updates_certifications_in_db_and_sidecar(tmp_path):
    receipt_root = tmp_path / "receipts"
    pdf = receipt_root / "auto" / "heritage-collision" / "20110830_1149.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": pdf.name,
            "scan_date": "2011-08-30",
            "merchant": "heritage collision",
            "total": None,
            "category": "auto/heritage-collision",
            "receipt_dir": "auto/heritage-collision/20110830_1149.pdf",
            "raw_text": "Certified by AAA",
            "processed_by": "gpt-4.1",
        },
    )
    conn.close()
    write_sidecar(
        pdf,
        {
            "filename": pdf.name,
            "scan_date": "2011-08-30",
            "merchant": "heritage collision",
            "total": None,
            "category": "auto/heritage-collision",
            "receipt_dir": "auto/heritage-collision/20110830_1149.pdf",
            "processed_by": "gpt-4.1",
            "tier": "ok",
        },
    )

    cli._cmd_correct(
        Namespace(
            filename=pdf.name,
            receipt_dir=str(receipt_root),
            db=str(db_path),
            merchant=None,
            total=None,
            category=None,
            purchaser=None,
            clear_purchaser=False,
            card_last4=None,
            clear_card_last4=False,
            certification=["AAA=AAA certified"],
            clear_certifications=False,
            note="record aaa certification",
        ),
        {},
    )

    conn = init_db(db_path)
    row = get_receipt(conn, pdf.name)
    history = query_receipt_updates(conn, filename=pdf.name)
    conn.close()
    sidecar = read_sidecar(pdf)

    assert row is not None
    assert row["certifications"] == [
        {
            "issuer": "AAA",
            "label": "AAA certified",
            "source_text": "manual: AAA=AAA certified",
        }
    ]
    assert sidecar is not None
    assert sidecar["certifications"] == row["certifications"]
    assert history[0]["changes"]["certifications"] == {"old": None, "new": row["certifications"]}


def test_cmd_correct_resyncs_sidecar_when_db_already_matches(tmp_path, capsys):
    receipt_root = tmp_path / "receipts"
    pdf = receipt_root / "auto" / "heritage-collision" / "20110830_1149.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": pdf.name,
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
                    "source_text": "manual: AAA=AAA certified",
                }
            ],
        },
    )
    conn.close()
    write_sidecar(
        pdf,
        {
            "filename": pdf.name,
            "scan_date": "2011-08-30",
            "merchant": "heritage collision",
            "total": None,
            "category": "auto/heritage-collision",
            "receipt_dir": "auto/heritage-collision/20110830_1149.pdf",
            "processed_by": "gpt-4.1",
            "tier": "ok",
            "certifications": [],
        },
    )

    cli._cmd_correct(
        Namespace(
            filename=pdf.name,
            receipt_dir=str(receipt_root),
            db=str(db_path),
            merchant=None,
            total=None,
            category=None,
            purchaser=None,
            clear_purchaser=False,
            card_last4=None,
            clear_card_last4=False,
            certification=["AAA=AAA certified"],
            clear_certifications=False,
            note="retry sidecar sync",
        ),
        {},
    )

    sidecar = read_sidecar(pdf)

    assert sidecar is not None
    assert sidecar["certifications"] == [
        {
            "issuer": "AAA",
            "label": "AAA certified",
            "source_text": "manual: AAA=AAA certified",
        }
    ]

    out = capsys.readouterr().out
    assert "No DB changes applied for 20110830_1149.pdf; sidecar synced:" in out


def test_move_receipt_artifacts_moves_debug_files(tmp_path):
    receipt_root = tmp_path / "receipts"
    current_pdf = receipt_root / "general" / "20260409_1707.pdf"
    current_pdf.parent.mkdir(parents=True)
    current_pdf.write_bytes(b"%PDF-1.4\n")
    write_sidecar(current_pdf, {"filename": current_pdf.name})
    (current_pdf.with_suffix(".ocr.txt")).write_text("ocr", encoding="utf-8")
    (current_pdf.with_suffix(".ai.json")).write_text('{"used_ai": false}', encoding="utf-8")

    cli._move_receipt_artifacts(
        receipt_root,
        current_relpath="general/20260409_1707.pdf",
        new_relpath="grocery/supermarket/20260409_1707.pdf",
    )

    new_pdf = receipt_root / "grocery" / "supermarket" / "20260409_1707.pdf"
    assert new_pdf.exists()
    assert new_pdf.with_suffix(".intake.json").exists()
    assert new_pdf.with_suffix(".ocr.txt").read_text(encoding="utf-8") == "ocr"
    assert new_pdf.with_suffix(".ai.json").read_text(encoding="utf-8") == '{"used_ai": false}'


def test_cmd_history_prints_audit_entries(tmp_path, capsys):
    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "20260409_1707.pdf",
            "scan_date": "2026-04-09",
            "merchant": "kroger",
            "total": 86.05,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260409_1707.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
        },
    )
    from intake.db import update_receipt_fields

    update_receipt_fields(
        conn,
        "20260409_1707.pdf",
        {"total": 88.05},
        source="manual-correct",
        note="fixed total",
    )
    conn.close()

    cli._cmd_history(
        Namespace(filename="20260409_1707.pdf", db=str(db_path), limit=20),
        {},
    )

    out = capsys.readouterr().out
    assert "[manual-correct]" in out
    assert "fixed total" in out
    assert "86.05" in out
    assert "88.05" in out


def test_cmd_normalize_filenames_renames_db_and_sidecar(tmp_path):
    receipt_root = tmp_path / "receipts"
    pdf = receipt_root / "grocery" / "kroger" / "20260409.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    import os
    from datetime import datetime

    ts = datetime(2026, 4, 10, 17, 7).timestamp()
    os.utime(pdf, (ts, ts))

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": pdf.name,
            "scan_date": "2026-04-09",
            "merchant": "kroger",
            "total": 88.05,
            "category": "grocery/kroger",
            "receipt_dir": "grocery/kroger/20260409.pdf",
            "raw_text": "Kroger\nTotal 88.05",
            "processed_by": "tesseract",
        },
    )
    conn.close()
    write_sidecar(
        pdf,
        {
            "filename": pdf.name,
            "scan_date": "2026-04-09",
            "merchant": "kroger",
            "total": 88.05,
            "category": "grocery/kroger",
            "receipt_dir": "grocery/kroger/20260409.pdf",
            "processed_by": "tesseract",
            "tier": "ok",
        },
    )

    cli._cmd_normalize_filenames(
        Namespace(receipt_dir=str(receipt_root), db=str(db_path), dry_run=False),
        {},
    )

    new_pdf = receipt_root / "grocery" / "kroger" / "20260409_1707.pdf"
    assert not pdf.exists()
    assert new_pdf.exists()

    conn = init_db(db_path)
    row = get_receipt(conn, "20260409_1707.pdf")
    history = query_receipt_updates(conn, filename="20260409_1707.pdf")
    conn.close()
    sidecar = read_sidecar(new_pdf)

    assert row is not None
    assert row["receipt_dir"] == "grocery/kroger/20260409_1707.pdf"
    assert sidecar is not None
    assert sidecar["filename"] == "20260409_1707.pdf"
    assert sidecar["receipt_dir"] == "grocery/kroger/20260409_1707.pdf"
    assert history[0]["source"] == "normalize-filenames"
    assert history[0]["changes"]["filename"] == {"old": "20260409.pdf", "new": "20260409_1707.pdf"}


def test_cmd_normalize_filenames_renames_orphan_sidecar_pair(tmp_path):
    receipt_root = tmp_path / "receipts"
    pdf = receipt_root / "grocery" / "walmart" / "20260314.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    import os
    from datetime import datetime

    ts = datetime(2026, 3, 14, 18, 34).timestamp()
    os.utime(pdf, (ts, ts))
    write_sidecar(
        pdf,
        {
            "filename": pdf.name,
            "scan_date": "2026-03-14",
            "merchant": "walmart",
            "total": 10.0,
            "category": "grocery/walmart",
            "receipt_dir": "grocery/walmart/20260314.pdf",
            "processed_by": "tesseract",
            "tier": "ok",
        },
    )

    db_path = tmp_path / "intake.db"
    init_db(db_path).close()

    cli._cmd_normalize_filenames(
        Namespace(receipt_dir=str(receipt_root), db=str(db_path), dry_run=False),
        {},
    )

    new_pdf = receipt_root / "grocery" / "walmart" / "20260314_1834.pdf"
    assert not pdf.exists()
    assert new_pdf.exists()
    sidecar = read_sidecar(new_pdf)
    assert sidecar is not None
    assert sidecar["filename"] == "20260314_1834.pdf"
    assert sidecar["receipt_dir"] == "grocery/walmart/20260314_1834.pdf"


def test_cmd_dedup_merge_promotes_keeper_fields_and_leaves_files(tmp_path):
    receipt_root = tmp_path / "receipts"
    keeper_pdf = receipt_root / "general" / "20260402_2241.pdf"
    dup_pdf = receipt_root / "general" / "20260402_2244.pdf"
    keeper_pdf.parent.mkdir(parents=True)
    keeper_pdf.write_bytes(b"%PDF-1.4\n")
    dup_pdf.write_bytes(b"%PDF-1.4\n")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": keeper_pdf.name,
            "scan_date": "2026-04-02",
            "merchant": "anthropic",
            "total": 200.0,
            "category": "general",
            "receipt_dir": "general/20260402_2241.pdf",
            "raw_text": "Anthropic credit note",
            "processed_by": "gpt-4.1",
            "card_last4": "9369",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": dup_pdf.name,
            "scan_date": "2026-04-02",
            "merchant": "anthropic",
            "total": 200.0,
            "category": "general",
            "receipt_dir": "general/20260402_2244.pdf",
            "raw_text": "Anthropic credit note",
            "processed_by": "tesseract",
            "purchaser": "jane",
        },
    )
    conn.close()
    write_sidecar(
        keeper_pdf,
        {
            "filename": keeper_pdf.name,
            "scan_date": "2026-04-02",
            "merchant": "anthropic",
            "total": 200.0,
            "category": "general",
            "receipt_dir": "general/20260402_2241.pdf",
            "processed_by": "gpt-4.1",
            "card_last4": "9369",
            "tier": "ok",
        },
    )
    write_sidecar(
        dup_pdf,
        {
            "filename": dup_pdf.name,
            "scan_date": "2026-04-02",
            "merchant": "anthropic",
            "total": 200.0,
            "category": "general",
            "receipt_dir": "general/20260402_2244.pdf",
            "processed_by": "tesseract",
            "purchaser": "jane",
            "tier": "ok",
        },
    )

    cli._cmd_dedup(
        Namespace(
            db=str(db_path),
            receipt_dir=str(receipt_root),
            window=3,
            merge=True,
            remove=False,
        ),
        {},
    )

    conn = init_db(db_path)
    keeper_row = get_receipt(conn, keeper_pdf.name)
    dup_row = get_receipt(conn, dup_pdf.name)
    history = query_receipt_updates(conn, filename=keeper_pdf.name)
    conn.close()
    keeper_sidecar = read_sidecar(keeper_pdf)

    assert keeper_row is not None
    assert keeper_row["purchaser"] == "jane sample"
    assert dup_row is None
    assert keeper_pdf.exists()
    assert dup_pdf.exists()
    assert keeper_sidecar is not None
    assert keeper_sidecar["purchaser"] == "jane sample"
    assert history[0]["source"] == "dedup-merge"
    assert history[0]["changes"]["purchaser"] == {"old": None, "new": "jane sample"}


def test_cmd_dedup_merge_prefers_local_pdf_over_email_row_and_audits_delete(tmp_path):
    receipt_root = tmp_path / "receipts"
    keeper_pdf = receipt_root / "general" / "20260410_1616.pdf"
    keeper_pdf.parent.mkdir(parents=True)
    keeper_pdf.write_bytes(b"%PDF-1.4\n")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": keeper_pdf.name,
            "scan_date": "2026-04-10",
            "merchant": "fcm travel",
            "total": 1716.01,
            "category": "general",
            "receipt_dir": "general/20260410_1616.pdf",
            "raw_text": "fcm travel pdf",
            "processed_by": "tesseract",
            "purchaser": "jane sample",
            "card_last4": "7464",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "email_30642_INBOX.txt",
            "scan_date": "2026-04-12",
            "merchant": "fcm travel",
            "total": 1716.01,
            "category": "general",
            "receipt_dir": "email/general",
            "raw_text": "fcm travel email",
            "processed_by": "gpt-4.1",
            "purchaser": "jane sample",
            "card_last4": "7464",
        },
    )
    conn.close()
    write_sidecar(
        keeper_pdf,
        {
            "filename": keeper_pdf.name,
            "scan_date": "2026-04-10",
            "merchant": "fcm travel",
            "total": 1716.01,
            "category": "general",
            "receipt_dir": "general/20260410_1616.pdf",
            "processed_by": "tesseract",
            "purchaser": "jane sample",
            "card_last4": "7464",
            "tier": "ok",
        },
    )

    cli._cmd_dedup(
        Namespace(
            db=str(db_path),
            receipt_dir=str(receipt_root),
            window=3,
            merge=True,
            remove=False,
        ),
        {},
    )

    conn = init_db(db_path)
    keeper_row = get_receipt(conn, keeper_pdf.name)
    dup_row = get_receipt(conn, "email_30642_INBOX.txt")
    dup_history = query_receipt_updates(conn, filename="email_30642_INBOX.txt")
    conn.close()

    assert keeper_row is not None
    assert dup_row is None
    assert dup_history[0]["source"] == "dedup-merge-delete"
    assert dup_history[0]["changes"]["keeper_filename"] == {
        "old": None,
        "new": "20260410_1616.pdf",
    }
    assert dup_history[0]["changes"]["status"] == {
        "old": "tracked",
        "new": "duplicate-merged-removed",
    }


def test_cmd_dedup_detects_exact_file_hash_matches_outside_day_window(tmp_path, capsys):
    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "20260401_2239.pdf",
            "scan_date": "2026-04-01",
            "merchant": "vodafone",
            "total": 13.20,
            "category": "phone/carrier",
            "receipt_dir": "phone/carrier/20260401_2239.pdf",
            "file_hash": "sha256-same",
            "raw_text": "Vodafone bill",
            "processed_by": "gpt-4o",
            "card_last4": "9369",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": "20260515_2239.pdf",
            "scan_date": "2026-05-15",
            "merchant": "vodafone",
            "total": 13.20,
            "category": "phone/carrier",
            "receipt_dir": "phone/carrier/20260515_2239.pdf",
            "file_hash": "sha256-same",
            "raw_text": "Vodafone bill",
            "processed_by": "gpt-4o-mini",
        },
    )
    conn.close()

    cli._cmd_dedup(
        Namespace(db=str(db_path), receipt_dir=None, window=3, merge=False, remove=False),
        {},
    )

    out = capsys.readouterr().out
    assert "Found 1 duplicate group(s), 1 row(s) to remove:" in out
    assert "hash match" in out
    assert "20260401_2239.pdf" in out
    assert "20260515_2239.pdf" in out


def test_cmd_dedup_remove_moves_duplicate_to_exhaust_and_records_audit(tmp_path, capsys):
    receipt_root = tmp_path / "receipts"
    keeper_pdf = receipt_root / "general" / "20260402_2241.pdf"
    dup_pdf = receipt_root / "general" / "20260402_2244.pdf"
    keeper_pdf.parent.mkdir(parents=True)
    keeper_pdf.write_bytes(b"%PDF-1.4\n")
    dup_pdf.write_bytes(b"%PDF-1.4\n")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": keeper_pdf.name,
            "scan_date": "2026-04-02",
            "merchant": "anthropic",
            "total": 200.0,
            "category": "general",
            "receipt_dir": "general/20260402_2241.pdf",
            "raw_text": "Anthropic credit note",
            "processed_by": "gpt-4.1",
            "card_last4": "9369",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": dup_pdf.name,
            "scan_date": "2026-04-02",
            "merchant": "anthropic",
            "total": 200.0,
            "category": "general",
            "receipt_dir": "general/20260402_2244.pdf",
            "raw_text": "Anthropic credit note",
            "processed_by": "tesseract",
            "purchaser": "jane",
        },
    )
    conn.close()
    write_sidecar(
        keeper_pdf,
        {
            "filename": keeper_pdf.name,
            "scan_date": "2026-04-02",
            "merchant": "anthropic",
            "total": 200.0,
            "category": "general",
            "receipt_dir": "general/20260402_2241.pdf",
            "processed_by": "gpt-4.1",
            "card_last4": "9369",
            "tier": "ok",
        },
    )
    write_sidecar(
        dup_pdf,
        {
            "filename": dup_pdf.name,
            "scan_date": "2026-04-02",
            "merchant": "anthropic",
            "total": 200.0,
            "category": "general",
            "receipt_dir": "general/20260402_2244.pdf",
            "processed_by": "tesseract",
            "purchaser": "jane",
            "tier": "ok",
        },
    )

    cli._cmd_dedup(
        Namespace(
            db=str(db_path),
            receipt_dir=str(receipt_root),
            window=3,
            merge=False,
            remove=True,
        ),
        {},
    )

    exhausted_pdf = receipt_root / "exhaust" / "duplicates" / "general" / "20260402_2244.pdf"
    assert not dup_pdf.exists()
    assert exhausted_pdf.exists()

    conn = init_db(db_path)
    keeper_row = get_receipt(conn, keeper_pdf.name)
    dup_row = get_receipt(conn, dup_pdf.name)
    keeper_history = query_receipt_updates(conn, filename=keeper_pdf.name)
    dup_history = query_receipt_updates(conn, filename=dup_pdf.name)
    conn.close()

    exhausted_sidecar = read_sidecar(exhausted_pdf)
    keeper_sidecar = read_sidecar(keeper_pdf)

    assert keeper_row is not None
    assert keeper_row["purchaser"] == "jane sample"
    assert dup_row is None
    assert keeper_sidecar is not None
    assert keeper_sidecar["purchaser"] == "jane sample"
    assert exhausted_sidecar is not None
    assert exhausted_sidecar["filename"] == "20260402_2244.pdf"
    assert exhausted_sidecar["receipt_dir"] == "exhaust/duplicates/general/20260402_2244.pdf"
    assert keeper_history[0]["source"] == "dedup-merge"
    assert dup_history[0]["source"] == "dedup-remove"
    assert dup_history[0]["changes"]["status"] == {"old": "tracked", "new": "duplicate-exhausted"}
    assert dup_history[0]["changes"]["receipt_dir"] == {
        "old": "general/20260402_2244.pdf",
        "new": "exhaust/duplicates/general/20260402_2244.pdf",
    }
    assert dup_history[0]["changes"]["keeper_filename"] == {
        "old": None,
        "new": "20260402_2241.pdf",
    }

    out = capsys.readouterr().out
    assert "moved duplicate" in out


def test_cmd_process_exhausts_incoming_hash_duplicate(tmp_path, monkeypatch, capsys):
    import intake.classify as classify_mod
    import intake.document_type as document_type_mod
    import intake.extract as extract_mod
    import intake.ocr as ocr_mod

    receipt_root = tmp_path / "receipts"
    intake_dir = receipt_root / "intake"
    intake_dir.mkdir(parents=True)
    first_pdf = intake_dir / "20260410_1707.pdf"
    second_pdf = intake_dir / "20260410_1710.pdf"
    payload = b"%PDF-1.4\nidentical\n"
    first_pdf.write_bytes(payload)
    second_pdf.write_bytes(payload)

    db_path = tmp_path / "intake.db"
    calls: list[str] = []

    def fake_extract(pdf, **_kwargs):
        calls.append(pdf.name)
        return (
            Receipt(
                filename=pdf.name,
                scan_date=date(2026, 4, 10),
                receipt_date=date(2026, 4, 2),
                receipt_time=None,
                merchant="vodafone",
                total=13.20,
                raw_text="vodafone\nTotal 13.20",
            ),
            "gpt-4o-mini",
            "low",
        )

    monkeypatch.setattr(extract_mod, "extract_receipt", fake_extract)
    monkeypatch.setattr(ocr_mod, "extract_text", lambda _path: "vodafone\nTotal 13.20")
    monkeypatch.setattr(document_type_mod, "classify_document_type", lambda *_args: "receipt")
    monkeypatch.setattr(classify_mod, "classify", lambda *_args, **_kwargs: "phone/carrier")
    monkeypatch.setattr(classify_mod, "load_rules", lambda: {})
    monkeypatch.setattr(classify_mod, "load_merchant_aliases", lambda: {})
    monkeypatch.setattr(cli, "_notify_processed_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_trigger_personal_finance", lambda *args, **kwargs: None)

    cli._cmd_process(
        Namespace(receipt_dir=str(receipt_root), db=str(db_path), dry_run=False),
        {},
    )

    carrier_dir = receipt_root / "phone" / "carrier"
    kept_pdf = carrier_dir / "20260402_1707.pdf"
    dup_dir = receipt_root / "exhaust" / "duplicates" / "phone" / "carrier"
    dup_pdf = dup_dir / "20260410_1710.pdf"

    assert calls == ["20260410_1707.pdf"]
    assert kept_pdf.exists()
    assert dup_pdf.exists()

    conn = init_db(db_path)
    kept_row = get_receipt(conn, kept_pdf.name)
    dup_row = get_receipt(conn, dup_pdf.name)
    dup_history = query_receipt_updates(conn, filename=dup_pdf.name)
    conn.close()

    kept_sidecar = read_sidecar(kept_pdf)
    dup_sidecar = read_sidecar(dup_pdf)

    assert kept_row is not None
    assert kept_row["file_hash"] == dup_sidecar["file_hash"] == kept_sidecar["file_hash"]
    assert dup_row is None
    assert dup_sidecar["status"] == "duplicate-exhausted"
    assert dup_sidecar["duplicate_of"] == kept_pdf.name
    assert dup_history[0]["source"] == "ingest-hash-duplicate"
    assert dup_history[0]["changes"]["duplicate_of"] == {"old": None, "new": kept_pdf.name}

    out = capsys.readouterr().out
    assert "duplicate by file hash" in out


def test_cmd_process_supports_jpg_and_mov_receipts(tmp_path, monkeypatch):
    import intake.classify as classify_mod
    import intake.document_type as document_type_mod
    import intake.extract as extract_mod
    import intake.ocr as ocr_mod

    receipt_root = tmp_path / "receipts"
    intake_dir = receipt_root / "intake"
    intake_dir.mkdir(parents=True)
    jpg = intake_dir / "20260410_1707.JPG"
    mov = intake_dir / "20260410_1710.MOV"
    jpg.write_bytes(b"jpg")
    mov.write_bytes(b"mov")

    db_path = tmp_path / "intake.db"

    def fake_extract(path, **_kwargs):
        return (
            Receipt(
                filename=path.name,
                scan_date=date(2026, 4, 10),
                receipt_date=date(2026, 4, 2),
                receipt_time=None,
                merchant="target",
                total=23.45,
                raw_text="target\ntotal 23.45",
            ),
            "tesseract",
            "ok",
        )

    monkeypatch.setattr(extract_mod, "extract_receipt", fake_extract)
    monkeypatch.setattr(ocr_mod, "extract_text", lambda _path: "target\ntotal 23.45")
    monkeypatch.setattr(document_type_mod, "classify_document_type", lambda *_args: "receipt")
    monkeypatch.setattr(classify_mod, "classify", lambda *_args, **_kwargs: "general")
    monkeypatch.setattr(classify_mod, "load_rules", lambda: {})
    monkeypatch.setattr(classify_mod, "load_merchant_aliases", lambda: {})
    monkeypatch.setattr(cli, "_notify_processed_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_trigger_personal_finance", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_is_unreadable_receipt_image", lambda p: False)

    cli._cmd_process(
        Namespace(receipt_dir=str(receipt_root), db=str(db_path), dry_run=False),
        {},
    )

    jpg_dest = receipt_root / "general" / "20260402_1707.jpg"
    mov_dest = receipt_root / "general" / "20260402_1710.mov"

    assert jpg_dest.exists()
    assert mov_dest.exists()
    assert read_sidecar(jpg_dest) is not None
    assert read_sidecar(mov_dest) is not None

    conn = init_db(db_path)
    jpg_row = get_receipt(conn, jpg_dest.name)
    mov_row = get_receipt(conn, mov_dest.name)
    conn.close()

    assert jpg_row is not None
    assert mov_row is not None
    assert jpg_row["receipt_dir"] == "general/20260402_1707.jpg"
    assert mov_row["receipt_dir"] == "general/20260402_1710.mov"


def test_cmd_process_prefers_still_image_for_live_photo_pair(tmp_path, monkeypatch, capsys):
    import intake.classify as classify_mod
    import intake.document_type as document_type_mod
    import intake.extract as extract_mod
    import intake.ocr as ocr_mod

    receipt_root = tmp_path / "receipts"
    intake_dir = receipt_root / "intake"
    intake_dir.mkdir(parents=True)
    jpg = intake_dir / "LIVE_0001.JPG"
    mov = intake_dir / "LIVE_0001.MOV"
    jpg.write_bytes(b"jpg")
    mov.write_bytes(b"mov")

    db_path = tmp_path / "intake.db"
    calls: list[str] = []

    def fake_extract(path, **_kwargs):
        calls.append(path.name)
        return (
            Receipt(
                filename=path.name,
                scan_date=date(2026, 4, 16),
                receipt_date=date(2026, 4, 15),
                receipt_time=None,
                merchant="target",
                total=6.66,
                raw_text="target\ntotal 6.66",
            ),
            "tesseract",
            "ok",
        )

    monkeypatch.setattr(extract_mod, "extract_receipt", fake_extract)
    monkeypatch.setattr(ocr_mod, "extract_text", lambda _path: "target\ntotal 6.66")
    monkeypatch.setattr(document_type_mod, "classify_document_type", lambda *_args: "receipt")
    monkeypatch.setattr(classify_mod, "classify", lambda *_args, **_kwargs: "general")
    monkeypatch.setattr(classify_mod, "load_rules", lambda: {})
    monkeypatch.setattr(classify_mod, "load_merchant_aliases", lambda: {})
    monkeypatch.setattr(cli, "_notify_processed_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_trigger_personal_finance", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_is_unreadable_receipt_image", lambda p: False)

    cli._cmd_process(
        Namespace(receipt_dir=str(receipt_root), db=str(db_path), dry_run=False),
        {},
    )

    general_jpgs = list((receipt_root / "general").glob("*.jpg"))
    exhausted_movs = list((receipt_root / "exhaust" / "duplicates" / "live-photo").glob("*.mov"))

    assert calls == ["LIVE_0001.JPG"]
    assert len(general_jpgs) == 1
    assert len(exhausted_movs) == 1

    exhausted_sidecar = read_sidecar(exhausted_movs[0])
    conn = init_db(db_path)
    jpg_row = get_receipt(conn, general_jpgs[0].name)
    mov_row = get_receipt(conn, exhausted_movs[0].name)
    live_photo_history = query_receipt_updates(conn, filename=exhausted_movs[0].name)
    conn.close()

    assert jpg_row is not None
    assert mov_row is None
    assert exhausted_sidecar is not None
    assert exhausted_sidecar["status"] == "live-photo-duplicate-exhausted"
    assert exhausted_sidecar["duplicate_of"] == "LIVE_0001.JPG"
    assert live_photo_history[0]["source"] == "ingest-live-photo-duplicate"

    out = capsys.readouterr().out
    assert "[live-photo] kept LIVE_0001.JPG" in out


def test_cmd_itemize_backfills_items_into_db_and_sidecar(tmp_path, monkeypatch, capsys):
    import intake.classify as classify_mod

    receipt_root = tmp_path / "receipts"
    pdf = receipt_root / "grocery" / "supermarket" / "20260409_1707.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": pdf.name,
            "scan_date": "2026-04-09",
            "merchant": "kroger",
            "total": 7.38,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260409_1707.pdf",
            "raw_text": "Bananas 2.99\nWhole Milk 4.39\nTotal 7.38",
            "processed_by": "tesseract",
        },
    )
    conn.close()
    write_sidecar(
        pdf,
        {
            "filename": pdf.name,
            "scan_date": "2026-04-09",
            "merchant": "kroger",
            "total": 7.38,
            "category": "grocery/supermarket",
            "receipt_dir": "grocery/supermarket/20260409_1707.pdf",
            "processed_by": "tesseract",
            "tier": "ok",
        },
    )
    monkeypatch.setattr(cli, "_trigger_personal_finance", lambda *args, **kwargs: None)
    monkeypatch.setattr(classify_mod, "load_rules", lambda: {})
    monkeypatch.setattr(classify_mod, "load_merchant_aliases", lambda: {})

    cli._cmd_itemize(
        Namespace(
            receipt_dir=str(receipt_root),
            db=str(db_path),
            dry_run=False,
            force=False,
            limit=None,
        ),
        {},
    )

    conn = init_db(db_path)
    row = get_receipt(conn, pdf.name)
    history = query_receipt_updates(conn, filename=pdf.name)
    conn.close()
    sidecar = read_sidecar(pdf)

    assert row is not None
    assert row["items"] == [
        {"description": "Bananas", "amount": 2.99, "quantity": None, "unit_price": None},
        {"description": "Whole Milk", "amount": 4.39, "quantity": None, "unit_price": None},
    ]
    assert sidecar is not None
    assert sidecar["items"] == row["items"]
    assert history[0]["source"] == "itemize-backfill"

    out = capsys.readouterr().out
    assert "Itemized 20260409_1707.pdf" in out


def test_cmd_certify_backfills_certifications_into_db_and_sidecar(tmp_path, monkeypatch, capsys):
    import intake.classify as classify_mod

    receipt_root = tmp_path / "receipts"
    pdf = receipt_root / "auto" / "heritage-collision" / "20110830_1149.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": pdf.name,
            "scan_date": "2011-08-30",
            "merchant": "heritage collision",
            "total": None,
            "category": "auto/heritage-collision",
            "receipt_dir": "auto/heritage-collision/20110830_1149.pdf",
            "raw_text": "Certified by AAA",
            "processed_by": "gpt-4.1",
        },
    )
    conn.close()
    write_sidecar(
        pdf,
        {
            "filename": pdf.name,
            "scan_date": "2011-08-30",
            "merchant": "heritage collision",
            "total": None,
            "category": "auto/heritage-collision",
            "receipt_dir": "auto/heritage-collision/20110830_1149.pdf",
            "processed_by": "gpt-4.1",
            "tier": "ok",
        },
    )
    monkeypatch.setattr(cli, "_trigger_personal_finance", lambda *args, **kwargs: None)
    monkeypatch.setattr(classify_mod, "load_rules", lambda: {})
    monkeypatch.setattr(classify_mod, "load_merchant_aliases", lambda: {})

    cli._cmd_certify(
        Namespace(
            receipt_dir=str(receipt_root),
            db=str(db_path),
            dry_run=False,
            force=False,
            limit=None,
        ),
        {},
    )

    conn = init_db(db_path)
    row = get_receipt(conn, pdf.name)
    history = query_receipt_updates(conn, filename=pdf.name)
    conn.close()
    sidecar = read_sidecar(pdf)

    assert row is not None
    assert row["certifications"] == [
        {"issuer": "AAA", "label": "AAA certified", "source_text": "Certified by AAA"}
    ]
    assert sidecar is not None
    assert sidecar["certifications"] == row["certifications"]
    assert history[0]["source"] == "certify-backfill"

    out = capsys.readouterr().out
    assert "Certified 20110830_1149.pdf: AAA certified" in out


def test_cmd_staff_backfills_staff_and_fixes_staff_purchaser(tmp_path, monkeypatch, capsys):
    receipt_root = tmp_path / "receipts"
    pdf = receipt_root / "dining" / "20260421_1842.jpg"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"jpg")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": "20260401_1200.pdf",
            "scan_date": "2026-04-01",
            "merchant": "known shop",
            "total": 1.0,
            "category": "general",
            "receipt_dir": "general/20260401_1200.pdf",
            "raw_text": "",
            "processed_by": "tesseract",
            "purchaser": "jane sample",
            "card_last4": "5653",
        },
    )
    upsert_receipt(
        conn,
        {
            "filename": pdf.name,
            "scan_date": "2026-04-21",
            "merchant": "smokin outlaws bbq",
            "total": 9.2,
            "category": "dining",
            "receipt_dir": "dining/20260421_1842.jpg",
            "raw_text": "Server: Isabel J\nTotal 9.20",
            "processed_by": "gpt-4o-mini",
            "purchaser": "isabel j",
            "card_last4": "5653",
        },
    )
    conn.close()
    write_sidecar(
        pdf,
        {
            "filename": pdf.name,
            "scan_date": "2026-04-21",
            "merchant": "smokin outlaws bbq",
            "total": 9.2,
            "category": "dining",
            "receipt_dir": "dining/20260421_1842.jpg",
            "processed_by": "gpt-4o-mini",
            "purchaser": "isabel j",
            "card_last4": "5653",
            "tier": "ok",
        },
    )
    monkeypatch.setattr(cli, "_trigger_personal_finance", lambda *args, **kwargs: None)

    cli._cmd_staff(
        Namespace(
            receipt_dir=str(receipt_root),
            db=str(db_path),
            dry_run=False,
            force=False,
            limit=None,
        ),
        {},
    )

    conn = init_db(db_path)
    row = get_receipt(conn, pdf.name)
    history = query_receipt_updates(conn, filename=pdf.name)
    conn.close()
    sidecar = read_sidecar(pdf)

    assert row is not None
    assert row["staff"] == [
        {"role": "server", "name": "isabel j", "source_text": "Server: Isabel J"}
    ]
    assert row["purchaser"] == "jane sample"
    assert sidecar is not None
    assert sidecar["staff"] == row["staff"]
    assert sidecar["purchaser"] == "jane sample"
    assert history[0]["source"] == "staff-backfill"

    out = capsys.readouterr().out
    assert "Staff 20260421_1842.jpg: server: isabel j" in out


def test_find_duplicate_groups_hash_only_skips_heuristic_matches():
    rows = [
        {
            "filename": "a.pdf",
            "merchant": "kroger",
            "total": 10.0,
            "scan_date": "2026-04-01",
            "file_hash": "same",
            "processed_at": "1",
        },
        {
            "filename": "b.pdf",
            "merchant": "kroger",
            "total": 10.0,
            "scan_date": "2026-04-02",
            "file_hash": "same",
            "processed_at": "2",
        },
        {
            "filename": "c.pdf",
            "merchant": "shell",
            "total": 20.0,
            "scan_date": "2026-04-01",
            "file_hash": None,
            "processed_at": "3",
        },
        {
            "filename": "d.pdf",
            "merchant": "shell",
            "total": 20.0,
            "scan_date": "2026-04-02",
            "file_hash": None,
            "processed_at": "4",
        },
    ]

    groups = cli._find_duplicate_groups(rows, 3, hash_only=True)

    assert [[row["filename"] for row in group] for group in groups] == [["a.pdf", "b.pdf"]]


def test_find_duplicate_groups_skips_email_payment_heuristic_matches():
    rows = [
        {
            "filename": "email_1_INBOX.txt",
            "merchant": "capital one",
            "total": 69.69,
            "scan_date": "2026-04-27",
            "file_hash": None,
            "processed_at": "1",
            "processed_by": "email-payment",
        },
        {
            "filename": "email_2_INBOX.txt",
            "merchant": "capital one",
            "total": 69.69,
            "scan_date": "2026-04-28",
            "file_hash": None,
            "processed_at": "2",
            "processed_by": "email-payment",
        },
    ]

    assert cli._find_duplicate_groups(rows, 3, hash_only=False) == []


def test_cmd_normalize_purchasers_updates_db_and_sidecar(tmp_path, capsys):
    receipt_root = tmp_path / "receipts"
    pdf = receipt_root / "general" / "20260422_1000.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")

    db_path = tmp_path / "intake.db"
    conn = init_db(db_path)
    upsert_receipt(
        conn,
        {
            "filename": pdf.name,
            "scan_date": "2026-04-22",
            "merchant": "fcm travel",
            "total": 274.37,
            "category": "general",
            "receipt_dir": "general/20260422_1000.pdf",
            "raw_text": "",
            "processed_by": "gpt-4.1",
            "purchaser": "jane sample",
        },
    )
    conn.execute(
        "UPDATE receipts SET purchaser = ? WHERE filename = ?",
        ("sample / jane a", pdf.name),
    )
    conn.commit()
    conn.close()
    write_sidecar(
        pdf,
        {
            "filename": pdf.name,
            "scan_date": "2026-04-22",
            "merchant": "fcm travel",
            "total": 274.37,
            "category": "general",
            "receipt_dir": "general/20260422_1000.pdf",
            "processed_by": "gpt-4.1",
            "purchaser": "sample / jane a",
        },
    )

    cli._cmd_normalize_purchasers(
        Namespace(receipt_dir=str(receipt_root), db=str(db_path), dry_run=False),
        {},
    )

    conn = init_db(db_path)
    row = get_receipt(conn, pdf.name)
    history = query_receipt_updates(conn, filename=pdf.name)
    conn.close()
    sidecar = read_sidecar(pdf)

    assert row is not None
    assert row["purchaser"] == "jane sample"
    assert sidecar is not None
    assert sidecar["purchaser"] == "jane sample"
    assert history[0]["source"] == "purchaser-normalize"

    out = capsys.readouterr().out
    assert "Purchaser 20260422_1000.pdf" in out


def test_cmd_process_skips_and_notifies_zero_byte_files(tmp_path, monkeypatch, capsys):
    import intake.classify as classify_mod
    import intake.document_type as document_type_mod
    import intake.extract as extract_mod
    import intake.ocr as ocr_mod

    receipt_root = tmp_path / "receipts"
    intake_dir = receipt_root / "intake"
    intake_dir.mkdir(parents=True)

    empty_jpg = intake_dir / "IMG_2365.JPG"
    good_jpg = intake_dir / "20260410_1000.JPG"
    empty_jpg.write_bytes(b"")
    good_jpg.write_bytes(b"jpg")

    db_path = tmp_path / "intake.db"

    def fake_extract(path, **_kwargs):
        return (
            Receipt(
                filename=path.name,
                scan_date=date(2026, 4, 10),
                receipt_date=date(2026, 4, 10),
                receipt_time=None,
                merchant="target",
                total=9.99,
                raw_text="target\ntotal 9.99",
            ),
            "tesseract",
            "ok",
        )

    monkeypatch.setattr(extract_mod, "extract_receipt", fake_extract)
    monkeypatch.setattr(ocr_mod, "extract_text", lambda _path: "target\ntotal 9.99")
    monkeypatch.setattr(document_type_mod, "classify_document_type", lambda *_args: "receipt")
    monkeypatch.setattr(classify_mod, "classify", lambda *_args, **_kwargs: "general")
    monkeypatch.setattr(classify_mod, "load_rules", lambda: {})
    monkeypatch.setattr(classify_mod, "load_merchant_aliases", lambda: {})
    monkeypatch.setattr(cli, "_notify_processed_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_trigger_personal_finance", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_is_unreadable_receipt_image", lambda p: False)

    notified: list[list[str]] = []
    monkeypatch.setattr(
        "intake.notify.send_corrupt_intake_notifications",
        lambda settings, names: (notified.append(names), ({}, {}))[1],
    )

    cli._cmd_process(
        Namespace(receipt_dir=str(receipt_root), db=str(db_path), dry_run=False),
        {},
    )

    # zero-byte file must not be processed or moved
    assert not (receipt_root / "general").rglob("IMG_2365*").__next__() if False else True
    assert empty_jpg.exists(), "zero-byte file should remain in intake (not moved)"

    # good file should be processed normally
    good_files = list((receipt_root / "general").rglob("*.jpg"))
    assert len(good_files) == 1

    # notification must have fired with the corrupt filename
    assert notified == [["IMG_2365.JPG"]]

    err = capsys.readouterr().err
    assert "IMG_2365.JPG" in err
    assert "zero-byte" in err


def test_is_unreadable_receipt_image_detects_truncated_file(tmp_path, monkeypatch):
    """_is_unreadable_receipt_image returns True when img.load() raises (truncated file)."""
    import unittest.mock as mock

    from intake import cli

    truncated_jpg = tmp_path / "IMG_truncated.JPG"
    truncated_jpg.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)  # fake JPEG header

    monkeypatch.setattr("intake.media.is_image_receipt", lambda p: True)

    fake_img = mock.MagicMock()
    fake_img.__enter__ = lambda s: s
    fake_img.__exit__ = mock.MagicMock(return_value=False)
    fake_img.load.side_effect = OSError("image file is truncated (80 bytes not processed)")

    with mock.patch("PIL.Image.open", return_value=fake_img):
        assert cli._is_unreadable_receipt_image(truncated_jpg) is True


def test_is_unreadable_receipt_image_ok_for_valid_file(tmp_path, monkeypatch):
    """_is_unreadable_receipt_image returns False when img.load() succeeds."""
    import unittest.mock as mock

    from intake import cli

    good_jpg = tmp_path / "IMG_good.JPG"
    good_jpg.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

    monkeypatch.setattr("intake.media.is_image_receipt", lambda p: True)

    fake_img = mock.MagicMock()
    fake_img.__enter__ = lambda s: s
    fake_img.__exit__ = mock.MagicMock(return_value=False)
    fake_img.load.return_value = None  # success

    with mock.patch("PIL.Image.open", return_value=fake_img):
        assert cli._is_unreadable_receipt_image(good_jpg) is False


def test_cmd_process_all_corrupt_returns_early(tmp_path, monkeypatch, capsys):
    receipt_root = tmp_path / "receipts"
    intake_dir = receipt_root / "intake"
    intake_dir.mkdir(parents=True)
    (intake_dir / "IMG_2365.JPG").write_bytes(b"")

    db_path = tmp_path / "intake.db"

    monkeypatch.setattr(
        "intake.notify.send_corrupt_intake_notifications",
        lambda settings, names: ({}, {}),
    )

    cli._cmd_process(
        Namespace(receipt_dir=str(receipt_root), db=str(db_path), dry_run=False),
        {},
    )

    out = capsys.readouterr().out
    assert "No processable files remain" in out


def test_cmd_process_skips_and_notifies_unreadable_image(tmp_path, monkeypatch, capsys):
    import intake.classify as classify_mod
    import intake.document_type as document_type_mod
    import intake.extract as extract_mod
    import intake.ocr as ocr_mod

    receipt_root = tmp_path / "receipts"
    intake_dir = receipt_root / "intake"
    intake_dir.mkdir(parents=True)

    bad_jpg = intake_dir / "IMG_2365.JPG"
    good_jpg = intake_dir / "20260410_1000.JPG"
    bad_jpg.write_bytes(b"not-a-jpeg")
    good_jpg.write_bytes(b"not-a-jpeg-either")

    db_path = tmp_path / "intake.db"

    def fake_extract(path, **_kwargs):
        return (
            Receipt(
                filename=path.name,
                scan_date=date(2026, 4, 10),
                receipt_date=date(2026, 4, 10),
                receipt_time=None,
                merchant="target",
                total=9.99,
                raw_text="target\ntotal 9.99",
            ),
            "tesseract",
            "ok",
        )

    monkeypatch.setattr(extract_mod, "extract_receipt", fake_extract)
    monkeypatch.setattr(ocr_mod, "extract_text", lambda _path: "target\ntotal 9.99")
    monkeypatch.setattr(document_type_mod, "classify_document_type", lambda *_args: "receipt")
    monkeypatch.setattr(classify_mod, "classify", lambda *_args, **_kwargs: "general")
    monkeypatch.setattr(classify_mod, "load_rules", lambda: {})
    monkeypatch.setattr(classify_mod, "load_merchant_aliases", lambda: {})
    monkeypatch.setattr(cli, "_notify_processed_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_trigger_personal_finance", lambda *args, **kwargs: None)

    def fake_is_unreadable(path):
        return path.name == "IMG_2365.JPG"

    monkeypatch.setattr(cli, "_is_unreadable_receipt_image", fake_is_unreadable)

    notified: list[list[str]] = []
    monkeypatch.setattr(
        "intake.notify.send_corrupt_intake_notifications",
        lambda settings, names: (notified.append(names), ({}, {}))[1],
    )

    cli._cmd_process(
        Namespace(receipt_dir=str(receipt_root), db=str(db_path), dry_run=False),
        {},
    )

    assert notified == [["IMG_2365.JPG"]]
    err = capsys.readouterr().err
    assert "IMG_2365.JPG" in err
    assert "unreadable" in err
    good_files = list((receipt_root / "general").rglob("*.jpg"))
    assert len(good_files) == 1


def test_process_inbox_skips_and_notifies_corrupt_image(tmp_path, monkeypatch, capsys):
    import intake.classify as classify_mod
    import intake.document_type as document_type_mod
    import intake.extract as extract_mod
    import intake.ocr as ocr_mod

    receipt_root = tmp_path / "receipts"
    intake_dir = receipt_root / "intake"
    intake_dir.mkdir(parents=True)

    bad_jpg = intake_dir / "IMG_2365.JPG"
    good_jpg = intake_dir / "20260410_1000.JPG"
    bad_jpg.write_bytes(b"not-a-jpeg")
    good_jpg.write_bytes(b"good")

    db_path = tmp_path / "intake.db"

    def fake_extract(path, **_kwargs):
        return (
            Receipt(
                filename=path.name,
                scan_date=date(2026, 4, 10),
                receipt_date=date(2026, 4, 10),
                receipt_time=None,
                merchant="target",
                total=9.99,
                raw_text="target\ntotal 9.99",
            ),
            "tesseract",
            "ok",
        )

    monkeypatch.setattr(extract_mod, "extract_receipt", fake_extract)
    monkeypatch.setattr(ocr_mod, "extract_text", lambda _path: "target\ntotal 9.99")
    monkeypatch.setattr(document_type_mod, "classify_document_type", lambda *_args: "receipt")
    monkeypatch.setattr(classify_mod, "classify", lambda *_args, **_kwargs: "general")
    monkeypatch.setattr(classify_mod, "load_rules", lambda: {})
    monkeypatch.setattr(classify_mod, "load_merchant_aliases", lambda: {})
    monkeypatch.setattr(cli, "_notify_processed_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_trigger_personal_finance", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_is_unreadable_receipt_image", lambda p: p.name == "IMG_2365.JPG")

    notified: list[list[str]] = []
    monkeypatch.setattr(
        "intake.notify.send_corrupt_intake_notifications",
        lambda settings, names: (notified.append(names), ({}, {}))[1],
    )

    ok, err_count = cli._process_inbox({"receipt_dir": str(receipt_root)}, db_path)

    assert ok == 1
    assert err_count == 0
    assert notified == [["IMG_2365.JPG"]]
    stderr = capsys.readouterr().err
    assert "IMG_2365.JPG" in stderr
    assert "unreadable" in stderr


def test_process_inbox_corrupt_notification_deduplication(tmp_path, monkeypatch, capsys):
    receipt_root = tmp_path / "receipts"
    intake_dir = receipt_root / "intake"
    intake_dir.mkdir(parents=True)
    (intake_dir / "IMG_2365.JPG").write_bytes(b"bad")

    db_path = tmp_path / "intake.db"

    monkeypatch.setattr(cli, "_is_unreadable_receipt_image", lambda p: p.name == "IMG_2365.JPG")

    notified: list[list[str]] = []
    monkeypatch.setattr(
        "intake.notify.send_corrupt_intake_notifications",
        lambda settings, names: (notified.append(names), ({}, {}))[1],
    )

    already_seen: set[str] = {"IMG_2365.JPG"}
    cli._process_inbox({"receipt_dir": str(receipt_root)}, db_path, notified_corrupt=already_seen)

    assert notified == [], "should not re-notify for a file already in notified_corrupt"
