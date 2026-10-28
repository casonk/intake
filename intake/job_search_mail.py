"""Capture employer and application-portal mail in the private career workspace.

Each message becomes one gitignored record in ``private/intake-email/`` with a
``Kind:`` line (confirmation, request, screening, interview, offer, rejection),
following career's ``docs/intake-email-contract.md``. Verification codes that
portals email before accepting a submission (Greenhouse's "security code") are
written to ``private/verification-codes/`` instead, for career's filler, and are
never printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email import message_from_bytes
from email.policy import default as default_policy
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from intake.email_provider import load_shock_relay, select_mailbox
from intake.email_text import html_to_text

_ACTION = re.compile(
    r"\b(?:update your profile|complete (?:a |the |your )?(?:short )?(?:form|profile)|"
    r"additional (?:information|profile)|schedule (?:an? |your )?interview|"
    r"select (?:a |your )?(?:time|slot)|respond to (?:this|our) (?:email|request))\b",
    re.I,
)
_CAREER = re.compile(
    r"\b(?:career|talent community|application|applicant|candidate|interview|recruiter)\b", re.I
)
_REQUISITION = re.compile(r"\bJR-\d{5,}\b", re.I)

# Application portals send on behalf of many employers; the employer is in the subject.
PORTAL_SENDERS = (
    "greenhouse-mail.io",
    "greenhouse.io",
    "myworkday.com",
    "hire.lever.co",
    "ashbyhq.com",
)
_SUBJECT_EMPLOYER = re.compile(
    r"\b(?:application to|applying to|interest in .+? at|position at|role at|at|with|to)\s+"
    r"(?P<org>[A-Z0-9][\w&.,'’ -]{0,60}?)\s*(?:[!.:|]|$)"
)
_CODE_SUBJECT = re.compile(r"\b(?:security|verification|one-time|confirmation) code\b", re.I)
_CODE_VALUE = re.compile(r"(?:code|application)\b[^:\n]{0,80}:\s*([A-Za-z0-9]{6,12})\b")
# Order matters: the first kind that matches wins.
_KINDS = (
    (
        "rejection",
        re.compile(
            r"move forward with other candidates|not (?:to )?(?:move|moving) forward|decided to (?:pursue|proceed with) other|"
            r"no longer (?:under consideration|being considered)|regret to inform|will not be (?:moving|proceeding)|"
            r"position has been filled|unable to offer you",
            re.I,
        ),
    ),
    ("offer", re.compile(r"pleased to offer|offer letter|extend (?:you )?an offer", re.I)),
    (
        "interview",
        re.compile(
            r"schedule (?:an? |your )?interview|interview invitation|invite you to (?:an )?interview|"
            r"onsite interview|technical interview",
            re.I,
        ),
    ),
    (
        "screening",
        re.compile(
            r"phone screen|recruiter (?:call|screen)|introductory call|"
            r"(?:speak|chat|connect) with you (?:about|regarding)",
            re.I,
        ),
    ),
    ("request", _ACTION),
    (
        "confirmation",
        re.compile(
            r"thank you for (?:applying|your application|your interest)|application (?:was |has been )?"
            r"received|we(?:'ve| have) received your application|received your application",
            re.I,
        ),
    ),
)
# Kinds that ask something of the applicant: their record carries an open action box.
_OPEN_KINDS = {"request", "screening", "interview", "offer"}
# Gmail labels under [career].label_prefix (<prefix>/Rejection, ...).
LABEL_NAMES = {
    "confirmation": "Confirmation",
    "request": "Request",
    "screening": "Screening",
    "interview": "Interview",
    "offer": "Offer",
    "rejection": "Rejection",
    "code": "Security code",
}


def file_career_mail(sr, conn, items: list[tuple[int, str]], prefix: str = "Career") -> int:
    """Label each message; keep ones that need a reply in Inbox, starred; archive the rest.

    Read state is never changed. Returns how many messages were filed.
    """
    archived = False
    for uid, item in items:
        kind, _, done = item.partition(":")  # "request:done" once its action box is ticked
        label = f"{prefix}/{LABEL_NAMES.get(kind, kind.title())}"
        if kind in _OPEN_KINDS and not done:
            conn.create(label)
            conn.uid("copy", str(uid), label)
            conn.uid("store", str(uid), "+FLAGS", r"(\Flagged)")
        else:
            sr.move_message(conn, uid, label)  # copy to the label, then leave Inbox
            archived = True
    if archived:
        conn.expunge()
    return len(items)


class _ActionLinks(HTMLParser):
    def __init__(self, sender_domain: str) -> None:
        super().__init__()
        self.sender_domain = sender_domain
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._label = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._label = ""

    def handle_data(self, data: str) -> None:
        if self._href:
            self._label += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            parsed = urlsplit(self._href)
            host = (parsed.hostname or "").lower()
            trusted_http = parsed.scheme == "http" and (
                host == self.sender_domain or host.endswith("." + self.sender_domain)
            )
            if (parsed.scheme == "https" or trusted_http) and _ACTION.search(self._label):
                self.links.append((self._label.strip(), self._href))
            self._href = ""


@dataclass(frozen=True)
class CareerMessage:
    message_id: str
    subject: str
    sender: str
    received: date
    body: str
    html: str
    organization: str
    uid: int
    kind: str = "request"


def _is_portal(sender: str) -> bool:
    domain = parseaddr(sender)[1].lower().rpartition("@")[2]
    return any(domain == p or domain.endswith("." + p) for p in PORTAL_SENDERS)


def _employer_for_sender(sender: str, employers: dict[str, str], subject: str = "") -> str:
    """The employer a message is from: an exact address key, a domain key, or a portal's subject line."""
    address = parseaddr(sender)[1].lower()
    domain = address.rpartition("@")[2]
    for key, organization in employers.items():
        if "@" in key.strip("@") and key.lower() == address:
            return str(organization)
    for suffix, organization in employers.items():
        suffix = suffix.lower().lstrip("@.")
        if "@" in suffix:
            continue
        if domain == suffix or domain.endswith("." + suffix):
            return str(organization)
    if _is_portal(sender):
        found = _SUBJECT_EMPLOYER.search(subject)
        if found:
            name = found.group("org").strip(" .,-")
            known = {str(v).lower(): str(v) for v in employers.values()}
            return known.get(name.lower(), name)
    return ""


def classify(subject: str, body: str) -> str:
    """confirmation, request, screening, interview, offer, or rejection; "" for unrelated mail."""
    text = f"{subject}\n{body[:6000]}"
    for kind, pattern in _KINDS:
        if pattern.search(text):
            return kind
    return ""


def verification_code(subject: str, body: str) -> str:
    """The one-time code a portal emailed, when the message is one."""
    if not _CODE_SUBJECT.search(subject):
        return ""
    found = _CODE_VALUE.search(body)
    return found.group(1) if found else ""


CODE_FRESH_SECONDS = 1800  # portals' codes expire within minutes; older ones are never written


def save_code(
    root: Path,
    message_id: str,
    organization: str,
    code: str,
    received: datetime,
    sender: str,
    now: datetime | None = None,
) -> Path | None:
    """Write a fresh code for career's filler (owner-only, once per message); never print it."""
    now = now or datetime.now(UTC)
    if (now - received.astimezone(UTC)).total_seconds() > CODE_FRESH_SECONDS:
        return None
    token = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:20]
    path = root / "private" / "verification-codes" / f"{token}.json"
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = {
        "organization": organization,
        "code": code,
        "received": received.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "sender": parseaddr(sender)[1],
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream)
    return path


def _is_actionable(subject: str, body: str) -> bool:
    text = f"{subject}\n{body[:6000]}"
    return bool(_ACTION.search(text) and _CAREER.search(text))


def _related_applications(root: Path, organization: str, text: str) -> list[str]:
    matches = []
    requisitions = {value.upper() for value in _REQUISITION.findall(text)}
    for path in (root / "applications").glob("*/**/application.md"):
        content = path.read_text(encoding="utf-8")
        if not re.search(rf"(?im)^organization:\s*{re.escape(organization)}\s*$", content):
            continue
        requisition = _REQUISITION.search(content)
        if requisitions and requisition and requisition.group().upper() not in requisitions:
            continue
        matches.append(str(path.parent.relative_to(root)))
    return sorted(matches)


def _record_path(root: Path, message_id: str) -> Path:
    token = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:20]
    return root / "private" / "intake-email" / f"{token}.md"


def save_action(root: Path, message: CareerMessage) -> Path | None:
    """Write one gitignored action per Message-ID; leave application statuses alone."""
    path = _record_path(root, message.message_id)
    sender_domain = parseaddr(message.sender)[1].lower().rpartition("@")[2]
    links = _ActionLinks(sender_domain)
    links.feed(message.html)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if links.links and "- Action link:" not in existing:
            link_lines = "\n".join(
                f"- Action link: [{label}]({url})" for label, url in links.links[:3]
            )
            path.write_text(existing.rstrip() + "\n" + link_lines + "\n", encoding="utf-8")
        return None
    related = _related_applications(
        root, message.organization, message.subject + "\n" + message.body
    )
    action = (
        "- [ ] Review and complete the requested career follow-up"
        if message.kind in _OPEN_KINDS
        else f"- [x] Nothing to do ({message.kind})"
    )
    lines = [
        f"# {message.subject}",
        "",
        action,
        f"- Organization: {message.organization}",
        f"- Received: {message.received.isoformat()}",
        f"- From: {message.sender}",
        f"- Message-ID: {message.message_id}",
    ]
    if related:
        lines += ["- Related applications: " + ", ".join(related)]
    lines += [f"- Kind: {message.kind}"]
    if links.links:
        lines += [f"- Action link: [{label}]({url})" for label, url in links.links[:3]]
    lines += ["", "## Email excerpt", "", message.body[:5000].strip(), ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write("\n".join(lines))
    except FileExistsError:
        return None
    return path


def fetch_actions(
    settings: dict, *, dry_run: bool = False, verbose: bool = True, refile: bool = False
) -> list[Path]:
    """Save career mail to the career repo; with [career].file_mail, label it in Gmail too.

    Filing applies <label_prefix>/<Kind> labels, keeps messages that need a reply in Inbox
    (starred), archives the rest, and never changes read state. Only newly saved
    messages are filed, or every matching one with ``refile``.
    """
    career = settings.get("career", {})
    root_value = str(career.get("repo_root") or "").strip()
    employers = career.get("employers") or {}
    if not root_value or not employers:
        return []
    root = Path(root_value).expanduser()
    if not (root / "applications").is_dir() or not (root / "private").is_dir():
        raise RuntimeError(f"Career repo is missing applications/ or private/: {root}")
    config_path = str(settings.get("email", {}).get("shock_relay_config") or "").strip()
    if not config_path:
        raise RuntimeError("[email].shock_relay_config is required for career mail")
    sr = load_shock_relay(config_path)
    config = sr.load_config(config_path)
    sr.validate_imap(config)
    mailbox = config.imap.mailboxes[0] if config.imap.mailboxes else "INBOX"
    saved: list[Path] = []
    filing: list[tuple[int, str]] = []
    file_mail = bool(career.get("file_mail")) and not dry_run
    with sr.open_imap_connection(config) as conn:
        status, _ = select_mailbox(conn, mailbox, readonly=not file_mail)
        if status != "OK":
            raise RuntimeError(f"Cannot select mailbox {mailbox!r}")
        uids: set[int] = set()
        for domain in [*employers, *PORTAL_SENDERS]:
            terms = sr.build_search_terms(
                from_contains=domain, since_days=int(career.get("since_days", 30))
            )
            status, data = conn.uid("search", config.imap.search_charset, *terms)
            if status == "OK":
                uids.update(sr.parse_int_uid_list(data))
        for uid in sorted(uids, reverse=True)[: int(career.get("limit", 200))]:
            status, fetched = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
            payload = sr.extract_fetch_payload(fetched)
            if status != "OK" or not payload:
                continue
            email = message_from_bytes(payload, policy=default_policy)
            sender = sr.decode_mime_header(email.get("From"))
            subject = sr.decode_mime_header(email.get("Subject"))
            organization = _employer_for_sender(sender, employers, subject)
            if not organization:
                continue
            plain, html = sr.extract_message_bodies(email)
            body = plain or html_to_text(html)
            message_id = str(email.get("Message-ID") or f"{mailbox}:{uid}")
            try:
                stamp = parsedate_to_datetime(str(email.get("Date")))
            except (TypeError, ValueError):
                stamp = datetime.now(UTC)
            if code := verification_code(subject, body):
                fresh = (
                    datetime.now(UTC) - stamp.astimezone(UTC)
                ).total_seconds() <= CODE_FRESH_SECONDS
                if verbose and fresh:
                    print(f"  Career verification code: {organization} (saved for career fill)")
                created = None
                if not dry_run and (
                    created := save_code(root, message_id, organization, code, stamp, sender)
                ):
                    saved.append(created)
                if created or refile:
                    filing.append((uid, "code"))
                continue
            kind = classify(subject, body)
            if not kind or not (_CAREER.search(f"{subject} {body[:6000]}") or _is_portal(sender)):
                continue
            path = _record_path(root, message_id)
            if refile and path.exists():
                ticked = "- [x]" in path.read_text(encoding="utf-8").lower()
                filing.append((uid, f"{kind}:done" if ticked else kind))
            if path.exists() and "- Action link:" in path.read_text(encoding="utf-8"):
                continue
            received = stamp.date()
            message = CareerMessage(
                message_id, subject, sender, received, body, html, organization, uid, kind
            )
            if verbose:
                print(f"  Career {kind}: {organization}: {subject} -> {path}")
            if not dry_run and (created := save_action(root, message)):
                saved.append(created)
                filing.append((uid, kind))
        if file_mail and filing:
            filed = file_career_mail(
                sr, conn, list(dict.fromkeys(filing)), str(career.get("label_prefix") or "Career")
            )
            if verbose:
                print(
                    f"  Career mail filed: {filed} message(s) labelled under {career.get('label_prefix') or 'Career'}/"
                )
    return saved
