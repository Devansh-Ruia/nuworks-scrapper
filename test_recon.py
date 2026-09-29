import base64
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import recon

HOST = "https://northeastern-csm.symplicity.com"
API = HOST + "/api/v2/jobs?page=1"
B64 = HOST + "/api/v2/jobs?page=2"
NOTEXT = HOST + "/api/v2/jobs/count"
PLAIN = HOST + "/api/v2/ping"
PAGE = HOST + "/students/app/jobs/search"
LOGIN = HOST + "/students/login"
BIG_JSON = '{"a":"' + "x" * 1000 + '"}'
# Each has letters outside 0-9a-f, so a salted hex hash can never contain one by chance.
SECRETS = ("sidsecret", "dark", "secret-bearer", "csrf-secret", "sess-secret", "authkey-secret", "hunter2")


def headers(*pairs):
    return [{"name": n, "value": v} for n, v in pairs]


def entry(method, url, req_headers=(), resp_headers=(), content=None, post=None,
          req_cookies=(), resp_cookies=()):
    req = {"method": method, "url": url, "headers": headers(*req_headers), "cookies": list(req_cookies)}
    if post is not None:
        req["postData"] = post
    return {
        "request": req,
        "response": {"status": 200, "headers": headers(*resp_headers),
                     "cookies": list(resp_cookies), "content": content or {}},
    }


def make_har():
    return {"log": {"entries": [
        entry("GET", PAGE,
              req_headers=[("Accept", "text/html")],
              resp_headers=[("Content-Type", "text/html")],
              content={"mimeType": "text/html", "text": "<html>page</html>"}),
        entry("GET", API,
              req_headers=[("Cookie", "sid=sidsecret; theme=dark"),
                           ("Authorization", "Bearer secret-bearer"),
                           ("X-CSRF-Token", "csrf-secret"),
                           ("X-Session-Id", "sess-secret"),
                           ("X-Auth-Key", "authkey-secret"),
                           ("Accept", "application/json")],
              resp_headers=[("Set-Cookie", "sid=sidsecret; Path=/; Max-Age=3600; HttpOnly"),
                            ("WWW-Authenticate", 'Bearer realm="nuworks"'),
                            ("Content-Type", "application/json; charset=utf-8")],
              req_cookies=[{"name": "sid", "value": "sidsecret"}],
              resp_cookies=[{"name": "sid", "value": "sidsecret", "path": "/"}],
              content={"mimeType": "application/json", "text": BIG_JSON}),
        entry("GET", B64,
              resp_headers=[("Content-Type", "application/json")],
              content={"mimeType": "application/json",
                       "text": base64.b64encode(b'{"ok": true}').decode(), "encoding": "base64"}),
        entry("GET", NOTEXT,
              resp_headers=[("Content-Type", "application/json")],
              content={"mimeType": "application/json"}),
        entry("GET", PLAIN,
              resp_headers=[("Content-Type", "text/plain")],
              content={"mimeType": "text/plain", "text": "not json at all"}),
        entry("POST", LOGIN,
              post={"mimeType": "application/x-www-form-urlencoded",
                    "text": "username=me&password=hunter2",
                    "params": [{"name": "username", "value": "me"},
                               {"name": "password", "value": "hunter2"}]}),
        entry("POST", API,
              post={"mimeType": "application/json", "text": '{"keywords": "python"}'}),
    ]}}


def hdr(e, where, name):
    return next(h["value"] for h in e[where]["headers"] if h["name"].lower() == name)


class ScrubTest(unittest.TestCase):
    def setUp(self):
        self.out = recon.scrub_har(make_har())
        self.api = self.out["log"]["entries"][1]

    def test_removes_secret_values(self):
        dumped = json.dumps(self.out)
        for secret in SECRETS:
            self.assertNotIn(secret, dumped)
        self.assertEqual(hdr(self.api, "request", "accept"), "application/json")
        self.assertEqual(hdr(self.api, "response", "www-authenticate"), 'Bearer realm="nuworks"')

    def test_keeps_cookie_names_and_set_cookie_attributes(self):
        cookie = hdr(self.api, "request", "cookie")
        self.assertRegex(cookie, r"^sid=sha256:[0-9a-f]{8}; theme=sha256:[0-9a-f]{8}$")
        set_cookie = hdr(self.api, "response", "set-cookie")
        self.assertRegex(set_cookie, r"^sid=sha256:[0-9a-f]{8}; Path=/; Max-Age=3600; HttpOnly$")
        self.assertEqual(self.api["response"]["cookies"][0]["name"], "sid")
        self.assertEqual(self.api["response"]["cookies"][0]["path"], "/")

    def test_multi_line_set_cookie(self):
        har = {"log": {"entries": [entry("GET", API, resp_headers=[
            ("set-cookie", "a=1; Path=/\nb=2; Expires=Wed, 01 Jan 2031 00:00:00 GMT")])]}}
        value = hdr(recon.scrub_har(har)["log"]["entries"][0], "response", "set-cookie")
        self.assertRegex(value, r"^a=sha256:\w{8}; Path=/\nb=sha256:\w{8}; Expires=Wed, 01 Jan 2031 00:00:00 GMT$")

    def test_equal_values_hash_equally(self):
        # Same salt within one run, so the same value hashes the same everywhere.
        sid = recon._hash("sidsecret")
        self.assertIn(f"sid={sid};", hdr(self.api, "request", "cookie"))
        self.assertTrue(hdr(self.api, "response", "set-cookie").startswith(f"sid={sid};"))
        self.assertEqual(self.api["request"]["cookies"][0]["value"], sid)
        self.assertEqual(self.api["response"]["cookies"][0]["value"], sid)
        self.assertEqual(recon.scrub_har(make_har()), self.out)
        self.assertNotEqual(recon._hash("sidsecret"), recon._hash("sidsecreu"))

    def test_hash_is_salted(self):
        unsalted = "sha256:" + hashlib.sha256(b"sidsecret").hexdigest()[:8]
        self.assertNotEqual(recon._hash("sidsecret"), unsalted)

    def test_password_post_redacted(self):
        post = self.out["log"]["entries"][5]["request"]["postData"]
        self.assertEqual(post["text"], "[redacted]")
        self.assertEqual([p["value"] for p in post["params"]], ["", ""])
        self.assertEqual([p["name"] for p in post["params"]], ["username", "password"])
        other = self.out["log"]["entries"][6]["request"]["postData"]
        self.assertEqual(other["text"], '{"keywords": "python"}')


class ScrubHarFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "recon.har"

    def write_raw(self):
        self.path.write_text(json.dumps(make_har()), encoding="utf-8")

    def test_success_rewrites_file(self):
        har = recon.scrub_har_file(self.path, self.write_raw)
        text = self.path.read_text(encoding="utf-8")
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(text), har)

    def test_malformed_json_deletes_file(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            recon.scrub_har_file(self.path)
        self.assertFalse(self.path.exists())

    def test_scrub_failure_deletes_file(self):
        self.write_raw()
        with mock.patch("recon.scrub_har", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                recon.scrub_har_file(self.path)
        self.assertFalse(self.path.exists())

    def test_stop_failure_deletes_file(self):
        def stop():
            self.write_raw()
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            recon.scrub_har_file(self.path, stop)
        self.assertFalse(self.path.exists())


class HarFilterTest(unittest.TestCase):
    def test_only_the_nuworks_host(self):
        self.assertTrue(recon.HAR_FILTER.search(HOST + "/api/x"))
        for url in ("https://northeastern-csm.symplicity.com.evil.com/",
                    "https://shibboleth-northeastern-csm.symplicity.com/sso/",
                    "https://neuidmsso.neu.edu/idp/"):
            with self.subTest(url=url):
                self.assertIsNone(recon.HAR_FILTER.search(url))


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self.har = recon.scrub_har(make_har())

    def test_only_tagged_entries(self):
        s = recon.summarize(self.har, {("GET", API), ("GET", B64)})
        self.assertIn("XHR/fetch entries in HAR: 2", s)
        self.assertIn(f"[1] GET {API}", s)
        self.assertIn(f"[2] GET {B64}", s)
        self.assertNotIn(PAGE, s)
        self.assertNotIn(PLAIN, s)
        self.assertNotIn(f"POST {API}", s)
        self.assertIn("request headers: accept, authorization, cookie, x-auth-key, x-csrf-token, x-session-id", s)
        self.assertIn("content-type: application/json; charset=utf-8", s)

    def test_truncates_json_body(self):
        s = recon.summarize(self.har, {("GET", API)})
        self.assertIn('{"a":"' + "x" * 494, s)
        self.assertNotIn("x" * 495, s)
        self.assertIn("truncated, 1008 chars total", s)

    def test_base64_body(self):
        s = recon.summarize(self.har, {("GET", B64)})
        self.assertIn('{"ok": true}', s)

    def test_missing_text_and_non_json(self):
        s = recon.summarize(self.har, {("GET", NOTEXT), ("GET", PLAIN)})
        self.assertIn("body: (no body in HAR)", s)
        self.assertIn("body: skipped (not JSON)", s)
        self.assertNotIn("not json at all", s)

    def test_empty_set(self):
        s = recon.summarize(self.har, set())
        self.assertIn("XHR/fetch entries in HAR: 0", s)
        self.assertIn("No XHR/fetch captured", s)

    def test_off_host_requests_reported(self):
        s = recon.summarize(self.har, {("GET", "https://analytics.example.com/x")})
        self.assertIn("No XHR/fetch captured", s)
        self.assertIn("hosts: analytics.example.com", s)


class CookieTableTest(unittest.TestCase):
    def test_no_values_and_readable_expiry(self):
        cookies = [
            {"name": "JSESSIONID", "value": "raw-secret", "domain": "northeastern-csm.symplicity.com",
             "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"},
            {"name": "remember", "value": "raw-secret-2", "domain": ".symplicity.com",
             "path": "/", "expires": 1893456000.5, "httpOnly": False, "secure": True, "sameSite": "None"},
        ]
        t = recon.cookie_table(cookies)
        self.assertNotIn("raw-secret", t)
        self.assertIn("session", t)
        self.assertIn("2030-01-01 00:00:00 UTC", t)
        self.assertIn("JSESSIONID", t)
        self.assertEqual(recon.cookie_table([]), "Cookies: none")


class FakePage:
    """Stands in for a Playwright page. Fails after about 2 seconds so a stuck wait can't hang the suite."""

    def __init__(self, closed=False, max_calls=200):
        self.closed = closed
        self.calls = 0
        self.max_calls = max_calls

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, ms):
        self.calls += 1
        if self.calls > self.max_calls:
            raise AssertionError("wait_for_enter did not return in time")
        time.sleep(0.01)


class WaitForEnterTest(unittest.TestCase):
    def test_returns_on_enter(self):
        with mock.patch("builtins.input", return_value="") as fake_input, \
                mock.patch("builtins.print") as fake_print:
            self.assertIsNone(recon.wait_for_enter(FakePage(), "go"))
        fake_input.assert_called_once_with()
        fake_print.assert_called_once_with("go")

    def test_closed_page_exits(self):
        entered, release = threading.Event(), threading.Event()

        def blocking_input():
            entered.set()
            release.wait(5)
            return ""

        with mock.patch("builtins.input", side_effect=blocking_input), mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as cm:
                recon.wait_for_enter(FakePage(closed=True), "go", script="fetch_jobs.py")
            # Keep the patch until the reader thread is inside the fake input, so it never reaches the real one.
            self.assertTrue(entered.wait(2))
            release.set()
        self.assertIn("closed", str(cm.exception))
        self.assertIn("Run fetch_jobs.py again", str(cm.exception))

    def test_input_failure_exits(self):
        for exc in (EOFError, OSError):
            with self.subTest(exc=exc.__name__):
                with mock.patch("builtins.input", side_effect=exc), mock.patch("builtins.print"):
                    with self.assertRaises(SystemExit) as cm:
                        recon.wait_for_enter(FakePage(), "go", script="fetch_jobs.py")
                self.assertIn("interactive terminal", str(cm.exception))
                self.assertIn("Run fetch_jobs.py from", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
