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
from html.parser import HTMLParser
from pathlib import Path

from intake.db import infer_purchaser_from_card_last4, is_email_processed, record_email_receipt
from intake.naming import allocate_canonical_path
from intake.purchaser import normalize_purchaser

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
                "your payment of",
                "your scheduled payment",
                "policy renewal reminder",
            ]
        )
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

                    if _is_internal_intake_notification(subject, sender, settings):
                        if verbose:
                            print(f"    skip (internal intake notification): {subject!r}")
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
    r"payment\s+of\s+\$"
    r")\b",
    re.IGNORECASE,
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
_PROGRESSIVE_SENDER_HINT = "customerservice@e.progressive.com"


def _looks_like_receipt(text: str) -> bool:
    """Heuristic: does this email body text resemble a receipt?"""
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


def _extract_structured_payment_email(subject: str, sender: str, plain_text: str) -> dict | None:
    """Return structured fields for known payment-notification email formats."""
    return _extract_credit_card_payment_email(
        subject, sender, plain_text
    ) or _extract_progressive_payment_email(subject, sender, plain_text)


def _is_internal_intake_notification(subject: str, sender: str, settings: dict) -> bool:
    """Return True when the email is an intake-generated notification."""
    gmail_cfg = settings.get("gmail", {})
    prefix = str(gmail_cfg.get("subject_prefix", "[intake] ")).strip()
    if not prefix:
        return False
    return subject.strip().startswith(prefix)


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
    receipt = parse_receipt(synthetic_name, plain_text)
    payment_email = _extract_structured_payment_email(subject, sender, plain_text)
    if payment_email is not None:
        receipt = replace(
            receipt,
            merchant=payment_email["merchant"],
            total=payment_email["total"],
            receipt_date=payment_email["receipt_date"],
            purchaser=payment_email["purchaser"],
            card_last4=payment_email["card_last4"],
            items=[],
        )
        preferred_category = payment_email.get("category")
        if isinstance(preferred_category, str) and _category_exists(rules, preferred_category):
            category = preferred_category
        elif preferred_category == "finance/credit-card-payment":
            category = _preferred_payment_category(rules)
        else:
            category = classify(receipt.merchant or "", rules, full_text=plain_text)
        processed_by = "email-payment"
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
                            '"items" (array of {"description","amount","quantity","unit_price"}), '
                            '"certifications" (array of {"issuer","label","source_text"}). '
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
            "processed_by": processed_by,
            "purchaser": receipt.purchaser,
            "card_last4": receipt.card_last4,
            "items": receipt.items,
            "certifications": receipt.certifications,
        },
    )
    _, errors = send_processed_receipt_notifications(
        settings,
        receipt,
        category=category,
        receipt_dir=f"email/{category}",
        filename=synthetic_name,
        processed_by=processed_by,
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
                    processed_by=processed_by,
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
