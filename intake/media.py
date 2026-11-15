"""Shared helpers for receipt media types handled by intake."""

from __future__ import annotations

from pathlib import Path

PDF_RECEIPT_SUFFIXES = frozenset({".pdf"})
IMAGE_RECEIPT_SUFFIXES = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".tif",
        ".tiff",
        ".bmp",
        ".webp",
        ".heic",
        ".heif",
    }
)
HEIC_RECEIPT_SUFFIXES = frozenset({".heic", ".heif"})
VIDEO_RECEIPT_SUFFIXES = frozenset({".mov", ".mp4", ".m4v"})
SUPPORTED_RECEIPT_SUFFIXES = PDF_RECEIPT_SUFFIXES | IMAGE_RECEIPT_SUFFIXES | VIDEO_RECEIPT_SUFFIXES


def receipt_suffix(path: str | Path) -> str:
    """Return the normalized lowercase suffix for *path*."""
    return Path(path).suffix.lower()


def is_pdf_receipt(path: str | Path) -> bool:
    """Return True when *path* looks like a supported PDF receipt artifact."""
    return receipt_suffix(path) in PDF_RECEIPT_SUFFIXES


def is_image_receipt(path: str | Path) -> bool:
    """Return True when *path* looks like a supported image receipt artifact."""
    return receipt_suffix(path) in IMAGE_RECEIPT_SUFFIXES


def is_video_receipt(path: str | Path) -> bool:
    """Return True when *path* looks like a supported video receipt artifact."""
    return receipt_suffix(path) in VIDEO_RECEIPT_SUFFIXES


def heic_decodable() -> bool:
    """Whether HEIC/HEIF photos can be read: through the sandbox, or locally with ``pillow-heif`` installed.

    A HEIC file on a machine that can read neither is *unsupported*, not corrupt: it stays where it is and
    nobody is told it is a bad upload.
    """
    from intake import sandbox

    if sandbox.enabled():
        return True  # the sandbox image carries the decoder; this process never decodes it
    import importlib.util

    return importlib.util.find_spec("pillow_heif") is not None


def is_supported_receipt(path: str | Path) -> bool:
    """Return True when *path* is a receipt artifact intake knows how to process."""
    suffix = receipt_suffix(path)
    if suffix not in SUPPORTED_RECEIPT_SUFFIXES:
        return False
    if suffix in HEIC_RECEIPT_SUFFIXES:
        return heic_decodable()
    return True


def iter_supported_receipt_files(folder: Path) -> list[Path]:
    """Return sorted direct child receipt files under *folder*."""
    if not folder.exists():
        return []
    return sorted(
        path for path in folder.iterdir() if path.is_file() and is_supported_receipt(path)
    )


def rglob_supported_receipt_files(folder: Path) -> list[Path]:
    """Return sorted receipt files found recursively under *folder*."""
    if not folder.exists():
        return []
    return sorted(
        path for path in folder.rglob("*") if path.is_file() and is_supported_receipt(path)
    )
