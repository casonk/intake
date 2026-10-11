"""Receipt text extraction for PDFs, images, and videos."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from intake.media import is_image_receipt, is_pdf_receipt, is_video_receipt

# Some PDF generators synthesize bold text by rendering each glyph twice at
# an near-identical position (e.g. "Invoice" -> "IInnvvooiiccee"). pdfplumber's
# native text extraction then reports every character in that run doubled,
# which corrupts merchant/total parsing on affected receipts (labels like
# "Total" or "Amount due" no longer match). Require a minimum length so
# short, legitimately-doubled tokens (e.g. "AA", "##") are left alone.
_MIN_DOUBLED_TOKEN_LEN = 6
_WHITESPACE_RUN_RE = re.compile(r"(\s+)")


def _undouble_token(token: str) -> str:
    if (
        len(token) >= _MIN_DOUBLED_TOKEN_LEN
        and len(token) % 2 == 0
        and any(ch.isalpha() for ch in token)
        and all(token[i] == token[i + 1] for i in range(0, len(token), 2))
    ):
        return token[::2]
    return token


def _undouble_glyph_runs(text: str) -> str:
    """Collapse doubled-glyph runs (see module docstring above) line by line."""
    return "\n".join(
        "".join(
            token if token.isspace() else _undouble_token(token)
            for token in _WHITESPACE_RUN_RE.split(line)
        )
        for line in text.splitlines()
    )


def extract_text(receipt_path: Path) -> str:
    """Extract text from a supported receipt file."""
    if is_pdf_receipt(receipt_path):
        return _extract_pdf_text(receipt_path)
    if is_image_receipt(receipt_path):
        return _ocr_image(_load_image(receipt_path))
    if is_video_receipt(receipt_path):
        return _ocr_image(_video_thumbnail_image(receipt_path))
    raise ValueError(f"unsupported receipt media type: {receipt_path}")


def render_preview_image(receipt_path: Path, *, resolution: int = 150):
    """Return a PIL image preview for a supported receipt file."""
    if is_pdf_receipt(receipt_path):
        return _render_pdf_page(receipt_path, resolution=resolution)
    if is_image_receipt(receipt_path):
        return _load_image(receipt_path)
    if is_video_receipt(receipt_path):
        return _video_thumbnail_image(receipt_path)
    raise ValueError(f"unsupported receipt media type: {receipt_path}")


def _extract_pdf_text(pdf_path: Path) -> str:
    """Extract text from a PDF via native text, falling back to OCR per page."""
    import pdfplumber

    pages: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if len(text.strip()) >= 20:
                pages.append(_undouble_glyph_runs(text))
            else:
                pages.append(_ocr_page(page))

    return "\n".join(pages)


def _render_pdf_page(pdf_path: Path, *, resolution: int):
    """Render the first page of *pdf_path* to a PIL image."""
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            raise ValueError(f"No pages in {pdf_path}")
        return pdf.pages[0].to_image(resolution=resolution).original


def _load_image(image_path: Path):
    """Load *image_path* as a normalized PIL image."""
    from PIL import Image, ImageOps

    with Image.open(image_path) as image:
        normalized = ImageOps.exif_transpose(image)
        return normalized.convert("RGB")


def _video_thumbnail_image(video_path: Path):
    """Render a representative video frame to a normalized PIL image."""
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-vf",
                "thumbnail",
                "-frames:v",
                "1",
                tmp.name,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return _load_image(Path(tmp.name))


def _ocr_page(page) -> str:
    """Render a pdfplumber page to an image and run pytesseract on it."""
    image = page.to_image(resolution=300).original
    return _ocr_image(image)


def _ocr_image(image) -> str:
    """Run pytesseract on a PIL image."""
    import pytesseract

    return pytesseract.image_to_string(image)
