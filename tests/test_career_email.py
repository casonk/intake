"""Career follow-up routing and private action records."""

from datetime import date

from intake.career_email import CareerMessage, _employer_for_sender, _is_actionable, save_action


def test_gm_talent_community_follow_up_is_actionable():
    body = (
        "Thank you for expressing interest in GM. To make sure we have the best "
        "information, click the link and complete a short form to add your preferred "
        "career area of interest. Update your profile."
    )
    assert _employer_for_sender("GM Careers <gmcareers@gm.com>", {"gm.com": "GM"}) == "GM"
    assert _is_actionable("We'd like to know more about you", body)
    assert not _is_actionable("Your application was received", "Thank you for applying.")


def test_action_saved_once_and_generic_message_does_not_pick_one_gm_role(tmp_path):
    root = tmp_path / "career"
    for role in ("role-one", "role-two"):
        app = root / "applications" / "jobs" / role / "application.md"
        app.parent.mkdir(parents=True)
        app.write_text(f"---\norganization: GM\nrole: {role}\n---\n", encoding="utf-8")
    (root / "private").mkdir()
    message = CareerMessage(
        "<gm-example>",
        "We'd like to know more about you",
        "GM Careers <gmcareers@gm.com>",
        date(2026, 10, 19),
        "Complete a short form for your career interests. Update your profile.",
        '<a href="https://example.com/profile?token=private">Update your profile</a>',
        "GM",
        123,
    )
    path = save_action(root, message)
    assert path is not None
    saved = path.read_text(encoding="utf-8")
    assert "- [ ] Review and complete" in saved
    assert "jobs/role-one" in saved and "jobs/role-two" in saved
    assert "https://example.com/profile?token=private" in saved
    assert save_action(root, message) is None


def test_http_action_link_allowed_only_for_sender_domain(tmp_path):
    root = tmp_path / "career"
    (root / "private").mkdir(parents=True)
    (root / "applications").mkdir()
    message = CareerMessage(
        "<gm-http>",
        "Update your profile",
        "GM Careers <gmcareers@gm.com>",
        date(2026, 10, 19),
        "Update your career profile.",
        '<a href="http://talent.gm.com/profile">Update your profile</a>'
        '<a href="http://other.example/profile">Update your profile</a>',
        "GM",
        1,
    )
    path = save_action(root, message)
    assert path is not None
    saved = path.read_text(encoding="utf-8")
    assert "http://talent.gm.com/profile" in saved
    assert "other.example" not in saved


def test_portal_senders_name_the_employer_from_the_subject():
    from intake.career_email import _employer_for_sender

    employers = {"gm.com": "GM"}
    greenhouse = "Greenhouse <no-reply@us.greenhouse-mail.io>"
    assert (
        _employer_for_sender(
            greenhouse, employers, "Security code for your application to Latitude AI"
        )
        == "Latitude AI"
    )
    assert (
        _employer_for_sender(greenhouse, employers, "Thank you for applying to Latitude AI!")
        == "Latitude AI"
    )
    workday = "generalmotors@myworkday.com"
    subject = (
        "Your interest in Senior Software Engineer, Rendering Infrastructure (AV Simulation) at GM"
    )
    assert _employer_for_sender(workday, employers, subject) == "GM"
    assert (
        _employer_for_sender(workday, {"generalmotors@myworkday.com": "General Motors"}, "Hello")
        == "General Motors"
    )
    assert (
        _employer_for_sender("news@unrelated.example", employers, "Thanks for applying to X") == ""
    )


def test_kinds_and_codes():
    from intake.career_email import classify, verification_code

    assert (
        classify(
            "Your interest in Role at GM",
            "At this time, we've decided to move forward with other candidates.",
        )
        == "rejection"
    )
    assert (
        classify("Thank you for applying to Latitude AI", "We received your application.")
        == "confirmation"
    )
    assert classify("Next steps", "We'd like to schedule an interview with you.") == "interview"
    assert classify("We'd like to know more about you", "Update your profile") == "request"
    assert classify("Weekly digest", "Top stories") == ""
    body = "Copy and paste this code into the security code field on your application: ndHyXxiC After you enter it"
    assert (
        verification_code("Security code for your application to Latitude AI", body) == "ndHyXxiC"
    )
    assert verification_code("Thank you for applying", body) == ""


def test_codes_go_to_career_owner_only_and_records_carry_their_kind(tmp_path):
    import json
    import stat
    from datetime import UTC, datetime

    from intake.career_email import save_code

    root = tmp_path / "career"
    (root / "applications" / "jobs" / "lat").mkdir(parents=True)
    (root / "applications" / "jobs" / "lat" / "application.md").write_text(
        "---\norganization: Latitude AI\n---\n"
    )
    (root / "private").mkdir()
    received = datetime(2026, 10, 20, 8, 43, 3, tzinfo=UTC)
    later = datetime(2026, 10, 20, 8, 45, tzinfo=UTC)
    sender = "Greenhouse <no-reply@us.greenhouse-mail.io>"
    path = save_code(root, "<m1>", "Latitude AI", "ABCD1234", received, sender, later)
    data = json.loads(path.read_text())
    assert data == {
        "organization": "Latitude AI",
        "code": "ABCD1234",
        "received": "2026-10-20T08:43:03Z",
        "sender": "no-reply@us.greenhouse-mail.io",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        save_code(root, "<m1>", "Latitude AI", "ABCD1234", received, sender, later) is None
    )  # once
    stale = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)
    assert (
        save_code(root, "<m3>", "Latitude AI", "OLD00000", received, sender, stale) is None
    )  # expired
    message = CareerMessage(
        "<m2>",
        "Thank you for applying to Latitude AI",
        "no-reply@us.greenhouse-mail.io",
        date(2026, 10, 20),
        "We received your application.",
        "",
        "Latitude AI",
        2,
        "confirmation",
    )
    saved = save_action(root, message).read_text()
    assert "- Kind: confirmation" in saved and "- [x] Nothing to do (confirmation)" in saved
    assert "jobs/lat" in saved
