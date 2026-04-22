from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path


def _resolve_dyno_lab_src() -> Path:
    worktree_guess = Path(__file__).resolve().parents[2] / "dyno-lab" / "src"
    if worktree_guess.exists():
        return worktree_guess

    common_dir = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=Path(__file__).resolve().parent,
        text=True,
    ).strip()
    for parent in Path(common_dir).resolve().parent.parents:
        candidate = parent / "util-repos" / "dyno-lab" / "src"
        if (candidate / "dyno_lab" / "__init__.py").exists():
            return candidate
    return worktree_guess


DYNO_LAB_SRC = _resolve_dyno_lab_src()
if str(DYNO_LAB_SRC) not in sys.path:
    sys.path.insert(0, str(DYNO_LAB_SRC))
for module_name in [
    name for name in sys.modules if name == "dyno_lab" or name.startswith("dyno_lab.")
]:
    del sys.modules[module_name]

auto_pass = importlib.import_module("dyno_lab.auto_pass")
cli = importlib.import_module("intake.cli")

AutoPassPatch = auto_pass.AutoPassPatch
AutoPassRecorder = auto_pass.AutoPassRecorder


def test_load_settings_resolves_openai_key_via_settings_toml(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    settings_path = repo_root / "config" / "settings.toml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        "\n".join(
            [
                'receipt_dir = "receipts"',
                'auto_pass_profile = "work"',
                'openai_api_key_entry = "api/openai"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "_REPO_ROOT", repo_root)
    monkeypatch.setattr(cli, "_SETTINGS_PATH", settings_path)

    recorder = AutoPassRecorder(
        responses={
            "api/openai": {"password": "sk-test-123"},
        }
    )

    with AutoPassPatch(recorder):
        settings = cli._load_settings()

    assert settings["auto_pass_profile"] == "work"
    assert settings["openai_api_key_entry"] == "api/openai"
    assert settings["openai_api_key"] == "sk-test-123"
    assert recorder.load_calls[0].profile == "work"
    assert str(recorder.load_calls[0].path).endswith("auto-pass/config/auto-pass.env.local")
    assert recorder.resolve_calls[0].entry == "api/openai"
    assert recorder.resolve_calls[0].attrs_map == {"password": "password"}


def test_load_settings_promotes_misnested_top_level_keys(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    settings_path = repo_root / "config" / "settings.toml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        "\n".join(
            [
                'receipt_dir = "receipts"',
                "",
                "[web]",
                'host = "127.0.0.1"',
                "port = 5200",
                'openai_api_key_entry = "api/openai"',
                'auto_pass_profile = "work"',
                'openai_model_low = "gpt-4o-mini"',
                "fuzzy_threshold = 0.91",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "_REPO_ROOT", repo_root)
    monkeypatch.setattr(cli, "_SETTINGS_PATH", settings_path)

    recorder = AutoPassRecorder(responses={"api/openai": {"password": "sk-test-456"}})

    with AutoPassPatch(recorder):
        settings = cli._load_settings()

    assert settings["openai_api_key_entry"] == "api/openai"
    assert settings["auto_pass_profile"] == "work"
    assert settings["openai_model_low"] == "gpt-4o-mini"
    assert settings["fuzzy_threshold"] == 0.91
    assert settings["openai_api_key"] == "sk-test-456"
    assert settings["web"] == {"host": "127.0.0.1", "port": 5200}
