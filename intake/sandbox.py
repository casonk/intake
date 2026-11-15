"""Run the untrusted-file parsers (PDF, image, video) in a sandbox service instead of in this process.

Receipts arrive from email attachments and a shared folder, so the PDF, image and video parsers (pdfplumber,
Pillow, tesseract, ffmpeg) read files an attacker can shape. With ``INTAKE_EXTRACTOR=sandbox`` this process never
opens such a file with a parser: it sends the bytes to a service that has no network, no secrets and no access to
the receipts folder, and gets text, PNG bytes or a yes/no back.

* ``local`` (the default): parse in this process, as intake always has. Nothing here is used.
* ``sandbox``: parse in the service at ``INTAKE_EXTRACTOR_URL`` (default ``http://127.0.0.1:5455``).

The sandbox **fails closed**. If it cannot be reached, callers get :class:`SandboxUnavailable` and nothing is
parsed locally instead (a fallback would let a crash steer a hostile file into the weaker place). An unknown
``INTAKE_EXTRACTOR`` value is an error, not a quiet ``local``.

Uses only the standard library; the service side is :mod:`intake.extractor_service`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_MODE = "INTAKE_EXTRACTOR"
ENV_URL = "INTAKE_EXTRACTOR_URL"
ENV_MAX_BYTES = "INTAKE_EXTRACTOR_MAX_BYTES"
ENV_TIMEOUT = "INTAKE_EXTRACTOR_TIMEOUT"

DEFAULT_URL = "http://127.0.0.1:5455"
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 180.0
HEALTH_TIMEOUT_SECONDS = 5.0
MODES = ("local", "sandbox")


class SandboxError(RuntimeError):
    """Base class for sandbox failures."""


class SandboxUnavailable(SandboxError):
    """The sandbox could not be reached, was busy, or timed out: transient, so retry later."""


class SandboxRejected(SandboxError):
    """The sandbox refused or could not parse this document: retrying the same file will not help."""


def mode() -> str:
    value = os.environ.get(ENV_MODE, "local").strip().lower() or "local"
    if value not in MODES:
        raise ValueError(f"{ENV_MODE}={value!r} is not one of {', '.join(MODES)}")
    return value


def enabled() -> bool:
    """True when parsing is delegated to the sandbox."""
    return mode() == "sandbox"


def base_url() -> str:
    return os.environ.get(ENV_URL, "").strip().rstrip("/") or DEFAULT_URL


def _number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def max_bytes() -> int:
    return int(_number(ENV_MAX_BYTES, DEFAULT_MAX_BYTES))


def timeout_seconds() -> float:
    return _number(ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS)


def healthy() -> bool:
    """Whether the sandbox answers its health check. Never raises."""
    try:
        with urllib.request.urlopen(
            f"{base_url()}/healthz", timeout=HEALTH_TIMEOUT_SECONDS
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _read(path: Path) -> bytes:
    """The file's bytes. This is a read, never a parse."""
    size = path.stat().st_size
    if size > max_bytes():
        raise SandboxRejected(f"{path.name} is {size} bytes, over the {max_bytes()}-byte limit")
    return path.read_bytes()


def _post(endpoint: str, path: Path, **query: object) -> bytes:
    data = _read(path)
    params = urllib.parse.urlencode({"suffix": path.suffix.lower(), **query})
    request = urllib.request.Request(
        f"{base_url()}/{endpoint}?{params}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds()) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = _error_text(exc)
        if exc.code in (400, 411, 413, 415, 422):
            raise SandboxRejected(f"{path.name}: {detail}") from exc
        raise SandboxUnavailable(f"sandbox answered {exc.code} for {path.name}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SandboxUnavailable(
            f"cannot reach the extractor sandbox at {base_url()}: {exc}"
        ) from exc


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(exc.read().decode("utf-8", "replace")).get("error", exc.reason))
    except (ValueError, OSError):
        return str(exc.reason)


def extract_text(path: Path) -> str:
    """Text of a receipt, parsed in the sandbox."""
    body = json.loads(_post("extract", path).decode("utf-8"))
    return str(body["text"])


def preview_png(path: Path, resolution: int = 150) -> bytes:
    """A PNG preview rendered in the sandbox. The caller gets bytes and never decodes them here."""
    return _post("preview", path, resolution=int(resolution))


def image_decodes(path: Path) -> bool:
    """Whether Pillow can fully decode the image, asked of the sandbox."""
    return bool(json.loads(_post("decode", path).decode("utf-8"))["decodes"])
