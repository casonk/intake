# intake

Process incoming receipt PDFs, images, and short videos from a snowbridge SMB share into a structured SQLite database and Markdown/HTML reports.

Consent reference: [`../../doc-repos/my-consent/financial-records.md`](../../doc-repos/my-consent/financial-records.md) documents the explicit consent covering receipt OCR, categorization, reporting, and downstream personal-finance processing handled by this repo.

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
When the inbox contains an iPhone-style live-photo pair with the same stem
(for example `LIVE_0001.JPG` and `LIVE_0001.MOV`), intake keeps the still image
as the canonical receipt input and exhausts the companion `.MOV` with an audit
trail instead of ingesting both as separate receipts.

Email ingestion also handles issuer payment-notification emails for supported
credit cards. Capital One and Discover payment scheduled/posted messages are
treated as finance records instead of ordinary merchant receipts, with issuer,
payment amount, posted/scheduled date, purchaser, and card last4 extracted
from the email body.

TODO: keep expanding regression coverage for live scheduled-payment variants
from Capital One, Discover, and Progressive as new provider templates arrive.

Every receipt now attempts an itemized breakdown. `intake` first parses line
items from stored OCR text and, when an OpenAI key is configured, will retry
with AI extraction if the receipt otherwise looks good but no itemization was
found. Itemization is stored in the DB, written into sidecars, included in the
personal-finance batch JSON, and shown in the report.

**Backfill itemized breakdowns** for receipts already in the database:

```bash
intake itemize
intake itemize --dry-run
intake itemize --limit 100
intake itemize --force
```

`items_json = NULL` means a receipt has not yet had an itemization attempt
recorded. `items_json = []` means intake attempted itemization but found no
usable line items.

**Backfill certification extraction** for receipts already in the database:

```bash
intake certify
intake certify --dry-run
intake certify --limit 100
intake certify --force
```

`certifications_json = NULL` means a receipt has not yet had certification
extraction attempted. `certifications_json = []` means intake attempted
certification extraction but found no structured claims worth storing.

By default `intake certify` trusts stored OCR first so a corpus-wide backfill
does not re-extract every local receipt file. Use `--force` when you explicitly
want a second pass that retries local extraction for receipts whose stored OCR
did not yield any certifications.

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

The generated report includes both a visual merchant timeline section for the
top merchants and an exact merchant-month breakdown table so you can review
historical spend over time by vendor. It also writes a static merchant
directory under `reports/merchants/` plus one detail page per merchant, so the
served site can drill from the summary into merchant-specific monthly history
and receipt-level detail.

**Generate a manual-review queue** for suspicious or novel receipts:

```bash
intake review
intake review --show-suppressed
intake suppress --all
intake suppress 20260409_1707.pdf
intake suppress --unsuppress 20260409_1707.pdf
```

`intake review` writes `reports/review.md` and flags two families of issues:

- extraction-quality issues such as missing or placeholder merchants, merchants
  that still contain email/header text, suspiciously short merchants, and
  missing or zero totals
- explicit total guard thresholds: `total_over_250`, `total_over_500`,
  `total_over_750`, and `total_over_1000`
- reasoned novelty issues on recent, otherwise plausible receipts: first-seen
  merchants, first-seen card last4 values, first-seen merchant/card pairs, and
  merchant-level amount outliers relative to prior history

Novelty and outlier flags intentionally do not stack onto obviously broken
rows. Fix extraction-quality issues first with `intake correct ...`, then rerun
`intake review` so the novelty signals stay high-signal instead of noisy.
The total guard thresholds do not auto-rewrite values; they deliberately keep
large totals visible in the review queue so likely OCR decimal/comma mistakes
get human review before they spread downstream.

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

**Audit intake** — list pending receipt files without processing them:

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

Merchant-level certifications and claims are tracked as structured receipt
metadata rather than being folded into the merchant name or category. The
current intake flow extracts them from receipt text when possible, stores them
in the DB and sidecars, carries them into downstream batch JSON, and renders
them on merchant detail pages in `reports/merchants/`.

**Record or clear certifications manually** through the audited correction path:

```bash
intake correct 20110830_1149.pdf --certification "AAA=AAA certified" --note "shop badge visible on estimate"
intake correct 20110830_1149.pdf --clear-certifications --note "badge was OCR noise"
```

Keep these as structured claims with evidence. Do not create a separate
merchant-metadata file or another local correction log for certifications; use
`intake correct` so the DB row, sidecar, and `receipt_updates` history stay in
sync.

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

Reply to those Gmail notifications to leave hints or correction requests on the
receipt. Intake now records free-form reply notes in `receipt_updates` and
surfaces them in `intake review` as `email_reply_request (...)`. Safe
structured lines like `merchant: kroger`, `total: 88.05`, `purchaser: ...`,
`card last4: 5653`, or `currency: usd` are applied automatically through the
same audited update path.

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
