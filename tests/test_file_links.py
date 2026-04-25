"""Unit tests for intake.file_links."""

from __future__ import annotations

from intake.file_links import (
    build_receipt_filebrowser_link,
    configured_filebrowser_base_url,
    configured_filebrowser_receipt_root,
)


def test_configured_filebrowser_defaults():
    assert configured_filebrowser_base_url({}) == "https://files.snowbridge.internal"
    assert configured_filebrowser_receipt_root({}) == "receipt"


def test_build_receipt_filebrowser_link_resolves_tracked_file(tmp_path):
    receipt_root = tmp_path / "receipts"
    receipt_file = receipt_root / "general" / "20260419_2018.jpg"
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_bytes(b"jpg")

    settings = {"receipt_dir": str(receipt_root)}
    row = {
        "filename": "20260419_2018.jpg",
        "receipt_dir": "general/20260419_2018.jpg",
        "category": "general",
    }

    assert (
        build_receipt_filebrowser_link(settings, row)
        == "https://files.snowbridge.internal/files/receipt/general/20260419_2018.jpg"
    )


def test_build_receipt_filebrowser_link_honors_custom_report_links(tmp_path):
    receipt_root = tmp_path / "receipts"
    receipt_file = receipt_root / "general" / "20260419_2018.jpg"
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_bytes(b"jpg")

    settings = {
        "receipt_dir": str(receipt_root),
        "report_links": {
            "filebrowser_base_url": "https://files.example.internal/browser",
            "filebrowser_receipt_root": "alt-root",
        },
    }
    row = {
        "filename": "20260419_2018.jpg",
        "receipt_dir": "general/20260419_2018.jpg",
        "category": "general",
    }

    assert (
        build_receipt_filebrowser_link(settings, row)
        == "https://files.example.internal/browser/files/alt-root/general/20260419_2018.jpg"
    )
