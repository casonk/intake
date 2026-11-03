"""Which email addresses intake may exchange mail with.

Intake reads replies to its own notifications and sends notifications, file links and digests.
Both directions are limited to the addresses listed in ``[email].allowed_addresses``: a reply from
anyone else is never acted on, and nothing is ever sent to anyone else. The rule fails closed:
with no list configured, no address is allowed (and a warning says so), because an unset list
must never mean "everyone".

The check compares the bare, lower-cased address (``Name <a@b.example>`` and ``a@b.example`` are
the same). It looks at the ``From`` header only, so it keeps strangers out but does not prove who
sent a message; DKIM/SPF verification is a separate, stronger step.
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
