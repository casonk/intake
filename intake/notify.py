"""Outbound notifications for processed receipts."""

from __future__ import annotations

import re
import subprocess
import sys
from email.utils import parseaddr
from pathlib import Path

from intake.file_links import (
    build_receipt_companion_filebrowser_links,
    build_receipt_filebrowser_link,
)
from intake.parse import Receipt

_PORTFOLIO_SERVICE_HEADER = "X-Portfolio-Service: intake"
_CREW_CHIEF_INTENT_HEADER = "X-Crew-Chief-Intent: notify"
_INTAKE_FILENAME_HEADER = "X-Intake-Receipt-Filename"
_INTAKE_RECEIPT_DIR_HEADER = "X-Intake-Receipt-Dir"
_INTAKE_REPLY_INTENT_HEADER = "X-Crew-Chief-Intent: reply-link"
_SIGNAL_SERVICE_META = "cc-service: intake"
_SIGNAL_INTENT_META = "cc-intent: notify"
_ARTIFACT_LINK_LABELS = {
    "ocr": "OCR",
    "sidecar_json": "Sidecar JSON",
    "ai_json": "AI JSON",
}


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


def gmail_reply_available(settings: dict) -> bool:
    """Return True when Gmail reply sending is configured."""
    return bool(_gmail_config_path(settings))


def format_processed_receipt_message(
    receipt: Receipt,
    *,
    category: str,
    receipt_dir: str,
    filename: str,
    processed_by: str,
    snowbridge_link: str | None = None,
    artifact_links: dict[str, str] | None = None,
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
    if snowbridge_link:
        lines.append(f"Link: {snowbridge_link}")
    lines.extend(_format_artifact_link_lines(artifact_links))
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


def format_receipt_file_link_reply_message(
    receipt_row: dict,
    *,
    snowbridge_link: str,
    artifact_links: dict[str, str] | None = None,
) -> str:
    """Build the reply body for a requested Snowbridge receipt link."""
    filename = str(receipt_row.get("filename") or "unknown")
    merchant = str(receipt_row.get("merchant") or "unknown")
    total = receipt_row.get("total")
    total_display = f"${float(total):.2f}" if total is not None else "unknown"
    lines = [
        "Snowbridge receipt link requested",
        f"Receipt: {filename}",
        f"Merchant: {merchant}",
        f"Total: {total_display}",
        f"Link: {snowbridge_link}",
    ]
    lines.extend(_format_artifact_link_lines(artifact_links))
    return "\n".join(lines)


def _format_artifact_link_lines(artifact_links: dict[str, str] | None) -> list[str]:
    """Return stable notification lines for optional OCR/JSON artifacts."""
    if not artifact_links:
        return []
    lines: list[str] = []
    for key, label in _ARTIFACT_LINK_LABELS.items():
        link = artifact_links.get(key)
        if link:
            lines.append(f"{label}: {link}")
    return lines


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

    snowbridge_link = build_receipt_filebrowser_link(
        settings,
        {
            "filename": filename,
            "receipt_dir": receipt_dir,
            "category": category,
        },
    )

    message = format_processed_receipt_message(
        receipt,
        category=category,
        receipt_dir=receipt_dir,
        filename=filename,
        processed_by=processed_by,
        snowbridge_link=snowbridge_link,
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

    snowbridge_link = build_receipt_filebrowser_link(
        settings,
        {
            "filename": filename,
            "receipt_dir": receipt_dir,
            "category": category,
        },
    )
    artifact_links = build_receipt_companion_filebrowser_links(
        settings,
        {
            "filename": filename,
            "receipt_dir": receipt_dir,
            "category": category,
        },
    )

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
        snowbridge_link=snowbridge_link,
        artifact_links=artifact_links,
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
            "--header",
            f"{_INTAKE_FILENAME_HEADER}: {filename}",
            "--header",
            f"{_INTAKE_RECEIPT_DIR_HEADER}: {receipt_dir}",
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


def send_receipt_file_link_gmail_reply(
    settings: dict,
    *,
    recipient: str,
    subject: str,
    receipt_row: dict,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Send a Gmail reply containing the Snowbridge link for a tracked receipt."""
    if not gmail_reply_available(settings):
        raise RuntimeError("Gmail reply sending is not configured")

    recipient_address = parseaddr(recipient)[1].strip()
    if not recipient_address:
        raise RuntimeError(f"Invalid reply recipient: {recipient!r}")

    config_path = Path(_gmail_config_path(settings)).expanduser()
    send_script = config_path.with_name("send_email.py")
    if not config_path.exists():
        raise RuntimeError(f"Gmail config not found: {config_path}")
    if not send_script.exists():
        raise RuntimeError(f"Gmail sender script not found: {send_script}")

    snowbridge_link = build_receipt_filebrowser_link(settings, receipt_row)
    if not snowbridge_link:
        raise RuntimeError(
            f"Could not resolve Snowbridge link for receipt {receipt_row.get('filename')!r}"
        )
    artifact_links = build_receipt_companion_filebrowser_links(settings, receipt_row)

    headers = [
        _PORTFOLIO_SERVICE_HEADER,
        _INTAKE_REPLY_INTENT_HEADER,
    ]
    filename = str(receipt_row.get("filename") or "").strip()
    if filename:
        headers.extend(["--header", f"{_INTAKE_FILENAME_HEADER}: {filename}"])
    receipt_dir = str(receipt_row.get("receipt_dir") or "").strip()
    if receipt_dir:
        headers.extend(["--header", f"{_INTAKE_RECEIPT_DIR_HEADER}: {receipt_dir}"])
    if in_reply_to:
        headers.extend(["--header", f"In-Reply-To: {in_reply_to}"])
    if references:
        headers.extend(["--header", f"References: {references}"])

    message = format_receipt_file_link_reply_message(
        receipt_row,
        snowbridge_link=snowbridge_link,
        artifact_links=artifact_links,
    )
    cmd = [
        sys.executable,
        str(send_script),
        "--header",
        headers[0],
        "--header",
        headers[1],
    ]
    cmd.extend(headers[2:])
    cmd.extend(
        [
            recipient_address,
            subject,
            message,
            "--config",
            str(config_path),
        ]
    )

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "").strip()
        detail = f": {error}" if error else ""
        raise RuntimeError(f"Gmail reply failed for {recipient_address}{detail}")
    return snowbridge_link


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


def _format_corrupt_intake_message(filenames: list[str]) -> str:
    lines = [
        "Corrupt intake file(s) detected (unreadable — upload may have failed or be incomplete)"
    ]
    for name in filenames:
        lines.append(f"  - {name}")
    lines.append("Re-upload and run `intake process` again.")
    return "\n".join(lines)


def send_corrupt_intake_signal_notification(settings: dict, filenames: list[str]) -> int:
    """Send a Signal alert for zero-byte files found in the intake folder."""
    if not signal_notifications_enabled(settings):
        return 0

    cfg = _signal_config(settings)
    config_path = Path(str(cfg["shock_relay_config"])).expanduser()
    send_script = config_path.with_name("send_message.py")
    if not config_path.exists():
        raise RuntimeError(f"Signal config not found: {config_path}")

    message = _signal_message_with_metadata(_format_corrupt_intake_message(filenames))
    sent = 0

    if _signal_note_to_self(settings):
        account, bus_name = _signal_cli_fields(config_path)
        cmd = ["signal-cli", "-a", account]
        if bus_name:
            cmd.extend(["--bus-name", bus_name])
        cmd.extend(["send", "--note-to-self", "-m", message])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            detail = f": {error}" if error else ""
            raise RuntimeError(f"Signal note-to-self notification failed{detail}")
        sent += 1

    if _signal_recipients(settings) and not send_script.exists():
        raise RuntimeError(f"Signal sender script not found: {send_script}")
    for recipient in _signal_recipients(settings):
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


def send_corrupt_intake_gmail_notification(settings: dict, filenames: list[str]) -> int:
    """Send a Gmail alert for zero-byte files found in the intake folder."""
    if not gmail_notifications_enabled(settings):
        return 0

    config_path = Path(_gmail_config_path(settings)).expanduser()
    send_script = config_path.with_name("send_email.py")
    if not config_path.exists():
        raise RuntimeError(f"Gmail config not found: {config_path}")
    if not send_script.exists():
        raise RuntimeError(f"Gmail sender script not found: {send_script}")

    cfg = _gmail_config(settings)
    subject_prefix = str(cfg.get("subject_prefix", ""))
    count_label = f"{len(filenames)} file" + ("s" if len(filenames) != 1 else "")
    subject = f"{subject_prefix}Corrupt intake file(s): {count_label} need re-upload"
    message = _format_corrupt_intake_message(filenames)

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


def send_corrupt_intake_notifications(
    settings: dict, filenames: list[str]
) -> tuple[dict[str, int], dict[str, str]]:
    """Send best-effort corrupt-file alerts across all configured channels."""
    sent: dict[str, int] = {}
    errors: dict[str, str] = {}

    for channel, sender in (
        ("signal", send_corrupt_intake_signal_notification),
        ("gmail", send_corrupt_intake_gmail_notification),
    ):
        try:
            count = sender(settings, filenames)
        except Exception as exc:  # noqa: BLE001
            errors[channel] = str(exc)
            continue
        if count:
            sent[channel] = count

    return sent, errors
