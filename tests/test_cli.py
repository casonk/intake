"""Tests for intake.cli path resolution and systemd unit generation."""

from pathlib import Path

from intake import cli


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
    assert settings["personal_finance"]["repo_root"] == str(repo_root / "repos" / "finance-pipeline")
    assert settings["personal_finance"]["event_dir"] == str(
        repo_root / "data" / "finance-pipeline"
    )


def test_install_systemd_unit_avoids_repo_working_directory(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(cli.sys, "executable", "/usr/bin/python3")

    cli._install_systemd_unit({})

    unit_path = home / ".config" / "systemd" / "user" / "intake-daemon.service"
    unit = unit_path.read_text(encoding="utf-8")

    assert "WorkingDirectory=%h" in unit
    assert "StartLimitIntervalSec=0" in unit
    assert 'ExecStart=/bin/bash -lc' in unit
    assert 'until /usr/bin/python3 -c "import intake.cli" >/dev/null 2>&1;' in unit
    assert "exec /usr/bin/python3 -m intake.cli daemon" in unit
