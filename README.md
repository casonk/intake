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

Keep `config/settings.toml` as the single source of truth for intake-wide
settings, including `auto_pass_profile` and `openai_api_key_entry`. Do not
create a separate `config/auto-pass.ini`. Also keep those intake-wide keys at
the top level of the TOML file: once you open a table like `[web]` or
`[email]`, later bare keys belong to that table, not to global intake
settings.

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

Processed receipts now normalize to the canonical `YYYYMMDD_HHMM.EXT` filename
format. When the minute slot is already occupied, `intake` keeps the same
format and advances minute-by-minute instead of appending `-1`, `-2`, etc.

**Generate reports** (Markdown + HTML) from the database:

```bash
intake report
intake report --format html --output-dir reports/
```

By default, report links target the Snowbridge File Browser at
`https://files.snowbridge.internal/files/receipt/...` so category, folder, and
receipt links work from the served report site instead of pointing at local
`file:///` paths. Override that behavior through `[report_links]` in
`config/settings.toml` when needed.

**Regenerate reports every day at noon** with a user-level systemd timer:

```bash
intake report --install-timer
systemctl --user daemon-reload
systemctl --user enable --now intake-report.timer
systemctl --user list-timers intake-report.timer
```

The install path now renders the tracked unit definition through the sibling
`clockwork` repo instead of keeping the unit text inline in `intake`.

**Serve the generated reports** on localhost for `wiring-harness` / shared
Caddy:

```bash
intake serve
intake serve --host 127.0.0.1 --port 5200
```

**Install the localhost report site** as a user-level systemd service:

```bash
intake serve --install
systemctl --user daemon-reload
systemctl --user enable --now intake-web.service
journalctl --user -u intake-web.service -f
```

By default `intake serve` reads `[web].host` / `[web].port` from
`config/settings.toml` and serves `reports/`, redirecting `/` to
`/intake-report.html` when that file exists.

**Resolve the OpenAI key** from KeePassXC via auto-pass directly in
`config/settings.toml`:

```toml
auto_pass_profile = "master"
openai_api_key_entry = "vendor/openai/api-key"
openai_model_low = "gpt-4o-mini"
openai_model_mid = "gpt-4o"
openai_model_high = "gpt-4.1"
```

Those keys are intake-wide settings, so keep them above the first table header
in the TOML file.

**Audit intake** — list pending PDFs without processing them:

```bash
intake audit
```

**Normalize historical filenames** for already-ingested receipts:

```bash
intake normalize-filenames --dry-run
intake normalize-filenames
```

The normalizer preserves the date already embedded in a filename when one is
present, fills in the missing `HHMM`, renames the companion `.intake.json`
sidecar, and updates the DB audit trail.

**Apply a manual correction** while keeping the DB, sidecar, and audit trail in
sync:

```bash
intake correct 20260409_1707.pdf --total 88.05 --note "card charge beats balance line"
intake correct 20260406_0902.pdf --category dining/fast-food --merchant "mcdonald's"
```

**Inspect correction history** for a receipt:

```bash
intake history 20260409_1707.pdf
```

Manual corrections are written to the `receipt_updates` SQLite table with a
field-level before/after diff, source label, note, and timestamp. That gives
you a durable trail you can mine later for auto-healing rules or regression
fixtures.

**Deduplicate historical receipts** without falling back to ad hoc scripts:

```bash
intake dedup
intake dedup --merge
intake dedup --remove
```

`--merge` keeps the best row, promotes missing purchaser/card fields into it,
and deletes the other DB rows. `--remove` implies that same keeper merge, then
moves duplicate artifacts into `exhaust/duplicates/...` and records the removal
in `receipt_updates` before deleting the duplicate DB row.

PDF-based ingest now computes a SHA-256 `file_hash` for every tracked receipt.
`intake dedup` uses exact hash matches first, then falls back to the older
merchant + total + scan-date window heuristic for rows that do not yet have a
stored hash.

New inbox duplicates are short-circuited at ingest: if a PDF arriving in
`receipt/intake/` matches the `file_hash` of an already-tracked receipt,
`intake process` moves the new copy into `exhaust/duplicates/...`, writes a
duplicate sidecar, and records an audit event instead of ingesting the same
receipt twice.

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

**Trigger finance-pipeline** after receipt ingestion by writing a batch JSON and
running a downstream command:

```toml
[personal_finance]
repo_root = "/mnt/4tb-m2/git/finance-pipeline"
command = ["python3", "scripts/all/monthly_controller.py"]
```

`intake` writes the generated batch JSON under `data/finance-pipeline/` by
default and exposes its path to the downstream command via both
`{receipts_json}` command placeholders and the `INTAKE_RECEIPTS_JSON`
environment variable.

## wiring-harness Integration

To publish the report site behind shared mTLS Caddy, run the localhost backend
from `intake` and register it in `wiring-harness` on port `5200`. The current
integration uses:

- `https://receipts.intake.internal`

After changing the `wiring-harness` service registry, refresh the server cert
SANs and reprovision Caddy:

```bash
cd ../wiring-harness
bash scripts/setup-mtls.sh --refresh-server
sudo python3 scripts/setup_caddy.py --provision
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Open an issue before starting significant work.

## License

MIT — see [LICENSE](LICENSE).
