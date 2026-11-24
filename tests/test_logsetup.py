from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

import pytest

from intake import logsetup


@pytest.fixture(autouse=True)
def _restore_streams():
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    out, err = sys.stdout, sys.stderr
    yield
    sys.stdout, sys.stderr = out, err
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()
    if hasattr(logger, "_intake_file_logging"):
        del logger._intake_file_logging


def test_tee_writes_timestamped_lines_and_still_prints(tmp_path, capsys):
    path = logsetup.setup_file_logging({"log_dir": str(tmp_path / "logs")}, base_dir=tmp_path)
    print("skip (no receipt content): 'Hello'")
    print("[finance-trigger] WARNING: boom", file=sys.stderr)
    for handler in logging.getLogger(logsetup.LOGGER_NAME).handlers:
        handler.flush()

    text = path.read_text(encoding="utf-8")
    assert "INFO" in text and "skip (no receipt content): 'Hello'" in text
    assert "WARNING" in text and "[finance-trigger] WARNING: boom" in text
    out = capsys.readouterr()
    assert "skip (no receipt content)" in out.out or "skip (no receipt content)" in out.err


def test_relative_log_dir_is_under_base_and_default_is_data_logs(tmp_path):
    path = logsetup.setup_file_logging({}, base_dir=tmp_path)
    assert path == tmp_path / "data" / "logs" / "intake.log"


def test_empty_log_dir_disables_logging(tmp_path):
    assert logsetup.setup_file_logging({"log_dir": ""}, base_dir=tmp_path) is None
    assert not (tmp_path / "data").exists()


def test_setup_is_idempotent_and_does_not_double_wrap(tmp_path):
    first = logsetup.setup_file_logging({}, base_dir=tmp_path)
    stdout = sys.stdout
    assert logsetup.setup_file_logging({}, base_dir=tmp_path) == first
    assert sys.stdout is stdout
    handlers = logging.getLogger(logsetup.LOGGER_NAME).handlers
    assert sum(isinstance(h, RotatingFileHandler) for h in handlers) == 1


def test_unwritable_log_dir_never_raises(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert (
        logsetup.setup_file_logging({"log_dir": str(blocker / "logs")}, base_dir=tmp_path) is None
    )
    assert "file logging disabled" in capsys.readouterr().err


def test_tee_proxies_stream_attributes(tmp_path):
    logsetup.setup_file_logging({}, base_dir=tmp_path)
    assert isinstance(sys.stdout.isatty(), bool)


def test_log_rotates_at_the_configured_size(tmp_path):
    settings = {"log_dir": str(tmp_path / "logs"), "log_max_bytes": 300, "log_backups": 2}
    logsetup.setup_file_logging(settings, base_dir=tmp_path)
    for index in range(40):
        print(f"line {index} " + "x" * 40)
    assert (tmp_path / "logs" / "intake.log.1").exists()
    assert not (tmp_path / "logs" / "intake.log.3").exists()
