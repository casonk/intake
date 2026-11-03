"""IMAP helpers behind email ingestion: mailbox quoting, select/fetch, and message lookup.

The IMAP connection and the shock-relay module are replaced with fakes, and the shock-relay import
test builds a throwaway ``common`` module under ``tmp_path`` and restores ``sys.path`` and
``sys.modules`` afterwards. Nothing here opens a socket or reads a real mailbox.
"""

from __future__ import annotations

import sys

import pytest

from intake import email_provider as ep


class Conn:
    """A fake IMAP connection. ``selects`` maps a mailbox name to a status, data pair or an
    exception; ``uids`` maps a command key to a result."""

    def __init__(self, selects=None, uid_results=None):
        self.selects = selects or {}
        self.uid_results = uid_results or {}
        self.select_calls = []
        self.uid_calls = []

    def select(self, name, readonly=False):
        self.select_calls.append((name, readonly))
        result = self.selects.get(name, ("NO", [b"no such mailbox"]))
        if isinstance(result, BaseException):
            raise result
        return result

    def uid(self, command, *args):
        self.uid_calls.append((command, *args))
        key = (command, args[0]) if command == "fetch" else command
        result = self.uid_results.get(key, ("NO", []))
        if isinstance(result, BaseException):
            raise result
        return result


# --- shock-relay import -------------------------------------------------------------------


@pytest.fixture
def clean_import_state(monkeypatch):
    monkeypatch.delitem(sys.modules, "common", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    yield
    sys.modules.pop("common", None)


def test_load_shock_relay_imports_common_from_the_config_directory(tmp_path, clean_import_state):
    pkg = tmp_path / "shock-relay"
    pkg.mkdir()
    (pkg / "common.py").write_text("MARKER = 'from-config-dir'\n")
    sr = ep.load_shock_relay(str(pkg / "settings.toml"))
    assert sr.MARKER == "from-config-dir"
    assert sys.path[0] == str(pkg.resolve())


def test_load_shock_relay_does_not_add_the_directory_twice(tmp_path, clean_import_state):
    pkg = tmp_path / "sr"
    pkg.mkdir()
    (pkg / "common.py").write_text("X = 1\n")
    ep.load_shock_relay(str(pkg / "settings.toml"))
    sys.modules.pop("common")
    ep.load_shock_relay(str(pkg / "settings.toml"))
    assert sys.path.count(str(pkg.resolve())) == 1


def test_load_shock_relay_failure_names_the_directory_and_the_setting(
    tmp_path, monkeypatch, clean_import_state
):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sys, "path", [])  # nothing else on the path can supply a `common`
    with pytest.raises(RuntimeError) as info:
        ep.load_shock_relay(str(empty / "settings.toml"))
    message = str(info.value)
    assert str(empty.resolve()) in message
    assert "shock_relay_config" in message
    assert isinstance(info.value.__cause__, ImportError)


# --- mailbox names ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("INBOX", ["INBOX"]),
        ("  INBOX  ", ["INBOX"]),
        ("Sent Items", ["Sent Items", '"Sent Items"']),
        ('"Sent Items"', ['"Sent Items"']),
        ("", []),
        ("   ", []),
        (None, []),
    ],
)
def test_mailbox_candidates(given, expected):
    assert ep.mailbox_select_candidates(given) == expected


# --- select -------------------------------------------------------------------------------


def test_select_uses_the_first_candidate_that_works():
    conn = Conn({"INBOX": ("OK", [b"3"])})
    assert ep.select_mailbox(conn, "INBOX", readonly=True) == ("OK", [b"3"])
    assert conn.select_calls == [("INBOX", True)]


def test_select_passes_readonly_through():
    conn = Conn({"INBOX": ("OK", [])})
    ep.select_mailbox(conn, "INBOX", readonly=False)
    assert conn.select_calls == [("INBOX", False)]


def test_select_retries_with_quotes_when_the_raw_name_is_refused():
    conn = Conn({"Sent Items": ("NO", [b"bad"]), '"Sent Items"': ("OK", [b"1"])})
    assert ep.select_mailbox(conn, "Sent Items", readonly=True) == ("OK", [b"1"])
    assert [name for name, _ in conn.select_calls] == ["Sent Items", '"Sent Items"']


def test_select_retries_after_an_exception_on_the_raw_name():
    conn = Conn({"Sent Items": OSError("syntax"), '"Sent Items"': ("OK", [])})
    assert ep.select_mailbox(conn, "Sent Items", readonly=True)[0] == "OK"


def test_select_reports_the_last_refusal_when_nothing_works():
    conn = Conn({"A B": ("NO", [b"first"]), '"A B"': ("BAD", [b"last"])})
    assert ep.select_mailbox(conn, "A B", readonly=True) == ("BAD", [b"last"])


def test_select_raises_the_last_error_when_every_candidate_raises():
    conn = Conn({"A B": OSError("first"), '"A B"': ConnectionError("last")})
    with pytest.raises(ConnectionError, match="last"):
        ep.select_mailbox(conn, "A B", readonly=True)


def test_select_raises_a_transport_error_even_if_another_candidate_was_merely_refused():
    # The caller sees the transport failure rather than a misleading "NO".
    conn = Conn({"A B": OSError("transport down"), '"A B"': ("NO", [b"no"])})
    with pytest.raises(OSError, match="transport down"):
        ep.select_mailbox(conn, "A B", readonly=True)


def test_select_of_a_blank_mailbox_makes_no_call():
    conn = Conn()
    assert ep.select_mailbox(conn, "  ", readonly=True) == ("NO", [])
    assert conn.select_calls == []


# --- fetch --------------------------------------------------------------------------------


def test_fetch_selects_then_fetches_the_rfc822_payload_by_uid():
    conn = Conn({"INBOX": ("OK", [])}, {("fetch", "42"): ("OK", [(b"42 (RFC822 {5}", b"hello")])})
    data = ep.fetch_message_by_uid(conn, "INBOX", 42, readonly=True)
    assert data == [(b"42 (RFC822 {5}", b"hello")]
    assert conn.uid_calls == [("fetch", "42", "(RFC822)")]


def test_fetch_from_an_unselectable_mailbox_names_it():
    with pytest.raises(RuntimeError, match="Cannot select mailbox 'Nope'"):
        ep.fetch_message_by_uid(Conn(), "Nope", 1, readonly=True)


def test_a_failed_fetch_names_the_uid_and_mailbox():
    conn = Conn({"INBOX": ("OK", [])}, {("fetch", "7"): ("NO", [])})
    with pytest.raises(RuntimeError, match=r"Cannot fetch uid 7 from 'INBOX'"):
        ep.fetch_message_by_uid(conn, "INBOX", 7, readonly=True)


# --- finding a message by Message-ID ------------------------------------------------------


class SR:
    """The slice of shock-relay that the lookup uses."""

    def __init__(self, messages):
        self.messages = messages  # {(mailbox, uid): message_id}

    @staticmethod
    def normalize_message_id_header(value):
        return str(value or "").strip().strip("<>").lower()

    @staticmethod
    def parse_int_uid_list(data):
        raw = data[0] if data else b""
        return [int(x) for x in bytes(raw).split()]

    @staticmethod
    def extract_fetch_payload(msg_data):
        return msg_data[0][1] if msg_data and msg_data[0] else b""

    def normalize_message(self, mailbox, uid, payload):
        return {"message_id": self.messages.get((mailbox, uid), "")}


def mailbox_with(uids, payloads=None):
    results = {"search": ("OK", [" ".join(map(str, uids)).encode()])}
    for uid in uids:
        results[("fetch", str(uid))] = (
            "OK",
            [(b"x", (payloads or {}).get(uid, b"payload"))],
        )
    return results


def build(mailboxes):
    selects, results = {}, {}
    sr_messages = {}
    for name, per_uid in mailboxes.items():
        selects[name] = ("OK", [])
        res = mailbox_with(list(per_uid))
        results.update({("fetch", str(uid)): res[("fetch", str(uid))] for uid in per_uid})
        results["search"] = res["search"]
        for uid, mid in per_uid.items():
            sr_messages[(name, uid)] = mid
    return selects, results, SR(sr_messages)


def test_the_message_is_found_with_its_canonical_id():
    selects, results, sr = build({"INBOX": {1: "<other@x>", 2: "<Wanted@Example.com>"}})
    conn = Conn(selects, results)
    found = ep.find_message_in_mailboxes(
        conn, sr, ["INBOX"], " <wanted@example.com> ", recent_limit=10, search_charset=None
    )
    assert found == {"mailbox": "INBOX", "uid": 2, "message_id": "wanted@example.com"}


def test_the_newest_messages_are_searched_first_and_only_recent_limit_of_them():
    selects, results, sr = build({"INBOX": {1: "<target@x>", 2: "<a@x>", 3: "<b@x>"}})
    conn = Conn(selects, results)
    assert (
        ep.find_message_in_mailboxes(
            conn, sr, ["INBOX"], "target@x", recent_limit=2, search_charset=None
        )
        is None
    )
    fetched = [call[1] for call in conn.uid_calls if call[0] == "fetch"]
    assert fetched == ["3", "2"]  # newest first, the oldest (uid 1) was never fetched


def test_the_search_uses_the_given_charset_and_selects_read_only():
    selects, results, sr = build({"INBOX": {1: "<a@x>"}})
    conn = Conn(selects, results)
    ep.find_message_in_mailboxes(conn, sr, ["INBOX"], "z@x", recent_limit=5, search_charset="UTF-8")
    assert ("search", "UTF-8", "ALL") in conn.uid_calls
    assert all(readonly for _, readonly in conn.select_calls)


def test_other_mailboxes_are_tried_when_the_first_has_no_match():
    selects, results, sr = build({"INBOX": {1: "<a@x>"}})
    selects["Archive"] = ("OK", [])
    sr.messages[("Archive", 1)] = "<target@x>"
    conn = Conn(selects, results)
    found = ep.find_message_in_mailboxes(
        conn, sr, ["INBOX", "Archive"], "target@x", recent_limit=5, search_charset=None
    )
    assert found["mailbox"] == "Archive"


def test_a_mailbox_that_cannot_be_selected_is_skipped():
    selects, results, sr = build({"Archive": {1: "<target@x>"}})
    selects["Broken"] = OSError("down")
    selects["Refused"] = ("NO", [])
    conn = Conn(selects, results)
    found = ep.find_message_in_mailboxes(
        conn, sr, ["Broken", "Refused", "Archive"], "target@x", recent_limit=5, search_charset=None
    )
    assert found["mailbox"] == "Archive"


def test_a_failed_search_or_an_empty_mailbox_is_skipped():
    selects = {"A": ("OK", []), "B": ("OK", []), "C": ("OK", [])}
    sr = SR({("C", 1): "<target@x>"})
    answers = iter([("NO", []), ("OK", [b""]), ("OK", [b"1"])])

    class SearchConn(Conn):
        def uid(self, command, *args):
            self.uid_calls.append((command, *args))
            if command == "search":
                return next(answers)
            return ("OK", [(b"x", b"payload")])

    conn = SearchConn(selects)
    found = ep.find_message_in_mailboxes(
        conn, sr, ["A", "B", "C"], "target@x", recent_limit=5, search_charset=None
    )
    assert found["mailbox"] == "C"


def test_a_message_with_an_empty_payload_is_skipped():
    selects, results, sr = build({"INBOX": {1: "<target@x>", 2: "<target@x>"}})
    results[("fetch", "2")] = ("OK", [(b"x", b"")])
    conn = Conn(selects, results)
    found = ep.find_message_in_mailboxes(
        conn, sr, ["INBOX"], "target@x", recent_limit=5, search_charset=None
    )
    assert found["uid"] == 1


def test_no_mailboxes_means_no_match():
    assert (
        ep.find_message_in_mailboxes(Conn(), SR({}), [], "x@y", recent_limit=5, search_charset=None)
        is None
    )


def test_a_blank_wanted_id_matches_only_messages_that_also_have_no_id():
    selects, results, sr = build({"INBOX": {1: "<real@x>"}})
    conn = Conn(selects, results)
    assert (
        ep.find_message_in_mailboxes(conn, sr, ["INBOX"], "", recent_limit=5, search_charset=None)
        is None
    )
