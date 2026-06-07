"""Email receipt ingestion via IMAP (shock-relay integration).

Searches Gmail IMAP for receipt / invoice emails, saves PDF attachments
to the receipt inbox for normal OCR backfill, parses inline HTML receipts
directly, and moves processed emails to a designated Gmail label.
"""

from __future__ import annotations

import re
import sys
import time
from contextlib import suppress
from datetime import UTC, date, datetime
from email import message_from_bytes
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from intake.db import (
    get_receipt,
    infer_purchaser_from_card_last4,
    is_email_message_id_processed,
    is_email_processed,
    record_email_receipt,
    record_receipt_update,
    update_receipt_fields,
)
from intake.naming import allocate_canonical_path
from intake.purchaser import normalize_purchaser

_DEFAULT_RECEIPT_SENDER_HINTS = (
    "service@paypal.com",
    "venmo@venmo.com",
    "account.irs.gov",
    "nelnet.studentaid.gov",
    "noreply@wise.com",
    "reserve-noreply@xanterra.com",
    "americanexpress.com",
)
_DEFAULT_REPLY_MAILBOXES = (
    "[Gmail]/All Mail",
    "[GoogleMail]/All Mail",
    "[Gmail]/Sent Mail",
    "[GoogleMail]/Sent Mail",
    "All Mail",
    "Sent",
    "Sent Mail",
    "Sent Items",
)
_INTAKE_REPLY_SOURCE = "email-reply"
_INTAKE_REPLY_NOTE_SOURCE = "email-reply-note"
_INTAKE_REPLY_LINK_SOURCE = "email-reply-link"
_INTAKE_RECEIPT_FILENAME_HEADER = "X-Intake-Receipt-Filename"
_INTAKE_RECEIPT_DIR_HEADER = "X-Intake-Receipt-Dir"
_PORTFOLIO_SERVICE_HEADER = "X-Portfolio-Service"
_CREW_CHIEF_INTENT_HEADER = "X-Crew-Chief-Intent"
_INTAKE_SERVICE_VALUE = "intake"
_INTAKE_GENERATED_INTENTS = {"notify", "reply-link"}

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
        self.reply_processed = 0
        self.reply_applied = 0
        self.reply_unmatched = 0
        self.errors = 0
        self.saved_files: list[Path] = []

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"FetchResult(found={self.found}, skipped={self.skipped}, "
            f"attachments={self.attachments_saved}, html={self.html_processed}, "
            f"reply_processed={self.reply_processed}, reply_applied={self.reply_applied}, "
            f"reply_unmatched={self.reply_unmatched}, "
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
    Raw RFC822 messages for handled receipts are archived under the receipt root.
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
    notification_label = email_cfg.get("notification_label", "intake/notify")
    since_days: int = int(email_cfg.get("since_days", 90))
    limit: int = int(email_cfg.get("limit", 500))
    configured_subjects: list[str] = list(
        email_cfg.get(
            "receipt_subjects",
            ["receipt", "invoice", "order confirmation", "your order", "order #"],
        )
    )
    subjects = list(
        dict.fromkeys(
            [
                *configured_subjects,
                "we've received your payment",
                "we processed your payment",
                "your payment of",
                "your scheduled payment",
                "policy renewal reminder",
                "reservation confirmation",
            ]
        )
    )
    sender_hints = _configured_receipt_sender_hints(email_cfg)

    result = FetchResult()

    sr.validate_imap(config)
    mailbox = config.imap.mailboxes[0] if config.imap.mailboxes else "INBOX"

    try:
        with sr.open_imap_connection(config) as conn:
            # Select with write access to allow mark-read + move
            status, _ = conn.select(mailbox, readonly=False)
            if status != "OK":
                raise RuntimeError(f"Cannot select mailbox {mailbox!r}")

            uids = _search_receipt_uids(
                conn,
                config,
                subjects,
                sender_hints=sender_hints,
                since_days=since_days,
                limit=limit,
            )
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

                    if _is_intake_generated_message(msg) or _is_internal_intake_notification(
                        subject, sender, settings
                    ):
                        tracking = _email_message_tracking_state(
                            db_conn,
                            uid=uid,
                            mailbox=mailbox,
                            message_id=message_id,
                        )
                        filed = False
                        if not dry_run and not tracking["tracked"]:
                            record_email_receipt(
                                db_conn,
                                uid=uid,
                                mailbox=mailbox,
                                message_id=message_id,
                                subject=subject,
                                sender=sender,
                                received_at=date_str,
                                attachment_count=0,
                                status="internal",
                            )
                        elif not dry_run and _internal_notification_ready_to_file(
                            tracking,
                            grace_seconds=_notification_inbox_grace_seconds(email_cfg),
                        ):
                            filed = _move_message_without_marking_read(
                                sr,
                                conn,
                                uid,
                                source_mailbox=mailbox,
                                processed_label=notification_label,
                            )
                            to_expunge = to_expunge or filed
                            if filed:
                                record_email_receipt(
                                    db_conn,
                                    uid=uid,
                                    mailbox=mailbox,
                                    message_id=message_id,
                                    subject=subject,
                                    sender=sender,
                                    received_at=date_str,
                                    attachment_count=0,
                                    status="internal-filed",
                                )
                        if verbose:
                            if dry_run:
                                suffix = ""
                            elif filed:
                                suffix = f"; filed to {notification_label!r}"
                            elif tracking["tracked"]:
                                suffix = "; already tracked"
                            else:
                                suffix = ""
                            print(f"    skip (internal intake message{suffix}): {subject!r}")
                        result.skipped += 1
                        continue

                    # Dedup check
                    if is_email_processed(db_conn, uid, mailbox) or (
                        message_id and is_email_message_id_processed(db_conn, message_id)
                    ):
                        if not dry_run:
                            filed = _file_message_to_processed_label(
                                sr,
                                conn,
                                uid,
                                source_mailbox=mailbox,
                                processed_label=processed_label,
                            )
                            to_expunge = to_expunge or filed
                        if verbose:
                            suffix = f"; filed to {processed_label!r}" if not dry_run else ""
                            print(f"    skip (already processed{suffix}): {subject!r}")
                        result.skipped += 1
                        continue

                    if _is_intake_reply_message(msg, settings):
                        if verbose:
                            print(f"    skip (intake reply; handled by reply scan): {subject!r}")
                        result.skipped += 1
                        continue

                    text_body, html_body = sr.extract_message_bodies(msg)
                    body_text = _combine_message_body_text(text_body, html_body)
                    if _is_known_non_receipt_notice(subject, sender, body_text):
                        if not dry_run:
                            record_email_receipt(
                                db_conn,
                                uid=uid,
                                mailbox=mailbox,
                                message_id=message_id,
                                subject=subject,
                                sender=sender,
                                received_at=date_str,
                                attachment_count=0,
                                status="ignored",
                            )
                            filed = _file_message_to_processed_label(
                                sr,
                                conn,
                                uid,
                                source_mailbox=mailbox,
                                processed_label=processed_label,
                            )
                            to_expunge = to_expunge or filed
                        if verbose:
                            suffix = f"; filed to {processed_label!r}" if not dry_run else ""
                            print(f"    skip (known non-receipt notice{suffix}): {subject!r}")
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
                    inline_processed = False
                    if not dry_run:
                        for filename, _ct, data in pdf_attachments:
                            dest = _safe_save_receipt_pdf(
                                inbox_dir,
                                filename,
                                data,
                                received_at=date_str,
                            )
                            result.saved_files.append(dest)
                            saved_count += 1
                            if verbose:
                                print(f"    saved attachment → {dest.name}")

                    result.attachments_saved += saved_count

                    # HTML inline receipt (no PDF attachments or alongside them)
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
                            inline_processed = True
                            if verbose:
                                print("    inline receipt → processed via AI")

                    if not dry_run:
                        if not _email_was_handled(saved_count, inline_processed):
                            if verbose:
                                print(f"    skip (no receipt content matched): {subject!r}")
                            result.skipped += 1
                            continue
                        archived_email = None
                        try:
                            archived_email = _write_processed_email_artifact(
                                settings,
                                uid=uid,
                                mailbox=mailbox,
                                message_id=message_id,
                                subject=subject,
                                sender=sender,
                                received_at=date_str,
                                payload=payload,
                                attachment_count=saved_count,
                                inline_processed=inline_processed,
                            )
                        except Exception as archive_exc:
                            if verbose:
                                print(
                                    f"    WARNING: could not archive email uid={uid}: {archive_exc}"
                                )
                        if verbose and archived_email is not None:
                            try:
                                archived_label = archived_email.relative_to(receipt_dir)
                            except ValueError:
                                archived_label = archived_email
                            print(f"    archived email → {archived_label}")
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
                        filed = _file_message_to_processed_label(
                            sr,
                            conn,
                            uid,
                            source_mailbox=mailbox,
                            processed_label=processed_label,
                        )
                        to_expunge = to_expunge or filed

                except Exception as exc:
                    result.errors += 1
                    if verbose:
                        print(f"    ERROR uid={uid}: {exc}")

            if to_expunge:
                conn.expunge()

    except Exception as exc:
        raise RuntimeError(f"Email fetch failed: {exc}") from exc

    try:
        _process_reply_messages(
            config,
            settings,
            db_conn,
            result,
            since_days=since_days,
            limit=limit,
            verbose=verbose,
        )
    except Exception as exc:
        result.errors += 1
        if verbose:
            print(f"  WARNING reply mailbox processing degraded: {exc}")

    return result


# ── helpers ────────────────────────────────────────────────────────────────────


def _search_receipt_uids(
    conn,
    config,
    subjects: list[str],
    *,
    sender_hints: list[str],
    since_days: int,
    limit: int,
) -> list[int]:
    """Search the selected mailbox for messages matching receipt subjects or senders."""
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
    for sender_hint in sender_hints:
        terms = build_search_terms(from_contains=sender_hint, since_days=since_days)
        charset = config.imap.search_charset
        status, data = conn.uid("search", charset, *terms)
        if status != "OK":
            continue
        for uid in parse_int_uid_list(data):
            seen.add(uid)
    # Return most-recent first, capped at limit
    return sorted(seen, reverse=True)[:limit]


def _configured_receipt_sender_hints(email_cfg: dict) -> list[str]:
    """Return sender fragments that should be searched as receipt candidates."""
    configured = [str(value).strip() for value in email_cfg.get("receipt_senders", [])]
    return list(dict.fromkeys([*configured, *_DEFAULT_RECEIPT_SENDER_HINTS]))


def _configured_reply_mailboxes(email_cfg: dict) -> list[str]:
    """Return mailboxes that may contain replies to intake notification emails."""
    configured = [str(value).strip() for value in email_cfg.get("reply_mailboxes", [])]
    return list(dict.fromkeys([*configured, *_DEFAULT_REPLY_MAILBOXES]))


def _email_was_handled(saved_count: int, inline_processed: bool) -> bool:
    """Return True when the message yielded at least one intake artifact."""
    return saved_count > 0 or inline_processed


def _same_mailbox_label(left: str, right: str) -> bool:
    """Return True when two Gmail IMAP mailbox names refer to the same label."""
    return left.strip().strip('"').casefold() == right.strip().strip('"').casefold()


def _notification_inbox_grace_seconds(email_cfg: dict) -> int:
    """Return how long generated notifications should remain visible in INBOX."""
    if email_cfg.get("notification_inbox_grace_seconds") is not None:
        raw_value = email_cfg.get("notification_inbox_grace_seconds")
        multiplier = 1
    else:
        raw_value = email_cfg.get("notification_inbox_grace_minutes", 60)
        multiplier = 60
    try:
        return max(0, int(float(raw_value) * multiplier))
    except (TypeError, ValueError):
        return 60 * 60


def _email_message_tracking_state(db_conn, *, uid: int, mailbox: str, message_id: str) -> dict:
    """Return email_receipts tracking state for a message."""
    row = db_conn.execute(
        "SELECT status, processed_at FROM email_receipts WHERE uid = ? AND mailbox = ?",
        (uid, mailbox),
    ).fetchone()
    if row is None and message_id:
        row = db_conn.execute(
            """
            SELECT status, processed_at
            FROM email_receipts
            WHERE message_id = ?
            ORDER BY processed_at ASC
            LIMIT 1
            """,
            (message_id,),
        ).fetchone()
    if row is None:
        return {"tracked": False, "status": "", "processed_at": None}
    return {
        "tracked": True,
        "status": str(row["status"] or ""),
        "processed_at": _parse_tracking_datetime(str(row["processed_at"] or "")),
    }


def _internal_notification_ready_to_file(tracking: dict, *, grace_seconds: int) -> bool:
    """Return True when a tracked internal notification has aged past the grace period."""
    if tracking.get("status") != "internal":
        return False
    processed_at = tracking.get("processed_at")
    if not isinstance(processed_at, datetime):
        return False
    age = datetime.now(UTC) - processed_at.astimezone(UTC)
    return age.total_seconds() >= grace_seconds


def _parse_tracking_datetime(value: str) -> datetime | None:
    """Parse an email_receipts timestamp."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _file_message_to_processed_label(
    sr,
    conn,
    uid: int,
    *,
    source_mailbox: str,
    processed_label: str,
) -> bool:
    """Mark *uid* read and move it to *processed_label* when needed.

    Returns True when the source mailbox needs expunge because a move occurred.
    """
    sr.mark_message_read(conn, uid)
    if not processed_label or _same_mailbox_label(source_mailbox, processed_label):
        return False
    sr.move_message(conn, uid, processed_label)
    return True


def _move_message_without_marking_read(
    sr,
    conn,
    uid: int,
    *,
    source_mailbox: str,
    processed_label: str,
) -> bool:
    """Move *uid* to *processed_label* without marking it read."""
    if not processed_label or _same_mailbox_label(source_mailbox, processed_label):
        return False
    sr.move_message(conn, uid, processed_label)
    return True


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


def _safe_save_receipt_pdf(
    dest_dir: Path,
    filename: str,
    data: bytes,
    *,
    received_at: str | None = None,
) -> Path:
    """Write a receipt PDF using the canonical ``YYYYMMDD_HHMM.pdf`` convention."""
    try:
        parsed = parsedate_to_datetime(received_at) if received_at else datetime.now()
        base_dt = parsed.replace(tzinfo=None) if isinstance(parsed, datetime) else datetime.now()
    except (TypeError, ValueError, IndexError, OverflowError):
        base_dt = datetime.now()
    dest = allocate_canonical_path(
        dest_dir,
        base_dt=base_dt.replace(second=0, microsecond=0, tzinfo=None),
        suffix=Path(filename).suffix or ".pdf",
    )
    dest.write_bytes(data)
    return dest


def _safe_archive_token(value: object) -> str:
    """Return a compact filesystem-safe token for email archive filenames."""
    token = re.sub(r"[^\w.-]+", "_", str(value or "").strip())
    return token.strip("._") or "mailbox"


def _processed_email_archive_dir(settings: dict) -> Path | None:
    """Return the local directory used for raw processed email downloads."""
    email_cfg = settings.get("email", {})
    if isinstance(email_cfg, dict) and email_cfg.get("archive_processed_messages", True) is False:
        return None

    receipt_root_value = str(settings.get("receipt_dir") or "").strip()
    if not receipt_root_value:
        return None
    receipt_root = Path(receipt_root_value).expanduser()

    configured = "email/messages"
    if isinstance(email_cfg, dict) and email_cfg.get("processed_email_dir"):
        configured = str(email_cfg["processed_email_dir"]).strip() or configured

    configured_path = Path(configured).expanduser()
    if configured_path.is_absolute():
        return configured_path
    return receipt_root / configured_path


def _email_archive_datetime(received_at: str | None) -> datetime:
    """Return the timestamp used for processed email archive filenames."""
    try:
        parsed = parsedate_to_datetime(received_at) if received_at else datetime.now()
        if isinstance(parsed, datetime):
            return parsed.replace(tzinfo=None, second=0, microsecond=0)
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    return datetime.now().replace(second=0, microsecond=0)


def _unique_email_archive_path(candidate: Path, payload: bytes) -> Path:
    """Return a non-clobbering path for *payload*, reusing identical archives."""
    if not candidate.exists():
        return candidate
    try:
        if candidate.read_bytes() == payload:
            return candidate
    except OSError:
        pass

    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 1000):
        alternate = candidate.with_name(f"{stem}_{index}{suffix}")
        if not alternate.exists():
            return alternate
        try:
            if alternate.read_bytes() == payload:
                return alternate
        except OSError:
            continue
    raise RuntimeError(f"could not allocate processed email archive path for {candidate}")


def _write_processed_email_artifact(
    settings: dict,
    *,
    uid: int,
    mailbox: str,
    message_id: str,
    subject: str,
    sender: str,
    received_at: str,
    payload: bytes,
    attachment_count: int,
    inline_processed: bool,
) -> Path | None:
    """Download the raw RFC822 email for a handled receipt message."""
    archive_dir = _processed_email_archive_dir(settings)
    if archive_dir is None:
        return None

    archive_dir.mkdir(parents=True, exist_ok=True)
    received_dt = _email_archive_datetime(received_at)
    mailbox_token = _safe_archive_token(mailbox)
    candidate = archive_dir / f"{received_dt:%Y%m%d_%H%M}_{mailbox_token}_{uid}.eml"
    dest = _unique_email_archive_path(candidate, payload)
    dest.write_bytes(payload)

    from intake.sidecar import write_sidecar  # noqa: PLC0415

    receipt_root_value = str(settings.get("receipt_dir") or "").strip()
    receipt_root = Path(receipt_root_value).expanduser() if receipt_root_value else None
    try:
        receipt_dir = str(dest.relative_to(receipt_root)) if receipt_root is not None else str(dest)
    except ValueError:
        receipt_dir = str(dest)

    write_sidecar(
        dest,
        {
            "filename": dest.name,
            "scan_date": received_dt.date().isoformat(),
            "category": "email/messages",
            "receipt_dir": receipt_dir,
            "processed_by": "email-fetch",
            "document_type": "email-message",
            "tier": "email",
            "uid": uid,
            "mailbox": mailbox,
            "message_id": message_id,
            "subject": subject,
            "sender": sender,
            "received_at": received_at,
            "attachment_count": attachment_count,
            "inline_processed": inline_processed,
        },
    )
    return dest


_RECEIPT_PATTERNS = re.compile(
    r"\b(total|amount\s+due|order\s+total|charged|subtotal|invoice|receipt\s+#)\b",
    re.IGNORECASE,
)
_PAYMENT_EMAIL_PATTERNS = re.compile(
    r"\b("
    r"thanks\s+for\s+(?:scheduling\s+)?your\s+payment|"
    r"we'?ve\s+received\s+your\s+payment|"
    r"your\s+scheduled\s+payment|"
    r"payment\s+amount|"
    r"payment\s+post\s+date|"
    r"scheduled\s+date|"
    r"posted\s+date|"
    r"payment\s+of\s+\$|"
    r"you\s+paid\s+\$|"
    r"you\s+paid\s+\w+|"  # Venmo: "You paid Name $N" (name precedes $)
    r"paid\s+you\s+\$|"
    r"\w+\s+paid\s+you\s+\$|"  # Venmo: "Name paid you $N"
    r"charged\s+you\s+\$|"
    r"\w+\s+charged\s+you\s+\$|"  # Venmo: "Name charged you $N"
    r"money\s+added\s+to\s+your\s+account|"
    r"you\s+added\s+\d+(?:\.\d{2})?\s+[A-Z]{3}\s+to\s+your\s+account"
    r")\b",
    re.IGNORECASE,
)
_CARD_SCHEDULED_NOTICE_RE = re.compile(
    r"\b("
    r"thanks\s+for\s+scheduling\s+your\s+payment|"
    r"your\s+scheduled\s+payment(?!\s+was\b)|"
    r"(?:payment|card\s+payment)\s+is\s+scheduled|"
    r"is\s+scheduled\s+for"
    r")\b",
    re.IGNORECASE,
)
_PAYPAL_UPCOMING_PURCHASE_RE = re.compile(
    r"\byou\s+have\s+an\s+upcoming\b(?=.*\bscheduled\b)(?=.*\bpurchase\b)",
    re.IGNORECASE | re.DOTALL,
)
_ANY_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PAYMENT_AMOUNT_RE = re.compile(
    r"\bpayment\s+amount\s*:\s*\$?\s*(\d+(?:\.\d{2})?)\b", re.IGNORECASE
)
_PAYMENT_OF_RE = re.compile(r"\bpayment\s+of\s+\$?\s*(\d+(?:\.\d{2})?)\b", re.IGNORECASE)
_PAYMENT_INLINE_AMOUNT_RE = re.compile(r"\bamount\s*:\s*\$?\s*(\d+(?:\.\d{2})?)\b", re.IGNORECASE)
_ACCOUNT_ENDING_RE = re.compile(
    r"\baccount(?:\s+number)?\s+ending\s+(?:in|with)\s+(\d{4})\b",
    re.IGNORECASE,
)
_LAST4_RE = re.compile(r"\blast\s*4\s*#?\s*:\s*(\d{4})\b", re.IGNORECASE)
_CAPITAL_ONE_CARD_RE = re.compile(r"\bcredit\s+card\.\.\.(\d{4})\b", re.IGNORECASE)
_PAYMENT_GREETING_RE = re.compile(
    r"(?:^|\n)\s*([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)\s*,", re.MULTILINE
)
_PAYMENT_DATE_TEXT_PATTERN = (
    r"(?:"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r")\.?\s+\d{1,2},\s+\d{4}"
)
_PAYMENT_DATE_RE = re.compile(
    rf"\b{_PAYMENT_DATE_TEXT_PATTERN}\b",
    re.IGNORECASE,
)
_PAYMENT_MDY_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_POSTED_TO_ACCOUNT_DATE_RE = re.compile(
    rf"\bposted\s+to\s+your\s+account\s+on\s+({_PAYMENT_DATE_TEXT_PATTERN})\b",
    re.IGNORECASE,
)
_SCHEDULED_FOR_DATE_RE = re.compile(
    rf"\bscheduled\s+for\s+({_PAYMENT_DATE_TEXT_PATTERN})\b",
    re.IGNORECASE,
)
_DUE_ON_DATE_RE = re.compile(r"\bdue\s+on\s+(\d{2}/\d{2}/\d{4})\b", re.IGNORECASE)
_CAPITAL_ONE_SENDER_HINT = "capitalone@notification.capitalone.com"
_DISCOVER_SENDER_HINT = "discover@services.discover.com"
_AMEX_SENDER_HINT = "americanexpress.com"
_AMEX_ACCOUNT_ENDING_RE = re.compile(r"\baccount\s+ending\s*:\s*(\d{4,5})\b", re.IGNORECASE)
_AMEX_CARDHOLDER_RE = re.compile(
    r"([A-Z][A-Z ]{3,}[A-Z])\s*\n\s*Account\s+Ending\b",
    re.MULTILINE,
)
_PROGRESSIVE_SENDER_HINT = "customerservice@e.progressive.com"
_PAYPAL_SENDER_HINT = "service@paypal.com"
_PAYPAL_SUBJECT_AMOUNT_RE = re.compile(r":\s*\$?\s*(\d+(?:\.\d{2})?)\s+[A-Z]{3}\b")
_VENMO_SENDER_HINT = "venmo@venmo.com"
_VENMO_YOU_PAID_RE = re.compile(
    r"^you\s+paid\s+(.+?)\s+\$\s*(\d+(?:\.\d{2})?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_VENMO_PAID_YOU_RE = re.compile(
    r"^(.+?)\s+paid\s+you\s+\$\s*(\d+(?:\.\d{2})?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_VENMO_CHARGED_YOU_RE = re.compile(
    r"^(.+?)\s+charged\s+you\s+\$\s*(\d+(?:\.\d{2})?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_IRS_SENDER_HINT = "account.irs.gov"
_NELNET_SENDER_HINT = "nelnet.studentaid.gov"
_NELNET_MDY_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
_WISE_SENDER_HINT = "noreply@wise.com"
_WISE_MONEY_ADDED_RE = re.compile(
    r"\byou\s+added\s+(\d+(?:\.\d{2})?)\s+([A-Z]{3})\s+to\s+your\s+account\b",
    re.IGNORECASE,
)
_XANTERRA_SENDER_HINT = "xanterra.com"
_XANTERRA_WELCOME_RE = re.compile(
    r"(?im)^\s*Welcome,\s*([A-Z][A-Za-z]+(?:[ \t]+[A-Z][A-Za-z]+){1,3})\s*$"
)
_BINANCE_SENDER_HINT = "post.binance.us"
_PAYPAL_YOU_PAID_RE = re.compile(
    r"\byou paid\s+\$?\s*(\d+(?:\.\d{2})?)\s+[A-Z]{3}\s+to\s+(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_QUOTED_REPLY_SPLIT_RE = re.compile(
    r"(?im)^\s*(?:on .+wrote:|from:\s.+|sent:\s.+|subject:\s.+|-----original message-----)\s*$"
)
_REPLY_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:re|fw|fwd)\s*:\s*", re.IGNORECASE)
_REPLY_MESSAGE_ID_RE = re.compile(r"<[^>]+>")
_REPLY_FILENAME_RE = re.compile(r"(?im)^\s*filename\s*:\s*(\S+)\s*$")
_REPLY_FILE_RE = re.compile(r"(?im)^\s*file\s*:\s*(.+?)\s*$")
_REPLY_UPDATE_RE = re.compile(
    r"^\s*(merchant|total|purchaser|card(?:\s+last4)?|card_last4|currency)\s*(?::|=|->)\s*(.+?)\s*$",
    re.IGNORECASE,
)
_REPLY_CLEAR_RE = re.compile(
    r"^\s*clear\s+(purchaser|card(?:\s+last4)?|card_last4)\s*$",
    re.IGNORECASE,
)
_REPLY_LINK_ACTION_RE = re.compile(r"\b(attach|upload|send|share|link)\b", re.IGNORECASE)
_REPLY_LINK_TARGET_RE = re.compile(r"\b(ocr|image|photo|file|receipt)\b", re.IGNORECASE)


def _intake_processed_subject_fragment(settings: dict) -> str:
    """Return the stable subject fragment used for processed-receipt emails."""
    del settings
    return "Receipt processed"


def _is_intake_reply_message(msg, settings: dict) -> bool:
    """Return True when *msg* is a reply to an intake processed-receipt email."""
    if _is_intake_generated_message(msg):
        return False

    subject = str(msg.get("Subject", "") or "")
    subject_lower = subject.strip().lower()
    fragment = _intake_processed_subject_fragment(settings).lower()
    if fragment not in subject_lower:
        return False
    if _REPLY_SUBJECT_PREFIX_RE.match(subject):
        return True
    return bool(msg.get("In-Reply-To") or msg.get("References"))


def _is_intake_generated_message(msg) -> bool:
    """Return True when a message was generated by intake itself."""
    service = str(msg.get(_PORTFOLIO_SERVICE_HEADER, "") or "").strip().lower()
    intent = str(msg.get(_CREW_CHIEF_INTENT_HEADER, "") or "").strip().lower()
    return service == _INTAKE_SERVICE_VALUE and intent in _INTAKE_GENERATED_INTENTS


def _contains_intake_notification_quote(text: str) -> bool:
    """Return True when body text contains a quoted intake notification."""
    normalized = str(text or "")
    if not normalized.strip():
        return False
    if _intake_processed_subject_fragment({}).lower() not in normalized.lower():
        return False
    return bool(
        _extract_receipt_filename_from_text(normalized)
        or re.search(r"(?im)^\s*(merchant|total|category|model)\s*:", normalized)
    )


def _message_text(msg, sr) -> str:
    """Return the best plain-text body for *msg*."""
    plain_text, html_body = sr.extract_message_bodies(msg)
    return _combine_message_body_text(plain_text, html_body)


def _combine_message_body_text(plain_text: str | None, html_body: str | None) -> str:
    """Return searchable text from plain and HTML message bodies."""
    html_text = html_to_text(html_body) if html_body else ""
    parts = [part.strip() for part in (plain_text, html_text) if part and part.strip()]
    return "\n\n".join(parts).strip()


def _is_known_non_receipt_notice(subject: str, sender: str, plain_text: str) -> bool:
    """Return True for payment notices that should be filed but not ingested."""
    combined = f"{subject}\n{plain_text or ''}"
    sender_lower = str(sender or "").lower()
    if "you paid" not in combined.lower() and _PAYPAL_UPCOMING_PURCHASE_RE.search(combined):
        return True
    if (
        _CAPITAL_ONE_SENDER_HINT in sender_lower
        or _DISCOVER_SENDER_HINT in sender_lower
        or _CARD_SCHEDULED_NOTICE_RE.search(combined)
    ):
        return bool(_CARD_SCHEDULED_NOTICE_RE.search(combined))
    if _IRS_SENDER_HINT in sender_lower and not re.search(r"\$\s*\d+", combined):
        return True
    if _VENMO_SENDER_HINT in sender_lower and re.search(
        r"transaction\s+history", combined, re.IGNORECASE
    ):
        return True
    return bool(
        _BINANCE_SENDER_HINT in sender_lower and re.search(r"\bstaking\b", combined, re.IGNORECASE)
    )


def _trim_reply_note_text(text: str) -> str:
    """Trim the user's new reply text from quoted original content."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return ""

    lines: list[str] = []
    for raw_line in normalized.split("\n"):
        stripped = raw_line.strip()
        if _QUOTED_REPLY_SPLIT_RE.match(stripped):
            break
        if stripped.startswith(">"):
            break
        lines.append(stripped)

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def _extract_receipt_filename_from_text(text: str) -> str | None:
    """Extract the tracked receipt filename from notification body text."""
    match = _REPLY_FILENAME_RE.search(text)
    if match:
        return Path(match.group(1).strip()).name or None

    match = _REPLY_FILE_RE.search(text)
    if not match:
        return None
    candidate = Path(match.group(1).strip()).name
    if not candidate:
        return None
    if "." not in candidate:
        return None
    return candidate


def _extract_receipt_filename_from_message(msg, sr) -> str | None:
    """Resolve the receipt filename from intake notification headers or body."""
    header_value = sr.decode_mime_header(msg.get(_INTAKE_RECEIPT_FILENAME_HEADER))
    if header_value:
        return Path(header_value.strip()).name or None

    receipt_dir = sr.decode_mime_header(msg.get(_INTAKE_RECEIPT_DIR_HEADER))
    if receipt_dir:
        candidate = Path(receipt_dir.strip()).name
        if candidate and "." in candidate:
            return candidate

    return _extract_receipt_filename_from_text(_message_text(msg, sr))


def _extract_reply_message_ids(msg) -> list[str]:
    """Return normalized message-id values referenced by a reply message."""
    seen: set[str] = set()
    values: list[str] = []
    for header_name in ("In-Reply-To", "References"):
        raw_value = str(msg.get(header_name, "") or "")
        for match in _REPLY_MESSAGE_ID_RE.findall(raw_value):
            if match in seen:
                continue
            seen.add(match)
            values.append(match)
    return values


def _reply_references_header(msg) -> str | None:
    """Return a References header that keeps follow-up replies threaded."""
    message_id = str(msg.get("Message-ID", "") or "").strip()
    references = " ".join(str(msg.get("References", "") or "").split()).strip()
    if message_id and message_id not in references:
        references = f"{references} {message_id}".strip() if references else message_id
    return references or None


def _reply_thread_token(msg, message_id: str) -> str:
    """Return a stable token for the replied-to thread."""
    reply_ids = _extract_reply_message_ids(msg)
    if reply_ids:
        return reply_ids[0]
    return message_id


def _mailbox_select_candidates(mailbox: str) -> list[str]:
    """Return raw and quoted mailbox names for IMAP select/examine calls."""
    cleaned = str(mailbox or "").strip()
    if not cleaned:
        return []
    candidates = [cleaned]
    if " " in cleaned and not cleaned.startswith('"'):
        candidates.append(f'"{cleaned}"')
    return candidates


def _select_mailbox(conn, mailbox: str, *, readonly: bool) -> tuple[str, object]:
    """Select *mailbox*, retrying with IMAP-safe quoting when needed."""
    last_status = "NO"
    last_data = []
    last_exc: Exception | None = None
    for candidate in _mailbox_select_candidates(mailbox):
        try:
            status, data = conn.select(candidate, readonly=readonly)
        except Exception as exc:
            last_exc = exc
            continue
        if status == "OK":
            return status, data
        last_status, last_data = status, data
    if last_exc is not None:
        raise last_exc
    return last_status, last_data


def _fetch_message_by_uid(conn, mailbox: str, uid: int, *, readonly: bool) -> object:
    """Select *mailbox* and fetch the RFC822 payload for *uid*."""
    status, _ = _select_mailbox(conn, mailbox, readonly=readonly)
    if status != "OK":
        raise RuntimeError(f"Cannot select mailbox {mailbox!r}")

    fetch_status, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
    if fetch_status != "OK":
        raise RuntimeError(f"Cannot fetch uid {uid} from {mailbox!r}")
    return msg_data


def _find_message_in_mailboxes(
    conn,
    sr,
    mailboxes: list[str],
    message_id: str,
    *,
    recent_limit: int,
    search_charset,
) -> dict[str, object] | None:
    """Locate *message_id* across candidate mailboxes with quoted-select fallback."""
    canonical_message_id = sr.normalize_message_id_header(message_id)
    for mailbox in mailboxes:
        try:
            status, _ = _select_mailbox(conn, mailbox, readonly=True)
        except Exception:
            continue
        if status != "OK":
            continue
        status, data = conn.uid("search", search_charset, "ALL")
        if status != "OK":
            continue
        uids = sr.parse_int_uid_list(data)
        if not uids:
            continue
        for uid in reversed(uids[-recent_limit:]):
            msg_data = _fetch_message_by_uid(conn, mailbox, uid, readonly=True)
            payload = sr.extract_fetch_payload(msg_data)
            if not payload:
                continue
            normalized = sr.normalize_message(mailbox, uid, payload)
            if sr.normalize_message_id_header(normalized.get("message_id")) != canonical_message_id:
                continue
            return {
                "mailbox": mailbox,
                "uid": uid,
                "message_id": canonical_message_id,
            }
    return None


def _resolve_reply_receipt_filename(
    conn, config, sr, settings: dict, msg, full_text: str
) -> str | None:
    """Resolve the tracked receipt filename targeted by a reply email."""
    filename = _extract_receipt_filename_from_text(full_text)
    if filename:
        return filename

    reply_mailboxes = _configured_reply_mailboxes(settings.get("email", {}))
    recent_limit = max(10, min(100, int(settings.get("email", {}).get("limit", 500))))
    for message_id in _extract_reply_message_ids(msg):
        original = _find_message_in_mailboxes(
            conn,
            sr,
            reply_mailboxes,
            message_id,
            recent_limit=recent_limit,
            search_charset=config.imap.search_charset,
        )
        if not original:
            continue
        original_data = _fetch_message_by_uid(
            conn,
            original["mailbox"],
            int(original["uid"]),
            readonly=True,
        )
        payload = sr.extract_fetch_payload(original_data)
        if not payload:
            continue
        original_msg = message_from_bytes(payload, policy=default_policy)
        filename = _extract_receipt_filename_from_message(original_msg, sr)
        if filename:
            return filename
    return None


def _normalize_reply_field(field: str) -> str:
    """Normalize a free-form field label from a reply note."""
    lowered = field.strip().lower().replace("_", " ")
    if lowered in {"card last4", "card"}:
        return "card_last4"
    return lowered.replace(" ", "_")


def _parse_reply_update_value(field: str, value: str):
    """Parse a structured field value from an intake reply line."""
    cleaned = value.strip()
    if not cleaned:
        return None

    none_markers = {"none", "null", "clear", "blank", "empty"}
    if field in {"purchaser", "card_last4"} and cleaned.lower() in none_markers:
        return None
    if field == "merchant":
        return cleaned.lower()
    if field == "total":
        match = re.search(r"-?\d+(?:\.\d{2})?", cleaned.replace(",", ""))
        return float(match.group(0)) if match else ...
    if field == "purchaser":
        return cleaned
    if field == "card_last4":
        digits = re.sub(r"\D", "", cleaned)
        return digits if len(digits) == 4 else ...
    if field == "currency":
        letters = re.sub(r"[^A-Za-z]", "", cleaned).upper()
        return letters if len(letters) == 3 else ...
    return ...


def _extract_reply_updates(note_text: str) -> tuple[dict[str, object | None], str]:
    """Parse safe structured updates from a reply note and return remaining free text."""
    updates: dict[str, object | None] = {}
    remaining_lines: list[str] = []
    for raw_line in note_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if remaining_lines and remaining_lines[-1] != "":
                remaining_lines.append("")
            continue

        clear_match = _REPLY_CLEAR_RE.match(raw_line)
        if clear_match:
            updates[_normalize_reply_field(clear_match.group(1))] = None
            continue

        update_match = _REPLY_UPDATE_RE.match(raw_line)
        if update_match:
            field = _normalize_reply_field(update_match.group(1))
            parsed = _parse_reply_update_value(field, update_match.group(2))
            if parsed is not ...:
                updates[field] = parsed
                continue

        remaining_lines.append(raw_line.rstrip())

    while remaining_lines and remaining_lines[0] == "":
        remaining_lines.pop(0)
    while remaining_lines and remaining_lines[-1] == "":
        remaining_lines.pop()
    return updates, "\n".join(remaining_lines).strip()


def _reply_requests_file_link(note_text: str) -> bool:
    """Return True when a reply note is asking for OCR/image/file access."""
    normalized = " ".join(str(note_text or "").split())
    if not normalized:
        return False
    if _looks_like_generated_file_link_reply(note_text):
        return False
    return bool(
        _REPLY_LINK_ACTION_RE.search(normalized) and _REPLY_LINK_TARGET_RE.search(normalized)
    )


def _looks_like_generated_file_link_reply(note_text: str) -> bool:
    """Return True for the reply body intake sends with a Snowbridge file link."""
    lines = [line.strip().lower() for line in str(note_text or "").splitlines() if line.strip()]
    if not lines or lines[0] != "snowbridge receipt link requested":
        return False
    return any(line.startswith("receipt:") for line in lines) and any(
        line.startswith("link:") for line in lines
    )


def _reply_link_already_recorded(
    db_conn,
    filename: str,
    reply_token: str,
    reply_message_id: str,
) -> bool:
    """Return True when a Snowbridge-link response was already sent for this reply."""
    tokens = [token for token in (reply_token, reply_message_id) if token]
    if not tokens:
        return False
    placeholders = ", ".join("?" for _ in tokens)
    row = db_conn.execute(
        f"""
        SELECT 1
        FROM receipt_updates
        WHERE filename = ? AND source = ? AND note IN ({placeholders})
        LIMIT 1
        """,
        (filename, _INTAKE_REPLY_LINK_SOURCE, *tokens),
    ).fetchone()
    return row is not None


def _maybe_send_reply_link(
    db_conn,
    settings: dict,
    *,
    filename: str,
    note_text: str,
    recipient: str,
    subject: str,
    reply_token: str,
    reply_message_id: str,
    references: str | None,
) -> str | None:
    """Send a Snowbridge-link follow-up when the reply note asks for file access."""
    if not _reply_requests_file_link(note_text):
        return None
    if _reply_link_already_recorded(db_conn, filename, reply_token, reply_message_id):
        return "link-already-sent"

    receipt_row = get_receipt(db_conn, filename)
    if receipt_row is None:
        raise KeyError(filename)

    from intake.notify import send_receipt_file_link_gmail_reply  # noqa: PLC0415

    link = send_receipt_file_link_gmail_reply(
        settings,
        recipient=recipient,
        subject=subject,
        receipt_row=receipt_row,
        in_reply_to=reply_message_id or None,
        references=references,
    )
    record_receipt_update(
        db_conn,
        filename,
        source=_INTAKE_REPLY_LINK_SOURCE,
        note=reply_token or None,
        changes={
            "snowbridge_link": {"old": None, "new": link},
            "reply_to": {"old": None, "new": recipient},
            "reply_subject": {"old": None, "new": subject},
            "reply_message_id": {"old": None, "new": reply_message_id},
        },
    )
    return "link-sent"


def _sync_updated_sidecar(settings: dict, row: dict) -> None:
    """Best-effort sidecar sync for reply-applied updates."""
    receipt_root_value = str(settings.get("receipt_dir") or "").strip()
    if not receipt_root_value:
        return
    receipt_root = Path(receipt_root_value).expanduser()
    if not receipt_root.exists():
        return
    try:
        from intake.cli import _sync_receipt_sidecar  # noqa: PLC0415
    except Exception:
        return
    try:
        _sync_receipt_sidecar(receipt_root, row)
    except Exception:
        return


def _record_reply_note(
    db_conn,
    *,
    filename: str,
    note_text: str,
    requested_updates: dict[str, object | None] | None = None,
) -> None:
    """Record a reply note as an auditable receipt update event."""
    changes: dict[str, dict[str, object | None]] = {}
    if note_text:
        changes["email_reply"] = {"old": None, "new": note_text}
    if requested_updates:
        changes["requested_updates"] = {"old": None, "new": requested_updates}
    if not changes:
        return
    record_receipt_update(
        db_conn,
        filename,
        source=_INTAKE_REPLY_NOTE_SOURCE,
        changes=changes,
        note=note_text or None,
    )


def _apply_reply_to_receipt(
    db_conn,
    settings: dict,
    *,
    filename: str,
    note_text: str,
) -> str:
    """Apply safe reply updates to *filename* and audit the remainder."""
    if get_receipt(db_conn, filename) is None:
        raise KeyError(filename)

    structured_updates, remaining_note = _extract_reply_updates(note_text)
    applied_changes: dict[str, dict[str, object | None]] = {}
    if structured_updates:
        updated_row, applied_changes = update_receipt_fields(
            db_conn,
            filename,
            structured_updates,
            source=_INTAKE_REPLY_SOURCE,
            note=None,
        )
        if applied_changes:
            _sync_updated_sidecar(settings, updated_row)

    if remaining_note or (structured_updates and not applied_changes):
        _record_reply_note(
            db_conn,
            filename=filename,
            note_text=remaining_note,
            requested_updates=structured_updates or None,
        )

    if applied_changes:
        return "reply-applied"
    if remaining_note or structured_updates:
        return "reply-recorded"
    return "reply-empty"


def _search_reply_uids(
    conn,
    config,
    *,
    subject_fragment: str,
    since_days: int,
    limit: int,
) -> list[int]:
    """Search the selected mailbox for replies to intake processed-receipt emails."""
    from common import build_search_terms, parse_int_uid_list  # noqa: PLC0415

    terms = build_search_terms(subject_contains=subject_fragment, since_days=since_days)
    charset = config.imap.search_charset
    status, data = conn.uid("search", charset, *terms)
    if status != "OK":
        return []
    return sorted(parse_int_uid_list(data), reverse=True)[:limit]


def _process_reply_messages(
    config,
    settings: dict,
    db_conn,
    result: FetchResult,
    *,
    since_days: int,
    limit: int,
    verbose: bool,
) -> None:
    """Process replies to intake processed-receipt notifications."""
    email_cfg = settings.get("email", {})
    subject_fragment = _intake_processed_subject_fragment(settings)
    reply_mailboxes = _configured_reply_mailboxes(email_cfg)
    sr = _load_shock_relay(str(email_cfg.get("shock_relay_config", "")))

    for mailbox in reply_mailboxes:
        try:
            with sr.open_imap_connection(config) as search_conn:
                status, _ = _select_mailbox(search_conn, mailbox, readonly=True)
                if status != "OK":
                    continue
                uids = _search_reply_uids(
                    search_conn,
                    config,
                    subject_fragment=subject_fragment,
                    since_days=since_days,
                    limit=limit,
                )
        except Exception as exc:
            result.errors += 1
            if verbose:
                print(f"  WARNING reply mailbox {mailbox}: {exc}")
            continue
        if not uids:
            continue
        if verbose:
            print(f"  Found {len(uids)} reply candidate(s) in {mailbox}")

        for uid in uids:
            try:
                with sr.open_imap_connection(config) as conn:
                    msg_data = _fetch_message_by_uid(conn, mailbox, uid, readonly=True)
                    payload = sr.extract_fetch_payload(msg_data)
                    if not payload:
                        result.skipped += 1
                        continue

                    msg = message_from_bytes(payload, policy=default_policy)
                    if not _is_intake_reply_message(msg, settings):
                        continue

                    subject = sr.decode_mime_header(msg.get("Subject"))
                    sender = sr.decode_mime_header(msg.get("From"))
                    message_id = sr.decode_mime_header(msg.get("Message-ID"))
                    date_str = str(msg.get("Date", "") or "").strip()

                    full_text = _message_text(msg, sr)
                    note_text = _trim_reply_note_text(full_text)
                    receipt_filename = _resolve_reply_receipt_filename(
                        conn,
                        config,
                        sr,
                        settings,
                        msg,
                        full_text,
                    )

                    if verbose:
                        print(f"  Processing reply: {subject!r}  from={sender}")

                    if not receipt_filename:
                        if verbose:
                            print("    reply skipped: could not resolve receipt filename")
                        record_email_receipt(
                            db_conn,
                            uid=uid,
                            mailbox=mailbox,
                            message_id=message_id,
                            subject=subject,
                            sender=sender,
                            received_at=date_str,
                            attachment_count=0,
                            status="reply-unmatched",
                        )
                        result.reply_unmatched += 1
                        continue

                    references = _reply_references_header(msg)
                    reply_token = _reply_thread_token(msg, message_id)

                    already_processed = is_email_message_id_processed(db_conn, message_id)
                    if already_processed:
                        link_action = None
                        try:
                            link_action = _maybe_send_reply_link(
                                db_conn,
                                settings,
                                filename=receipt_filename,
                                note_text=note_text,
                                recipient=sender,
                                subject=subject,
                                reply_token=reply_token,
                                reply_message_id=message_id,
                                references=references,
                            )
                        except Exception as exc:
                            result.errors += 1
                            if verbose:
                                print(f"    WARNING reply-link for {receipt_filename}: {exc}")
                        if verbose and link_action == "link-sent":
                            print(f"    reply → {link_action} for {receipt_filename}")
                        if verbose and link_action == "link-already-sent":
                            print(f"    skip (reply link already sent): {subject!r}")
                        if verbose:
                            print(f"    skip (reply message-id already processed): {subject!r}")
                        result.skipped += 1
                        continue

                    if is_email_processed(db_conn, uid, mailbox):
                        if verbose:
                            print(f"    skip (reply already processed): {subject!r}")
                        result.skipped += 1
                        continue

                    try:
                        status_name = _apply_reply_to_receipt(
                            db_conn,
                            settings,
                            filename=receipt_filename,
                            note_text=note_text,
                        )
                    except KeyError:
                        if verbose:
                            print(f"    reply skipped: receipt not found in DB: {receipt_filename}")
                        record_email_receipt(
                            db_conn,
                            uid=uid,
                            mailbox=mailbox,
                            message_id=message_id,
                            subject=subject,
                            sender=sender,
                            received_at=date_str,
                            attachment_count=0,
                            status="reply-unmatched",
                        )
                        result.reply_unmatched += 1
                        continue

                    link_action = None
                    try:
                        link_action = _maybe_send_reply_link(
                            db_conn,
                            settings,
                            filename=receipt_filename,
                            note_text=note_text,
                            recipient=sender,
                            subject=subject,
                            reply_token=reply_token,
                            reply_message_id=message_id,
                            references=references,
                        )
                    except Exception as exc:
                        result.errors += 1
                        if verbose:
                            print(f"    WARNING reply-link for {receipt_filename}: {exc}")
                    if link_action == "link-sent":
                        status_name = f"{status_name}-link-sent"

                    record_email_receipt(
                        db_conn,
                        uid=uid,
                        mailbox=mailbox,
                        message_id=message_id,
                        subject=subject,
                        sender=sender,
                        received_at=date_str,
                        attachment_count=0,
                        status=status_name,
                    )
                    if status_name.startswith("reply-applied"):
                        result.reply_applied += 1
                    if status_name.startswith("reply-applied") or status_name.startswith(
                        "reply-recorded"
                    ):
                        result.reply_processed += 1
                    elif status_name == "reply-unmatched":
                        result.reply_unmatched += 1
                    if verbose:
                        print(f"    reply → {status_name} for {receipt_filename}")
            except Exception as exc:
                result.errors += 1
                if verbose:
                    print(f"    ERROR reply uid={uid} mailbox={mailbox}: {exc}")

        if "all mail" in mailbox.lower():
            break


def _looks_like_receipt(text: str) -> bool:
    """Heuristic: does this email body text resemble a receipt?"""
    if _contains_intake_notification_quote(text):
        return False
    if _is_known_non_receipt_notice("", "", text):
        return False
    return bool(_RECEIPT_PATTERNS.search(text) or _PAYMENT_EMAIL_PATTERNS.search(text))


def _category_exists(rules: dict, category_path: str) -> bool:
    """Return True when *category_path* exists in the configured rule tree."""
    parts = category_path.split("/")
    if len(parts) == 1:
        return parts[0] in rules
    if len(parts) != 2:
        return False
    category, subcategory = parts
    return category in rules and isinstance(rules[category].get(subcategory), dict)


def _preferred_payment_category(rules: dict) -> str:
    """Return the best configured category for card-payment notification emails."""
    for candidate in ("finance/credit-card-payment", "general/credit-card-payment", "finance"):
        if _category_exists(rules, candidate):
            return candidate
    return "general"


def _parse_payment_date_text(text: str) -> date | None:
    """Parse a month-name payment date used in issuer notification emails."""
    cleaned = " ".join(text.replace("\n", " ").split())
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _parse_numeric_payment_date(text: str) -> date | None:
    """Parse an ``MM/DD/YYYY`` payment date used in billing reminders."""
    cleaned = " ".join(text.replace("\n", " ").split())
    try:
        return datetime.strptime(cleaned, "%m/%d/%Y").date()
    except ValueError:
        return None


def _parse_email_header_date(text: str) -> date | None:
    """Parse an RFC822 email date header into the sender-local calendar date."""
    try:
        parsed = parsedate_to_datetime(text) if text else None
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return parsed.date() if isinstance(parsed, datetime) else None


def _extract_labeled_block_value(plain_text: str, labels: tuple[str, ...]) -> str | None:
    """Return the value following one of *labels* on the same or next line."""
    lines = [line.strip() for line in plain_text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if not any(label in lowered for label in labels):
            continue
        if ":" in line:
            _, after = line.split(":", 1)
            after = after.strip()
            if after:
                return after
        for candidate in lines[idx + 1 : idx + 3]:
            if candidate:
                return candidate
    return None


def _extract_labeled_money_amount(plain_text: str, labels: tuple[str, ...]) -> float | None:
    """Return the first money amount printed after one of *labels*."""
    flattened = " ".join(plain_text.replace("\xa0", " ").split())
    for label in labels:
        label_pattern = r"\s+".join(re.escape(part) for part in label.split())
        match = re.search(
            rf"\b{label_pattern}\s*:?\s*\$?\s*([0-9,]+(?:\.\d{{2}})?)\b",
            flattened,
            re.IGNORECASE,
        )
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _extract_payment_amount(subject: str, plain_text: str) -> float | None:
    """Extract the payment amount from a card-payment notification email."""
    for pattern in (_PAYMENT_AMOUNT_RE, _PAYMENT_OF_RE, _PAYMENT_INLINE_AMOUNT_RE):
        match = pattern.search(plain_text)
        if match:
            return float(match.group(1))
    match = _PAYMENT_OF_RE.search(subject)
    if match:
        return float(match.group(1))
    return None


def _extract_payment_card_last4(plain_text: str) -> str | None:
    """Extract the card account last4 from a card-payment notification email."""
    for pattern in (_CAPITAL_ONE_CARD_RE, _ACCOUNT_ENDING_RE, _LAST4_RE):
        match = pattern.search(plain_text)
        if match:
            return match.group(1)
    return None


def _extract_payment_purchaser(plain_text: str) -> str | None:
    """Extract the purchaser from a card-payment notification email."""
    for match in _ANY_EMAIL_RE.finditer(plain_text):
        normalized = normalize_purchaser(match.group(0))
        if normalized is not None:
            return normalized
    match = _PAYMENT_GREETING_RE.search(plain_text)
    if match:
        normalized = normalize_purchaser(match.group(1))
        if normalized is not None:
            return normalized
    return None


def _extract_payment_effective_date(subject: str, plain_text: str) -> date | None:
    """Extract the posted/scheduled payment date from a notification email."""
    for labels in (
        ("posted date", "payment post date"),
        ("scheduled date",),
    ):
        value = _extract_labeled_block_value(plain_text, labels)
        if value is None:
            continue
        match = _PAYMENT_DATE_RE.search(value)
        if match:
            parsed = _parse_payment_date_text(match.group(0))
            if parsed is not None:
                return parsed

    for pattern in (_POSTED_TO_ACCOUNT_DATE_RE, _SCHEDULED_FOR_DATE_RE):
        match = pattern.search(f"{subject}\n{plain_text}")
        if match:
            parsed = _parse_payment_date_text(match.group(1))
            if parsed is not None:
                return parsed
    match = _DUE_ON_DATE_RE.search(f"{subject}\n{plain_text}")
    if match:
        return _parse_numeric_payment_date(match.group(1))
    return None


def _extract_progressive_payment_amount(plain_text: str) -> float | None:
    """Extract the due installment amount from a Progressive renewal reminder."""
    for labels in (
        ("minimum installment",),
        ("pay in full",),
        ("total premium",),
    ):
        value = _extract_labeled_block_value(plain_text, labels)
        if value is None:
            continue
        match = re.search(r"\$?\s*(\d+(?:\.\d{2})?)", value)
        if match:
            return float(match.group(1))
    return None


def _extract_credit_card_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for known credit-card payment-notification emails."""
    sender_lower = sender.lower()
    if _CAPITAL_ONE_SENDER_HINT in sender_lower:
        merchant = "capital one"
    elif _DISCOVER_SENDER_HINT in sender_lower:
        merchant = "discover"
    else:
        return None

    if not _PAYMENT_EMAIL_PATTERNS.search(f"{subject}\n{plain_text}"):
        return None
    if _is_known_non_receipt_notice(subject, sender, plain_text):
        return None

    return {
        "merchant": merchant,
        "total": _extract_payment_amount(subject, plain_text),
        "receipt_date": _extract_payment_effective_date(subject, plain_text),
        "purchaser": _extract_payment_purchaser(plain_text),
        "card_last4": _extract_payment_card_last4(plain_text),
        "category": "finance/credit-card-payment",
    }


def _extract_progressive_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for Progressive insurance payment reminders."""
    if _PROGRESSIVE_SENDER_HINT not in sender.lower():
        return None
    if "policy renewal reminder" not in subject.lower():
        return None
    if "renewal payment" not in plain_text.lower():
        return None
    return {
        "merchant": "progressive",
        "total": _extract_progressive_payment_amount(plain_text),
        "receipt_date": _extract_payment_effective_date(subject, plain_text),
        "purchaser": _extract_payment_purchaser(plain_text),
        "card_last4": None,
        "category": "auto/insurance",
    }


def _extract_paypal_receipt_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for PayPal merchant receipt emails."""
    if _PAYPAL_SENDER_HINT not in sender.lower():
        return None
    if "you paid" not in plain_text.lower():
        return None

    merchant = _extract_labeled_block_value(plain_text, ("merchant",))
    if not merchant and ":" in subject:
        merchant = subject.split(":", 1)[0].strip()
    if not merchant:
        match = _PAYPAL_YOU_PAID_RE.search(plain_text)
        if match:
            merchant = match.group(2).strip()

    total = None
    total_value = _extract_labeled_block_value(plain_text, ("total",))
    if total_value:
        match = re.search(r"\$?\s*(\d+(?:\.\d{2})?)", total_value)
        if match:
            total = float(match.group(1))
    if total is None:
        match = _PAYPAL_YOU_PAID_RE.search(plain_text)
        if match:
            total = float(match.group(1))
        else:
            match = _PAYPAL_SUBJECT_AMOUNT_RE.search(subject)
            if match:
                total = float(match.group(1))

    date_value = _extract_labeled_block_value(plain_text, ("transaction date",))
    receipt_date = _parse_payment_date_text(date_value) if date_value else None

    return {
        "merchant": merchant,
        "total": total,
        "receipt_date": receipt_date,
        "purchaser": _extract_payment_purchaser(plain_text),
        "card_last4": None,
        "category": None,
    }


def _extract_irs_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for IRS online payment confirmation emails."""
    if _IRS_SENDER_HINT not in sender.lower():
        return None
    if not re.search(
        r"confirmation\s+of\s+(?:scheduled\s+)?(?:payment\s+)?transaction"
        r"|you.?ve\s+successfully\s+submitted\s+your\s+payment",
        plain_text,
        re.IGNORECASE,
    ):
        return None

    total = None
    total_value = _extract_labeled_block_value(plain_text, ("total amount",))
    if total_value:
        match = re.search(r"\$?\s*(\d+(?:\.\d{2})?)", total_value)
        if match:
            total = float(match.group(1))

    receipt_date = None
    date_value = _extract_labeled_block_value(plain_text, ("date",))
    if date_value:
        receipt_date = _parse_payment_date_text(date_value)

    return {
        "merchant": "IRS",
        "total": total,
        "receipt_date": receipt_date,
        "purchaser": None,
        "card_last4": None,
        "category": None,
    }


def _extract_nelnet_billing_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for Nelnet student loan billing statement emails."""
    if _NELNET_SENDER_HINT not in sender.lower():
        return None
    if "billing statement" not in subject.lower() and "billing statement" not in plain_text.lower():
        return None

    total = None
    amount_value = _extract_labeled_block_value(plain_text, ("amount due",))
    if amount_value:
        match = re.search(r"\$?\s*(\d+(?:\.\d{2})?)", amount_value)
        if match:
            total = float(match.group(1))

    receipt_date = None
    date_value = _extract_labeled_block_value(plain_text, ("due date",))
    if date_value:
        date_match = _NELNET_MDY_DATE_RE.search(date_value)
        if date_match:
            receipt_date = _parse_numeric_payment_date(date_match.group(1))

    return {
        "merchant": "Nelnet",
        "total": total,
        "receipt_date": receipt_date,
        "purchaser": _extract_payment_purchaser(plain_text),
        "card_last4": None,
        "category": None,
    }


def _extract_venmo_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for Venmo payment notification emails."""
    if _VENMO_SENDER_HINT not in sender.lower():
        return None

    combined = f"{subject}\n{plain_text}"
    merchant: str | None = None
    total: float | None = None

    for pattern in (_VENMO_YOU_PAID_RE, _VENMO_PAID_YOU_RE, _VENMO_CHARGED_YOU_RE):
        match = pattern.search(combined)
        if match:
            merchant = match.group(1).strip()
            with suppress(ValueError, IndexError):
                total = float(match.group(2))
            break

    receipt_date: date | None = None
    date_match = _PAYMENT_DATE_RE.search(plain_text)
    if date_match:
        receipt_date = _parse_payment_date_text(date_match.group(0))

    return {
        "merchant": merchant,
        "total": total,
        "receipt_date": receipt_date,
        "purchaser": _extract_payment_purchaser(plain_text),
        "card_last4": None,
        "category": None,
    }


def _extract_wise_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for Wise money-movement notification emails."""
    if _WISE_SENDER_HINT not in sender.lower():
        return None
    if "money added to your account" not in subject.lower():
        return None

    match = _WISE_MONEY_ADDED_RE.search(plain_text)
    total = float(match.group(1)) if match else None

    return {
        "merchant": "Wise",
        "total": total,
        "receipt_date": None,
        "purchaser": None,
        "card_last4": None,
        "category": "finance",
    }


def _extract_xanterra_reservation_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for Xanterra/Yellowstone reservation confirmations."""
    combined = f"{subject}\n{plain_text}"
    if _XANTERRA_SENDER_HINT not in sender.lower():
        return None
    if not re.search(r"\breservation\s+confirmation\b", subject, re.IGNORECASE):
        return None
    if "yellowstone national park" not in combined.lower():
        return None

    total = _extract_labeled_money_amount(
        plain_text,
        (
            "deposit paid",
            "deposit required",
            "reservation total",
            "lodging total",
        ),
    )
    booking_date = None
    booking_value = _extract_labeled_block_value(plain_text, ("original booking date",))
    if booking_value:
        booking_date = _parse_numeric_payment_date(booking_value)

    lodging_amount = _extract_labeled_money_amount(
        plain_text,
        ("lodging total", "lodging", "nightly rate"),
    )
    taxes_amount = _extract_labeled_money_amount(plain_text, ("taxes/fees", "taxes & fees"))
    items = []
    if lodging_amount is not None:
        items.append(
            {
                "description": "Bridge Bay Campground lodging",
                "amount": lodging_amount,
                "quantity": None,
                "unit_price": None,
            }
        )
    if taxes_amount is not None:
        items.append(
            {
                "description": "Taxes/Fees",
                "amount": taxes_amount,
                "quantity": None,
                "unit_price": None,
            }
        )

    purchaser = None
    match = _XANTERRA_WELCOME_RE.search(plain_text)
    if match:
        purchaser = normalize_purchaser(match.group(1))

    return {
        "merchant": "Yellowstone National Park Lodges",
        "total": total,
        "receipt_date": booking_date,
        "purchaser": purchaser,
        "card_last4": None,
        "category": "outdoor/park",
        "processed_by": "email-reservation",
        "items": items,
    }


def _extract_amex_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for American Express payment confirmation emails."""
    if _AMEX_SENDER_HINT not in sender.lower():
        return None
    if "payment" not in subject.lower():
        return None

    amount = _extract_labeled_money_amount(plain_text, ("payment amount",))

    receipt_date = None
    date_value = _extract_labeled_block_value(plain_text, ("processed on",))
    if date_value:
        m = _PAYMENT_DATE_RE.search(date_value)
        if m:
            receipt_date = _parse_payment_date_text(m.group(0))
    if receipt_date is None:
        m = re.search(
            r"processed\s+on\s+(" + _PAYMENT_DATE_TEXT_PATTERN + r")\b",
            f"{subject}\n{plain_text}",
            re.IGNORECASE,
        )
        if m:
            receipt_date = _parse_payment_date_text(m.group(1))

    purchaser = None
    m = _AMEX_CARDHOLDER_RE.search(plain_text)
    if m:
        purchaser = normalize_purchaser(m.group(1).strip())

    card_last4 = None
    m = _AMEX_ACCOUNT_ENDING_RE.search(plain_text)
    if m:
        card_last4 = m.group(1)

    return {
        "merchant": "american express",
        "total": amount,
        "receipt_date": receipt_date,
        "purchaser": purchaser,
        "card_last4": card_last4,
        "category": "finance/credit-card-payment",
        "processed_by": "email-payment-amex",
    }


def _extract_structured_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for known payment-notification email formats."""
    return (
        _extract_credit_card_payment_email(subject, sender, plain_text)
        or _extract_amex_payment_email(subject, sender, plain_text)
        or _extract_progressive_payment_email(subject, sender, plain_text)
        or _extract_paypal_receipt_email(subject, sender, plain_text)
        or _extract_venmo_payment_email(subject, sender, plain_text)
        or _extract_irs_payment_email(subject, sender, plain_text)
        or _extract_nelnet_billing_email(subject, sender, plain_text)
        or _extract_wise_payment_email(subject, sender, plain_text)
        or _extract_xanterra_reservation_email(subject, sender, plain_text)
    )


def _is_internal_intake_notification(subject: str, sender: str, settings: dict) -> bool:
    """Return True when the email is an intake-generated notification."""
    gmail_cfg = settings.get("gmail", {})
    prefix = str(gmail_cfg.get("subject_prefix", "[intake] ")).strip()
    if not prefix:
        return False
    return subject.strip().startswith(prefix)


def _write_inline_email_receipt_artifact(
    settings: dict,
    *,
    category: str,
    filename: str,
    plain_text: str,
    row: dict,
) -> Path | None:
    """Persist an inline email receipt body under the Snowbridge receipt tree."""
    receipt_root_value = str(settings.get("receipt_dir") or "").strip()
    if not receipt_root_value:
        return None

    try:
        from intake.certification import receipt_certifications_to_dicts  # noqa: PLC0415
        from intake.parse import receipt_items_to_dicts  # noqa: PLC0415
        from intake.sidecar import write_sidecar  # noqa: PLC0415
        from intake.staff import receipt_staff_to_dicts  # noqa: PLC0415

        receipt_root = Path(receipt_root_value).expanduser()
        artifact_dir = receipt_root / "email" / category
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact = artifact_dir / filename
        artifact.write_text(plain_text, encoding="utf-8")
        payload = {k: v for k, v in row.items() if k != "raw_text"}
        payload["items"] = receipt_items_to_dicts(payload.get("items"))
        payload["certifications"] = receipt_certifications_to_dicts(payload.get("certifications"))
        payload["staff"] = receipt_staff_to_dicts(payload.get("staff"))
        write_sidecar(
            artifact,
            {
                **payload,
                "document_type": "email",
                "tier": "email",
            },
        )
        return artifact
    except OSError as exc:
        print(
            f"    [email] WARNING: could not write Snowbridge email artifact: {exc}",
            file=sys.stderr,
        )
        return None


def _upsert_html_receipt(
    db_conn, uid, mailbox, subject, sender, date_str, plain_text, settings
) -> None:
    """Try to extract fields from an inline HTML receipt and upsert into DB."""
    from dataclasses import replace  # noqa: PLC0415

    from intake.classify import classify, load_merchant_aliases, load_rules  # noqa: PLC0415
    from intake.db import upsert_receipt  # noqa: PLC0415
    from intake.merchant import consolidate_merchant  # noqa: PLC0415
    from intake.notify import send_processed_receipt_notifications  # noqa: PLC0415
    from intake.parse import parse_receipt  # noqa: PLC0415
    from intake.personal_finance import (  # noqa: PLC0415
        build_receipt_event,
        trigger_personal_finance,
    )

    rules = load_rules()
    merchant_aliases = load_merchant_aliases()
    # Build a synthetic filename for the receipt (uid-based)
    synthetic_name = f"email_{uid}_{mailbox.replace('/', '_')}.txt"
    email_header_date = _parse_email_header_date(date_str)
    receipt = parse_receipt(synthetic_name, plain_text)
    if email_header_date is not None:
        receipt = replace(receipt, scan_date=email_header_date)
    payment_email = _extract_structured_payment_email(subject, sender, plain_text)
    if payment_email is not None:
        receipt = replace(
            receipt,
            merchant=payment_email["merchant"],
            total=payment_email["total"],
            receipt_date=payment_email["receipt_date"],
            purchaser=payment_email["purchaser"],
            card_last4=payment_email["card_last4"],
            items=payment_email.get("items", []),
        )
        preferred_category = payment_email.get("category")
        if isinstance(preferred_category, str) and _category_exists(rules, preferred_category):
            category = preferred_category
        elif preferred_category == "finance/credit-card-payment":
            category = _preferred_payment_category(rules)
        else:
            category = classify(receipt.merchant or "", rules, full_text=plain_text)
        processed_by = payment_email.get("processed_by") or (
            "email-payment" if preferred_category else "email-parse"
        )
    else:
        receipt = replace(
            receipt, merchant=consolidate_merchant(receipt.merchant, merchant_aliases)
        )
        category = classify(receipt.merchant or "", rules, full_text=plain_text)
        processed_by = "email-parse"

    # Try AI extraction if API key available
    api_key = settings.get("openai_api_key")
    if api_key and payment_email is None:
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
                            '"receipt_time" (HH:MM), "purchaser", "card_last4", '
                            '"currency" (ISO-4217 code e.g. "USD", "CAD", "EUR", "JPY"; default "USD"), '
                            '"items" (array of {"description","amount","quantity","unit_price"}), '
                            '"certifications" (array of {"issuer","label","source_text"}), '
                            '"staff" (array of {"role","name","source_text"} for server, waitress, waiter, cashier, or clerk fields). '
                            "Use null for missing values and [] when no itemized lines are visible.\n\n"
                            f"Subject: {subject}\nFrom: {sender}\n\n{plain_text[:3000]}"
                        ),
                    }
                ],
                max_tokens=512,
            )
            import json

            raw = (response.choices[0].message.content or "{}").strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            ai_data = json.loads(raw)
            receipt = merge_ai_result(receipt, ai_data)
            receipt = replace(
                receipt,
                merchant=consolidate_merchant(receipt.merchant, merchant_aliases),
            )
            processed_by = settings.get("openai_model_high", "gpt-4o")
        except Exception:
            pass

    if receipt.card_last4 and not receipt.purchaser:
        inferred = infer_purchaser_from_card_last4(db_conn, receipt.card_last4)
        if inferred:
            receipt = replace(receipt, purchaser=inferred)

    row = {
        "filename": synthetic_name,
        "scan_date": (receipt.receipt_date or receipt.scan_date).isoformat(),
        "merchant": receipt.merchant,
        "total": receipt.total,
        "category": category,
        "receipt_dir": f"email/{category}",
        "raw_text": plain_text[:4000],
        "processed_by": processed_by,
        "purchaser": receipt.purchaser,
        "card_last4": receipt.card_last4,
        "currency": receipt.currency,
        "items": receipt.items,
        "certifications": receipt.certifications,
        "staff": receipt.staff,
    }

    upsert_receipt(
        db_conn,
        row,
    )
    _write_inline_email_receipt_artifact(
        settings,
        category=category,
        filename=synthetic_name,
        plain_text=plain_text,
        row=row,
    )
    sent, errors = send_processed_receipt_notifications(
        settings,
        receipt,
        category=category,
        receipt_dir=f"email/{category}",
        filename=synthetic_name,
        processed_by=processed_by,
    )
    for channel, error in errors.items():
        print(f"    [{channel}] WARNING: {error}", file=sys.stderr)
    for channel, count in sent.items():
        print(f"    [{channel}] notified {count} recipient(s)")
    try:
        trigger_personal_finance(
            settings,
            [
                build_receipt_event(
                    receipt,
                    category=category,
                    receipt_dir=f"email/{category}",
                    filename=synthetic_name,
                    processed_by=processed_by,
                    tier="email",
                )
            ],
            trigger="email",
        )
    except Exception as exc:
        print(f"    [personal-finance] WARNING: {exc}", file=sys.stderr)


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
