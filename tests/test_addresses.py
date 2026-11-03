"""Which addresses intake may exchange mail with.

The list in ``[email].allowed_addresses`` limits who may drive reply handling and who intake mails.
Because it guards a mailbox, the rule is pinned tightly: a missing list allows nobody, matching is
on the bare address (display names and case do not matter), look-alike addresses never match, and
refusals are reported once on stderr without ever raising for the notification paths.
"""

from __future__ import annotations

import pytest

from intake import addresses as ad

ME = "me@example.com"
SETTINGS = {"email": {"allowed_addresses": [ME, "Second Account <Second@Example.org>"]}}


@pytest.fixture(autouse=True)
def fresh_warnings():
    ad._WARNED.clear()
    yield
    ad._WARNED.clear()


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("me@example.com", "me@example.com"),
        ("  ME@Example.COM ", "me@example.com"),
        ("Alex Sample <Alex@Example.com>", "alex@example.com"),
        ('"Sample, Alex" <alex@example.com>', "alex@example.com"),
        ("not an address", ""),
        ("", ""),
        (None, ""),
        (5, ""),
    ],
)
def test_addresses_are_reduced_to_the_bare_lower_case_form(given, expected):
    assert ad.normalize_address(given) == expected


@pytest.mark.parametrize(
    "settings",
    [{}, {"email": {}}, {"email": None}, {"email": []}, {"email": {"allowed_addresses": []}},
     {"email": {"allowed_addresses": None}}, {"email": {"allowed_addresses": 5}},
     {"email": {"allowed_addresses": {"a": "b"}}}, None],
)  # fmt: skip
def test_with_no_usable_list_nobody_is_allowed(settings):
    assert ad.allowed_addresses(settings) == frozenset()
    assert ad.is_allowed_address(settings, ME) is False


def test_the_list_is_normalised_and_junk_entries_are_ignored():
    settings = {
        "email": {
            "allowed_addresses": [" ME@Example.com", "Name <B@x.example>", "nonsense", "", None, 7]
        }
    }
    assert ad.allowed_addresses(settings) == frozenset({"me@example.com", "b@x.example"})


def test_a_single_string_is_accepted_as_a_one_address_list():
    assert ad.allowed_addresses({"email": {"allowed_addresses": "Me@Example.com"}}) == {ME}


@pytest.mark.parametrize(
    "value",
    ["me@example.com", "ME@EXAMPLE.COM", "Someone Else <me@example.com>", "second@example.org",
     "Second Account <second@example.org>"],
)  # fmt: skip
def test_listed_addresses_match_whatever_their_display_name_or_case(value):
    assert ad.is_allowed_address(SETTINGS, value) is True


@pytest.mark.parametrize(
    "value",
    ["other@example.com", "me@example.com.evil.test", "xme@example.com", "me@mail.example.com",
     "me+tag@example.com", "evil@example.com <me@example.com.evil>", "", None, "me", "@example.com"],
)  # fmt: skip
def test_look_alike_and_empty_addresses_never_match(value):
    assert ad.is_allowed_address(SETTINGS, value) is False


def test_filtering_keeps_the_original_text_of_allowed_entries_and_drops_the_rest(capsys):
    kept = ad.filter_allowed(
        SETTINGS,
        [ME, "stranger@example.net", "Second <second@example.org>"],
        purpose="notification",
    )
    assert kept == [ME, "Second <second@example.org>"]
    assert "stranger@example.net is not in [email].allowed_addresses" in capsys.readouterr().err


def test_each_refusal_is_reported_once_per_purpose(capsys):
    for _ in range(3):
        ad.filter_allowed(SETTINGS, ["stranger@example.net"], purpose="notification")
    assert capsys.readouterr().err.count("stranger@example.net") == 1
    ad.filter_allowed(SETTINGS, ["stranger@example.net"], purpose="digest")
    assert "digest" in capsys.readouterr().err


def test_a_missing_list_is_called_out_as_such(capsys):
    assert ad.filter_allowed({}, [ME], purpose="notification") == []
    err = capsys.readouterr().err
    assert "[email].allowed_addresses is not set" in err and "no notification is sent" in err


def test_filtering_nothing_prints_nothing(capsys):
    assert ad.filter_allowed(SETTINGS, [], purpose="notification") == []
    assert capsys.readouterr().err == ""


def test_requiring_an_allowed_address_returns_it_bare():
    assert ad.require_allowed_address(SETTINGS, "Me <ME@example.com>", purpose="reply") == ME


def test_requiring_a_disallowed_address_raises_with_the_reason():
    with pytest.raises(
        RuntimeError, match="other@example.com is not in .*allowed_addresses.*no file link is sent"
    ):
        ad.require_allowed_address(SETTINGS, "other@example.com", purpose="file link")
    with pytest.raises(RuntimeError, match="allowed_addresses is not set"):
        ad.require_allowed_address({}, ME, purpose="file link")
    with pytest.raises(RuntimeError, match="an empty address"):
        ad.require_allowed_address(SETTINGS, "", purpose="file link")
