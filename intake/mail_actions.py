"""Mailbox-changing actions for the review UI: every one is a reversible Gmail label/flag change.

Nothing here deletes, trashes, sends or unsubscribes.  Each action is a ``STORE`` of Gmail
labels (``X-GM-LABELS``) or the ``\\Seen`` flag on the message's ``[Gmail]/All Mail`` UID, so it
is atomic, needs no copy-then-delete, and has an obvious inverse:

=====================  ======================================  =========================
action                 effect                                  undo
=====================  ======================================  =========================
return to Inbox        add ``\\Inbox``, drop its queue label    file under a queue again
file under a queue     add that queue's label, drop ``\\Inbox``  return to Inbox
mark read / unread     set / clear ``\\Seen``                   the other one
=====================  ======================================  =========================

Safety rails: label targets come only from a fixed whitelist (a form field can never invent or
delete an arbitrary label); before changing anything the UID is re-checked against the message's
``X-GM-MSGID`` so a UID that has moved on can never change the wrong message; actions are
serialised; each one is logged.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from intake import mailindex

log = logging.getLogger("intake.mailactions")

INBOX = "\\Inbox"
# queue -> the Gmail label a message is filed under (the only labels the UI may add).
FILE_TARGETS = {
    "promos": "intake/promos",
    "notices": "intake/notices",
    "notes": "intake/notes",
    "notify": "intake/notify",
    "receipts": "intake/processed",
}
# Every queue label the UI may remove when it moves a message (never any other label).
REMOVABLE_LABELS = frozenset(
    {label for _, labels in mailindex.QUEUE_LABELS for label in labels} | set(FILE_TARGETS.values())
)
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9 _./-]{1,80}$")


class MailActionError(Exception):
    """An action could not be applied; the message is safe to show to the user."""


def _atom(label: str) -> str:
    """Render a label for ``STORE``: system labels are atoms, user labels are quoted."""
    if label.startswith("\\"):
        return label
    if not _SAFE_LABEL.match(label):
        raise MailActionError("Refusing an unexpected label name.")
    return '"' + label.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MailActions:
    """Applies review-page actions through the same IMAP session settings intake already uses."""

    def __init__(self, settings: dict, index_db: Path | str, *, load_relay=None) -> None:
        self._settings = settings
        self._index_db = Path(index_db)
        self._load_relay = load_relay
        self._lock = threading.Lock()

    # -- public actions ------------------------------------------------------------------

    def return_to_inbox(self, gm_msgid: str) -> str:
        row = self._row(gm_msgid)
        labels = json.loads(row["labels"] or "[]")
        drop = [x for x in labels if x in REMOVABLE_LABELS]
        self._apply(row, add=[INBOX], remove=drop)
        return "inbox"

    def file_under(self, gm_msgid: str, queue: str) -> str:
        target = FILE_TARGETS.get(queue)
        if target is None:
            raise MailActionError("That is not a queue messages can be filed under.")
        row = self._row(gm_msgid)
        labels = json.loads(row["labels"] or "[]")
        drop = [x for x in labels if x in REMOVABLE_LABELS and x != target]
        self._apply(row, add=[target], remove=[INBOX, *drop])
        return "filed"

    def set_read(self, gm_msgid: str, read: bool) -> str:
        self._apply(self._row(gm_msgid), flag=("+FLAGS" if read else "-FLAGS", "(\\Seen)"))
        return "read" if read else "unread"

    # -- internals -----------------------------------------------------------------------

    def _row(self, gm_msgid: str):
        conn = mailindex.connect(self._index_db)
        try:
            row = mailindex.get_message(conn, gm_msgid)
        finally:
            conn.close()
        if row is None or row["gone"]:
            raise MailActionError("That message is not in the index any more.")
        if not row["uid"]:
            raise MailActionError("The index is out of date for this message; run a mail sync.")
        return row

    def _relay(self):
        from intake.email_provider import load_shock_relay  # noqa: PLC0415

        path = str(self._settings.get("email", {}).get("shock_relay_config") or "").strip()
        if not path:
            raise MailActionError("The mail connection is not configured.")
        loader = self._load_relay or load_shock_relay
        sr = loader(path)
        return sr, sr.load_config(path)

    def _apply(self, row, *, add=(), remove=(), flag: tuple[str, str] | None = None) -> None:
        from intake.email_provider import select_mailbox  # noqa: PLC0415

        uid, gm_msgid = int(row["uid"]), str(row["gm_msgid"])
        with self._lock:
            sr, config = self._relay()
            try:
                with sr.open_imap_connection(config) as imap:
                    status, _ = select_mailbox(imap, mailindex.ALL_MAIL, readonly=False)
                    if status != "OK":
                        raise MailActionError("Could not open the mailbox for writing.")
                    self._verify(imap, uid, gm_msgid)
                    if add:
                        self._store(imap, uid, "+X-GM-LABELS", add)
                    if remove:
                        self._store(imap, uid, "-X-GM-LABELS", remove)
                    if flag:
                        status, _ = imap.uid("store", str(uid), flag[0], flag[1])
                        if status != "OK":
                            raise MailActionError("The mailbox refused the change.")
            except MailActionError:
                raise
            except Exception as exc:  # noqa: BLE001 - network/IMAP errors become a safe message
                log.error("mail action failed uid=%s gm_msgid=%s: %s", uid, gm_msgid, exc)
                raise MailActionError(
                    "The mailbox could not be reached; nothing was changed."
                ) from exc
            log.info(
                "mail action gm_msgid=%s uid=%s add=%s remove=%s flag=%s",
                gm_msgid,
                uid,
                list(add),
                list(remove),
                flag,
            )
            self._update_index(gm_msgid, add, remove, flag)

    @staticmethod
    def _verify(imap, uid: int, gm_msgid: str) -> None:
        status, data = imap.uid("fetch", str(uid), "(X-GM-MSGID)")
        line = b" ".join(x if isinstance(x, bytes) else x[0] for x in (data or []) if x)
        found = re.search(rb"X-GM-MSGID (\d+)", line)
        if status != "OK" or not found or found.group(1).decode() != gm_msgid:
            raise MailActionError(
                "That message has changed on the server. Run a mail sync and try again."
            )

    @staticmethod
    def _store(imap, uid: int, command: str, labels) -> None:
        wire = "(" + " ".join(_atom(label) for label in labels) + ")"
        status, _ = imap.uid("store", str(uid), command, wire)
        if status != "OK":
            raise MailActionError("The mailbox refused the change.")

    def _update_index(self, gm_msgid: str, add, remove, flag) -> None:
        conn = mailindex.connect(self._index_db)
        try:
            mailindex.apply_local(
                conn,
                gm_msgid,
                add=list(add),
                remove=list(remove),
                unread=None if flag is None else (flag[0] == "-FLAGS"),
            )
            conn.commit()
        finally:
            conn.close()
