"""Relay-authentication trust, Kalshi Spam recovery, tracking helpers and archive paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from intake import email as intake_email
from intake.db import init_db, is_email_processed, record_email_receipt
from intake.email import (
    FetchResult,
    _email_archive_datetime,
    _email_message_tracking_state,
    _internal_notification_ready_to_file,
    _notification_inbox_grace_seconds,
    _parse_tracking_datetime,
    _processed_email_archive_dir,
    _safe_archive_token,
    _unique_email_archive_path,
)

GOOD = (
    "mx.google.com; dkim=pass header.i=@privaterelay.appleid.com header.s=s1; "
    "spf=pass smtp.mailfrom=x; dmarc=pass (p=NONE) header.from=privaterelay.appleid.com"
)


def message(*results: str) -> EmailMessage:
    msg = EmailMessage()
    for value in results:
        msg["Authentication-Results"] = value
    return msg


class TestRelayAuthentication:
    def test_accepts_gmails_passing_apple_relay_result(self):
        assert intake_email._has_google_apple_relay_authentication(message(GOOD))

    @pytest.mark.parametrize(
        "value",
        [
            "mx.google.com; dkim=fail header.i=@privaterelay.appleid.com; dmarc=pass",
            "mx.google.com; dkim=pass header.i=@privaterelay.appleid.com; dmarc=fail",
            "mx.google.com; dkim=pass header.i=@privaterelay.appleid.com",
            "mx.google.com; dkim=pass header.i=@other.example; dmarc=pass",
            "other.example; dkim=pass header.i=@privaterelay.appleid.com; dmarc=pass",
            "  ",
        ],
    )
    def test_rejects_anything_short_of_dkim_and_dmarc_pass_from_gmail(self, value):
        assert not intake_email._has_google_apple_relay_authentication(message(value))

    def test_the_trusted_name_must_be_the_issuer_not_a_substring(self):
        value = (
            "evil.example; note=mx.google.com; "
            "dkim=pass header.i=@privaterelay.appleid.com; dmarc=pass"
        )
        assert not intake_email._has_google_apple_relay_authentication(message(value))

    def test_signing_domain_at_the_very_end_of_the_header_is_accepted(self):
        value = "mx.google.com; dmarc=pass; dkim=pass header.i=@privaterelay.appleid.com"
        assert intake_email._has_google_apple_relay_authentication(message(value))

    def test_no_header_is_not_authenticated(self):
        assert not intake_email._has_google_apple_relay_authentication(EmailMessage())

    def test_whitespace_and_case_are_normalised(self):
        value = "MX.Google.com;  dkim=PASS   header.i=@PrivateRelay.AppleID.com;   DMARC=pass"
        assert intake_email._has_google_apple_relay_authentication(message(value))

    def test_a_longer_domain_that_merely_starts_with_the_relay_domain_is_rejected(self):
        value = (
            "mx.google.com; dkim=pass header.i=@privaterelay.appleid.com.attacker.example; "
            "dmarc=pass"
        )
        assert not intake_email._has_google_apple_relay_authentication(message(value))

    def test_a_forged_result_below_gmails_own_does_not_authenticate(self):
        # Gmail prepends its verdict; anything the sender supplied sits beneath it.
        failing = "mx.google.com; dkim=fail header.i=@privaterelay.appleid.com; dmarc=fail"
        assert not intake_email._has_google_apple_relay_authentication(message(failing, GOOD))


class TestGraceSeconds:
    def test_defaults_and_units(self):
        assert _notification_inbox_grace_seconds({}) == 3600
        assert _notification_inbox_grace_seconds({"notification_inbox_grace_minutes": 2}) == 120
        assert _notification_inbox_grace_seconds({"notification_inbox_grace_minutes": 0.5}) == 30
        assert _notification_inbox_grace_seconds({"notification_inbox_grace_seconds": 90}) == 90

    def test_seconds_take_precedence_over_minutes(self):
        cfg = {"notification_inbox_grace_seconds": 5, "notification_inbox_grace_minutes": 9}
        assert _notification_inbox_grace_seconds(cfg) == 5

    @pytest.mark.parametrize("raw", ["soon", None, [1], {}, float("nan"), float("inf"), "inf"])
    def test_unusable_values_fall_back_to_an_hour(self, raw):
        assert _notification_inbox_grace_seconds({"notification_inbox_grace_minutes": raw}) == 3600
        assert _notification_inbox_grace_seconds({"notification_inbox_grace_seconds": raw}) == 3600

    def test_negative_values_clamp_to_zero(self):
        assert _notification_inbox_grace_seconds({"notification_inbox_grace_seconds": -5}) == 0


class TestTracking:
    def test_parse_tracking_datetime(self):
        assert _parse_tracking_datetime("") is None
        assert _parse_tracking_datetime("garbage") is None
        parsed = _parse_tracking_datetime("2026-01-02T03:04:05Z")
        assert parsed == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        assert _parse_tracking_datetime("2026-01-02T03:04:05+00:00") == parsed

    def test_ready_only_for_aged_internal_notifications(self):
        old = datetime.now(UTC) - timedelta(seconds=100)
        assert _internal_notification_ready_to_file(
            {"status": "internal", "processed_at": old}, grace_seconds=100
        )
        assert not _internal_notification_ready_to_file(
            {"status": "internal", "processed_at": old}, grace_seconds=1000
        )
        assert not _internal_notification_ready_to_file(
            {"status": "processed", "processed_at": old}, grace_seconds=0
        )
        assert not _internal_notification_ready_to_file(
            {"status": "internal", "processed_at": None}, grace_seconds=0
        )
        assert not _internal_notification_ready_to_file({}, grace_seconds=0)

    def test_tracking_state_matches_by_uid_then_message_id(self):
        conn = init_db(Path(":memory:"))
        assert _email_message_tracking_state(conn, uid=1, mailbox="INBOX", message_id="") == {
            "tracked": False,
            "status": "",
            "processed_at": None,
        }
        record_email_receipt(conn, 5, "INBOX", "<a@x>", "s", "f", "d", 0, status="internal")
        by_uid = _email_message_tracking_state(conn, uid=5, mailbox="INBOX", message_id="")
        assert by_uid["tracked"] and by_uid["status"] == "internal"
        assert isinstance(by_uid["processed_at"], datetime)
        by_id = _email_message_tracking_state(conn, uid=99, mailbox="Other", message_id="<a@x>")
        assert by_id["tracked"] and by_id["status"] == "internal"
        miss = _email_message_tracking_state(conn, uid=99, mailbox="Other", message_id="<zzz>")
        assert miss["tracked"] is False

    def test_a_blank_message_id_never_matches_another_message(self):
        conn = init_db(Path(":memory:"))
        record_email_receipt(conn, 1, "INBOX", "", "s", "f", "d", 0, status="internal")
        other = _email_message_tracking_state(conn, uid=2, mailbox="INBOX", message_id="")
        assert other["tracked"] is False


class TestArchivePaths:
    def test_safe_token(self):
        assert _safe_archive_token("[Gmail]/Spam") == "Gmail_Spam"
        assert _safe_archive_token("../..") == "mailbox"
        assert _safe_archive_token(None) == "mailbox"
        assert _safe_archive_token("  a b  ") == "a_b"
        assert "/" not in _safe_archive_token("a/b\\c")

    def test_archive_dir_rules(self, tmp_path):
        assert _processed_email_archive_dir({"receipt_dir": ""}) is None
        assert _processed_email_archive_dir({}) is None
        base = {"receipt_dir": str(tmp_path)}
        assert _processed_email_archive_dir(base) == tmp_path / "email/messages"
        off = {**base, "email": {"archive_processed_messages": False}}
        assert _processed_email_archive_dir(off) is None
        rel = {**base, "email": {"processed_email_dir": "custom/dir"}}
        assert _processed_email_archive_dir(rel) == tmp_path / "custom/dir"
        absolute = {**base, "email": {"processed_email_dir": str(tmp_path / "abs")}}
        assert _processed_email_archive_dir(absolute) == tmp_path / "abs"
        blank = {**base, "email": {"processed_email_dir": "   "}}
        assert _processed_email_archive_dir(blank) == tmp_path / "email/messages"
        assert (
            _processed_email_archive_dir({**base, "email": "nope"}) == tmp_path / "email/messages"
        )

    def test_archive_datetime(self):
        parsed = _email_archive_datetime("Tue, 03 Feb 2026 04:05:59 +0000")
        assert (parsed.year, parsed.month, parsed.day, parsed.minute, parsed.second) == (
            2026,
            2,
            3,
            5,
            0,
        )
        assert parsed.tzinfo is None
        for bad in (None, "", "not a date"):
            assert isinstance(_email_archive_datetime(bad), datetime)

    def test_unique_path_reuses_identical_content_and_never_clobbers(self, tmp_path):
        first = tmp_path / "m.eml"
        assert _unique_email_archive_path(first, b"A") == first
        first.write_bytes(b"A")
        assert _unique_email_archive_path(first, b"A") == first
        second = _unique_email_archive_path(first, b"B")
        assert second == tmp_path / "m_2.eml"
        second.write_bytes(b"B")
        assert _unique_email_archive_path(first, b"B") == second
        assert _unique_email_archive_path(first, b"C") == tmp_path / "m_3.eml"
        assert first.read_bytes() == b"A"

    def test_unique_path_gives_up_after_many_collisions(self, tmp_path):
        first = tmp_path / "m.eml"
        first.write_bytes(b"0")
        for index in range(2, 1000):
            (tmp_path / f"m_{index}.eml").write_bytes(b"x")
        with pytest.raises(RuntimeError, match="could not allocate"):
            _unique_email_archive_path(first, b"new")


class FakeSr:
    """Stand-in for shock-relay's helpers used by the Spam recovery path."""

    def __init__(self, uids, payloads, search_status="OK"):
        self.uids = uids
        self.payloads = payloads
        self.search_status = search_status
        self.moved: list[int] = []
        self.read: list[int] = []
        self.fetched: list[int] = []

    def open_imap_connection(self, config):
        sr = self

        class Conn:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def uid(self_inner, command, *args):
                if command == "search":
                    return sr.search_status, [b" ".join(str(u).encode() for u in sr.uids)]
                uid = int(args[0])
                sr.fetched.append(uid)
                return "OK", [(b"x", sr.payloads[uid])]

            def expunge(self_inner):
                return None

        return Conn()

    def build_search_terms(self, **kwargs):
        return ["TERMS"]

    def parse_int_uid_list(self, data):
        return [int(x) for x in data[0].split()]

    def extract_fetch_payload(self, data):
        return data[0][1]

    def decode_mime_header(self, value):
        return str(value or "")

    def extract_message_bodies(self, msg):
        body = msg.get_body(preferencelist=("plain",))
        return (body.get_content() if body else ""), ""

    def move_message(self, conn, uid, label):
        self.moved.append(uid)

    def mark_message_read(self, conn, uid):
        self.read.append(uid)


def kalshi_payload(
    results: tuple[str, ...], subject="Kalshi settled", message_id="<k1@x>"
) -> bytes:
    msg = EmailMessage()
    for value in results:
        msg["Authentication-Results"] = value
    msg["Subject"] = subject
    msg["From"] = "Kalshi <no-reply_at_kalshi_com_abc123_def456@privaterelay.appleid.com>"
    msg["Message-ID"] = message_id
    msg["Date"] = "Tue, 03 Feb 2026 04:05:59 +0000"
    msg.set_content("body")
    return msg.as_bytes()


@pytest.fixture
def run_spam(monkeypatch, tmp_path):
    def run(sr, *, dry_run=False, limit=10, structured=None, select_status="OK"):
        conn = init_db(Path(":memory:"))
        result = FetchResult()
        calls = {"upsert": 0, "artifact": 0}
        monkeypatch.setattr(
            intake_email, "select_mailbox", lambda c, m, readonly=False: (select_status, [])
        )
        monkeypatch.setattr(
            intake_email,
            "_extract_structured_finance_email",
            lambda s, f, b: structured,
        )
        monkeypatch.setattr(
            intake_email,
            "_upsert_html_receipt",
            lambda *a, **k: calls.__setitem__("upsert", calls["upsert"] + 1),
        )
        monkeypatch.setattr(
            intake_email,
            "_write_processed_email_artifact",
            lambda *a, **k: calls.__setitem__("artifact", calls["artifact"] + 1),
        )
        intake_email._fetch_kalshi_spam_liquidations(
            sr,
            SimpleNamespace(imap=SimpleNamespace(search_charset="UTF-8")),
            {"receipt_dir": str(tmp_path)},
            conn,
            result,
            processed_label="Processed",
            since_days=30,
            limit=limit,
            dry_run=dry_run,
            verbose=False,
        )
        return conn, result, calls

    return run


LIQUIDATION = {"processed_by": "email-kalshi-liquidation"}


class TestKalshiSpamRecovery:
    def test_authenticated_liquidation_is_recorded_filed_and_marked_read(self, run_spam):
        sr = FakeSr([7], {7: kalshi_payload((GOOD,))})
        conn, result, calls = run_spam(sr, structured=LIQUIDATION)
        assert (result.found, result.html_processed, result.errors) == (1, 1, 0)
        assert calls == {"upsert": 1, "artifact": 1}
        assert sr.moved == [7] and sr.read == [7]
        assert is_email_processed(conn, 7, "[Gmail]/Spam")

    def test_unauthenticated_mail_in_spam_is_never_promoted(self, run_spam):
        forged = "mx.google.com; dkim=fail header.i=@privaterelay.appleid.com; dmarc=fail"
        for results in ((), (forged,), (forged, GOOD)):
            sr = FakeSr([7], {7: kalshi_payload(results)})
            conn, result, calls = run_spam(sr, structured=LIQUIDATION)
            assert (result.found, sr.moved, calls["upsert"]) == (0, [], 0), results

    def test_only_liquidation_shaped_messages_count(self, run_spam):
        for structured in (None, {}, {"processed_by": "email-other"}):
            sr = FakeSr([7], {7: kalshi_payload((GOOD,))})
            _, result, calls = run_spam(sr, structured=structured)
            assert (result.found, sr.moved, calls["upsert"]) == (0, [], 0)

    def test_already_processed_by_uid_or_message_id_is_filed_not_reprocessed(self, run_spam):
        for seed in ("uid", "message_id"):
            sr = FakeSr([7], {7: kalshi_payload((GOOD,), message_id="<dup@x>")})

            def runner(sr=sr, seed=seed):
                return run_spam(sr, structured=LIQUIDATION)

            # Seed the DB through a first pass, then run again on the same connection state.
            conn = init_db(Path(":memory:"))
            uid, mailbox = (7, "[Gmail]/Spam") if seed == "uid" else (1, "INBOX")
            record_email_receipt(conn, uid, mailbox, "<dup@x>", "s", "f", "d", 0)
            self._run_with(conn, sr, run_spam)
            assert sr.moved == [7]
            assert sr.read == []

    @staticmethod
    def _run_with(conn, sr, run_spam):
        import intake.email as module

        result = FetchResult()
        original = module.select_mailbox
        module.select_mailbox = lambda c, m, readonly=False: ("OK", [])
        module._extract_structured_finance_email, saved = (
            lambda s, f, b: LIQUIDATION,
            module._extract_structured_finance_email,
        )
        try:
            module._fetch_kalshi_spam_liquidations(
                sr,
                SimpleNamespace(imap=SimpleNamespace(search_charset="UTF-8")),
                {},
                conn,
                result,
                processed_label="P",
                since_days=1,
                limit=5,
                dry_run=False,
                verbose=False,
            )
        finally:
            module.select_mailbox = original
            module._extract_structured_finance_email = saved
        assert result.skipped == 1

    def test_dry_run_changes_nothing(self, run_spam):
        sr = FakeSr([7], {7: kalshi_payload((GOOD,))})
        conn, result, calls = run_spam(sr, dry_run=True, structured=LIQUIDATION)
        assert (result.found, sr.moved, sr.read, calls) == (1, [], [], {"upsert": 0, "artifact": 0})
        assert not is_email_processed(conn, 7, "[Gmail]/Spam")

    def test_newest_first_and_capped_by_limit(self, run_spam):
        sr = FakeSr(
            [3, 9, 5], {u: kalshi_payload((GOOD,), message_id=f"<{u}@x>") for u in (3, 5, 9)}
        )
        run_spam(sr, limit=2, structured=LIQUIDATION)
        assert sr.fetched == [9, 5]

    def test_failures_are_counted_and_do_not_stop_the_batch(self, run_spam):
        sr = FakeSr([9, 5], {5: kalshi_payload((GOOD,), message_id="<5@x>"), 9: b""})
        _, result, _ = run_spam(sr, structured=LIQUIDATION)
        # uid 9 has no payload (skipped silently); uid 5 still processed.
        assert (result.html_processed, result.errors) == (1, 0)

        class Exploding(FakeSr):
            def decode_mime_header(self, value):
                raise ValueError("boom")

        sr = Exploding([9, 5], {u: kalshi_payload((GOOD,)) for u in (5, 9)})
        _, result, _ = run_spam(sr, structured=LIQUIDATION)
        assert result.errors == 2 and result.html_processed == 0

    def test_unavailable_spam_mailbox_or_search_is_a_quiet_no_op(self, run_spam):
        sr = FakeSr([7], {7: kalshi_payload((GOOD,))})
        _, result, _ = run_spam(sr, structured=LIQUIDATION, select_status="NO")
        assert (result.found, sr.fetched) == (0, [])
        sr = FakeSr([7], {7: kalshi_payload((GOOD,))}, search_status="NO")
        _, result, _ = run_spam(sr, structured=LIQUIDATION)
        assert (result.found, sr.fetched) == (0, [])

    def test_helper_less_shock_relay_is_a_no_op(self, run_spam):
        class Bare:
            pass

        _, result, calls = run_spam(Bare(), structured=LIQUIDATION)
        assert (result.found, calls["upsert"]) == (0, 0)

    def test_spam_is_opened_read_only_on_dry_run(self, monkeypatch, tmp_path):
        seen = []
        monkeypatch.setattr(
            intake_email,
            "select_mailbox",
            lambda c, m, readonly=False: seen.append((m, readonly)) or ("NO", []),
        )
        for dry in (True, False):
            intake_email._fetch_kalshi_spam_liquidations(
                FakeSr([], {}),
                SimpleNamespace(imap=SimpleNamespace(search_charset="UTF-8")),
                {},
                init_db(Path(":memory:")),
                FetchResult(),
                processed_label="P",
                since_days=1,
                limit=1,
                dry_run=dry,
                verbose=False,
            )
        assert seen == [("[Gmail]/Spam", True), ("[Gmail]/Spam", False)]


class TestDaemon:
    def test_polls_repeatedly_survives_errors_and_sleeps_the_interval(self, monkeypatch, capsys):
        outcomes = [FetchResult(), RuntimeError("imap down"), FetchResult()]
        sleeps: list[int] = []

        def fetch(settings, conn, verbose=True):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise KeyboardInterrupt

        monkeypatch.setattr(intake_email, "fetch_receipts", fetch)
        monkeypatch.setattr(intake_email.time, "sleep", sleep)
        with pytest.raises(KeyboardInterrupt):
            intake_email.run_daemon({"email": {"poll_interval_seconds": "42"}}, object())
        assert sleeps == [42, 42, 42]
        out = capsys.readouterr().out
        assert "poll every 42s" in out and "fetch error: imap down" in out

    def test_default_interval_and_quiet_mode(self, monkeypatch, capsys):
        monkeypatch.setattr(intake_email, "fetch_receipts", lambda *a, **k: FetchResult())
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            raise KeyboardInterrupt

        monkeypatch.setattr(intake_email.time, "sleep", sleep)
        with pytest.raises(KeyboardInterrupt):
            intake_email.run_daemon({}, object(), verbose=False)
        assert sleeps == [300]
        assert capsys.readouterr().out == ""
