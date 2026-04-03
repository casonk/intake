"""Email receipt ingestion via IMAP (shock-relay integration).

Searches Gmail IMAP for receipt / invoice emails, saves PDF attachments
to the receipt inbox for normal OCR backfill, parses inline HTML receipts
directly, and moves processed emails to a designated Gmail label.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default as default_policy
from html.parser import HTMLParser
from pathlib import Path

from intake.db import is_email_processed, record_email_receipt

# ── HTML → plain text ─────────────────────────────────────────────────────────


class _StripHTML(HTMLParser):
    """Minimal HTML → plain-text stripper (no extra deps)."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # Collapse excessive whitespace while preserving newlines
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def html_to_text(html: str) -> str:
    parser = _StripHTML()
    parser.feed(html)
    return parser.text()


# ── shock-relay import helper ──────────────────────────────────────────────────


def _load_shock_relay(config_path: str):
    """Import shock-relay common module by adding its dir to sys.path."""
    module_dir = str(Path(config_path).parent.resolve())
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    try:
        import common as sr  # noqa: PLC0415

        return sr
    except ImportError as exc:
        raise RuntimeError(
            f"Cannot import shock-relay common from {module_dir}: {exc}\n"
            "Ensure the shock-relay repo is accessible and the path in "
            "settings.toml [email] shock_relay_config is correct."
        ) from exc


# ── core fetch logic ───────────────────────────────────────────────────────────


class FetchResult:
    """Outcome of a single email receipt fetch run."""

    def __init__(self) -> None:
        self.found = 0
        self.skipped = 0
        self.attachments_saved = 0
        self.html_processed = 0
        self.errors = 0
        self.saved_files: list[Path] = []

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"FetchResult(found={self.found}, skipped={self.skipped}, "
            f"attachments={self.attachments_saved}, html={self.html_processed}, "
            f"errors={self.errors})"
        )


def fetch_receipts(
    settings: dict,
    db_conn,
    dry_run: bool = False,
    verbose: bool = True,
) -> FetchResult:
    """Connect to Gmail, find receipt emails, save PDFs + process HTML bodies.

    PDF attachments are saved to ``receipt_dir/intake/`` for normal backfill.
    HTML inline receipts are parsed and upserted into the DB directly.
    Successfully processed emails are marked read and moved to the processed label.
    """
    email_cfg = settings.get("email", {})
    shock_config_path = email_cfg.get("shock_relay_config", "")
    if not shock_config_path:
        raise RuntimeError(
            "settings.toml is missing [email] shock_relay_config. "
            "Set it to the path of your shock-relay gmail-imap config."
        )

    sr = _load_shock_relay(shock_config_path)
    config = sr.load_config(shock_config_path)

    receipt_dir = Path(settings["receipt_dir"])
    inbox_dir = receipt_dir / "intake"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    processed_label = email_cfg.get("processed_label", "intake/processed")
    since_days: int = int(email_cfg.get("since_days", 90))
    limit: int = int(email_cfg.get("limit", 500))
    subjects: list[str] = email_cfg.get(
        "receipt_subjects",
        ["receipt", "invoice", "order confirmation", "your order", "order #"],
    )

    result = FetchResult()

    sr.validate_imap(config)
    mailbox = config.imap.mailboxes[0] if config.imap.mailboxes else "INBOX"

    try:
        with sr.open_imap_connection(config) as conn:
            # Select with write access to allow mark-read + move
            status, _ = conn.select(mailbox, readonly=False)
            if status != "OK":
                raise RuntimeError(f"Cannot select mailbox {mailbox!r}")

            uids = _search_receipt_uids(conn, config, subjects, since_days, limit)
            result.found = len(uids)

            if verbose:
                print(f"  Found {len(uids)} candidate email(s) in {mailbox}")

            to_expunge = False
            for uid in uids:
                try:
                    _status, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
                    payload = sr.extract_fetch_payload(msg_data)
                    if not payload:
                        result.skipped += 1
                        continue

                    msg = message_from_bytes(payload, policy=default_policy)
                    message_id = sr.decode_mime_header(msg.get("Message-ID"))
                    subject = sr.decode_mime_header(msg.get("Subject"))
                    sender = sr.decode_mime_header(msg.get("From"))
                    date_str = str(msg.get("Date", "") or "").strip()

                    # Dedup check
                    if is_email_processed(db_conn, uid, mailbox):
                        if verbose:
                            print(f"    skip (already processed): {subject!r}")
                        result.skipped += 1
                        continue

                    if verbose:
                        print(f"  Processing: {subject!r}  from={sender}")

                    attachments = sr.extract_attachments(payload)
                    pdf_attachments = [
                        (fn, ct, data)
                        for fn, ct, data in attachments
                        if ct == "application/pdf" or fn.lower().endswith(".pdf")
                    ]

                    saved_count = 0
                    if not dry_run:
                        for filename, _ct, data in pdf_attachments:
                            dest = _safe_save(inbox_dir, filename, data)
                            result.saved_files.append(dest)
                            saved_count += 1
                            if verbose:
                                print(f"    saved attachment → {dest.name}")

                    result.attachments_saved += saved_count

                    # HTML inline receipt (no PDF attachments or alongside them)
                    _text_body, html_body = sr.extract_message_bodies(msg)
                    if html_body and not dry_run:
                        plain = html_to_text(html_body)
                        if _looks_like_receipt(plain):
                            _upsert_html_receipt(
                                db_conn,
                                uid,
                                mailbox,
                                subject,
                                sender,
                                date_str,
                                plain,
                                settings,
                            )
                            result.html_processed += 1
                            if verbose:
                                print("    inline receipt → processed via AI")

                    if not dry_run:
                        record_email_receipt(
                            db_conn,
                            uid=uid,
                            mailbox=mailbox,
                            message_id=message_id,
                            subject=subject,
                            sender=sender,
                            received_at=date_str,
                            attachment_count=saved_count,
                        )
                        sr.mark_message_read(conn, uid)
                        sr.move_message(conn, uid, processed_label)
                        to_expunge = True

                except Exception as exc:
                    result.errors += 1
                    if verbose:
                        print(f"    ERROR uid={uid}: {exc}")

            if to_expunge:
                conn.expunge()

    except Exception as exc:
        raise RuntimeError(f"Email fetch failed: {exc}") from exc

    return result


# ── helpers ────────────────────────────────────────────────────────────────────


def _search_receipt_uids(
    conn, config, subjects: list[str], since_days: int, limit: int
) -> list[int]:
    """Search the selected mailbox for messages matching any receipt subject."""
    from common import build_search_terms, parse_int_uid_list  # noqa: PLC0415

    seen: set[int] = set()
    for keyword in subjects:
        terms = build_search_terms(subject_contains=keyword, since_days=since_days)
        charset = config.imap.search_charset
        status, data = conn.uid("search", charset, *terms)
        if status != "OK":
            continue
        for uid in parse_int_uid_list(data):
            seen.add(uid)
    # Return most-recent first, capped at limit
    return sorted(seen, reverse=True)[:limit]


def _safe_save(dest_dir: Path, filename: str, data: bytes) -> Path:
    """Write *data* to *dest_dir/filename*, avoiding clobbers with a suffix."""
    safe = re.sub(r"[^\w\-. ]", "_", filename).strip() or "attachment.pdf"
    dest = dest_dir / safe
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        dest = dest_dir / f"{stem}_{ts}{suffix}"
    dest.write_bytes(data)
    return dest


_RECEIPT_PATTERNS = re.compile(
    r"\b(total|amount\s+due|order\s+total|charged|subtotal|invoice|receipt\s+#)\b",
    re.IGNORECASE,
)


def _looks_like_receipt(text: str) -> bool:
    """Heuristic: does this email body text resemble a receipt?"""
    return bool(_RECEIPT_PATTERNS.search(text))


def _upsert_html_receipt(
    db_conn, uid, mailbox, subject, sender, date_str, plain_text, settings
) -> None:
    """Try to extract fields from an inline HTML receipt and upsert into DB."""
    from intake.classify import classify, load_rules  # noqa: PLC0415
    from intake.db import upsert_receipt  # noqa: PLC0415
    from intake.notify import send_processed_receipt_notifications  # noqa: PLC0415
    from intake.personal_finance import build_receipt_event, trigger_personal_finance  # noqa: PLC0415
    from intake.parse import parse_receipt  # noqa: PLC0415

    rules = load_rules()
    # Build a synthetic filename for the receipt (uid-based)
    synthetic_name = f"email_{uid}_{mailbox.replace('/', '_')}.txt"
    receipt = parse_receipt(synthetic_name, plain_text)

    # Try AI extraction if API key available
    api_key = settings.get("openai_api_key")
    if api_key:
        import openai  # noqa: PLC0415

        from intake.ai import merge_ai_result  # noqa: PLC0415

        try:
            client = openai.OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model=settings.get("openai_model_high", "gpt-4o"),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Extract receipt fields from this email body and return ONLY valid JSON "
                            'with keys: "merchant", "total" (float), "receipt_date" (YYYY-MM-DD), '
                            '"receipt_time" (HH:MM), "purchaser", "card_last4". Use null for missing.\n\n'
                            f"Subject: {subject}\nFrom: {sender}\n\n{plain_text[:3000]}"
                        ),
                    }
                ],
                max_tokens=256,
            )
            import json

            raw = (response.choices[0].message.content or "{}").strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            ai_data = json.loads(raw)
            receipt = merge_ai_result(receipt, ai_data)
        except Exception:
            pass

    category = classify(receipt.merchant or "", rules, full_text=plain_text)
    upsert_receipt(
        db_conn,
        {
            "filename": synthetic_name,
            "scan_date": (receipt.receipt_date or datetime.now(UTC).date()).isoformat(),
            "merchant": receipt.merchant,
            "total": receipt.total,
            "category": category,
            "receipt_dir": f"email/{category}",
            "raw_text": plain_text[:4000],
            "processed_by": (
                settings.get("openai_model_high", "gpt-4o") if api_key else "email-parse"
            ),
            "purchaser": receipt.purchaser,
            "card_last4": receipt.card_last4,
        },
    )
    _, errors = send_processed_receipt_notifications(
        settings,
        receipt,
        category=category,
        receipt_dir=f"email/{category}",
        filename=synthetic_name,
        processed_by=(settings.get("openai_model_high", "gpt-4o") if api_key else "email-parse"),
    )
    for channel, error in errors.items():
        print(f"    [{channel}] WARNING: {error}", file=sys.stderr)
    try:
        trigger_personal_finance(
            settings,
            [
                build_receipt_event(
                    receipt,
                    category=category,
                    receipt_dir=f"email/{category}",
                    filename=synthetic_name,
                    processed_by=(
                        settings.get("openai_model_high", "gpt-4o") if api_key else "email-parse"
                    ),
                    tier="email",
                )
            ],
            trigger="email",
        )
    except Exception as exc:
        print(f"    [finance-pipeline] WARNING: {exc}", file=sys.stderr)


# ── daemon ─────────────────────────────────────────────────────────────────────


def run_daemon(settings: dict, db_conn, verbose: bool = True) -> None:
    """Poll for new receipt emails indefinitely.

    Designed for systemd: runs until killed (SIGTERM/SIGINT).
    """
    email_cfg = settings.get("email", {})
    interval: int = int(email_cfg.get("poll_interval_seconds", 300))

    if verbose:
        print(f"intake email daemon started (poll every {interval}s) — Ctrl-C to stop")

    while True:
        try:
            result = fetch_receipts(settings, db_conn, verbose=verbose)
            if verbose:
                print(
                    f"  fetch done: {result.attachments_saved} attachment(s), "
                    f"{result.html_processed} inline, {result.errors} error(s)"
                )
        except Exception as exc:
            if verbose:
                print(f"  fetch error: {exc}")
        time.sleep(interval)
