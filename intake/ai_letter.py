"""OpenAI vision fallback for letter field extraction."""

from __future__ import annotations

import contextlib
import json
from datetime import date
from pathlib import Path

from intake.parse_letter import Letter


def extract_with_openai_letter(
    letter_path: Path,
    raw_ocr: str,
    api_key: str,
    model: str = "gpt-4o-mini",
) -> dict:
    """Call the OpenAI vision API and return extracted letter fields."""
    import openai

    from intake.ai import _preview_image_b64

    image_b64 = _preview_image_b64(letter_path)

    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Extract the following fields from this letter and return ONLY valid JSON "
                "with these keys (use null for any field you cannot determine):\n"
                '  "sender"       — name of the letter writer (string)\n'
                '  "recipient"    — name of the intended recipient (string)\n'
                '  "letter_date"  — date on the letter in YYYY-MM-DD format (string)\n'
                '  "subject"      — Re: or Subject: line if present (string)\n'
                '  "salutation"   — opening greeting e.g. "Dear Jane," (string)\n'
                '  "body_excerpt" — first 400 characters of the letter body (string)\n'
                "Output only the JSON object, no markdown, no explanation."
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{image_b64}", "detail": "high"},
        },
    ]
    if raw_ocr.strip():
        content.insert(
            1, {"type": "text", "text": f"OCR text (may contain noise):\n{raw_ocr[:2000]}"}
        )

    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=512,
    )
    raw = (response.choices[0].message.content or "{}").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def merge_ai_letter_result(letter: Letter, ai: dict) -> Letter:
    """Return a new Letter with AI-extracted fields overlaid where present."""
    from dataclasses import replace

    kwargs: dict = {}

    sender = ai.get("sender")
    if sender and isinstance(sender, str) and sender.strip():
        kwargs["sender"] = sender.strip()[:80]

    recipient = ai.get("recipient")
    if recipient and isinstance(recipient, str) and recipient.strip():
        kwargs["recipient"] = recipient.strip()[:80]

    letter_date_str = ai.get("letter_date")
    if letter_date_str and isinstance(letter_date_str, str):
        with contextlib.suppress(ValueError):
            kwargs["letter_date"] = date.fromisoformat(letter_date_str)

    subject = ai.get("subject")
    if subject and isinstance(subject, str) and subject.strip():
        kwargs["subject"] = subject.strip()[:120]

    salutation = ai.get("salutation")
    if salutation and isinstance(salutation, str) and salutation.strip():
        kwargs["salutation"] = salutation.strip()[:80]

    body_excerpt = ai.get("body_excerpt")
    if body_excerpt and isinstance(body_excerpt, str) and body_excerpt.strip():
        kwargs["body_excerpt"] = body_excerpt.strip()[:500]

    return replace(letter, **kwargs) if kwargs else letter
