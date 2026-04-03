"""Best-effort downstream trigger for the personal-finance repo."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from intake.parse import Receipt

_INTAKE_REPO_ROOT = Path(__file__).resolve().parent.parent


def _pf_config(settings: dict) -> dict:
    cfg = settings.get("personal_finance")
    return cfg if isinstance(cfg, dict) else {}


def personal_finance_enabled(settings: dict) -> bool:
    """Return True when the personal-finance trigger is configured."""
    cfg = _pf_config(settings)
    if cfg.get("enabled", True) is False:
        return False
    return bool(cfg.get("repo_root"))


def _event_dir(settings: dict) -> Path:
    cfg = _pf_config(settings)
    value = cfg.get("event_dir")
    if value:
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else (_INTAKE_REPO_ROOT / path).resolve()
    return (_INTAKE_REPO_ROOT / "data" / "personal-finance").resolve()


def _repo_root(settings: dict) -> Path:
    cfg = _pf_config(settings)
    path = Path(str(cfg["repo_root"])).expanduser()
    return path if path.is_absolute() else (_INTAKE_REPO_ROOT / path).resolve()


def build_receipt_event(
    receipt: Receipt,
    *,
    category: str,
    receipt_dir: str,
    filename: str,
    processed_by: str,
    tier: str,
) -> dict:
    """Build the JSON payload for one processed receipt."""
    return {
        "filename": filename,
        "receipt_dir": receipt_dir,
        "scan_date": receipt.scan_date.isoformat(),
        "receipt_date": receipt.receipt_date.isoformat() if receipt.receipt_date else None,
        "receipt_time": receipt.receipt_time.strftime("%H:%M") if receipt.receipt_time else None,
        "merchant": receipt.merchant,
        "total": receipt.total,
        "category": category,
        "processed_by": processed_by,
        "tier": tier,
        "purchaser": receipt.purchaser,
        "card_last4": receipt.card_last4,
    }


def _write_receipts_json(settings: dict, receipts: list[dict], *, trigger: str) -> Path:
    event_dir = _event_dir(settings)
    event_dir.mkdir(parents=True, exist_ok=True)
    batch_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = event_dir / f"receipts-{batch_id}.json"
    latest_path = event_dir / "latest.json"
    payload = {
        "source": "intake",
        "trigger": trigger,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "receipt_count": len(receipts),
        "receipts": receipts,
    }
    rendered = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    json_path.write_text(rendered, encoding="utf-8")
    latest_path.write_text(rendered, encoding="utf-8")
    return json_path


def _render_command(settings: dict, json_path: Path, *, trigger: str, receipt_count: int) -> list[str]:
    cfg = _pf_config(settings)
    template = cfg.get("command")
    if template is None:
        return [sys.executable, "scripts/all/monthly_controller.py"]
    if not isinstance(template, list) or not all(isinstance(item, str) for item in template):
        raise RuntimeError("personal_finance.command must be a TOML string array.")
    tokens = {
        "receipts_json": str(json_path),
        "receipt_count": str(receipt_count),
        "trigger": trigger,
    }
    return [part.format(**tokens) for part in template]


def trigger_personal_finance(settings: dict, receipts: list[dict], *, trigger: str) -> Path | None:
    """Write receipt JSON and invoke the configured personal-finance command."""
    if not receipts or not personal_finance_enabled(settings):
        return None

    repo_root = _repo_root(settings)
    if not repo_root.exists():
        raise RuntimeError(f"personal-finance repo_root not found: {repo_root}")

    json_path = _write_receipts_json(settings, receipts, trigger=trigger)
    cmd = _render_command(settings, json_path, trigger=trigger, receipt_count=len(receipts))
    timeout = int(_pf_config(settings).get("timeout_seconds", 1800))

    env = os.environ.copy()
    env.update(
        {
            "INTAKE_RECEIPTS_JSON": str(json_path),
            "INTAKE_RECEIPT_COUNT": str(len(receipts)),
            "INTAKE_TRIGGER": trigger,
        }
    )

    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"personal-finance trigger timed out after {timeout}s") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            detail = detail.splitlines()[-1]
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"personal-finance trigger failed with exit code {result.returncode}{suffix}"
        )

    return json_path
