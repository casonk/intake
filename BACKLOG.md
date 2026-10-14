# BACKLOG.md

Portfolio backlog for this repository. Pending items are candidates for execution —
manually or via crew-chief. Entries sourced from archility audit are tagged
`[archility:YYYY-MM-DD]`; manual entries use `[manual:YYYY-MM-DD]`.

The archility twice-weekly job populates this file automatically via `archility audit --write-backlog`.
To execute a backlog item with crew-chief: `crew-chief agent "Work on item: <item text>"`.
Mark items `[x]` when complete and move them to Done.

## Pending

## In Progress

## Done

- [x] [manual:2026-06-15] Extract the file-inbox document routing pipeline from `intake/cli.py` into a dedicated module so `process` and daemon ingestion share one implementation for duplicate handling, OCR, document-type routing, moves, sidecars, DB writes, notifications, and finance events.
- [x] [manual:2026-06-15] Move large inline report templates out of `intake/report.py` into tracked template files, preserving the static HTML/Markdown output contract and focused report regression tests.
- [x] [manual:2026-06-15] Split low-level email body text, IMAP provider, and filing/save helpers out of `intake/email.py` while preserving compatibility wrappers for existing tests and patch points.
- [x] [manual:2026-06-15] Finish splitting `intake/email.py` by moving structured finance parsing and notification/reply handling into smaller modules while keeping Gmail ingestion behavior and existing regression coverage stable.
