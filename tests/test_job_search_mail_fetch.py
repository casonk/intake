"""Career mail capture: what is saved, what is filed, and how verification codes are handled.

Employer and application-portal mail is written to the private career workspace. The parts that
need care: a one-time verification code is written owner-only, only while fresh, never printed,
and never on a dry run; the mailbox is fetched with BODY.PEEK so read state is untouched unless
filing is switched on; only mapped employers and known portals are ever captured. IMAP and
shock-relay are fakes and the career workspace is a temporary directory.
"""

from __future__ import annotations

import json
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from intake import job_search_mail as jm

EMPLOYERS = {"acme.com": "Acme"}


def fresh():
    return format_datetime(datetime.now(UTC))


def stale():
    return format_datetime(datetime.now(UTC) - timedelta(hours=2))


def raw(sender="Careers <jobs@acme.com>", subject="Application received", body="Thank you for applying to Acme.",date_header="fresh", message_id="<m1@x>", html=None):  # fmt: skip
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    if date_header == "fresh":
        msg["Date"] = fresh()
    elif date_header == "stale":
        msg["Date"] = stale()
    elif date_header is not None:
        msg["Date"] = date_header
    if message_id is not None:
        msg["Message-ID"] = message_id
    if body is not None:
        msg.set_content(body)
        if html is not None:
            msg.add_alternative(html, subtype="html")
    else:
        msg.set_content("x")
        msg.set_content(html or "", subtype="html")
    return msg.as_bytes()


class SR:
    def __init__(self, conn, mailboxes=None):
        self.conn = conn
        self.mailboxes = mailboxes
        self.moved = []

    def load_config(self, path):
        return SimpleNamespace(
            imap=SimpleNamespace(mailboxes=self.mailboxes, search_charset="UTF-8")
        )

    def validate_imap(self, config):
        pass

    def open_imap_connection(self, config):
        @contextmanager
        def cm():
            yield self.conn

        return cm()

    @staticmethod
    def build_search_terms(*, from_contains, since_days):
        return ["FROM", from_contains, f"SINCE{since_days}"]

    @staticmethod
    def parse_int_uid_list(data):
        return [int(x) for x in data[0].split()] if data and data[0] else []

    @staticmethod
    def extract_fetch_payload(fetched):
        return fetched[0][1] if fetched and fetched[0] else b""

    @staticmethod
    def decode_mime_header(value):
        return str(value or "")

    @staticmethod
    def extract_message_bodies(msg):
        plain = msg.get_body(preferencelist=("plain",))
        html = msg.get_body(preferencelist=("html",))
        # like the real shock-relay: a missing part is an empty string, never None
        return (
            plain.get_content().strip() if plain is not None else "",
            html.get_content().strip() if html is not None else "",
        )

    def move_message(self, conn, uid, label):
        self.moved.append((uid, label))


class Conn:
    def __init__(self, messages):
        self.messages = messages
        self.searches = []
        self.fetches = []
        self.ops = []
        self.expunged = 0

    def uid(self, command, *args):
        if command == "search":
            charset, *terms = args
            self.searches.append(tuple(terms))
            return "OK", [" ".join(map(str, self.messages)).encode()]
        if command == "fetch":
            uid, spec = args
            self.fetches.append((int(uid), spec))
            payload = self.messages.get(int(uid))
            return ("OK", [(b"x", payload)]) if payload is not None else ("NO", [])
        self.ops.append((command, *args))
        return "OK", []

    def create(self, label):
        self.ops.append(("create", label))

    def expunge(self):
        self.expunged += 1


@pytest.fixture
def career(tmp_path, monkeypatch):
    root = tmp_path / "career"
    (root / "applications").mkdir(parents=True)
    (root / "private").mkdir()
    state = SimpleNamespace(root=root, selected=[], select_status="OK", sr=None, conn=None)

    def setup(messages, *, mailboxes=None):
        conn = Conn(messages)
        sr = SR(conn, mailboxes)
        state.sr, state.conn = sr, conn
        monkeypatch.setattr(jm, "load_shock_relay", lambda path: sr)

        def select(connection, mailbox, *, readonly):
            state.selected.append((mailbox, readonly))
            return state.select_status, []

        monkeypatch.setattr(jm, "select_mailbox", select)
        return state

    state.setup = setup
    state.settings = lambda **career_kw: {
        "email": {"shock_relay_config": "/cfg/settings.toml"},
        "career": {"repo_root": str(root), "employers": dict(EMPLOYERS), **career_kw},
    }
    return state


def records(root):
    return sorted((root / "private" / "intake-email").glob("*.md"))


def codes(root):
    return sorted((root / "private" / "verification-codes").glob("*.json"))


# --- configuration ------------------------------------------------------------------------


def test_without_a_career_repo_or_employers_nothing_happens(career):
    state = career.setup({1: raw()})
    assert jm.fetch_actions({}) == []
    assert jm.fetch_actions({"career": {"employers": EMPLOYERS}}) == []
    assert jm.fetch_actions({"career": {"repo_root": str(career.root)}}) == []
    assert state.conn.searches == []


def test_a_career_repo_without_its_folders_is_refused(career, tmp_path):
    career.setup({})
    broken = tmp_path / "broken"
    (broken / "applications").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="missing applications/ or private/"):
        jm.fetch_actions({"career": {"repo_root": str(broken), "employers": EMPLOYERS}})


def test_the_shock_relay_config_is_required(career):
    career.setup({})
    settings = career.settings()
    settings["email"] = {}
    with pytest.raises(RuntimeError, match="shock_relay_config is required"):
        jm.fetch_actions(settings)


def test_the_first_configured_mailbox_is_used_else_inbox(career):
    state = career.setup({}, mailboxes=["Jobs", "Other"])
    jm.fetch_actions(career.settings())
    assert state.selected == [("Jobs", True)]
    state2 = career.setup({}, mailboxes=None)
    state2.selected.clear()
    jm.fetch_actions(career.settings())
    assert state2.selected[-1] == ("INBOX", True)


def test_an_unselectable_mailbox_is_an_error(career):
    state = career.setup({})
    state.select_status = "NO"
    with pytest.raises(RuntimeError, match="Cannot select mailbox 'INBOX'"):
        jm.fetch_actions(career.settings())


def test_every_employer_and_portal_is_searched_with_the_window(career):
    state = career.setup({})
    jm.fetch_actions(career.settings(since_days=7))
    domains = [terms[1] for terms in state.conn.searches]
    assert domains == ["acme.com", *jm.PORTAL_SENDERS]
    assert all(terms[2] == "SINCE7" for terms in state.conn.searches)
    state.conn.searches.clear()
    jm.fetch_actions(career.settings())
    assert state.conn.searches[0][2] == "SINCE30"


def test_only_the_newest_messages_up_to_the_limit_are_fetched(career):
    state = career.setup({i: raw(message_id=f"<m{i}@x>") for i in range(1, 8)})
    jm.fetch_actions(career.settings(limit=3), verbose=False)
    assert [uid for uid, _ in state.conn.fetches] == [7, 6, 5]


def test_messages_are_fetched_with_peek_so_read_state_is_untouched(career):
    state = career.setup({1: raw()})
    jm.fetch_actions(career.settings(), verbose=False)
    assert state.conn.fetches == [(1, "(BODY.PEEK[])")]
    assert state.selected == [("INBOX", True)]


# --- saving records -----------------------------------------------------------------------


def test_an_employer_message_is_saved_once_with_its_kind(career):
    career.setup({1: raw()})
    saved = jm.fetch_actions(career.settings(), verbose=False)
    (path,) = records(career.root)
    assert saved == [path]
    text = path.read_text()
    assert "- Kind: confirmation" in text and "- Organization: Acme" in text
    assert "- [x] Nothing to do (confirmation)" in text
    assert jm.fetch_actions(career.settings(), verbose=False) == []  # not saved twice
    assert len(records(career.root)) == 1


def test_a_request_is_saved_as_an_open_action(career):
    career.setup(
        {1: raw(subject="Next steps", body="Please complete the form for your application.")}
    )
    jm.fetch_actions(career.settings(), verbose=False)
    (path,) = records(career.root)
    assert "- [ ] Review and complete the requested career follow-up" in path.read_text()


def test_unmapped_senders_and_unrelated_mail_are_not_captured(career):
    career.setup(
        {
            1: raw(sender="Spam <x@unknown.example>", message_id="<1>"),
            2: raw(subject="Lunch?", body="Want to grab lunch?", message_id="<2>"),
            3: raw(subject="Sale", body="Thank you for applying this discount", message_id="<3>"),
        }
    )
    jm.fetch_actions(career.settings(), verbose=False)
    ids = "".join(p.read_text() for p in records(career.root))
    assert "<1>" not in ids and "<2>" not in ids


def test_a_classified_message_needs_a_career_word_unless_it_comes_from_a_portal(career):
    career.setup(
        {
            1: raw(subject="Thanks", body="We have received your application", message_id="<ok>"),
            2: raw(
                subject="Update",
                body="regret to inform you the position has been filled",
                message_id="<no-career-word>",
            ),
            3: raw(
                sender="x@greenhouse-mail.io",
                subject="Update on your application to Acme",
                body="regret to inform you",
                message_id="<portal>",
            ),  # fmt: skip
        }
    )
    jm.fetch_actions(career.settings(), verbose=False)
    text = "".join(p.read_text() for p in records(career.root))
    assert "<ok>" in text and "<portal>" in text and "<no-career-word>" not in text


def test_a_portal_message_names_its_employer_from_the_subject(career):
    career.setup(
        {
            1: raw(
                sender="no-reply@greenhouse-mail.io",
                subject="Thank you for applying to Acme!",
                body="Your application was received.",
                message_id="<g>",
            ),  # fmt: skip
        }
    )
    jm.fetch_actions(career.settings(), verbose=False)
    assert "- Organization: Acme" in records(career.root)[0].read_text()


def test_html_only_mail_is_converted_to_text(career):
    career.setup({1: raw(body=None, html="<p>Thank you for applying to Acme.</p>")})
    jm.fetch_actions(career.settings(), verbose=False)
    text = records(career.root)[0].read_text()
    assert "Thank you for applying" in text and "<p>" not in text


def test_a_message_without_an_id_gets_one_from_its_mailbox_and_uid(career):
    career.setup({4: raw(message_id=None)})
    jm.fetch_actions(career.settings(), verbose=False)
    assert "- Message-ID: INBOX:4" in records(career.root)[0].read_text()


def test_a_bad_or_missing_date_falls_back_to_today(career):
    career.setup({1: raw(date_header="not a date"), 2: raw(date_header=None, message_id="<2>")})
    jm.fetch_actions(career.settings(), verbose=False)
    today = datetime.now(UTC).date().isoformat()
    assert all(f"- Received: {today}" in p.read_text() for p in records(career.root))
    assert len(records(career.root)) == 2


def test_progress_is_printed_only_when_verbose_and_never_in_a_dry_run_write(career, capsys):
    career.setup({1: raw()})
    jm.fetch_actions(career.settings(), dry_run=True, verbose=True)
    out = capsys.readouterr().out
    assert "Career confirmation: Acme: Application received ->" in out
    assert records(career.root) == []  # a dry run writes nothing
    jm.fetch_actions(career.settings(), verbose=False)
    assert capsys.readouterr().out == ""


def test_a_record_that_already_has_its_action_link_is_not_revisited(career, monkeypatch):
    html = '<a href="https://acme.com/f">Complete the form</a>'
    career.setup(
        {1: raw(subject="Next", body="Please complete the form for your application.", html=html)}
    )
    jm.fetch_actions(career.settings(), verbose=False)
    assert "- Action link:" in records(career.root)[0].read_text()
    calls = []
    monkeypatch.setattr(jm, "save_action", lambda root, message: calls.append(message))
    jm.fetch_actions(career.settings(), verbose=False)
    assert calls == []


# --- verification codes -------------------------------------------------------------------


CODE_MAIL = dict(subject="Your verification code", body="Verification code: ABC12345")


def test_a_fresh_code_is_saved_owner_only_and_never_printed(career, capsys):
    career.setup({1: raw(**CODE_MAIL)})
    saved = jm.fetch_actions(career.settings(), verbose=True)
    (path,) = codes(career.root)
    assert saved == [path]
    assert json.loads(path.read_text())["code"] == "ABC12345"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    out = capsys.readouterr().out
    assert "Career verification code: Acme (saved for career fill)" in out
    assert "ABC12345" not in out
    assert records(career.root) == []  # a code mail is not also an action record


def test_a_stale_code_is_neither_written_nor_announced(career, capsys):
    career.setup({1: raw(date_header="stale", **CODE_MAIL)})
    assert jm.fetch_actions(career.settings(), verbose=True) == []
    assert codes(career.root) == []
    assert "verification code" not in capsys.readouterr().out


def test_a_code_is_not_written_on_a_dry_run(career):
    career.setup({1: raw(**CODE_MAIL)})
    assert jm.fetch_actions(career.settings(), dry_run=True, verbose=False) == []
    assert codes(career.root) == []


def test_a_code_is_written_once_per_message(career):
    career.setup({1: raw(**CODE_MAIL)})
    jm.fetch_actions(career.settings(), verbose=False)
    assert jm.fetch_actions(career.settings(), verbose=False) == []
    assert len(codes(career.root)) == 1


def test_a_code_mail_without_a_date_is_treated_as_fresh(career):
    career.setup({1: raw(date_header=None, **CODE_MAIL)})
    jm.fetch_actions(career.settings(), verbose=False)
    assert len(codes(career.root)) == 1


# --- filing in Gmail ----------------------------------------------------------------------


def test_without_file_mail_nothing_is_labelled_or_moved(career):
    state = career.setup({1: raw()})
    jm.fetch_actions(career.settings(), verbose=False)
    assert state.sr.moved == [] and state.conn.ops == [] and state.conn.expunged == 0


def test_with_file_mail_new_messages_are_filed_under_the_label_prefix(career):
    state = career.setup(
        {
            1: raw(
                subject="Next",
                body="Please complete the form for your application.",
                message_id="<req>",
            ),
            2: raw(message_id="<conf>"),
            3: raw(**CODE_MAIL, message_id="<code>"),
        }
    )
    jm.fetch_actions(career.settings(file_mail=True, label_prefix="Jobs"), verbose=False)
    assert state.selected == [("INBOX", False)]  # writable only because filing is on
    assert ("create", "Jobs/Request") in state.conn.ops
    assert ("copy", "1", "Jobs/Request") in state.conn.ops
    assert ("store", "1", "+FLAGS", r"(\Flagged)") in state.conn.ops
    assert sorted(state.sr.moved) == [(2, "Jobs/Confirmation"), (3, "Jobs/Security code")]
    assert state.conn.expunged == 1


def test_the_default_label_prefix_is_career(career):
    state = career.setup({2: raw()})
    jm.fetch_actions(career.settings(file_mail=True), verbose=False)
    assert state.sr.moved == [(2, "Career/Confirmation")]


def test_a_dry_run_never_files_even_with_file_mail(career):
    state = career.setup({2: raw()})
    jm.fetch_actions(career.settings(file_mail=True), dry_run=True, verbose=False)
    assert state.selected == [("INBOX", True)]
    assert state.sr.moved == [] and state.conn.ops == []


def test_messages_already_saved_are_filed_again_only_with_refile(career):
    state = career.setup({2: raw()})
    jm.fetch_actions(career.settings(), verbose=False)  # saved earlier, not filed
    jm.fetch_actions(career.settings(file_mail=True), verbose=False)
    assert state.sr.moved == []
    jm.fetch_actions(career.settings(file_mail=True), verbose=False, refile=True)
    assert state.sr.moved == [(2, "Career/Confirmation")]


def test_refile_treats_a_ticked_action_as_done_and_files_it_away(career):
    state = career.setup(
        {1: raw(subject="Next", body="Please complete the form for your application.")}
    )
    jm.fetch_actions(career.settings(), verbose=False)
    path = records(career.root)[0]
    path.write_text(path.read_text().replace("- [ ]", "- [x]"))
    jm.fetch_actions(career.settings(file_mail=True), verbose=False, refile=True)
    assert state.sr.moved == [(1, "Career/Request")]  # done, so archived rather than kept starred
    assert state.conn.ops == []


def test_refile_of_a_code_files_it_even_though_nothing_new_was_written(career):
    state = career.setup({1: raw(**CODE_MAIL)})
    jm.fetch_actions(career.settings(), verbose=False)
    jm.fetch_actions(career.settings(file_mail=True), verbose=False, refile=True)
    assert state.sr.moved == [(1, "Career/Security code")]


def test_the_filing_summary_is_printed_when_verbose(career, capsys):
    career.setup({2: raw()})
    jm.fetch_actions(career.settings(file_mail=True), verbose=True)
    assert "Career mail filed: 1 message(s) labelled under Career/" in capsys.readouterr().out


def test_unfetchable_or_empty_messages_are_skipped(career):
    state = career.setup({1: raw()})

    class Flaky(Conn):
        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1 2 3"]
            if command == "fetch" and args[0] == "2":
                return "NO", [(b"x", raw(message_id="<no>"))]
            if command == "fetch" and args[0] == "3":
                return "OK", [(b"x", b"")]
            return super().uid(command, *args)

    state.conn.__class__ = Flaky
    jm.fetch_actions(career.settings(), verbose=False)
    assert "<no>" not in "".join(p.read_text() for p in records(career.root))
    assert len(records(career.root)) == 1


def test_a_failed_search_is_ignored_even_with_data(career):
    state = career.setup({1: raw()})

    class FailedButChatty(Conn):
        def uid(self, command, *args):
            if command == "search":
                return "NO", [b"1"]
            return super().uid(command, *args)

    state.conn.__class__ = FailedButChatty
    assert jm.fetch_actions(career.settings(), verbose=False) == []
    assert state.conn.fetches == []


# --- employer mapping, related applications, and record updates -------------------------


def test_an_exact_address_key_beats_a_domain_key_and_is_not_treated_as_a_domain():
    employers = {"acme.com": "Acme", "jobs@acme.com": "Acme Jobs", "@other.org": "Other"}
    assert jm._employer_for_sender("A <jobs@acme.com>", employers) == "Acme Jobs"
    assert jm._employer_for_sender("A <hr@acme.com>", employers) == "Acme"
    assert jm._employer_for_sender("A <hr@mail.acme.com>", employers) == "Acme"
    assert jm._employer_for_sender("A <x@other.org>", employers) == "Other"
    # an address key never matches by domain
    assert jm._employer_for_sender("A <hr@acme.com>", {"jobs@acme.com": "Acme Jobs"}) == ""
    # nor does a look-alike domain
    assert jm._employer_for_sender("A <x@notacme.com>", {"acme.com": "Acme"}) == ""
    assert jm._employer_for_sender("A <x@acme.com.evil.test>", {"acme.com": "Acme"}) == ""


def application(root, name, organization, requisition=""):
    folder = root / "applications" / name / "2026"
    folder.mkdir(parents=True)
    extra = f"\nrequisition: {requisition}\n" if requisition else ""
    (folder / "application.md").write_text(f"# App\nOrganization: {organization}\n{extra}")
    return f"applications/{name}/2026"


def test_related_applications_match_the_organization_exactly_and_sort(career):
    root = career.root
    b = application(root, "b-role", "Acme")
    a = application(root, "a-role", "acme")
    application(root, "other", "Acme Corp")
    application(root, "different", "Other")
    assert jm._related_applications(root, "Acme", "no requisition here") == sorted([a, b])


def test_organization_names_are_matched_literally_not_as_patterns(career):
    root = career.root
    exact = application(root, "x", "A.C.M.E (Inc)")
    application(root, "y", "AxCxMxE (Inc)")
    assert jm._related_applications(root, "A.C.M.E (Inc)", "") == [exact]


def test_a_requisition_in_the_mail_narrows_the_applications(career):
    root = career.root
    one = application(root, "one", "Acme", "JR-10001")
    application(root, "two", "Acme", "JR-20002")
    no_req = application(root, "none", "Acme")
    assert jm._related_applications(root, "Acme", "Re: jr-10001 update") == sorted([one, no_req])
    assert len(jm._related_applications(root, "Acme", "no number")) == 3


def test_related_applications_are_listed_in_a_new_record(career):
    application(career.root, "role", "Acme")
    career.setup({1: raw()})
    jm.fetch_actions(career.settings(), verbose=False)
    assert "- Related applications: applications/role/2026" in records(career.root)[0].read_text()


def message(html="", **kw):
    fields = dict(
        message_id="<link@x>", subject="Next", sender="Careers <jobs@acme.com>",
        received=datetime.now(UTC).date(), body="Please complete the form.", html=html,
        organization="Acme", uid=1, kind="request",
    )  # fmt: skip
    fields.update(kw)
    return jm.CareerMessage(**fields)


def test_a_link_that_arrives_later_is_appended_once_to_an_existing_record(career):
    root = career.root
    first = jm.save_action(root, message())
    assert first is not None and "- Action link:" not in first.read_text()
    html = '<a href="https://acme.com/form">Complete the form</a>'
    assert jm.save_action(root, message(html=html)) is None  # existing record: nothing new
    assert first.read_text().count("- Action link:") == 1
    jm.save_action(root, message(html=html))
    assert first.read_text().count("- Action link:") == 1  # not appended again


def test_at_most_three_links_are_recorded(career):
    links = "".join(f'<a href="https://acme.com/{i}">Complete the form</a>' for i in range(5))
    path = jm.save_action(career.root, message(html=links, message_id="<many@x>"))
    assert path.read_text().count("- Action link:") == 3


def test_losing_a_race_to_create_the_record_is_not_an_error(career, monkeypatch):
    from pathlib import Path

    real_open = Path.open

    def racing(self, mode="r", *args, **kwargs):
        if mode == "x":
            raise FileExistsError(self)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing)
    assert jm.save_action(career.root, message(message_id="<race@x>")) is None


# --- gaps found by mutation testing -------------------------------------------------------


def test_the_default_limit_is_two_hundred_messages(career):
    state = career.setup({i: raw(message_id=f"<m{i}@x>") for i in range(1, 251)})
    jm.fetch_actions(career.settings(), verbose=False, dry_run=True)
    assert len(state.conn.fetches) == 200
    assert min(uid for uid, _ in state.conn.fetches) == 51  # the newest two hundred


def test_a_code_already_saved_is_not_filed_again_without_refile(career):
    state = career.setup({1: raw(**CODE_MAIL)})
    jm.fetch_actions(career.settings(), verbose=False)  # saved earlier, filing off
    jm.fetch_actions(career.settings(file_mail=True), verbose=False)
    assert state.sr.moved == []


def test_a_code_mail_never_also_becomes_an_action_record(career):
    career.setup(
        {
            1: raw(
                subject="Your verification code",
                body="Verification code: ABC12345\nThank you for your application to Acme.",
            )
        }  # fmt: skip
    )
    jm.fetch_actions(career.settings(), verbose=False)
    assert len(codes(career.root)) == 1
    assert records(career.root) == []


def test_a_portal_message_needs_no_career_word_but_an_employer_message_does(career):
    career.setup(
        {
            1: raw(
                sender="no-reply@greenhouse-mail.io",
                subject="Update at Acme",
                body="We regret to inform you.",
                message_id="<portal>",
            ),  # fmt: skip
            2: raw(
                sender="Careers <jobs@acme.com>",
                subject="Update at Acme",
                body="We regret to inform you.",
                message_id="<direct>",
            ),  # fmt: skip
        }
    )
    jm.fetch_actions(career.settings(), verbose=False)
    text = "".join(p.read_text() for p in records(career.root))
    assert "<portal>" in text and "<direct>" not in text


def test_a_link_is_recorded_only_when_its_label_asks_for_an_action():
    parser = jm._ActionLinks("acme.com")
    parser.feed(
        '<a href="https://acme.com/a">Click here</a>'
        '<a href="https://acme.com/b">Complete the form</a>'
        '<a href="https://acme.com/c">Schedule an interview</a>'
    )
    assert [label for label, _ in parser.links] == ["Complete the form", "Schedule an interview"]


@pytest.mark.parametrize(
    ("href", "kept"),
    [
        ("https://anywhere.example/x", True),
        ("http://acme.com/x", True),
        ("http://mail.acme.com/x", True),
        ("http://evil.example/x", False),
        ("http://acme.com.evil.test/x", False),
        ("http://notacme.com/x", False),
        ("javascript:alert(1)", False),
        ("ftp://acme.com/x", False),
        ("", False),
    ],
)
def test_which_links_are_trusted(href, kept):
    parser = jm._ActionLinks("acme.com")
    parser.feed(f'<a href="{href}">Complete the form</a>')
    assert bool(parser.links) is kept


@pytest.mark.parametrize(
    ("sender", "expected"),
    [
        ("x@greenhouse.io", True),
        ("Jobs <x@mail.greenhouse.io>", True),
        ("x@hire.lever.co", True),
        ("x@greenhouse.io.evil.test", False),
        ("x@evilgreenhouse.io", False),
        ("x@acme.com", False),
        ("", False),
    ],
)
def test_a_sender_is_a_portal_only_by_exact_domain(sender, expected):
    assert jm._is_portal(sender) is expected


def test_at_most_three_links_are_appended_to_an_existing_record(career):
    root = career.root
    jm.save_action(root, message(message_id="<late@x>"))
    links = "".join(f'<a href="https://acme.com/{i}">Complete the form</a>' for i in range(5))
    jm.save_action(root, message(html=links, message_id="<late@x>"))
    (path,) = records(root)
    assert path.read_text().count("- Action link:") == 3


def test_related_applications_are_sorted_whatever_order_the_filesystem_lists_them(
    career, monkeypatch
):
    from pathlib import Path

    root = career.root
    a = application(root, "a-role", "Acme")
    b = application(root, "b-role", "Acme")
    real = Path.glob
    monkeypatch.setattr(
        Path, "glob", lambda self, pattern: iter(sorted(real(self, pattern), reverse=True))
    )
    assert jm._related_applications(root, "Acme", "") == [a, b]
