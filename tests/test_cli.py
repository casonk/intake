"""Tests for intake.cli path resolution and clockwork-backed unit generation."""

import importlib.util
from pathlib import Path

import pytest

from intake import cli

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
