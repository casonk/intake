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


def _get_extract_kwargs(settings: dict) -> dict:
    """Build keyword arguments for extract_receipt() from settings."""
    return {
        "openai_api_key": settings.get("openai_api_key") or None,
        "openai_model_low": (
            settings.get("openai_model_low")
            or settings.get("openai_model")
            or "gpt-4o-mini"
        ),
        "openai_model_high": settings.get("openai_model_high") or "gpt-4o",
        "escalation_patterns": settings.get("escalation_patterns") or None,
        "fuzzy_threshold": float(settings.get("fuzzy_threshold", 0.82)),
    }


def _low_model(settings: dict) -> str:
    if not settings.get("openai_api_key"):
        from intake.extract import OCR_MODEL

        return OCR_MODEL
    return (
        settings.get("openai_model_low")
        or settings.get("openai_model")
        or "gpt-4o-mini"
    )


def _high_model(settings: dict) -> str:
    if not settings.get("openai_api_key"):
        from intake.extract import OCR_MODEL

        return OCR_MODEL
    return settings.get("openai_model_high") or "gpt-4o"


def _skip_sidecar(sc: dict, settings: dict) -> bool:
    """Return True if a sidecar indicates the receipt is already up-to-date.

    Uses tier-aware comparison so a receipt processed at the low-fidelity
    model level is only re-queued when a better *low* model is configured,
    and similarly for high-fidelity.  This prevents low-tier receipts from
    being endlessly re-queued just because a high model is also configured.
    """
    from intake.extract import OCR_MODEL, model_rank

    tier = sc.get("tier", "ok")
    sidecar_model = sc.get("processed_by", OCR_MODEL)
    if tier == "ok":
        return True
    if tier == "low":
        return model_rank(sidecar_model) >= model_rank(_low_model(settings))
    if tier == "high":
        return model_rank(sidecar_model) >= model_rank(_high_model(settings))
    return False


def _cmd_process(args: argparse.Namespace, settings: dict) -> None:
    from intake.classify import classify, load_rules
    from intake.db import init_db, upsert_receipt
    from intake.extract import extract_receipt
    from intake.sidecar import write_sidecar

    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    db_path = Path(args.db or settings.get("db_path", _DEFAULT_DB))
    intake_dir = receipt_dir / "intake"

    if not intake_dir.exists():
        print(f"ERROR: intake directory not found: {intake_dir}", file=sys.stderr)
        sys.exit(1)

    pdfs = sorted(intake_dir.glob("*.pdf"))
    if not pdfs:
        print("No PDFs found in intake.")
        return

    rules = load_rules()
    conn = init_db(db_path)

    for pdf in pdfs:
        print(f"  Processing: {pdf.name}")
        if args.dry_run:
            print("    [dry-run] skipping OCR and move")
            continue

        try:
            receipt, model, tier = extract_receipt(
                pdf,
                **_get_extract_kwargs(settings),
                fuzzy_rules=rules,
            )
            category = classify(receipt.merchant, rules, receipt.raw_text)
            dest_dir = receipt_dir / category
            dest_dir.mkdir(parents=True, exist_ok=True)

            # Rename based on the date/time printed on the receipt
            new_name = receipt.suggested_filename()
            dest = dest_dir / new_name
            # Avoid overwriting an existing file by appending a counter suffix
            if dest.exists() and dest != pdf:
                stem = dest.stem
                counter = 1
                while dest.exists():
                    dest = dest_dir / f"{stem}-{counter}.pdf"
                    counter += 1

            shutil.move(str(pdf), dest)
            renamed = (
                f" (renamed {pdf.name} -> {dest.name})" if dest.name != pdf.name else ""
            )

            row = {
                "filename": dest.name,
                "scan_date": receipt.scan_date.isoformat(),
                "merchant": receipt.merchant,
                "total": receipt.total,
                "category": category,
                "receipt_dir": str(dest.relative_to(receipt_dir)),
                "raw_text": receipt.raw_text,
                "processed_by": model,
                "purchaser": receipt.purchaser,
                "card_last4": receipt.card_last4,
                "tier": tier,
            }
            upsert_receipt(conn, row)
            write_sidecar(dest, {k: v for k, v in row.items() if k != "raw_text"})
            print(
                f"    -> {category}/{dest.name} (merchant={receipt.merchant!r}, total={receipt.total}, model={model}){renamed}"
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


def _cmd_backfill(args: argparse.Namespace, settings: dict) -> None:
    from intake.classify import load_rules
    from intake.db import init_db, upsert_receipt
    from intake.extract import extract_receipt
    from intake.sidecar import read_sidecar, write_sidecar

    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    db_path = Path(args.db or settings.get("db_path", _DEFAULT_DB))
    rules = load_rules()
    extract_kwargs = _get_extract_kwargs(settings)

    all_pdfs = [
        pdf
        for pdf in sorted(receipt_dir.rglob("*.pdf"))
        if pdf.relative_to(receipt_dir).parts[0] != "intake"
        and len(pdf.relative_to(receipt_dir).parts) >= 2
    ]

    if not all_pdfs:
        print("No receipts found in category folders.")
        return

    pending: list[Path] = []
    skipped = 0
    for pdf in all_pdfs:
        sc = read_sidecar(pdf)
        if sc is not None and _skip_sidecar(sc, settings):
            skipped += 1
            continue
        pending.append(pdf)

    print(
        f"Found {len(all_pdfs)} receipt(s); {skipped} up-to-date, {len(pending)} pending."
    )

    if not pending:
        print("Nothing to backfill.")
        return

    if args.dry_run:
        for pdf in pending:
            rel = pdf.relative_to(receipt_dir)
            sc = read_sidecar(pdf)
            prior_model = sc.get("processed_by", "none") if sc else "none"
            prior_tier = sc.get("tier", "—") if sc else "—"
            print(f"  [dry-run] {rel}  (prior: {prior_model}/{prior_tier})")
        return

    conn = init_db(db_path)
    ok = failed = 0

    for pdf in pending:
        rel = pdf.relative_to(receipt_dir)
        category = "/".join(rel.parts[:-1])
        print(f"  Backfilling: {rel}")
        try:
            receipt, model, tier = extract_receipt(
                pdf, **extract_kwargs, fuzzy_rules=rules
            )
            row = {
                "filename": pdf.name,
                "scan_date": receipt.scan_date.isoformat(),
                "merchant": receipt.merchant,
                "total": receipt.total,
                "category": category,
                "receipt_dir": str(rel),
                "raw_text": receipt.raw_text,
                "processed_by": model,
                "purchaser": receipt.purchaser,
                "card_last4": receipt.card_last4,
                "tier": tier,
            }
            upsert_receipt(conn, row)
            write_sidecar(pdf, {k: v for k, v in row.items() if k != "raw_text"})
            print(
                f"    -> [{tier}] category={category!r}, merchant={receipt.merchant!r}, "
                f"total={receipt.total}, model={model}"
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR: {exc}", file=sys.stderr)
            failed += 1

    conn.close()
    print(f"\nBackfill complete: {ok} ingested, {failed} error(s).")


def _cmd_audit(args: argparse.Namespace, settings: dict) -> None:
    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    intake_dir = receipt_dir / "intake"

    if not intake_dir.exists():
        print(f"ERROR: intake directory not found: {intake_dir}", file=sys.stderr)
        sys.exit(1)

    pdfs = sorted(intake_dir.glob("*.pdf"))
    if not pdfs:
        print("Intake folder is empty.")
        return

    print(f"Found {len(pdfs)} PDF(s) in {intake_dir}:")
    for pdf in pdfs:
        print(f"  {pdf.name}")


def _parse_scan_datetime(pdf: Path):
    """Return a datetime from a YYYYMMDD_HHMM filename, or None."""
    import re
    from datetime import datetime

    m = re.match(r"(\d{8})_?(\d{4})?", pdf.stem)
    if m:
        try:
            return datetime.strptime(m.group(1) + (m.group(2) or "0000"), "%Y%m%d%H%M")
        except ValueError:
            pass
    return None


def _detect_merge_groups(receipt_dir: Path, window_minutes: int) -> list[list[Path]]:
    """Return groups of PDFs in the same folder whose scan times are within *window_minutes*."""
    from datetime import timedelta

    groups: list[list[Path]] = []

    for folder in sorted(p for p in receipt_dir.rglob("*") if p.is_dir()):
        if folder.relative_to(receipt_dir).parts[0] == "intake":
            continue
        pdfs = sorted(folder.glob("*.pdf"))
        if len(pdfs) < 2:
            continue

        timed = [(p, _parse_scan_datetime(p)) for p in pdfs]
        timed = [(p, t) for p, t in timed if t is not None]
        if len(timed) < 2:
            continue
        timed.sort(key=lambda x: x[1])

        current: list[Path] = [timed[0][0]]
        for pdf, dt in timed[1:]:
            prev_dt = _parse_scan_datetime(current[-1])
            if prev_dt and (dt - prev_dt) <= timedelta(minutes=window_minutes):
                current.append(pdf)
            else:
                if len(current) >= 2:
                    groups.append(current)
                current = [pdf]
        if len(current) >= 2:
            groups.append(current)

    return groups


def _cmd_merge(args: argparse.Namespace, settings: dict) -> None:
    from pypdf import PdfReader, PdfWriter
    from intake.classify import load_rules
    from intake.db import init_db, upsert_receipt
    from intake.extract import extract_receipt
    from intake.sidecar import write_sidecar

    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    db_path = Path(args.db or settings.get("db_path", _DEFAULT_DB))
    window = int(args.window)

    groups = _detect_merge_groups(receipt_dir, window)
    if not groups:
        print("No multi-page groups detected.")
        return

    print(f"Found {len(groups)} merge group(s) (scan window: {window} min):")
    for i, group in enumerate(groups, 1):
        print(f"  Group {i}: {len(group)} page(s)")
        for pdf in group:
            print(f"    {pdf.relative_to(receipt_dir)}")

    if args.dry_run:
        return

    rules = load_rules()
    extract_kwargs = _get_extract_kwargs(settings)
    conn = init_db(db_path)
    ok = failed = 0

    for group in groups:
        primary = group[0]
        secondaries = group[1:]
        rel = primary.relative_to(receipt_dir)
        category = "/".join(rel.parts[:-1])
        print(f"\n  Merging {len(group)} pages → {rel}")
        try:
            # Merge all PDF pages into primary
            writer = PdfWriter()
            for pdf in group:
                for page in PdfReader(str(pdf)).pages:
                    writer.add_page(page)
            with open(primary, "wb") as f:
                writer.write(f)

            # Remove secondary files and their sidecars
            for sec in secondaries:
                sc_path = sec.with_suffix(".intake.json")
                if sc_path.exists():
                    sc_path.unlink()
                sec.unlink()
                print(f"    removed {sec.name}")

            # Re-extract merged PDF
            receipt, model, tier = extract_receipt(
                primary, **extract_kwargs, fuzzy_rules=rules
            )
            row = {
                "filename": primary.name,
                "scan_date": receipt.scan_date.isoformat(),
                "merchant": receipt.merchant,
                "total": receipt.total,
                "category": category,
                "receipt_dir": str(rel),
                "raw_text": receipt.raw_text,
                "processed_by": model,
                "purchaser": receipt.purchaser,
                "card_last4": receipt.card_last4,
                "tier": tier,
            }
            upsert_receipt(conn, row)
            write_sidecar(primary, {k: v for k, v in row.items() if k != "raw_text"})
            print(
                f"    -> merchant={receipt.merchant!r}, total={receipt.total}, model={model}"
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR: {exc}", file=sys.stderr)
            failed += 1

    conn.close()
    print(f"\nMerge complete: {ok} group(s) merged, {failed} error(s).")


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

    # backfill
    p_bf = sub.add_parser(
        "backfill",
        help="Ingest receipts already in category folders (tier-aware skip)",
    )
    p_bf.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_bf.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_bf.add_argument(
        "--dry-run", action="store_true", help="Print actions without writing to DB"
    )

    # merge
    p_mrg = sub.add_parser(
        "merge",
        help="Detect and merge multi-page receipt scans into single documents",
    )
    p_mrg.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_mrg.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_mrg.add_argument(
        "--window",
        metavar="MINUTES",
        default=5,
        help="Group PDFs whose scan times are within this many minutes (default: 5)",
    )
    p_mrg.add_argument(
        "--dry-run", action="store_true", help="Show groups without merging"
    )

    # audit
    p_aud = sub.add_parser("audit", help="List intake PDFs without processing them")
    p_aud.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")

    args = parser.parse_args()
    settings = _load_settings()

    if args.command == "process":
        _cmd_process(args, settings)
    elif args.command == "report":
        _cmd_report(args, settings)
    elif args.command == "backfill":
        _cmd_backfill(args, settings)
    elif args.command == "merge":
        _cmd_merge(args, settings)
    elif args.command == "audit":
        _cmd_audit(args, settings)


if __name__ == "__main__":
    main()
