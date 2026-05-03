"""Unit tests for intake.file_links."""

from __future__ import annotations

from intake.file_links import (
    build_receipt_companion_filebrowser_links,
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


def test_build_receipt_filebrowser_link_resolves_email_text_artifact(tmp_path):
    receipt_root = tmp_path / "receipts"
    receipt_file = receipt_root / "email" / "general" / "email_30999_INBOX.txt"
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_text("Receipt body", encoding="utf-8")

    settings = {"receipt_dir": str(receipt_root)}
    row = {
        "filename": "email_30999_INBOX.txt",
        "receipt_dir": "email/general",
        "category": "general",
    }

    assert (
        build_receipt_filebrowser_link(settings, row)
        == "https://files.snowbridge.internal/files/receipt/email/general/email_30999_INBOX.txt"
    )


def test_build_receipt_companion_filebrowser_links_resolves_existing_artifacts(tmp_path):
    receipt_root = tmp_path / "receipts"
    receipt_file = receipt_root / "general" / "20260419_2018.jpg"
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_bytes(b"jpg")
    receipt_file.with_suffix(".ocr.txt").write_text("ocr", encoding="utf-8")
    receipt_file.with_suffix(".intake.json").write_text("{}", encoding="utf-8")
    receipt_file.with_suffix(".ai.json").write_text("{}", encoding="utf-8")

    settings = {"receipt_dir": str(receipt_root)}
    row = {
        "filename": "20260419_2018.jpg",
        "receipt_dir": "general/20260419_2018.jpg",
        "category": "general",
    }

    assert build_receipt_companion_filebrowser_links(settings, row) == {
        "ocr": "https://files.snowbridge.internal/files/receipt/general/20260419_2018.ocr.txt",
        "sidecar_json": "https://files.snowbridge.internal/files/receipt/general/20260419_2018.intake.json",
        "ai_json": "https://files.snowbridge.internal/files/receipt/general/20260419_2018.ai.json",
    }


def test_build_receipt_companion_filebrowser_links_omits_missing_artifacts(tmp_path):
    receipt_root = tmp_path / "receipts"
    receipt_file = receipt_root / "general" / "20260419_2018.jpg"
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_bytes(b"jpg")
    receipt_file.with_suffix(".ocr.txt").write_text("ocr", encoding="utf-8")

    settings = {"receipt_dir": str(receipt_root)}
    row = {
        "filename": "20260419_2018.jpg",
        "receipt_dir": "general/20260419_2018.jpg",
        "category": "general",
    }

    assert build_receipt_companion_filebrowser_links(settings, row) == {
        "ocr": "https://files.snowbridge.internal/files/receipt/general/20260419_2018.ocr.txt",
    }
