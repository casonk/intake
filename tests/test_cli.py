"""Tests for intake.cli path resolution and clockwork-backed unit generation."""

import importlib.util
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
                'repo_root = "repos/personal-finance"',
                'event_dir = "data/personal-finance"',
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
        repo_root / "repos" / "personal-finance"
    )
    assert settings["personal_finance"]["event_dir"] == str(repo_root / "data" / "personal-finance")


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
    assert sidecar["tier"] == "ok"
    assert history[0]["source"] == "manual-correct"
    assert history[0]["changes"]["total"] == {"old": 86.05, "new": 88.05}

    out = capsys.readouterr().out
    assert "Updated 20260409_1707.pdf" in out
    assert "sidecar:" in out


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
            "purchaser": "cason",
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
            "purchaser": "cason",
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
    assert keeper_row["purchaser"] == "cason konzer"
    assert dup_row is None
    assert keeper_pdf.exists()
    assert dup_pdf.exists()
    assert keeper_sidecar is not None
    assert keeper_sidecar["purchaser"] == "cason konzer"
    assert history[0]["source"] == "dedup-merge"
    assert history[0]["changes"]["purchaser"] == {"old": None, "new": "cason konzer"}


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
            "purchaser": "cason",
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
            "purchaser": "cason",
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
    assert keeper_row["purchaser"] == "cason konzer"
    assert dup_row is None
    assert keeper_sidecar is not None
    assert keeper_sidecar["purchaser"] == "cason konzer"
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
    import intake.extract as extract_mod

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
