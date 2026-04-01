"""SQLite schema, upsert, and query helpers for the intake receipt database."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT UNIQUE NOT NULL,
    scan_date       TEXT NOT NULL,
    merchant        TEXT,
    total           REAL,
    category        TEXT NOT NULL,
    receipt_dir     TEXT NOT NULL,
    raw_text        TEXT,
    processed_by    TEXT NOT NULL DEFAULT 'tesseract',
    processed_at    TEXT NOT NULL
);
"""

_MIGRATIONS = [
    "ALTER TABLE receipts ADD COLUMN processed_by TEXT NOT NULL DEFAULT 'tesseract'",
    "ALTER TABLE receipts ADD COLUMN purchaser TEXT",
    "ALTER TABLE receipts ADD COLUMN card_last4 TEXT",
]


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (and initialise if needed) the SQLite database at *db_path*."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
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
                 purchaser (optional), card_last4 (optional).
    processed_at is set automatically to now (UTC).
    """
    processed_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO receipts
            (filename, scan_date, merchant, total, category, receipt_dir,
             raw_text, processed_by, purchaser, card_last4, processed_at)
        VALUES
            (:filename, :scan_date, :merchant, :total, :category, :receipt_dir,
             :raw_text, :processed_by, :purchaser, :card_last4, :processed_at)
        ON CONFLICT(filename) DO UPDATE SET
            scan_date    = excluded.scan_date,
            merchant     = excluded.merchant,
            total        = excluded.total,
            category     = excluded.category,
            receipt_dir  = excluded.receipt_dir,
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
            **data,
            "processed_at": processed_at,
        },
    )
    conn.commit()


def query_summary(conn: sqlite3.Connection) -> dict:
    """Return aggregated summary data for report generation."""
    rows = lambda sql, *a: [dict(r) for r in conn.execute(sql, *a).fetchall()]  # noqa: E731

    total_receipts = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    total_spend = conn.execute(
        "SELECT COALESCE(SUM(total), 0) FROM receipts WHERE total IS NOT NULL"
    ).fetchone()[0]

    date_range = conn.execute(
        "SELECT MIN(scan_date), MAX(scan_date) FROM receipts"
    ).fetchone()

    by_category = rows(
        """
        SELECT category,
               COUNT(*) AS count,
               COALESCE(SUM(total), 0) AS total_spend
        FROM receipts
        GROUP BY category
        ORDER BY total_spend DESC
        """
    )

    by_month = rows(
        """
        SELECT SUBSTR(scan_date, 1, 7) AS month,
               COUNT(*) AS count,
               COALESCE(SUM(total), 0) AS total_spend
        FROM receipts
        GROUP BY month
        ORDER BY month
        """
    )

    top_merchants = rows(
        """
        SELECT merchant,
               COUNT(*) AS count,
               COALESCE(SUM(total), 0) AS total_spend
        FROM receipts
        GROUP BY merchant
        ORDER BY total_spend DESC
        LIMIT 10
        """
    )

    by_model = rows(
        """
        SELECT COALESCE(processed_by, 'tesseract') AS model,
               COUNT(*) AS count
        FROM receipts
        GROUP BY model
        ORDER BY count DESC
        """
    )

    recent = rows(
        """
        SELECT scan_date, merchant, category, total, filename,
               purchaser, card_last4, processed_by, processed_at
        FROM receipts
        ORDER BY scan_date DESC, processed_at DESC
        LIMIT 20
        """
    )

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


def query_flagged(conn: sqlite3.Connection) -> list[dict]:
    """Return rows that look suspicious for manual review.

    Flags any receipt where:
    - merchant is null, 'unknown', or 'receipt' (OCR placeholder)
    - merchant contains an @ sign (email leaked in)
    - merchant is very short (≤ 3 chars)
    - total is null or exactly 0.0
    - processed_by is tesseract but merchant smells like an escalation trigger
    """
    cursor = conn.execute(
        """
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
            processed_at
        FROM receipts
        WHERE
            merchant IS NULL
            OR LOWER(TRIM(merchant)) IN ('unknown', 'receipt', '')
            OR merchant LIKE '%@%'
            OR LENGTH(TRIM(merchant)) <= 3
            OR total IS NULL
            OR total = 0.0
        ORDER BY category, filename
        """
    )
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]
