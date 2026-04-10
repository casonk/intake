"""Filename helpers for receipt artifacts."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

_CANONICAL_STEM_RE = re.compile(r"^(?P<date>\d{8})_(?P<time>\d{4})$")
_DATE_TIME_PREFIX_RE = re.compile(r"^(?P<date>\d{8})[_-](?P<time>\d{4})(?:$|[-_].*)")
_DATE8_PREFIX_RE = re.compile(r"^(?P<date>\d{8})(?:$|[-_].*)")
_DATE6_PREFIX_RE = re.compile(r"^(?P<date>\d{6})(?:$|[-_].*)")


def is_canonical_receipt_filename(filename: str | Path) -> bool:
    """Return True when *filename* matches ``YYYYMMDD_HHMM.EXT``."""
    return _CANONICAL_STEM_RE.fullmatch(Path(filename).stem) is not None


def _parse_date_token(token: str) -> date | None:
    try:
        return datetime.strptime(token, "%Y%m%d").date()
    except ValueError:
        return None


def _parse_time_token(token: str) -> time | None:
    try:
        return datetime.strptime(token, "%H%M").time()
    except ValueError:
        return None


def parse_name_date_time(filename: str | Path) -> tuple[date | None, time | None]:
    """Extract a best-effort date/time pair from a filename stem."""
    stem = Path(filename).stem

    match = _CANONICAL_STEM_RE.fullmatch(stem)
    if match:
        return _parse_date_token(match.group("date")), _parse_time_token(match.group("time"))

    match = _DATE_TIME_PREFIX_RE.match(stem)
    if match:
        return _parse_date_token(match.group("date")), _parse_time_token(match.group("time"))

    match = _DATE8_PREFIX_RE.match(stem)
    if match:
        return _parse_date_token(match.group("date")), None

    match = _DATE6_PREFIX_RE.match(stem)
    if match:
        try:
            return datetime.strptime(match.group("date") + "01", "%Y%m%d").date(), None
        except ValueError:
            return None, None

    return None, None


def infer_artifact_datetime(
    *,
    filename: str | Path | None = None,
    path: Path | None = None,
    default_date: date | None = None,
    default_time: time | None = None,
) -> datetime:
    """Return the best available timestamp for a receipt artifact."""
    source_name = str(filename or (path.name if path is not None else ""))
    name_date, name_time = parse_name_date_time(source_name)

    mtime_dt: datetime | None = None
    if path is not None and path.exists():
        stat_dt = datetime.fromtimestamp(path.stat().st_mtime)
        mtime_dt = stat_dt.replace(second=0, microsecond=0)

    chosen_date = default_date or name_date or (mtime_dt.date() if mtime_dt else date.today())
    if default_time is not None:
        chosen_time = default_time
    elif name_time is not None:
        chosen_time = name_time
    elif mtime_dt is not None:
        chosen_time = time(mtime_dt.hour, mtime_dt.minute)
    else:
        chosen_time = time(0, 0)

    return datetime.combine(chosen_date, chosen_time)


def canonical_filename_for_datetime(dt: datetime, suffix: str) -> str:
    """Render a timestamp and suffix as ``YYYYMMDD_HHMM.EXT``."""
    ext = suffix.lower() or ".pdf"
    if not ext.startswith("."):
        ext = f".{ext}"
    return f"{dt.strftime('%Y%m%d_%H%M')}{ext}"


def allocate_canonical_path(
    dest_dir: Path,
    *,
    base_dt: datetime,
    suffix: str,
    existing_path: Path | None = None,
    reserved_paths: set[Path] | None = None,
) -> Path:
    """Return an available canonical path, advancing minute-by-minute if needed."""
    reserved = reserved_paths if reserved_paths is not None else set()
    candidate_dt = base_dt.replace(second=0, microsecond=0)
    for _ in range(24 * 60 * 14):
        candidate = dest_dir / canonical_filename_for_datetime(candidate_dt, suffix)
        if candidate == existing_path:
            reserved.add(candidate)
            return candidate
        if candidate not in reserved and not candidate.exists():
            reserved.add(candidate)
            return candidate
        candidate_dt += timedelta(minutes=1)
    raise RuntimeError(f"could not allocate canonical filename under {dest_dir}")
