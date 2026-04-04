# LESSONSLEARNED.md

Tracked durable lessons for `intake`.
Unlike `CHATHISTORY.md`, this file should keep only reusable lessons that should change how future sessions work in this repo.

## How To Use

- Read this file after `AGENTS.md` and before `CHATHISTORY.md` when resuming work.
- Add lessons that generalize beyond a single session.
- Keep entries concise and action-oriented.
- Do not use this file for transient status updates or full session logs.

## Lessons

- Document the repository around its real execution, curation, or integration flow instead of only the top-level folder list.
- Keep local-only, private, reference-only, or generated boundaries explicit so published or runtime behavior is not confused with offline material or non-committable inputs.
- Re-run repo-appropriate validation after changing generated artifacts, diagrams, workflows, or other CI-facing files so formatting and compatibility issues are caught before push.
- Receipt PDFs from snowbridge may be image-only scans (no text layer); always attempt pdfplumber extraction first and fall back to pytesseract rather than assuming one approach works for all files.
- When inbox processing stops unexpectedly, check `systemctl --user status intake-daemon` first; an `inactive` service with `status=200/CHDIR` means systemd could not enter the configured working directory, so the daemon never reached its own volume-availability checks.
- For editable installs launched by systemd, resolve repo-local config and output paths from `intake/cli.py` instead of caller cwd, and keep the unit `WorkingDirectory` on a stable path like `%h` rather than a mount-dependent repo path.
- Outbound receipt notifications should be best-effort: wire them into every successful ingest path, but never let a Signal or relay failure block the receipt move, sidecar write, or DB upsert.
- For expensive downstream consumers like `personal-finance`, emit a stable batch JSON and trigger once per successful intake batch (`process`/`backfill`/daemon cycle) instead of spawning one heavyweight downstream run per receipt.
- When both inbound Gmail polling and outbound Gmail notifications are enabled, reuse the same `shock-relay` gmail-imap config by default and keep the notification-specific recipient/subject settings in a separate `[gmail]` section rather than overloading `[email]`.
- For scheduled report generation, use a separate oneshot systemd service plus timer rather than embedding report writes in the long-running intake daemon loop; that keeps scheduling explicit and lets the report be run on demand through the same service unit.
- Route tracked systemd unit generation through `./util-repos/clockwork` instead of expanding more inline service/timer strings in `intake/cli.py`; keep the public `intake ... --install` commands stable, but let `clockwork` own the scheduler artifact shape.
- Keep generated reports self-contained: prefer inline SVG/CSS charts in HTML and simple text-bar fallbacks in Markdown so scheduled report artifacts stay portable and do not depend on external JS, CDNs, or browser-only runtime data fetching.
- For merchant cleanup, exact keyword hints across the full OCR text recover real retailers more reliably than fuzzy matching only the first merchant line; use the full receipt text to catch later hints like `KROGER PLUS` or `Walmart.com`.
- For local reports meant to be used as navigation surfaces, emit `file:///...` links for both folders and receipt PDFs so the report doubles as a drill-down index instead of just a static summary.
- Total extraction should be line-aware rather than regex-last-match: exclude non-payment totals like loyalty points/items/tax/discount lines, and when no grand total is printed, compute the payable amount from subtotal plus taxes/fees minus discounts.
- Purchaser extraction should require stronger label boundaries for generic words like `customer` or `name`; otherwise OCR phrases such as `customer satisfaction` can be mis-stored as cardholder names.
- Category keyword matching should be token/phrase-aware rather than raw substring matching; otherwise short fuel keywords like `mobil` will falsely classify telecom receipts whose text contains `mobile`.
- Telecom merchants often print the legal entity rather than the customer-facing brand; add explicit canonical merchant patterns like `vodafone` so report rollups do not split between brand and legal-company variants.
- In report recent tables, category links should be derived from the normalized category path, not the receipt file path; synthetic/email rows do not have local PDFs, but their category folders can still be linked under the receipt root.
- Only emit `file:///...` links for report file/folder targets that actually exist; otherwise show plain text so missing files do not create dead links in the report UI.
- Kroger OCR can degrade into variants like `POGER PLUS` or `kreager.com`; merchant recovery should include known-pattern hints for these later-line variants instead of trusting the first OCR line.
- Gmail-rendered receipt PDFs often put browser chrome or message headers on the first line; merchant recovery should prefer later sender/domain/store signals like `orders@em.autozone.com` over the opening `Gmail - ...` text.
- Payment-card footer text like `Cardholder Signature Verified` or `Customer Copy` should be blacklisted from purchaser extraction, or OCR cleanup will reintroduce false purchaser names during merchant-correction sweeps.
- Bass Pro receipts can OCR down to `World` while still containing later `basspro.com` signals; add domain-aware merchant patterns for these print-receipt cases instead of relying on the first merchant line.
- Canonicalization-only merchant sweeps must not blindly overwrite previously AI-derived totals or card metadata with weaker Tesseract output; when only the merchant casing/normalization is changing, preserve higher-fidelity numeric fields unless a fresh extraction actually improves them.
- Recent-report navigation should not trust `receipt_dir` blindly: when a stored PDF path is stale, fall back to a unique on-disk filename match, and when an email/synthetic receipt has no local file, link its row through to the best available category folder instead of leaving a dead or missing target.
- Merchant consolidation belongs in a config-driven alias map (`[merchants]` in `categories.toml`) rather than only hardcoded regex patterns; canonical entries with empty alias lists handle case/punctuation normalization, while explicit alias lists handle clean-text name variants (e.g. "O'Reilly Auto Parts" → "o'reilly").
- A post-ingestion `consolidate` command that patches the DB and sidecars in-place (no re-OCR) is more practical than re-running backfill; it should sweep both DB-tracked rows and all `.intake.json` files on disk to catch orphaned sidecars.
- Sidecar-only backfill upserts must guard against filename collisions: if the filename already maps to a different `receipt_dir` in the DB, skip rather than overwrite the existing row silently.
- Duplicate receipt detection (same merchant + total within a day window) is best expressed as a separate `dedup` command with `--remove`; auto-selecting the keeper by model rank then field completeness (purchaser/card_last4) then processed_at avoids interactive prompts while still being deterministic.
- When `dedup --remove` drops a row that held unique purchaser data, that field is lost; consider a merge-fields-into-keeper step before deletion, or at minimum surface the discarded fields in the output so the user can manually patch the keeper row.
