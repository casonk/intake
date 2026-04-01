# Contributor Architecture Blueprint — intake

## Overview

`intake` is a receipt-processing pipeline. PDFs arrive on a snowbridge SMB share, are OCR-extracted, parsed, classified, moved to category folders, and upserted into a local SQLite database. Reports are generated on demand.

## Receipt Source

- **Mount point**: `/mnt/setup/bully/info/receipt/` (snowbridge SMB share)
- **Inbox**: `receipt/intake/` — new PDFs land here, named `YYYYMMDD_HHMM.pdf`
- **Category folders** alongside intake: `auto`, `cvs`, `edu`, `gas-station`, `general`, `grocery`, `moto`, `office`, `outdoor`, `PC`, `phone`, `sw`, `weed`

## Pipeline Stages

### 1. OCR (`intake/ocr.py`)

For each PDF in `intake/`:
- Open with `pdfplumber` and extract native text layer per page
- If a page yields fewer than 20 characters, render at 300 DPI and run `pytesseract` OCR on the image
- Return concatenated page text

### 2. Parse (`intake/parse.py`)

From the filename and raw OCR text, produce a `Receipt` dataclass:
- `scan_date`: parsed from the `YYYYMMDD` prefix of the filename stem
- `merchant`: first non-empty trimmed line of OCR text, up to 60 characters
- `total`: last line matching `Total`, `Amount Due`, or `Balance Due` followed by a dollar amount

### 3. Classify (`intake/classify.py`)

- Load keyword rules from `config/categories.toml` (falls back to `categories.toml.example` if local file absent)
- Subcategory keywords evaluated before parent keywords — most specific match wins
- Lowercase substring match against merchant name, falling back to full OCR text
- Returns `"category"` or `"category/subcategory"`; falls back to `general`

### 4. Move & Sidecar

PDF is moved from `intake/` to `receipt/<category>/` (or `receipt/<category>/<subcategory>/`).
A `<stem>.intake.json` sidecar is written alongside the PDF recording merchant, total, category,
scan_date, and processed_at.  The sidecar is the filesystem-level "already ingested" marker used
by `intake backfill` to avoid duplicate processing without needing a live DB connection.

### 5. Database Upsert (`intake/db.py`)

- SQLite database at `data/intake.db`
- `receipts` table with `UNIQUE` constraint on `filename` — safe to re-run (upsert)
- Fields: filename, scan_date, merchant, total, category, receipt_dir, raw_text, processed_at

### 6. Report Generation (`intake/report.py`)

- Reads aggregated summaries from SQLite
- Renders via Jinja2 into `reports/intake-report.md` and `reports/intake-report.html`
- Report sections: summary, spending by category, spending by month, top 10 merchants, recent 20 receipts

## CLI Entry Points (`intake/cli.py`)

| Command | Description |
|---|---|
| `intake process`  | Run the full pipeline |
| `intake backfill` | Ingest pre-existing receipts from category folders |
| `intake report`   | Generate reports from DB |
| `intake audit`    | List intake PDFs without processing |

## Configuration

- `config/settings.toml` (gitignored) — copy from `config/settings.toml.example`
- `config/categories.toml` (gitignored) — copy from `config/categories.toml.example`; defines merchant keyword → category/subcategory mapping
- Category and subcategory keys must be lowercase with only letters, digits, `-`, or `_`
- Classification returns `"category"` or `"category/subcategory"` (most specific match wins)

## Outputs

| Artifact | Location | Tracked |
|---|---|---|
| SQLite database | `data/intake.db` | No (gitignored) |
| Markdown report | `reports/intake-report.md` | No (gitignored) |
| HTML report | `reports/intake-report.html` | No (gitignored) |

## Testing

- Unit tests for `parse.py` and `classify.py` run in CI (no system dependencies)
- OCR integration tests require tesseract and should be marked `pytest.mark.integration`
- Fixture plain-text OCR outputs live in `tests/fixtures/`
