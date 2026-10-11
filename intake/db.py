"""SQLite schema, upsert, and query helpers for the intake receipt database."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median

from intake.certification import receipt_certifications_from_dicts, receipt_certifications_to_dicts
from intake.parse import receipt_items_from_dicts, receipt_items_to_dicts
from intake.purchaser import (
    extract_billed_to_name,
    has_registered_purchaser_aliases,
    is_known_purchaser_name,
    normalize_purchaser,
)
from intake.staff import receipt_staff_from_dicts, receipt_staff_to_dicts

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
    items_json      TEXT,
    certifications_json TEXT,
    staff_json      TEXT,
    language        TEXT NOT NULL DEFAULT 'und',
    translated_text TEXT,
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

_LETTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS letters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT UNIQUE NOT NULL,
    scan_date       TEXT NOT NULL,
    letter_date     TEXT,
    sender          TEXT,
    recipient       TEXT,
    subject         TEXT,
    salutation      TEXT,
    body_excerpt    TEXT,
    category        TEXT NOT NULL DEFAULT 'letters/general',
    receipt_dir     TEXT NOT NULL,
    file_hash       TEXT,
    raw_text        TEXT,
    processed_by    TEXT NOT NULL DEFAULT 'tesseract',
    processed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_letters_file_hash ON letters(file_hash);
"""

_CARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT UNIQUE NOT NULL,
    scan_date       TEXT NOT NULL,
    card_date       TEXT,
    occasion        TEXT,
    sender          TEXT,
    recipient       TEXT,
    message_excerpt TEXT,
    category        TEXT NOT NULL DEFAULT 'cards/general',
    receipt_dir     TEXT NOT NULL,
    file_hash       TEXT,
    raw_text        TEXT,
    processed_by    TEXT NOT NULL DEFAULT 'tesseract',
    processed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_file_hash ON cards(file_hash);
"""

_PAYMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    filename            TEXT UNIQUE NOT NULL,
    scan_date           TEXT NOT NULL,
    payment_date        TEXT,
    amount              REAL,
    account_name        TEXT,
    account_last4       TEXT,
    payment_type        TEXT,
    confirmation_number TEXT,
    payer               TEXT,
    category            TEXT NOT NULL DEFAULT 'payments/other',
    receipt_dir         TEXT NOT NULL,
    file_hash           TEXT,
    raw_text            TEXT,
    processed_by        TEXT NOT NULL DEFAULT 'tesseract',
    processed_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_file_hash ON payments(file_hash);
"""

_DOCUMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT UNIQUE NOT NULL,
    scan_date       TEXT NOT NULL,
    document_date   TEXT,
    document_kind   TEXT NOT NULL DEFAULT 'documents/reference',
    title           TEXT,
    issuer          TEXT,
    subject         TEXT,
    model_number    TEXT,
    serial_number   TEXT,
    body_excerpt    TEXT,
    category        TEXT NOT NULL DEFAULT 'documents/reference',
    receipt_dir     TEXT NOT NULL,
    file_hash       TEXT,
    raw_text        TEXT,
    processed_by    TEXT NOT NULL DEFAULT 'tesseract',
    processed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_file_hash ON documents(file_hash);
"""

_DISMISSED_REFERENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS dismissed_reference_matches (
    kind          TEXT NOT NULL,
    ref_id        TEXT NOT NULL,
    note          TEXT,
    dismissed_at  TEXT NOT NULL,
    PRIMARY KEY (kind, ref_id)
);
"""

_MIGRATIONS = [
    "ALTER TABLE receipts ADD COLUMN processed_by TEXT NOT NULL DEFAULT 'tesseract'",
    "ALTER TABLE receipts ADD COLUMN purchaser TEXT",
    "ALTER TABLE receipts ADD COLUMN card_last4 TEXT",
    "ALTER TABLE receipts ADD COLUMN reviewed_at TEXT",  # ISO timestamp when suppressed; NULL = not reviewed
    "ALTER TABLE receipts ADD COLUMN file_hash TEXT",
    "ALTER TABLE receipts ADD COLUMN items_json TEXT",
    "ALTER TABLE receipts ADD COLUMN certifications_json TEXT",
    "CREATE INDEX IF NOT EXISTS idx_receipts_file_hash ON receipts(file_hash)",
    "ALTER TABLE receipts ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'",
    "ALTER TABLE receipts ADD COLUMN staff_json TEXT",
    "ALTER TABLE receipts ADD COLUMN language TEXT NOT NULL DEFAULT 'und'",
    "ALTER TABLE receipts ADD COLUMN translated_text TEXT",
]


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (and initialise if needed) the SQLite database at *db_path*."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.executescript(_EMAIL_SCHEMA)
    conn.executescript(_RECEIPT_UPDATE_SCHEMA)
    conn.executescript(_LETTER_SCHEMA)
    conn.executescript(_CARD_SCHEMA)
    conn.executescript(_PAYMENT_SCHEMA)
    conn.executescript(_DOCUMENT_SCHEMA)
    conn.executescript(_DISMISSED_REFERENCE_SCHEMA)
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


def _serialize_items(items: object | None) -> str | None:
    """Return a normalized JSON payload for itemized receipt lines."""
    if items is None:
        return None
    return json.dumps(receipt_items_to_dicts(items), ensure_ascii=True, sort_keys=True)


def _decode_items_json(items_json: str | None) -> list[dict] | None:
    """Decode stored receipt item JSON, preserving NULL as "not attempted"."""
    if items_json is None:
        return None
    try:
        payload = json.loads(items_json)
    except json.JSONDecodeError:
        return []
    return receipt_items_to_dicts(receipt_items_from_dicts(payload))


def _serialize_certifications(certifications: object | None) -> str | None:
    """Return a normalized JSON payload for receipt certifications."""
    if certifications is None:
        return None
    return json.dumps(
        receipt_certifications_to_dicts(certifications),
        ensure_ascii=True,
        sort_keys=True,
    )


def _decode_certifications_json(certifications_json: str | None) -> list[dict] | None:
    """Decode stored certification JSON, preserving NULL as "not attempted/present"."""
    if certifications_json is None:
        return None
    try:
        payload = json.loads(certifications_json)
    except json.JSONDecodeError:
        return []
    return receipt_certifications_to_dicts(receipt_certifications_from_dicts(payload))


def _serialize_staff(staff: object | None) -> str | None:
    """Return a normalized JSON payload for receipt staff/service metadata."""
    if staff is None:
        return None
    return json.dumps(receipt_staff_to_dicts(staff), ensure_ascii=True, sort_keys=True)


def _decode_staff_json(staff_json: str | None) -> list[dict] | None:
    """Decode stored staff JSON, preserving NULL as "not attempted/present"."""
    if staff_json is None:
        return None
    try:
        payload = json.loads(staff_json)
    except json.JSONDecodeError:
        return []
    return receipt_staff_to_dicts(receipt_staff_from_dicts(payload))


def _decode_receipt_row(row: sqlite3.Row) -> dict:
    """Return a JSON-friendly receipt row with decoded itemization."""
    item = dict(row)
    item["items"] = _decode_items_json(item.get("items_json"))
    item["certifications"] = _decode_certifications_json(item.get("certifications_json"))
    item["staff"] = _decode_staff_json(item.get("staff_json"))
    return item


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
    if "merchant" in normalized_data and normalized_data.get("merchant") is not None:
        normalized_data["merchant"] = str(normalized_data["merchant"]).strip().lower()[:120]
    if "purchaser" in normalized_data:
        normalized_data["purchaser"] = normalize_purchaser(normalized_data.get("purchaser"))
    if "language" in normalized_data:
        normalized_data["language"] = str(normalized_data.get("language") or "und").strip().lower()
    if "translated_text" in normalized_data and normalized_data.get("translated_text") is not None:
        normalized_data["translated_text"] = str(normalized_data["translated_text"]).strip() or None
    items_json = _serialize_items(normalized_data.pop("items", None))
    if items_json is None and "items_json" in normalized_data:
        items_json = _serialize_items(_decode_items_json(normalized_data.pop("items_json")))
    certifications_json = _serialize_certifications(normalized_data.pop("certifications", None))
    if certifications_json is None and "certifications_json" in normalized_data:
        certifications_json = _serialize_certifications(
            _decode_certifications_json(normalized_data.pop("certifications_json"))
        )
    staff_json = _serialize_staff(normalized_data.pop("staff", None))
    if staff_json is None and "staff_json" in normalized_data:
        staff_json = _serialize_staff(_decode_staff_json(normalized_data.pop("staff_json")))
    conn.execute(
        """
        INSERT INTO receipts
            (filename, scan_date, merchant, total, category, receipt_dir, file_hash,
             raw_text, items_json, certifications_json, staff_json, processed_by, purchaser, card_last4,
             currency, language, translated_text, processed_at)
        VALUES
            (:filename, :scan_date, :merchant, :total, :category, :receipt_dir, :file_hash,
             :raw_text, :items_json, :certifications_json, :staff_json, :processed_by, :purchaser, :card_last4,
             :currency, :language, :translated_text, :processed_at)
        ON CONFLICT(filename) DO UPDATE SET
            scan_date    = excluded.scan_date,
            merchant     = excluded.merchant,
            total        = excluded.total,
            category     = excluded.category,
            receipt_dir  = excluded.receipt_dir,
            file_hash    = excluded.file_hash,
            raw_text     = excluded.raw_text,
            items_json   = COALESCE(excluded.items_json, receipts.items_json),
            certifications_json = COALESCE(excluded.certifications_json, receipts.certifications_json),
            staff_json   = COALESCE(excluded.staff_json, receipts.staff_json),
            processed_by = excluded.processed_by,
            purchaser    = excluded.purchaser,
            card_last4   = excluded.card_last4,
            currency     = excluded.currency,
            language     = excluded.language,
            translated_text = excluded.translated_text,
            processed_at = excluded.processed_at
        """,
        {
            "processed_by": "tesseract",
            "purchaser": None,
            "card_last4": None,
            "currency": "USD",
            "language": "und",
            "translated_text": None,
            "file_hash": None,
            "items_json": items_json,
            "certifications_json": certifications_json,
            "staff_json": staff_json,
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
               raw_text, items_json, certifications_json, staff_json, processed_by, purchaser, card_last4,
               currency, language, translated_text, processed_at, reviewed_at
        FROM receipts
        WHERE filename = ?
        """,
        (filename,),
    ).fetchone()
    return _decode_receipt_row(row) if row is not None else None


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
               raw_text, items_json, certifications_json, staff_json, processed_by, purchaser, card_last4,
               currency, language, translated_text, processed_at, reviewed_at
        FROM receipts
        {where}
        ORDER BY processed_at ASC, filename ASC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return _decode_receipt_row(row) if row is not None else None


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
        "currency",
        "language",
        "translated_text",
        "items",
        "certifications",
        "staff",
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
        db_field = field
        if field == "purchaser":
            new_value = normalize_purchaser(new_value)
        if field == "items":
            db_field = "items_json"
            new_value = _serialize_items(new_value)
            old_value = current.get("items")
            new_display_value = _decode_items_json(new_value)
        elif field == "certifications":
            db_field = "certifications_json"
            new_value = _serialize_certifications(new_value)
            old_value = current.get("certifications")
            new_display_value = _decode_certifications_json(new_value)
        elif field == "staff":
            db_field = "staff_json"
            new_value = _serialize_staff(new_value)
            old_value = current.get("staff")
            new_display_value = _decode_staff_json(new_value)
        else:
            old_value = current.get(field)
            new_display_value = new_value
        if old_value == new_display_value:
            continue
        changes[field] = {"old": old_value, "new": new_display_value}
        assignments.append(f"{db_field} = ?")
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
    total_spend = conn.execute("""
        SELECT COALESCE(SUM(total), 0)
        FROM receipts
        WHERE total IS NOT NULL AND UPPER(COALESCE(currency, 'USD')) = 'USD'
        """).fetchone()[0]

    date_range = conn.execute("SELECT MIN(scan_date), MAX(scan_date) FROM receipts").fetchone()

    by_category = rows("""
        SELECT category,
               COUNT(*) AS count,
               SUM(CASE WHEN total IS NOT NULL
                         AND UPPER(COALESCE(currency, 'USD')) = 'USD'
                        THEN 1 ELSE 0 END) AS known_total_count,
               COALESCE(SUM(CASE WHEN UPPER(COALESCE(currency, 'USD')) = 'USD'
                                 THEN total ELSE 0 END), 0) AS total_spend
        FROM receipts
        GROUP BY category
        ORDER BY total_spend DESC
        """)

    by_month = rows("""
        SELECT SUBSTR(scan_date, 1, 7) AS month,
               COUNT(*) AS count,
               SUM(CASE WHEN total IS NOT NULL
                         AND UPPER(COALESCE(currency, 'USD')) = 'USD'
                        THEN 1 ELSE 0 END) AS known_total_count,
               COALESCE(SUM(CASE WHEN UPPER(COALESCE(currency, 'USD')) = 'USD'
                                 THEN total ELSE 0 END), 0) AS total_spend
        FROM receipts
        GROUP BY month
        ORDER BY month
        """)

    all_merchants = rows("""
        SELECT merchant,
               COUNT(*) AS count,
               SUM(CASE WHEN total IS NOT NULL
                         AND UPPER(COALESCE(currency, 'USD')) = 'USD'
                        THEN 1 ELSE 0 END) AS known_total_count,
               COALESCE(SUM(CASE WHEN UPPER(COALESCE(currency, 'USD')) = 'USD'
                                 THEN total ELSE 0 END), 0) AS total_spend,
               MIN(scan_date) AS date_min,
               MAX(scan_date) AS date_max,
               COUNT(DISTINCT SUBSTR(scan_date, 1, 7)) AS active_months
        FROM receipts
        GROUP BY merchant
        ORDER BY total_spend DESC, count DESC, merchant
        """)

    top_merchants = all_merchants[:10]

    merchant_by_month = rows("""
        SELECT merchant,
               SUBSTR(scan_date, 1, 7) AS month,
               COUNT(*) AS count,
               SUM(CASE WHEN total IS NOT NULL
                         AND UPPER(COALESCE(currency, 'USD')) = 'USD'
                        THEN 1 ELSE 0 END) AS known_total_count,
               COALESCE(SUM(CASE WHEN UPPER(COALESCE(currency, 'USD')) = 'USD'
                                 THEN total ELSE 0 END), 0) AS total_spend
        FROM receipts
        GROUP BY merchant, month
        ORDER BY merchant, month
        """)

    by_model = rows("""
        SELECT COALESCE(processed_by, 'tesseract') AS model,
               COUNT(*) AS count
        FROM receipts
        GROUP BY model
        ORDER BY count DESC
        """)

    by_purchaser = rows("""
        SELECT COALESCE(purchaser, 'Unknown') AS purchaser,
               COUNT(*) AS count,
               SUM(CASE WHEN total IS NOT NULL
                         AND UPPER(COALESCE(currency, 'USD')) = 'USD'
                        THEN 1 ELSE 0 END) AS known_total_count,
               COALESCE(SUM(CASE WHEN UPPER(COALESCE(currency, 'USD')) = 'USD'
                                 THEN total ELSE 0 END), 0) AS total_spend
        FROM receipts
        GROUP BY purchaser
        ORDER BY total_spend DESC, count DESC
        """)

    recent = rows("""
        SELECT scan_date, merchant, category, total, currency, filename, receipt_dir,
               purchaser, card_last4, language, translated_text,
               items_json, certifications_json, staff_json,
               processed_by, processed_at
        FROM receipts
        ORDER BY scan_date DESC, processed_at DESC
        LIMIT 50
        """)

    merchant_receipts = rows("""
        SELECT merchant, scan_date, category, total, currency, filename, receipt_dir,
               purchaser, card_last4, language, translated_text,
               items_json, certifications_json, staff_json,
               processed_by, processed_at
        FROM receipts
        ORDER BY COALESCE(merchant, ''), scan_date DESC, processed_at DESC, filename DESC
        """)

    item_rows = rows("""
        SELECT items_json
        FROM receipts
        """)
    itemization_attempted = 0
    itemized_receipts = 0
    total_line_items = 0
    for row in item_rows:
        items = _decode_items_json(row.get("items_json"))
        if items is None:
            continue
        itemization_attempted += 1
        if items:
            itemized_receipts += 1
            total_line_items += len(items)

    return {
        "total_receipts": total_receipts,
        "total_spend": total_spend,
        "date_min": date_range[0],
        "date_max": date_range[1],
        "itemization_attempted": itemization_attempted,
        "itemized_receipts": itemized_receipts,
        "total_line_items": total_line_items,
        "by_category": by_category,
        "by_month": by_month,
        "by_model": by_model,
        "by_purchaser": by_purchaser,
        "all_merchants": all_merchants,
        "top_merchants": top_merchants,
        "merchant_by_month": merchant_by_month,
        "merchant_receipts": merchant_receipts,
        "recent": recent,
    }


def query_all_receipts(conn: sqlite3.Connection) -> list[dict]:
    """Return all receipts for the audit page, newest first."""
    rows = conn.execute("""
            SELECT scan_date, merchant, category, total, currency, filename, receipt_dir,
                   purchaser, card_last4, language, translated_text, items_json, staff_json,
                   processed_by, processed_at, reviewed_at
            FROM receipts
            ORDER BY scan_date DESC, processed_at DESC, filename DESC
        """).fetchall()
    return [dict(row) for row in rows]


def dismiss_reference_match(
    conn: sqlite3.Connection, kind: str, ref_id: str, note: str | None = None
) -> None:
    """Mark a (kind, ref_id) reference-match group as reviewed and not a real
    duplicate — e.g. a recurring tag/license number, or a shared invoice-series
    prefix — so `dedup-refs` stops resurfacing it."""
    conn.execute(
        """
        INSERT INTO dismissed_reference_matches (kind, ref_id, note, dismissed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(kind, ref_id) DO UPDATE SET
            note = excluded.note,
            dismissed_at = excluded.dismissed_at
        """,
        (kind, ref_id, note, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def undismiss_reference_match(conn: sqlite3.Connection, kind: str, ref_id: str) -> None:
    """Clear a prior dismiss_reference_match call so the group reappears."""
    conn.execute(
        "DELETE FROM dismissed_reference_matches WHERE kind = ? AND ref_id = ?",
        (kind, ref_id),
    )
    conn.commit()


def query_dismissed_reference_matches(conn: sqlite3.Connection) -> list[dict]:
    """Return all dismissed (kind, ref_id) pairs, most recent first."""
    rows = conn.execute(
        "SELECT kind, ref_id, note, dismissed_at FROM dismissed_reference_matches "
        "ORDER BY dismissed_at DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def query_reference_duplicate_groups(
    conn: sqlite3.Connection,
    *,
    include_dismissed: bool = False,
) -> list[tuple[str, str, list[dict]]]:
    """Group receipts that share an embedded reference ID (EFT/transaction/
    order/invoice number), regardless of merchant name, category, or date.

    This catches the class of duplicate that merchant+total dedup misses
    entirely: the same real-world transaction saved as two different
    documents with two different merchant guesses (a payment-confirmation
    email and a screenshot of the same confirmation page, say). A shared ID
    is strong evidence but not proof by itself — two genuinely separate
    mailings of the same notice would also share nothing to distinguish them
    here — so this is report-only. Callers must not auto-remove on this
    signal; a human should confirm each group before acting.

    Groups previously dismissed via dismiss_reference_match are excluded
    unless include_dismissed is True (they were confirmed not to be
    duplicates — a recurring tag number, a shared invoice-series prefix —
    and shouldn't keep resurfacing on every run).
    """
    from intake.reference_ids import extract_reference_ids

    dismissed = (
        set()
        if include_dismissed
        else {
            (row["kind"], row["ref_id"])
            for row in conn.execute(
                "SELECT kind, ref_id FROM dismissed_reference_matches"
            ).fetchall()
        }
    )

    rows = conn.execute(
        "SELECT filename, merchant, total, category, scan_date, raw_text FROM receipts"
    ).fetchall()

    by_ref: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        row = dict(row)
        raw_text = row.pop("raw_text") or ""
        for kind, ref_id in extract_reference_ids(raw_text).items():
            by_ref[(kind, ref_id)].append(row)

    return [
        (kind, ref_id, group)
        for (kind, ref_id), group in by_ref.items()
        if len(group) > 1 and (kind, ref_id) not in dismissed
    ]


def query_flagged(conn: sqlite3.Connection, include_suppressed: bool = False) -> list[dict]:
    """Return rows that look suspicious for manual review.

    Flags any receipt where:
    - merchant is null, 'unknown', or 'receipt' (OCR placeholder)
    - merchant contains an @ sign (email leaked in)
    - merchant is very short (≤ 3 chars)
    - total is null or exactly 0.0
    - total crosses one or more explicit parser-review guard thresholds
    - merchant/card novelty signals for recent scan dates
    - merchant-level amount outliers for recent scan dates
    - the document's "Bill To"/"Invoice To"/"Ship To" name doesn't match any
      registered purchaser alias (only checked once [purchaser_aliases] is
      configured in settings, to stay silent otherwise)

    Rows with a ``reviewed_at`` timestamp are suppressed from results unless
    *include_suppressed* is True.
    """
    reply_note_rows = conn.execute("""
        SELECT filename, note, updated_at
        FROM receipt_updates
        WHERE source = 'email-reply-note'
        ORDER BY updated_at DESC, id DESC
        """).fetchall()
    reply_resolution_rows = conn.execute("""
        SELECT filename, MAX(updated_at) AS resolved_at
        FROM receipt_updates
        WHERE source IN ('manual-correct', 'email-reply', 'email-reply-link')
        GROUP BY filename
        """).fetchall()
    reply_resolved_at = {
        str(row["filename"] or ""): str(row["resolved_at"] or "") for row in reply_resolution_rows
    }
    latest_reply_notes: dict[str, str] = {}
    for row in reply_note_rows:
        filename = str(row["filename"] or "")
        if not filename or filename in latest_reply_notes:
            continue
        resolved_at = reply_resolved_at.get(filename)
        if resolved_at and str(row["updated_at"] or "") <= resolved_at:
            continue
        note = " ".join(str(row["note"] or "").split())
        latest_reply_notes[filename] = note

    cursor = conn.execute("""
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
            currency,
            raw_text,
            processed_by,
            processed_at,
            reviewed_at
        FROM receipts
        ORDER BY scan_date, processed_at, filename
        """)
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]

    def _parse_scan_date(value: object | None) -> date | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None

    latest_scan_date = max(
        (_parse_scan_date(row.get("scan_date")) for row in rows),
        default=None,
    )
    novelty_cutoff = latest_scan_date - timedelta(days=30) if latest_scan_date is not None else None

    merchant_seen: dict[str, int] = defaultdict(int)
    card_seen: dict[str, int] = defaultdict(int)
    merchant_card_seen: dict[tuple[str, str], int] = defaultdict(int)
    merchant_totals: dict[str, list[float]] = defaultdict(list)

    total_guard_thresholds = (250.0, 500.0, 750.0, 1000.0)
    purchaser_aliases_registered = has_registered_purchaser_aliases()

    flagged: list[dict] = []
    for row in rows:
        reasons: list[str] = []
        quality_reasons: list[str] = []
        total_guard_reasons: list[str] = []

        merchant = str(row.get("merchant") or "").strip()
        merchant_norm = merchant.casefold()
        card_last4 = str(row.get("card_last4") or "").strip()
        currency = str(row.get("currency") or "USD").strip().upper()
        total = row.get("total")
        scan_date = _parse_scan_date(row.get("scan_date"))
        novelty_allowed = (
            novelty_cutoff is not None and scan_date is not None and scan_date >= novelty_cutoff
        )

        if not merchant_norm:
            quality_reasons.append("missing_merchant")
        elif merchant_norm in {"unknown", "receipt"}:
            quality_reasons.append("placeholder_merchant")
        if "@" in merchant:
            quality_reasons.append("merchant_contains_email")
        if merchant and len(merchant) <= 3:
            quality_reasons.append("short_merchant")
        if total is None:
            quality_reasons.append("missing_total")
        elif total == 0.0:
            quality_reasons.append("zero_total")
        elif float(total) > 0:
            for threshold in total_guard_thresholds:
                if float(total) > threshold:
                    total_guard_reasons.append(f"total_over_{int(threshold)}")
        if currency != "USD":
            quality_reasons.append(f"currency_not_usd ({currency})")

        if purchaser_aliases_registered:
            billed_to = extract_billed_to_name(str(row.get("raw_text") or ""))
            billed_to_key = re.sub(r"[^a-z0-9]+", "", (billed_to or "").casefold())
            merchant_key = re.sub(r"[^a-z0-9]+", "", merchant_norm)
            # Skip the many invoices whose "Bill To" field is just the seller
            # reprinting their own legal name (e.g. "Anthropic, PBC" on an
            # Anthropic invoice) — that's not a different party.
            is_self_referential = bool(merchant_key) and (
                merchant_key in billed_to_key or billed_to_key in merchant_key
            )
            if billed_to and not is_self_referential and not is_known_purchaser_name(billed_to):
                quality_reasons.append(f"billed_to_other_party ({billed_to})")

        reasons.extend(quality_reasons)
        reasons.extend(total_guard_reasons)

        if novelty_allowed and not quality_reasons:
            if merchant_norm and merchant_seen[merchant_norm] == 0:
                reasons.append("new_merchant")
            if card_last4 and card_seen[card_last4] == 0:
                reasons.append("new_card_last4")
            if (
                merchant_norm
                and card_last4
                and merchant_card_seen[(merchant_norm, card_last4)] == 0
            ):
                reasons.append("new_merchant_card_pair")
            prior_totals = merchant_totals[merchant_norm]
            if total is not None and len(prior_totals) >= 3:
                prior_median = float(median(prior_totals))
                if prior_median > 0 and float(total) >= max(prior_median * 3, prior_median + 250):
                    reasons.append(
                        f"amount_outlier_for_merchant (prior median ${prior_median:.2f})"
                    )

        if merchant_norm:
            merchant_seen[merchant_norm] += 1
            if total is not None and float(total) > 0:
                merchant_totals[merchant_norm].append(float(total))
        if card_last4:
            card_seen[card_last4] += 1
        if merchant_norm and card_last4:
            merchant_card_seen[(merchant_norm, card_last4)] += 1

        reply_note = latest_reply_notes.get(str(row.get("filename") or ""))
        if reply_note:
            excerpt = reply_note[:80]
            if len(reply_note) > 80:
                excerpt += "..."
            reasons.append(f"email_reply_request ({excerpt})")

        if reasons and (include_suppressed or row.get("reviewed_at") is None):
            enriched = dict(row)
            enriched.pop("raw_text", None)
            if reply_note:
                enriched["email_reply_note"] = reply_note
            enriched["review_reasons"] = reasons
            enriched["review_reason_summary"] = ", ".join(reasons)
            flagged.append(enriched)

    flagged.sort(key=lambda row: (row.get("category") or "", row.get("filename") or ""))
    return flagged


# ── email receipt helpers ──────────────────────────────────────────────────────


def is_email_processed(conn: sqlite3.Connection, uid: int, mailbox: str) -> bool:
    """Return True if this (uid, mailbox) pair is already in email_receipts."""
    row = conn.execute(
        "SELECT 1 FROM email_receipts WHERE uid = ? AND mailbox = ?",
        (uid, mailbox),
    ).fetchone()
    return row is not None


def is_email_message_id_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    """Return True if *message_id* has already been recorded in email_receipts."""
    cleaned = str(message_id or "").strip()
    if not cleaned:
        return False
    row = conn.execute(
        "SELECT 1 FROM email_receipts WHERE message_id = ? LIMIT 1",
        (cleaned,),
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


# ── letter helpers ─────────────────────────────────────────────────────────────


def upsert_letter(conn: sqlite3.Connection, data: dict) -> None:
    """Insert or replace a letter record.

    *data* keys: filename, scan_date, letter_date, sender, recipient, subject,
                 salutation, body_excerpt, category, receipt_dir, file_hash,
                 raw_text, processed_by.
    processed_at is set automatically to now (UTC).
    """
    processed_at = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO letters
            (filename, scan_date, letter_date, sender, recipient, subject,
             salutation, body_excerpt, category, receipt_dir, file_hash,
             raw_text, processed_by, processed_at)
        VALUES
            (:filename, :scan_date, :letter_date, :sender, :recipient, :subject,
             :salutation, :body_excerpt, :category, :receipt_dir, :file_hash,
             :raw_text, :processed_by, :processed_at)
        ON CONFLICT(filename) DO UPDATE SET
            scan_date    = excluded.scan_date,
            letter_date  = excluded.letter_date,
            sender       = excluded.sender,
            recipient    = excluded.recipient,
            subject      = excluded.subject,
            salutation   = excluded.salutation,
            body_excerpt = excluded.body_excerpt,
            category     = excluded.category,
            receipt_dir  = excluded.receipt_dir,
            file_hash    = excluded.file_hash,
            raw_text     = excluded.raw_text,
            processed_by = excluded.processed_by,
            processed_at = excluded.processed_at
        """,
        {
            "processed_by": "tesseract",
            "letter_date": None,
            "sender": None,
            "recipient": None,
            "subject": None,
            "salutation": None,
            "body_excerpt": None,
            "file_hash": None,
            **data,
            "processed_at": processed_at,
        },
    )
    conn.commit()


def get_letter_by_file_hash(
    conn: sqlite3.Connection,
    file_hash: str,
    *,
    exclude_filename: str | None = None,
) -> dict | None:
    """Return a tracked letter row for *file_hash*, or None if absent."""
    params: list[object] = [file_hash]
    where = "WHERE file_hash = ?"
    if exclude_filename is not None:
        where += " AND filename != ?"
        params.append(exclude_filename)
    row = conn.execute(
        f"""
        SELECT filename, scan_date, letter_date, sender, recipient, subject,
               salutation, body_excerpt, category, receipt_dir, file_hash,
               processed_by, processed_at
        FROM letters
        {where}
        ORDER BY processed_at ASC, filename ASC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return dict(row) if row is not None else None


# ── card helpers ───────────────────────────────────────────────────────────────


def upsert_card(conn: sqlite3.Connection, data: dict) -> None:
    """Insert or replace a card record.

    *data* keys: filename, scan_date, card_date, occasion, sender, recipient,
                 message_excerpt, category, receipt_dir, file_hash,
                 raw_text, processed_by.
    processed_at is set automatically to now (UTC).
    """
    processed_at = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO cards
            (filename, scan_date, card_date, occasion, sender, recipient,
             message_excerpt, category, receipt_dir, file_hash,
             raw_text, processed_by, processed_at)
        VALUES
            (:filename, :scan_date, :card_date, :occasion, :sender, :recipient,
             :message_excerpt, :category, :receipt_dir, :file_hash,
             :raw_text, :processed_by, :processed_at)
        ON CONFLICT(filename) DO UPDATE SET
            scan_date       = excluded.scan_date,
            card_date       = excluded.card_date,
            occasion        = excluded.occasion,
            sender          = excluded.sender,
            recipient       = excluded.recipient,
            message_excerpt = excluded.message_excerpt,
            category        = excluded.category,
            receipt_dir     = excluded.receipt_dir,
            file_hash       = excluded.file_hash,
            raw_text        = excluded.raw_text,
            processed_by    = excluded.processed_by,
            processed_at    = excluded.processed_at
        """,
        {
            "processed_by": "tesseract",
            "card_date": None,
            "occasion": None,
            "sender": None,
            "recipient": None,
            "message_excerpt": None,
            "file_hash": None,
            **data,
            "processed_at": processed_at,
        },
    )
    conn.commit()


def get_card_by_file_hash(
    conn: sqlite3.Connection,
    file_hash: str,
    *,
    exclude_filename: str | None = None,
) -> dict | None:
    """Return a tracked card row for *file_hash*, or None if absent."""
    params: list[object] = [file_hash]
    where = "WHERE file_hash = ?"
    if exclude_filename is not None:
        where += " AND filename != ?"
        params.append(exclude_filename)
    row = conn.execute(
        f"""
        SELECT filename, scan_date, card_date, occasion, sender, recipient,
               message_excerpt, category, receipt_dir, file_hash,
               processed_by, processed_at
        FROM cards
        {where}
        ORDER BY processed_at ASC, filename ASC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return dict(row) if row is not None else None


def upsert_payment(conn: sqlite3.Connection, data: dict) -> None:
    """Insert or replace a payment record.

    *data* keys: filename, scan_date, payment_date, amount, account_name,
                 account_last4, payment_type, confirmation_number, payer,
                 category, receipt_dir, file_hash, raw_text, processed_by.
    processed_at is set automatically to now (UTC).
    """
    processed_at = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO payments
            (filename, scan_date, payment_date, amount, account_name,
             account_last4, payment_type, confirmation_number, payer,
             category, receipt_dir, file_hash, raw_text, processed_by, processed_at)
        VALUES
            (:filename, :scan_date, :payment_date, :amount, :account_name,
             :account_last4, :payment_type, :confirmation_number, :payer,
             :category, :receipt_dir, :file_hash, :raw_text, :processed_by, :processed_at)
        ON CONFLICT(filename) DO UPDATE SET
            scan_date           = excluded.scan_date,
            payment_date        = excluded.payment_date,
            amount              = excluded.amount,
            account_name        = excluded.account_name,
            account_last4       = excluded.account_last4,
            payment_type        = excluded.payment_type,
            confirmation_number = excluded.confirmation_number,
            payer               = excluded.payer,
            category            = excluded.category,
            receipt_dir         = excluded.receipt_dir,
            file_hash           = excluded.file_hash,
            raw_text            = excluded.raw_text,
            processed_by        = excluded.processed_by,
            processed_at        = excluded.processed_at
        """,
        {
            "processed_by": "tesseract",
            "payment_date": None,
            "amount": None,
            "account_name": None,
            "account_last4": None,
            "payment_type": None,
            "confirmation_number": None,
            "payer": None,
            "file_hash": None,
            **data,
            "processed_at": processed_at,
        },
    )
    conn.commit()


def get_payment_by_file_hash(
    conn: sqlite3.Connection,
    file_hash: str,
    *,
    exclude_filename: str | None = None,
) -> dict | None:
    """Return a tracked payment row for *file_hash*, or None if absent."""
    params: list[object] = [file_hash]
    where = "WHERE file_hash = ?"
    if exclude_filename is not None:
        where += " AND filename != ?"
        params.append(exclude_filename)
    row = conn.execute(
        f"""
        SELECT filename, scan_date, payment_date, amount, account_name,
               account_last4, payment_type, confirmation_number, payer,
               category, receipt_dir, file_hash, processed_by, processed_at
        FROM payments
        {where}
        ORDER BY processed_at ASC, filename ASC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return dict(row) if row is not None else None


# ── generic document helpers ──────────────────────────────────────────────────


def upsert_document(conn: sqlite3.Connection, data: dict) -> None:
    """Insert or replace a reference document record."""
    processed_at = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO documents
            (filename, scan_date, document_date, document_kind, title, issuer,
             subject, model_number, serial_number, body_excerpt, category,
             receipt_dir, file_hash, raw_text, processed_by, processed_at)
        VALUES
            (:filename, :scan_date, :document_date, :document_kind, :title, :issuer,
             :subject, :model_number, :serial_number, :body_excerpt, :category,
             :receipt_dir, :file_hash, :raw_text, :processed_by, :processed_at)
        ON CONFLICT(filename) DO UPDATE SET
            scan_date     = excluded.scan_date,
            document_date = excluded.document_date,
            document_kind = excluded.document_kind,
            title         = excluded.title,
            issuer        = excluded.issuer,
            subject       = excluded.subject,
            model_number  = excluded.model_number,
            serial_number = excluded.serial_number,
            body_excerpt  = excluded.body_excerpt,
            category      = excluded.category,
            receipt_dir   = excluded.receipt_dir,
            file_hash     = excluded.file_hash,
            raw_text      = excluded.raw_text,
            processed_by  = excluded.processed_by,
            processed_at  = excluded.processed_at
        """,
        {
            "processed_by": "tesseract",
            "document_date": None,
            "document_kind": "documents/reference",
            "title": None,
            "issuer": None,
            "subject": None,
            "model_number": None,
            "serial_number": None,
            "body_excerpt": None,
            "file_hash": None,
            **data,
            "processed_at": processed_at,
        },
    )
    conn.commit()


def get_document_by_file_hash(
    conn: sqlite3.Connection,
    file_hash: str,
    *,
    exclude_filename: str | None = None,
) -> dict | None:
    """Return a tracked reference document row for *file_hash*, or None if absent."""
    params: list[object] = [file_hash]
    where = "WHERE file_hash = ?"
    if exclude_filename is not None:
        where += " AND filename != ?"
        params.append(exclude_filename)
    row = conn.execute(
        f"""
        SELECT filename, scan_date, document_date, document_kind, title, issuer,
               subject, model_number, serial_number, body_excerpt, category,
               receipt_dir, file_hash, processed_by, processed_at
        FROM documents
        {where}
        ORDER BY processed_at ASC, filename ASC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return dict(row) if row is not None else None
