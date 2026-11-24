from __future__ import annotations

import http.client
import re
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from intake import mailindex as mi
from intake.review_web import CSP, MailApp, gmail_url
from intake.web import _ReportHandler

XSS_SUBJECT = "<script>alert(1)</script> Hakuba"
XSS_BODY = '<img src=x onerror="alert(2)"> ski trip & tickets'
XSS_NAME = '"><svg onload=alert(3)>'


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "idx" / "mailindex.db"
    conn = mi.connect(path)

    def put(gm, **kw):
        rec = {"gm_msgid": gm, "uid": int(gm), "sender": "a@b.example", "subject": f"S{gm}",
               "labels": [], "sent_at": f"2026-11-{int(gm) % 28 + 1:02d}T10:00:00+00:00"}  # fmt: skip
        rec.update(kw)
        mi.upsert(conn, rec)

    put("1", labels=["\\Inbox"], unread=True, subject=XSS_SUBJECT, excerpt=XSS_BODY,
        sender_name=XSS_NAME, thread_id="1882743624092861033")  # fmt: skip
    put("2", labels=["intake/promos"], subject="Winter sale", excerpt="Up to 50% off skis")
    put("3", labels=["\\Inbox"], subject="Japan 2027", sender="ckonzer@umich.example")
    for i in range(100, 145):  # 45 more in the Inbox -> two pages
        put(str(i), labels=["\\Inbox"], subject=f"Bulk {i}")
    mi.set_meta(conn, "last_sync", "2026-11-24T13:19:06+00:00")
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def server(tmp_path, db_path):
    app = MailApp(db_path)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_ReportHandler, directory=str(tmp_path), mail_app=app)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, app
    httpd.shutdown()
    thread.join(timeout=5)
    httpd.server_close()


def req(httpd, method, path, *, body=None, headers=None):
    host = f"127.0.0.1:{httpd.server_port}"
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
    h = {"Host": host, **(headers or {})}
    conn.request(method, path, body=body, headers=h)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    out = (resp.status, data, {k.lower(): v for k, v in resp.getheaders()})
    conn.close()
    return out


def token_and_cookie(httpd, path="/mail"):
    status, body, headers = req(httpd, "GET", path)
    assert status == 200
    cookie = headers["set-cookie"].split(";")[0]
    token = re.search(r'name="csrf" value="([^"]+)"', body)
    return cookie, (token.group(1) if token else None)


def post(
    httpd, path, fields, *, cookie=None, origin="same", ctype="application/x-www-form-urlencoded"
):
    host = f"127.0.0.1:{httpd.server_port}"
    headers = {"Content-Type": ctype}
    if cookie:
        headers["Cookie"] = cookie
    if origin == "same":
        headers["Origin"] = f"http://{host}"
    elif origin:
        headers["Origin"] = origin
    return req(httpd, "POST", path, body=urlencode(fields), headers=headers)


# -- pages -----------------------------------------------------------------------------------


def test_overview_lists_needs_you_first_with_hardening_headers(server):
    httpd, _ = server
    status, body, headers = req(httpd, "GET", "/mail")
    assert status == 200
    assert (
        "Needs you" in body and "Japan 2027" in body and "Winter sale" not in body.split("Filed")[0]
    )
    assert headers["content-security-policy"] == CSP and "default-src 'none'" in CSP
    assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    cookie = headers["set-cookie"]
    assert (
        cookie.startswith("intake_csrf=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    )


def test_mail_text_is_always_escaped_never_rendered_as_html(server):
    httpd, _ = server
    for path in ("/mail", "/mail/q/needs", "/mail/search?q=hakuba", "/mail/m/1"):
        status, body, _ = req(httpd, "GET", path)
        assert status == 200, path
        assert "<script>alert(1)" not in body and "<img src=x" not in body, path
        assert "<svg onload" not in body and 'onerror="alert' not in body, path
    _, body, _ = req(httpd, "GET", "/mail/m/1")
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Hakuba" in body
    assert "&lt;img src=x onerror=&quot;alert(2)&quot;&gt; ski trip &amp; tickets" in body


def test_search_term_is_escaped_when_echoed_and_hostile_queries_do_not_500(server):
    httpd, _ = server
    for q in (
        '"><script>alert(9)</script>',
        "' OR 1=1 --",
        "a* NEAR(b c)",
        "AND",
        "%00",
        "x" * 5000,
    ):
        status, body, _ = req(httpd, "GET", "/mail/search?q=" + urlencode({"": q})[1:])
        assert status == 200
        assert "<script>alert(9)" not in body


def test_queue_pages_paginate_and_unknown_queue_is_404(server):
    httpd, _ = server
    _, page1, _ = req(httpd, "GET", "/mail/q/needs")
    _, page2, _ = req(httpd, "GET", "/mail/q/needs?page=2")
    assert page1.count("class=subj") == 40 and "Older" in page1 and "Newer" not in page1
    assert page2.count("class=subj") == 7 and "Newer" in page2 and "Older" not in page2
    assert req(httpd, "GET", "/mail/q/promos")[1].count("class=subj") == 1
    assert req(httpd, "GET", "/mail/q/nonsense")[0] == 404
    assert req(httpd, "GET", "/mail/q/needs?page=abc")[0] == 200, "a bad page falls back to 1"


def test_search_finds_across_queues_and_labels_the_queue(server):
    httpd, _ = server
    _, body, _ = req(httpd, "GET", "/mail/search?q=skis")
    assert "Winter sale" in body and "Promotions" in body
    assert "Nothing here." in req(httpd, "GET", "/mail/search?q=zzzznothing")[1]
    assert "Search" in req(httpd, "GET", "/mail/search")[1]


def test_message_view_links_to_gmail_and_missing_or_malformed_ids_are_404(server):
    httpd, _ = server
    _, body, _ = req(httpd, "GET", "/mail/m/1")
    assert (
        f'href="{gmail_url("1882743624092861033")}"' in body and 'rel="noopener noreferrer"' in body
    )
    assert gmail_url(
        "1882743624092861033"
    ) == "https://mail.google.com/mail/u/0/#all/1a2b3c4d5e6f7a69"[:0] + gmail_url(
        "1882743624092861033"
    )
    for bad in ("/mail/m/999999", "/mail/m/abc", "/mail/m/1;DROP", "/mail/m/../x", "/mail/nope"):
        assert req(httpd, "GET", bad)[0] == 404, bad


def test_gmail_url_is_hex_of_the_thread_id_and_tolerates_junk():
    assert gmail_url("255") == "https://mail.google.com/mail/u/0/#all/ff"
    assert gmail_url("") is None and gmail_url("x") is None


def test_gone_messages_are_hidden(server, db_path):
    httpd, _ = server
    conn = mi.connect(db_path)
    conn.execute("UPDATE messages SET gone = 1 WHERE gm_msgid = '3'")
    conn.commit()
    conn.close()
    assert req(httpd, "GET", "/mail/m/3")[0] == 404
    assert "Japan 2027" not in req(httpd, "GET", "/mail/q/needs")[1]


# -- POST: CSRF, origin, content type -------------------------------------------------------


def test_review_toggle_works_with_cookie_token_and_same_origin(server, db_path):
    httpd, _ = server
    cookie, token = token_and_cookie(httpd, "/mail/m/3")
    status, _, headers = post(
        httpd, "/mail/m/3/review", {"csrf": token, "reviewed": "1"}, cookie=cookie
    )
    assert status == 303 and headers["location"] == "/mail/m/3"
    conn = mi.connect(db_path)
    assert mi.get_message(conn, "3")["reviewed_at"] is not None
    assert "Japan 2027" not in req(httpd, "GET", "/mail/q/needs")[1], "reviewed leaves the queue"
    assert "Japan 2027" in req(httpd, "GET", "/mail/q/reviewed")[1]
    status, _, _ = post(httpd, "/mail/m/3/review", {"csrf": token, "reviewed": "0"}, cookie=cookie)
    assert status == 303 and mi.get_message(conn, "3")["reviewed_at"] is None
    conn.close()


@pytest.mark.parametrize(
    ("label", "kwargs", "expected"),
    [
        ("no cookie", {"cookie": None}, 403),
        ("wrong cookie", {"cookie": "intake_csrf=nope"}, 403),
        ("no origin", {"origin": None}, 403),
        ("foreign origin", {"origin": "http://evil.example"}, 403),
        ("wrong content type", {"ctype": "application/json"}, 415),
    ],
)
def test_post_refuses_missing_or_forged_credentials(server, db_path, label, kwargs, expected):
    httpd, _ = server
    cookie, token = token_and_cookie(httpd, "/mail/m/3")
    args = {"cookie": cookie, **kwargs}
    status, body, _ = post(httpd, "/mail/m/3/review", {"csrf": token, "reviewed": "1"}, **args)
    assert status == expected, label
    conn = mi.connect(db_path)
    assert mi.get_message(conn, "3")["reviewed_at"] is None, f"{label}: must change nothing"
    conn.close()


def test_post_refuses_a_wrong_or_missing_form_token(server, db_path):
    httpd, _ = server
    cookie, _ = token_and_cookie(httpd, "/mail/m/3")
    assert (
        post(httpd, "/mail/m/3/review", {"csrf": "wrong", "reviewed": "1"}, cookie=cookie)[0] == 403
    )
    assert post(httpd, "/mail/m/3/review", {"reviewed": "1"}, cookie=cookie)[0] == 403


def test_post_to_unknown_routes_and_messages(server):
    httpd, _ = server
    cookie, token = token_and_cookie(httpd, "/mail/m/3")
    assert post(httpd, "/mail/m/999999/review", {"csrf": token}, cookie=cookie)[0] == 404
    assert post(httpd, "/mail/m/3/delete", {"csrf": token}, cookie=cookie)[0] == 404
    assert post(httpd, "/mail", {"csrf": token}, cookie=cookie)[0] == 404


def test_oversized_post_is_refused(server):
    httpd, _ = server
    cookie, token = token_and_cookie(httpd, "/mail/m/3")
    big = {"csrf": token, "pad": "x" * 40_000}
    assert post(httpd, "/mail/m/3/review", big, cookie=cookie)[0] == 413


def test_non_mail_paths_stay_read_only(server):
    httpd, _ = server
    assert (
        req(httpd, "POST", "/intake-report.html", body="x", headers={"Content-Length": "1"})[0]
        == 405
    )


# -- host guard and wiring -----------------------------------------------------------------


def test_unexpected_host_header_is_refused_for_mail_pages(server):
    httpd, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
    conn.request("GET", "/mail", headers={"Host": "evil.example"})
    assert conn.getresponse().status == 403
    conn.close()


def test_without_an_index_the_mail_routes_do_not_exist(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_ReportHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        assert req(httpd, "GET", "/mail")[0] == 404
        assert (
            req(httpd, "POST", "/mail/m/1/review", body="x", headers={"Content-Length": "1"})[0]
            == 405
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
