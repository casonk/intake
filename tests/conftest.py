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


@pytest.fixture(autouse=True)
def _isolated_portfolio_site(tmp_path_factory, monkeypatch):
    """Resolve the portfolio site from an empty store, never this machine's real one."""
    monkeypatch.setenv("PORTFOLIO_STORE", str(tmp_path_factory.mktemp("portfolio-store")))
    monkeypatch.delenv("PORTFOLIO_SITE_DIR", raising=False)
    monkeypatch.delenv("PORTFOLIO_ALLOWED_HOSTS", raising=False)


@pytest.fixture(autouse=True)
def _restore_intake_logging():
    """Keep ``setup_file_logging`` (called by CLI tests) from leaking into later tests.

    It detaches the ``intake`` logger from the root logger and tees stdout/stderr;
    without a restore, ``caplog`` stops seeing intake records depending on test order.
    """
    import logging
    import sys

    logger = logging.getLogger("intake")
    state = (logger.propagate, logger.level, list(logger.handlers), sys.stdout, sys.stderr)
    had_marker = hasattr(logger, "_intake_file_logging")
    yield
    logger.propagate, logger.level = state[0], state[1]
    logger.handlers[:] = state[2]
    sys.stdout, sys.stderr = state[3], state[4]
    if not had_marker and hasattr(logger, "_intake_file_logging"):
        del logger._intake_file_logging
