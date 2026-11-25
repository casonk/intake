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


def test_every_mail_page_links_back_to_the_intake_dashboard(server):
    httpd, _ = server
    for path in ("/mail", "/mail/q/inbox", "/mail/search?q=x"):
        _, body, _ = req(httpd, "GET", path)
        assert 'href="/intake-report.html"' in body


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


# -- mailbox actions (stage 3) -----------------------------------------------------------------


class FakeActions:
    def __init__(self):
        self.calls: list[tuple] = []
        self.error: str | None = None

    def _go(self, *call, code):
        if self.error:
            from intake.mail_actions import MailActionError

            raise MailActionError(self.error)
        self.calls.append(call)
        return code

    def return_to_inbox(self, gm):
        return self._go("inbox", gm, code="inbox")

    def file_under(self, gm, queue):
        return self._go("file", gm, queue, code="filed")

    def set_read(self, gm, read):
        return self._go("read" if read else "unread", gm, code="read" if read else "unread")


@pytest.fixture()
def action_server(tmp_path, db_path):
    actions = FakeActions()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(
            _ReportHandler, directory=str(tmp_path), mail_app=MailApp(db_path, actions=actions)
        ),
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, actions
    httpd.shutdown()
    thread.join(timeout=5)
    httpd.server_close()


def test_action_buttons_only_exist_when_actions_are_configured(server, action_server):
    plain, _ = server
    httpd, _ = action_server
    assert "File under" not in req(plain, "GET", "/mail/m/3")[1]
    assert "Return to Inbox" not in req(plain, "GET", "/mail/m/2")[1]
    _, inbox_msg, _ = req(httpd, "GET", "/mail/m/3")
    assert "File under" in inbox_msg and "Return to Inbox" not in inbox_msg
    assert inbox_msg.count("<option") == 4 and 'value="ci"' not in inbox_msg
    _, filed_msg, _ = req(httpd, "GET", "/mail/m/2")
    assert "Return to Inbox" in filed_msg and "File under" not in filed_msg


@pytest.mark.parametrize(
    ("action", "fields", "call", "flash"),
    [
        ("inbox", {}, ("inbox", "2"), "Returned to your Inbox."),
        ("file", {"queue": "promos"}, ("file", "2", "promos"), "Filed."),
        ("read", {}, ("read", "2"), "Marked as read."),
        ("unread", {}, ("unread", "2"), "Marked as unread."),
    ],
)
def test_actions_call_through_and_show_a_fixed_flash(action_server, action, fields, call, flash):
    httpd, actions = action_server
    cookie, token = token_and_cookie(httpd, "/mail/m/2")
    status, _, headers = post(
        httpd, f"/mail/m/2/{action}", {"csrf": token, **fields}, cookie=cookie
    )
    assert status == 303 and headers["location"].startswith("/mail/m/2?done=")
    assert actions.calls == [call]
    assert flash in req(httpd, "GET", headers["location"])[1]


def test_action_routes_enforce_csrf_origin_and_content_type(action_server):
    httpd, actions = action_server
    cookie, token = token_and_cookie(httpd, "/mail/m/2")
    for kwargs, expected in (
        ({"cookie": None}, 403),
        ({"origin": "http://evil.example"}, 403),
        ({"origin": None}, 403),
        ({"ctype": "text/plain"}, 415),
    ):
        args = {"cookie": cookie, **kwargs}
        assert post(httpd, "/mail/m/2/inbox", {"csrf": token}, **args)[0] == expected
    assert post(httpd, "/mail/m/2/inbox", {"csrf": "forged"}, cookie=cookie)[0] == 403
    assert actions.calls == [], "no refused request may reach the mailbox"


def test_a_refused_action_shows_an_escaped_error_and_changes_nothing(action_server):
    httpd, actions = action_server
    actions.error = "<script>alert(5)</script> The mailbox refused it."
    cookie, token = token_and_cookie(httpd, "/mail/m/2")
    status, body, _ = post(httpd, "/mail/m/2/inbox", {"csrf": token}, cookie=cookie)
    assert status == 409 and "Nothing was changed" in body
    assert "<script>alert(5)" not in body and "&lt;script&gt;alert(5)" in body


def test_flash_text_cannot_be_injected_through_the_url(action_server):
    httpd, _ = action_server
    for done in ("<script>alert(6)</script>", "inbox<b>", "../../x", "x" * 500):
        body = req(httpd, "GET", "/mail/m/2?done=" + urlencode({"": done})[1:])[1]
        assert "alert(6)" not in body and "inbox<b>" not in body and "inbox&lt;b" not in body
        for text in ("Returned to your Inbox", "Filed.", "Marked as"):
            assert text not in body, "an unknown code must not produce any flash"


def test_actions_are_404_when_not_configured(server):
    httpd, _ = server
    cookie, token = token_and_cookie(httpd, "/mail/m/3")
    for action in ("inbox", "file", "read", "unread"):
        assert (
            post(httpd, f"/mail/m/3/{action}", {"csrf": token, "queue": "promos"}, cookie=cookie)[0]
            == 404
        )


# -- summaries and TLS cookie (stage 4) ------------------------------------------------------


def test_summary_is_shown_escaped_and_labelled_auto_generated(server, db_path):
    httpd, _ = server
    conn = mi.connect(db_path)
    mi.set_summary(conn, "3", "Family trip planning <b>doc</b>")
    conn.commit()
    conn.close()
    body = req(httpd, "GET", "/mail/m/3")[1]
    assert "auto-generated, local model" in body
    assert (
        "Family trip planning &lt;b&gt;doc&lt;/b&gt;" in body and "planning <b>doc</b>" not in body
    )


def test_csrf_cookie_is_secure_only_behind_the_tls_ingress(server):
    httpd, _ = server
    plain = req(httpd, "GET", "/mail")[2]["set-cookie"]
    behind_tls = req(httpd, "GET", "/mail", headers={"X-Forwarded-Proto": "https"})[2]["set-cookie"]
    assert "Secure" not in plain and "Secure" in behind_tls
    assert "HttpOnly" in behind_tls and "SameSite=Strict" in behind_tls
