"""Header-only triage of Inbox mail that receipt discovery never examines.

Receipt discovery (``intake.email``) searches by subject keyword and sender allow-list, so mail
from any other sender is never looked at and accumulates in the Inbox.  This pass looks at
*headers and the subject only* (never the body, never an LLM) and files the clearly
non-actionable mail, leaving everything that could need a human where it is:

========  =====================================================  ======================
kind      what it is                                             action
========  =====================================================  ======================
notify    portfolio-generated mail (digests, job/torrent alerts)  move to notify label
promo     bulk mail: List-Unsubscribe / List-Id / Precedence      move to promo label
notice    automated no-reply mail with no bulk headers            move to notice label
keep      anything that looks transactional, a security or        stay in the Inbox
          billing alert, or comes from a person
========  =====================================================  ======================

Moves keep the message *unread* and are plain Gmail label moves, so they are reversible.  Every
decision is recorded in ``email_receipts`` (``triage-<kind>``) so a message is judged once, and
logged so it can be traced.  Off by default; enable with ``[email.triage] enabled = true`` and
preview with ``intake email triage --dry-run``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default as default_policy
from email.utils import parseaddr, parsedate_to_datetime

log = logging.getLogger("intake.triage")

DEFAULT_LABELS = {
    "notify": "intake/notify",
    "promo": "intake/promos",
    "notice": "intake/notices",
}

# Subjects that keep a message in the Inbox *even when it carries bulk headers*: money moving,
# security, deadlines, and a real transaction (an order, a booking, a delivery).  A receipt or a
# sign-in alert can carry a List-Unsubscribe header, and missing one is far worse than leaving
# a promo behind.
HARD_KEEP_SUBJECT_RE = re.compile(
    r"\b(?:receipt|invoice|payment|paid|statement|bill(?:ing)?|charge[ds]?|refund|"
    r"verif(?:y|ication)|security|sign[- ]?in|log[- ]?in|password|passcode|one[- ]time|"
    r"fraud|declined|failed|failure|suspend(?:ed)?|action required|urgent|due|overdue|"
    r"order(?:ed)?|confirm(?:ation|ed)?|booking|reservation|itinerary|boarding|"
    r"delivered|shipped)\b",
    re.IGNORECASE,
)
# Words that keep *non-bulk* mail (a trip, a ticket, an alert) but that marketing reuses
# ("extending your trip", "no lift ticket", "important updates", "promo code"), so bulk headers
# override them and the mail is filed as a promo.
SOFT_KEEP_SUBJECT_RE = re.compile(
    r"\b(?:ticket|trip|ride|code|alert|important|expir(?:e|es|ing|ed)|usage|quota|limit|minutes)\b",
    re.IGNORECASE,
)
# Informational mail whose subject trips the broad keep keywords above (usage, minutes, limit).
# Matched first, so it is filed as a notice.  Extend with [email.triage] notice_subject_regexes.
DEFAULT_NOTICE_SUBJECT_RES = (
    # GitHub Actions quota notices: CI falls back to the cloud and local runners by itself.
    r"\bused \d+% of the Actions minutes\b",
)
AUTOMATED_SENDER_RE = re.compile(
    r"(?:^|[-_.+])(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply)(?:$|[-_.+\d])|"
    r"^(?:notifications?|notify|mailer|alerts?|updates?|news|newsletter|info|hello|support|"
    r"team|bounce|auto)(?:$|[-_.+\d])",
    re.IGNORECASE,
)
DEFAULT_SELF_SUBJECT_RE = (
    r"^\s*\[(?:intake|portfolio)\]|^\s*Magneto complete|^\s*Job guard|^\s*\[tradility\]"
)

HEADER_FIELDS = "FROM SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE LIST-ID PRECEDENCE AUTO-SUBMITTED X-PORTFOLIO-SERVICE"

# Kept messages are deliberately NOT recorded in email_receipts: the receipt loop treats any
# recorded uid as already handled and would file a kept message that later becomes a receipt
# candidate.  This only quiets the repeat log lines for them within one daemon run.
_KEPT_LOGGED: set[tuple[str, int]] = set()


@dataclass(frozen=True)
class Decision:
    kind: str  # notify | promo | notice | keep
    reason: str
    label: str | None = None


@dataclass
class TriageResult:
    examined: int = 0
    moved: int = 0
    by_kind: Counter = field(default_factory=Counter)
    decisions: list[tuple[int, str, str, Decision]] = field(default_factory=list)


def is_bulk(headers) -> bool:
    precedence = str(headers.get("Precedence", "")).strip().lower()
    return bool(
        headers.get("List-Unsubscribe")
        or headers.get("List-Id")
        or precedence in {"bulk", "list", "junk"}
    )


def classify_headers(
    headers,
    *,
    own_addresses: frozenset[str] = frozenset(),
    managed_senders: frozenset[str] = frozenset(),
    skip_senders: frozenset[str] = frozenset(),
    labels: dict[str, str] | None = None,
    self_subject_re: str = DEFAULT_SELF_SUBJECT_RE,
    managed_window_days: int = 7,
    now: datetime | None = None,
    notice_subject_res: tuple[str, ...] = DEFAULT_NOTICE_SUBJECT_RES,
) -> Decision:
    """Decide what to do with one message from its headers (``headers.get(name)``)."""
    labels = {**DEFAULT_LABELS, **(labels or {})}
    sender = parseaddr(str(headers.get("From", "")))[1].lower()
    subject = str(headers.get("Subject", ""))
    local = sender.split("@", 1)[0]

    if sender in skip_senders:
        return Decision("keep", "sender is on the skip list")
    if sender in managed_senders and _within_days(headers, managed_window_days, now):
        return Decision("keep", "handled by the newsletter pipeline")
    # Older than the newsletter job's look-back, it can never be digested or filed by it, so an
    # aged-out issue falls through and is filed like any other bulk mail.
    if sender in own_addresses:
        generated = bool(headers.get("X-Portfolio-Service")) or bool(
            re.search(self_subject_re, subject, re.IGNORECASE)
        )
        if generated:
            return Decision("notify", "portfolio-generated mail", labels["notify"])
        return Decision("keep", "a note from yourself")
    if any(re.search(pattern, subject, re.IGNORECASE) for pattern in notice_subject_res):
        return Decision("notice", "informational subject", labels["notice"])
    if HARD_KEEP_SUBJECT_RE.search(subject):
        return Decision("keep", "subject is about money, security or a deadline")
    if is_bulk(headers):
        return Decision("promo", "bulk mail headers", labels["promo"])
    if SOFT_KEEP_SUBJECT_RE.search(subject):
        return Decision("keep", "subject looks transactional or like an alert")
    if str(headers.get("Auto-Submitted", "no")).strip().lower() not in {"", "no"}:
        return Decision("notice", "auto-submitted mail", labels["notice"])
    if AUTOMATED_SENDER_RE.search(local):
        return Decision("notice", "automated sender, no bulk headers", labels["notice"])
    return Decision("keep", "looks like a person")


def _within_days(headers, days: int, now: datetime | None) -> bool:
    """True when the Date header is within *days* (also when it cannot be read: be cautious)."""
    try:
        sent = parsedate_to_datetime(str(headers.get("Date", "")))
    except (TypeError, ValueError):
        return True
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - sent).days <= days


def _managed_senders() -> frozenset[str]:
    try:
        from intake.newsletter import _SOURCES  # noqa: PLC0415

        return frozenset(str(s).lower() for s in _SOURCES)
    except Exception:  # noqa: BLE001 - triage must not depend on the newsletter module loading
        return frozenset()


def run_triage(
    sr,
    config,
    settings: dict,
    db_conn,
    *,
    dry_run: bool = False,
    verbose: bool = True,
    since_days: int | None = None,
    limit: int | None = None,
) -> TriageResult:
    """Triage Inbox mail not yet judged by any intake pass; returns what was decided."""
    from intake.db import is_email_processed, record_email_receipt  # noqa: PLC0415
    from intake.email_filing import move_message_without_marking_read  # noqa: PLC0415
    from intake.email_provider import select_mailbox  # noqa: PLC0415

    cfg = (settings.get("email") or {}).get("triage") or {}
    since_days = int(since_days if since_days is not None else cfg.get("since_days", 30))
    limit = int(limit if limit is not None else cfg.get("limit", 200))
    labels = {**DEFAULT_LABELS, **{k: v for k, v in (cfg.get("labels") or {}).items() if v}}
    own = {str(a).lower() for a in cfg.get("own_addresses", [])}
    username = str(getattr(config.imap, "username", "") or "").lower()
    if username:
        own.add(username)
    managed = _managed_senders()
    always_skip = frozenset(str(s).lower() for s in cfg.get("skip_senders", []))
    self_re = str(cfg.get("self_subject_regex") or DEFAULT_SELF_SUBJECT_RE)
    notice_res = (*DEFAULT_NOTICE_SUBJECT_RES, *map(str, cfg.get("notice_subject_regexes", [])))

    result = TriageResult()
    mailbox = config.imap.mailboxes[0] if config.imap.mailboxes else "INBOX"
    with sr.open_imap_connection(config) as conn:
        status, _ = select_mailbox(conn, mailbox, readonly=dry_run)
        if status != "OK":
            return result
        terms = sr.build_search_terms(since_days=since_days)
        status, data = conn.uid("search", config.imap.search_charset, *terms)
        if status != "OK":
            return result
        uids = sorted(sr.parse_int_uid_list(data), reverse=True)
        needs_expunge = False
        for uid in uids:
            if result.examined >= limit:
                break
            if is_email_processed(db_conn, uid, mailbox):
                continue
            status, fetched = conn.uid(
                "fetch", str(uid), f"(BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
            )
            payload = sr.extract_fetch_payload(fetched)
            if status != "OK" or not payload:
                continue
            msg = message_from_bytes(payload, policy=default_policy)
            decision = classify_headers(
                msg,
                own_addresses=frozenset(own),
                managed_senders=managed,
                skip_senders=always_skip,
                labels=labels,
                self_subject_re=self_re,
                notice_subject_res=notice_res,
            )
            sender = parseaddr(str(msg.get("From", "")))[1]
            subject = sr.decode_mime_header(msg.get("Subject"))
            result.examined += 1
            result.by_kind[decision.kind] += 1
            result.decisions.append((uid, sender, subject, decision))
            if decision.kind == "keep":
                if (mailbox, uid) in _KEPT_LOGGED and not dry_run:
                    continue
                _KEPT_LOGGED.add((mailbox, uid))
            target = f" -> {decision.label}" if decision.label else ""
            line = (
                f"triage {decision.kind:<6}{target}: uid={uid} sender={sender} "
                f"subject={subject[:70]!r} ({decision.reason})"
            )
            # Printed lines are mirrored into the file log by the stdout tee, so only log
            # directly when nothing is printed; otherwise every decision appears twice.
            if verbose:
                print(f"  {line}")
            else:
                log.info(line)
            if dry_run or decision.kind == "keep":
                continue
            if decision.label:
                moved = move_message_without_marking_read(
                    sr, conn, uid, source_mailbox=mailbox, processed_label=decision.label
                )
                needs_expunge |= moved
                result.moved += int(moved)
            record_email_receipt(
                db_conn,
                uid=uid,
                mailbox=mailbox,
                message_id=str(msg.get("Message-ID", "") or ""),
                subject=subject,
                sender=sender,
                received_at="",
                attachment_count=0,
                status=f"triage-{decision.kind}",
            )
        if needs_expunge:
            conn.expunge()
    return result
