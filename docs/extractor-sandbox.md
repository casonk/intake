# The extractor sandbox

The parsers for receipt files (pdfplumber, Pillow, tesseract, ffmpeg) read content an attacker can shape: an email
attachment, an upload to the shared folder. This document describes the optional sandbox that keeps them out of the
daemon's process, which holds your mailbox, API keys and database.

## Modes

| `INTAKE_EXTRACTOR` | What happens |
|---|---|
| `local` (default) | Files are parsed in-process, as intake always has. Nothing else is needed. |
| `sandbox` | Files are sent as bytes to the extractor service at `INTAKE_EXTRACTOR_URL` (default `http://127.0.0.1:5455`). |

Anything else is an error: an unknown value never quietly means `local`.

Related settings: `INTAKE_EXTRACTOR_TIMEOUT` (seconds the daemon waits, default 180) and
`INTAKE_EXTRACTOR_MAX_BYTES` (default 100 MiB; larger files are refused before they are sent).

## What the daemon does, and does not do, in `sandbox` mode

- Text extraction, preview rendering for the vision models, and the "is this image readable?" check all go to the
  service. The daemon only reads the file's bytes; it does not parse them.
- Preview images come back as PNG bytes and go straight into the model request. The daemon does not decode them.
- **Fail closed.** If the sandbox does not answer, the daemon logs it, skips the inbox for that cycle and leaves the
  files where they are. It does **not** parse locally instead. An outage never marks a receipt corrupt.
- A file the sandbox refuses or cannot parse is an ordinary per-file error, as a bad PDF always was.
- Not covered: the manual `intake merge` command reads PDFs with `pypdf` in its own process.

## The service

`python -m intake.extractor_service [--host 0.0.0.0] [--port 5455]` runs a small HTTP server:

| Request | Answer |
|---|---|
| `GET /healthz` | `200 {"ok": true}` |
| `POST /extract?suffix=.pdf` (body: the file) | `200 {"text": "..."}` |
| `POST /preview?suffix=.png&resolution=150` | `200` PNG bytes |
| `POST /decode?suffix=.jpg` (images only) | `200 {"decodes": true|false}` (any decoder failure is `false`) |

Refusals are `{"error": ...}` with 400 (unsupported type or bad query), 411 (no `Content-Length`), 413 (too large),
422 (the parser failed or timed out) or 503 (busy). Error text never contains document content.

Each document is written to a private temporary directory and parsed by `python -m intake.extractor_worker` in a
child process with a CPU limit and a wall-clock timeout, so a hostile file can hang or crash a child but not the
service. Run the service in a container with no network, no secrets and no access to the receipts folder; the
`deploy/` folder does this for K3s.
