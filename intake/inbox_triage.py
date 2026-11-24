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

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default as default_policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

log = logging.getLogger("intake.triage")

DEFAULT_LABELS = {
    "notify": "intake/notify",
    "promo": "intake/promos",
    "notice": "intake/notices",
    "note": "intake/notes",
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
    r"delivered|shipped)(?:s|es)?\b",
    re.IGNORECASE,
)
# Words that keep *non-bulk* mail (a trip, a ticket, an alert) but that marketing reuses
# ("extending your trip", "no lift ticket", "important updates", "promo code"), so bulk headers
# override them and the mail is filed as a promo.
SOFT_KEEP_SUBJECT_RE = re.compile(
    r"\b(?:ticket|trip|ride|code|alert|important|expir(?:e|es|ing|ed)|usage|quota|limit|minutes)(?:s|es)?\b",
    re.IGNORECASE,
)
# Informational mail whose subject trips the broad keep keywords above (usage, minutes, limit).
# Matched first, so it is filed as a notice.  Extend with [email.triage] notice_subject_regexes.
DEFAULT_NOTICE_SUBJECT_RES = (
    # GitHub Actions quota notices: CI falls back to the cloud and local runners by itself.
    r"\bused \d+% of the Actions minutes\b",
)
# Never triaged: GitHub's own mail is handled by the GitHub passes (Codecov, CI runs) or is a
# comment on one of your pull requests, which needs you.  GitHub sends bulk-style headers, so
# without this it would be filed as a promotion.
DEFAULT_SKIP_SENDERS = frozenset({"notifications@github.com"})
# Plainly automated senders.  Role accounts (support, team, info, hello, help) are deliberately
# NOT here: they are answered by people, and genuine marketing from them already carries bulk
# headers, which are handled separately.
AUTOMATED_SENDER_RE = re.compile(
    r"(?:^|[-_.+])(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply)(?:$|[-_.+\d])|"
    r"^(?:notifications?|notify|mailer|alerts?|updates?|news|newsletter|bounce|auto)"
    r"(?:$|[-_.+\d])",
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


# --- who may be second-guessed by the local model -----------------------------------------
# The model only ever gets to look at mail that is *itself* automated.  On a year of real mail an
# earlier version called a banker's personal replies "promo", a relocation agent's emails
# "automated" and a support thread a "notice"; a person's reply must never be filed by a model.
_REPLY_SUBJECT_RE = re.compile(r"^\s*(?:re|aw|sv)\s*:", re.IGNORECASE)
_FORWARD_SUBJECT_RE = re.compile(r"^\s*(?:fw|fwd|wg|tr)\s*:", re.IGNORECASE)
_ROBOT_LOCAL_RE = re.compile(
    r"(?:^|[-_.+])(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|noreply)(?:$|[-_.+\d])|"
    r"(?:^|[-_.+])reply\+|[-_.]reply$|^(?:notifications?|notify|mailer|bounces?|alerts?|"
    r"newsletter|news|updates?|digest|automated?)(?:$|[-_.+\d])",
    re.IGNORECASE,
)
# A marketing/notification subdomain (o.delta.com, email.example.com, news.example.com ...).
_BULK_SUBDOMAIN_RE = re.compile(
    r"^(?:o|e|em|email|emails|mail|mailer|news|newsletter|notify|notification|notifications|"
    r"promo|promomail|communication|communications|campaign|campaigns|offer|offers|"
    r"marketing|msemail|click|link|go|t|m|info)\.",
    re.IGNORECASE,
)
# Role accounts are answered by people (support, claims, billing, service desks).
_ROLE_LOCAL_RE = re.compile(
    r"support|help|service|billing|claims?|accounts?|admin|legal|contact|sales|care|"
    r"customer|office|team|hr|careers?|recruit",
    re.IGNORECASE,
)


def is_reply(headers) -> bool:
    """A reply: part of a conversation (Re:, In-Reply-To, References)."""
    return bool(
        headers.get("In-Reply-To")
        or headers.get("References")
        or _REPLY_SUBJECT_RE.match(str(headers.get("Subject", "")))
    )


def is_threaded(headers) -> bool:
    """A reply or a forward: never auto-filed by a model."""
    return is_reply(headers) or bool(_FORWARD_SUBJECT_RE.match(str(headers.get("Subject", ""))))


def is_machine_sender(headers) -> bool:
    """True only for senders that are plainly automated, never for a named person or a role."""
    sender = parseaddr(str(headers.get("From", "")))[1].lower()
    local, _, domain = sender.partition("@")
    if not local or not domain or _ROLE_LOCAL_RE.search(local):
        return False
    return bool(_ROBOT_LOCAL_RE.search(local) or _BULK_SUBDOMAIN_RE.match(domain))


def model_may_review(headers) -> bool:
    """Whether the local model is allowed to look at this message at all."""
    return not is_threaded(headers) and is_machine_sender(headers)


def _only_to_yourself(headers, own_addresses: frozenset[str]) -> bool:
    """A real note to self: every recipient (To and Cc) is one of your own addresses."""
    recipients = {
        addr.lower()
        for _, addr in getaddresses([*headers.get_all("To", []), *headers.get_all("Cc", [])])
        if addr
    }
    return bool(recipients) and recipients <= own_addresses


REASON_PERSON = "looks like a person"
REASON_SOFT_SUBJECT = "subject looks transactional or like an alert"
REASON_SELF_NOTE = "a note from yourself"
# Keeps the header rules could not settle either way.  The local model may take a second look at
# these, and only these; a security/money subject or a managed sender is never second-guessed.
REFINABLE_REASONS = frozenset({REASON_PERSON, REASON_SOFT_SUBJECT})


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
    file_notes: bool = True,
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
        if file_notes and not is_reply(headers) and _only_to_yourself(headers, own_addresses):
            return Decision("note", "a note to yourself", labels["note"])
        return Decision("keep", REASON_SELF_NOTE)
    if any(re.search(pattern, subject, re.IGNORECASE) for pattern in notice_subject_res):
        return Decision("notice", "informational subject", labels["notice"])
    if HARD_KEEP_SUBJECT_RE.search(subject):
        return Decision("keep", "subject is about money, security or a deadline")
    if is_bulk(headers):
        return Decision("promo", "bulk mail headers", labels["promo"])
    if SOFT_KEEP_SUBJECT_RE.search(subject):
        return Decision("keep", REASON_SOFT_SUBJECT)
    if str(headers.get("Auto-Submitted", "no")).strip().lower() not in {"", "no"}:
        return Decision("notice", "auto-submitted mail", labels["notice"])
    if AUTOMATED_SENDER_RE.search(local):
        return Decision("notice", "automated sender, no bulk headers", labels["notice"])
    return Decision("keep", REASON_PERSON)


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


def _load_verdicts(path: Path | None) -> dict:
    """Messages the model already judged to stay put, so it is asked once per message."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_verdicts(path: Path | None, verdicts: dict) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(list(verdicts.items())[-1000:])), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save triage verdicts: %s", exc)


def _ask_model(sr, conn, uid: int, settings: dict, sender: str, subject: str):
    """Fetch the message body (read-only) and get the local model's classification."""
    from intake.email_text import html_to_text  # noqa: PLC0415
    from intake.promo_digest import clean_body  # noqa: PLC0415
    from intake.triage_llm import judge  # noqa: PLC0415

    status, fetched = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
    payload = sr.extract_fetch_payload(fetched)
    if status != "OK" or not payload:
        return None
    msg = message_from_bytes(payload, policy=default_policy)
    plain, html = sr.extract_message_bodies(msg)
    body = "\n".join(clean_body(plain or (html_to_text(html) if html else "")))
    return judge(settings, sender=sender, subject=subject, body=body)


def _apply_verdict(decision: Decision, verdict, labels: dict[str, str]) -> Decision:
    """The model may only file clear automated mail; anything else stays in the Inbox."""
    if verdict.confident and verdict.kind == "marketing":
        return Decision("promo", f"local model: {verdict.summary}", labels["promo"])
    if verdict.confident and verdict.kind == "notice":
        return Decision("notice", f"local model: {verdict.summary}", labels["notice"])
    suffix = f"; model: {verdict.kind}{'' if verdict.confident else ' (unsure)'}"
    return Decision("keep", decision.reason + suffix)


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
    state_path: Path | None = None,
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
    always_skip = DEFAULT_SKIP_SENDERS | {str(s).lower() for s in cfg.get("skip_senders", [])}
    self_re = str(cfg.get("self_subject_regex") or DEFAULT_SELF_SUBJECT_RE)
    notice_res = (*DEFAULT_NOTICE_SUBJECT_RES, *map(str, cfg.get("notice_subject_regexes", [])))
    file_notes = bool(cfg.get("file_notes", True))
    llm_ok = [bool(cfg.get("llm", False))]
    verdicts = _load_verdicts(state_path)
    verdicts_dirty = False

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
                file_notes=file_notes,
            )
            sender = parseaddr(str(msg.get("From", "")))[1]
            subject = sr.decode_mime_header(msg.get("Subject"))
            key = f"{mailbox}:{uid}"
            if (
                decision.kind == "keep"
                and decision.reason in REFINABLE_REASONS
                and llm_ok[0]
                and key not in verdicts
                and model_may_review(msg)
            ):
                verdict = _ask_model(sr, conn, uid, settings, sender, subject)
                if verdict is None:
                    llm_ok[0] = False  # model down or unclear: ask again next poll, once
                else:
                    verdicts[key] = {"kind": verdict.kind, "confident": verdict.confident}
                    verdicts_dirty = True
                    decision = _apply_verdict(decision, verdict, labels)
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
    if verdicts_dirty and not dry_run:
        _save_verdicts(state_path, verdicts)
    return result
