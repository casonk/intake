## Unreleased

### Added
- Career mail filing (`[career] file_mail`, `label_prefix`): each career
  email gets a `Career/<Kind>` Gmail label; requests, screenings, interviews,
  and offers stay in Inbox, starred, while confirmations, rejections, security
  codes, and completed requests are archived. Read state is never changed.
  `intake career fetch --refile` files messages saved earlier.
- Career mail: application-portal senders (Greenhouse, Workday, Lever, Ashby)
  are recognised, with the employer taken from the subject or an exact
  address key; every career email is saved with a `Kind:` line
  (confirmation, request, screening, interview, offer, rejection), and only
  requests and outcomes needing a reply keep an open action box; emailed
  verification codes are written owner-only to career's
  `private/verification-codes/` and never printed.

### Changed
- The downstream finance trigger no longer names a private repository in
  tracked files. Its default event directory is now `data/finance-events`;
  set `[personal_finance] event_dir` to keep using an existing directory. The
  cleanup flags are renamed to `--finance-events` and `--finance-event-dir`.
  Log lines are prefixed `[finance-trigger]`.
  Private downstream consumers move to the gitignored
  `config/downstream-repos.local.toml`.
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
