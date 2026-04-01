"""CLI entry point for intake: process, report, audit subcommands."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

_DEFAULT_DB = "data/intake.db"
_DEFAULT_REPORTS_DIR = "reports"
_SETTINGS_EXAMPLE = "config/settings.toml.example"
_SETTINGS_PATH = "config/settings.toml"


def _load_settings() -> dict:
    settings_path = Path(_SETTINGS_PATH)
    if not settings_path.exists():
        return {}
    import toml

    return toml.load(settings_path)


def _require_receipt_dir(settings: dict, args_value: str | None) -> Path:
    val = args_value or settings.get("receipt_dir")
    if not val:
        print(
            "ERROR: receipt_dir is not set.\n"
            f"  Copy {_SETTINGS_EXAMPLE} to {_SETTINGS_PATH} and set receipt_dir.",
            file=sys.stderr,
        )
        sys.exit(1)
    p = Path(val)
    if not p.exists():
        print(f"ERROR: receipt_dir does not exist: {p}", file=sys.stderr)
        sys.exit(1)
    return p


def _cmd_process(args: argparse.Namespace, settings: dict) -> None:
    from intake.classify import classify, load_rules
    from intake.db import init_db, upsert_receipt
    from intake.ocr import extract_text
    from intake.parse import parse_receipt

    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    db_path = Path(args.db or settings.get("db_path", _DEFAULT_DB))
    inbox = receipt_dir / "inbox"

    if not inbox.exists():
        print(f"ERROR: inbox directory not found: {inbox}", file=sys.stderr)
        sys.exit(1)

    pdfs = sorted(inbox.glob("*.pdf"))
    if not pdfs:
        print("No PDFs found in inbox.")
        return

    rules = load_rules()
    conn = init_db(db_path)

    for pdf in pdfs:
        print(f"  Processing: {pdf.name}")
        if args.dry_run:
            print("    [dry-run] skipping OCR and move")
            continue

        try:
            raw_text = extract_text(pdf)
            receipt = parse_receipt(pdf.name, raw_text)
            category = classify(receipt.merchant, rules, receipt.raw_text)
            dest_dir = receipt_dir / category
            dest_dir.mkdir(exist_ok=True)
            dest = dest_dir / pdf.name
            shutil.move(str(pdf), dest)

            upsert_receipt(
                conn,
                {
                    "filename": pdf.name,
                    "scan_date": receipt.scan_date.isoformat(),
                    "merchant": receipt.merchant,
                    "total": receipt.total,
                    "category": category,
                    "receipt_dir": str(dest.relative_to(receipt_dir)),
                    "raw_text": raw_text,
                },
            )
            print(
                f"    → {category}/ (merchant={receipt.merchant!r}, total={receipt.total})"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR processing {pdf.name}: {exc}", file=sys.stderr)

    conn.close()


def _cmd_report(args: argparse.Namespace, settings: dict) -> None:
    from intake.db import init_db
    from intake.report import generate_reports

    db_path = Path(args.db or settings.get("db_path", _DEFAULT_DB))
    output_dir = Path(
        args.output_dir or settings.get("reports_dir", _DEFAULT_REPORTS_DIR)
    )
    fmt = args.format or "both"

    conn = init_db(db_path)
    written = generate_reports(conn, output_dir, fmt=fmt)
    conn.close()

    for p in written:
        print(f"  Report written: {p}")


def _cmd_audit(args: argparse.Namespace, settings: dict) -> None:
    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    inbox = receipt_dir / "inbox"

    if not inbox.exists():
        print(f"ERROR: inbox directory not found: {inbox}", file=sys.stderr)
        sys.exit(1)

    pdfs = sorted(inbox.glob("*.pdf"))
    if not pdfs:
        print("Inbox is empty.")
        return

    print(f"Found {len(pdfs)} PDF(s) in {inbox}:")
    for pdf in pdfs:
        print(f"  {pdf.name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="intake",
        description="Process, sort, and report on receipt PDF scans.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # process
    p_proc = sub.add_parser(
        "process", help="Run the full OCR → classify → move → DB pipeline"
    )
    p_proc.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_proc.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_proc.add_argument(
        "--dry-run", action="store_true", help="Print actions without moving files"
    )

    # report
    p_rep = sub.add_parser(
        "report", help="Generate Markdown and/or HTML summary reports"
    )
    p_rep.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_rep.add_argument(
        "--output-dir", metavar="PATH", help="Directory to write reports into"
    )
    p_rep.add_argument(
        "--format", choices=["md", "html", "both"], default="both", help="Report format"
    )

    # audit
    p_aud = sub.add_parser("audit", help="List inbox PDFs without processing them")
    p_aud.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")

    args = parser.parse_args()
    settings = _load_settings()

    if args.command == "process":
        _cmd_process(args, settings)
    elif args.command == "report":
        _cmd_report(args, settings)
    elif args.command == "audit":
        _cmd_audit(args, settings)


if __name__ == "__main__":
    main()
