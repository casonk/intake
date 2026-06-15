"""IMAP/shock-relay provider helpers for email ingestion."""

from __future__ import annotations

import sys
from pathlib import Path


def load_shock_relay(config_path: str):
    """Import shock-relay common module by adding its directory to sys.path."""
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


def mailbox_select_candidates(mailbox: str) -> list[str]:
    """Return raw and quoted mailbox names for IMAP select/examine calls."""
    cleaned = str(mailbox or "").strip()
    if not cleaned:
        return []
    candidates = [cleaned]
    if " " in cleaned and not cleaned.startswith('"'):
        candidates.append(f'"{cleaned}"')
    return candidates


def select_mailbox(conn, mailbox: str, *, readonly: bool) -> tuple[str, object]:
    """Select *mailbox*, retrying with IMAP-safe quoting when needed."""
    last_status = "NO"
    last_data = []
    last_exc: Exception | None = None
    for candidate in mailbox_select_candidates(mailbox):
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


def fetch_message_by_uid(conn, mailbox: str, uid: int, *, readonly: bool) -> object:
    """Select *mailbox* and fetch the RFC822 payload for *uid*."""
    status, _ = select_mailbox(conn, mailbox, readonly=readonly)
    if status != "OK":
        raise RuntimeError(f"Cannot select mailbox {mailbox!r}")

    fetch_status, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
    if fetch_status != "OK":
        raise RuntimeError(f"Cannot fetch uid {uid} from {mailbox!r}")
    return msg_data


def find_message_in_mailboxes(
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
            status, _ = select_mailbox(conn, mailbox, readonly=True)
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
            msg_data = fetch_message_by_uid(conn, mailbox, uid, readonly=True)
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
