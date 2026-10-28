"""Email receipt ingestion via IMAP (shock-relay integration).

Searches Gmail IMAP for receipt / invoice emails, saves PDF attachments
to the receipt inbox for normal OCR backfill, parses inline HTML receipts
directly, and moves processed emails to a designated Gmail label.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import UTC, date, datetime
from email import message_from_bytes
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime
from pathlib import Path

from intake import email_finance, email_reply
from intake.db import (
    infer_purchaser_from_card_last4,
    is_email_message_id_processed,
    is_email_processed,
    record_email_receipt,
    upsert_document,
)
from intake.email_filing import (
    file_message_to_processed_label,
    move_message_without_marking_read,
    safe_save,
    safe_save_receipt_pdf,
    same_mailbox_label,
)
from intake.email_local_classifier import (
    ClassifierUnavailable,
    classify_ambiguous_email,
    is_legal_payment_correspondence,
)
from intake.email_provider import (
    fetch_message_by_uid,
    find_message_in_mailboxes,
    load_shock_relay,
    mailbox_select_candidates,
    select_mailbox,
)
from intake.email_text import html_to_text as _html_to_text
from intake.translation import maybe_translate_receipt

_DEFAULT_RECEIPT_SENDER_HINTS = (
    "service@paypal.com",
    "service.paypal.com",
    "venmo@venmo.com",
    "account.irs.gov",
    "nelnet.studentaid.gov",
    "noreply@wise.com",
    "reserve-noreply@xanterra.com",
    "info@smbctb.co.jp",
    "americanexpress.com",
    "no-reply@equifax.com",
    "support@s.usa.experian.com",
    "no-reply@revolut.com",
    "noreply@gotinder.com",
    "hello@binance.us",
    "post.binance.us",
    "donotreply@mail.schwab.com",
    "email.huntington.com",
    "customerservice@e.progressive.com",
    "email@t.sb.fanduel.com",
)
_DEFAULT_REPLY_MAILBOXES = email_reply._DEFAULT_REPLY_MAILBOXES
_INTAKE_REPLY_SOURCE = email_reply._INTAKE_REPLY_SOURCE
_INTAKE_REPLY_NOTE_SOURCE = email_reply._INTAKE_REPLY_NOTE_SOURCE
_INTAKE_REPLY_LINK_SOURCE = email_reply._INTAKE_REPLY_LINK_SOURCE
_INTAKE_RECEIPT_FILENAME_HEADER = email_reply._INTAKE_RECEIPT_FILENAME_HEADER
_INTAKE_RECEIPT_DIR_HEADER = email_reply._INTAKE_RECEIPT_DIR_HEADER
_PORTFOLIO_SERVICE_HEADER = email_reply._PORTFOLIO_SERVICE_HEADER
_CREW_CHIEF_INTENT_HEADER = email_reply._CREW_CHIEF_INTENT_HEADER
_INTAKE_SERVICE_VALUE = email_reply._INTAKE_SERVICE_VALUE
# Recorded for candidates with no receipt content: skipped later, never filed.
UNMATCHED_STATUS = "unmatched"
_INTAKE_GENERATED_INTENTS = email_reply._INTAKE_GENERATED_INTENTS


def html_to_text(html: str) -> str:
    return _html_to_text(html)


# ── shock-relay import helper ──────────────────────────────────────────────────


def _load_shock_relay(config_path: str):
    return load_shock_relay(config_path)


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
                "cash payment",
                "credit report",
                "credit score",
                "FICO",
                "policy renewal reminder",
                "reservation confirmation",
                "thank you for your payment",
                "payment confirmation",
                "statement",
                "monthly interest",
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
            status, _ = conn.select(mailbox, readonly=dry_run)
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
                    _status, msg_data = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
                    if _status != "OK":
                        raise RuntimeError(f"Cannot fetch email uid={uid}")
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
                        tracking = _email_message_tracking_state(
                            db_conn, uid=uid, mailbox=mailbox, message_id=message_id
                        )
                        if tracking["status"] == UNMATCHED_STATUS:
                            # Checked before and not a receipt: leave it in the
                            # inbox, and do not classify it (an LLM call) again.
                            if verbose:
                                print(f"    skip (no receipt content, checked before): {subject!r}")
                            result.skipped += 1
                            continue
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
                    attachments = sr.extract_attachments(payload)
                    pdf_attachments = [
                        (fn, ct, data)
                        for fn, ct, data in attachments
                        if ct == "application/pdf" or fn.lower().endswith(".pdf")
                    ]
                    non_receipt_notice = _is_known_non_receipt_notice(subject, sender, body_text)
                    if not pdf_attachments and non_receipt_notice:
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

                    saved_count = 0
                    inline_processed = False
                    classifier_unavailable = False
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
                    if body_text and not dry_run and not non_receipt_notice:
                        plain = html_to_text(html_body) if html_body else text_body
                        structured_email = _extract_structured_finance_email(
                            subject,
                            sender,
                            plain,
                        )
                        legal_payment_discussion = is_legal_payment_correspondence(subject, plain)
                        if structured_email is None and not pdf_attachments:
                            try:
                                structured_email = classify_ambiguous_email(
                                    subject, sender, plain, settings
                                )
                            except ClassifierUnavailable:
                                classifier_unavailable = True
                        if structured_email is not None or (
                            not pdf_attachments
                            and not legal_payment_discussion
                            and _looks_like_receipt(plain)
                        ):
                            _upsert_html_receipt(
                                db_conn,
                                uid,
                                mailbox,
                                subject,
                                sender,
                                date_str,
                                plain,
                                settings,
                                structured_email=structured_email,
                            )
                            result.html_processed += 1
                            inline_processed = True
                            if verbose:
                                kind = (
                                    "document"
                                    if structured_email and structured_email.get("document_kind")
                                    else "receipt"
                                )
                                print(f"    inline {kind} → processed")

                    if not dry_run:
                        if not _email_was_handled(saved_count, inline_processed):
                            if classifier_unavailable:
                                # No verdict yet (model down or held by the GPU
                                # gate): leave it unrecorded to classify next poll.
                                if verbose:
                                    print(
                                        f"    skip (classifier unavailable, retry later): {subject!r}"
                                    )
                                result.skipped += 1
                                continue
                            # Remember the verdict so the next poll does not
                            # classify the same message again.
                            record_email_receipt(
                                db_conn,
                                uid=uid,
                                mailbox=mailbox,
                                message_id=message_id,
                                subject=subject,
                                sender=sender,
                                received_at=date_str,
                                attachment_count=0,
                                status=UNMATCHED_STATUS,
                            )
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
        _fetch_kalshi_spam_liquidations(
            sr,
            config,
            settings,
            db_conn,
            result,
            processed_label=processed_label,
            since_days=since_days,
            limit=limit,
            dry_run=dry_run,
            verbose=verbose,
        )
    except Exception as exc:
        result.errors += 1
        if verbose:
            print(f"  WARNING Kalshi spam processing degraded: {exc}")

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

    if settings.get("career", {}).get("repo_root"):
        try:
            from intake.job_search_mail import fetch_actions  # noqa: PLC0415

            fetch_actions(settings, dry_run=dry_run, verbose=verbose)
        except Exception as exc:
            result.errors += 1
            if verbose:
                print(f"  WARNING career mail processing degraded: {exc}")

    return result


# ── helpers ────────────────────────────────────────────────────────────────────


def _fetch_kalshi_spam_liquidations(
    sr,
    config,
    settings: dict,
    db_conn,
    result: FetchResult,
    *,
    processed_label: str,
    since_days: int,
    limit: int,
    dry_run: bool,
    verbose: bool,
) -> None:
    """Recover only verified-shape Kalshi liquidation notices from Gmail Spam."""
    if not hasattr(sr, "build_search_terms") or not hasattr(sr, "parse_int_uid_list"):
        return
    mailbox = "[Gmail]/Spam"
    with sr.open_imap_connection(config) as conn:
        status, _ = select_mailbox(conn, mailbox, readonly=dry_run)
        if status != "OK":
            return
        terms = sr.build_search_terms(from_contains="kalshi", since_days=since_days)
        status, data = conn.uid("search", config.imap.search_charset, *terms)
        if status != "OK":
            return
        for uid in sorted(sr.parse_int_uid_list(data), reverse=True)[:limit]:
            try:
                status, msg_data = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
                payload = sr.extract_fetch_payload(msg_data)
                if status != "OK" or not payload:
                    continue
                msg = message_from_bytes(payload, policy=default_policy)
                if not _has_google_apple_relay_authentication(msg):
                    continue
                subject = sr.decode_mime_header(msg.get("Subject"))
                sender = sr.decode_mime_header(msg.get("From"))
                plain, html = sr.extract_message_bodies(msg)
                body = plain or (html_to_text(html) if html else "")
                structured = _extract_structured_finance_email(subject, sender, body)
                if not structured or structured.get("processed_by") != "email-kalshi-liquidation":
                    continue
                result.found += 1
                message_id = sr.decode_mime_header(msg.get("Message-ID"))
                if is_email_processed(db_conn, uid, mailbox) or (
                    message_id and is_email_message_id_processed(db_conn, message_id)
                ):
                    if not dry_run:
                        sr.move_message(conn, uid, processed_label)
                        conn.expunge()
                    result.skipped += 1
                    continue
                if dry_run:
                    if verbose:
                        print(f"  Kalshi Spam candidate: {subject!r}")
                    continue
                date_str = str(msg.get("Date", "") or "").strip()
                _upsert_html_receipt(
                    db_conn,
                    uid,
                    mailbox,
                    subject,
                    sender,
                    date_str,
                    body,
                    settings,
                    structured_email=structured,
                )
                _write_processed_email_artifact(
                    settings,
                    uid=uid,
                    mailbox=mailbox,
                    message_id=message_id,
                    subject=subject,
                    sender=sender,
                    received_at=date_str,
                    payload=payload,
                    attachment_count=0,
                    inline_processed=True,
                )
                record_email_receipt(
                    db_conn,
                    uid=uid,
                    mailbox=mailbox,
                    message_id=message_id,
                    subject=subject,
                    sender=sender,
                    received_at=date_str,
                    attachment_count=0,
                )
                sr.mark_message_read(conn, uid)
                sr.move_message(conn, uid, processed_label)
                conn.expunge()
                result.html_processed += 1
                if verbose:
                    print(
                        f"  Kalshi liquidation → documents/investments; filed to {processed_label!r}"
                    )
            except Exception as exc:
                result.errors += 1
                if verbose:
                    print(f"  ERROR Kalshi Spam uid={uid}: {exc}")


def _has_google_apple_relay_authentication(msg) -> bool:
    """Require Gmail's passing Apple private relay checks before promoting Spam."""
    for value in msg.get_all("Authentication-Results", []):
        normalized = " ".join(str(value).lower().split())
        if (
            normalized.startswith("mx.google.com;")
            and re.search(r"\bdkim=pass\s+header\.i=@privaterelay\.appleid\.com\b", normalized)
            and re.search(r"\bdmarc=pass\b", normalized)
        ):
            return True
    return False


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


def _configured_reply_mailboxes(email_cfg: dict):
    return email_reply._configured_reply_mailboxes(email_cfg)


def _email_was_handled(saved_count: int, inline_processed: bool) -> bool:
    """Return True when the message yielded at least one intake artifact."""
    return saved_count > 0 or inline_processed


def _same_mailbox_label(left: str, right: str) -> bool:
    return same_mailbox_label(left, right)


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
    return file_message_to_processed_label(
        sr,
        conn,
        uid,
        source_mailbox=source_mailbox,
        processed_label=processed_label,
    )


def _move_message_without_marking_read(
    sr,
    conn,
    uid: int,
    *,
    source_mailbox: str,
    processed_label: str,
) -> bool:
    return move_message_without_marking_read(
        sr,
        conn,
        uid,
        source_mailbox=source_mailbox,
        processed_label=processed_label,
    )


def _safe_save(dest_dir: Path, filename: str, data: bytes) -> Path:
    return safe_save(dest_dir, filename, data)


def _safe_save_receipt_pdf(
    dest_dir: Path,
    filename: str,
    data: bytes,
    *,
    received_at: str | None = None,
) -> Path:
    return safe_save_receipt_pdf(dest_dir, filename, data, received_at=received_at)


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


_RECEIPT_PATTERNS = email_finance._RECEIPT_PATTERNS
_PAYMENT_EMAIL_PATTERNS = email_finance._PAYMENT_EMAIL_PATTERNS
_CARD_SCHEDULED_NOTICE_RE = email_finance._CARD_SCHEDULED_NOTICE_RE
_PAYPAL_UPCOMING_PURCHASE_RE = email_finance._PAYPAL_UPCOMING_PURCHASE_RE
_ANY_EMAIL_RE = email_finance._ANY_EMAIL_RE
_PAYMENT_AMOUNT_RE = email_finance._PAYMENT_AMOUNT_RE
_PAYMENT_OF_RE = email_finance._PAYMENT_OF_RE
_PAYMENT_INLINE_AMOUNT_RE = email_finance._PAYMENT_INLINE_AMOUNT_RE
_ACCOUNT_ENDING_RE = email_finance._ACCOUNT_ENDING_RE
_LAST4_RE = email_finance._LAST4_RE
_CAPITAL_ONE_CARD_RE = email_finance._CAPITAL_ONE_CARD_RE
_PAYMENT_GREETING_RE = email_finance._PAYMENT_GREETING_RE
_PAYMENT_DATE_TEXT_PATTERN = email_finance._PAYMENT_DATE_TEXT_PATTERN
_PAYMENT_DATE_RE = email_finance._PAYMENT_DATE_RE
_PAYMENT_MDY_DATE_RE = email_finance._PAYMENT_MDY_DATE_RE
_POSTED_TO_ACCOUNT_DATE_RE = email_finance._POSTED_TO_ACCOUNT_DATE_RE
_SCHEDULED_FOR_DATE_RE = email_finance._SCHEDULED_FOR_DATE_RE
_DUE_ON_DATE_RE = email_finance._DUE_ON_DATE_RE
_CAPITAL_ONE_SENDER_HINT = email_finance._CAPITAL_ONE_SENDER_HINT
_DISCOVER_SENDER_HINT = email_finance._DISCOVER_SENDER_HINT
_AMEX_SENDER_HINT = email_finance._AMEX_SENDER_HINT
_EQUIFAX_SENDER_HINT = email_finance._EQUIFAX_SENDER_HINT
_EXPERIAN_SENDER_HINT = email_finance._EXPERIAN_SENDER_HINT
_TRANSUNION_SENDER_HINT = email_finance._TRANSUNION_SENDER_HINT
_CREDIT_KARMA_SENDER_HINT = email_finance._CREDIT_KARMA_SENDER_HINT
_AMEX_ACCOUNT_ENDING_RE = email_finance._AMEX_ACCOUNT_ENDING_RE
_AMEX_CARDHOLDER_RE = email_finance._AMEX_CARDHOLDER_RE
_PAYPAL_SENDER_HINT = email_finance._PAYPAL_SENDER_HINT
_PAYPAL_SUBJECT_AMOUNT_RE = email_finance._PAYPAL_SUBJECT_AMOUNT_RE
_VENMO_SENDER_HINT = email_finance._VENMO_SENDER_HINT
_VENMO_YOU_PAID_RE = email_finance._VENMO_YOU_PAID_RE
_VENMO_PAID_YOU_RE = email_finance._VENMO_PAID_YOU_RE
_VENMO_CHARGED_YOU_RE = email_finance._VENMO_CHARGED_YOU_RE
_IRS_SENDER_HINT = email_finance._IRS_SENDER_HINT
_NELNET_SENDER_HINT = email_finance._NELNET_SENDER_HINT
_NELNET_MDY_DATE_RE = email_finance._NELNET_MDY_DATE_RE
_WISE_SENDER_HINT = email_finance._WISE_SENDER_HINT
_WISE_MONEY_ADDED_RE = email_finance._WISE_MONEY_ADDED_RE
_XANTERRA_SENDER_HINT = email_finance._XANTERRA_SENDER_HINT
_XANTERRA_WELCOME_RE = email_finance._XANTERRA_WELCOME_RE
_CREDIT_REPORTING_ALLCAPS_GREETING_RE = email_finance._CREDIT_REPORTING_ALLCAPS_GREETING_RE
_CREDIT_REPORTING_INLINE_GREETING_RE = email_finance._CREDIT_REPORTING_INLINE_GREETING_RE
_BINANCE_SENDER_HINT = email_finance._BINANCE_SENDER_HINT
_PAYPAL_YOU_PAID_RE = email_finance._PAYPAL_YOU_PAID_RE
_QUOTED_REPLY_SPLIT_RE = email_reply._QUOTED_REPLY_SPLIT_RE
_REPLY_SUBJECT_PREFIX_RE = email_reply._REPLY_SUBJECT_PREFIX_RE
_REPLY_MESSAGE_ID_RE = email_reply._REPLY_MESSAGE_ID_RE
_REPLY_FILENAME_RE = email_reply._REPLY_FILENAME_RE
_REPLY_FILE_RE = email_reply._REPLY_FILE_RE
_REPLY_UPDATE_RE = email_reply._REPLY_UPDATE_RE
_REPLY_CLEAR_RE = email_reply._REPLY_CLEAR_RE
_REPLY_LINK_ACTION_RE = email_reply._REPLY_LINK_ACTION_RE
_REPLY_LINK_TARGET_RE = email_reply._REPLY_LINK_TARGET_RE


def _intake_processed_subject_fragment(settings: dict):
    return email_reply._intake_processed_subject_fragment(settings)


def _is_intake_reply_message(msg, settings: dict):
    return email_reply._is_intake_reply_message(msg, settings)


def _is_intake_generated_message(msg):
    return email_reply._is_intake_generated_message(msg)


def _contains_intake_notification_quote(text: str):
    return email_reply._contains_intake_notification_quote(text)


def _message_text(msg, sr):
    return email_reply._message_text(msg, sr)


def _combine_message_body_text(plain_text: str | None, html_body: str | None) -> str:
    """Return searchable text from plain and HTML message bodies."""
    html_text = html_to_text(html_body) if html_body else ""
    parts = [part.strip() for part in (plain_text, html_text) if part and part.strip()]
    return "\n\n".join(parts).strip()


def _is_known_non_receipt_notice(subject: str, sender: str, plain_text: str):
    return email_finance._is_known_non_receipt_notice(subject, sender, plain_text)


def _trim_reply_note_text(text: str):
    return email_reply._trim_reply_note_text(text)


def _extract_receipt_filename_from_text(text: str):
    return email_reply._extract_receipt_filename_from_text(text)


def _extract_receipt_filename_from_message(msg, sr):
    return email_reply._extract_receipt_filename_from_message(msg, sr)


def _extract_reply_message_ids(msg):
    return email_reply._extract_reply_message_ids(msg)


def _reply_references_header(msg):
    return email_reply._reply_references_header(msg)


def _reply_thread_token(msg, message_id: str):
    return email_reply._reply_thread_token(msg, message_id)


def _mailbox_select_candidates(mailbox: str) -> list[str]:
    return mailbox_select_candidates(mailbox)


def _select_mailbox(conn, mailbox: str, *, readonly: bool) -> tuple[str, object]:
    return select_mailbox(conn, mailbox, readonly=readonly)


def _fetch_message_by_uid(conn, mailbox: str, uid: int, *, readonly: bool) -> object:
    return fetch_message_by_uid(conn, mailbox, uid, readonly=readonly)


def _find_message_in_mailboxes(
    conn,
    sr,
    mailboxes: list[str],
    message_id: str,
    *,
    recent_limit: int,
    search_charset,
) -> dict[str, object] | None:
    return find_message_in_mailboxes(
        conn,
        sr,
        mailboxes,
        message_id,
        recent_limit=recent_limit,
        search_charset=search_charset,
    )


def _resolve_reply_receipt_filename(conn, config, sr, settings: dict, msg, full_text: str):
    return email_reply._resolve_reply_receipt_filename(conn, config, sr, settings, msg, full_text)


def _normalize_reply_field(field: str):
    return email_reply._normalize_reply_field(field)


def _parse_reply_update_value(field: str, value: str):
    return email_reply._parse_reply_update_value(field, value)


def _extract_reply_updates(note_text: str):
    return email_reply._extract_reply_updates(note_text)


def _reply_requests_file_link(note_text: str):
    return email_reply._reply_requests_file_link(note_text)


def _looks_like_generated_file_link_reply(note_text: str):
    return email_reply._looks_like_generated_file_link_reply(note_text)


def _reply_link_already_recorded(db_conn, filename: str, reply_token: str, reply_message_id: str):
    return email_reply._reply_link_already_recorded(
        db_conn, filename, reply_token, reply_message_id
    )


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
):
    return email_reply._maybe_send_reply_link(
        db_conn,
        settings,
        filename=filename,
        note_text=note_text,
        recipient=recipient,
        subject=subject,
        reply_token=reply_token,
        reply_message_id=reply_message_id,
        references=references,
    )


def _sync_updated_sidecar(settings: dict, row: dict):
    return email_reply._sync_updated_sidecar(settings, row)


def _record_reply_note(
    db_conn,
    *,
    filename: str,
    note_text: str,
    requested_updates: dict[str, object | None] | None = None,
):
    return email_reply._record_reply_note(
        db_conn, filename=filename, note_text=note_text, requested_updates=requested_updates
    )


def _apply_reply_to_receipt(db_conn, settings: dict, *, filename: str, note_text: str):
    return email_reply._apply_reply_to_receipt(
        db_conn, settings, filename=filename, note_text=note_text
    )


def _search_reply_uids(conn, config, *, subject_fragment: str, since_days: int, limit: int):
    return email_reply._search_reply_uids(
        conn, config, subject_fragment=subject_fragment, since_days=since_days, limit=limit
    )


def _process_reply_messages(
    config,
    settings: dict,
    db_conn,
    result: FetchResult,
    *,
    since_days: int,
    limit: int,
    verbose: bool,
):
    return email_reply._process_reply_messages(
        config, settings, db_conn, result, since_days=since_days, limit=limit, verbose=verbose
    )


def _looks_like_receipt(text: str):
    if _contains_intake_notification_quote(text):
        return False
    if _is_known_non_receipt_notice("", "", text):
        return False
    return bool(_RECEIPT_PATTERNS.search(text) or _PAYMENT_EMAIL_PATTERNS.search(text))


def _category_exists(rules: dict, category_path: str):
    return email_finance._category_exists(rules, category_path)


def _preferred_payment_category(rules: dict):
    return email_finance._preferred_payment_category(rules)


def _preferred_credit_reporting_category(rules: dict):
    return email_finance._preferred_credit_reporting_category(rules)


def _parse_payment_date_text(text: str):
    return email_finance._parse_payment_date_text(text)


def _parse_numeric_payment_date(text: str):
    return email_finance._parse_numeric_payment_date(text)


def _parse_email_header_date(text: str) -> date | None:
    """Parse an RFC822 email date header into the sender-local calendar date."""
    try:
        parsed = parsedate_to_datetime(text) if text else None
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return parsed.date() if isinstance(parsed, datetime) else None


def _extract_labeled_block_value(plain_text: str, labels: tuple[str, ...]):
    return email_finance._extract_labeled_block_value(plain_text, labels)


def _extract_labeled_money_amount(plain_text: str, labels: tuple[str, ...]):
    return email_finance._extract_labeled_money_amount(plain_text, labels)


def _extract_payment_amount(subject: str, plain_text: str):
    return email_finance._extract_payment_amount(subject, plain_text)


def _extract_payment_card_last4(plain_text: str):
    return email_finance._extract_payment_card_last4(plain_text)


def _extract_payment_purchaser(plain_text: str):
    return email_finance._extract_payment_purchaser(plain_text)


def _extract_credit_reporting_purchaser(plain_text: str):
    return email_finance._extract_credit_reporting_purchaser(plain_text)


def _extract_payment_effective_date(subject: str, plain_text: str):
    return email_finance._extract_payment_effective_date(subject, plain_text)


def _extract_credit_card_payment_email(subject: str, sender: str, plain_text: str):
    return email_finance._extract_credit_card_payment_email(subject, sender, plain_text)


def _extract_paypal_receipt_email(subject: str, sender: str, plain_text: str):
    return email_finance._extract_paypal_receipt_email(subject, sender, plain_text)


def _extract_irs_payment_email(subject: str, sender: str, plain_text: str):
    return email_finance._extract_irs_payment_email(subject, sender, plain_text)


def _extract_nelnet_billing_email(subject: str, sender: str, plain_text: str):
    return email_finance._extract_nelnet_billing_email(subject, sender, plain_text)


def _extract_venmo_payment_email(subject: str, sender: str, plain_text: str):
    return email_finance._extract_venmo_payment_email(subject, sender, plain_text)


def _extract_wise_payment_email(subject: str, sender: str, plain_text: str):
    return email_finance._extract_wise_payment_email(subject, sender, plain_text)


def _extract_xanterra_reservation_email(subject: str, sender: str, plain_text: str):
    return email_finance._extract_xanterra_reservation_email(subject, sender, plain_text)


def _extract_amex_payment_email(subject: str, sender: str, plain_text: str):
    return email_finance._extract_amex_payment_email(subject, sender, plain_text)


def _extract_credit_reporting_email(subject: str, sender: str, plain_text: str):
    return email_finance._extract_credit_reporting_email(subject, sender, plain_text)


def _extract_structured_finance_email(subject: str, sender: str, plain_text: str):
    return email_finance._extract_structured_finance_email(subject, sender, plain_text)


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


def _upsert_html_income_document(
    db_conn,
    *,
    synthetic_name: str,
    receipt,
    plain_text: str,
    settings: dict,
) -> None:
    """File an inline email notification of money received as a non-spend
    document instead of a receipt (see the is_income branch in
    _upsert_html_receipt)."""
    scan_date = (receipt.receipt_date or receipt.scan_date).isoformat()
    category = "documents/income"
    row = {
        "filename": synthetic_name,
        "scan_date": scan_date,
        "document_date": scan_date,
        "document_kind": category,
        "title": f"{receipt.merchant} — payment received",
        "issuer": receipt.merchant,
        "category": category,
        "receipt_dir": f"email/{category}",
        "raw_text": plain_text[:4000],
        "processed_by": "email-payment",
    }
    upsert_document(db_conn, row)
    _write_inline_email_receipt_artifact(
        settings,
        category=category,
        filename=synthetic_name,
        plain_text=plain_text,
        row=row,
    )


def _upsert_html_receipt(
    db_conn,
    uid,
    mailbox,
    subject,
    sender,
    date_str,
    plain_text,
    settings,
    *,
    structured_email: dict | None = None,
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
    finance_email = structured_email or _extract_structured_finance_email(
        subject,
        sender,
        plain_text,
    )
    if finance_email is not None and finance_email.get("document_kind"):
        category = finance_email["document_kind"]
        row = {
            "filename": synthetic_name,
            "scan_date": receipt.scan_date.isoformat(),
            "document_date": finance_email.get("document_date")
            or (email_header_date.isoformat() if email_header_date else None),
            "document_kind": category,
            "title": finance_email["title"],
            "issuer": finance_email["merchant"],
            "subject": finance_email.get("subject"),
            "body_excerpt": finance_email.get("body_excerpt"),
            "category": category,
            "receipt_dir": f"email/{category}",
            "raw_text": plain_text,
            "processed_by": finance_email.get("processed_by") or "email-finance-notice",
        }
        upsert_document(db_conn, row)
        _write_inline_email_receipt_artifact(
            settings, category=category, filename=synthetic_name, plain_text=plain_text, row=row
        )
        return
    if finance_email is not None and finance_email.get("is_income"):
        # Money credited to the account (e.g. "X paid you $Y" on Venmo) is
        # income, not spend — file it as a non-spend document instead of a
        # receipt, mirroring how a check payable to a known purchaser is
        # routed (see is_check_payable_to_known_purchaser).
        receipt = replace(
            receipt,
            merchant=finance_email["merchant"],
            total=finance_email["total"],
            receipt_date=finance_email["receipt_date"],
            purchaser=finance_email["purchaser"],
        )
        _upsert_html_income_document(
            db_conn,
            synthetic_name=synthetic_name,
            receipt=receipt,
            plain_text=plain_text,
            settings=settings,
        )
        return
    if finance_email is not None:
        receipt = replace(
            receipt,
            merchant=finance_email["merchant"],
            total=finance_email["total"],
            receipt_date=finance_email["receipt_date"],
            purchaser=finance_email["purchaser"],
            card_last4=finance_email["card_last4"],
            currency=finance_email.get("currency") or receipt.currency,
            items=finance_email.get("items", []),
        )
        preferred_category = finance_email.get("category")
        if isinstance(preferred_category, str) and _category_exists(rules, preferred_category):
            category = preferred_category
        elif preferred_category == "finance/credit-card-payment":
            category = _preferred_payment_category(rules)
        elif preferred_category == "finance/credit-reporting":
            category = _preferred_credit_reporting_category(rules)
        else:
            category = classify(receipt.merchant or "", rules, full_text=plain_text)
        processed_by = finance_email.get("processed_by") or (
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
    if api_key and finance_email is None:
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
                            '"currency" (ISO-4217 code e.g. "USD", "CAD", "EUR", "JPY"; use null if not shown or uncertain), '
                            '"language" ("en", "ja", or "und"), '
                            '"translated_text" (concise English translation when the receipt body is non-English; otherwise null), '
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

    receipt = maybe_translate_receipt(
        receipt,
        provider=settings.get("translation_provider") or "ollama",
        model=settings.get("translation_model") or "aya-expanse:8b",
        base_url=settings.get("translation_base_url") or "http://localhost:11434",
        timeout_seconds=int(settings.get("translation_timeout_seconds") or 60),
    )

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
        "language": receipt.language,
        "translated_text": receipt.translated_text,
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
        print(f"    [finance-trigger] WARNING: {exc}", file=sys.stderr)


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
