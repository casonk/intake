"""Email filing and artifact-save helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from intake.naming import allocate_canonical_path


def same_mailbox_label(left: str, right: str) -> bool:
    """Return True when two Gmail IMAP mailbox names refer to the same label."""
    return left.strip().strip('"').casefold() == right.strip().strip('"').casefold()


def file_message_to_processed_label(
    sr,
    conn,
    uid: int,
    *,
    source_mailbox: str,
    processed_label: str,
) -> bool:
    """Mark *uid* read and move it to *processed_label* when needed."""
    sr.mark_message_read(conn, uid)
    if not processed_label or same_mailbox_label(source_mailbox, processed_label):
        return False
    sr.move_message(conn, uid, processed_label)
    return True


def move_message_without_marking_read(
    sr,
    conn,
    uid: int,
    *,
    source_mailbox: str,
    processed_label: str,
) -> bool:
    """Move *uid* to *processed_label* without marking it read."""
    if not processed_label or same_mailbox_label(source_mailbox, processed_label):
        return False
    sr.move_message(conn, uid, processed_label)
    return True


def safe_save(dest_dir: Path, filename: str, data: bytes) -> Path:
    """Write *data* to *dest_dir/filename*, avoiding clobbers with a suffix."""
    safe = re.sub(r"[^\w\-. ]", "_", filename).strip() or "attachment.pdf"
    dest = dest_dir / safe
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        dest = dest_dir / f"{stem}_{ts}{suffix}"
    dest.write_bytes(data)
    return dest


def safe_save_receipt_pdf(
    dest_dir: Path,
    filename: str,
    data: bytes,
    *,
    received_at: str | None = None,
) -> Path:
    """Write a receipt PDF using the canonical ``YYYYMMDD_HHMM.pdf`` convention."""
    try:
        parsed = parsedate_to_datetime(received_at) if received_at else datetime.now()
        base_dt = parsed.replace(tzinfo=None) if isinstance(parsed, datetime) else datetime.now()
    except (TypeError, ValueError, IndexError, OverflowError):
        base_dt = datetime.now()
    dest = allocate_canonical_path(
        dest_dir,
        base_dt=base_dt.replace(second=0, microsecond=0, tzinfo=None),
        suffix=Path(filename).suffix or ".pdf",
    )
    dest.write_bytes(data)
    return dest
