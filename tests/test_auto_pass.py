from __future__ import annotations

import importlib
import sys
from pathlib import Path

DYNO_LAB_SRC = Path(__file__).resolve().parents[2] / "dyno-lab" / "src"
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


def test_load_settings_resolves_openai_key_via_auto_pass(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    settings_path = repo_root / "config" / "settings.toml"
    auto_pass_config_path = repo_root / "config" / "auto-pass.ini"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        'receipt_dir = "receipts"\n',
        encoding="utf-8",
    )
    auto_pass_config_path.write_text(
        "\n".join(
            [
                "[auto_pass]",
                "profile = work",
                "",
                "[intake]",
                "openai_api_key_entry = api/openai",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "_REPO_ROOT", repo_root)
    monkeypatch.setattr(cli, "_SETTINGS_PATH", settings_path)
    monkeypatch.setattr(cli, "_AUTO_PASS_CONFIG_PATH", auto_pass_config_path)

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
