from __future__ import annotations

import json
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from intake import promo_digest as pd

BODY = """View this email in your browser
Jackets & pants - Up to 88% off
Our winter sale ends November 30 on all skiwear and accessories for the season.
Free shipping on orders over $100
Unsubscribe | Privacy policy
© 2026 Example Ski Co. All rights reserved
https://example.com/track/abc
"""


def msg(
    subject="Winter sale", body=BODY, sender="shop@example.com", mid="<1@x>", uid=1
) -> pd.PromoMessage:
    return pd.PromoMessage(mid, uid, sender, subject, date(2026, 11, 22), body)


def test_clean_body_drops_links_boilerplate_and_invisible_padding():
    lines = pd.clean_body(BODY + "‌͏  \n")
    text = "\n".join(lines)
    assert "Up to 88% off" in text and "winter sale ends November 30" in text
    for noise in ("Unsubscribe", "Privacy policy", "©", "https://", "View this email"):
        assert noise not in text


def test_extract_facts_quotes_offer_and_deadline_only_when_stated():
    facts = pd.extract_facts("Winter sale", BODY)
    assert facts["offer"].lower() == "up to 88% off"
    assert facts["deadline"].lower() == "ends november 30"
    assert pd.extract_facts("Hello", "Just a friendly newsletter with no offer.") == {}


def test_extractive_summary_is_the_first_substantive_sentence_not_the_subject():
    summary = pd.extractive_summary(msg(subject="Jackets & pants - Up to 88% off"))
    assert summary.startswith("Our winter sale ends November 30")
    assert pd.extractive_summary(msg(body="Hi\n\n")) == ""


def _reply(text: str):
    return BytesIO(json.dumps({"response": text}).encode())


def test_summarize_uses_the_local_model_and_keeps_asking_while_it_answers():
    ok = [True]
    with patch(
        "intake.promo_digest.urlopen", side_effect=lambda *a, **k: _reply(' "Ski gear sale."')
    ):
        assert pd.summarize(msg(), {}, model_ok=ok) == "Ski gear sale."
    assert ok == [True]


def test_summarize_falls_back_and_stops_calling_an_unreachable_model():
    from urllib.error import URLError

    ok = [True]
    with patch("intake.promo_digest.urlopen", side_effect=URLError("down")) as call:
        first = pd.summarize(msg(), {}, model_ok=ok)
        second = pd.summarize(msg(mid="<2@x>"), {}, model_ok=ok)
    assert first and first == second == pd.extractive_summary(msg())
    assert ok == [False] and call.call_count == 1, "a dead model is asked once per run"


def test_pending_and_mark_digested_round_trip_and_cap(tmp_path):
    state = tmp_path / "data" / "state.json"
    a, b = msg(mid="<a>"), msg(mid="<b>")
    assert pd.pending([a, b], state) == [a, b]
    pd.mark_digested([a], state)
    assert pd.pending([a, b], state) == [b]
    state.write_text("not json")
    assert pd.pending([a, b], state) == [a, b], "a corrupt state file must not crash the job"
    pd.mark_digested([msg(mid=f"<{i}>") for i in range(2100)], state)
    assert len(json.loads(state.read_text())["digested_message_ids"]) == 2000


def test_render_groups_by_sender_and_marks_facts_as_quoted():
    a = msg(subject="Winter sale", mid="<a>")
    b = msg(
        subject="More gear", sender="other@example.org", mid="<b>", body="Plain text only here."
    )
    digest = pd.render_digest([a, b], {"<a>": "Ski gear sale."}, date(2026, 11, 24))
    assert digest.startswith("# Promotions digest — 2026-11-24")
    assert "2 marketing email(s) from 2 sender(s)" in digest
    assert "## shop@example.com (1)" in digest and "## other@example.org (1)" in digest
    assert "**Winter sale** (2026-11-22) — Ski gear sale." in digest
    assert "offer: Up to 88% off" in digest and "deadline: ends November 30" in digest
    assert "not verified" in digest


def test_write_digest_does_not_clobber_a_different_digest_the_same_day(tmp_path):
    first = pd.write_digest("one\n", tmp_path, date(2026, 11, 24))
    again = pd.write_digest("one\n", tmp_path, date(2026, 11, 24))
    other = pd.write_digest("two\n", tmp_path, date(2026, 11, 24))
    assert first == again and other.name == "promos-20261124-2.md"
    assert (tmp_path / "promos-latest.md").read_text() == "two\n"


def test_promo_label_follows_triage_label_settings():
    assert pd.promo_label({}) == "intake/promos"
    custom = {"email": {"triage": {"labels": {"promo": "Mail/Promos"}}}}
    assert pd.promo_label(custom) == "Mail/Promos"


def test_fetch_promos_reads_only_and_parses_dates_and_bodies():
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = "Shop <Shop@Example.com>"
    m["Subject"] = "Winter sale"
    m["Date"] = "Sun, 22 Nov 2026 10:00:00 +0000"
    m["Message-ID"] = "<m@x>"
    m.set_content(BODY)
    raw = m.as_bytes()
    calls: list[tuple] = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def select(self, mailbox, readonly=False):
            calls.append(("select", mailbox, readonly))
            return ("OK", [b"1"])

        def uid(self, command, *args):
            calls.append((command, *args))
            return ("OK", [b"7"] if command == "search" else [raw])

    sr = SimpleNamespace(
        load_config=lambda _p: SimpleNamespace(imap=SimpleNamespace(search_charset=None)),
        validate_imap=lambda _c: None,
        open_imap_connection=lambda _c: Conn(),
        build_search_terms=lambda **kw: ["ALL"],
        parse_int_uid_list=lambda data: [7],
        extract_fetch_payload=lambda fetched: fetched[0],
        decode_mime_header=lambda v: str(v or ""),
        extract_message_bodies=lambda mm: (BODY, None),
    )
    settings = {"email": {"shock_relay_config": "/x.yaml"}}
    with patch("intake.promo_digest.load_shock_relay", return_value=sr):
        found = pd.fetch_promos(settings, since_days=14)

    assert [(f.sender, f.subject, f.sent_date.isoformat()) for f in found] == [
        ("shop@example.com", "Winter sale", "2026-11-22")
    ]
    assert ("select", "intake/promos", True) in calls, "must open the label read-only"
    assert all(c[0] != "store" for c in calls), "must not change flags"
    assert "BODY.PEEK" in str(calls[-1])


def test_promos_manifest_runs_the_send_command_weekly():
    from intake.cli import _build_promos_manifest_text

    text = _build_promos_manifest_text(
        python_executable="/usr/bin/python", on_calendar="Sun *-*-* 19:00:00"
    )
    assert 'name = "intake-promos"' in text
    assert "promos digest --send" in text
    assert 'on_calendar = "Sun *-*-* 19:00:00"' in text


def test_extractive_summary_drops_a_subject_repeated_at_the_start_of_the_line():
    body = "A farming pool is quoting 98% APY today It is number one on the vault board today."
    summary = pd.extractive_summary(
        msg(subject="A farming pool is quoting 98% APY today", body=body)
    )
    assert summary == "It is number one on the vault board today."


def test_newsletter_pipeline_senders_are_not_promotions():
    assert "pulse@mail.tokenmetrics.com" in pd._newsletter_senders()


def test_html_render_formats_markdown_and_escapes_hostile_mail_text():
    from intake.promo_digest import render_html

    digest = (
        "# Promotions digest — 2026-11-24\n\nIntro text.\n\n## a@x.com (1)\n"
        "- **<script>alert(1)</script> 50% off** (2026-11-20) — fine _[offer: 50% off]_\n"
    )
    page = render_html(digest)
    assert "<h1>Promotions digest" in page and "<h2>a@x.com (1)</h2>" in page
    assert "<ul>" in page and "<li><strong>" in page and "<em>[offer: 50% off]</em>" in page
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert 'href="/intake-report.html"' in page


def test_write_digest_writes_html_beside_markdown_unless_disabled(tmp_path):
    from datetime import date

    from intake.promo_digest import write_digest

    out = write_digest("# T\n\n- **x**\n", tmp_path / "a", date(2026, 11, 24))
    assert out.with_suffix(".html").exists() and (tmp_path / "a/promos-latest.html").exists()
    write_digest("# T\n", tmp_path / "b", date(2026, 11, 24), html_page=False)
    assert not list((tmp_path / "b").glob("*.html"))
