"""Shared pytest fixtures for the intake test suite."""

from pathlib import Path

import pytest
import toml

from intake.purchaser import register_purchaser_aliases

# Purchaser-alias test data follows the same local-config convention as
# config/settings.toml / config/categories.toml: real name-condensation data
# belongs in a gitignored local file, never hardcoded into a tracked test
# file. tests/local_aliases.toml (gitignored) overrides this; absent that,
# fall back to the tracked example so the suite runs out of the box.
_LOCAL_ALIASES_PATH = Path(__file__).parent / "local_aliases.toml"
_LOCAL_ALIASES_EXAMPLE_PATH = Path(__file__).parent / "local_aliases.toml.example"


def _load_test_aliases() -> dict[str, list[str]]:
    path = _LOCAL_ALIASES_PATH if _LOCAL_ALIASES_PATH.exists() else _LOCAL_ALIASES_EXAMPLE_PATH
    return toml.load(path).get("aliases", {})


_TEST_ALIASES = _load_test_aliases()


@pytest.fixture(autouse=True)
def _purchaser_aliases():
    register_purchaser_aliases(_TEST_ALIASES)
    yield
    register_purchaser_aliases({})
