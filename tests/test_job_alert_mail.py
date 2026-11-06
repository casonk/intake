"""Job-alert digests become private/job-alerts/ records for career's mail-leads.

Reuses the fake mailbox of test_job_search_mail_fetch. Only senders listed in [career.alerts]
are read; a record carries the posting links on the employer's own domain and nothing else.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
import stat

from intake import job_search_mail as jm
from tests.test_job_search_mail_fetch import raw  # career is a fixture

HTML = """<table><style>a {color: #00688C;}</style><tr><td>Welcome to your jobs update.</td></tr>
<tr><td class="reqList"><a href="https://careers.oracle.com/en/sites/jobsearch/job/340493/?utm_medium=career+site&utm_source=job+alert">Pre-Sales Engineer</a><br>
<a href="https://careers.oracle.com/en/sites/jobsearch/job/349331/?utm_medium=career+site&utm_source=job+alert">NetSuite Consultant</a><br>
<a href="https://careers.oracle.com/en/sites/jobsearch/job/340493/?utm_source=again">Pre-Sales Engineer (again)</a><br>
<a href="http://careers.oracle.com/en/sites/jobsearch/job/111/">Plain http</a><br>
<a href="https://elsewhere.example.net/en/sites/jobsearch/job/222/">Other domain</a></td></tr>
<tr><td><a href="https://careers.oracle.com/jobs/#en/sites/jobsearch/requisitions">See all jobs</a>
<a href="https://careers.oracle.com/s/y7t1Qb6f15">unsubscribe</a>
<a href="https://careers.oracle.com/jobs/#en/sites/jobsearch/my-profile/sign-in">job preferences</a>
<a href="mailto:help@oracle.com">mail</a></td></tr></table>"""
SENDER = "Oracle Talent Acquisition <noreply@oracle.com>"
ALERTS = {"noreply@oracle.com": "Oracle"}


def digest(**kw):
    return raw(
        sender=SENDER,
        subject="Your latest Oracle jobs",
        body="Welcome to your jobs update.",
        html=HTML,
        **kw,
    )


def alerts(root):
    return sorted((root / "private" / "job-alerts").glob("*.json"))


def settings(career, **kw):
    s = career.settings(**kw)
    s["career"]["alerts"] = dict(ALERTS)
    return s


def test_only_posting_links_on_the_employers_domain_are_kept():
    links = jm.alert_links("noreply@oracle.com", HTML)
    assert links == [
        ("Pre-Sales Engineer", "https://careers.oracle.com/en/sites/jobsearch/job/340493/"),
        ("NetSuite Consultant", "https://careers.oracle.com/en/sites/jobsearch/job/349331/"),
    ]


def test_a_title_missing_from_the_link_is_taken_from_the_text_before_it():
    html = '<td>Senior Data Engineer<a href="https://jobs.acme.com/jobs/12345"></a></td>'
    assert jm.alert_links("noreply@acme.com", html) == [
        ("Senior Data Engineer", "https://jobs.acme.com/jobs/12345")
    ]


def test_a_digest_is_saved_owner_only_and_labelled_but_left_in_inbox(career):
    state = career.setup({7: digest()})
    saved = jm.fetch_actions(settings(career), verbose=False)
    (path,) = alerts(career.root)
    assert saved == [path]
    record = json.loads(path.read_text())
    assert (record["organization"], record["sender"], record["subject"]) == (
        "Oracle",
        "noreply@oracle.com",
        "Your latest Oracle jobs",
    )
    assert [link["url"] for link in record["links"]] == [
        "https://careers.oracle.com/en/sites/jobsearch/job/340493/",
        "https://careers.oracle.com/en/sites/jobsearch/job/349331/",
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (career.root / "private" / "intake-email").exists()
    assert state.conn.fetches and all(spec == "(BODY.PEEK[])" for _, spec in state.conn.fetches)


def test_filing_labels_the_digest_without_archiving_it(career):
    state = career.setup({7: digest()})
    jm.fetch_actions(settings(career, file_mail=True), verbose=False)
    assert ("create", "Career/Job alerts") in state.conn.ops
    assert ("copy", "7", "Career/Job alerts") in state.conn.ops
    assert state.sr.moved == [] and state.conn.expunged == 0
    assert not any(
        op[0] == "store" for op in state.conn.ops
    )  # unread stays unread, nothing starred


def test_the_same_digest_is_saved_once(career):
    career.setup({7: digest()})
    jm.fetch_actions(settings(career), verbose=False)
    assert jm.fetch_actions(settings(career), verbose=False) == []
    assert len(alerts(career.root)) == 1


def test_dry_run_saves_and_files_nothing(career):
    state = career.setup({7: digest()})
    assert jm.fetch_actions(settings(career, file_mail=True), dry_run=True, verbose=False) == []
    assert alerts(career.root) == [] and state.conn.ops == []


def test_a_digest_without_posting_links_is_ignored(career):
    career.setup(
        {7: raw(sender=SENDER, subject="Welcome", body="hi", html="<p>No roles today</p>")}
    )
    assert jm.fetch_actions(settings(career), verbose=False) == []
    assert alerts(career.root) == []


def test_alert_senders_alone_are_enough_to_run(career):
    state = career.setup({7: digest()})
    s = career.settings()
    s["career"].pop("employers")
    s["career"]["alerts"] = dict(ALERTS)
    assert len(jm.fetch_actions(s, verbose=False)) == 1
    assert any("noreply@oracle.com" in search for search in state.conn.searches)


def test_an_application_reply_from_an_alert_sender_is_still_an_action(career):
    career.setup(
        {
            8: raw(
                sender=SENDER,
                subject="Thank you for applying to Oracle",
                body="We have received your application.",
                html=HTML,
                message_id="<reply@x>",
            )
        }
    )
    s = settings(career)
    s["career"]["employers"] = {"oracle.com": "Oracle"}
    jm.fetch_actions(s, verbose=False)
    assert alerts(career.root) == []
    assert len(list((career.root / "private" / "intake-email").glob("*.md"))) == 1


def test_senders_not_listed_are_never_read(career):
    state = career.setup(
        {7: raw(sender="Jobs <alerts@other.example>", subject="Your latest jobs", html=HTML)}
    )
    assert jm.fetch_actions(settings(career), verbose=False) == []
    assert alerts(career.root) == [] and state.conn.ops == []
