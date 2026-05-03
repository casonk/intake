"""OpenAI vision fallback for payment field extraction."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from intake.parse_payment import Payment


def extract_with_openai_payment(
    payment_path: Path,
    raw_ocr: str,
    api_key: str,
    model: str = "gpt-4o-mini",
) -> dict:
    """Call the OpenAI vision API and return extracted payment fields."""
    import openai

    from intake.ai import _preview_image_b64

    image_b64 = _preview_image_b64(payment_path)

    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Extract the following fields from this payment document and return ONLY valid JSON "
                "with these keys (use null for any field you cannot determine):\n"
                '  "payment_date"        — date of payment in YYYY-MM-DD format (string)\n'
                '  "amount"              — payment amount as a number, no currency symbol (number)\n'
                '  "account_name"        — name of the account, lender, or card issuer (string)\n'
                '  "account_last4"       — last 4 digits of the account or card number (string)\n'
                '  "payment_type"        — one of: "credit-card", "loan", "mortgage", "auto", "utility", "other" (string)\n'
                '  "confirmation_number" — confirmation, reference, or authorization number (string)\n'
                '  "payer"               — name of the person or entity making the payment (string)\n'
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
        max_tokens=384,
    )
    raw = (response.choices[0].message.content or "{}").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def merge_ai_payment_result(payment: Payment, ai: dict) -> Payment:
    """Return a new Payment with AI-extracted fields overlaid where present."""
    from dataclasses import replace
    from datetime import date

    kwargs: dict = {}

    payment_date_str = ai.get("payment_date")
    if payment_date_str and isinstance(payment_date_str, str):
        with contextlib.suppress(ValueError):
            kwargs["payment_date"] = date.fromisoformat(payment_date_str)

    amount = ai.get("amount")
    if amount is not None:
        with contextlib.suppress(TypeError, ValueError):
            kwargs["amount"] = float(amount)

    account_name = ai.get("account_name")
    if account_name and isinstance(account_name, str) and account_name.strip():
        kwargs["account_name"] = account_name.strip()[:80]

    account_last4 = ai.get("account_last4")
    if account_last4 and isinstance(account_last4, str):
        digits = "".join(c for c in account_last4 if c.isdigit())[-4:]
        if digits:
            kwargs["account_last4"] = digits

    payment_type = ai.get("payment_type")
    if payment_type and isinstance(payment_type, str) and payment_type.strip():
        kwargs["payment_type"] = payment_type.strip().lower()[:20]

    confirmation_number = ai.get("confirmation_number")
    if confirmation_number and isinstance(confirmation_number, str) and confirmation_number.strip():
        kwargs["confirmation_number"] = confirmation_number.strip()[:60]

    payer = ai.get("payer")
    if payer and isinstance(payer, str) and payer.strip():
        kwargs["payer"] = payer.strip()[:80]

    return replace(payment, **kwargs) if kwargs else payment
