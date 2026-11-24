## Unreleased

- Review UI actions (on by default, `[mail_index] actions = false` turns them off): on a message page, **Return to Inbox**, **File under** (Promotions, Notices, Notes, Portfolio notifications), and **Mark read/unread**. Every action is a reversible Gmail change made with `STORE +/-X-GM-LABELS` or the `\\Seen` flag on the message's All Mail UID; nothing is ever deleted, trashed, sent or unsubscribed. Safety rails: label targets come only from a fixed whitelist, only queue labels are ever removed (other labels are untouched), the UID is re-checked against the message's `X-GM-MSGID` before changing anything so a reused UID can never change the wrong message, actions are serialised and logged, and a failure shows a plain message and changes nothing (the index included). A message returned to the Inbox stays there: triage now skips a Message-ID it already recorded, and the receipt loop leaves a `triage-*` message in place instead of filing it as already processed. Checked on real Gmail with a full round trip that ended in the exact starting state.
- Add the email review UI at `/mail` on `intake serve`, over the local mail index: a **Needs you** queue first, then Promotions, Notices, Notes, Notifications, CI, Processed, Sent and Everything else with unread counts, full-text search across the whole mailbox, and a message view (sender, recipients, labels, summary, indexed text, an Open-in-Gmail link). A **Mark reviewed** button takes a message out of the queue (a local flag; the mailbox is not touched). Mail text is always escaped and shown as plain text, never rendered as HTML; every response carries a strict CSP (`default-src 'none'`, so a tracking pixel cannot load) and `Cache-Control: no-store`; every POST needs a per-process CSRF token in a `SameSite=Strict` cookie plus the same token in the form, and an `Origin` matching the `Host`; the existing Host-header guard still applies. The routes only exist when an index has been built (`intake mail sync`).
- Add a local, searchable mail index (`intake mail sync | search | stats`), the foundation for an email review UI. It keeps one row per Gmail message (keyed by `X-GM-MSGID`, so a message is indexed once however many labels it has) in its own `data/mailindex.db` with sender, subject, date, labels, read state, a cleaned body excerpt and an FTS5 full-text index, and sorts every message into exactly one review queue (Needs you, Promotions, Notices, Notes, Notifications, CI, Processed, Sent, Everything else) from its labels. Sync reads `[Gmail]/All Mail` read-only (readonly select, `BODY.PEEK`), indexes new mail once, refreshes labels and read state of recent mail so triage's label moves show up, marks vanished mail as gone, skips attachments on large messages, and survives a UIDVALIDITY change; local review state is never overwritten.
- Triage safety: a year-wide dry run showed the local model would have filed real conversations (a banker's replies as promo, a relocation agent's and a support thread as notices, mail from your work address to other people as a note to yourself). The model may now only review mail that is itself automated (a `noreply`-style sender or a marketing/notification subdomain), never a reply or forward (`Re:`/`Fw:`/`In-Reply-To`/`References`), a role account (support, claims, service, billing...) or a named person; a note to yourself must be addressed only to your own addresses and not be a reply; role-like names (`support@`, `team@`, `hello@`, `info@`) are no longer treated as automated; and the keep keywords match plurals (`Tickets`). The same dry run now files 5 of 107 messages instead of 52, all from automated senders.
- Triage second stage, so the mail header rules cannot settle is processed too: a message from your own addresses (`own_addresses`) that is not portfolio-generated is a note and is filed to `intake/notes` (`file_notes`); and with `[email.triage] llm = true` a **local** Ollama model gets a second opinion on ambiguous automated mail and may file only a clear notice or marketing message (never a person's mail, a transaction, a threaded reply, or an unsure answer). Keep-verdicts are remembered in `data/triage-verdicts.json`, so the model is asked once per message and a dead model is tried once per poll. GitHub notification mail (`notifications@github.com`) is never triaged, since its bulk headers would have filed a pull-request comment as a promotion. New shared `intake/local_llm.py` client.
- Add `intake promos digest [--send] [--since-days N] [--no-model] [--install-timer]`: a separate, weekly job (own command and timer, independent of triage) that reads the promo label **read-only** and writes `reports/promos/promos-YYYYMMDD.md`: per sender, each mail's one-line summary from the local Ollama model plus its quoted offer and deadline. Facts come from regexes, so the digest is useful with no model; a dead model is asked once per run. Newsletter-pipeline senders are excluded, each message is digested once (`data/promo-digest-state.json`), and nothing is clicked, unsubscribed, or changed in the mailbox.
- Triage: bulk headers (`List-Unsubscribe`/`List-Id`/`Precedence`) now override the soft subject keywords marketing reuses (`trip`, `ticket`, `important`, `code`, `expires`, `limit`), so those promos are filed; a subject about money, security, a deadline or a real transaction (`receipt`, `payment`, `verification`, `order`, `confirmation`, `booking`, `itinerary`, `shipped`...) still keeps the message in the Inbox even with bulk headers.
- Triage files GitHub Actions quota notices (`You have used N% of the Actions minutes`) as notices instead of keeping them, since CI falls back to other runners; add your own with `[email.triage] notice_subject_regexes`. A ticket/pass QR-code delivery (`<shop> QR code for Your Ordered Ticket`) is now filed as a `documents/tickets` record tied to its order number rather than left `unmatched` in the Inbox (the order confirmation stays the receipt; the pick-up code is not stored). Triage prints or logs each decision once, not both.
- Add header-only Inbox triage (`intake email triage [--dry-run]`, and a daemon pass behind `[email.triage] enabled`). Receipt discovery only ever looked at allow-listed senders and subject keywords, so everything else piled up unexamined in the Inbox. Triage files bulk mail (`List-Unsubscribe`/`List-Id`/`Precedence`) to `intake/promos`, no-reply mail to `intake/notices` and portfolio-generated mail to `intake/notify`, unread and reversibly; anything transactional, an alert (billing, usage, security, codes) or from a person stays. Kept mail is never recorded, so it can still become a receipt candidate. Newsletter-pipeline senders are left to it only while the mail is inside its 7-day look-back; an older issue can never be digested, so it is filed like other bulk mail (this is what stranded the early-November Token Metrics Pulse issues). Receipt discovery now also searches `ordered ticket`, `e-ticket`, `booking confirmation` and `itinerary`.
- Keep a rotating, timestamped `data/logs/intake.log` of everything intake prints (`log_dir`, `log_max_bytes`, `log_backups`), and log each finance trigger's start, result and, on failure, the full downstream stdout/stderr plus the receipts JSON to re-run. A failed trigger's error now says the receipts were not delivered and names that JSON; before, only the last stderr line survived and the traceback was in another repo's log.
- Route recent Schwab, Binance, SMBC, Social Security, card-decline, Amex,
  Progressive, and Booking.com mail deterministically instead of leaving it
  unmatched or allowing promotional amounts to become receipts.
- File Codecov bot comments under a dedicated Gmail label and recognize newer
  Token Metrics Pulse layouts without relying on legacy section headings.
- File no-receipt mail from known provider senders (security alerts, promos,
  statements, rate notices) to `email.notice_label` (default `intake/notices`),
  unread, instead of leaving it in the Inbox and re-checking it every poll.
- File GitHub Actions run mail (`Run failed: CI …`) to `email.ci_label` (default
  `GitHub/CI`), unread so red builds still stand out.
- Discover Uber trip and Uber Eats receipts, including copies forwarded from another
  address, by subject; their subjects never contain "receipt".
- Parse Uber ride receipts deterministically (merchant, total in its own currency such
  as JPY, trip date rather than the forward's date, card last four) as `travel/transit`.

- Support iPhone HEIC/HEIF receipts (`.heic`, `.heif`). They were never in the supported list, so uploaded photos sat in the inbox forever. They are read with `pillow-heif` (now a dependency) in the extractor sandbox or in-process, with the phone's orientation applied. Where HEIC cannot be decoded (no `pillow-heif` and no sandbox) a HEIC file is unsupported, not corrupt: it is left alone and no corrupt-upload alert is sent. Unreadable or truncated HEIC files are reported corrupt like any other image.

- Make `deploy/k3s/verify.sh` executable (it was committed without the bit, so `deploy/deploy.sh --mode k3s up` stopped at the verify step with "Permission denied"), and add a test that every deploy script is executable.

- Add `deploy/deploy.sh [--mode dev|host|k3s] up|down|status|verify|mode`, with the mode from `--mode`, `INTAKE_DEPLOY_MODE`, or `config/deploy.local.env` (example: `config/deploy.example.env`, gitignored), defaulting to `dev`. It chooses where untrusted receipt files are parsed: a foreground sandbox service (`dev`), the daemon's own process (`host`), or a K3s pod with no network, no secrets and none of your files (`k3s`). Only `k3s` touches kubectl or podman. The daemon is switched to the sandbox only after the pod is verified and reachable through a loopback bridge, and any failure restores in-process parsing. `docs/deployment.md` explains each mode and how to switch.

- Add an optional extractor sandbox. With `INTAKE_EXTRACTOR=sandbox` the daemon sends receipt bytes to a service
  (`python -m intake.extractor_service`) that parses them in a child process with a timeout and CPU limit, and gets
  text, a PNG preview or a decode check back, so PDF, image and video parsers never run on files an attacker can
  shape inside the daemon's process. It fails closed (an unreachable sandbox skips the cycle; nothing is parsed
  locally instead), refuses oversize and unsupported files, never echoes document content in errors, and
  reports an unreachable sandbox as unavailable, not as a corrupt receipt. The default is `local`, unchanged.
  Preview images are now passed as PNG bytes (`ocr.render_preview_png`) so the host does not decode them.

- Capture employers' job-alert digests: senders listed in `[career.alerts]` are saved as `private/job-alerts/<id>.json` in the career repo with the posting links on the employer's own domain, for career's `mail-leads`. With `[career].file_mail` the digest is labelled `<label_prefix>/Job alerts` and stays in Inbox, unread. A message from an alert sender that answers an application is still a career action.

- Refuse report requests (GET and HEAD) whose `Host` header is not a loopback name or this site's registered hostname (from the portfolio site store, or `PORTFOLIO_ALLOWED_HOSTS`). The report server had no host check, so a DNS-rebinding page, which sends its own hostname as `Host`, could read the generated reports (receipts, spending) as "same origin". The shared check lives in the vendored portfolio site seam (re-vendored here). A server bound beyond loopback with no registered name is not checked. **Upgrade note:** a deployment reached through a proxy under an unregistered name will get 403.

- Authenticate the sender of a reply, not just its `From:` header: a reply is acted on only when the receiving server's topmost `Authentication-Results` header (default `mx.google.com`) reports `dmarc=pass` for the sender's own domain. Otherwise it is recorded as `reply-unauthenticated` and ignored. This fails closed: a mailbox whose server adds no such header needs `[email].require_authenticated_replies = false`; `[email].authentication_server` names a different trusted server.

- Tighten the Kalshi Spam-recovery trust check: only Gmail's own (topmost) `Authentication-Results` header counts, so a forged header supplied by the sender can no longer authenticate a message, and the signing domain must be `privaterelay.appleid.com` itself rather than a longer name that begins with it. An infinite `notification_inbox_grace_*` value now falls back to the default instead of raising `OverflowError`.

- Limit intake's mail to `[email].allowed_addresses`: replies from any other sender are recorded as `reply-untrusted` and never acted on (no receipt change, no Snowbridge link), and no notification, file-link reply or newsletter digest is sent to any other address. With no list configured nobody is allowed and intake says so on stderr, so **set `allowed_addresses` in your local `settings.toml`** or notifications stop. The check reads the `From` header only; DKIM/SPF verification is a separate step.
- Fall back to local OCR and the local translation model whenever a remote translation fails: if OpenAI errors, has no API key, or returns nothing usable (or the text unchanged), the receipt text is translated by the local Crew Chief/Ollama model instead, for `translation_provider = "openai"` at ingest and for `intake translate --provider openai` (which re-reads the receipt with local OCR). The stored translation metadata records `fallback_from`. A disabled or unknown provider never falls back, and if the local model fails too the receipt keeps its OCR text untranslated.
- Re-vendor the portfolio theme fallback: the top bar's theme menu offers This site / Everywhere and "Follow the global theme".
- Answer `/_portfolio/*` itself from a vendored portfolio site seam (`intake/portfolio/`), so the theme and top bar work with no Caddy in front: the owner-only site store first, then a pinned fallback copy. Add `/.portfolio` (ignored) and `config/portfolio-site.example.toml`. See traction-control `docs/portfolio-sites.md`.

- Show the "Extraction Model Mix" chart at full width; it was the only half-width card in its row and left the other half empty.

- Follow the portfolio system themes: the report pages take their colours from the shared tokens (`/_portfolio/theme.css`), so the theme chosen in the top bar restyles them. Reports generated before this change keep their old colours until `intake report` is run again.

- Load the portfolio top bar (`<script src="/_portfolio/chrome.js" defer></script>`, served by the shared Caddy) on the report pages it serves (added when a page is served, so reports generated earlier get it too), so the global navigation and theme rotator appear here too.
- Rename the career-mail module to `intake.job_search_mail` (and its test) so the
  repository no longer carries a path that matches a private repository name.
  Behaviour, the `[career]` settings section, the `intake career` command, and
  the default `Career` label prefix are unchanged.

### Added
- Career mail filing (`[career] file_mail`, `label_prefix`): each career
  email gets a `<label_prefix>/<Kind>` Gmail label; requests, screenings, interviews,
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
