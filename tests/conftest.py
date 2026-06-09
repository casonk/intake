"""Shared pytest fixtures for the intake test suite."""

import pytest

from intake.purchaser import register_purchaser_aliases

_TEST_ALIASES = {
    "jane sample": [
        "jane",
        "jane sample",
        "janesample",
        "janesample@gmail.com",
        "jane konzep",
        "sample / jane a",
        "jane a sample",
        "sample jane a",
    ],
    "Alaric g Thornwood": [
        "Alaric g Thornwood",
        "Al Thornwood",
    ],
    "Michael Harrington": [
        "Michael Harrington",
        "Mike Harrington",
        "Mike Harringtonn",
        "Michael Harrington jr",
        "Michael j Harrington jr",
        "Michael j. Harrington jr.",
        "Michael Marcus Harrington jr",
    ],
}


@pytest.fixture(autouse=True)
def _purchaser_aliases():
    register_purchaser_aliases(_TEST_ALIASES)
    yield
    register_purchaser_aliases({})
