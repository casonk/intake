"""Durable, rotating file log for intake runs.

Everything intake prints (daemon polls, per-message verdicts, filing, finance-trigger
warnings) goes to stdout/stderr and so to journald, which rotates and expires on its own
schedule and is awkward to search by message.  This keeps a timestamped, size-rotated copy in
``<log_dir>/intake.log`` so a stranded receipt or a failed downstream trigger can be traced
after the fact.  Nothing is redacted beyond what intake already prints, so the log directory
lives under the gitignored ``data/``.

Settings (all optional, top level)::

    log_dir = "data/logs"      # "" disables file logging
    log_max_bytes = 5242880    # rotate at 5 MiB
    log_backups = 5            # keep intake.log.1 .. intake.log.5
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "intake"
LOG_FILENAME = "intake.log"
DEFAULT_LOG_DIR = "data/logs"
_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


class _Tee:
    """File-like wrapper that forwards to the real stream and logs each completed line."""

    def __init__(self, stream, logger: logging.Logger, level: int) -> None:
        self._stream = stream
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, text: str) -> int:
        written = self._stream.write(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._logger.log(self._level, line.rstrip())
        return written if isinstance(written, int) else len(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str):  # isatty, fileno, encoding, ...
        return getattr(self._stream, name)


def _resolve_log_dir(settings: dict, base: Path) -> Path | None:
    raw = settings.get("log_dir", DEFAULT_LOG_DIR)
    if raw is None or str(raw).strip() == "":
        return None
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else base / path


def setup_file_logging(settings: dict, *, base_dir: Path) -> Path | None:
    """Start the rotating file log and tee stdout/stderr into it; return the log path.

    Idempotent, and never raises: an unwritable directory must not stop the pipeline.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if getattr(logger, "_intake_file_logging", None):
        return logger._intake_file_logging
    log_dir = _resolve_log_dir(settings, base_dir)
    if log_dir is None:
        return None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / LOG_FILENAME
        handler = RotatingFileHandler(
            path,
            maxBytes=int(settings.get("log_max_bytes", 5 * 1024 * 1024)),
            backupCount=int(settings.get("log_backups", 5)),
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        print(f"intake: file logging disabled ({exc})", file=sys.stderr)
        return None
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Teed lines are already on the console; the file handler is the only sink, so a
    # console StreamHandler is deliberately not added (it would print everything twice).
    sys.stdout = _Tee(sys.stdout, logger.getChild("stdout"), logging.INFO)
    sys.stderr = _Tee(sys.stderr, logger.getChild("stderr"), logging.WARNING)
    logger._intake_file_logging = path
    return path
