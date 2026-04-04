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

**Regenerate reports every day at noon** with a user-level systemd timer:

```bash
intake report --install-timer
systemctl --user daemon-reload
systemctl --user enable --now intake-report.timer
systemctl --user list-timers intake-report.timer
```

The install path now renders the tracked unit definition through the sibling
`clockwork` repo instead of keeping the unit text inline in `intake`.

**Audit intake** — list pending PDFs without processing them:

```bash
intake audit
```

**Run continuously** with a user-level systemd service:

```bash
intake daemon --install
systemctl --user daemon-reload
systemctl --user enable --now intake-daemon
journalctl --user -u intake-daemon -f
```

**Send a Signal message** after each successfully processed receipt:

```toml
[signal]
shock_relay_config = "/path/to/shock-relay/services/signal-cli/config.local.yaml"
recipient = "+15551234567"

# Or use your Signal "note to self" thread instead of a separate recipient:
# note_to_self = true
```

**Send a Gmail notification** after each successfully processed receipt:

```toml
[gmail]
recipient = "you@example.com"

# Optional when [email].shock_relay_config is already configured for receipt
# email ingestion; otherwise point it at shock-relay's gmail-imap config:
# shock_relay_config = "/path/to/shock-relay/services/gmail-imap/config.local.yaml"
```

**Trigger personal-finance** after receipt ingestion by writing a batch JSON and
running a downstream command:

```toml
[personal_finance]
repo_root = "/mnt/4tb-m2/git/personal-finance"
command = ["python3", "scripts/all/monthly_controller.py"]
```

`intake` writes the generated batch JSON under `data/personal-finance/` by
default and exposes its path to the downstream command via both
`{receipts_json}` command placeholders and the `INTAKE_RECEIPTS_JSON`
environment variable.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Open an issue before starting significant work.

## License

MIT — see [LICENSE](LICENSE).
