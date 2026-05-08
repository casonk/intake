# AGENTS.md — intake

## Purpose

`intake` processes incoming receipt PDF scans from a snowbridge SMB share into a structured SQLite database and Markdown/HTML summary reports.

- **Receipt source**: `/mnt/setup/bully/info/receipt/` (snowbridge SMB share)
- **Inbox**: `intake/` subfolder where new PDFs land, named `YYYYMMDD_HHMM.pdf`
- **Categories**: `auto`, `edu`, `gas-station`, `general`, `grocery`, `moto`, `office`, `outdoor`, `pc`, `pharmacy`, `phone`, `sw`, `weed` — each supports subcategories (e.g. `grocery/supermarket`)

## Key Entry Points

```bash
intake process   # OCR → parse → classify → move → upsert DB → write sidecar
intake backfill  # ingest receipts already in category folders (skips sidecared files)
intake report    # generate Markdown + HTML spending summary
intake audit     # list intake PDFs without processing
```

## Configuration

```bash
cp config/settings.toml.example config/settings.toml
# Set receipt_dir to your local snowbridge mount path
```

`config/settings.toml` is gitignored — never commit it. Same for `data/` and `reports/`.

## Pipeline Overview

1. **OCR** (`intake/ocr.py`): pdfplumber text extraction, pytesseract fallback for image-only pages
2. **Parse** (`intake/parse.py`): extract date (from filename), merchant (first OCR line), total (last Total/Amount Due match)
3. **Classify** (`intake/classify.py`): keyword rules from `config/categories.toml` (gitignored; copy from `config/categories.toml.example`); subcategory keywords checked first; returns `"category"` or `"category/subcategory"`; falls back to `general`
4. **Move**: PDF moves from `intake/` to the matched category folder; a `<stem>.intake.json` sidecar is written alongside it
5. **DB** (`intake/db.py`): upsert into `data/intake.db` (SQLite)
6. **Backfill** (`intake backfill`): walks all category folders, skips PDFs that already have a sidecar, OCRs and upserts the rest — use this to onboard receipts that pre-date the pipeline
6. **Report** (`intake/report.py`): Jinja2-rendered Markdown + HTML to `reports/`

## Session Continuity

- Read `AGENTS.md` → `LESSONSLEARNED.md` → `CHATHISTORY.md` (local-only, gitignored) before resuming work.
- Update `CHATHISTORY.md` after meaningful sessions.
- Add durable lessons to `LESSONSLEARNED.md`.

## Portfolio References

This repository is governed by portfolio-wide standards in `./util-repos/traction-control`.

Shared utility repos available for integration:

- `./util-repos/archility` — architecture toolchain bootstrap and diagram rendering
- `./util-repos/auto-pass` — KeePassXC-backed password management
- `./util-repos/clockwork` — shared cron and systemd scheduler rendering
- `./util-repos/nordility` — NordVPN CLI/API automation
- `./util-repos/shock-relay` — external messaging (Signal, Telegram, SMS, Gmail)
- `./util-repos/short-circuit` — WireGuard VPN setup and configuration
- `./util-repos/snowbridge` — SMB-based file sharing — **receipt source for this repo**
- `./util-repos/dyno-lab` — unified test bench utilities — fixtures, subprocess/HTTP/env mocks, schema validation, smoke scaffolding, and pytest markers/fixtures
- `./finance-pipeline` — downstream consumer of aggregated spend data from this repo

## Sudo Boundary

Agents will never be able to run `sudo` commands in this environment. If a task requires elevated system changes, make the repo edits and run the validation that can be done without `sudo`, then give the user the exact command(s) to run.

Always require the user to run those commands instead of retrying `sudo`; do not claim a sudo-backed live change was applied until the user shares the result.

## Local CI Verification

Run before every push:

```bash
pre-commit run --all-files
pytest -q
```

Do not push changes that have not passed all checks locally.
