"""Tests for intake.web."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer

import pytest

from intake.web import _ReportHandler


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _serve(tmp_path):
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(_ReportHandler, directory=str(tmp_path)),
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def test_root_redirects_to_intake_report(tmp_path):
    (tmp_path / "intake-report.html").write_text("<h1>report</h1>", encoding="utf-8")
    httpd, thread = _serve(tmp_path)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            opener.open(f"http://127.0.0.1:{httpd.server_port}/")
        assert excinfo.value.code == 302
        assert excinfo.value.headers["Location"] == "/intake-report.html"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_root_falls_back_to_directory_listing_without_report(tmp_path):
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    httpd, thread = _serve(tmp_path)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/") as response:
            body = response.read().decode("utf-8")
        assert response.status == 200
        assert "Directory listing for /" in body
        assert "notes.txt" in body
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
