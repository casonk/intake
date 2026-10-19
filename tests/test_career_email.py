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
