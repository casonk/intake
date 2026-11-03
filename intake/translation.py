"""Helpers for persisting English translations of non-English receipt OCR."""

from __future__ import annotations

import io
import re
import sys
from base64 import b64encode
from dataclasses import replace
from pathlib import Path

from intake.parse import Receipt

DEFAULT_TRANSLATION_PROVIDER = "ollama"
DEFAULT_TRANSLATION_MODEL = "aya-expanse:8b"
DEFAULT_OPENAI_TRANSLATION_MODEL = "gpt-4.1"
DEFAULT_TRANSLATION_BASE_URL = "http://localhost:11434"
DEFAULT_TRANSLATION_TIMEOUT_SECONDS = 60
_DISABLED_TRANSLATION_PROVIDERS = {"", "0", "false", "none", "off", "disabled"}
_LOCAL_TRANSLATION_PROVIDER = "ollama"
_KNOWN_TRANSLATION_PROVIDERS = {_LOCAL_TRANSLATION_PROVIDER, "openai"}


def normalize_translation_provider(value: object | None) -> str:
    """Return a normalized translation-provider name."""
    provider = str(value or DEFAULT_TRANSLATION_PROVIDER).strip().lower()
    return provider or DEFAULT_TRANSLATION_PROVIDER


def translation_enabled(value: object | None) -> bool:
    """Return True when translation should run for non-English receipts."""
    return normalize_translation_provider(value) not in _DISABLED_TRANSLATION_PROVIDERS


def needs_translation(
    *,
    language: str | None,
    translated_text: str | None,
    raw_text: str | None,
) -> bool:
    """Return True when a receipt has source OCR but no stored English translation."""
    normalized_language = str(language or "und").strip().lower()
    normalized_translation = str(translated_text or "").strip()
    normalized_raw = str(raw_text or "").strip()
    return (
        normalized_language not in {"", "en", "und"}
        and not normalized_translation
        and bool(normalized_raw)
    )


def maybe_translate_receipt(
    receipt: Receipt,
    *,
    provider: str | None = DEFAULT_TRANSLATION_PROVIDER,
    model: str = DEFAULT_TRANSLATION_MODEL,
    base_url: str = DEFAULT_TRANSLATION_BASE_URL,
    timeout_seconds: int = DEFAULT_TRANSLATION_TIMEOUT_SECONDS,
    local_model: str = DEFAULT_TRANSLATION_MODEL,
) -> Receipt:
    """Return *receipt* with ``translated_text`` filled when translation succeeds."""
    if not needs_translation(
        language=receipt.language,
        translated_text=receipt.translated_text,
        raw_text=receipt.raw_text,
    ):
        return receipt

    translated_text, metadata = translate_receipt_text(
        receipt.raw_text,
        language=receipt.language,
        provider=provider,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        local_model=local_model,
    )
    if not translated_text:
        return receipt

    updated = replace(receipt, translated_text=translated_text)
    ai_attempts = getattr(receipt, "_debug_ai_attempts", None)
    if isinstance(ai_attempts, list):
        updated._debug_ai_attempts = list(ai_attempts)  # type: ignore[attr-defined]
    if metadata:
        updated._debug_translation = metadata  # type: ignore[attr-defined]
    return updated


def translate_receipt_text(
    raw_text: str,
    *,
    language: str | None,
    provider: str | None = DEFAULT_TRANSLATION_PROVIDER,
    model: str = DEFAULT_TRANSLATION_MODEL,
    base_url: str = DEFAULT_TRANSLATION_BASE_URL,
    timeout_seconds: int = DEFAULT_TRANSLATION_TIMEOUT_SECONDS,
    openai_api_key: str | None = None,
    local_model: str = DEFAULT_TRANSLATION_MODEL,
) -> tuple[str | None, dict | None]:
    """Translate receipt OCR text into English using the configured provider.

    When a remote provider (OpenAI) fails, has no key, or returns nothing usable, the text is
    translated locally instead (the shared Crew Chief/Ollama service, with *local_model*), so a
    remote outage or a missing key never leaves a receipt untranslated. The returned metadata then
    names the provider that failed in ``fallback_from``. A disabled or unknown provider never
    falls back: that is a choice, not a failure.
    """
    normalized_provider = normalize_translation_provider(provider)
    if normalized_provider in _DISABLED_TRANSLATION_PROVIDERS:
        return None, None
    if normalized_provider not in _KNOWN_TRANSLATION_PROVIDERS:
        return None, None

    translated = _translate_with_provider(
        raw_text,
        language=language,
        provider=normalized_provider,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        openai_api_key=openai_api_key,
    )
    if translated and translated != raw_text.strip():
        return translated, {"provider": normalized_provider, "model": model}

    if normalized_provider == _LOCAL_TRANSLATION_PROVIDER:
        return None, None
    return _translate_locally(
        raw_text,
        language=language,
        failed_provider=normalized_provider,
        local_model=local_model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def _translate_with_provider(
    raw_text: str,
    *,
    language: str | None,
    provider: str,
    model: str,
    base_url: str,
    timeout_seconds: int,
    openai_api_key: str | None,
) -> str | None:
    """One translation attempt; any failure is ``None`` rather than an error."""
    try:
        if provider == _LOCAL_TRANSLATION_PROVIDER:
            prompt = _translation_prompt(raw_text, language=language)
            client_class = _load_crew_chief_client_class()
            client = client_class(base_url=base_url, model=model, timeout=timeout_seconds)
            return _sanitize_translation_output(client.generate(prompt))
        return _translate_text_with_openai(
            raw_text, language=language, api_key=openai_api_key, model=model
        )
    except Exception:  # noqa: BLE001
        return None


def _translate_locally(
    raw_text: str,
    *,
    language: str | None,
    failed_provider: str,
    local_model: str,
    base_url: str,
    timeout_seconds: int,
) -> tuple[str | None, dict | None]:
    """The local fallback: translate the locally read text with the local model."""
    translated = _translate_with_provider(
        raw_text,
        language=language,
        provider=_LOCAL_TRANSLATION_PROVIDER,
        model=local_model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        openai_api_key=None,
    )
    if not translated or translated == raw_text.strip():
        return None, None
    return translated, {
        "provider": _LOCAL_TRANSLATION_PROVIDER,
        "model": local_model,
        "fallback_from": failed_provider,
    }


def translate_receipt_artifact(
    receipt_path: Path,
    *,
    raw_text: str | None = None,
    language: str | None = None,
    provider: str | None = DEFAULT_TRANSLATION_PROVIDER,
    model: str = DEFAULT_TRANSLATION_MODEL,
    base_url: str = DEFAULT_TRANSLATION_BASE_URL,
    timeout_seconds: int = DEFAULT_TRANSLATION_TIMEOUT_SECONDS,
    openai_api_key: str | None = None,
    local_model: str = DEFAULT_TRANSLATION_MODEL,
) -> tuple[str | None, dict | None]:
    """Translate a receipt artifact image/PDF into English.

    The OpenAI provider reads the receipt image itself. If that fails, has no key, or returns
    nothing, the receipt is read with local OCR and translated with the local model instead
    (``fallback_from`` in the metadata names what failed), so only a disabled provider or a
    failure of the local path as well leaves the receipt untranslated.
    """
    normalized_provider = normalize_translation_provider(provider)
    if normalized_provider in _DISABLED_TRANSLATION_PROVIDERS:
        return None, None

    if normalized_provider == "openai":
        try:
            translated = _translate_artifact_with_openai(
                receipt_path,
                raw_text=raw_text,
                language=language,
                api_key=openai_api_key,
                model=model,
            )
        except Exception:  # noqa: BLE001
            translated = None
        if translated:
            return translated, {
                "provider": normalized_provider,
                "model": model,
                "source": "artifact",
            }
        local_text = _local_ocr_text(receipt_path, raw_text)
        if not local_text:
            return None, None
        translated, metadata = _translate_locally(
            local_text,
            language=language,
            failed_provider=normalized_provider,
            local_model=local_model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
        if metadata:
            metadata["source"] = "local-ocr"
        return translated, metadata

    if raw_text is None:
        from intake.ocr import extract_text

        raw_text = extract_text(receipt_path)
    return translate_receipt_text(
        raw_text,
        language=language,
        provider=normalized_provider,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        openai_api_key=openai_api_key,
        local_model=local_model,
    )


def _local_ocr_text(receipt_path: Path, raw_text: str | None) -> str | None:
    """The text to translate locally: what was already read, else a fresh local OCR pass."""
    if raw_text is not None and raw_text.strip():
        return raw_text
    try:
        from intake.ocr import extract_text

        text = extract_text(receipt_path)
    except Exception:  # noqa: BLE001
        return None
    return text if text and text.strip() else None


def _translation_prompt(raw_text: str, *, language: str | None) -> str:
    source_language = str(language or "unknown").strip().lower()
    return (
        "Translate the following receipt OCR text into concise, line-faithful English.\n"
        "Rules:\n"
        "- Return only the translated text.\n"
        "- Preserve line order and line breaks as closely as possible.\n"
        "- Keep all numbers, dates, times, currency amounts, and merchant names faithful.\n"
        "- Do not summarize, explain, add bullets, or invent missing words.\n"
        "- If a line is already English or unreadable OCR noise, keep it as-is.\n\n"
        f"Source language hint: {source_language}\n\n"
        f"{raw_text.strip()}"
    )


def _openai_artifact_translation_prompt(*, language: str | None) -> str:
    source_language = str(language or "unknown").strip().lower()
    return (
        "Translate this receipt fully into English.\n"
        "Rules:\n"
        "- Translate every non-English line into English.\n"
        "- Do not leave source-language text in the output.\n"
        "- Preserve line order and line breaks as closely as possible.\n"
        "- Keep merchant names, addresses, dates, item names, and currency amounts faithful.\n"
        "- If a merchant or place name is normally written in the source language, transliterate it into Latin characters and include the English meaning only when obvious.\n"
        "- Return only the translated text. Do not summarize or explain.\n\n"
        f"Source language hint: {source_language}"
    )


def _sanitize_translation_output(text: str) -> str | None:
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    cleaned = cleaned.removeprefix("```text").removeprefix("```").removesuffix("```").strip()
    cleaned = re.sub(
        r"^\s*(?:english translation|translation)\s*:\s*",
        "",
        cleaned,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned or None


def _load_crew_chief_client_class():
    try:
        from crew_chief import CrewChiefClient

        return CrewChiefClient
    except ModuleNotFoundError as exc:
        if exc.name != "crew_chief":
            raise

    sibling_src = Path(__file__).resolve().parents[2] / "crew-chief" / "src"
    if not sibling_src.exists():
        raise ModuleNotFoundError("crew_chief")
    if str(sibling_src) not in sys.path:
        sys.path.insert(0, str(sibling_src))
    from crew_chief import CrewChiefClient

    return CrewChiefClient


def _translate_text_with_openai(
    raw_text: str,
    *,
    language: str | None,
    api_key: str | None,
    model: str,
) -> str | None:
    if not api_key:
        return None

    import openai

    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": _translation_prompt(raw_text, language=language),
            }
        ],
        max_tokens=900,
    )
    return _sanitize_translation_output(response.choices[0].message.content or "")


def _translate_artifact_with_openai(
    receipt_path: Path,
    *,
    raw_text: str | None,
    language: str | None,
    api_key: str | None,
    model: str,
) -> str | None:
    if not api_key:
        return None

    import openai

    from intake.ocr import render_preview_image

    image = render_preview_image(receipt_path, resolution=150)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    image_b64 = b64encode(buf.getvalue()).decode()

    content: list[dict] = [
        {
            "type": "text",
            "text": _openai_artifact_translation_prompt(language=language),
        }
    ]
    if str(raw_text or "").strip():
        content.append(
            {
                "type": "text",
                "text": f"OCR text (may contain noise):\n{str(raw_text).strip()[:2000]}",
            }
        )
    content.append(
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{image_b64}",
                "detail": "high",
            },
        }
    )

    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=1200,
    )
    return _sanitize_translation_output(response.choices[0].message.content or "")
