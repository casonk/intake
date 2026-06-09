"""Unit tests for intake.translation."""

from __future__ import annotations

import sys
import types
from datetime import date

from intake.parse import Receipt
from intake.translation import (
    DEFAULT_TRANSLATION_BASE_URL,
    DEFAULT_TRANSLATION_MODEL,
    maybe_translate_receipt,
    needs_translation,
    translate_receipt_artifact,
    translate_receipt_text,
    translation_enabled,
)


def _receipt(**kwargs) -> Receipt:
    defaults = dict(
        filename="20260401_1000.pdf",
        scan_date=date(2026, 4, 1),
        receipt_date=None,
        receipt_time=None,
        merchant="lawson",
        total=1280.0,
        raw_text="ローソン\n合計 1280円",
        language="ja",
        translated_text=None,
    )
    defaults.update(kwargs)
    return Receipt(**defaults)


def test_needs_translation_only_for_non_english_without_translation():
    assert needs_translation(language="ja", translated_text=None, raw_text="ローソン") is True
    assert needs_translation(language="de", translated_text=None, raw_text="Gesamt 4,06 EUR") is True
    assert needs_translation(language="fr", translated_text=None, raw_text="Total TTC 9,00 €") is True
    assert needs_translation(language="en", translated_text=None, raw_text="Lawson") is False
    assert needs_translation(language="ja", translated_text="Lawson", raw_text="ローソン") is False


def test_translation_enabled_defaults_to_true_for_ollama():
    assert translation_enabled(None) is True
    assert translation_enabled("ollama") is True
    assert translation_enabled("none") is False


def test_translate_receipt_text_uses_crew_chief_client(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, *, base_url, model, timeout):
            captured["base_url"] = base_url
            captured["model"] = model
            captured["timeout"] = timeout

        def generate(self, prompt):
            captured["prompt"] = prompt
            return "Translation:\nLawson\nTotal 1,280 yen"

    monkeypatch.setattr(
        "intake.translation._load_crew_chief_client_class",
        lambda: FakeClient,
    )

    translated, metadata = translate_receipt_text(
        "ローソン\n合計 1280円",
        language="ja",
        provider="ollama",
        model=DEFAULT_TRANSLATION_MODEL,
        base_url=DEFAULT_TRANSLATION_BASE_URL,
        timeout_seconds=45,
    )

    assert translated == "Lawson\nTotal 1,280 yen"
    assert metadata == {"provider": "ollama", "model": DEFAULT_TRANSLATION_MODEL}
    assert captured["base_url"] == DEFAULT_TRANSLATION_BASE_URL
    assert captured["model"] == DEFAULT_TRANSLATION_MODEL
    assert captured["timeout"] == 45
    assert (
        "Translate the following receipt OCR text into concise, line-faithful English."
        in captured["prompt"]
    )


def test_translate_receipt_text_uses_openai_provider(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, *, api_key):
            captured["api_key"] = api_key
            self.chat = types.SimpleNamespace(completions=self)

        def create(self, *, model, messages, max_tokens):
            captured["model"] = model
            captured["messages"] = messages
            captured["max_tokens"] = max_tokens
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(
                            content="Translation:\nLawson\nTotal 1,280 yen"
                        )
                    )
                ]
            )

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeClient))

    translated, metadata = translate_receipt_text(
        "ローソン\n合計 1280円",
        language="ja",
        provider="openai",
        model="gpt-4.1",
        openai_api_key="sk-test",
    )

    assert translated == "Lawson\nTotal 1,280 yen"
    assert metadata == {"provider": "openai", "model": "gpt-4.1"}
    assert captured["api_key"] == "sk-test"
    assert captured["model"] == "gpt-4.1"
    assert captured["max_tokens"] == 900


def test_translate_receipt_artifact_ollama_uses_ocr_then_text_translation(monkeypatch, tmp_path):
    receipt_path = tmp_path / "receipt.jpg"
    receipt_path.write_bytes(b"fake-image")

    monkeypatch.setattr("intake.ocr.extract_text", lambda _path: "ローソン\n合計 1280円")
    monkeypatch.setattr(
        "intake.translation.translate_receipt_text",
        lambda raw_text, **_kwargs: (
            f"translated::{raw_text}",
            {"provider": "ollama", "model": "aya-expanse:8b"},
        ),
    )

    translated, metadata = translate_receipt_artifact(
        receipt_path,
        language="ja",
        provider="ollama",
    )

    assert translated == "translated::ローソン\n合計 1280円"
    assert metadata == {"provider": "ollama", "model": "aya-expanse:8b"}


def test_maybe_translate_receipt_sets_translated_text(monkeypatch):
    monkeypatch.setattr(
        "intake.translation.translate_receipt_text",
        lambda *_args, **_kwargs: (
            "Lawson\nTotal 1,280 yen",
            {"provider": "ollama", "model": "aya-expanse:8b"},
        ),
    )

    receipt = maybe_translate_receipt(_receipt())

    assert receipt.translated_text == "Lawson\nTotal 1,280 yen"
    assert receipt._debug_translation == {  # type: ignore[attr-defined]
        "provider": "ollama",
        "model": "aya-expanse:8b",
    }
