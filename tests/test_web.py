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


def test_html_reports_get_the_portfolio_top_bar_without_duplicating_it(tmp_path):
    (tmp_path / "old.html").write_text(
        "<html><head><title>x</title></head><body>b</body></html>", encoding="utf-8"
    )
    (tmp_path / "new.html").write_text(
        '<head><script src="/_portfolio/chrome.js" defer></script></head>', encoding="utf-8"
    )
    (tmp_path / "bare.html").write_text("<p>no head</p>", encoding="utf-8")
    (tmp_path / "data.csv").write_text("a,b", encoding="utf-8")
    httpd, thread = _serve(tmp_path)
    base = f"http://127.0.0.1:{httpd.server_port}/"
    tag = '<script src="/_portfolio/chrome.js" defer></script>'
    try:
        old = urllib.request.urlopen(base + "old.html").read().decode()
        assert old.count(tag) == 1 and old.index(tag) < old.index("</head>")
        assert urllib.request.urlopen(base + "new.html").read().decode().count(tag) == 1
        assert urllib.request.urlopen(base + "bare.html").read().decode().startswith(tag)
        assert urllib.request.urlopen(base + "data.csv").read() == b"a,b"
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base + "gone.html")
        assert missing.value.code == 404
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


# --------------------------------------------------------------------------- DNS rebinding


def _get(httpd, path, host, method="GET"):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
    conn.putrequest(method, path, skip_host=True)
    if host is not None:
        conn.putheader("Host", host)
    conn.endheaders()
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, body


@pytest.fixture
def served(tmp_path):
    (tmp_path / "intake-report.html").write_text("<h1>secret spending</h1>", encoding="utf-8")
    httpd, thread = _serve(tmp_path)
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_a_rebinding_request_cannot_read_the_reports(served):
    for method in ("GET", "HEAD"):
        for path in ("/", "/intake-report.html", "/_portfolio/theme.css"):
            status, body = _get(served, path, f"evil.example:{served.server_port}", method)
            assert status == 403, (method, path)
            assert b"secret spending" not in body


@pytest.mark.parametrize("host", ["localhost", "localhost:5200", "127.0.0.1:5200", "[::1]:5200"])
def test_loopback_hosts_still_serve_the_report(served, host):
    status, body = _get(served, "/intake-report.html", host)
    assert status == 200 and b"secret spending" in body


@pytest.mark.parametrize(
    "host", ["localhost.evil.example", "127.0.0.1.evil.example", "evil.example", "", None]
)
def test_lookalike_or_missing_hosts_are_refused(served, host):
    assert _get(served, "/intake-report.html", host)[0] == 403


def test_the_refusal_does_not_echo_the_host_header(served):
    status, body = _get(served, "/intake-report.html", "<b>x</b>.example")
    assert status == 403 and b"<b>x</b>" not in body


def test_the_registered_hostname_is_served(tmp_path, monkeypatch):
    entry = tmp_path / "owner__repo"
    entry.mkdir()
    (entry / "site.local.toml").write_text('[site]\nhostname = "intake.example.test"\n')
    monkeypatch.setenv("PORTFOLIO_SITE_DIR", str(entry))
    monkeypatch.setenv("PORTFOLIO_STORE", str(tmp_path))
    (tmp_path / "intake-report.html").write_text("<h1>report</h1>", encoding="utf-8")
    httpd, thread = _serve(tmp_path)
    try:
        assert _get(httpd, "/intake-report.html", "intake.example.test")[0] == 200
        assert _get(httpd, "/intake-report.html", "Intake.Example.Test:8443")[0] == 200
        assert _get(httpd, "/intake-report.html", "evil.example")[0] == 403
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_the_guard_can_be_switched_off_only_by_the_server_setup(tmp_path):
    (tmp_path / "intake-report.html").write_text("<h1>report</h1>", encoding="utf-8")
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_ReportHandler, directory=str(tmp_path), enforce_host=False)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        assert _get(httpd, "/intake-report.html", "anything.example")[0] == 200
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


class _CapturingServer:
    def __init__(self, address, handler):
        _CapturingServer.handler = handler

    def serve_forever(self):
        raise KeyboardInterrupt


def test_serve_reports_enforces_on_loopback_and_on_wider_binds_only_with_known_names(
    tmp_path, monkeypatch
):
    from intake import web

    monkeypatch.setattr(web, "ThreadingHTTPServer", _CapturingServer)
    web.serve_reports(tmp_path, host="127.0.0.1", port=1)
    assert _CapturingServer.handler.keywords["enforce_host"] is True
    web.serve_reports(tmp_path, host="::1", port=1)
    assert _CapturingServer.handler.keywords["enforce_host"] is True
    web.serve_reports(tmp_path, host="0.0.0.0", port=1)  # noqa: S104
    assert _CapturingServer.handler.keywords["enforce_host"] is False
    monkeypatch.setenv("PORTFOLIO_ALLOWED_HOSTS", "lan.example.test")
    web.serve_reports(tmp_path, host="0.0.0.0", port=1)  # noqa: S104
    assert _CapturingServer.handler.keywords["enforce_host"] is True
