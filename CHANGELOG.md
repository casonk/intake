## Unreleased

### Changed
- `*.ai.json` debug artifacts now append new AI model attempts across
  re-extractions and include a fidelity-weighted `models` summary.

## [0.1.0] - 2026-04-01

### Added
- Initial release with full `process` / `report` / `audit` pipeline
- pdfplumber extraction with pytesseract fallback for image-only PDFs
- Merchant/date/total parsing from OCR text
- Configurable keyword-based category classification via `config/categories.toml`
- SQLite database upsert for deduplication across runs
- Markdown and HTML summary reports (spending by category, by month, top merchants, recent receipts)
- `intake process`, `intake report`, `intake audit` CLI subcommands
- Unit tests for parse and classify modules
- Pre-commit hooks and GitHub Actions CI workflow
