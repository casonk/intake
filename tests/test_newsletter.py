"""Newsletter summaries retain source claims and avoid duplicate delivery."""

import json
from datetime import date
from types import SimpleNamespace

from intake.newsletter import (
    NewsletterIssue,
    _clean_lines,
    _fallback_highlights,
    _is_hump_days_issue,
    file_sent_issues,
    pending_issues,
    render_digest,
    send_digest,
    write_digest,
)


def _issue() -> NewsletterIssue:
    return NewsletterIssue(
        message_id="<weekly@example.com>",
        subject="Weekly Recap: Starknet may leave Ethereum",
        sent_date=date(2026, 10, 18),
        body="\n".join(
            [
                "The Week's Biggest Story",
                "Starknet opened a vote on its Ethereum link. Token holders decide next.",
                "Alpha · 14-day free trial",
                "The Week's 5 Biggest Stories",
                "Source: theblock.co1Interactive Brokers added Ethereum staking.",
                "Source: coindesk.com2Regulators flagged stablecoin stress gaps.",
                "What to Watch Next",
                "Watch for the token-holder vote result.",
            ]
        ),
    )


def test_fallback_highlights_skip_promotions_and_preserve_vote_status():
    summary = _fallback_highlights([_issue()])
    assert "opened a vote" in summary
    assert "holders decide next" in summary
    assert "Interactive Brokers" in summary
    assert "Regulators" in summary
    assert "14-day free trial" not in summary
    assert "Watch for the token-holder vote" in summary
    rendered = render_digest([_issue()], summary, date(2026, 10, 18))
    assert "not independently verified" in rendered


def test_bloomberg_highlight_is_attributed_and_listed():
    issue = NewsletterIssue(
        "<bloomberg@example.com>",
        "Bw Reads: The Reeducation of Marc Andreessen",
        date(2026, 10, 18),
        "Welcome to Bw Reads\n"
        + "Marc Andreessen's politics changed after disputes over tech policy. " * 3,
        "Bloomberg",
    )
    summary = _fallback_highlights([issue])
    assert "**Bloomberg:** Bw Reads" in summary
    assert "politics changed" in summary
    rendered = render_digest([issue], summary, date(2026, 10, 18))
    assert "Bloomberg: Bw Reads" in rendered


def test_hump_days_weekly_brief_ignores_invisible_preheader():
    body = (
        "Happy Wednesday\n\u034f \u2007 \xad\u034f \u2007 \xad\n"
        "The Weekly Brief\n"
        "Federal Student Loan Rules Just Changed\n"
        "The newsletter says parent loan limits changed for the 2026-27 aid cycle.\n"
    )
    assert "" not in _clean_lines(body)
    issue = NewsletterIssue(
        "<hump@example.com>",
        "🎓📚 New Federal Student Loan Rules",
        date(2026, 10, 14),
        body,
        "Hump Days",
    )
    summary = _fallback_highlights([issue])
    assert "**Hump Days:** Federal Student Loan Rules Just Changed" in summary
    assert "parent loan limits" in summary
    assert "Hump Days: 🎓📚 New Federal Student Loan Rules" in render_digest(
        [issue], summary, date(2026, 10, 18)
    )


def test_hump_days_market_report_is_included_and_uses_its_lead():
    body = (
        "Forwarded this email? Subscribe here for more\n"
        "Market Report\n"
        "The AI Trade Is Cracking Under the Weight of Its Own Debt\n"
        "The newsletter reports an eight-day semiconductor selloff.\n"
    )
    assert _is_hump_days_issue(body)
    assert not _is_hump_days_issue("Subscribe here for more\nLimited-time offer")
    issue = NewsletterIssue(
        "<market@example.com>",
        "⛓️‍💥🤖 AI Trade Beginning to Crack?",
        date(2026, 10, 18),
        body,
        "Hump Days",
    )
    summary = _fallback_highlights([issue])
    assert "**Hump Days:** The AI Trade Is Cracking Under the Weight of Its Own Debt" in summary
    assert "eight-day semiconductor selloff" in summary


def test_same_day_supplement_does_not_overwrite_earlier_digest(tmp_path):
    day = date(2026, 10, 18)
    first = write_digest([_issue()], "- First", tmp_path, day)
    second = write_digest([_issue()], "- Second", tmp_path, day)
    assert first.name == "email-highlights-20261018.md"
    assert second.name == "email-highlights-20261018-2.md"
    assert "First" in first.read_text()
    assert "Second" in second.read_text()
    assert write_digest([_issue()], "- Second", tmp_path, day) == second


def test_send_digest_is_once_per_message_id(monkeypatch, tmp_path):
    config = tmp_path / "config.local.yaml"
    config.write_text("imap: {}\n", encoding="utf-8")
    sent = []

    def fake_run(cmd, **kwargs):
        sent.append(cmd)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("intake.newsletter.subprocess.run", fake_run)
    settings = {
        "email": {"shock_relay_config": str(config)},
        "gmail": {"recipient": "reader@example.com"},
    }
    state = tmp_path / "state.json"
    issue = _issue()
    assert send_digest([issue], "Highlights", settings, state, date(2026, 10, 18)) is True
    assert send_digest([issue], "Highlights", settings, state, date(2026, 10, 18)) is False
    assert len(sent) == 1
    assert "reader@example.com" in sent[0]
    assert "X-Portfolio-Service: intake" in sent[0]
    newer = NewsletterIssue(
        "<next@example.com>", "New issue", date(2026, 10, 19), "Lead Change", "Bloomberg"
    )
    assert pending_issues([issue, newer], state) == [newer]


def test_file_sent_issues_uses_processed_label_once(monkeypatch, tmp_path):
    config = tmp_path / "config.local.yaml"
    config.write_text("imap: {}\n", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"sent_message_ids": ["<sent@example.com>"]}), encoding="utf-8")
    issue = NewsletterIssue(
        "<sent@example.com>", "Bw Reads", date(2026, 10, 18), "article", "Bloomberg", "INBOX", 42
    )
    unsent = NewsletterIssue(
        "<unsent@example.com>", "Other", date(2026, 10, 18), "article", "Bloomberg", "INBOX", 43
    )
    calls = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def expunge(self):
            calls.append("expunge")

    provider = SimpleNamespace(
        load_config=lambda _path: object(), open_imap_connection=lambda _cfg: Conn()
    )
    monkeypatch.setattr("intake.newsletter.load_shock_relay", lambda _path: provider)
    monkeypatch.setattr("intake.newsletter.select_mailbox", lambda _conn, _box, **_kw: ("OK", []))
    monkeypatch.setattr(
        "intake.newsletter.file_message_to_processed_label",
        lambda _sr, _conn, uid, **kw: calls.append((uid, kw["processed_label"])) or True,
    )
    settings = {"email": {"shock_relay_config": str(config)}}
    assert file_sent_issues([issue, unsent], settings, state) == 1
    assert calls == [(42, "intake/processed"), "expunge"]
    assert file_sent_issues([issue, unsent], settings, state) == 0
    assert json.loads(state.read_text())["filed_message_ids"] == [issue.message_id]
