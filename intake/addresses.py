"""Which email addresses intake may exchange mail with.

Intake reads replies to its own notifications and sends notifications, file links and digests.
Both directions are limited to the addresses listed in ``[email].allowed_addresses``: a reply from
anyone else is never acted on, and nothing is ever sent to anyone else. The rule fails closed:
with no list configured, no address is allowed (and a warning says so), because an unset list
must never mean "everyone".

The check compares the bare, lower-cased address (``Name <a@b.example>`` and ``a@b.example`` are
the same). The ``From`` header alone only keeps strangers out; it does not prove who sent a
message. :func:`sender_is_authenticated` is the stronger step: it requires the receiving server's
own DMARC pass for the sender's domain.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from email.utils import parseaddr

_WARNED: set[tuple[str, str]] = set()
_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+")


def normalize_address(value: object) -> str:
    """The bare lower-case address in *value* (``Name <A@B.example>`` -> ``a@b.example``).

    Anything that is not shaped like ``local@domain`` (a bare word, ``@domain``) is ``""``.
    """
    address = parseaddr(str(value or ""))[1].strip().lower()
    return address if _ADDRESS.fullmatch(address) else ""


def allowed_addresses(settings: dict) -> frozenset[str]:
    """The addresses intake may exchange mail with; empty when none are configured."""
    email_cfg = settings.get("email") if isinstance(settings, dict) else None
    configured = email_cfg.get("allowed_addresses") if isinstance(email_cfg, dict) else None
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, list):
        return frozenset()
    return frozenset(filter(None, (normalize_address(item) for item in configured)))


def is_allowed_address(settings: dict, value: object) -> bool:
    address = normalize_address(value)
    return bool(address) and address in allowed_addresses(settings)


def _refusal(settings: dict, address: str, purpose: str) -> str:
    if not allowed_addresses(settings):
        return f"[email].allowed_addresses is not set, so no {purpose} is sent"
    return f"{address or 'an empty address'} is not in [email].allowed_addresses, so no {purpose} is sent"


def filter_allowed(settings: dict, addresses: Iterable[str], *, purpose: str) -> list[str]:
    """Keep only the allowed addresses, warning once (on stderr) about each one dropped."""
    kept: list[str] = []
    for item in addresses:
        if is_allowed_address(settings, item):
            kept.append(item)
            continue
        address = normalize_address(item)
        if (address, purpose) not in _WARNED:
            _WARNED.add((address, purpose))
            print(f"intake: {_refusal(settings, address, purpose)}", file=sys.stderr)
    return kept


def require_allowed_address(settings: dict, value: object, *, purpose: str) -> str:
    """Return the bare address, or raise ``RuntimeError`` if intake may not mail it."""
    address = normalize_address(value)
    if not is_allowed_address(settings, address):
        raise RuntimeError(_refusal(settings, address, purpose))
    return address


DEFAULT_AUTHENTICATION_SERVER = "mx.google.com"


def _authentication_enabled(settings: dict) -> bool:
    email_cfg = settings.get("email") if isinstance(settings, dict) else None
    return not (
        isinstance(email_cfg, dict) and email_cfg.get("require_authenticated_replies") is False
    )


def sender_is_authenticated(settings: dict, msg, sender: object) -> bool:
    """True when the receiving server vouches that *sender* really sent *msg*.

    Only the topmost ``Authentication-Results`` header counts: the receiving server prepends its
    own verdict, so any further header was supplied by the sender and may be forged. That header
    must come from the configured server (``[email].authentication_server``, default
    ``mx.google.com``) and report ``dmarc=pass`` for the domain of the sender's address. Fails
    closed: a missing header, another server, a failed check or a different domain is not
    authenticated. ``[email].require_authenticated_replies = false`` turns the check off.
    """
    if not _authentication_enabled(settings):
        return True
    address = normalize_address(sender)
    if not address:
        return False
    domain = address.rsplit("@", 1)[1]
    values = msg.get_all("Authentication-Results", [])
    if not values:
        return False
    email_cfg = settings.get("email") if isinstance(settings, dict) else {}
    configured = str((email_cfg or {}).get("authentication_server") or "").strip().lower()
    server = configured or DEFAULT_AUTHENTICATION_SERVER
    normalized = " ".join(str(values[0]).lower().split())
    if not normalized.startswith(f"{server};"):
        return False
    if not re.search(r"(?:^|[\s;])dmarc=pass(?=[\s;(]|$)", normalized):
        return False
    return (
        re.search(rf"(?:^|[\s;(])header\.from={re.escape(domain)}(?=[\s;)]|$)", normalized)
        is not None
    )
