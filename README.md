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
cp config/settings.toml.example config/settings.toml
# Edit config/settings.toml and set receipt_dir to your local SMB mount path
```

`config/settings.toml` is gitignored — never commit it.

## Usage

**Process new receipts** from `inbox/` → OCR → classify → move → database:

```bash
intake process
intake process --receipt-dir /mnt/setup/bully/info/receipt --dry-run
```

**Generate reports** (Markdown + HTML) from the database:

```bash
intake report
intake report --format html --output-dir reports/
```

**Audit inbox** — list pending PDFs without processing them:

```bash
intake audit
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Open an issue before starting significant work.

## License

MIT — see [LICENSE](LICENSE).
