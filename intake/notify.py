"""Outbound notifications for processed receipts."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from intake.parse import Receipt

_PORTFOLIO_SERVICE_HEADER = "X-Portfolio-Service: intake"
_CREW_CHIEF_INTENT_HEADER = "X-Crew-Chief-Intent: notify"
_SIGNAL_SERVICE_META = "cc-service: intake"
_SIGNAL_INTENT_META = "cc-intent: notify"


def _section_config(settings: dict, section: str) -> dict:
    cfg = settings.get(section)
    return cfg if isinstance(cfg, dict) else {}


def _configured_recipients(cfg: dict) -> list[str]:
    recipients: list[str] = []

    recipient = cfg.get("recipient")
    if isinstance(recipient, str) and recipient.strip():
        recipients.append(recipient.strip())

    extra = cfg.get("recipients")
    if isinstance(extra, list):
        recipients.extend(item.strip() for item in extra if isinstance(item, str) and item.strip())

    return recipients


def _signal_config(settings: dict) -> dict:
    return _section_config(settings, "signal")


def _signal_recipients(settings: dict) -> list[str]:
    return _configured_recipients(_signal_config(settings))


def _signal_note_to_self(settings: dict) -> bool:
    cfg = _signal_config(settings)
    return bool(cfg.get("note_to_self", False))


def _signal_message_with_metadata(message: str) -> str:
    metadata_lines = [_SIGNAL_SERVICE_META, _SIGNAL_INTENT_META]
    return "\n".join([*metadata_lines, "", message])


def _signal_cli_fields(config_path: Path) -> tuple[str, str]:
    """Read signal_cli.account and optional bus_name from config.local.yaml."""
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Signal config not found: {config_path}") from exc

    account = ""
    bus_name = ""
    in_signal_cli = False
    base_indent: int | None = None

    def _value(line: str) -> str:
        match = re.match(r"^\s*[^:]+:\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s#]+))\s*(?:#.*)?$", line)
        if not match:
            return ""
        return next(part for part in match.groups() if part is not None)

    for line in config_text.splitlines():
        if not in_signal_cli:
            if re.match(r"^\s*signal_cli:\s*$", line):
                in_signal_cli = True
                base_indent = len(line) - len(line.lstrip())
            continue

        if line.strip():
            indent = len(line) - len(line.lstrip())
            if (
                base_indent is not None
                and indent <= base_indent
                and not re.match(r"^\s*signal_cli:\s*$", line)
            ):
                break

        if re.match(r"^\s*account:\s*", line):
            account = _value(line)
        elif re.match(r"^\s*bus_name:\s*", line):
            bus_name = _value(line)

    if not account:
        raise RuntimeError(f"Missing signal_cli.account in {config_path}")

    return account, bus_name


def signal_notifications_enabled(settings: dict) -> bool:
    """Return True when Signal notifications are configured."""
    cfg = _signal_config(settings)
    if cfg.get("enabled", True) is False:
        return False
    return bool(cfg.get("shock_relay_config")) and (
        bool(_signal_recipients(settings)) or _signal_note_to_self(settings)
    )


def _gmail_config(settings: dict) -> dict:
    return _section_config(settings, "gmail")


def _gmail_recipients(settings: dict) -> list[str]:
    return _configured_recipients(_gmail_config(settings))


def _gmail_config_path(settings: dict) -> str:
    cfg = _gmail_config(settings)
    value = cfg.get("shock_relay_config")
    if isinstance(value, str) and value.strip():
        return value.strip()

    email_cfg = _section_config(settings, "email")
    fallback = email_cfg.get("shock_relay_config")
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()

    return ""


def gmail_notifications_enabled(settings: dict) -> bool:
    """Return True when Gmail notifications are configured."""
    cfg = _gmail_config(settings)
    if cfg.get("enabled", True) is False:
        return False
    return bool(_gmail_config_path(settings)) and bool(_gmail_recipients(settings))


def format_processed_receipt_message(
    receipt: Receipt,
    *,
    category: str,
    receipt_dir: str,
    filename: str,
    processed_by: str,
) -> str:
    """Build the notification body for a processed receipt."""
    total = f"${receipt.total:.2f}" if receipt.total is not None else "unknown"
    when = (receipt.receipt_date or receipt.scan_date).isoformat()
    if receipt.receipt_time is not None:
        when = f"{when} {receipt.receipt_time.strftime('%H:%M')}"

    lines = [
        "Receipt processed",
        f"Merchant: {receipt.merchant or 'unknown'}",
        f"Total: {total}",
        f"Category: {category}",
        f"When: {when}",
        f"File: {receipt_dir}",
        f"Model: {processed_by}",
    ]
    if receipt.purchaser:
        lines.append(f"Purchaser: {receipt.purchaser}")
    if receipt.card_last4:
        lines.append(f"Card: ****{receipt.card_last4}")
    if filename != Path(receipt_dir).name:
        lines.append(f"Filename: {filename}")
    return "\n".join(lines)


def format_processed_receipt_subject(
    receipt: Receipt,
    *,
    subject_prefix: str = "",
) -> str:
    """Build the Gmail subject line for a processed receipt."""
    merchant = receipt.merchant or "unknown merchant"
    total = f"${receipt.total:.2f}" if receipt.total is not None else "unknown total"
    return f"{subject_prefix}Receipt processed: {merchant} {total}"


def send_processed_receipt_notification(
    settings: dict,
    receipt: Receipt,
    *,
    category: str,
    receipt_dir: str,
    filename: str,
    processed_by: str,
) -> int:
    """Send a Signal notification for a processed receipt.

    Returns the number of recipients notified. Missing configuration is treated
    as a no-op so receipt processing can stay optional and local-first.
    """
    if not signal_notifications_enabled(settings):
        return 0

    cfg = _signal_config(settings)
    config_path = Path(str(cfg["shock_relay_config"])).expanduser()
    send_script = config_path.with_name("send_message.py")
    if not config_path.exists():
        raise RuntimeError(f"Signal config not found: {config_path}")

    message = format_processed_receipt_message(
        receipt,
        category=category,
        receipt_dir=receipt_dir,
        filename=filename,
        processed_by=processed_by,
    )

    recipients = _signal_recipients(settings)
    sent = 0
    if _signal_note_to_self(settings):
        account, bus_name = _signal_cli_fields(config_path)
        cmd = ["signal-cli", "-a", account]
        if bus_name:
            cmd.extend(["--bus-name", bus_name])
        cmd.extend(["send", "--note-to-self", "-m", _signal_message_with_metadata(message)])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            detail = f": {error}" if error else ""
            raise RuntimeError(f"Signal note-to-self notification failed{detail}")
        sent += 1

    if recipients and not send_script.exists():
        raise RuntimeError(f"Signal sender script not found: {send_script}")
    for recipient in recipients:
        cmd = [
            sys.executable,
            str(send_script),
            "--meta",
            _SIGNAL_SERVICE_META,
            "--meta",
            _SIGNAL_INTENT_META,
            "--config",
            str(config_path),
            recipient,
            message,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            detail = f": {error}" if error else ""
            raise RuntimeError(f"Signal notification failed for {recipient}{detail}")
        sent += 1

    return sent


def send_processed_receipt_gmail_notification(
    settings: dict,
    receipt: Receipt,
    *,
    category: str,
    receipt_dir: str,
    filename: str,
    processed_by: str,
) -> int:
    """Send a Gmail notification for a processed receipt."""
    if not gmail_notifications_enabled(settings):
        return 0

    cfg = _gmail_config(settings)
    config_path = Path(_gmail_config_path(settings)).expanduser()
    send_script = config_path.with_name("send_email.py")
    if not config_path.exists():
        raise RuntimeError(f"Gmail config not found: {config_path}")
    if not send_script.exists():
        raise RuntimeError(f"Gmail sender script not found: {send_script}")

    subject = format_processed_receipt_subject(
        receipt,
        subject_prefix=str(cfg.get("subject_prefix", "")),
    )
    message = format_processed_receipt_message(
        receipt,
        category=category,
        receipt_dir=receipt_dir,
        filename=filename,
        processed_by=processed_by,
    )

    sent = 0
    for recipient in _gmail_recipients(settings):
        cmd = [
            sys.executable,
            str(send_script),
            "--header",
            _PORTFOLIO_SERVICE_HEADER,
            "--header",
            _CREW_CHIEF_INTENT_HEADER,
            recipient,
            subject,
            message,
            "--config",
            str(config_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            detail = f": {error}" if error else ""
            raise RuntimeError(f"Gmail notification failed for {recipient}{detail}")
        sent += 1

    return sent


def send_processed_receipt_notifications(
    settings: dict,
    receipt: Receipt,
    *,
    category: str,
    receipt_dir: str,
    filename: str,
    processed_by: str,
) -> tuple[dict[str, int], dict[str, str]]:
    """Send best-effort notifications across all configured outbound channels."""
    sent: dict[str, int] = {}
    errors: dict[str, str] = {}
    params = {
        "category": category,
        "receipt_dir": receipt_dir,
        "filename": filename,
        "processed_by": processed_by,
    }

    for channel, sender in (
        ("signal", send_processed_receipt_notification),
        ("gmail", send_processed_receipt_gmail_notification),
    ):
        try:
            count = sender(settings, receipt, **params)
        except Exception as exc:  # noqa: BLE001
            errors[channel] = str(exc)
            continue
        if count:
            sent[channel] = count

    return sent, errors
