"""SQLite schema, upsert, and query helpers for the intake receipt database."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from intake.purchaser import normalize_purchaser

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT UNIQUE NOT NULL,
    scan_date       TEXT NOT NULL,
    merchant        TEXT,
    total           REAL,
    category        TEXT NOT NULL,
    receipt_dir     TEXT NOT NULL,
    file_hash       TEXT,
    raw_text        TEXT,
    processed_by    TEXT NOT NULL DEFAULT 'tesseract',
    processed_at    TEXT NOT NULL
);
"""

_EMAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_receipts (
    uid             INTEGER NOT NULL,
    mailbox         TEXT NOT NULL,
    message_id      TEXT,
    subject         TEXT,
    sender          TEXT,
    received_at     TEXT,
    processed_at    TEXT NOT NULL,
    attachment_count INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'processed',
    PRIMARY KEY (uid, mailbox)
);
"""

_RECEIPT_UPDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipt_updates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    source          TEXT NOT NULL,
    note            TEXT,
    changes_json    TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipt_updates_filename_updated_at
    ON receipt_updates(filename, updated_at DESC);
"""

_MIGRATIONS = [
    "ALTER TABLE receipts ADD COLUMN processed_by TEXT NOT NULL DEFAULT 'tesseract'",
    "ALTER TABLE receipts ADD COLUMN purchaser TEXT",
    "ALTER TABLE receipts ADD COLUMN card_last4 TEXT",
    "ALTER TABLE receipts ADD COLUMN reviewed_at TEXT",  # ISO timestamp when suppressed; NULL = not reviewed
    "ALTER TABLE receipts ADD COLUMN file_hash TEXT",
    "CREATE INDEX IF NOT EXISTS idx_receipts_file_hash ON receipts(file_hash)",
]


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (and initialise if needed) the SQLite database at *db_path*."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.executescript(_EMAIL_SCHEMA)
    conn.executescript(_RECEIPT_UPDATE_SCHEMA)
    conn.commit()
    _run_migrations(conn)
    return conn


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply any pending schema migrations (idempotent)."""
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column / index already exists


def upsert_receipt(conn: sqlite3.Connection, data: dict) -> None:
    """Insert or replace a receipt record.

    *data* keys: filename, scan_date, merchant, total, category,
                 receipt_dir, raw_text, processed_by (optional),
                 purchaser (optional), card_last4 (optional),
                 file_hash (optional).
    processed_at is set automatically to now (UTC).
    """
    processed_at = datetime.now(UTC).isoformat()
    normalized_data = dict(data)
    if "purchaser" in normalized_data:
        normalized_data["purchaser"] = normalize_purchaser(normalized_data.get("purchaser"))
    conn.execute(
        """
        INSERT INTO receipts
            (filename, scan_date, merchant, total, category, receipt_dir, file_hash,
             raw_text, processed_by, purchaser, card_last4, processed_at)
        VALUES
            (:filename, :scan_date, :merchant, :total, :category, :receipt_dir, :file_hash,
             :raw_text, :processed_by, :purchaser, :card_last4, :processed_at)
        ON CONFLICT(filename) DO UPDATE SET
            scan_date    = excluded.scan_date,
            merchant     = excluded.merchant,
            total        = excluded.total,
            category     = excluded.category,
            receipt_dir  = excluded.receipt_dir,
            file_hash    = excluded.file_hash,
            raw_text     = excluded.raw_text,
            processed_by = excluded.processed_by,
            purchaser    = excluded.purchaser,
            card_last4   = excluded.card_last4,
            processed_at = excluded.processed_at
        """,
        {
            "processed_by": "tesseract",
            "purchaser": None,
            "card_last4": None,
            "file_hash": None,
            **normalized_data,
            "processed_at": processed_at,
        },
    )
    conn.commit()


def get_receipt(conn: sqlite3.Connection, filename: str) -> dict | None:
    """Return a receipt row by filename, or None if it does not exist."""
    row = conn.execute(
        """
        SELECT filename, scan_date, merchant, total, category, receipt_dir, file_hash,
               raw_text, processed_by, purchaser, card_last4, processed_at, reviewed_at
        FROM receipts
        WHERE filename = ?
        """,
        (filename,),
    ).fetchone()
    return dict(row) if row is not None else None


def get_receipt_by_file_hash(
    conn: sqlite3.Connection,
    file_hash: str,
    *,
    exclude_filename: str | None = None,
) -> dict | None:
    """Return a tracked receipt row for *file_hash*, or None if absent."""
    params: list[object] = [file_hash]
    where = "WHERE file_hash = ?"
    if exclude_filename is not None:
        where += " AND filename != ?"
        params.append(exclude_filename)
    row = conn.execute(
        f"""
        SELECT filename, scan_date, merchant, total, category, receipt_dir, file_hash,
               raw_text, processed_by, purchaser, card_last4, processed_at, reviewed_at
        FROM receipts
        {where}
        ORDER BY processed_at ASC, filename ASC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return dict(row) if row is not None else None


def infer_purchaser_from_card_last4(
    conn: sqlite3.Connection,
    card_last4: str | None,
    *,
    exclude_filename: str | None = None,
) -> str | None:
    """Return the most recent known purchaser for *card_last4*."""
    if not card_last4:
        return None

    params: list[object] = [card_last4]
    where = "WHERE card_last4 = ? AND purchaser IS NOT NULL AND TRIM(purchaser) != ''"
    if exclude_filename is not None:
        where += " AND filename != ?"
        params.append(exclude_filename)

    rows = conn.execute(
        f"""
        SELECT purchaser
        FROM receipts
        {where}
        ORDER BY scan_date DESC, processed_at DESC, filename DESC
        """,
        params,
    ).fetchall()
    for row in rows:
        purchaser = normalize_purchaser(row["purchaser"])
        if purchaser is not None:
            return purchaser
    return None


def record_receipt_update(
    conn: sqlite3.Connection,
    filename: str,
    *,
    source: str,
    changes: dict,
    note: str | None = None,
    updated_at: str | None = None,
) -> str:
    """Persist a field-level audit event for a receipt update."""
    if not changes:
        return updated_at or datetime.now(UTC).isoformat()

    ts = updated_at or datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO receipt_updates (filename, source, note, changes_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (filename, source, note, json.dumps(changes, sort_keys=True), ts),
    )
    conn.commit()
    return ts


def update_receipt_fields(
    conn: sqlite3.Connection,
    filename: str,
    updates: dict,
    *,
    source: str,
    note: str | None = None,
) -> tuple[dict, dict]:
    """Update receipt fields, bump processed_at, and write an audit event.

    Returns ``(row, changes)`` where *row* is the updated receipt and *changes*
    is the field-level diff that was recorded.
    """
    current = get_receipt(conn, filename)
    if current is None:
        raise KeyError(filename)

    allowed_fields = {
        "filename",
        "scan_date",
        "merchant",
        "total",
        "category",
        "receipt_dir",
        "file_hash",
        "raw_text",
        "processed_by",
        "purchaser",
        "card_last4",
        "reviewed_at",
    }
    unknown_fields = sorted(set(updates) - allowed_fields)
    if unknown_fields:
        joined = ", ".join(unknown_fields)
        raise ValueError(f"unsupported receipt field(s): {joined}")

    changes: dict[str, dict[str, object | None]] = {}
    assignments: list[str] = []
    params: list[object | None] = []
    for field, new_value in updates.items():
        if field == "purchaser":
            new_value = normalize_purchaser(new_value)
        old_value = current.get(field)
        if old_value == new_value:
            continue
        changes[field] = {"old": old_value, "new": new_value}
        assignments.append(f"{field} = ?")
        params.append(new_value)

    if not changes:
        return current, {}

    updated_at = datetime.now(UTC).isoformat()
    changes["processed_at"] = {"old": current.get("processed_at"), "new": updated_at}
    assignments.append("processed_at = ?")
    params.append(updated_at)
    params.append(filename)
    conn.execute(f"UPDATE receipts SET {', '.join(assignments)} WHERE filename = ?", params)
    conn.commit()
    audit_filename = str(updates.get("filename") or filename)
    record_receipt_update(
        conn,
        audit_filename,
        source=source,
        changes=changes,
        note=note,
        updated_at=updated_at,
    )
    updated = get_receipt(conn, audit_filename)
    assert updated is not None  # updated row must exist if the original row existed
    return updated, changes


def query_receipt_updates(
    conn: sqlite3.Connection,
    *,
    filename: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return recent receipt update audit events."""
    params: list[object] = []
    where = ""
    if filename is not None:
        where = "WHERE filename = ?"
        params.append(filename)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT filename, source, note, changes_json, updated_at
        FROM receipt_updates
        {where}
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["changes"] = json.loads(item.pop("changes_json"))
        results.append(item)
    return results


def suppress_receipts(conn: sqlite3.Connection, filenames: list[str]) -> int:
    """Mark *filenames* as manually reviewed; they are excluded from future review output."""
    reviewed_at = datetime.now(UTC).isoformat()
    conn.executemany(
        "UPDATE receipts SET reviewed_at = ? WHERE filename = ?",
        [(reviewed_at, f) for f in filenames],
    )
    conn.commit()
    return conn.total_changes


def unsuppress_receipts(conn: sqlite3.Connection, filenames: list[str]) -> int:
    """Clear the reviewed_at flag so records appear in review output again."""
    conn.executemany(
        "UPDATE receipts SET reviewed_at = NULL WHERE filename = ?",
        [(f,) for f in filenames],
    )
    conn.commit()
    return conn.total_changes


def query_summary(conn: sqlite3.Connection) -> dict:
    """Return aggregated summary data for report generation."""
    rows = lambda sql, *a: [dict(r) for r in conn.execute(sql, *a).fetchall()]  # noqa: E731  # fmt: skip

    total_receipts = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    total_spend = conn.execute(
        "SELECT COALESCE(SUM(total), 0) FROM receipts WHERE total IS NOT NULL"
    ).fetchone()[0]

    date_range = conn.execute("SELECT MIN(scan_date), MAX(scan_date) FROM receipts").fetchone()

    by_category = rows("""
        SELECT category,
               COUNT(*) AS count,
               COALESCE(SUM(total), 0) AS total_spend
        FROM receipts
        GROUP BY category
        ORDER BY total_spend DESC
        """)

    by_month = rows("""
        SELECT SUBSTR(scan_date, 1, 7) AS month,
               COUNT(*) AS count,
               COALESCE(SUM(total), 0) AS total_spend
        FROM receipts
        GROUP BY month
        ORDER BY month
        """)

    top_merchants = rows("""
        SELECT merchant,
               COUNT(*) AS count,
               COALESCE(SUM(total), 0) AS total_spend
        FROM receipts
        GROUP BY merchant
        ORDER BY total_spend DESC
        LIMIT 10
        """)

    by_model = rows("""
        SELECT COALESCE(processed_by, 'tesseract') AS model,
               COUNT(*) AS count
        FROM receipts
        GROUP BY model
        ORDER BY count DESC
        """)

    recent = rows("""
        SELECT scan_date, merchant, category, total, filename, receipt_dir,
               purchaser, card_last4, processed_by, processed_at
        FROM receipts
        ORDER BY scan_date DESC, processed_at DESC
        LIMIT 50
        """)

    return {
        "total_receipts": total_receipts,
        "total_spend": total_spend,
        "date_min": date_range[0],
        "date_max": date_range[1],
        "by_category": by_category,
        "by_month": by_month,
        "by_model": by_model,
        "top_merchants": top_merchants,
        "recent": recent,
    }


def query_flagged(conn: sqlite3.Connection, include_suppressed: bool = False) -> list[dict]:
    """Return rows that look suspicious for manual review.

    Flags any receipt where:
    - merchant is null, 'unknown', or 'receipt' (OCR placeholder)
    - merchant contains an @ sign (email leaked in)
    - merchant is very short (≤ 3 chars)
    - total is null or exactly 0.0

    Rows with a ``reviewed_at`` timestamp are suppressed from results unless
    *include_suppressed* is True.
    """
    suppressed_clause = "" if include_suppressed else "AND reviewed_at IS NULL"
    cursor = conn.execute(f"""
        SELECT
            id,
            filename,
            receipt_dir,
            scan_date,
            merchant,
            total,
            category,
            purchaser,
            card_last4,
            processed_by,
            processed_at,
            reviewed_at
        FROM receipts
        WHERE (
            merchant IS NULL
            OR LOWER(TRIM(merchant)) IN ('unknown', 'receipt', '')
            OR merchant LIKE '%@%'
            OR LENGTH(TRIM(merchant)) <= 3
            OR total IS NULL
            OR total = 0.0
        )
        {suppressed_clause}
        ORDER BY category, filename
        """)
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]


# ── email receipt helpers ──────────────────────────────────────────────────────


def is_email_processed(conn: sqlite3.Connection, uid: int, mailbox: str) -> bool:
    """Return True if this (uid, mailbox) pair is already in email_receipts."""
    row = conn.execute(
        "SELECT 1 FROM email_receipts WHERE uid = ? AND mailbox = ?",
        (uid, mailbox),
    ).fetchone()
    return row is not None


def record_email_receipt(
    conn: sqlite3.Connection,
    uid: int,
    mailbox: str,
    message_id: str,
    subject: str,
    sender: str,
    received_at: str,
    attachment_count: int,
    status: str = "processed",
) -> None:
    """Insert or replace a processed email record."""
    processed_at = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO email_receipts
            (uid, mailbox, message_id, subject, sender, received_at,
             processed_at, attachment_count, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            mailbox,
            message_id,
            subject,
            sender,
            received_at,
            processed_at,
            attachment_count,
            status,
        ),
    )
    conn.commit()


def query_email_stats(conn: sqlite3.Connection) -> dict:
    """Return summary stats for email ingestion."""
    row = conn.execute("SELECT COUNT(*), MAX(processed_at) FROM email_receipts").fetchone()
    total, last_run = (row[0], row[1]) if row else (0, None)
    by_status = conn.execute(
        "SELECT status, COUNT(*) FROM email_receipts GROUP BY status"
    ).fetchall()
    return {
        "total_processed": total,
        "last_run": last_run,
        "by_status": {r[0]: r[1] for r in by_status},
    }
