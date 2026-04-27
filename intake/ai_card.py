"""OpenAI vision fallback for greeting card field extraction."""

from __future__ import annotations

import contextlib
import json
from datetime import date
from pathlib import Path

from intake.parse_card import Card


def extract_with_openai_card(
    card_path: Path,
    raw_ocr: str,
    api_key: str,
    model: str = "gpt-4o-mini",
) -> dict:
    """Call the OpenAI vision API and return extracted card fields."""
    import openai

    from intake.ai import _preview_image_b64

    image_b64 = _preview_image_b64(card_path)

    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Extract the following fields from this greeting card and return ONLY valid JSON "
                "with these keys (use null for any field you cannot determine):\n"
                '  "occasion"        — type of occasion: "birthday", "holiday", "anniversary", '
                '"congratulations", "thank-you", "sympathy", "get-well", "farewell", or "general" (string)\n'
                '  "sender"          — who signed or sent the card (string)\n'
                '  "recipient"       — who the card is addressed to (string)\n'
                '  "card_date"       — any date on the card in YYYY-MM-DD format (string)\n'
                '  "message_excerpt" — first 300 characters of the card message (string)\n'
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
            1, {"type": "text", "text": f"OCR text (may contain noise):\n{raw_ocr[:1000]}"}
        )

    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=384,
    )
    raw = (response.choices[0].message.content or "{}").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def merge_ai_card_result(card: Card, ai: dict) -> Card:
    """Return a new Card with AI-extracted fields overlaid where present."""
    from dataclasses import replace

    kwargs: dict = {}

    occasion = ai.get("occasion")
    if occasion and isinstance(occasion, str) and occasion.strip():
        kwargs["occasion"] = occasion.strip().lower()[:40]

    sender = ai.get("sender")
    if sender and isinstance(sender, str) and sender.strip():
        kwargs["sender"] = sender.strip()[:60]

    recipient = ai.get("recipient")
    if recipient and isinstance(recipient, str) and recipient.strip():
        kwargs["recipient"] = recipient.strip()[:60]

    card_date_str = ai.get("card_date")
    if card_date_str and isinstance(card_date_str, str):
        with contextlib.suppress(ValueError):
            kwargs["card_date"] = date.fromisoformat(card_date_str)

    message_excerpt = ai.get("message_excerpt")
    if message_excerpt and isinstance(message_excerpt, str) and message_excerpt.strip():
        kwargs["message_excerpt"] = message_excerpt.strip()[:300]

    return replace(card, **kwargs) if kwargs else card
