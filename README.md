# intake

Process incoming receipt PDF scans from a snowbridge SMB share into a structured SQLite database and Markdown/HTML reports.

## Prerequisites

- Python 3.11+
- [Tesseract OCR](https://tesseract-ocr.github.io/tessdoc/Installation.html) system package (`tesseract-ocr` on Debian/Ubuntu, `tesseract` on Fedora/macOS)

## Install

```bash
pip install -e ".[dev]"
```

## Configuration

```bash
cp config/categories.toml.example config/categories.toml
cp config/settings.toml.example config/settings.toml
# Edit config/settings.toml and set receipt_dir to your local SMB mount path
# Edit config/categories.toml and fill in your own merchant keywords
```

`config/settings.toml` and `config/categories.toml` are both gitignored — never commit them.

## Usage

**Backfill pre-existing receipts** already sorted into category folders:

```bash
intake backfill
intake backfill --dry-run          # preview without writing
intake backfill --receipt-dir /mnt/setup/bully/info/receipt
```

**Process new receipts** from `intake/` → OCR → classify → move → database:

```bash
intake process
intake process --receipt-dir /mnt/setup/bully/info/receipt --dry-run
```

**Generate reports** (Markdown + HTML) from the database:

```bash
intake report
intake report --format html --output-dir reports/
```

**Audit intake** — list pending PDFs without processing them:

```bash
intake audit
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Open an issue before starting significant work.

## License

MIT — see [LICENSE](LICENSE).
