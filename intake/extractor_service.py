"""The extractor sandbox: a small HTTP service that parses one untrusted document per request.

    python -m intake.extractor_service [--host 0.0.0.0] [--port 5455]

It is meant to run in a container with no network, no secrets and no access to the receipts folder (see
``deploy/k3s``). Each request carries the file's bytes; the service writes them to a private temporary directory
and parses them in a child process (:mod:`intake.extractor_worker`) with a timeout, so a hostile file can hang or
crash a child but not the service.

    GET  /healthz                     200 {"ok": true}
    POST /extract?suffix=.pdf         200 {"text": "..."}
    POST /preview?suffix=.png&resolution=150    200 image/png bytes
    POST /decode?suffix=.jpg          200 {"decodes": true|false}   (any parser failure is "false")

Refusals are JSON ``{"error": ...}``: 400 unsupported type or bad query, 411 no Content-Length, 413 too large,
422 the parser failed or timed out, 503 busy. The error text never contains document content.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from intake.media import IMAGE_RECEIPT_SUFFIXES, SUPPORTED_RECEIPT_SUFFIXES

DEFAULT_PORT = 5455
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 150
DEFAULT_CONCURRENCY = 2
BUSY_WAIT_SECONDS = 5
MAX_RESOLUTION = 300

ENDPOINTS = {"/extract": "text", "/preview": "png", "/decode": "decode"}
WORKER_MODULE = "intake.extractor_worker"


class Extractor:
    """Parses documents through the worker. Separate from the HTTP layer so it can be tested without a socket."""

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        concurrency: int = DEFAULT_CONCURRENCY,
        worker_command: list[str] | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.slots = threading.BoundedSemaphore(concurrency)
        self.worker_command = worker_command or [sys.executable, "-I", "-m", WORKER_MODULE]

    def run(self, kind: str, suffix: str, data: bytes, resolution: int) -> tuple[int, bytes, str]:
        """Return ``(status, body, content_type)`` for one document."""
        if not self.slots.acquire(timeout=BUSY_WAIT_SECONDS):
            return 503, _error("busy: try again"), "application/json"
        workdir = tempfile.mkdtemp(prefix="doc-")
        try:
            os.chmod(workdir, 0o700)
            path = Path(workdir) / f"document{suffix}"
            path.write_bytes(data)
            try:
                done = subprocess.run(  # noqa: S603  (fixed argv; the path is ours, the suffix is whitelisted)
                    [*self.worker_command, kind, str(path), str(resolution)],
                    capture_output=True,
                    timeout=self.timeout,
                    cwd=workdir,
                    env={"PATH": os.environ.get("PATH", ""), "HOME": workdir, "TMPDIR": workdir},
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return self._failed(kind, "the parser timed out")
            if done.returncode != 0:
                return self._failed(kind, "the parser failed")
            content_type = "image/png" if kind == "png" else "application/json"
            return 200, done.stdout, content_type
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            self.slots.release()

    @staticmethod
    def _failed(kind: str, reason: str) -> tuple[int, bytes, str]:
        if kind == "decode":  # "can it be decoded?" has a definite answer when the decoder dies: no
            return 200, json.dumps({"decodes": False}).encode("utf-8"), "application/json"
        return 422, _error(reason), "application/json"


def _error(message: str) -> bytes:
    return json.dumps({"error": message}).encode("utf-8")


def make_handler(extractor: Extractor) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(
            self, fmt: str, *args: object
        ) -> None:  # paths and sizes only, never content
            sys.stderr.write("extractor: " + (fmt % args) + "\n")

        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if urllib.parse.urlsplit(self.path).path == "/healthz":
                self._send(200, b'{"ok": true}')
            else:
                self._send(404, _error("not found"))

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            kind = ENDPOINTS.get(url.path)
            if kind is None:
                return self._send(404, _error("not found"))
            query = urllib.parse.parse_qs(url.query)
            suffix = (query.get("suffix", [""])[0]).lower()
            allowed = IMAGE_RECEIPT_SUFFIXES if kind == "decode" else SUPPORTED_RECEIPT_SUFFIXES
            if suffix not in allowed:
                return self._send(400, _error("unsupported file type"))
            try:
                resolution = int(query.get("resolution", ["150"])[0])
            except ValueError:
                return self._send(400, _error("resolution must be a number"))
            if not 20 <= resolution <= MAX_RESOLUTION:
                return self._send(
                    400, _error(f"resolution must be between 20 and {MAX_RESOLUTION}")
                )
            length_header = self.headers.get("Content-Length")
            if length_header is None or not length_header.isdigit():
                return self._send(411, _error("Content-Length is required"))
            length = int(length_header)
            if length > extractor.max_bytes:
                return self._send(413, _error("document too large"))
            data = self.rfile.read(length)
            if len(data) != length:
                return self._send(400, _error("incomplete body"))
            status, body, content_type = extractor.run(kind, suffix, data, resolution)
            self._send(status, body, content_type)

    return Handler


def make_server(host: str, port: int, extractor: Extractor | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(extractor or Extractor()))
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Listens on every address inside the container; its network policy admits only the node.
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port)
    print(f"extractor sandbox listening on {args.host}:{args.port}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
