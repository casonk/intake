from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from intake import mailindex as mi
from intake.mail_actions import FILE_TARGETS, MailActionError, MailActions


class FakeImap:
    """Gmail-ish IMAP: records every command; X-GM-MSGID per UID for the identity check."""

    def __init__(self, msgids, *, store_status="OK", fail_open=False):
        self.msgids = msgids  # uid -> X-GM-MSGID
        self.store_status = store_status
        self.fail_open = fail_open
        self.commands: list[tuple] = []

    def __enter__(self):
        if self.fail_open:
            raise OSError("network down")
        return self

    def __exit__(self, *a):
        return False

    def select(self, mailbox, readonly=False):
        self.commands.append(("select", mailbox, readonly))
        return ("OK", [b"1"])

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "fetch":
            return ("OK", [f"1 (X-GM-MSGID {self.msgids[int(args[0])]} UID {args[0]})".encode()])
        if command == "store":
            return (self.store_status, [b"ok"])
        raise AssertionError(f"unexpected command {command}")

    def stores(self):
        return [c for c in self.commands if c[0] == "store"]


@pytest.fixture()
def index(tmp_path):
    path = tmp_path / "idx.db"
    conn = mi.connect(path)

    def put(gm, uid, labels, unread=False):
        mi.upsert(conn, {"gm_msgid": gm, "uid": uid, "sender": "a@b.example", "subject": f"S{gm}",
                         "labels": labels, "unread": unread})  # fmt: skip

    put("1001", 11, ["\\Inbox"], unread=True)
    put("1002", 12, ["intake/promos", "Career/Alerts"])
    put("1003", 13, ["intake/notices"])
    conn.commit()
    conn.close()
    return path


def make(index, fake):
    sr = SimpleNamespace(
        load_config=lambda _p: SimpleNamespace(),
        open_imap_connection=lambda _c: fake,
    )
    settings = {"email": {"shock_relay_config": "/x.yaml"}}
    return MailActions(settings, index, load_relay=lambda _p: sr)


def labels_of(index, gm):
    conn = mi.connect(index)
    row = mi.get_message(conn, gm)
    conn.close()
    return json.loads(row["labels"]), row


def test_return_to_inbox_adds_inbox_and_drops_only_queue_labels(index):
    fake = FakeImap({12: "1002"})
    assert make(index, fake).return_to_inbox("1002") == "inbox"
    assert fake.stores() == [
        ("store", "12", "+X-GM-LABELS", "(\\Inbox)"),
        ("store", "12", "-X-GM-LABELS", '("intake/promos")'),
    ]
    labels, row = labels_of(index, "1002")
    assert "Career/Alerts" in labels, "labels outside the queue set are never touched"
    assert "\\Inbox" in labels and "intake/promos" not in labels and row["in_inbox"] == 1


def test_file_under_adds_target_and_removes_inbox(index):
    fake = FakeImap({11: "1001"})
    assert make(index, fake).file_under("1001", "promos") == "filed"
    assert fake.stores() == [
        ("store", "11", "+X-GM-LABELS", '("intake/promos")'),
        ("store", "11", "-X-GM-LABELS", "(\\Inbox)"),
    ]
    labels, row = labels_of(index, "1001")
    assert labels == ["intake/promos"] and row["in_inbox"] == 0


def test_file_under_moves_between_queues_without_stacking_labels(index):
    fake = FakeImap({13: "1003"})
    make(index, fake).file_under("1003", "promos")
    assert ("store", "13", "-X-GM-LABELS", '(\\Inbox "intake/notices")') in fake.stores()
    assert labels_of(index, "1003")[0] == ["intake/promos"]


@pytest.mark.parametrize(
    "queue", ["", "ci", "archive", "needs", 'promos"); evil', "../x", "INBOX", "Career/Alerts"]
)
def test_only_whitelisted_queues_are_file_targets(index, queue):
    fake = FakeImap({11: "1001"})
    with pytest.raises(MailActionError):
        make(index, fake).file_under("1001", queue)
    assert fake.commands == [], "a refused target must not even open the mailbox"


def test_whitelist_is_exactly_the_intake_labels():
    assert set(FILE_TARGETS.values()) == {
        "intake/promos", "intake/notices", "intake/notes", "intake/notify", "intake/processed",
    }  # fmt: skip


def test_mark_read_and_unread_toggle_the_seen_flag_and_the_index(index):
    fake = FakeImap({11: "1001"})
    actions = make(index, fake)
    assert actions.set_read("1001", True) == "read"
    assert fake.stores()[-1] == ("store", "11", "+FLAGS", "(\\Seen)")
    assert labels_of(index, "1001")[1]["unread"] == 0
    assert actions.set_read("1001", False) == "unread"
    assert fake.stores()[-1] == ("store", "11", "-FLAGS", "(\\Seen)")
    assert labels_of(index, "1001")[1]["unread"] == 1


def test_a_uid_that_now_points_at_another_message_changes_nothing(index):
    fake = FakeImap({12: "9999"})  # the UID was reused by a different message
    with pytest.raises(MailActionError, match="changed on the server"):
        make(index, fake).return_to_inbox("1002")
    assert fake.stores() == []
    assert "intake/promos" in labels_of(index, "1002")[0], "the index is left alone too"


def test_a_failing_mailbox_gives_a_safe_message_and_leaves_the_index_alone(index):
    with pytest.raises(MailActionError, match="could not be reached"):
        make(index, FakeImap({12: "1002"}, fail_open=True)).return_to_inbox("1002")
    with pytest.raises(MailActionError, match="refused"):
        make(index, FakeImap({12: "1002"}, store_status="NO")).return_to_inbox("1002")
    assert "intake/promos" in labels_of(index, "1002")[0]


def test_unknown_or_gone_messages_are_refused(index):
    with pytest.raises(MailActionError):
        make(index, FakeImap({})).return_to_inbox("424242")
    conn = mi.connect(index)
    conn.execute("UPDATE messages SET gone=1 WHERE gm_msgid='1003'")
    conn.commit()
    conn.close()
    with pytest.raises(MailActionError):
        make(index, FakeImap({})).return_to_inbox("1003")


def test_nothing_destructive_is_ever_issued(index):
    fake = FakeImap({11: "1001", 12: "1002", 13: "1003"})
    a = make(index, fake)
    a.file_under("1001", "notes")
    a.return_to_inbox("1001")
    a.set_read("1001", True)
    a.set_read("1001", False)
    a.return_to_inbox("1002")
    wire = " ".join(str(c).lower() for c in fake.commands)
    for forbidden in ("\\deleted", "expunge", "copy", "trash", "spam", "delete"):
        assert forbidden not in wire, forbidden
    assert all(c[2] is False for c in fake.commands if c[0] == "select"), "opened for writing"
    assert all(c[1] == mi.ALL_MAIL for c in fake.commands if c[0] == "select")
