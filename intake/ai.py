"""OpenAI vision API fallback for receipt field extraction.

Invoked when Tesseract OCR quality is too poor to reliably extract
the merchant name, total, or receipt date.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import re
from datetime import date, time
from pathlib import Path

from intake.certification import merge_certifications, receipt_certifications_from_dicts
from intake.parse import Receipt, receipt_items_from_dicts
from intake.purchaser import is_blacklisted_purchaser, is_probable_staff_name, normalize_purchaser
from intake.staff import receipt_staff_from_dicts


def extract_with_openai(
    receipt_path: Path,
    raw_ocr: str,
    api_key: str,
    model: str = "gpt-4o-mini",
) -> dict:
    """Call the OpenAI vision API and return extracted receipt fields.

    Returns a dict with any subset of:
        merchant, total, receipt_date (YYYY-MM-DD str), receipt_time (HH:MM str)
    Absent or unrecognised fields are omitted.
    """
    import openai

    image_b64 = _preview_image_b64(receipt_path)

    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Extract the following fields from this receipt and return ONLY valid JSON "
                "with these keys (use null for any field you cannot determine with confidence):\n"
                '  "merchant"      — store or business name (string)\n'
                '  "total"         — final amount charged, as a number (float)\n'
                '  "receipt_date"  — date printed on the receipt in YYYY-MM-DD format (string)\n'
                '  "receipt_time"  — time printed on the receipt in HH:MM 24-hr format (string)\n'
                '  "purchaser"     — cardholder or customer name if visible (string)\n'
                '  "card_last4"    — last 4 digits of the payment card if visible (string)\n'
                '  "currency"       — ISO-4217 currency code (e.g. "USD", "CAD", "EUR", "JPY"); default "USD" if not shown\n'
                '  "language"      — dominant receipt language code ("en", "ja", or "und")\n'
                '  "translated_text" — concise English translation of the non-English receipt text; use null when the receipt is already primarily English or unreadable\n'
                '  "items"         — array of itemized line objects with keys '
                '"description", "amount", "quantity", "unit_price" (null when unknown)\n'
                '  "certifications" — array of certification objects with keys '
                '"issuer", "label", "source_text" for merchant or shop certifications (use [] when none visible)\n'
                '  "staff"         — array of staff/service objects with keys '
                '"role", "name", "source_text" for visible server, waitress, waiter, cashier, or clerk fields (use [] when none visible)\n'
                "Output only the JSON object, no markdown, no explanation."
            ),
        },
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{image_b64}",
                "detail": "high",
            },
        },
    ]

    if raw_ocr.strip():
        content.insert(
            1,
            {
                "type": "text",
                "text": f"OCR text (may contain noise):\n{raw_ocr[:2000]}",
            },
        )

    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=768,
    )

    raw = response.choices[0].message.content or "{}"
    # Strip accidental markdown fences
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def merge_ai_result(receipt: Receipt, ai: dict) -> Receipt:
    """Return a new Receipt with AI-extracted fields overlaid where present.

    Only non-null AI values replace the parsed values, so a partial AI
    response still improves on whatever Tesseract managed.
    """
    from dataclasses import replace

    kwargs: dict = {}

    merchant = ai.get("merchant")
    if merchant and isinstance(merchant, str) and merchant.strip():
        kwargs["merchant"] = merchant.strip()[:60].lower()

    receipt_date_str = ai.get("receipt_date")
    if receipt_date_str and isinstance(receipt_date_str, str):
        with contextlib.suppress(ValueError):
            kwargs["receipt_date"] = date.fromisoformat(receipt_date_str)

    receipt_time_str = ai.get("receipt_time")
    if receipt_time_str and isinstance(receipt_time_str, str):
        try:
            h, m = receipt_time_str.split(":")[:2]
            kwargs["receipt_time"] = time(int(h), int(m))
        except (ValueError, TypeError):
            pass

    purchaser = ai.get("purchaser")
    if purchaser and isinstance(purchaser, str) and purchaser.strip():
        normalized = normalize_purchaser(purchaser)
        if normalized is not None and not is_probable_staff_name(normalized, receipt.raw_text):
            kwargs["purchaser"] = normalized

    card_last4 = ai.get("card_last4")
    if card_last4 and isinstance(card_last4, str):
        digits = re.sub(r"\D", "", card_last4)
        if len(digits) == 4:
            kwargs["card_last4"] = digits

    items = receipt_items_from_dicts(ai.get("items"))
    if items:
        kwargs["items"] = items

    certifications = receipt_certifications_from_dicts(ai.get("certifications"))
    if certifications:
        kwargs["certifications"] = merge_certifications(receipt.certifications, certifications)

    staff = receipt_staff_from_dicts(ai.get("staff"))
    if staff:
        kwargs["staff"] = staff

    currency = ai.get("currency")
    if currency and isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency.upper()):
        kwargs["currency"] = currency.upper()

    language = ai.get("language")
    if isinstance(language, str):
        normalized_language = language.strip().lower()
        if normalized_language in {"en", "ja", "und"}:
            kwargs["language"] = normalized_language
        elif normalized_language in {"english", "eng"}:
            kwargs["language"] = "en"
        elif normalized_language in {"japanese", "jpn"}:
            kwargs["language"] = "ja"

    translated_text = ai.get("translated_text")
    if translated_text and isinstance(translated_text, str):
        normalized_translation = translated_text.strip()
        if normalized_translation and normalized_translation != receipt.raw_text.strip():
            kwargs["translated_text"] = normalized_translation

    total = ai.get("total")
    if total is not None:
        from intake.parse import normalize_transaction_total  # noqa: PLC0415

        try:
            val = float(total)
            if val > 0:
                kwargs["total"] = normalize_transaction_total(receipt.raw_text, val)
            elif receipt.total is not None:
                normalized = normalize_transaction_total(receipt.raw_text, val)
                if normalized is None:
                    kwargs["total"] = None
        except (TypeError, ValueError):
            pass

    return replace(receipt, **kwargs) if kwargs else receipt


def purchaser_was_blacklisted(ai_dict: dict) -> bool:
    """Return True if the AI returned a purchaser value that was rejected by the blacklist.

    Used by extract_receipt to decide whether to upgrade to the mid-fidelity model.
    """
    raw = ai_dict.get("purchaser")
    if not raw or not isinstance(raw, str) or not raw.strip():
        return False
    return normalize_purchaser(raw) is None and is_blacklisted_purchaser(raw)


# ── helpers ───────────────────────────────────────────────────────────────────


def _preview_image_b64(receipt_path: Path) -> str:
    """Render a supported receipt file to a base64-encoded PNG string."""
    from intake.ocr import render_preview_image

    image = render_preview_image(receipt_path, resolution=150)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()
