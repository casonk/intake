"""PDF text extraction: pdfplumber first, pytesseract fallback for image pages."""

from __future__ import annotations

from pathlib import Path


def extract_text(pdf_path: Path) -> str:
    """Extract text from a PDF.

    Try pdfplumber first; fall back to pytesseract on image-only pages
    (pages that yield fewer than 20 characters of native text).
    """
    import pdfplumber

    pages: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if len(text.strip()) >= 20:
                pages.append(text)
            else:
                pages.append(_ocr_page(page))

    return "\n".join(pages)


def _ocr_page(page) -> str:
    """Render a pdfplumber page to an image and run pytesseract on it."""
    import pytesseract

    image = page.to_image(resolution=300).original
    return pytesseract.image_to_string(image)
