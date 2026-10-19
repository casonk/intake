"""Capture actionable employer mail in the private career workspace."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
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


def _employer_for_sender(sender: str, employers: dict[str, str]) -> str:
    address = parseaddr(sender)[1].lower()
    domain = address.rpartition("@")[2]
    for suffix, organization in employers.items():
        suffix = suffix.lower().lstrip("@.")
        if domain == suffix or domain.endswith("." + suffix):
            return str(organization)
    return ""


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
    lines = [
        f"# {message.subject}",
        "",
        "- [ ] Review and complete the requested career follow-up",
        f"- Organization: {message.organization}",
        f"- Received: {message.received.isoformat()}",
        f"- From: {message.sender}",
        f"- Message-ID: {message.message_id}",
    ]
    if related:
        lines += ["- Related applications: " + ", ".join(related)]
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


def fetch_actions(settings: dict, *, dry_run: bool = False, verbose: bool = True) -> list[Path]:
    """Read matching employer mail without changing Inbox flags or labels."""
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
    with sr.open_imap_connection(config) as conn:
        status, _ = select_mailbox(conn, mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Cannot select mailbox {mailbox!r}")
        uids: set[int] = set()
        for domain in employers:
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
            organization = _employer_for_sender(sender, employers)
            if not organization:
                continue
            subject = sr.decode_mime_header(email.get("Subject"))
            plain, html = sr.extract_message_bodies(email)
            body = plain or html_to_text(html)
            if not _is_actionable(subject, body):
                continue
            message_id = str(email.get("Message-ID") or f"{mailbox}:{uid}")
            path = _record_path(root, message_id)
            if path.exists() and "- Action link:" in path.read_text(encoding="utf-8"):
                continue
            try:
                received = parsedate_to_datetime(str(email.get("Date"))).date()
            except (TypeError, ValueError):
                received = date.today()
            message = CareerMessage(
                message_id, subject, sender, received, body, html, organization, uid
            )
            if verbose:
                print(f"  Career action: {organization}: {subject} -> {path}")
            if not dry_run and (created := save_action(root, message)):
                saved.append(created)
    return saved
