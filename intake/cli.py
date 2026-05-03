"""CLI entry point for intake: process, report, audit subcommands."""

from __future__ import annotations

import argparse
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
_SETTINGS_EXAMPLE_LABEL = "config/settings.toml.example"
_SETTINGS_PATH_LABEL = "config/settings.toml"
_CLOCKWORK_CONFIG_DIR = _REPO_ROOT / "config" / "clockwork"
_CLOCKWORK_SRC_DIR = _REPO_ROOT.parent / "clockwork" / "src"
_PROMOTED_TOP_LEVEL_SETTINGS = {
    "auto_pass_profile",
    "escalation_patterns",
    "fuzzy_threshold",
    "openai_api_key",
    "openai_api_key_entry",
    "openai_model_high",
    "openai_model_low",
    "openai_model_mid",
    "total_escalation_threshold",
}


def _repo_relative_path(value: str | Path) -> Path:
    """Resolve a configured path relative to the repository root."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (_REPO_ROOT / path).resolve()


def _intake_receipt_files(intake_dir: Path) -> list[Path]:
    """Return supported receipt files waiting in the intake folder."""
    from intake.media import iter_supported_receipt_files

    return iter_supported_receipt_files(intake_dir)


def _tracked_receipt_files(receipt_dir: Path) -> list[Path]:
    """Return supported tracked receipt files outside intake/exhaust."""
    from intake.media import rglob_supported_receipt_files

    return [
        path
        for path in rglob_supported_receipt_files(receipt_dir)
        if path.relative_to(receipt_dir).parts[0] not in ("intake", "exhaust")
        and len(path.relative_to(receipt_dir).parts) >= 2
    ]


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


def _promote_misnested_top_level_settings(settings: dict) -> None:
    """Lift known top-level settings out of nested TOML tables.

    Users sometimes place settings after a table header like ``[web]`` while
    intending them to remain top-level. TOML scopes those keys into the active
    table, so promote the known intake-wide keys back to top-level before the
    rest of settings resolution runs.
    """
    for value in list(settings.values()):
        if not isinstance(value, dict):
            continue
        for key in tuple(value.keys()):
            if key not in _PROMOTED_TOP_LEVEL_SETTINGS:
                continue
            settings.setdefault(key, value.pop(key))


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
    repo_root = str(_REPO_ROOT).replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'/bin/bash -lc \'export PYTHONPATH="{repo_root}${{PYTHONPATH:+:$PYTHONPATH}}"; '
        f'until {python_q} -c "import intake.cli" >/dev/null 2>&1; do '
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


def _build_web_manifest_text(*, python_executable: str) -> str:
    """Render the tracked clockwork manifest for intake-web.service."""
    exec_start = _build_wait_exec_start(
        python_executable=python_executable,
        ready_message="intake web: waiting for intake module path to become available",
        command="serve",
    )
    return _render_clockwork_template(
        "intake-web.toml.template",
        {"__EXEC_START__": _toml_string(exec_start)},
    )


def _load_settings() -> dict:
    settings_path = _SETTINGS_PATH
    if not settings_path.exists():
        return {}
    import toml

    settings = toml.load(settings_path)
    _promote_misnested_top_level_settings(settings)
    _normalise_settings_paths(settings)

    # Resolve the optional OpenAI API key through auto-pass when configured.
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


def _sync_receipt_sidecar(receipt_root: Path, row: dict) -> Path | None:
    """Patch the companion sidecar to match the current DB row."""
    from intake.sidecar import read_sidecar, sidecar_path

    receipt_dir_rel = row.get("receipt_dir") or ""
    if not receipt_dir_rel:
        return None

    artifact = _receipt_artifact_for_row(receipt_root, row)
    if artifact is None or not artifact.exists() or artifact.is_dir():
        return None

    sc_path = sidecar_path(artifact)
    payload = read_sidecar(artifact) if sc_path.exists() else {}
    payload.update(
        {
            "filename": row["filename"],
            "scan_date": row["scan_date"],
            "merchant": row["merchant"],
            "total": row["total"],
            "category": row["category"],
            "receipt_dir": row["receipt_dir"],
            "file_hash": row.get("file_hash"),
            "processed_by": row["processed_by"],
            "purchaser": row.get("purchaser"),
            "card_last4": row.get("card_last4"),
            "items": row.get("items"),
            "certifications": row.get("certifications"),
            "staff": row.get("staff"),
            "processed_at": row["processed_at"],
        }
    )
    sc_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return sc_path


def _receipt_artifact_for_row(receipt_root: Path, row: dict) -> Path | None:
    """Return the concrete receipt artifact path for a DB row, if inferable."""
    receipt_dir_rel = str(row.get("receipt_dir") or "")
    if not receipt_dir_rel:
        return None

    artifact = receipt_root / receipt_dir_rel
    filename = str(row.get("filename") or "")
    if artifact.exists() and artifact.is_dir() and filename:
        return artifact / filename
    return artifact


def _move_receipt_artifacts(
    receipt_root: Path,
    *,
    current_relpath: str,
    new_relpath: str,
) -> str:
    """Move or rename a receipt PDF and sidecar to a new relative path."""
    from intake.sidecar import ai_debug_path, ocr_debug_path, sidecar_path

    current_pdf = receipt_root / current_relpath
    if not current_pdf.exists():
        raise FileNotFoundError(f"receipt file not found: {current_pdf}")

    if new_relpath == current_relpath:
        return current_relpath

    new_pdf = receipt_root / new_relpath
    new_pdf.parent.mkdir(parents=True, exist_ok=True)
    if new_pdf.exists():
        raise FileExistsError(f"destination receipt already exists: {new_pdf}")

    current_sc = sidecar_path(current_pdf)
    new_sc = sidecar_path(new_pdf)
    current_ocr = ocr_debug_path(current_pdf)
    new_ocr = ocr_debug_path(new_pdf)
    current_ai = ai_debug_path(current_pdf)
    new_ai = ai_debug_path(new_pdf)

    shutil.move(str(current_pdf), str(new_pdf))
    if current_sc.exists():
        shutil.move(str(current_sc), str(new_sc))
    if current_ocr.exists():
        shutil.move(str(current_ocr), str(new_ocr))
    if current_ai.exists():
        shutil.move(str(current_ai), str(new_ai))
    return new_relpath


def _relocate_receipt_artifacts(
    receipt_root: Path,
    *,
    current_relpath: str,
    category: str,
    filename: str,
) -> str:
    """Move a receipt PDF and sidecar into a different category folder."""
    return _move_receipt_artifacts(
        receipt_root,
        current_relpath=current_relpath,
        new_relpath=str(Path(category) / filename),
    )


def _relocate_email_receipt_artifact(
    receipt_root: Path,
    *,
    current_relpath: str,
    new_category: str,
    filename: str,
    raw_text: str | None,
) -> str:
    """Move or create an inline email artifact for an email receipt row."""
    from intake.sidecar import ai_debug_path, ocr_debug_path, sidecar_path

    new_relpath = str(Path("email") / new_category)
    current_dir = receipt_root / current_relpath
    current_artifact = current_dir if current_dir.suffix else current_dir / filename
    new_artifact = receipt_root / new_relpath / filename

    if current_artifact.exists() and current_artifact.resolve() != new_artifact.resolve():
        new_artifact.parent.mkdir(parents=True, exist_ok=True)
        if new_artifact.exists():
            raise FileExistsError(f"destination receipt already exists: {new_artifact}")

        current_sc = sidecar_path(current_artifact)
        new_sc = sidecar_path(new_artifact)
        current_ocr = ocr_debug_path(current_artifact)
        new_ocr = ocr_debug_path(new_artifact)
        current_ai = ai_debug_path(current_artifact)
        new_ai = ai_debug_path(new_artifact)

        shutil.move(str(current_artifact), str(new_artifact))
        if current_sc.exists():
            shutil.move(str(current_sc), str(new_sc))
        if current_ocr.exists():
            shutil.move(str(current_ocr), str(new_ocr))
        if current_ai.exists():
            shutil.move(str(current_ai), str(new_ai))
    elif not new_artifact.exists() and raw_text:
        new_artifact.parent.mkdir(parents=True, exist_ok=True)
        new_artifact.write_text(raw_text, encoding="utf-8")

    return new_relpath


def _canonical_receipt_destination(
    dest_dir: Path,
    receipt,
    *,
    source_path: Path,
    reserved_paths: set[Path] | None = None,
) -> Path:
    """Return the canonical destination path for a receipt artifact."""
    from intake.naming import allocate_canonical_path, infer_artifact_datetime

    preferred_dt = infer_artifact_datetime(
        filename=source_path.name,
        path=source_path,
        default_date=receipt.receipt_date or receipt.scan_date,
        default_time=receipt.receipt_time,
    )
    return allocate_canonical_path(
        dest_dir,
        base_dt=preferred_dt,
        suffix=source_path.suffix or ".pdf",
        existing_path=source_path,
        reserved_paths=reserved_paths,
    )


def _canonical_document_destination(
    dest_dir: Path,
    *,
    source_path: Path,
    document_date,
    reserved_paths: set[Path] | None = None,
) -> Path:
    """Return the canonical destination path for any document artifact (letter, card)."""
    from intake.naming import allocate_canonical_path, infer_artifact_datetime

    preferred_dt = infer_artifact_datetime(
        filename=source_path.name,
        path=source_path,
        default_date=document_date,
        default_time=None,
    )
    return allocate_canonical_path(
        dest_dir,
        base_dt=preferred_dt,
        suffix=source_path.suffix or ".pdf",
        existing_path=source_path,
        reserved_paths=reserved_paths,
    )


def _canonical_existing_receipt_path(
    pdf: Path,
    *,
    scan_date: str | None = None,
    raw_text: str | None = None,
    reserved_paths: set[Path] | None = None,
) -> Path:
    """Return the canonical on-disk path for an existing receipt file."""
    return _canonical_receipt_artifact_path(
        pdf.parent,
        source_path=pdf,
        scan_date=scan_date,
        raw_text=raw_text,
        reserved_paths=reserved_paths,
    )


def _canonical_receipt_artifact_path(
    dest_dir: Path,
    *,
    source_path: Path,
    scan_date: str | None = None,
    raw_text: str | None = None,
    reserved_paths: set[Path] | None = None,
) -> Path:
    """Return the canonical path for an existing receipt artifact under *dest_dir*."""
    from datetime import date

    from intake.naming import allocate_canonical_path, infer_artifact_datetime, parse_name_date_time
    from intake.parse import _parse_receipt_datetime

    default_date = None
    if scan_date:
        try:
            default_date = date.fromisoformat(scan_date)
        except ValueError:
            default_date = None
    name_date, _name_time = parse_name_date_time(source_path.name)

    receipt_date = receipt_time = None
    if raw_text:
        receipt_date, receipt_time = _parse_receipt_datetime(raw_text)

    preferred_dt = infer_artifact_datetime(
        filename=source_path.name,
        path=source_path,
        default_date=receipt_date or name_date or default_date,
        default_time=receipt_time,
    )
    return allocate_canonical_path(
        dest_dir,
        base_dt=preferred_dt,
        suffix=source_path.suffix or ".pdf",
        existing_path=source_path if source_path.parent == dest_dir else None,
        reserved_paths=reserved_paths,
    )


def _file_hash(path: Path) -> str:
    """Return the SHA-256 fingerprint for *path*."""
    from intake.fingerprint import sha256_file

    return sha256_file(path)


def _backfill_purchaser_from_card(conn, receipt):
    """Fill a missing purchaser from the most recent known holder of the same card."""
    if receipt.purchaser or not receipt.card_last4:
        return receipt

    from dataclasses import replace

    from intake.db import infer_purchaser_from_card_last4

    inferred = infer_purchaser_from_card_last4(conn, receipt.card_last4)
    if not inferred:
        return receipt
    return replace(receipt, purchaser=inferred)


def _backfill_row_purchaser_from_card(conn, row: dict) -> dict:
    """Fill a missing purchaser on a serialized row using ``card_last4`` history."""
    if row.get("purchaser") or not row.get("card_last4"):
        return row

    from intake.db import infer_purchaser_from_card_last4

    inferred = infer_purchaser_from_card_last4(conn, row.get("card_last4"))
    if not inferred:
        return row

    updated = dict(row)
    updated["purchaser"] = inferred
    return updated


def _receipt_row_from_receipt(
    receipt,
    *,
    filename: str,
    category: str,
    receipt_dir: str,
    file_hash: str | None,
    processed_by: str,
    tier: str,
) -> dict:
    """Serialize a Receipt into the DB/sidecar row shape."""
    from intake.certification import receipt_certifications_to_dicts
    from intake.parse import receipt_items_to_dicts
    from intake.staff import receipt_staff_to_dicts

    return {
        "filename": filename,
        "scan_date": receipt.scan_date.isoformat(),
        "merchant": receipt.merchant,
        "total": receipt.total,
        "category": category,
        "receipt_dir": receipt_dir,
        "file_hash": file_hash,
        "raw_text": receipt.raw_text,
        "processed_by": processed_by,
        "purchaser": receipt.purchaser,
        "card_last4": receipt.card_last4,
        "currency": receipt.currency,
        "items": receipt_items_to_dicts(receipt.items),
        "certifications": receipt_certifications_to_dicts(receipt.certifications),
        "staff": receipt_staff_to_dicts(receipt.staff),
        "tier": tier,
    }


def _letter_row_from_letter(
    letter,
    *,
    filename: str,
    category: str,
    receipt_dir: str,
    file_hash: str | None,
    processed_by: str,
) -> dict:
    """Serialize a Letter into the DB/sidecar row shape."""
    return {
        "filename": filename,
        "scan_date": letter.scan_date.isoformat(),
        "letter_date": letter.letter_date.isoformat() if letter.letter_date else None,
        "sender": letter.sender,
        "recipient": letter.recipient,
        "subject": letter.subject,
        "salutation": letter.salutation,
        "body_excerpt": letter.body_excerpt,
        "category": category,
        "receipt_dir": receipt_dir,
        "file_hash": file_hash,
        "raw_text": letter.raw_text,
        "processed_by": processed_by,
    }


def _card_row_from_card(
    card,
    *,
    filename: str,
    category: str,
    receipt_dir: str,
    file_hash: str | None,
    processed_by: str,
) -> dict:
    """Serialize a Card into the DB/sidecar row shape."""
    return {
        "filename": filename,
        "scan_date": card.scan_date.isoformat(),
        "card_date": card.card_date.isoformat() if card.card_date else None,
        "occasion": card.occasion,
        "sender": card.sender,
        "recipient": card.recipient,
        "message_excerpt": card.message_excerpt,
        "category": category,
        "receipt_dir": receipt_dir,
        "file_hash": file_hash,
        "raw_text": card.raw_text,
        "processed_by": processed_by,
    }


def _payment_row_from_payment(
    payment,
    *,
    filename: str,
    category: str,
    receipt_dir: str,
    file_hash: str | None,
    processed_by: str,
) -> dict:
    """Serialize a Payment into the DB/sidecar row shape."""
    return {
        "filename": filename,
        "scan_date": payment.scan_date.isoformat(),
        "payment_date": payment.payment_date.isoformat() if payment.payment_date else None,
        "amount": payment.amount,
        "account_name": payment.account_name,
        "account_last4": payment.account_last4,
        "payment_type": payment.payment_type,
        "confirmation_number": payment.confirmation_number,
        "payer": payment.payer,
        "category": category,
        "receipt_dir": receipt_dir,
        "file_hash": file_hash,
        "raw_text": payment.raw_text,
        "processed_by": processed_by,
    }


def _receipt_event_from_row(row: dict, *, tier: str) -> dict:
    """Build a downstream personal-finance event from a serialized row."""
    return {
        "filename": row["filename"],
        "receipt_dir": row["receipt_dir"],
        "scan_date": row["scan_date"],
        "receipt_date": None,
        "receipt_time": None,
        "merchant": row.get("merchant"),
        "total": row.get("total"),
        "category": row["category"],
        "processed_by": row.get("processed_by") or "tesseract",
        "tier": tier,
        "purchaser": row.get("purchaser"),
        "card_last4": row.get("card_last4"),
        "items": row.get("items") or [],
        "certifications": row.get("certifications") or [],
        "staff": row.get("staff") or [],
    }


def _debug_ai_attempts(receipt) -> list[dict]:
    attempts = getattr(receipt, "_debug_ai_attempts", [])
    return attempts if isinstance(attempts, list) else []


def _write_receipt_debug_artifacts(pdf: Path, receipt, *, processed_by: str, tier: str) -> None:
    from intake.sidecar import write_debug_artifacts

    write_debug_artifacts(
        pdf,
        raw_text=receipt.raw_text,
        processed_by=processed_by,
        tier=tier,
        ai_attempts=_debug_ai_attempts(receipt),
    )


def _preferred_live_photo_still(files: list[Path]) -> Path | None:
    """Return the preferred still image from a same-stem live-photo group."""
    from intake.media import is_image_receipt

    stills = [path for path in files if is_image_receipt(path)]
    if not stills:
        return None
    preferred_suffixes = {
        ".jpg": 0,
        ".jpeg": 1,
        ".png": 2,
        ".tif": 3,
        ".tiff": 4,
        ".bmp": 5,
        ".webp": 6,
    }
    return sorted(
        stills,
        key=lambda path: (preferred_suffixes.get(path.suffix.lower(), 99), path.name.lower()),
    )[0]


def _exhaust_live_photo_duplicate(
    receipt_root: Path,
    conn,
    *,
    duplicate: Path,
    keeper: Path,
    reserved_paths: set[Path] | None = None,
    source: str = "ingest-live-photo-duplicate",
) -> Path:
    """Move a same-stem live-photo companion file to exhaust with audit history."""
    from intake.db import record_receipt_update
    from intake.parse import _parse_scan_date
    from intake.sidecar import write_sidecar

    current_rel = str(duplicate.relative_to(receipt_root))
    dest_dir = receipt_root / "exhaust" / "duplicates" / "live-photo"
    dest = _canonical_receipt_artifact_path(
        dest_dir,
        source_path=duplicate,
        scan_date=None,
        raw_text=None,
        reserved_paths=reserved_paths,
    )
    dest_rel = str(dest.relative_to(receipt_root))
    duplicate_hash = _file_hash(duplicate)
    _move_receipt_artifacts(
        receipt_root,
        current_relpath=current_rel,
        new_relpath=dest_rel,
    )

    duplicate_scan_date = _parse_scan_date(dest.name, dest).isoformat()
    write_sidecar(
        dest,
        {
            "filename": dest.name,
            "scan_date": duplicate_scan_date,
            "merchant": None,
            "total": None,
            "category": "exhaust/duplicates/live-photo",
            "receipt_dir": dest_rel,
            "file_hash": duplicate_hash,
            "processed_by": "live-photo-skip",
            "purchaser": None,
            "card_last4": None,
            "items": [],
            "certifications": [],
            "status": "live-photo-duplicate-exhausted",
            "duplicate_of": keeper.name,
        },
    )

    changes = {
        "status": {"old": "intake-pending", "new": "live-photo-duplicate-exhausted"},
        "receipt_dir": {"old": current_rel, "new": dest_rel},
        "file_hash": {"old": None, "new": duplicate_hash},
        "duplicate_of": {"old": None, "new": keeper.name},
    }
    if dest.name != duplicate.name:
        changes["filename"] = {"old": duplicate.name, "new": dest.name}
    record_receipt_update(
        conn,
        dest.name,
        source=source,
        note=f"same-stem live-photo pair detected; kept still image {keeper.name}",
        changes=changes,
    )
    return dest


def _collapse_intake_live_photo_duplicates(
    receipt_root: Path,
    conn,
    receipt_files: list[Path],
    *,
    reserved_paths: set[Path] | None = None,
) -> tuple[list[Path], list[str]]:
    """Exhaust same-stem image/video live-photo duplicates before OCR."""
    from collections import defaultdict

    from intake.media import is_image_receipt, is_video_receipt

    stem_groups: dict[str, list[Path]] = defaultdict(list)
    for path in receipt_files:
        stem_groups[path.stem.lower()].append(path)

    kept: list[Path] = []
    messages: list[str] = []
    for paths in stem_groups.values():
        keeper = _preferred_live_photo_still(paths)
        if keeper is not None and any(is_video_receipt(path) for path in paths):
            kept.append(keeper)
            for duplicate in sorted(
                (
                    path
                    for path in paths
                    if path != keeper and is_video_receipt(path) and is_image_receipt(keeper)
                ),
                key=lambda path: path.name.lower(),
            ):
                dest = _exhaust_live_photo_duplicate(
                    receipt_root,
                    conn,
                    duplicate=duplicate,
                    keeper=keeper,
                    reserved_paths=reserved_paths,
                )
                messages.append(
                    f"  [live-photo] kept {keeper.name}, exhausted {duplicate.name} -> {dest.relative_to(receipt_root)}"
                )
            for other in paths:
                if other == keeper or is_video_receipt(other):
                    continue
                kept.append(other)
            continue
        kept.extend(paths)

    return sorted(set(kept)), messages


def _exhaust_hash_duplicate(
    receipt_root: Path,
    conn,
    *,
    pdf: Path,
    existing_row: dict,
    file_hash: str,
    reserved_paths: set[Path] | None = None,
    source: str = "ingest-hash-duplicate",
) -> Path:
    """Move an exact duplicate PDF to exhaust and record an audit event."""
    from intake.db import record_receipt_update
    from intake.parse import _parse_scan_date
    from intake.sidecar import write_sidecar

    current_rel = str(pdf.relative_to(receipt_root))
    category = str(existing_row.get("category") or "").strip().strip("/") or "general"
    dest_dir = receipt_root / "exhaust" / "duplicates" / category
    dest = _canonical_receipt_artifact_path(
        dest_dir,
        source_path=pdf,
        scan_date=None,
        raw_text=None,
        reserved_paths=reserved_paths,
    )
    dest_rel = str(dest.relative_to(receipt_root))
    _move_receipt_artifacts(
        receipt_root,
        current_relpath=current_rel,
        new_relpath=dest_rel,
    )

    duplicate_scan_date = _parse_scan_date(dest.name, dest).isoformat()
    write_sidecar(
        dest,
        {
            "filename": dest.name,
            "scan_date": duplicate_scan_date,
            "merchant": existing_row.get("merchant"),
            "total": existing_row.get("total"),
            "category": category,
            "receipt_dir": dest_rel,
            "file_hash": file_hash,
            "processed_by": existing_row.get("processed_by") or "tesseract",
            "purchaser": existing_row.get("purchaser"),
            "card_last4": existing_row.get("card_last4"),
            "items": existing_row.get("items"),
            "certifications": existing_row.get("certifications"),
            "status": "duplicate-exhausted",
            "duplicate_of": existing_row.get("filename"),
        },
    )

    changes = {
        "status": {"old": "intake-pending", "new": "duplicate-exhausted"},
        "receipt_dir": {"old": current_rel, "new": dest_rel},
        "file_hash": {"old": None, "new": file_hash},
        "duplicate_of": {"old": None, "new": existing_row.get("filename")},
    }
    if dest.name != pdf.name:
        changes["filename"] = {"old": pdf.name, "new": dest.name}
    record_receipt_update(
        conn,
        dest.name,
        source=source,
        note=f"file hash matched existing receipt {existing_row.get('filename')}",
        changes=changes,
    )
    return dest


def _hydrate_missing_file_hashes(
    conn,
    rows: list[dict],
    *,
    receipt_root: Path | None,
) -> list[dict]:
    """Populate missing DB file hashes from local PDFs when available."""
    if receipt_root is None:
        return rows

    from intake.db import update_receipt_fields

    hydrated: list[dict] = []
    for row in rows:
        if row.get("file_hash"):
            hydrated.append(row)
            continue

        rel = row.get("receipt_dir") or ""
        if not rel or rel.startswith("email/"):
            hydrated.append(row)
            continue

        pdf = receipt_root / rel
        if not pdf.exists():
            hydrated.append(row)
            continue

        file_hash = _file_hash(pdf)
        updated_row, _changes = update_receipt_fields(
            conn,
            row["filename"],
            {"file_hash": file_hash},
            source="file-hash-backfill",
            note="computed missing file hash during duplicate detection",
        )
        _sync_receipt_sidecar(receipt_root, updated_row)
        hydrated.append(dict(updated_row))
    return hydrated


def _get_extract_kwargs(settings: dict) -> dict:
    """Build keyword arguments for extract_receipt() from settings."""
    from intake.extract import DEFAULT_TOTAL_ESCALATION_THRESHOLD

    total_escalation_threshold = settings.get(
        "total_escalation_threshold", DEFAULT_TOTAL_ESCALATION_THRESHOLD
    )
    return {
        "openai_api_key": settings.get("openai_api_key") or None,
        "openai_model_low": (
            settings.get("openai_model_low") or settings.get("openai_model") or "gpt-4o-mini"
        ),
        "openai_model_mid": settings.get("openai_model_mid") or "gpt-4o",
        "openai_model_high": settings.get("openai_model_high") or "gpt-4.1",
        "escalation_patterns": settings.get("escalation_patterns") or None,
        "fuzzy_threshold": float(settings.get("fuzzy_threshold", 0.82)),
        "total_escalation_threshold": (
            None if total_escalation_threshold is None else float(total_escalation_threshold)
        ),
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
    from intake.db import (
        get_card_by_file_hash,
        get_letter_by_file_hash,
        get_payment_by_file_hash,
        get_receipt_by_file_hash,
        init_db,
        upsert_card,
        upsert_letter,
        upsert_payment,
        upsert_receipt,
    )
    from intake.document_type import classify_document_type
    from intake.extract import extract_card, extract_letter, extract_payment, extract_receipt
    from intake.ocr import extract_text
    from intake.parse_card import classify_card
    from intake.parse_letter import classify_letter
    from intake.parse_payment import classify_payment
    from intake.personal_finance import build_receipt_event
    from intake.sidecar import write_sidecar

    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    intake_dir = receipt_dir / "intake"

    if not intake_dir.exists():
        print(f"ERROR: intake directory not found: {intake_dir}", file=sys.stderr)
        sys.exit(1)

    receipt_files = _intake_receipt_files(intake_dir)
    if not receipt_files:
        print("No files found in intake.")
        return

    rules = load_rules()
    merchant_aliases = load_merchant_aliases()
    conn = init_db(db_path)
    pf_events: list[dict] = []
    reserved_paths: set[Path] = set()
    receipt_files, live_photo_messages = _collapse_intake_live_photo_duplicates(
        receipt_dir,
        conn,
        receipt_files,
        reserved_paths=reserved_paths,
    )
    for message in live_photo_messages:
        print(message)

    for pdf in receipt_files:
        print(f"  Processing: {pdf.name}")
        if args.dry_run:
            print("    [dry-run] skipping OCR and move")
            continue

        try:
            file_hash = _file_hash(pdf)
            existing = (
                get_receipt_by_file_hash(conn, file_hash)
                or get_letter_by_file_hash(conn, file_hash)
                or get_card_by_file_hash(conn, file_hash)
                or get_payment_by_file_hash(conn, file_hash)
            )
            if existing is not None:
                dest = _exhaust_hash_duplicate(
                    receipt_dir,
                    conn,
                    pdf=pdf,
                    existing_row=existing,
                    file_hash=file_hash,
                    reserved_paths=reserved_paths,
                )
                print(
                    "    -> duplicate by file hash, moved to "
                    f"{dest.relative_to(receipt_dir)} (matches {existing['receipt_dir']})"
                )
                continue

            raw_text = extract_text(pdf)
            doc_type = classify_document_type(raw_text, pdf.name)

            if doc_type == "letter":
                letter, model = extract_letter(
                    pdf,
                    raw_text,
                    openai_api_key=settings.get("openai_api_key") or None,
                    openai_model=settings.get("openai_model_low") or "gpt-4o-mini",
                )
                category = classify_letter(letter)
                dest_dir = receipt_dir / category
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _canonical_document_destination(
                    dest_dir,
                    source_path=pdf,
                    document_date=letter.letter_date or letter.scan_date,
                    reserved_paths=reserved_paths,
                )
                shutil.move(str(pdf), dest)
                renamed = f" (renamed {pdf.name} -> {dest.name})" if dest.name != pdf.name else ""
                row = _letter_row_from_letter(
                    letter,
                    filename=dest.name,
                    category=category,
                    receipt_dir=str(dest.relative_to(receipt_dir)),
                    file_hash=file_hash,
                    processed_by=model,
                )
                upsert_letter(conn, row)
                write_sidecar(
                    dest,
                    {
                        **{k: v for k, v in row.items() if k != "raw_text"},
                        "document_type": "letter",
                        "tier": "ok",
                    },
                )
                print(
                    f"    -> {category}/{dest.name} [letter]"
                    f" sender={letter.sender!r} model={model}{renamed}"
                )
                continue

            if doc_type == "card":
                card, model = extract_card(
                    pdf,
                    raw_text,
                    openai_api_key=settings.get("openai_api_key") or None,
                    openai_model=settings.get("openai_model_low") or "gpt-4o-mini",
                )
                category = classify_card(card)
                dest_dir = receipt_dir / category
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _canonical_document_destination(
                    dest_dir,
                    source_path=pdf,
                    document_date=card.card_date or card.scan_date,
                    reserved_paths=reserved_paths,
                )
                shutil.move(str(pdf), dest)
                renamed = f" (renamed {pdf.name} -> {dest.name})" if dest.name != pdf.name else ""
                row = _card_row_from_card(
                    card,
                    filename=dest.name,
                    category=category,
                    receipt_dir=str(dest.relative_to(receipt_dir)),
                    file_hash=file_hash,
                    processed_by=model,
                )
                upsert_card(conn, row)
                write_sidecar(
                    dest,
                    {
                        **{k: v for k, v in row.items() if k != "raw_text"},
                        "document_type": "card",
                        "tier": "ok",
                    },
                )
                print(
                    f"    -> {category}/{dest.name} [card]"
                    f" occasion={card.occasion!r} model={model}{renamed}"
                )
                continue

            if doc_type == "payment":
                payment, model = extract_payment(
                    pdf,
                    raw_text,
                    openai_api_key=settings.get("openai_api_key") or None,
                    openai_model=settings.get("openai_model_low") or "gpt-4o-mini",
                )
                category = classify_payment(payment)
                dest_dir = receipt_dir / category
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _canonical_document_destination(
                    dest_dir,
                    source_path=pdf,
                    document_date=payment.payment_date or payment.scan_date,
                    reserved_paths=reserved_paths,
                )
                shutil.move(str(pdf), dest)
                renamed = f" (renamed {pdf.name} -> {dest.name})" if dest.name != pdf.name else ""
                row = _payment_row_from_payment(
                    payment,
                    filename=dest.name,
                    category=category,
                    receipt_dir=str(dest.relative_to(receipt_dir)),
                    file_hash=file_hash,
                    processed_by=model,
                )
                upsert_payment(conn, row)
                write_sidecar(
                    dest,
                    {
                        **{k: v for k, v in row.items() if k != "raw_text"},
                        "document_type": "payment",
                        "tier": "ok",
                    },
                )
                print(
                    f"    -> {category}/{dest.name} [payment]"
                    f" amount={payment.amount!r} account={payment.account_name!r}"
                    f" model={model}{renamed}"
                )
                continue

            # receipt (or unknown — fall through to receipt pipeline)
            receipt, model, tier = extract_receipt(
                pdf,
                raw_text=raw_text,
                **_get_extract_kwargs(settings),
                fuzzy_rules=rules,
                merchant_aliases=merchant_aliases,
            )
            receipt = _backfill_purchaser_from_card(conn, receipt)
            category = classify(receipt.merchant, rules, receipt.raw_text)
            dest_dir = receipt_dir / category
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = _canonical_receipt_destination(
                dest_dir,
                receipt,
                source_path=pdf,
                reserved_paths=reserved_paths,
            )

            shutil.move(str(pdf), dest)
            renamed = f" (renamed {pdf.name} -> {dest.name})" if dest.name != pdf.name else ""

            row = _receipt_row_from_receipt(
                receipt,
                filename=dest.name,
                category=category,
                receipt_dir=str(dest.relative_to(receipt_dir)),
                file_hash=file_hash,
                processed_by=model,
                tier=tier,
            )
            upsert_receipt(conn, row)
            write_sidecar(dest, {k: v for k, v in row.items() if k != "raw_text"})
            _write_receipt_debug_artifacts(dest, receipt, processed_by=model, tier=tier)
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
    report_links_cfg = settings.get("report_links", {})
    filebrowser_base_url = "https://files.snowbridge.internal"
    filebrowser_receipt_root = "receipt"
    if isinstance(report_links_cfg, dict):
        if "filebrowser_base_url" in report_links_cfg:
            configured = str(report_links_cfg.get("filebrowser_base_url") or "").strip()
            filebrowser_base_url = configured or None
        if report_links_cfg.get("filebrowser_receipt_root"):
            filebrowser_receipt_root = str(report_links_cfg["filebrowser_receipt_root"]).strip()

    conn = init_db(db_path)
    receipt_root = Path(settings["receipt_dir"]) if settings.get("receipt_dir") else None
    written = generate_reports(
        conn,
        output_dir,
        fmt=fmt,
        receipt_root=receipt_root,
        filebrowser_base_url=filebrowser_base_url,
        filebrowser_receipt_root=filebrowser_receipt_root,
    )
    conn.close()

    for p in written:
        print(f"  Report written: {p}")


def _web_host(settings: dict, args_value: str | None) -> str:
    web_cfg = settings.get("web", {})
    if args_value:
        return args_value
    if isinstance(web_cfg, dict) and web_cfg.get("host"):
        return str(web_cfg["host"])
    return "127.0.0.1"


def _web_port(settings: dict, args_value: int | None) -> int:
    web_cfg = settings.get("web", {})
    if args_value is not None:
        return int(args_value)
    if isinstance(web_cfg, dict) and web_cfg.get("port") is not None:
        return int(web_cfg["port"])
    return 5200


def _cmd_serve(args: argparse.Namespace, settings: dict) -> None:
    """Serve the generated report directory over localhost HTTP."""
    if getattr(args, "install", False):
        _install_web_unit()
        return

    from intake.web import serve_reports

    output_dir = Path(args.output_dir or settings.get("reports_dir") or _DEFAULT_REPORTS_DIR)
    serve_reports(
        output_dir,
        host=_web_host(settings, getattr(args, "host", None)),
        port=_web_port(settings, getattr(args, "port", None)),
    )


def _cmd_backfill(args: argparse.Namespace, settings: dict) -> None:
    from datetime import date

    from intake.classify import load_merchant_aliases, load_rules
    from intake.db import get_receipt, init_db, update_receipt_fields, upsert_receipt
    from intake.extract import extract_receipt
    from intake.naming import allocate_canonical_path, infer_artifact_datetime
    from intake.personal_finance import build_receipt_event
    from intake.sidecar import read_sidecar, write_sidecar

    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    rules = load_rules()
    merchant_aliases = load_merchant_aliases()
    extract_kwargs = _get_extract_kwargs(settings)

    all_pdfs = _tracked_receipt_files(receipt_dir)

    if not all_pdfs:
        print("No receipts found in category folders.")
        return

    # Open DB early so we can detect sidecared receipts that are absent from the DB.
    conn = init_db(db_path)
    db_receipt_dirs: set[str] = {
        row[0]
        for row in conn.execute("SELECT receipt_dir FROM receipts WHERE receipt_dir IS NOT NULL")
    }

    pending: list[Path] = []
    sidecar_only: list[Path] = []  # sidecar present + up-to-date, but not in DB
    skipped = 0
    for pdf in all_pdfs:
        sc = read_sidecar(pdf)
        rel = str(pdf.relative_to(receipt_dir))
        if sc is not None and _skip_sidecar(sc, settings, pdf_path=pdf):
            if rel not in db_receipt_dirs:
                sidecar_only.append(pdf)
            else:
                skipped += 1
            continue
        pending.append(pdf)

    print(
        f"Found {len(all_pdfs)} receipt(s); {skipped} up-to-date, "
        f"{len(sidecar_only)} sidecar-only (not in DB), {len(pending)} pending."
    )

    if not pending and not sidecar_only:
        print("Nothing to backfill.")
        return

    if args.dry_run:
        for pdf in sidecar_only:
            rel = pdf.relative_to(receipt_dir)
            sc = read_sidecar(pdf)
            print(f"  [dry-run] {rel}  (sidecar-only, would upsert from sidecar)")
        for pdf in pending:
            rel = pdf.relative_to(receipt_dir)
            sc = read_sidecar(pdf)
            prior_model = sc.get("processed_by", "none") if sc else "none"
            prior_tier = sc.get("tier", "—") if sc else "—"
            print(f"  [dry-run] {rel}  (prior: {prior_model}/{prior_tier})")
        return

    from intake.merchant import consolidate_merchant

    ok = failed = 0
    pf_events: list[dict] = []
    reserved_paths: set[Path] = set()

    # ── Sidecar-only: upsert from existing sidecar without re-OCR ────────────
    for pdf in sidecar_only:
        sc = read_sidecar(pdf)
        if sc is None:
            continue
        scan_date_raw = str(sc.get("scan_date") or "")
        scan_date = None
        try:
            scan_date = date.fromisoformat(scan_date_raw) if scan_date_raw else None
        except ValueError:
            scan_date = None
        canonical_pdf = allocate_canonical_path(
            pdf.parent,
            base_dt=infer_artifact_datetime(
                filename=pdf.name,
                path=pdf,
                default_date=scan_date,
            ),
            suffix=pdf.suffix or ".pdf",
            existing_path=pdf,
            reserved_paths=reserved_paths,
        )
        if canonical_pdf != pdf:
            _move_receipt_artifacts(
                receipt_dir,
                current_relpath=str(pdf.relative_to(receipt_dir)),
                new_relpath=str(canonical_pdf.relative_to(receipt_dir)),
            )
            pdf = canonical_pdf

        rel = pdf.relative_to(receipt_dir)

        # Guard: if this filename already maps to a *different* receipt_dir in the
        # DB, the file is a same-name copy of a different receipt.  Upserting would
        # silently overwrite the existing row — skip and warn instead.
        existing = conn.execute(
            "SELECT receipt_dir FROM receipts WHERE filename = ?", (pdf.name,)
        ).fetchone()
        if existing and existing["receipt_dir"] != str(rel):
            print(
                f"  SKIP (sidecar-only): {rel}  "
                f"— filename already tracked as {existing['receipt_dir']!r}"
            )
            continue

        print(f"  Upserting (sidecar-only): {rel}")
        merchant = consolidate_merchant(sc.get("merchant") or "", merchant_aliases)
        row = {
            "filename": pdf.name,
            "scan_date": sc.get("scan_date", ""),
            "merchant": merchant,
            "total": sc.get("total"),
            "category": sc.get("category", "/".join(rel.parts[:-1])),
            "receipt_dir": str(rel),
            "file_hash": _file_hash(pdf),
            "raw_text": None,
            "processed_by": sc.get("processed_by", "tesseract"),
            "purchaser": sc.get("purchaser"),
            "card_last4": sc.get("card_last4"),
            "currency": sc.get("currency", "USD"),
            "items": sc.get("items"),
            "certifications": sc.get("certifications"),
            "staff": sc.get("staff"),
            "tier": sc.get("tier", "ok"),
        }
        row = _backfill_row_purchaser_from_card(conn, row)
        try:
            upsert_receipt(conn, row)
            write_sidecar(pdf, {k: v for k, v in row.items() if k != "raw_text"})
            print(f"    -> merchant={merchant!r}, total={row['total']}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR: {exc}", file=sys.stderr)
            failed += 1

    # ── Full re-extract pending receipts ─────────────────────────────────────

    for pdf in pending:
        rel = pdf.relative_to(receipt_dir)
        category = "/".join(rel.parts[:-1])
        original_filename = pdf.name
        print(f"  Backfilling: {rel}")
        try:
            receipt, model, tier = extract_receipt(
                pdf, **extract_kwargs, fuzzy_rules=rules, merchant_aliases=merchant_aliases
            )
            receipt = _backfill_purchaser_from_card(conn, receipt)
            dest = _canonical_receipt_destination(
                pdf.parent,
                receipt,
                source_path=pdf,
                reserved_paths=reserved_paths,
            )
            renamed_pdf = dest != pdf
            if dest != pdf:
                _move_receipt_artifacts(
                    receipt_dir,
                    current_relpath=str(rel),
                    new_relpath=str(dest.relative_to(receipt_dir)),
                )
                pdf = dest
                rel = pdf.relative_to(receipt_dir)
                category = "/".join(rel.parts[:-1])

            row = _receipt_row_from_receipt(
                receipt,
                filename=pdf.name,
                category=category,
                receipt_dir=str(rel),
                file_hash=_file_hash(pdf),
                processed_by=model,
                tier=tier,
            )
            existing_old = get_receipt(conn, original_filename)
            if renamed_pdf and existing_old is not None:
                db_updates = {k: v for k, v in row.items() if k != "tier"}
                update_receipt_fields(
                    conn,
                    original_filename,
                    db_updates,
                    source="backfill-rename",
                    note="canonicalized filename during backfill",
                )
            else:
                upsert_receipt(conn, row)
            write_sidecar(pdf, {k: v for k, v in row.items() if k != "raw_text"})
            _write_receipt_debug_artifacts(pdf, receipt, processed_by=model, tier=tier)
            if getattr(args, "notify", False):
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


def _cmd_itemize(args: argparse.Namespace, settings: dict) -> None:
    """Attempt an itemized breakdown for existing receipt rows."""
    from intake.classify import load_merchant_aliases, load_rules
    from intake.db import init_db, update_receipt_fields
    from intake.extract import extract_receipt
    from intake.parse import parse_receipt, receipt_items_to_dicts

    receipt_root = None
    if args.receipt_dir or settings.get("receipt_dir"):
        receipt_root = _require_receipt_dir(settings, args.receipt_dir)

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    conn = init_db(db_path)
    row_cursor = conn.execute("""
            SELECT filename, scan_date, merchant, total, category, receipt_dir, raw_text,
                   items_json, processed_by, purchaser, card_last4, processed_at
            FROM receipts
            ORDER BY scan_date DESC, processed_at DESC, filename DESC
            """)
    rows = [dict(row) for row in row_cursor.fetchall()]
    if args.limit is not None:
        rows = rows[: args.limit]

    rules = load_rules()
    merchant_aliases = load_merchant_aliases()

    updated = skipped = failed = 0
    itemized = 0
    line_items = 0
    pf_events: list[dict] = []

    for row in rows:
        if row.get("items_json") is not None and not args.force:
            skipped += 1
            continue

        receipt_dir_rel = str(row.get("receipt_dir") or "")
        pdf = None
        if (
            receipt_root is not None
            and receipt_dir_rel
            and not receipt_dir_rel.startswith("email/")
        ):
            candidate = receipt_root / receipt_dir_rel
            if candidate.exists():
                pdf = candidate

        try:
            receipt = None
            processed_by = row.get("processed_by") or "tesseract"
            tier = "stored-ocr"
            note_parts: list[str] = []

            raw_text = row.get("raw_text") or ""
            if raw_text:
                receipt = parse_receipt(row["filename"], raw_text, pdf_path=pdf)
                note_parts.append("stored OCR")

            if pdf is not None and (args.force or receipt is None or not receipt.items):
                reextracted, processed_by, tier = extract_receipt(
                    pdf,
                    **_get_extract_kwargs(settings),
                    fuzzy_rules=rules,
                    merchant_aliases=merchant_aliases,
                )
                if receipt is None or reextracted.items or not raw_text:
                    receipt = reextracted
                note_parts.append("PDF re-extraction")

            items = receipt_items_to_dicts(receipt.items) if receipt is not None else []
            items_label = str(len(items))
            if items:
                preview = ", ".join(
                    item["description"] for item in items[:3] if item.get("description")
                )
                if len(items) > 3:
                    preview += ", ..."
                items_label = f"{len(items)} [{preview}]"

            if args.dry_run:
                print(f"  [dry-run] {row['filename']}: {items_label}")
                continue

            note = (
                "attempted itemized breakdown via " + " + ".join(note_parts)
                if note_parts
                else "attempted itemized breakdown with no OCR text or local PDF available"
            )
            updated_row, changes = update_receipt_fields(
                conn,
                row["filename"],
                {"items": items},
                source="itemize-backfill",
                note=note,
            )
            if not changes:
                skipped += 1
                continue

            if receipt_root is not None:
                _sync_receipt_sidecar(receipt_root, updated_row)
            if pdf is not None and receipt is not None:
                _write_receipt_debug_artifacts(
                    pdf,
                    receipt,
                    processed_by=processed_by,
                    tier=f"itemize-{tier}",
                )

            if updated_row.get("items"):
                itemized += 1
                line_items += len(updated_row["items"])
            pf_events.append(_receipt_event_from_row(updated_row, tier=f"itemize-{tier}"))
            updated += 1
            print(f"  Itemized {row['filename']}: {items_label}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR itemizing {row['filename']}: {exc}", file=sys.stderr)
            failed += 1

    conn.close()
    _trigger_personal_finance(settings, pf_events, trigger="itemize")
    print(
        f"\nItemization complete: {updated} updated, {skipped} skipped, {failed} error(s), "
        f"{itemized} with line items, {line_items} total line items."
    )


def _cmd_certify(args: argparse.Namespace, settings: dict) -> None:
    """Attempt structured certification extraction for existing receipt rows."""
    from intake.certification import receipt_certifications_to_dicts
    from intake.classify import load_merchant_aliases, load_rules
    from intake.db import init_db, update_receipt_fields
    from intake.extract import extract_receipt
    from intake.parse import parse_receipt

    receipt_root = None
    if args.receipt_dir or settings.get("receipt_dir"):
        receipt_root = _require_receipt_dir(settings, args.receipt_dir)

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    conn = init_db(db_path)
    row_cursor = conn.execute("""
            SELECT filename, scan_date, merchant, total, category, receipt_dir, raw_text,
                   certifications_json, processed_by, purchaser, card_last4, processed_at
            FROM receipts
            ORDER BY scan_date DESC, processed_at DESC, filename DESC
            """)
    rows = [dict(row) for row in row_cursor.fetchall()]
    if args.limit is not None:
        rows = rows[: args.limit]

    rules = load_rules()
    merchant_aliases = load_merchant_aliases()

    updated = skipped = failed = 0
    certified = 0
    pf_events: list[dict] = []

    for row in rows:
        if row.get("certifications_json") is not None and not args.force:
            skipped += 1
            continue

        receipt_dir_rel = str(row.get("receipt_dir") or "")
        pdf = None
        if (
            receipt_root is not None
            and receipt_dir_rel
            and not receipt_dir_rel.startswith("email/")
        ):
            candidate = receipt_root / receipt_dir_rel
            if candidate.exists():
                pdf = candidate

        try:
            receipt = None
            processed_by = row.get("processed_by") or "tesseract"
            tier = "stored-ocr"
            note_parts: list[str] = []

            raw_text = row.get("raw_text") or ""
            if raw_text:
                receipt = parse_receipt(row["filename"], raw_text, pdf_path=pdf)
                note_parts.append("stored OCR")

            if pdf is not None and (receipt is None or (args.force and not receipt.certifications)):
                reextracted, processed_by, tier = extract_receipt(
                    pdf,
                    **_get_extract_kwargs(settings),
                    fuzzy_rules=rules,
                    merchant_aliases=merchant_aliases,
                )
                if receipt is None or reextracted.certifications or not raw_text:
                    receipt = reextracted
                note_parts.append("PDF re-extraction")

            certifications = (
                receipt_certifications_to_dicts(receipt.certifications)
                if receipt is not None
                else []
            )
            cert_labels = ", ".join(
                cert["label"] for cert in certifications if cert.get("label")
            ) or str(len(certifications))

            if args.dry_run:
                print(f"  [dry-run] {row['filename']}: {cert_labels}")
                continue

            note = (
                "attempted structured certification extraction via " + " + ".join(note_parts)
                if note_parts
                else "attempted structured certification extraction with no OCR text or local PDF available"
            )
            updated_row, changes = update_receipt_fields(
                conn,
                row["filename"],
                {"certifications": certifications},
                source="certify-backfill",
                note=note,
            )
            if not changes:
                skipped += 1
                continue

            if receipt_root is not None:
                _sync_receipt_sidecar(receipt_root, updated_row)
            if pdf is not None and receipt is not None:
                _write_receipt_debug_artifacts(
                    pdf,
                    receipt,
                    processed_by=processed_by,
                    tier=f"certify-{tier}",
                )

            if updated_row.get("certifications"):
                certified += 1
            pf_events.append(_receipt_event_from_row(updated_row, tier=f"certify-{tier}"))
            updated += 1
            print(f"  Certified {row['filename']}: {cert_labels}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR certifying {row['filename']}: {exc}", file=sys.stderr)
            failed += 1

    conn.close()
    _trigger_personal_finance(settings, pf_events, trigger="certify")
    print(
        f"\nCertification backfill complete: {updated} updated, {skipped} skipped, "
        f"{failed} error(s), {certified} with certifications."
    )


def _cmd_staff(args: argparse.Namespace, settings: dict) -> None:
    """Attempt staff/server/cashier extraction for existing receipt rows."""
    from intake.db import infer_purchaser_from_card_last4, init_db, update_receipt_fields
    from intake.sidecar import ocr_debug_path
    from intake.staff import extract_staff, receipt_staff_to_dicts

    receipt_root = None
    if args.receipt_dir or settings.get("receipt_dir"):
        receipt_root = _require_receipt_dir(settings, args.receipt_dir)

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    conn = init_db(db_path)
    row_cursor = conn.execute("""
            SELECT filename, scan_date, merchant, total, category, receipt_dir, raw_text,
                   staff_json, processed_by, purchaser, card_last4, processed_at
            FROM receipts
            ORDER BY scan_date DESC, processed_at DESC, filename DESC
            """)
    rows = [dict(row) for row in row_cursor.fetchall()]
    if args.limit is not None:
        rows = rows[: args.limit]

    updated = skipped = failed = 0
    with_staff = 0
    purchaser_fixes = 0
    pf_events: list[dict] = []

    for row in rows:
        if row.get("staff_json") is not None and not args.force:
            skipped += 1
            continue

        receipt_dir_rel = str(row.get("receipt_dir") or "")
        receipt_path = None
        if (
            receipt_root is not None
            and receipt_dir_rel
            and not receipt_dir_rel.startswith("email/")
        ):
            candidate = receipt_root / receipt_dir_rel
            if candidate.exists():
                receipt_path = candidate

        try:
            raw_text = row.get("raw_text") or ""
            note_parts: list[str] = []
            if raw_text:
                note_parts.append("stored OCR")
            if not raw_text and receipt_path is not None:
                debug_path = ocr_debug_path(receipt_path)
                if debug_path.exists():
                    raw_text = debug_path.read_text(encoding="utf-8", errors="replace")
                    note_parts.append("OCR debug artifact")

            staff = receipt_staff_to_dicts(extract_staff(raw_text)) if raw_text else []
            labels = (
                ", ".join(f"{item['role']}: {item['name']}" for item in staff if item.get("name"))
                or "0"
            )

            updates: dict[str, object | None] = {"staff": staff}
            purchaser = str(row.get("purchaser") or "").strip().lower()
            staff_names = {str(item.get("name") or "").strip().lower() for item in staff}
            if purchaser and purchaser in staff_names:
                inferred = (
                    infer_purchaser_from_card_last4(
                        conn,
                        row.get("card_last4"),
                        exclude_filename=row["filename"],
                    )
                    if row.get("card_last4")
                    else None
                )
                updates["purchaser"] = inferred
                purchaser_fixes += 1

            if args.dry_run:
                extra = ""
                if "purchaser" in updates:
                    extra = f"; purchaser -> {updates['purchaser'] or 'cleared'}"
                print(f"  [dry-run] {row['filename']}: {labels}{extra}")
                continue

            note = (
                "attempted staff/service extraction via " + " + ".join(note_parts)
                if note_parts
                else "attempted staff/service extraction with no OCR text or debug artifact available"
            )
            updated_row, changes = update_receipt_fields(
                conn,
                row["filename"],
                updates,
                source="staff-backfill",
                note=note,
            )
            if not changes:
                skipped += 1
                continue

            if receipt_root is not None:
                _sync_receipt_sidecar(receipt_root, updated_row)
            if updated_row.get("staff"):
                with_staff += 1
            pf_events.append(_receipt_event_from_row(updated_row, tier="staff-backfill"))
            updated += 1
            print(f"  Staff {row['filename']}: {labels}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR extracting staff for {row['filename']}: {exc}", file=sys.stderr)
            failed += 1

    conn.close()
    _trigger_personal_finance(settings, pf_events, trigger="staff")
    print(
        f"\nStaff extraction complete: {updated} updated, {skipped} skipped, "
        f"{failed} error(s), {with_staff} with staff, {purchaser_fixes} purchaser fix(es)."
    )


def _cmd_normalize_purchasers(args: argparse.Namespace, settings: dict) -> None:
    """Normalize existing purchaser aliases through the audited update path."""
    from intake.db import init_db, update_receipt_fields
    from intake.purchaser import normalize_purchaser

    receipt_root = None
    if args.receipt_dir or settings.get("receipt_dir"):
        receipt_root = _require_receipt_dir(settings, args.receipt_dir)

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    conn = init_db(db_path)
    purchaser_rows = conn.execute("""
            SELECT filename, purchaser
            FROM receipts
            WHERE purchaser IS NOT NULL AND TRIM(purchaser) != ''
            ORDER BY filename
            """).fetchall()
    rows = [dict(row) for row in purchaser_rows]

    updated = skipped = 0
    for row in rows:
        normalized = normalize_purchaser(row.get("purchaser"))
        if normalized == row.get("purchaser"):
            skipped += 1
            continue
        if args.dry_run:
            print(f"  [dry-run] {row['filename']}: {row['purchaser']!r} -> {normalized!r}")
            continue
        updated_row, changes = update_receipt_fields(
            conn,
            row["filename"],
            {"purchaser": normalized},
            source="purchaser-normalize",
            note="normalized purchaser alias",
        )
        if not changes:
            skipped += 1
            continue
        if receipt_root is not None:
            _sync_receipt_sidecar(receipt_root, updated_row)
        updated += 1
        print(f"  Purchaser {row['filename']}: {row['purchaser']!r} -> {normalized!r}")

    conn.close()
    if args.dry_run:
        print(f"\nPurchaser normalization dry-run complete: {len(rows) - skipped} change(s).")
    else:
        print(f"\nPurchaser normalization complete: {updated} updated, {skipped} skipped.")


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
    reason_width = max(42, *(len(row.get("review_reason_summary") or "") for row in flagged))
    col_w = {"#": 4, "file": 36, "merchant": 24, "total": 9, "model": 12, "reasons": reason_width}
    header = (
        f"{'#':<{col_w['#']}} "
        f"{'file':<{col_w['file']}} "
        f"{'merchant':<{col_w['merchant']}} "
        f"{'total':>{col_w['total']}} "
        f"{'model':<{col_w['model']}} "
        f"{'reasons':<{col_w['reasons']}}"
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
        reasons = row.get("review_reason_summary") or ""
        print(
            f"{i:<{col_w['#']}} "
            f"{filename:<{col_w['file']}} "
            f"{merchant:<{col_w['merchant']}} "
            f"{total:>{col_w['total']}} "
            f"{model:<{col_w['model']}} "
            f"{reasons:<{col_w['reasons']}}"
        )

    print(sep)
    print(
        "\nFlag reasons: missing/placeholder merchant · merchant_contains_email · "
        "short_merchant · missing/zero total · total_over_250/500/750/1000 · "
        "email_reply_request · "
        "recent new_merchant/new_card_last4/new_merchant_card_pair · "
        "amount_outlier_for_merchant"
    )

    # ── Markdown export ───────────────────────────────────────────────────────
    output_dir = Path(args.output_dir or settings.get("reports_dir") or _DEFAULT_REPORTS_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "review.md"

    lines = [
        "# Intake Review — Flagged Receipts",
        "",
        f"**{len(flagged)} record(s)** flagged for manual review.",
        "Apply fixes with `intake correct FILENAME ...`, which patches the DB + sidecar and records an audit event.",
        "",
        "| # | file | receipt_dir | merchant | total | model | reasons | correction |",
        "|---|------|-------------|----------|-------|-------|---------|------------|",
    ]
    for i, row in enumerate(flagged, 1):
        merchant = (row["merchant"] or "—").replace("|", "\\|")
        filename = row["filename"].replace("|", "\\|")
        receipt_dir = (row["receipt_dir"] or "").replace("|", "\\|")
        total = f"{row['total']:.2f}" if row["total"] is not None else "—"
        model = row["processed_by"] or "tesseract"
        reasons = (row.get("review_reason_summary") or "—").replace("|", "\\|")
        lines.append(
            f"| {i} | `{filename}` | `{receipt_dir}` | {merchant} | {total} | {model} | {reasons} |  |"
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
              `intake correct 20260409_1707.pdf --merchant kroger --note "manual fix"`
            - Total guard flags (`total_over_250`, `total_over_500`, `total_over_750`,
              `total_over_1000`) are explicit parser-review thresholds. They do not
              auto-rewrite totals; they force human eyes on expensive receipts.
            - `email_reply_request (...)` means intake recorded a reply email note or
              request for that receipt. Inspect it with `intake history FILENAME`.
            - Novelty and amount-outlier flags are only applied to recent receipts
              that already have a plausible merchant and total, so `intake review`
              stays operational instead of flooding on old or obviously broken rows.
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


def _cmd_correct(args: argparse.Namespace, settings: dict) -> None:
    """Apply a manual correction, update the sidecar, and record an audit event."""
    from intake.certification import parse_manual_certification, receipt_certifications_to_dicts
    from intake.db import init_db, update_receipt_fields

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    receipt_root = _require_receipt_dir(settings, args.receipt_dir)

    updates: dict[str, object | None] = {}
    if args.merchant is not None:
        updates["merchant"] = args.merchant.strip().lower()
    if args.total is not None:
        updates["total"] = float(args.total)
    if args.category is not None:
        updates["category"] = args.category.strip().strip("/")
    if args.purchaser is not None:
        updates["purchaser"] = args.purchaser.strip() or None
    if args.clear_purchaser:
        updates["purchaser"] = None
    if args.card_last4 is not None:
        updates["card_last4"] = args.card_last4.strip() or None
    if args.clear_card_last4:
        updates["card_last4"] = None
    if args.certification:
        manual_certs = [parse_manual_certification(spec) for spec in args.certification]
        manual_certs = [cert for cert in manual_certs if cert is not None]
        updates["certifications"] = receipt_certifications_to_dicts(manual_certs)
    if args.clear_certifications:
        updates["certifications"] = []
    if getattr(args, "currency", None) is not None:
        updates["currency"] = args.currency.strip().upper()

    if not updates:
        print("ERROR: no changes requested. Pass at least one field to correct.", file=sys.stderr)
        sys.exit(1)

    conn = init_db(db_path)
    row = conn.execute(
        "SELECT * FROM receipts WHERE filename = ?",
        (args.filename,),
    ).fetchone()
    if row is None:
        print(f"ERROR: receipt not found in DB: {args.filename}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    if "category" in updates:
        current_rel = row["receipt_dir"] or ""
        if current_rel.startswith("email/"):
            new_rel = _relocate_email_receipt_artifact(
                receipt_root,
                current_relpath=current_rel,
                new_category=str(updates["category"]),
                filename=args.filename,
                raw_text=row["raw_text"],
            )
        else:
            new_rel = _relocate_receipt_artifacts(
                receipt_root,
                current_relpath=current_rel,
                category=str(updates["category"]),
                filename=args.filename,
            )
        updates["receipt_dir"] = new_rel

    updated_row, changes = update_receipt_fields(
        conn,
        args.filename,
        updates,
        source="manual-correct",
        note=args.note,
    )
    conn.close()

    if not changes:
        sc_path = _sync_receipt_sidecar(receipt_root, updated_row)
        if sc_path is not None:
            print(f"No DB changes applied for {args.filename}; sidecar synced: {sc_path}")
        else:
            print(f"No changes applied for {args.filename}.")
        return

    sc_path = _sync_receipt_sidecar(receipt_root, updated_row)
    print(f"Updated {args.filename}:")
    for field, change in changes.items():
        if field == "processed_at":
            continue
        print(f"  {field}: {change['old']!r} -> {change['new']!r}")
    if args.note:
        print(f"  note: {args.note}")
    if sc_path is not None:
        print(f"  sidecar: {sc_path}")


def _cmd_history(args: argparse.Namespace, settings: dict) -> None:
    """Show recent audit events for a receipt."""
    from intake.db import init_db, query_receipt_updates

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    conn = init_db(db_path)
    updates = query_receipt_updates(conn, filename=args.filename, limit=args.limit)
    conn.close()

    if not updates:
        print(f"No update history recorded for {args.filename}.")
        return

    for item in updates:
        print(f"{item['updated_at']}  {item['filename']}  [{item['source']}]")
        if item.get("note"):
            print(f"  note: {item['note']}")
        for field, change in item["changes"].items():
            print(f"  {field}: {change['old']!r} -> {change['new']!r}")


def _cmd_normalize_filenames(args: argparse.Namespace, settings: dict) -> None:
    """Rename tracked receipts to the canonical ``YYYYMMDD_HHMM.EXT`` format."""
    from intake.db import init_db, update_receipt_fields
    from intake.sidecar import SIDECAR_SUFFIX

    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    conn = init_db(db_path)
    rows = conn.execute("""
        SELECT filename, scan_date, receipt_dir, raw_text
        FROM receipts
        WHERE receipt_dir IS NOT NULL
          AND receipt_dir NOT LIKE 'email/%'
        ORDER BY receipt_dir
        """).fetchall()

    renamed = skipped = missing = 0
    reserved_paths: set[Path] = set()
    handled_sidecars: set[Path] = set()
    for row in rows:
        current_rel = row["receipt_dir"]
        if not current_rel:
            skipped += 1
            continue
        current_pdf = receipt_dir / current_rel
        handled_sidecars.add(current_pdf.with_suffix(SIDECAR_SUFFIX))
        if not current_pdf.exists():
            print(f"  MISSING: {current_rel}")
            missing += 1
            continue

        desired_pdf = _canonical_existing_receipt_path(
            current_pdf,
            scan_date=row["scan_date"],
            raw_text=row["raw_text"],
            reserved_paths=reserved_paths,
        )
        handled_sidecars.add(desired_pdf.with_suffix(SIDECAR_SUFFIX))
        if desired_pdf == current_pdf:
            skipped += 1
            continue

        desired_rel = str(desired_pdf.relative_to(receipt_dir))
        if args.dry_run:
            print(f"  [dry-run] {current_rel} -> {desired_rel}")
            renamed += 1
            continue

        _move_receipt_artifacts(
            receipt_dir,
            current_relpath=current_rel,
            new_relpath=desired_rel,
        )
        updated_row, _changes = update_receipt_fields(
            conn,
            row["filename"],
            {"filename": desired_pdf.name, "receipt_dir": desired_rel},
            source="normalize-filenames",
            note="normalized receipt filename to canonical timestamp format",
        )
        _sync_receipt_sidecar(receipt_dir, updated_row)
        print(f"  {current_rel} -> {desired_rel}")
        renamed += 1

    # Sweep orphaned sidecars that are not currently represented in the DB.
    for sc_path in sorted(receipt_dir.rglob(f"*{SIDECAR_SUFFIX}")):
        rel_sc = sc_path.relative_to(receipt_dir)
        if not rel_sc.parts or rel_sc.parts[0] in ("email", "exhaust", "intake"):
            continue
        if sc_path in handled_sidecars:
            continue

        try:
            payload = json.loads(sc_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"  WARNING: could not read orphan sidecar {rel_sc}", file=sys.stderr)
            continue

        current_pdf = sc_path.with_name(sc_path.name.removesuffix(SIDECAR_SUFFIX) + ".pdf")
        if not current_pdf.exists():
            print(f"  MISSING: {current_pdf.relative_to(receipt_dir)}")
            missing += 1
            continue

        desired_pdf = _canonical_existing_receipt_path(
            current_pdf,
            scan_date=str(payload.get("scan_date") or ""),
            raw_text=None,
            reserved_paths=reserved_paths,
        )
        if desired_pdf == current_pdf:
            skipped += 1
            continue

        current_rel = str(current_pdf.relative_to(receipt_dir))
        desired_rel = str(desired_pdf.relative_to(receipt_dir))
        if args.dry_run:
            print(f"  [dry-run] {current_rel} -> {desired_rel}")
            renamed += 1
            continue

        _move_receipt_artifacts(
            receipt_dir,
            current_relpath=current_rel,
            new_relpath=desired_rel,
        )
        payload["filename"] = desired_pdf.name
        payload["receipt_dir"] = desired_rel
        desired_sc = desired_pdf.with_suffix(SIDECAR_SUFFIX)
        desired_sc.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"  {current_rel} -> {desired_rel}")
        renamed += 1

    conn.close()
    suffix = " (dry-run)" if args.dry_run else ""
    print(
        f"\nFilename normalization: {renamed} rename(s), {skipped} already canonical, {missing} missing.{suffix}"
    )


def _cmd_audit(args: argparse.Namespace, settings: dict) -> None:
    receipt_dir = _require_receipt_dir(settings, args.receipt_dir)
    intake_dir = receipt_dir / "intake"

    if not intake_dir.exists():
        print(f"ERROR: intake directory not found: {intake_dir}", file=sys.stderr)
        sys.exit(1)

    receipt_files = _intake_receipt_files(intake_dir)
    if not receipt_files:
        print("Intake folder is empty.")
        return

    print(f"Found {len(receipt_files)} receipt file(s) in {intake_dir}:")
    for pdf in receipt_files:
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
    from intake.db import get_receipt, init_db, update_receipt_fields, upsert_receipt
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
    reserved_paths: set[Path] = set()

    for group in groups:
        primary = group[0]
        secondaries = group[1:]
        rel = primary.relative_to(receipt_dir)
        category = "/".join(rel.parts[:-1])
        original_filename = primary.name
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
                primary, **extract_kwargs, fuzzy_rules=rules, merchant_aliases=merchant_aliases
            )
            receipt = _backfill_purchaser_from_card(conn, receipt)
            dest = _canonical_receipt_destination(
                primary.parent,
                receipt,
                source_path=primary,
                reserved_paths=reserved_paths,
            )
            renamed_primary = dest != primary
            if dest != primary:
                _move_receipt_artifacts(
                    receipt_dir,
                    current_relpath=str(rel),
                    new_relpath=str(dest.relative_to(receipt_dir)),
                )
                primary = dest
                rel = primary.relative_to(receipt_dir)
                category = "/".join(rel.parts[:-1])
            row = _receipt_row_from_receipt(
                receipt,
                filename=primary.name,
                category=category,
                receipt_dir=str(rel),
                file_hash=_file_hash(primary),
                processed_by=model,
                tier=tier,
            )
            existing_old = get_receipt(conn, original_filename)
            if renamed_primary and existing_old is not None:
                db_updates = {k: v for k, v in row.items() if k != "tier"}
                update_receipt_fields(
                    conn,
                    original_filename,
                    db_updates,
                    source="merge-rename",
                    note="canonicalized filename during multi-page merge",
                )
            else:
                upsert_receipt(conn, row)
            write_sidecar(primary, {k: v for k, v in row.items() if k != "raw_text"})
            _write_receipt_debug_artifacts(primary, receipt, processed_by=model, tier=tier)
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

    elif args.email_command == "prune-nonreceipts":
        _cmd_email_prune_nonreceipts(args, settings, conn)

    conn.close()


def _email_filename_uid(filename: str) -> int | None:
    """Return the Gmail UID embedded in an inline email receipt filename."""
    parts = Path(filename).stem.split("_", 2)
    if len(parts) < 3 or parts[0] != "email":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _email_artifact_relpath(row: dict) -> str:
    """Return the artifact file path for an inline email receipt row."""
    receipt_dir = Path(str(row.get("receipt_dir") or ""))
    filename = str(row.get("filename") or "")
    if receipt_dir.name == filename:
        return str(receipt_dir)
    return str(receipt_dir / filename)


def _unique_exhaust_relpath(receipt_root: Path, relpath: str) -> str:
    """Return a non-clobbering exhaust path under *receipt_root*."""
    candidate = receipt_root / relpath
    if not candidate.exists():
        return relpath
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 1000):
        alternate = candidate.with_name(f"{stem}_{index}{suffix}")
        if not alternate.exists():
            return str(alternate.relative_to(receipt_root))
    raise RuntimeError(f"could not allocate exhaust path for {candidate}")


def _cmd_email_prune_nonreceipts(
    args: argparse.Namespace,
    settings: dict,
    conn,
) -> None:
    """Move known non-receipt email rows out of receipt reporting."""
    from intake.db import record_receipt_update
    from intake.email import _is_known_non_receipt_notice

    receipt_root = _require_receipt_dir(settings, getattr(args, "receipt_dir", None))
    dry_run = bool(getattr(args, "dry_run", False))

    row_query = """
            SELECT filename, merchant, total, scan_date, category, receipt_dir,
                   raw_text, processed_by, purchaser, card_last4, processed_at,
                   file_hash
            FROM receipts
            WHERE filename LIKE 'email\\_%' ESCAPE '\\'
            ORDER BY scan_date, filename
            """
    rows = []
    for row in conn.execute(row_query).fetchall():
        rows.append(dict(row))

    candidates: list[tuple[dict, dict | None]] = []
    for row in rows:
        uid = _email_filename_uid(row["filename"])
        email_meta = None
        if uid is not None:
            email_meta = conn.execute(
                """
                SELECT uid, mailbox, message_id, subject, sender, received_at, status
                FROM email_receipts
                WHERE uid = ?
                ORDER BY processed_at DESC
                LIMIT 1
                """,
                (uid,),
            ).fetchone()
            email_meta = dict(email_meta) if email_meta is not None else None
        subject = (email_meta or {}).get("subject") or ""
        sender = (email_meta or {}).get("sender") or ""
        if _is_known_non_receipt_notice(subject, sender, row.get("raw_text") or ""):
            candidates.append((row, email_meta))

    if not candidates:
        print("No known non-receipt email rows found.")
        return

    print(f"Found {len(candidates)} known non-receipt email row(s):")
    removed = 0
    for row, email_meta in candidates:
        current_rel = _email_artifact_relpath(row)
        current_path = receipt_root / current_rel
        target_rel = _unique_exhaust_relpath(
            receipt_root,
            str(Path("exhaust") / "non-receipts" / current_rel),
        )
        print(
            f"  {row['filename']:<28} {row.get('scan_date')}  "
            f"{row.get('merchant') or '?'}  ${float(row.get('total') or 0):.2f}"
        )
        if dry_run:
            print(f"    would move → {target_rel}")
            continue

        moved_artifact = False
        if current_path.exists():
            _move_receipt_artifacts(
                receipt_root,
                current_relpath=current_rel,
                new_relpath=target_rel,
            )
            exhausted_row = dict(row)
            exhausted_row["filename"] = Path(target_rel).name
            exhausted_row["receipt_dir"] = target_rel
            _sync_receipt_sidecar(receipt_root, exhausted_row)
            moved_artifact = True

        record_receipt_update(
            conn,
            row["filename"],
            source="email-prune-nonreceipt",
            note="removed known scheduled/upcoming non-receipt email from receipts",
            changes={
                "status": {"old": "tracked", "new": "non-receipt-exhausted"},
                "receipt_dir": {
                    "old": row["receipt_dir"],
                    "new": target_rel if moved_artifact else None,
                },
                "artifact": {
                    "old": "present" if moved_artifact else "missing",
                    "new": "exhausted" if moved_artifact else "db-row-removed",
                },
            },
        )
        if email_meta is not None:
            conn.execute(
                """
                UPDATE email_receipts
                SET status = 'ignored'
                WHERE uid = ? AND mailbox = ?
                """,
                (email_meta["uid"], email_meta["mailbox"]),
            )
        conn.execute("DELETE FROM receipts WHERE filename = ?", (row["filename"],))
        conn.commit()
        if moved_artifact:
            print(f"    moved → {target_rel}")
        else:
            print(f"    removed DB row; artifact missing at {current_path}")
        removed += 1

    if dry_run:
        print(f"\nDry run complete: {len(candidates)} row(s) would be pruned.")
    else:
        print(f"\nPruned {removed} non-receipt email row(s).")


def _process_inbox(settings: dict, db_path: Path) -> tuple[int, int]:
    """Process all supported files currently in the intake inbox folder.

    Returns (ok_count, error_count).  Mirrors _cmd_process but is callable
    from the daemon loop without requiring argparse Namespace objects.
    """
    from intake.classify import classify, load_merchant_aliases, load_rules
    from intake.db import (
        get_card_by_file_hash,
        get_letter_by_file_hash,
        get_payment_by_file_hash,
        get_receipt_by_file_hash,
        init_db,
        upsert_card,
        upsert_letter,
        upsert_payment,
        upsert_receipt,
    )
    from intake.document_type import classify_document_type
    from intake.extract import extract_card, extract_letter, extract_payment, extract_receipt
    from intake.ocr import extract_text
    from intake.parse_card import classify_card
    from intake.parse_letter import classify_letter
    from intake.parse_payment import classify_payment
    from intake.personal_finance import build_receipt_event
    from intake.sidecar import write_sidecar

    receipt_dir = Path(settings.get("receipt_dir", ""))
    if not receipt_dir:
        return 0, 0
    intake_dir = receipt_dir / "intake"
    pdfs = _intake_receipt_files(intake_dir) if intake_dir.exists() else []
    if not pdfs:
        return 0, 0

    rules = load_rules()
    merchant_aliases = load_merchant_aliases()
    conn = init_db(db_path)
    ok = errors = 0
    pf_events: list[dict] = []
    reserved_paths: set[Path] = set()
    pdfs, live_photo_messages = _collapse_intake_live_photo_duplicates(
        receipt_dir,
        conn,
        pdfs,
        reserved_paths=reserved_paths,
    )
    for message in live_photo_messages:
        print(message)
    for pdf in pdfs:
        try:
            file_hash = _file_hash(pdf)
            existing = (
                get_receipt_by_file_hash(conn, file_hash)
                or get_letter_by_file_hash(conn, file_hash)
                or get_card_by_file_hash(conn, file_hash)
                or get_payment_by_file_hash(conn, file_hash)
            )
            if existing is not None:
                dest = _exhaust_hash_duplicate(
                    receipt_dir,
                    conn,
                    pdf=pdf,
                    existing_row=existing,
                    file_hash=file_hash,
                    reserved_paths=reserved_paths,
                    source="daemon-hash-duplicate",
                )
                print(
                    "  [inbox] duplicate by file hash, moved to "
                    f"{dest.relative_to(receipt_dir)} (matches {existing['receipt_dir']})"
                )
                continue

            raw_text = extract_text(pdf)
            doc_type = classify_document_type(raw_text, pdf.name)

            if doc_type == "letter":
                letter, model = extract_letter(
                    pdf,
                    raw_text,
                    openai_api_key=settings.get("openai_api_key") or None,
                    openai_model=settings.get("openai_model_low") or "gpt-4o-mini",
                )
                category = classify_letter(letter)
                dest_dir = receipt_dir / category
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _canonical_document_destination(
                    dest_dir,
                    source_path=pdf,
                    document_date=letter.letter_date or letter.scan_date,
                    reserved_paths=reserved_paths,
                )
                shutil.move(str(pdf), dest)
                row = _letter_row_from_letter(
                    letter,
                    filename=dest.name,
                    category=category,
                    receipt_dir=str(dest.relative_to(receipt_dir)),
                    file_hash=file_hash,
                    processed_by=model,
                )
                upsert_letter(conn, row)
                write_sidecar(
                    dest,
                    {
                        **{k: v for k, v in row.items() if k != "raw_text"},
                        "document_type": "letter",
                        "tier": "ok",
                    },
                )
                print(
                    f"  [inbox] {dest.relative_to(receipt_dir)} "
                    f"[letter] sender={letter.sender!r} model={model}"
                )
                ok += 1
                continue

            if doc_type == "card":
                card, model = extract_card(
                    pdf,
                    raw_text,
                    openai_api_key=settings.get("openai_api_key") or None,
                    openai_model=settings.get("openai_model_low") or "gpt-4o-mini",
                )
                category = classify_card(card)
                dest_dir = receipt_dir / category
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _canonical_document_destination(
                    dest_dir,
                    source_path=pdf,
                    document_date=card.card_date or card.scan_date,
                    reserved_paths=reserved_paths,
                )
                shutil.move(str(pdf), dest)
                row = _card_row_from_card(
                    card,
                    filename=dest.name,
                    category=category,
                    receipt_dir=str(dest.relative_to(receipt_dir)),
                    file_hash=file_hash,
                    processed_by=model,
                )
                upsert_card(conn, row)
                write_sidecar(
                    dest,
                    {
                        **{k: v for k, v in row.items() if k != "raw_text"},
                        "document_type": "card",
                        "tier": "ok",
                    },
                )
                print(
                    f"  [inbox] {dest.relative_to(receipt_dir)} "
                    f"[card] occasion={card.occasion!r} model={model}"
                )
                ok += 1
                continue

            if doc_type == "payment":
                payment, model = extract_payment(
                    pdf,
                    raw_text,
                    openai_api_key=settings.get("openai_api_key") or None,
                    openai_model=settings.get("openai_model_low") or "gpt-4o-mini",
                )
                category = classify_payment(payment)
                dest_dir = receipt_dir / category
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _canonical_document_destination(
                    dest_dir,
                    source_path=pdf,
                    document_date=payment.payment_date or payment.scan_date,
                    reserved_paths=reserved_paths,
                )
                shutil.move(str(pdf), dest)
                row = _payment_row_from_payment(
                    payment,
                    filename=dest.name,
                    category=category,
                    receipt_dir=str(dest.relative_to(receipt_dir)),
                    file_hash=file_hash,
                    processed_by=model,
                )
                upsert_payment(conn, row)
                write_sidecar(
                    dest,
                    {
                        **{k: v for k, v in row.items() if k != "raw_text"},
                        "document_type": "payment",
                        "tier": "ok",
                    },
                )
                print(
                    f"  [inbox] {dest.relative_to(receipt_dir)} "
                    f"[payment] amount={payment.amount!r} account={payment.account_name!r}"
                    f" model={model}"
                )
                ok += 1
                continue

            # receipt (or unknown — fall through to receipt pipeline)
            receipt, model, tier = extract_receipt(
                pdf,
                raw_text=raw_text,
                **_get_extract_kwargs(settings),
                fuzzy_rules=rules,
                merchant_aliases=merchant_aliases,
            )
            receipt = _backfill_purchaser_from_card(conn, receipt)
            category = classify(receipt.merchant, rules, receipt.raw_text)
            dest_dir = receipt_dir / category
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = _canonical_receipt_destination(
                dest_dir,
                receipt,
                source_path=pdf,
                reserved_paths=reserved_paths,
            )
            shutil.move(str(pdf), dest)
            row = _receipt_row_from_receipt(
                receipt,
                filename=dest.name,
                category=category,
                receipt_dir=str(dest.relative_to(receipt_dir)),
                file_hash=file_hash,
                processed_by=model,
                tier=tier,
            )
            upsert_receipt(conn, row)
            write_sidecar(dest, {k: v for k, v in row.items() if k != "raw_text"})
            _write_receipt_debug_artifacts(dest, receipt, processed_by=model, tier=tier)
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
            inbox_pdfs = len(_intake_receipt_files(receipt_dir / "intake"))
            if inbox_pdfs:
                print(f"[inbox] {inbox_pdfs} receipt file(s) found — processing…", flush=True)
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


def _install_web_unit() -> None:
    """Install the web-serving unit through the shared clockwork renderer."""
    _install_clockwork_manifest(
        _build_web_manifest_text(
            python_executable=sys.executable,
        ),
        target="systemd-user",
    )


def _find_duplicate_groups(rows: list, window_days: int, *, hash_only: bool = False) -> list[list]:
    """Return clusters of rows that likely represent the same transaction.

    Exact file-hash matches are treated as duplicates first. Remaining rows
    fall back to the merchant+total+scan-date heuristic.
    """
    from datetime import date

    def parse_date(s: str | None) -> date | None:
        try:
            return date.fromisoformat((s or "")[:10])
        except ValueError:
            return None

    groups: list[list] = []
    exact_filenames: set[str] = set()

    hash_buckets: dict[str, list] = {}
    for row in rows:
        file_hash = (row.get("file_hash") or "").strip()
        if not file_hash:
            continue
        hash_buckets.setdefault(file_hash, []).append(row)

    for bucket_rows in hash_buckets.values():
        if len(bucket_rows) < 2:
            continue
        bucket_rows.sort(
            key=lambda r: (r["scan_date"] or "", r["processed_at"] or "", r["filename"])
        )
        groups.append(bucket_rows)
        exact_filenames.update(r["filename"] for r in bucket_rows)

    if hash_only:
        return groups

    # Group by (merchant, rounded total).
    buckets: dict[tuple, list] = {}
    for row in rows:
        if row["filename"] in exact_filenames:
            continue
        if row.get("processed_by") == "email-payment":
            continue
        merchant = (row["merchant"] or "").strip().lower()
        total = round(row["total"] or 0.0, 2)
        if not merchant or total == 0.0:
            continue
        buckets.setdefault((merchant, total), []).append(row)
    for bucket_rows in buckets.values():
        if len(bucket_rows) < 2:
            continue
        # Sort by scan_date so window logic is monotone.
        bucket_rows.sort(key=lambda r: r["scan_date"] or "")
        # Sliding-window cluster: start a new cluster when the gap between
        # successive dates exceeds window_days.
        cluster: list = [bucket_rows[0]]
        for row in bucket_rows[1:]:
            prev_d = parse_date(cluster[-1]["scan_date"])
            curr_d = parse_date(row["scan_date"])
            if prev_d and curr_d and abs((curr_d - prev_d).days) <= window_days:
                cluster.append(row)
            else:
                if len(cluster) > 1:
                    groups.append(cluster)
                cluster = [row]
        if len(cluster) > 1:
            groups.append(cluster)

    return groups


def _best_row(rows: list) -> dict:
    """Return the row that best represents the transaction.

    Priority: real local receipt artifact over email/synthetic rows → most
    extra fields filled (purchaser, card_last4) → highest model rank →
    earliest processed_at.
    """
    from intake.extract import model_rank

    def has_local_artifact(row: dict) -> int:
        receipt_dir = str(row.get("receipt_dir") or "").strip()
        return int(bool(receipt_dir) and not receipt_dir.startswith("email/"))

    def score(row: dict) -> tuple:
        local_artifact = has_local_artifact(row)
        extra = sum(1 for f in ("purchaser", "card_last4") if row.get(f))
        rank = model_rank(row.get("processed_by") or "tesseract")
        # Negate processed_at so earlier timestamps sort higher.
        ts = row.get("processed_at") or ""
        return (local_artifact, extra, rank, ts)

    return max(rows, key=score)


def _duplicate_exhaust_relpath(
    receipt_root: Path,
    row: dict,
    *,
    reserved_paths: set[Path] | None = None,
) -> str:
    """Return the exhaust destination for a duplicate receipt artifact."""
    current_rel = row.get("receipt_dir") or ""
    if not current_rel:
        raise ValueError(f"missing receipt_dir for {row.get('filename') or 'receipt'}")

    current_path = receipt_root / current_rel
    if not current_path.exists():
        raise FileNotFoundError(f"receipt file not found: {current_path}")

    category = str(row.get("category") or "").strip().strip("/") or "general"
    dest_dir = receipt_root / "exhaust" / "duplicates" / category
    dest_path = _canonical_receipt_artifact_path(
        dest_dir,
        source_path=current_path,
        scan_date=row.get("scan_date"),
        raw_text=row.get("raw_text"),
        reserved_paths=reserved_paths,
    )
    return str(dest_path.relative_to(receipt_root))


def _cmd_dedup(args: argparse.Namespace, settings: dict) -> None:
    """Detect and optionally merge duplicate receipt rows in the database.

    Two rows are treated as duplicates when they share the same merchant and
    total amount, and their scan dates fall within --window days of each other.

    Without flags, the command lists duplicate groups only (dry-run).
    With --merge, it:
      1. Selects the best keeper row (highest model rank, most complete fields).
      2. Promotes any purchaser/card_last4 from other group members into the
         keeper if the keeper is missing them.
      3. Deletes all non-keeper rows from the DB (PDF files are untouched).
    With --remove, it performs the same keeper merge, then moves duplicate
    artifacts under ``exhaust/duplicates/...`` and records an audited removal
    before deleting the DB row.
    """
    from intake.db import init_db, record_receipt_update, update_receipt_fields

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    window = int(args.window)
    remove = getattr(args, "remove", False)
    merge = bool(getattr(args, "merge", False) or remove)
    receipt_root = None
    if remove:
        receipt_root = _require_receipt_dir(settings, getattr(args, "receipt_dir", None))
    elif getattr(args, "receipt_dir", None) or settings.get("receipt_dir"):
        candidate_root = Path(getattr(args, "receipt_dir", None) or settings["receipt_dir"])
        if candidate_root.exists():
            receipt_root = candidate_root

    conn = init_db(db_path)
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT filename, merchant, total, scan_date, category, receipt_dir, file_hash, raw_text, "
            "processed_by, purchaser, card_last4, processed_at "
            "FROM receipts"
        ).fetchall()
    ]
    rows = _hydrate_missing_file_hashes(conn, rows, receipt_root=receipt_root)

    groups = _find_duplicate_groups(rows, window, hash_only=getattr(args, "hash_only", False))

    if not groups:
        if getattr(args, "hash_only", False):
            print("No duplicates found (exact file-hash matches).")
        else:
            print(f"No duplicates found (merchant+total, within {window}-day window).")
        conn.close()
        return

    total_dupes = sum(len(g) - 1 for g in groups)
    print(f"Found {len(groups)} duplicate group(s), {total_dupes} row(s) to remove:\n")

    planned_removals: dict[str, str] = {}
    if remove:
        reserved_paths: set[Path] = set()
        issues: list[str] = []
        for group in groups:
            keeper = _best_row(group)
            for row in group:
                if row["filename"] == keeper["filename"]:
                    continue
                try:
                    planned_removals[row["filename"]] = _duplicate_exhaust_relpath(
                        receipt_root,
                        row,
                        reserved_paths=reserved_paths,
                    )
                except (FileNotFoundError, ValueError) as exc:
                    issues.append(str(exc))

        if issues:
            print("ERROR: cannot remove duplicates safely:", file=sys.stderr)
            for issue in issues:
                print(f"  - {issue}", file=sys.stderr)
            conn.close()
            sys.exit(1)

    removed = 0
    for i, group in enumerate(groups, 1):
        keeper = _best_row(group)
        merchant = keeper["merchant"] or "?"
        total_amt = keeper["total"]
        exact_hash = bool(keeper.get("file_hash")) and all(
            row.get("file_hash") == keeper.get("file_hash") for row in group
        )
        label = "hash match" if exact_hash else "merchant+total window"
        total_label = f"${total_amt:.2f}" if total_amt is not None else "total unknown"
        print(f"  Group {i} — {merchant!r}  {total_label}  ({len(group)} copies, {label})")
        for row in group:
            tag = " [KEEP]" if row["filename"] == keeper["filename"] else ""
            extras = []
            if row.get("purchaser"):
                extras.append(f"purchaser={row['purchaser']!r}")
            if row.get("card_last4"):
                extras.append(f"card=*{row['card_last4']}")
            extra_str = ("  " + "  ".join(extras)) if extras else ""
            print(
                f"    {'*' if tag else ' '} {row['filename']:<36} "
                f"{row['scan_date']}  {row['processed_by'] or 'tesseract':<12}{tag}{extra_str}"
            )

        if merge:
            # Promote any missing fields from duplicates into the keeper.
            from intake.purchaser import normalize_purchaser

            merged_purchaser = normalize_purchaser(
                keeper.get("purchaser")
                or next((r["purchaser"] for r in group if r.get("purchaser")), None)
            )
            merged_card = keeper.get("card_last4") or next(
                (r["card_last4"] for r in group if r.get("card_last4")), None
            )
            if merged_purchaser != keeper.get("purchaser") or merged_card != keeper.get(
                "card_last4"
            ):
                updated_row, _changes = update_receipt_fields(
                    conn,
                    keeper["filename"],
                    {"purchaser": merged_purchaser, "card_last4": merged_card},
                    source="dedup-merge",
                    note="promoted missing keeper fields from duplicate receipts",
                )
                if receipt_root is not None:
                    _sync_receipt_sidecar(receipt_root, updated_row)
                promoted = []
                if merged_purchaser and merged_purchaser != keeper.get("purchaser"):
                    promoted.append(f"purchaser={merged_purchaser!r}")
                if merged_card and merged_card != keeper.get("card_last4"):
                    promoted.append(f"card=*{merged_card}")
                if promoted:
                    print(f"    ^ promoted into keeper: {', '.join(promoted)}")

            for row in group:
                if row["filename"] != keeper["filename"]:
                    if remove:
                        new_rel = planned_removals[row["filename"]]
                        new_filename = Path(new_rel).name
                        _move_receipt_artifacts(
                            receipt_root,
                            current_relpath=row["receipt_dir"],
                            new_relpath=new_rel,
                        )
                        exhausted_row = dict(row)
                        exhausted_row["filename"] = new_filename
                        exhausted_row["receipt_dir"] = new_rel
                        _sync_receipt_sidecar(receipt_root, exhausted_row)

                        changes = {
                            "status": {"old": "tracked", "new": "duplicate-exhausted"},
                            "keeper_filename": {"old": None, "new": keeper["filename"]},
                            "receipt_dir": {"old": row["receipt_dir"], "new": new_rel},
                        }
                        if new_filename != row["filename"]:
                            changes["filename"] = {"old": row["filename"], "new": new_filename}
                        record_receipt_update(
                            conn,
                            row["filename"],
                            source="dedup-remove",
                            note=f"moved duplicate receipt to exhaust; kept {keeper['filename']}",
                            changes=changes,
                        )
                        conn.execute("DELETE FROM receipts WHERE filename = ?", (row["filename"],))
                        conn.commit()
                        print(f"    - moved duplicate → {new_rel}")
                    else:
                        record_receipt_update(
                            conn,
                            row["filename"],
                            source="dedup-merge-delete",
                            note=f"deleted duplicate DB row; kept {keeper['filename']}",
                            changes={
                                "status": {"old": "tracked", "new": "duplicate-merged-removed"},
                                "keeper_filename": {"old": None, "new": keeper["filename"]},
                            },
                        )
                        conn.execute("DELETE FROM receipts WHERE filename = ?", (row["filename"],))
                    removed += 1
            if not remove:
                conn.commit()

    conn.close()

    if remove:
        print(
            f"\nRemoved {removed} duplicate row(s): moved artifacts to exhaust/duplicates "
            "and recorded audit history."
        )
    elif merge:
        print(f"\nMerged {removed} duplicate row(s) into keepers (PDF files untouched).")
    else:
        print(
            f"\nRun with --merge to consolidate {total_dupes} duplicate row(s), "
            "or --remove to exhaust duplicate artifacts with audit history."
        )


def _cmd_consolidate(args: argparse.Namespace, settings: dict) -> None:
    """Apply the merchant alias map to existing DB rows and sidecars.

    Does NOT re-OCR.  Two-pass approach:

    Pass 1 — DB rows: update every receipt whose merchant matches an alias,
    and patch the companion sidecar alongside the PDF.

    Pass 2 — filesystem sweep: walk all ``.intake.json`` files in the receipt
    directory and patch any whose merchant matches an alias, even if the file
    is not tracked in the DB (orphaned sidecars from earlier processing runs).
    """
    from intake.classify import load_merchant_aliases
    from intake.db import init_db, update_receipt_fields
    from intake.merchant import consolidate_merchant
    from intake.sidecar import sidecar_path

    db_path = Path(args.db or settings.get("db_path") or _DEFAULT_DB)
    receipt_dir = _require_receipt_dir(settings, getattr(args, "receipt_dir", None))

    merchant_aliases = load_merchant_aliases()
    if not merchant_aliases:
        print("No merchant aliases configured in categories.toml — nothing to do.")
        return

    # ── Pass 1: DB rows ───────────────────────────────────────────────────────
    conn = init_db(db_path)
    rows = conn.execute("SELECT filename, merchant, receipt_dir FROM receipts").fetchall()

    db_changed = db_skipped = 0
    for row in rows:
        filename = row["filename"]
        old_merchant = row["merchant"] or ""
        new_merchant = consolidate_merchant(old_merchant, merchant_aliases)

        if new_merchant == old_merchant:
            db_skipped += 1
            continue

        if args.dry_run:
            print(f"  [db]  {filename}: {old_merchant!r} → {new_merchant!r}  [dry-run]")
            db_changed += 1
            continue

        updated_row, _changes = update_receipt_fields(
            conn,
            filename,
            {"merchant": new_merchant},
            source="consolidate",
            note="applied merchant alias consolidation",
        )
        _sync_receipt_sidecar(receipt_dir, updated_row)

        print(f"  [db]  {filename}: {old_merchant!r} → {new_merchant!r}")
        db_changed += 1

    # ── Pass 2: filesystem sweep (catches orphaned sidecars) ─────────────────
    # Track which sidecar paths were already handled in pass 1.
    db_sc_paths: set[Path] = set()
    for row in rows:
        rel = row["receipt_dir"] or ""
        if rel:
            db_sc_paths.add(sidecar_path(receipt_dir / rel))

    fs_changed = fs_skipped = 0
    for sc_path in sorted(receipt_dir.rglob("*.intake.json")):
        if sc_path in db_sc_paths:
            continue  # already handled in pass 1
        try:
            sc = json.loads(sc_path.read_text())
            old_merchant = sc.get("merchant") or ""
            new_merchant = consolidate_merchant(old_merchant, merchant_aliases)
            if new_merchant == old_merchant:
                fs_skipped += 1
                continue
            if args.dry_run:
                print(
                    f"  [fs]  {sc_path.relative_to(receipt_dir)}: {old_merchant!r} → {new_merchant!r}  [dry-run]"
                )
            else:
                sc["merchant"] = new_merchant
                sc_path.write_text(json.dumps(sc, indent=2))
                print(
                    f"  [fs]  {sc_path.relative_to(receipt_dir)}: {old_merchant!r} → {new_merchant!r}"
                )
            fs_changed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: could not patch {sc_path.name}: {exc}", file=sys.stderr)

    suffix = "  (dry-run)" if args.dry_run else ""
    print(
        f"\nDB:  {db_changed} updated, {db_skipped} already canonical.{suffix}"
        f"\nFS:  {fs_changed} sidecar(s) patched, {fs_skipped} skipped.{suffix}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="intake",
        description="Process, sort, and report on receipt files.",
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

    # serve
    p_srv = sub.add_parser(
        "serve",
        help="Serve generated reports over localhost HTTP for wiring-harness/Caddy",
    )
    p_srv.add_argument(
        "--output-dir", metavar="PATH", help="Directory to serve (default: reports_dir)"
    )
    p_srv.add_argument("--host", metavar="HOST", help="Bind host (default: web.host or 127.0.0.1)")
    p_srv.add_argument(
        "--port",
        metavar="PORT",
        type=int,
        help="Bind port (default: web.port or 5200)",
    )
    p_srv.add_argument(
        "--install",
        action="store_true",
        help="Write a systemd user unit file and print activation instructions",
    )

    # backfill
    p_bf = sub.add_parser(
        "backfill",
        help="Ingest receipts already in category folders (tier-aware skip)",
    )
    p_bf.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_bf.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_bf.add_argument("--dry-run", action="store_true", help="Print actions without writing to DB")
    p_bf.add_argument(
        "--notify",
        action="store_true",
        default=False,
        help="Send notifications for each backfilled receipt (default: off — historical data)",
    )

    # itemize
    p_itm = sub.add_parser(
        "itemize",
        help="Attempt itemized breakdowns for historical receipts and store them in DB/sidecars",
    )
    p_itm.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_itm.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_itm.add_argument("--dry-run", action="store_true", help="Preview itemization changes only")
    p_itm.add_argument(
        "--force",
        action="store_true",
        help="Re-attempt itemization even when a receipt already has stored items_json",
    )
    p_itm.add_argument(
        "--limit",
        metavar="N",
        type=int,
        help="Process only the most recent N receipt rows",
    )

    # certify
    p_crt = sub.add_parser(
        "certify",
        help="Attempt certification extraction for historical receipts and store it in DB/sidecars",
    )
    p_crt.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_crt.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_crt.add_argument("--dry-run", action="store_true", help="Preview certification changes only")
    p_crt.add_argument(
        "--force",
        action="store_true",
        help="Re-attempt certification extraction even when a receipt already has stored certifications_json",
    )
    p_crt.add_argument(
        "--limit",
        metavar="N",
        type=int,
        help="Process only the most recent N receipt rows",
    )

    # staff
    p_stf = sub.add_parser(
        "staff",
        help="Extract server/cashier/clerk metadata for historical receipts",
    )
    p_stf.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_stf.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_stf.add_argument("--dry-run", action="store_true", help="Preview staff changes only")
    p_stf.add_argument(
        "--force",
        action="store_true",
        help="Re-attempt staff extraction even when a receipt already has stored staff_json",
    )
    p_stf.add_argument(
        "--limit",
        metavar="N",
        type=int,
        help="Process only the most recent N receipt rows",
    )

    # normalize-purchasers
    p_np = sub.add_parser(
        "normalize-purchasers",
        help="Normalize purchaser aliases in existing DB rows and sidecars",
    )
    p_np.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_np.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_np.add_argument("--dry-run", action="store_true", help="Preview purchaser changes only")

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
        help="Group PDF receipts whose scan times are within this many minutes (default: 5)",
    )
    p_mrg.add_argument("--dry-run", action="store_true", help="Show groups without merging")

    # exhaust
    p_exh = sub.add_parser(
        "exhaust",
        help="Move product docs / non-receipt PDF receipts to the exhaust folder",
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
        help="One or more receipt filenames to suppress (e.g. 20110729_1149.pdf)",
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

    # correct
    p_cor = sub.add_parser(
        "correct",
        help="Apply a manual correction, sync the sidecar, and record an audit event",
    )
    p_cor.add_argument("filename", metavar="FILENAME", help="Receipt filename to correct")
    p_cor.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_cor.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_cor.add_argument("--merchant", metavar="NAME", help="Corrected merchant name")
    p_cor.add_argument("--total", metavar="AMOUNT", type=float, help="Corrected total amount")
    p_cor.add_argument("--category", metavar="PATH", help="Corrected category/subcategory path")
    p_cor.add_argument("--purchaser", metavar="NAME", help="Corrected purchaser name")
    p_cor.add_argument(
        "--clear-purchaser",
        action="store_true",
        help="Clear the stored purchaser value",
    )
    p_cor.add_argument("--card-last4", metavar="LAST4", help="Corrected card last4 value")
    p_cor.add_argument(
        "--clear-card-last4",
        action="store_true",
        help="Clear the stored card_last4 value",
    )
    p_cor.add_argument(
        "--currency",
        metavar="CODE",
        help="Corrected ISO-4217 currency code (e.g. USD, CAD, EUR, JPY)",
    )
    p_cor.add_argument(
        "--certification",
        action="append",
        metavar="SPEC",
        help="Add or replace certifications, e.g. 'AAA=AAA certified' (repeatable)",
    )
    p_cor.add_argument(
        "--clear-certifications",
        action="store_true",
        help="Clear the stored certifications",
    )
    p_cor.add_argument("--note", metavar="TEXT", help="Optional audit note for this correction")

    # history
    p_hist = sub.add_parser(
        "history",
        help="Show the recorded audit trail for a receipt",
    )
    p_hist.add_argument("filename", metavar="FILENAME", help="Receipt filename")
    p_hist.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_hist.add_argument(
        "--limit",
        metavar="N",
        type=int,
        default=20,
        help="Maximum audit events to show (default: 20)",
    )

    # normalize-filenames
    p_norm = sub.add_parser(
        "normalize-filenames",
        help="Rename tracked receipts to the canonical YYYYMMDD_HHMM.EXT format",
    )
    p_norm.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_norm.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_norm.add_argument("--dry-run", action="store_true", help="Print proposed renames only")

    # dedup
    p_ded = sub.add_parser(
        "dedup",
        help="Detect (and optionally merge) duplicate receipt rows in the database",
    )
    p_ded.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_ded.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_ded.add_argument(
        "--window",
        metavar="DAYS",
        default=3,
        help="Max days between scan dates to consider rows duplicates (default: 3)",
    )
    p_ded.add_argument(
        "--merge",
        action="store_true",
        help="Merge fields into the best row and delete the rest (receipt files untouched)",
    )
    p_ded.add_argument(
        "--remove",
        action="store_true",
        help="Implies --merge; move duplicate artifacts to exhaust/duplicates and audit removal",
    )
    p_ded.add_argument(
        "--hash-only",
        action="store_true",
        help="Only consider exact file-hash duplicate groups",
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
    p_aud = sub.add_parser("audit", help="List intake receipt files without processing them")
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

    p_em_prune = p_em_sub.add_parser(
        "prune-nonreceipts",
        help="Move already-ingested scheduled/upcoming email notices out of receipts",
    )
    p_em_prune.add_argument("--db", metavar="PATH", help="SQLite database path")
    p_em_prune.add_argument("--receipt-dir", metavar="PATH", help="Root receipt directory")
    p_em_prune.add_argument("--dry-run", action="store_true", help="Show changes without writing")

    args = parser.parse_args()
    settings = _load_settings()

    if args.command == "process":
        _cmd_process(args, settings)
    elif args.command == "report":
        _cmd_report(args, settings)
    elif args.command == "serve":
        _cmd_serve(args, settings)
    elif args.command == "backfill":
        _cmd_backfill(args, settings)
    elif args.command == "itemize":
        _cmd_itemize(args, settings)
    elif args.command == "certify":
        _cmd_certify(args, settings)
    elif args.command == "staff":
        _cmd_staff(args, settings)
    elif args.command == "normalize-purchasers":
        _cmd_normalize_purchasers(args, settings)
    elif args.command == "merge":
        _cmd_merge(args, settings)
    elif args.command == "exhaust":
        _cmd_exhaust(args, settings)
    elif args.command == "review":
        _cmd_review(args, settings)
    elif args.command == "suppress":
        _cmd_suppress(args, settings)
    elif args.command == "correct":
        _cmd_correct(args, settings)
    elif args.command == "history":
        _cmd_history(args, settings)
    elif args.command == "normalize-filenames":
        _cmd_normalize_filenames(args, settings)
    elif args.command == "dedup":
        _cmd_dedup(args, settings)
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
