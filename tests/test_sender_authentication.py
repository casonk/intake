"""Which replies the receiving server vouches for."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from intake.addresses import sender_is_authenticated

SETTINGS = {"email": {"allowed_addresses": ["me@example.com"]}}
SENDER = "Me <me@example.com>"
GOOD = (
    "mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=example.com; "
    "dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.com"
)


def message(*results: str) -> EmailMessage:
    msg = EmailMessage()
    for value in results:
        msg["Authentication-Results"] = value
    return msg


def test_dmarc_pass_for_the_senders_domain_authenticates():
    assert sender_is_authenticated(SETTINGS, message(GOOD), SENDER)
    assert sender_is_authenticated(SETTINGS, message(GOOD), "ME@EXAMPLE.COM")


@pytest.mark.parametrize(
    "value",
    [
        "mx.google.com; dkim=pass header.i=@example.com; spf=pass; dmarc=fail header.from=example.com",
        "mx.google.com; dkim=pass header.i=@example.com; spf=pass",
        "mx.google.com; dmarc=pass header.from=attacker.example",
        "mx.google.com; dmarc=pass header.from=example.com.attacker.example",
        "mx.google.com; dmarc=pass header.from=sub.example.com",
        "mx.google.com; dmarc=pass header.from=notexample.com",
        "mx.google.com; dmarc=none header.from=example.com",
        "mx.google.com; dmarc=passed header.from=example.com",
        "mx.google.com; xdmarc=pass header.from=example.com",
        "mx.google.com; dmarc=pass xheader.from=example.com",
        "mx.google.com; dmarc=pass header.from=exampleXcom",
        "other.example; dmarc=pass header.from=example.com",
        "evil; x=mx.google.com; dmarc=pass header.from=example.com",
        "",
    ],
)
def test_anything_short_of_a_dmarc_pass_for_that_domain_is_not_authenticated(value):
    assert not sender_is_authenticated(SETTINGS, message(value), SENDER)


def test_dmarc_pass_may_be_the_last_token():
    value = "mx.google.com; header.from=example.com; dmarc=pass"
    assert sender_is_authenticated(SETTINGS, message(value), SENDER)


def test_a_sender_without_a_domain_cannot_match_a_header_from():
    value = "mx.google.com; dmarc=pass header.from=y"
    for sender in ("", "not an address", "x@y@z"):
        assert not sender_is_authenticated(SETTINGS, message(value), sender)


def test_missing_header_or_unusable_sender_is_not_authenticated():
    assert not sender_is_authenticated(SETTINGS, message(), SENDER)
    for sender in ("", None, "not an address", "@example.com"):
        assert not sender_is_authenticated(SETTINGS, message(GOOD), sender)


def test_only_the_topmost_header_counts():
    failing = "mx.google.com; dmarc=fail header.from=example.com"
    assert not sender_is_authenticated(SETTINGS, message(failing, GOOD), SENDER)
    assert sender_is_authenticated(SETTINGS, message(GOOD, failing), SENDER)


def test_whitespace_and_case_are_normalised():
    value = "MX.Google.COM;   DMARC=PASS   (p=none)   Header.From=EXAMPLE.com"
    assert sender_is_authenticated(SETTINGS, message(value), SENDER)


def test_header_from_may_follow_other_tokens_and_a_closing_parenthesis():
    value = "mx.google.com; dmarc=pass (p=NONE header.from=example.com)"
    assert sender_is_authenticated(SETTINGS, message(value), SENDER)


def test_the_trusted_server_is_configurable():
    settings = {"email": {"authentication_server": "mail.example.net"}}
    value = "mail.example.net; dmarc=pass header.from=example.com"
    assert sender_is_authenticated(settings, message(value), SENDER)
    assert not sender_is_authenticated(settings, message(GOOD), SENDER)
    shouting = {"email": {"authentication_server": " MAIL.Example.NET "}}
    assert sender_is_authenticated(shouting, message(value), SENDER)
    blank = {"email": {"authentication_server": "  "}}
    assert sender_is_authenticated(blank, message(GOOD), SENDER)


def test_only_an_explicit_false_turns_the_check_off():
    unauthenticated = message()
    assert sender_is_authenticated(
        {"email": {"require_authenticated_replies": False}}, unauthenticated, SENDER
    )
    for value in (True, None, 0, "false", "no"):
        settings = {"email": {"require_authenticated_replies": value}}
        assert not sender_is_authenticated(settings, unauthenticated, SENDER), value
    assert not sender_is_authenticated({}, unauthenticated, SENDER)
    assert not sender_is_authenticated({"email": "nope"}, unauthenticated, SENDER)
