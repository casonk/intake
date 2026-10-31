"""Serve generated intake reports over localhost HTTP."""

from __future__ import annotations

import contextlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CHROME_TAG = b'<script src="/_portfolio/chrome.js" defer></script>'


def with_chrome(document: bytes) -> bytes:
    """Add the portfolio top bar script to a generated report page, which may predate it."""
    if CHROME_TAG in document:
        return document
    lowered = document.lower()
    at = lowered.find(b"</head>")
    return document[:at] + CHROME_TAG + document[at:] if at >= 0 else CHROME_TAG + document


class _ReportHandler(SimpleHTTPRequestHandler):
    """Static file handler that prefers the generated HTML report as the root page."""

    def __init__(self, *args, directory: str, index_name: str = "intake-report.html", **kwargs):
        self._index_name = index_name
        super().__init__(*args, directory=directory, **kwargs)

    def _redirect_to_index(self) -> None:
        self.send_response(302)
        self.send_header("Location", f"/{self._index_name}")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("", "/") and Path(self.directory, self._index_name).exists():
            self._redirect_to_index()
            return
        target = Path(self.translate_path(self.path.split("?", 1)[0].split("#", 1)[0]))
        if target.suffix == ".html" and target.is_file():
            body = with_chrome(target.read_bytes())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if self.path in ("", "/") and Path(self.directory, self._index_name).exists():
            self._redirect_to_index()
            return
        super().do_HEAD()

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        """Suppress the default per-request access log noise."""
        return


def serve_reports(output_dir: Path, *, host: str = "127.0.0.1", port: int = 5200) -> None:
    """Serve *output_dir* over HTTP until interrupted."""
    output_dir.mkdir(parents=True, exist_ok=True)
    handler = partial(_ReportHandler, directory=str(output_dir))
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"intake web : http://{host}:{port}/")
    print(f"report dir : {output_dir}")
    if (output_dir / "intake-report.html").exists():
        print("landing    : /intake-report.html")
    else:
        print("landing    : directory listing (generate a report to add intake-report.html)")
    print("Press Ctrl-C to stop.")
    with contextlib.suppress(KeyboardInterrupt):
        httpd.serve_forever()
