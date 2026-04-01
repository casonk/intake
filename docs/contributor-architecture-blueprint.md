# Contributor Architecture Blueprint — intake

## Overview

`intake` is a receipt-processing pipeline. PDFs arrive on a snowbridge SMB share, are OCR-extracted, parsed, classified, moved to category folders, and upserted into a local SQLite database. Reports are generated on demand.

## Receipt Source

- **Mount point**: `/mnt/setup/bully/info/receipt/` (snowbridge SMB share)
- **Inbox**: `receipt/inbox/` — new PDFs land here, named `YYYYMMDD_HHMM.pdf`
- **Category folders** alongside inbox: `auto`, `cvs`, `edu`, `gas-station`, `general`, `grocery`, `moto`, `office`, `outdoor`, `PC`, `phone`, `sw`, `weed`

## Pipeline Stages

### 1. OCR (`intake/ocr.py`)

For each PDF in `inbox/`:
- Open with `pdfplumber` and extract native text layer per page
- If a page yields fewer than 20 characters, render at 300 DPI and run `pytesseract` OCR on the image
- Return concatenated page text

### 2. Parse (`intake/parse.py`)

From the filename and raw OCR text, produce a `Receipt` dataclass:
- `scan_date`: parsed from the `YYYYMMDD` prefix of the filename stem
- `merchant`: first non-empty trimmed line of OCR text, up to 60 characters
- `total`: last line matching `Total`, `Amount Due`, or `Balance Due` followed by a dollar amount

### 3. Classify (`intake/classify.py`)

- Load keyword rules from `config/categories.toml`
- Lowercase substring match against merchant name
- First matching category wins; falls back to `general`

### 4. Move

PDF is moved from `inbox/` to `receipt/<category>/`.

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
| `intake process` | Run the full pipeline |
| `intake report` | Generate reports from DB |
| `intake audit` | List inbox PDFs without processing |

## Configuration

- `config/settings.toml` (gitignored) — copy from `config/settings.toml.example`
- `config/categories.toml` — tracked; defines merchant keyword → category mapping

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
