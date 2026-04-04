"""CLI entry point for intake: process, report, audit subcommands."""

from __future__ import annotations

import argparse
import configparser
import importlib
import json
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO_ROOT / "data" / "intake.db"
_DEFAULT_REPORTS_DIR = _REPO_ROOT / "reports"
_SETTINGS_EXAMPLE = _REPO_ROOT / "config" / "settings.toml.example"
_SETTINGS_PATH = _REPO_ROOT / "config" / "settings.toml"
_AUTO_PASS_CONFIG_PATH = _REPO_ROOT / "config" / "auto-pass.ini"
_SETTINGS_EXAMPLE_LABEL = "config/settings.toml.example"
_SETTINGS_PATH_LABEL = "config/settings.toml"
_AUTO_PASS_CONFIG_LABEL = "config/auto-pass.ini"
_CLOCKWORK_CONFIG_DIR = _REPO_ROOT / "config" / "clockwork"
_CLOCKWORK_SRC_DIR = _REPO_ROOT.parent / "clockwork" / "src"


def _repo_relative_path(value: str | Path) -> Path:
    """Resolve a configured path relative to the repository root."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (_REPO_ROOT / path).resolve()


def _normalise_settings_paths(settings: dict) -> None:
    """Convert repo-relative settings to absolute paths.

    The daemon is launched by systemd and should not depend on its cwd to find
    repo-local config, database, or reports paths.
    """
    for key in ("receipt_dir", "db_path", "reports_dir"):
        if settings.get(key):
            settings[key] = str(_repo_relative_path(settings[key]))

    email_cfg = settings.get("email")
    if isinstance(email_cfg, dict) and email_cfg.get("shock_relay_config"):
        email_cfg["shock_relay_config"] = str(_repo_relative_path(email_cfg["shock_relay_config"]))

    signal_cfg = settings.get("signal")
    if isinstance(signal_cfg, dict) and signal_cfg.get("shock_relay_config"):
        signal_cfg["shock_relay_config"] = str(
            _repo_relative_path(signal_cfg["shock_relay_config"])
        )

    gmail_cfg = settings.get("gmail")
    if isinstance(gmail_cfg, dict) and gmail_cfg.get("shock_relay_config"):
        gmail_cfg["shock_relay_config"] = str(_repo_relative_path(gmail_cfg["shock_relay_config"]))

    pf_cfg = settings.get("personal_finance")
    if isinstance(pf_cfg, dict):
        for key in ("repo_root", "event_dir"):
            if pf_cfg.get(key):
                pf_cfg[key] = str(_repo_relative_path(pf_cfg[key]))


def _notify_processed_receipt(
    settings: dict,
    receipt,
    *,
    category: str,
    receipt_dir: str,
    filename: str,
    processed_by: str,
    prefix: str = "    ",
) -> None:
    """Send best-effort notifications for a newly processed receipt."""
    from intake.notify import send_processed_receipt_notifications

    sent, errors = send_processed_receipt_notifications(
        settings,
        receipt,
        category=category,
        receipt_dir=receipt_dir,
        filename=filename,
        processed_by=processed_by,
    )
    for channel, error in errors.items():
        print(f"{prefix}[{channel}] WARNING: {error}", file=sys.stderr)
    for channel, count in sent.items():
        print(f"{prefix}[{channel}] notified {count} recipient(s)")


def _trigger_personal_finance(
    settings: dict,
    receipt_events: list[dict],
    *,
    trigger: str,
    prefix: str = "",
) -> None:
    """Run the configured downstream personal-finance trigger."""
    from intake.personal_finance import trigger_personal_finance

    try:
        json_path = trigger_personal_finance(settings, receipt_events, trigger=trigger)
    except Exception as exc:  # noqa: BLE001
        print(f"{prefix}[personal-finance] WARNING: {exc}", file=sys.stderr)
        return

    if json_path is not None:
        print(f"{prefix}[personal-finance] triggered via {json_path}")


def _toml_string(value: str) -> str:
    """Render a string as a TOML-compatible basic string literal."""
    return json.dumps(value)


def _render_clockwork_template(template_name: str, replacements: dict[str, str]) -> str:
    """Render a tracked clockwork template with TOML-safe replacements."""
    template_path = _CLOCKWORK_CONFIG_DIR / template_name
    rendered = template_path.read_text(encoding="utf-8")
    for token, replacement in replacements.items():
        rendered = rendered.replace(token, replacement)
    return rendered


def _load_clockwork_main():
    """Return the clockwork CLI entry point from install or sibling repo."""
    try:
        return importlib.import_module("clockwork.cli").main
    except ModuleNotFoundError as exc:
        if exc.name != "clockwork":
            raise

    if not _CLOCKWORK_SRC_DIR.exists():
        raise RuntimeError(
            "clockwork is not installed and sibling repo was not found at "
            f"{_CLOCKWORK_SRC_DIR}. Install clockwork or clone ./util-repos/clockwork."
        )

    clockwork_src = str(_CLOCKWORK_SRC_DIR)
    if clockwork_src not in sys.path:
        sys.path.insert(0, clockwork_src)
    return importlib.import_module("clockwork.cli").main


def _install_clockwork_manifest(manifest_text: str, *, target: str) -> None:
    """Write scheduler units through the shared clockwork renderer."""
    clockwork_main = _load_clockwork_main()
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as handle:
        handle.write(manifest_text)
        manifest_path = Path(handle.name)
    try:
        exit_code = clockwork_main(
            [
                "install",
                "--manifest",
                str(manifest_path),
                "--target",
                target,
                "--unit-dir",
                str(unit_dir),
            ]
        )
    finally:
        manifest_path.unlink(missing_ok=True)

    if exit_code != 0:
        raise RuntimeError(f"clockwork install failed with exit code {exit_code}")


def _build_wait_exec_start(*, python_executable: str, ready_message: str, command: str) -> str:
    """Build the guarded shell wrapper used by intake's scheduled units."""
    python_q = shlex.quote(python_executable)
    return (
        f'/bin/bash -lc \'until {python_q} -c "import intake.cli" >/dev/null 2>&1; do '
        f'echo "{ready_message}"; '
        "sleep 30; "
        f"done; exec {python_q} -m intake.cli {command}'"
    )


def _build_daemon_manifest_text(python_executable: str) -> str:
    """Render the tracked clockwork manifest for intake-daemon.service."""
    exec_start = _build_wait_exec_start(
        python_executable=python_executable,
        ready_message="intake daemon: waiting for intake module path to become available",
        command="daemon",
    )
    return _render_clockwork_template(
        "intake-daemon.toml.template",
        {"__EXEC_START__": _toml_string(exec_start)},
    )


def _build_report_manifest_text(*, python_executable: str, on_calendar: str) -> str:
    """Render the tracked clockwork manifest for intake-report.{service,timer}."""
    exec_start = _build_wait_exec_start(
        python_executable=python_executable,
        ready_message="intake report timer: waiting for intake module path to become available",
        command="report",
    )
    return _render_clockwork_template(
        "intake-report.toml.template",
        {
            "__EXEC_START__": _toml_string(exec_start),
            "__ON_CALENDAR__": _toml_string(on_calendar),
        },
    )


def _load_repo_auto_pass_config() -> dict[str, str]:
    if not _AUTO_PASS_CONFIG_PATH.exists():
        return {}

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        with _AUTO_PASS_CONFIG_PATH.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise RuntimeError(f"invalid {_AUTO_PASS_CONFIG_LABEL}: {exc}") from exc

    defaults: dict[str, str] = {}
    if parser.has_section("auto_pass"):
        profile = parser.get("auto_pass", "profile", fallback="").strip()
        if profile:
            defaults["auto_pass_profile"] = profile
    if parser.has_section("intake"):
        entry = parser.get("intake", "openai_api_key_entry", fallback="").strip()
        if entry:
            defaults["openai_api_key_entry"] = entry
    return defaults


def _load_settings() -> dict:
    settings_path = _SETTINGS_PATH
    if not settings_path.exists():
        return {}
    import toml

    settings = toml.load(settings_path)
    _normalise_settings_paths(settings)
    try:
        auto_pass_defaults = _load_repo_auto_pass_config()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    for key, value in auto_pass_defaults.items():
        settings.setdefault(key, value)

    # Resolve OpenAI API key from KeePassXC via auto-pass if not set directly
    if not settings.get("openai_api_key") and settings.get("openai_api_key_entry"):
        try:
            from pathlib import Path as _Path

            from auto_pass.envfile import load_config_environment
            from auto_pass.keepassxc import resolve_keepassxc_entry

            # auto-pass.env.local lives in the auto-pass sibling repo
            _ap_env = _Path(__file__).parent.parent.parent / "auto-pass/config/auto-pass.env.local"
            load_config_environment(
                path=_ap_env,
                profile=settings.get("auto_pass_profile") or None,
            )

            result = resolve_keepassxc_entry(
                settings["openai_api_key_entry"],
                attrs_map={"password": "password"},
            )
            key = result.get("password", "").strip()
            if key:
                settings["openai_api_key"] = key
        except Exception:  # noqa: BLE001
            pass

    return settings


def _require_receipt_dir(settings: dict, args_value: str | None) -> Path:
    val = args_value or settings.get("receipt_dir")
    if not val:
        print(
            "ERROR: receipt_dir is not set.\n"
            f"  Copy {_SETTINGS_EXAMPLE_LABEL} to {_SETTINGS_PATH_LABEL} and set receipt_dir.",
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
            settings.get("openai_model_low") or settings.get("openai_model") or "gpt-4o-mini"
        ),
        "openai_model_mid": settings.get("openai_model_mid") or "gpt-4o",
        "openai_model_high": settings.get("openai_model_high") or "gpt-4.1",
        "escalation_patterns": settings.get("escalation_patterns") or None,
        "fuzzy_threshold": float(settings.get("fuzzy_threshold", 0.82)),
    }


def _low_model(settings: dict) -> str:
    if not settings.get("openai_api_key"):
        from intake.extract import OCR_MODEL

        return OCR_MODEL
    return settings.get("openai_model_low") or settings.get("openai_model") or "gpt-4o-mini"


def _mid_model(settings: dict) -> str:
    if not settings.get("openai_api_key"):
        from intake.extract import OCR_MODEL

        return OCR_MODEL
    return settings.get("openai_model_mid") or "gpt-4o"


def _high_model(settings: dict) -> str:
    if not settings.get("openai_api_key"):
        from intake.extract import OCR_MODEL

        return OCR_MODEL
    return settings.get("openai_model_high") or "gpt-4.1"


def _skip_sidecar(sc: dict, settings: dict, pdf_path: Path | None = None) -> bool:
    """Return True if a sidecar indicates the receipt is already up-to-date.

    Uses tier-aware comparison so a receipt processed at the low-fidelity
    model level is only re-queued when a better *low* model is configured,
    and similarly for high-fidelity.  This prevents low-tier receipts from
    being endlessly re-queued just because a high model is also configured.

    Exception: a previously ``ok``-tier file whose filename matches the
    filename escalation patterns is re-queued for high-fidelity extraction.
    """
    from intake.extract import (
        DEFAULT_FILENAME_ESCALATION_PATTERNS,
        OCR_MODEL,
        _compile_patterns,
        _filename_escalates,
        model_rank,
    )

    tier = sc.get("tier", "ok")
    sidecar_model = sc.get("processed_by", OCR_MODEL)
    if tier == "ok":
        # Re-queue if filename now matches escalation and we have an AI key
        if pdf_path and settings.get("openai_api_key"):
            fname_patterns = _compile_patterns(DEFAULT_FILENAME_ESCALATION_PATTERNS)
            if _filename_escalates(pdf_path.name, fname_patterns):
                return model_rank(sidecar_model) >= model_rank(_high_model(settings))
        return True
    if tier == "low":
        return model_rank(sidecar_model) >= model_rank(_low_model(settings))
    if tier == "mid":
        return model_rank(sidecar_model) >= model_rank(_mid_model(settings))
    if tier == "high":
        return model_rank(sidecar_model) >= model_rank(_high_model(settings))
    return False


def _cmd_process(args: argparse.Namespace, settings: dict) -> None:
    from intake.classify import classify, load_merchant_aliases, load_rules
    from intake.db import init_db, upsert_receipt
    from intake.extract import extract_receipt
    from intake.personal_finance import build_receipt_event
    from intake.sidecar import write_sidecar

    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    intake_dir = receipt_dir / "intake"

    if not intake_dir.exists():
        print(f"ERROR: intake directory not found: {intake_dir}", file=sys.stderr)
        sys.exit(1)

    pdfs = sorted(intake_dir.glob("*.pdf"))
    if not pdfs:
        print("No PDFs found in intake.")
        return

    rules = load_rules()
    merchant_aliases = load_merchant_aliases()
    conn = init_db(db_path)
    pf_events: list[dict] = []

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
                merchant_aliases=merchant_aliases,
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
            renamed = f" (renamed {pdf.name} -> {dest.name})" if dest.name != pdf.name else ""

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
            _notify_processed_receipt(
                settings,
                receipt,
                category=category,
                receipt_dir=str(dest.relative_to(receipt_dir)),
                filename=dest.name,
                processed_by=model,
            )
            pf_events.append(
                build_receipt_event(
                    receipt,
                    category=category,
                    receipt_dir=str(dest.relative_to(receipt_dir)),
                    filename=dest.name,
                    processed_by=model,
                    tier=tier,
                )
            )
            print(
                f"    -> {category}/{dest.name} (merchant={receipt.merchant!r}, total={receipt.total}, model={model}){renamed}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR processing {pdf.name}: {exc}", file=sys.stderr)

    conn.close()
    _trigger_personal_finance(settings, pf_events, trigger="process")


def _cmd_report(args: argparse.Namespace, settings: dict) -> None:
    if getattr(args, "install_timer", False):
        _install_report_timer_units(on_calendar=args.on_calendar)
        return

    from intake.db import init_db
    from intake.report import generate_reports

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    output_dir = Path(args.output_dir or settings.get("reports_dir") or _DEFAULT_REPORTS_DIR)
    fmt = args.format or "both"

    conn = init_db(db_path)
    receipt_root = Path(settings["receipt_dir"]) if settings.get("receipt_dir") else None
    written = generate_reports(conn, output_dir, fmt=fmt, receipt_root=receipt_root)
    conn.close()

    for p in written:
        print(f"  Report written: {p}")


def _cmd_backfill(args: argparse.Namespace, settings: dict) -> None:
    from intake.classify import load_merchant_aliases, load_rules
    from intake.db import init_db, upsert_receipt
    from intake.extract import extract_receipt
    from intake.personal_finance import build_receipt_event
    from intake.sidecar import read_sidecar, write_sidecar

    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    rules = load_rules()
    merchant_aliases = load_merchant_aliases()
    extract_kwargs = _get_extract_kwargs(settings)

    all_pdfs = [
        pdf
        for pdf in sorted(receipt_dir.rglob("*.pdf"))
        if pdf.relative_to(receipt_dir).parts[0] not in ("intake", "exhaust")
        and len(pdf.relative_to(receipt_dir).parts) >= 2
    ]

    if not all_pdfs:
        print("No receipts found in category folders.")
        return

    pending: list[Path] = []
    skipped = 0
    for pdf in all_pdfs:
        sc = read_sidecar(pdf)
        if sc is not None and _skip_sidecar(sc, settings, pdf_path=pdf):
            skipped += 1
            continue
        pending.append(pdf)

    print(f"Found {len(all_pdfs)} receipt(s); {skipped} up-to-date, {len(pending)} pending.")

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
    pf_events: list[dict] = []

    for pdf in pending:
        rel = pdf.relative_to(receipt_dir)
        category = "/".join(rel.parts[:-1])
        print(f"  Backfilling: {rel}")
        try:
            receipt, model, tier = extract_receipt(pdf, **extract_kwargs, fuzzy_rules=rules, merchant_aliases=merchant_aliases)
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
            _notify_processed_receipt(
                settings,
                receipt,
                category=category,
                receipt_dir=str(rel),
                filename=pdf.name,
                processed_by=model,
            )
            pf_events.append(
                build_receipt_event(
                    receipt,
                    category=category,
                    receipt_dir=str(rel),
                    filename=pdf.name,
                    processed_by=model,
                    tier=tier,
                )
            )
            print(
                f"    -> [{tier}] category={category!r}, merchant={receipt.merchant!r}, "
                f"total={receipt.total}, model={model}"
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR: {exc}", file=sys.stderr)
            failed += 1

    conn.close()
    _trigger_personal_finance(settings, pf_events, trigger="backfill")
    print(f"\nBackfill complete: {ok} ingested, {failed} error(s).")


# Keywords in merchant names that suggest product docs rather than receipts
_EXHAUST_MERCHANT_KEYWORDS = frozenset(
    [
        "product brief",
        "manual",
        "user guide",
        "datasheet",
        "specification",
        "quick start",
        "installation guide",
    ]
)
# Filename stem substrings that indicate product docs
_EXHAUST_FILENAME_KEYWORDS = frozenset(["manual", "datasheet", "spec", "guide", "brief", "readme"])


def _is_exhaust_candidate(pdf: Path, sidecar: dict | None) -> bool:
    """Return True if the PDF looks like a product doc rather than a receipt."""
    stem = pdf.stem.lower()
    if any(kw in stem for kw in _EXHAUST_FILENAME_KEYWORDS):
        return True
    if sidecar:
        merchant = (sidecar.get("merchant") or "").lower()
        if any(kw in merchant for kw in _EXHAUST_MERCHANT_KEYWORDS):
            return True
        # No total and merchant name looks non-transactional
        if sidecar.get("total") in (None, 0.0) and any(
            kw in merchant for kw in ("brief", "manual", "guide", "spec")
        ):
            return True
    return False


def _cmd_exhaust(args: argparse.Namespace, settings: dict) -> None:
    """Move product docs / non-receipt PDFs to the exhaust folder."""
    from intake.db import init_db
    from intake.sidecar import read_sidecar

    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    exhaust_dir = receipt_dir / "exhaust"

    all_pdfs = [
        pdf
        for pdf in sorted(receipt_dir.rglob("*.pdf"))
        if pdf.relative_to(receipt_dir).parts[0] not in ("intake", "exhaust")
        and len(pdf.relative_to(receipt_dir).parts) >= 2
    ]

    candidates = [
        (pdf, read_sidecar(pdf))
        for pdf in all_pdfs
        if _is_exhaust_candidate(pdf, read_sidecar(pdf))
    ]

    if not candidates:
        print("No exhaust candidates found.")
        return

    print(f"Found {len(candidates)} exhaust candidate(s):")
    for pdf, sc in candidates:
        merchant = sc.get("merchant", "?") if sc else "?"
        total = sc.get("total", "?") if sc else "?"
        print(f"  {pdf.relative_to(receipt_dir)}  (merchant={merchant!r}, total={total})")

    if args.dry_run:
        return

    conn = init_db(db_path)
    moved = 0
    for pdf, _sc in candidates:
        rel = pdf.relative_to(receipt_dir)
        dest = exhaust_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        pdf.rename(dest)
        sc_src = pdf.with_suffix(".intake.json")
        if sc_src.exists():
            sc_src.rename(dest.with_suffix(".intake.json"))
        # Remove from DB — exhausted items are not receipts
        conn.execute("DELETE FROM receipts WHERE filename = ?", (pdf.name,))
        conn.commit()
        print(f"  moved → exhaust/{rel}  (removed from DB)")
        moved += 1

    conn.close()
    print(f"\nExhaust complete: {moved} file(s) moved.")


def _cmd_review(args: argparse.Namespace, settings: dict) -> None:
    """Print flagged receipts to the terminal and optionally write a Markdown file."""
    import textwrap

    from intake.db import init_db, query_flagged

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    include_suppressed = getattr(args, "show_suppressed", False)
    conn = init_db(db_path)
    flagged = query_flagged(conn, include_suppressed=include_suppressed)
    conn.close()

    if not flagged:
        msg = "No suspicious records found"
        msg += (
            " (suppressed records hidden — use --show-suppressed to see all)."
            if not include_suppressed
            else "."
        )
        print(msg)
        return

    # ── terminal summary ──────────────────────────────────────────────────────
    col_w = {"#": 4, "file": 36, "merchant": 28, "total": 9, "model": 12, "dir": 34}
    header = (
        f"{'#':<{col_w['#']}} "
        f"{'file':<{col_w['file']}} "
        f"{'merchant':<{col_w['merchant']}} "
        f"{'total':>{col_w['total']}} "
        f"{'model':<{col_w['model']}} "
        f"{'dir':<{col_w['dir']}}"
    )
    sep = "-" * len(header)

    print(f"\nFlagged receipts: {len(flagged)}")
    print(sep)
    print(header)
    print(sep)

    for i, row in enumerate(flagged, 1):
        merchant = (row["merchant"] or "—")[: col_w["merchant"]]
        filename = row["filename"][: col_w["file"]]
        total = f"{row['total']:.2f}" if row["total"] is not None else "—"
        model = (row["processed_by"] or "tesseract")[: col_w["model"]]
        receipt_dir = (row["receipt_dir"] or "")[: col_w["dir"]]
        print(
            f"{i:<{col_w['#']}} "
            f"{filename:<{col_w['file']}} "
            f"{merchant:<{col_w['merchant']}} "
            f"{total:>{col_w['total']}} "
            f"{model:<{col_w['model']}} "
            f"{receipt_dir:<{col_w['dir']}}"
        )

    print(sep)
    print("\nFlag reasons: null/unknown/short merchant · zero/null total · email in merchant")

    # ── Markdown export ───────────────────────────────────────────────────────
    output_dir = Path(args.output_dir or settings.get("reports_dir") or _DEFAULT_REPORTS_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "review.md"

    lines = [
        "# Intake Review — Flagged Receipts",
        "",
        f"**{len(flagged)} record(s)** flagged for manual review.",
        "Edit the `correction` column and re-import with `intake correct` *(future)*.",
        "",
        "| # | file | receipt_dir | merchant | total | model | correction |",
        "|---|------|-------------|----------|-------|-------|------------|",
    ]
    for i, row in enumerate(flagged, 1):
        merchant = (row["merchant"] or "—").replace("|", "\\|")
        filename = row["filename"].replace("|", "\\|")
        receipt_dir = (row["receipt_dir"] or "").replace("|", "\\|")
        total = f"{row['total']:.2f}" if row["total"] is not None else "—"
        model = row["processed_by"] or "tesseract"
        lines.append(
            f"| {i} | `{filename}` | `{receipt_dir}` | {merchant} | {total} | {model} |  |"
        )

    lines += [
        "",
        "## Notes",
        "",
        textwrap.dedent("""\
            - To force re-extraction with GPT: delete the `.intake.json` sidecar
              next to the PDF and run `intake backfill`.
            - To move a non-receipt to exhaust: `intake exhaust` (auto-detects) or
              move manually then re-run `intake backfill`.
            - To correct a merchant name directly:
              `sqlite3 data/intake.db "UPDATE receipts SET merchant='name' WHERE filename='...'"`
            """),
    ]

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReview file written → {md_path}")


def _cmd_suppress(args: argparse.Namespace, settings: dict) -> None:
    """Mark flagged receipts as reviewed so they no longer appear in 'intake review'."""
    from intake.db import init_db, query_flagged, suppress_receipts, unsuppress_receipts

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    conn = init_db(db_path)

    if args.unsuppress:
        if args.filenames:
            n = unsuppress_receipts(conn, args.filenames)
            print(f"Unsuppressed {n} record(s).")
        else:
            print("ERROR: --unsuppress requires at least one filename.", file=sys.stderr)
            sys.exit(1)
        conn.close()
        return

    if args.all:
        # Suppress everything currently flagged
        flagged = query_flagged(conn, include_suppressed=False)
        if not flagged:
            print("Nothing to suppress — no unsuppressed flagged records.")
            conn.close()
            return
        filenames = [r["filename"] for r in flagged]
        n = suppress_receipts(conn, filenames)
        print(f"Suppressed {n} record(s):")
        for f in filenames:
            print(f"  {f}")
    elif args.filenames:
        n = suppress_receipts(conn, args.filenames)
        print(f"Suppressed {n} record(s).")
    else:
        print("ERROR: provide filename(s) or --all.", file=sys.stderr)
        sys.exit(1)

    conn.close()


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
        if folder.relative_to(receipt_dir).parts[0] in ("intake", "exhaust"):
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

    from intake.classify import load_merchant_aliases, load_rules
    from intake.db import init_db, upsert_receipt
    from intake.extract import extract_receipt
    from intake.sidecar import write_sidecar

    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
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
    merchant_aliases = load_merchant_aliases()
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
            receipt, model, tier = extract_receipt(primary, **extract_kwargs, fuzzy_rules=rules, merchant_aliases=merchant_aliases)
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
            print(f"    -> merchant={receipt.merchant!r}, total={receipt.total}, model={model}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR: {exc}", file=sys.stderr)
            failed += 1

    conn.close()
    print(f"\nMerge complete: {ok} group(s) merged, {failed} error(s).")


def _cmd_email(args: argparse.Namespace, settings: dict) -> None:
    from intake.db import init_db, query_email_stats
    from intake.email import fetch_receipts, run_daemon

    db_path = Path(getattr(args, "db", None) or settings.get("db_path") or _DEFAULT_DB)
    conn = init_db(db_path)

    email_cfg = settings.setdefault("email", {})

    if args.email_command == "fetch":
        if hasattr(args, "since_days") and args.since_days is not None:
            email_cfg["since_days"] = args.since_days
        dry = getattr(args, "dry_run", False)
        print(f"Fetching receipt emails{'(dry run)' if dry else ''}…")
        result = fetch_receipts(settings, conn, dry_run=dry, verbose=True)
        print(
            f"\nFetch complete: {result.attachments_saved} PDF attachment(s) saved, "
            f"{result.html_processed} inline receipt(s) processed, "
            f"{result.skipped} skipped, {result.errors} error(s)."
        )
        if result.saved_files:
            print("Saved files:")
            for f in result.saved_files:
                print(f"  {f}")
            print("\nRun 'intake process' to OCR and classify the new attachments.")

    elif args.email_command == "daemon":
        if hasattr(args, "interval") and args.interval is not None:
            email_cfg["poll_interval_seconds"] = args.interval
        run_daemon(settings, conn, verbose=True)

    elif args.email_command == "status":
        stats = query_email_stats(conn)
        print(f"Email receipts processed : {stats['total_processed']}")
        print(f"Last run                 : {stats['last_run'] or 'never'}")
        if stats["by_status"]:
            print("By status:")
            for status_val, count in stats["by_status"].items():
                print(f"  {status_val:<20} {count}")

    conn.close()


def _process_inbox(settings: dict, db_path: Path) -> tuple[int, int]:
    """Process all PDFs currently in the intake inbox folder.

    Returns (ok_count, error_count).  Mirrors _cmd_process but is callable
    from the daemon loop without requiring argparse Namespace objects.
    """
    from intake.classify import classify, load_merchant_aliases, load_rules
    from intake.db import init_db, upsert_receipt
    from intake.extract import extract_receipt
    from intake.personal_finance import build_receipt_event
    from intake.sidecar import write_sidecar

    receipt_dir = Path(settings.get("receipt_dir", ""))
    if not receipt_dir:
        return 0, 0
    intake_dir = receipt_dir / "intake"
    pdfs = sorted(intake_dir.glob("*.pdf")) if intake_dir.exists() else []
    if not pdfs:
        return 0, 0

    rules = load_rules()
    merchant_aliases = load_merchant_aliases()
    conn = init_db(db_path)
    ok = errors = 0
    pf_events: list[dict] = []
    for pdf in pdfs:
        try:
            receipt, model, tier = extract_receipt(
                pdf,
                **_get_extract_kwargs(settings),
                fuzzy_rules=rules,
                merchant_aliases=merchant_aliases,
            )
            category = classify(receipt.merchant, rules, receipt.raw_text)
            dest_dir = receipt_dir / category
            dest_dir.mkdir(parents=True, exist_ok=True)
            new_name = receipt.suggested_filename()
            dest = dest_dir / new_name
            if dest.exists() and dest != pdf:
                stem, counter = dest.stem, 1
                while dest.exists():
                    dest = dest_dir / f"{stem}-{counter}.pdf"
                    counter += 1
            shutil.move(str(pdf), dest)
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
            _notify_processed_receipt(
                settings,
                receipt,
                category=category,
                receipt_dir=str(dest.relative_to(receipt_dir)),
                filename=dest.name,
                processed_by=model,
                prefix="  ",
            )
            pf_events.append(
                build_receipt_event(
                    receipt,
                    category=category,
                    receipt_dir=str(dest.relative_to(receipt_dir)),
                    filename=dest.name,
                    processed_by=model,
                    tier=tier,
                )
            )
            print(
                f"  [inbox] {dest.relative_to(receipt_dir)} "
                f"merchant={receipt.merchant!r} total={receipt.total} model={model}"
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [inbox] ERROR {pdf.name}: {exc}", file=sys.stderr)
            errors += 1
    conn.close()
    _trigger_personal_finance(settings, pf_events, trigger="daemon")
    return ok, errors


def _cmd_daemon(args: argparse.Namespace, settings: dict) -> None:
    """Run the unified intake daemon (file inbox + email polling)."""
    import signal
    import time

    # ── systemd unit install ──────────────────────────────────────────────────
    if getattr(args, "install", False):
        _install_systemd_unit(settings)
        return

    interval: int = getattr(args, "interval", None) or int(
        settings.get("email", {}).get("poll_interval_seconds", 300)
    )
    db_path = Path(getattr(args, "db", None) or settings.get("db_path") or _DEFAULT_DB)

    from intake.db import init_db
    from intake.email import fetch_receipts

    db_conn = init_db(db_path)

    stop = False

    def _handle_signal(signum, frame):  # noqa: ANN001
        nonlocal stop
        print(f"\nintake daemon: received signal {signum}, shutting down…")
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Ensure output is unbuffered when running as a systemd service (no TTY)
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[union-attr]

    print(
        f"intake daemon started — polling every {interval}s  (Ctrl-C or SIGTERM to stop)",
        flush=True,
    )

    while not stop:
        receipt_dir = Path(settings.get("receipt_dir", ""))
        volume_ok = bool(receipt_dir) and receipt_dir.exists()

        if not volume_ok:
            print(
                f"[volume] {receipt_dir} not accessible (LUKS/SMB not mounted?) — skipping cycle",
                flush=True,
            )
        else:
            # 1. Process file inbox
            inbox_pdfs = (
                len(list((receipt_dir / "intake").glob("*.pdf")))
                if (receipt_dir / "intake").exists()
                else 0
            )
            if inbox_pdfs:
                print(f"[inbox] {inbox_pdfs} PDF(s) found — processing…", flush=True)
                ok, err = _process_inbox(settings, db_path)
                print(f"[inbox] done: {ok} processed, {err} error(s)", flush=True)
            else:
                print("[inbox] nothing new", flush=True)

            # 2. Fetch email receipts (PDFs land in receipt_dir/intake, needs volume)
            email_cfg = settings.get("email", {})
            if email_cfg.get("shock_relay_config"):
                try:
                    result = fetch_receipts(settings, db_conn, verbose=True)
                    print(
                        f"[email] done: {result.attachments_saved} attachment(s), "
                        f"{result.html_processed} inline, {result.errors} error(s)",
                        flush=True,
                    )
                    # Process any PDFs saved from email attachments
                    if result.attachments_saved:
                        ok, err = _process_inbox(settings, db_path)
                        print(
                            f"[inbox] post-email: {ok} processed, {err} error(s)",
                            flush=True,
                        )
                except Exception as exc:  # noqa: BLE001
                    print(f"[email] ERROR: {exc}", file=sys.stderr, flush=True)
            else:
                print("[email] skipped (no shock_relay_config configured)", flush=True)

        if not stop:
            print(f"sleeping {interval}s…\n", flush=True)
            # Sleep in small increments so SIGTERM is handled promptly
            for _ in range(interval):
                if stop:
                    break
                time.sleep(1)

    db_conn.close()
    print("intake daemon stopped.", flush=True)


def _install_systemd_unit(settings: dict) -> None:
    """Install the daemon unit through the shared clockwork renderer."""
    _install_clockwork_manifest(
        _build_daemon_manifest_text(sys.executable),
        target="systemd-user",
    )


def _install_report_timer_units(*, on_calendar: str) -> None:
    """Install report service/timer units through the shared clockwork renderer."""
    _install_clockwork_manifest(
        _build_report_manifest_text(
            python_executable=sys.executable,
            on_calendar=on_calendar,
        ),
        target="systemd-user",
    )


def _cmd_consolidate(args: argparse.Namespace, settings: dict) -> None:
    """Apply the merchant alias map to existing DB rows and sidecars.

    Does NOT re-OCR.  Reads every receipt from the database, applies
    ``consolidate_merchant`` using the current ``[merchants]`` config, and
    patches any row whose merchant name changes.  The companion sidecar JSON
    is updated in-place if one exists alongside the PDF.
    """
    from intake.classify import load_merchant_aliases
    from intake.db import init_db
    from intake.merchant import consolidate_merchant
    from intake.sidecar import read_sidecar, sidecar_path, write_sidecar

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    receipt_dir = _require_receipt_dir(settings, getattr(args, "receipt_dir", None))

    merchant_aliases = load_merchant_aliases()
    if not merchant_aliases:
        print("No merchant aliases configured in categories.toml — nothing to do.")
        return

    conn = init_db(db_path)
    rows = conn.execute("SELECT filename, merchant, receipt_dir FROM receipts").fetchall()

    changed = skipped = 0
    for row in rows:
        filename = row["filename"]
        old_merchant = row["merchant"] or ""
        new_merchant = consolidate_merchant(old_merchant, merchant_aliases)

        if new_merchant == old_merchant:
            skipped += 1
            continue

        if args.dry_run:
            print(f"  {filename}: {old_merchant!r} → {new_merchant!r}  [dry-run]")
            changed += 1
            continue

        conn.execute(
            "UPDATE receipts SET merchant = ? WHERE filename = ?",
            (new_merchant, filename),
        )
        conn.commit()

        # Patch sidecar if it exists alongside the PDF
        receipt_dir_rel = row["receipt_dir"] or ""
        if receipt_dir_rel:
            pdf = receipt_dir / receipt_dir_rel
            sc_path = sidecar_path(pdf)
            if sc_path.exists():
                sc = read_sidecar(pdf)
                if sc is not None:
                    sc["merchant"] = new_merchant
                    sc_path.write_text(__import__("json").dumps(sc, indent=2))

        print(f"  {filename}: {old_merchant!r} → {new_merchant!r}")
        changed += 1

    noun = "receipt" if changed == 1 else "receipts"
    suffix = "  (dry-run)" if args.dry_run else ""
    print(f"\nConsolidated {changed} {noun}; {skipped} already canonical.{suffix}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="intake",
        description="Process, sort, and report on receipt PDF scans.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # process
    p_proc = sub.add_parser("process", help="Run the full OCR → classify → move → DB pipeline")
    p_proc.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_proc.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_proc.add_argument("--dry-run", action="store_true", help="Print actions without moving files")

    # report
    p_rep = sub.add_parser("report", help="Generate Markdown and/or HTML summary reports")
    p_rep.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_rep.add_argument("--output-dir", metavar="PATH", help="Directory to write reports into")
    p_rep.add_argument(
        "--format", choices=["md", "html", "both"], default="both", help="Report format"
    )
    p_rep.add_argument(
        "--install-timer",
        action="store_true",
        help="Write user-level systemd units to regenerate reports on a schedule",
    )
    p_rep.add_argument(
        "--on-calendar",
        default="*-*-* 12:00:00",
        help="systemd OnCalendar expression for --install-timer (default: daily at noon)",
    )

    # backfill
    p_bf = sub.add_parser(
        "backfill",
        help="Ingest receipts already in category folders (tier-aware skip)",
    )
    p_bf.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_bf.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_bf.add_argument("--dry-run", action="store_true", help="Print actions without writing to DB")

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
    p_mrg.add_argument("--dry-run", action="store_true", help="Show groups without merging")

    # exhaust
    p_exh = sub.add_parser(
        "exhaust",
        help="Move product docs / non-receipt PDFs to the exhaust folder",
    )
    p_exh.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_exh.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_exh.add_argument("--dry-run", action="store_true", help="Show candidates without moving")

    # review
    p_rev = sub.add_parser(
        "review",
        help="Print flagged receipts to terminal and write reports/review.md",
    )
    p_rev.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_rev.add_argument("--output-dir", metavar="PATH", help="Directory to write review.md into")
    p_rev.add_argument(
        "--show-suppressed",
        action="store_true",
        help="Include records already marked as reviewed",
    )

    # suppress
    p_sup = sub.add_parser(
        "suppress",
        help="Mark flagged receipts as reviewed so they no longer appear in 'intake review'",
    )
    p_sup.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_sup.add_argument(
        "filenames",
        nargs="*",
        metavar="FILENAME",
        help="One or more PDF filenames to suppress (e.g. 20110729.pdf)",
    )
    p_sup.add_argument(
        "--all",
        action="store_true",
        help="Suppress all currently flagged records at once",
    )
    p_sup.add_argument(
        "--unsuppress",
        action="store_true",
        help="Clear the reviewed flag (requires filename argument)",
    )

    # consolidate
    p_con = sub.add_parser(
        "consolidate",
        help="Apply merchant aliases to existing DB rows and sidecars (no re-OCR)",
    )
    p_con.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_con.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_con.add_argument("--dry-run", action="store_true", help="Print changes without writing")

    # audit
    p_aud = sub.add_parser("audit", help="List intake PDFs without processing them")
    p_aud.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")

    # daemon
    p_dmn = sub.add_parser(
        "daemon",
        help="Poll file inbox + email for new receipts continuously (systemd-compatible)",
    )
    p_dmn.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_dmn.add_argument(
        "--interval",
        metavar="SECONDS",
        type=int,
        help="Poll interval in seconds (default: poll_interval_seconds from settings, or 300)",
    )
    p_dmn.add_argument(
        "--install",
        action="store_true",
        help="Write a systemd user unit file and print activation instructions",
    )

    # email
    p_em = sub.add_parser(
        "email",
        help="Fetch receipt emails from Gmail via IMAP (shock-relay)",
    )
    p_em_sub = p_em.add_subparsers(dest="email_command", required=True)

    p_em_fetch = p_em_sub.add_parser("fetch", help="One-shot: fetch and process new receipt emails")
    p_em_fetch.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_em_fetch.add_argument("--dry-run", action="store_true", help="Show what would be fetched")
    p_em_fetch.add_argument(
        "--since-days", metavar="N", type=int, help="Override since_days from settings"
    )

    p_em_daemon = p_em_sub.add_parser(
        "daemon", help="Poll for new receipt emails continuously (systemd-compatible)"
    )
    p_em_daemon.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_em_daemon.add_argument(
        "--interval",
        metavar="SECONDS",
        type=int,
        help="Override poll_interval_seconds from settings",
    )

    p_em_status = p_em_sub.add_parser("status", help="Show email ingestion statistics")
    p_em_status.add_argument("--db", metavar="PATH", help="SQLite database path")

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
    elif args.command == "exhaust":
        _cmd_exhaust(args, settings)
    elif args.command == "review":
        _cmd_review(args, settings)
    elif args.command == "suppress":
        _cmd_suppress(args, settings)
    elif args.command == "consolidate":
        _cmd_consolidate(args, settings)
    elif args.command == "audit":
        _cmd_audit(args, settings)
    elif args.command == "email":
        _cmd_email(args, settings)
    elif args.command == "daemon":
        _cmd_daemon(args, settings)


if __name__ == "__main__":
    main()
