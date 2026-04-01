"""OpenAI vision API fallback for receipt field extraction.

Invoked when Tesseract OCR quality is too poor to reliably extract
the merchant name, total, or receipt date.
"""

from __future__ import annotations

import base64
import io
import json
from datetime import date, time
from pathlib import Path

from intake.parse import Receipt


def extract_with_openai(
    pdf_path: Path,
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

    image_b64 = _first_page_b64(pdf_path)

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
        max_tokens=256,
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
        kwargs["merchant"] = merchant.strip()[:60]

    total = ai.get("total")
    if total is not None:
        try:
            kwargs["total"] = float(total)
        except (TypeError, ValueError):
            pass

    receipt_date_str = ai.get("receipt_date")
    if receipt_date_str and isinstance(receipt_date_str, str):
        try:
            kwargs["receipt_date"] = date.fromisoformat(receipt_date_str)
        except ValueError:
            pass

    receipt_time_str = ai.get("receipt_time")
    if receipt_time_str and isinstance(receipt_time_str, str):
        try:
            h, m = receipt_time_str.split(":")[:2]
            kwargs["receipt_time"] = time(int(h), int(m))
        except (ValueError, TypeError):
            pass

    return replace(receipt, **kwargs) if kwargs else receipt


# ── helpers ───────────────────────────────────────────────────────────────────


def _first_page_b64(pdf_path: Path) -> str:
    """Render the first page of a PDF to a base64-encoded PNG string."""
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            raise ValueError(f"No pages in {pdf_path}")
        image = pdf.pages[0].to_image(resolution=150).original

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()
