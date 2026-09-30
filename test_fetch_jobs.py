import csv
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from playwright.sync_api import Error, TimeoutError as PlaywrightTimeoutError

import fetch_jobs
from fetch_jobs import COOP_LIST_URL, DETAIL_URL, LIST_URL

HOST = "https://northeastern-csm.symplicity.com"
SECRET = "Bearer sentinel-auth-value"
AUTH_HEADERS = {"authorization": SECRET, "x-requested-system-user": "students"}


def qualified_url(ids):
    # Written out rather than built from fetch_jobs.QUALIFIED_URL, so the double slash is checked too.
    return HOST + "/api/v2//jobs/discovery/qualified-status?jobs=" + ",".join(ids)


def model(n, **overrides):
    m = {"job_title": f"Engineer {n} ", "name": f"Acme {n} ", "visual_id": str(1000 + n),
         "job_id": f"{n:032x}", "job_location": "Waltham, Massachusetts, United States",
         "symp_remote_onsite": None,
         "jobs_iso_times": {"job_start": "2026-09-25T00:00:00-04:00", "job_end": "2026-10-30T23:59:59-04:00"},
         "deadline": "Oct 30, 2026", "compensation_from": None, "compensation_to": None,
         "compensation_frequency": None, "job_desc": f"<p>Job {n}</p>", "job_type": ["Co-op"]}
    m.update(overrides)
    return m


def list_page(page, ids, total, per_page=20):
    return {"perPage": per_page, "page": page, "total": total, "className": "job",
            "models": [model(i) for i in ids], "metadata": {}}


class FakeFetch:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self.responses[url]


def record(**overrides):
    r = fetch_jobs.to_record(model(1), "Waltham, Massachusetts, United States", "", "f")
    r.update(overrides)
    return r


class HtmlToTextTest(unittest.TestCase):
    def test_blocks_lists_and_breaks(self):
        html = ("<p><strong>Role:</strong> Build things</p><ul><li>Python</li><li>SQL</li></ul>"
                "<p>Line one<br>Line two</p>")
        self.assertEqual(fetch_jobs.html_to_text(html),
                         "Role: Build things\n\n- Python\n- SQL\n\nLine one\nLine two")

    def test_entities(self):
        self.assertEqual(fetch_jobs.html_to_text("<p>It&rsquo;s&nbsp;fast &amp; fun&nbsp;</p>"),
                         "It\u2019s fast & fun")

    def test_links(self):
        self.assertEqual(fetch_jobs.html_to_text('<p>Apply <a href="https://x.example/apply">here</a>.</p>'),
                         "Apply here (https://x.example/apply).")
        self.assertEqual(fetch_jobs.html_to_text('<a href="https://x.example">https://x.example</a>'),
                         "https://x.example")
        self.assertEqual(fetch_jobs.html_to_text('<a href="">plain</a> <a>bare</a>'), "plain bare")

    def test_source_whitespace_and_plain_text(self):
        self.assertEqual(fetch_jobs.html_to_text("<ul>\r\n<li>a</li>\r\n<li>b</li>\r\n</ul>"), "- a\n- b")
        # Both patterns are in the recon.har job descriptions.
        self.assertEqual(fetch_jobs.html_to_text("<ul><li>\n<p>First</p></li><li><p>Second</p></li>"
                                                 "<li>&nbsp;Third</li></ul>"),
                         "- First\n\n- Second\n\n- Third")
        self.assertEqual(fetch_jobs.html_to_text("<ul><li>a</li><li>\r\n<p>&nbsp;</p>\r\n</li></ul><p>Next</p>"), "- a\n\nNext")
        self.assertEqual(fetch_jobs.html_to_text("<p>&nbsp;<strong>Overview</strong></p>"
                                                 "<p>&nbsp;&nbsp;&nbsp;&nbsp;Indented</p>"),
                         "Overview\n\nIndented")
        self.assertEqual(fetch_jobs.html_to_text("Line 1\nLine 2\n\n\n\nLine 3"), "Line 1\nLine 2\n\nLine 3")

    def test_long_input_not_truncated(self):
        html = "".join(f"<p>Paragraph {i} " + "word " * 20 + "</p>" for i in range(300))
        self.assertGreater(len(html), 20_000)
        text = fetch_jobs.html_to_text(html)
        for i in range(300):
            self.assertIn(f"Paragraph {i} ", text)
        self.assertEqual(text.count("word"), 6000)

    def test_empty(self):
        self.assertEqual(fetch_jobs.html_to_text(None), "")
        self.assertEqual(fetch_jobs.html_to_text(""), "")


class ToRecordTest(unittest.TestCase):
    def test_fields(self):
        r = fetch_jobs.to_record(model(7), "Somewhere", "Not Qualified", "f")
        self.assertEqual(list(r), ["title", "employer", "visual_id", "job_id", "qualified", "location",
                                   "remote_onsite", "job_start", "job_end", "deadline", "compensation_from",
                                   "compensation_to", "compensation_frequency", "url", "description"])
        self.assertEqual(list(r), list(fetch_jobs.FIELDS))
        self.assertEqual((r["title"], r["employer"]), ("Engineer 7", "Acme 7"))
        self.assertEqual(r["qualified"], "Not Qualified")
        self.assertEqual(r["location"], "Somewhere")
        self.assertEqual(r["remote_onsite"], "not specified")
        self.assertEqual((r["compensation_from"], r["compensation_to"], r["compensation_frequency"]), ("", "", ""))
        self.assertEqual((r["job_start"], r["job_end"], r["deadline"]),
                         ("2026-09-25T00:00:00-04:00", "2026-10-30T23:59:59-04:00", "Oct 30, 2026"))
        self.assertEqual(r["url"], HOST + "/students/app/jobs/search?perPage=20&page=1&sort=!postdate&ocr=f"
                                          "&currentJobId=" + model(7)["job_id"])
        self.assertEqual(r["description"], "Job 7")

    def test_coop_posting_url(self):
        self.assertEqual(fetch_jobs.to_record(model(7), "x", "", "c")["url"],
                         HOST + "/students/app/jobs/search?perPage=20&page=1&sort=!postdate&ocr=c"
                                "&currentJobId=" + model(7)["job_id"])

    def test_remote_and_compensation(self):
        r = fetch_jobs.to_record(model(1, symp_remote_onsite={"id": "hybrid", "label": "Hybrid"},
                                       compensation_from="140", compensation_to="280",
                                       compensation_frequency="per day"), "x", "", "f")
        self.assertEqual(r["remote_onsite"], "Hybrid")
        self.assertEqual(fetch_jobs.format_compensation(r), "140 to 280 per day")
        self.assertEqual(fetch_jobs.format_compensation(record(compensation_from="64,000")), "64,000")
        self.assertEqual(fetch_jobs.format_compensation(record()), "")


class FetchModelsTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("builtins.print")
        self.printed = patcher.start()
        self.addCleanup(patcher.stop)

    def test_paginates_after_page_2_check(self):
        fetch = FakeFetch({LIST_URL: list_page(1, range(0, 20), 45),
                           LIST_URL + "&page=2": list_page(2, range(20, 40), 45),
                           LIST_URL + "&page=3": list_page(3, range(40, 45), 45)})
        models, note = fetch_jobs.fetch_models(fetch)
        self.assertEqual(fetch.urls, [LIST_URL, LIST_URL + "&page=2", LIST_URL + "&page=3"])
        self.assertEqual([m["job_id"] for m in models], [model(i)["job_id"] for i in range(45)])
        self.assertIn("passed", note)
        self.printed.assert_not_called()

    def test_page_param_ignored_exits(self):
        fetch = FakeFetch({LIST_URL: list_page(1, range(0, 20), 45),
                           LIST_URL + "&page=2": list_page(1, range(20, 40), 45)})
        with self.assertRaises(SystemExit) as cm:
            fetch_jobs.fetch_models(fetch)
        self.assertIn("page param did not work", str(cm.exception))
        self.assertEqual(len(fetch.urls), 2)

    def test_overlapping_or_empty_page_2_exits(self):
        for ids in (range(0, 20), range(15, 35), range(0)):
            with self.subTest(ids=ids):
                fetch = FakeFetch({LIST_URL: list_page(1, range(0, 20), 45),
                                   LIST_URL + "&page=2": list_page(2, ids, 45)})
                with self.assertRaises(SystemExit):
                    fetch_jobs.fetch_models(fetch)
                self.assertEqual(len(fetch.urls), 2)

    def test_single_page_skips_probe(self):
        fetch = FakeFetch({LIST_URL: list_page(1, range(0, 15), 15)})
        models, note = fetch_jobs.fetch_models(fetch)
        self.assertEqual(fetch.urls, [LIST_URL])
        self.assertEqual(len(models), 15)
        self.assertIn("skipped", note)

    def test_dedupes_stops_on_empty_page_and_warns(self):
        fetch = FakeFetch({LIST_URL: list_page(1, range(0, 20), 65),
                           LIST_URL + "&page=2": list_page(2, range(20, 40), 65),
                           LIST_URL + "&page=3": list_page(3, [39, 40], 65),
                           LIST_URL + "&page=4": list_page(4, [], 65)})
        models, _ = fetch_jobs.fetch_models(fetch)
        self.assertEqual(fetch.urls[-1], LIST_URL + "&page=4")
        self.assertEqual(len(fetch.urls), 4)
        self.assertEqual(len(models), 41)
        self.assertIn("41 unique jobs", self.printed.call_args.args[0])
        self.assertIn("total of 65", self.printed.call_args.args[0])

    def fetch_coop(self, responses):
        fetch = FakeFetch(responses)
        with self.assertRaises(SystemExit) as cm:
            fetch_jobs.fetch_models(fetch, COOP_LIST_URL, "Co-op")
        return fetch, str(cm.exception)

    def test_job_type_guard_checks_page_1_before_the_probe(self):
        page1 = list_page(1, range(0, 20), 45)
        page1["models"][3]["job_type"] = ["Full Time / Part Time"]
        page1["models"][4]["job_type"] = None
        fetch, message = self.fetch_coop({COOP_LIST_URL: page1})
        self.assertEqual(fetch.urls, [COOP_LIST_URL])
        self.assertIn("2 of 20 jobs on list page 1 are not Co-op", message)
        self.assertIn("No output files were written", message)

    def test_job_type_guard_stops_on_page_2(self):
        page2 = list_page(2, range(20, 40), 45)
        page2["models"][0]["job_type"] = ["Internship"]
        fetch, message = self.fetch_coop({COOP_LIST_URL: list_page(1, range(0, 20), 45),
                                          COOP_LIST_URL + "&page=2": page2})
        self.assertEqual(fetch.urls, [COOP_LIST_URL, COOP_LIST_URL + "&page=2"])
        self.assertIn("1 of 20 jobs on list page 2", message)

    def test_job_type_guard_stops_on_page_3(self):
        page3 = list_page(3, range(40, 60), 65)
        page3["models"][7]["job_type"] = ["Internship"]
        # No page 4, so a guard that let page 3 through would fail on the lookup.
        fetch, message = self.fetch_coop({COOP_LIST_URL: list_page(1, range(0, 20), 65),
                                          COOP_LIST_URL + "&page=2": list_page(2, range(20, 40), 65),
                                          COOP_LIST_URL + "&page=3": page3})
        self.assertEqual(fetch.urls, [COOP_LIST_URL, COOP_LIST_URL + "&page=2", COOP_LIST_URL + "&page=3"])
        self.assertIn("1 of 20 jobs on list page 3", message)

    def test_job_type_guard_accepts_multiple_labels(self):
        page1 = list_page(1, range(0, 15), 15)
        for m in page1["models"]:
            m["job_type"] = ["Co-op", "Full Time / Part Time"]
        models, _ = fetch_jobs.fetch_models(FakeFetch({COOP_LIST_URL: page1}), COOP_LIST_URL, "Co-op")
        self.assertEqual(len(models), 15)

    def test_no_job_type_never_checks(self):
        page1 = list_page(1, range(0, 15), 15)
        page1["models"][0]["job_type"] = ["Internship"]
        del page1["models"][1]["job_type"]
        models, _ = fetch_jobs.fetch_models(FakeFetch({LIST_URL: page1}))
        self.assertEqual(len(models), 15)


class FetchQualifiedTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("builtins.print")
        self.printed = patcher.start()
        self.addCleanup(patcher.stop)

    def test_batches_of_40(self):
        ids = [model(i)["job_id"] for i in range(85)]
        batches = [ids[0:40], ids[40:80], ids[80:85]]
        fetch = FakeFetch({qualified_url(b): {"qualified": dict.fromkeys(b, "Not Qualified")} for b in batches})
        statuses, calls = fetch_jobs.fetch_qualified(ids, fetch)
        self.assertEqual(calls, 3)
        self.assertEqual(fetch.urls, [qualified_url(b) for b in batches])
        self.assertEqual([len(url.split("jobs=")[1].split(",")) for url in fetch.urls], [40, 40, 5])
        self.assertEqual(statuses, dict.fromkeys(ids, "Not Qualified"))
        self.printed.assert_not_called()

    def test_missing_ids_are_marked_and_warned(self):
        ids = [model(i)["job_id"] for i in range(3)]
        fetch = FakeFetch({qualified_url(ids): {"qualified": {ids[0]: "", ids[2]: "Campus Interviews"}}})
        statuses, calls = fetch_jobs.fetch_qualified(ids, fetch)
        self.assertEqual(calls, 1)
        self.assertEqual(statuses, {ids[0]: "", ids[1]: "not returned", ids[2]: "Campus Interviews"})
        self.assertIn("did not return 1 of 3 jobs", self.printed.call_args.args[0])

    def test_empty_list_is_an_empty_map(self):
        ids = [model(i)["job_id"] for i in range(3)]
        statuses, _ = fetch_jobs.fetch_qualified(ids, FakeFetch({qualified_url(ids): {"qualified": []}}))
        self.assertEqual(statuses, dict.fromkeys(ids, "not returned"))
        self.assertIn("did not return 3 of 3 jobs", self.printed.call_args.args[0])

    def test_malformed_response_exits(self):
        ids = [model(i)["job_id"] for i in range(45)]
        good = {"qualified": dict.fromkeys(ids[:40], "")}
        for bad in ({"total": 5}, {"qualified": "Not Qualified"}, [{"qualified": {}}]):
            with self.subTest(bad=bad):
                # The bad batch is the second one, so it can't pass on what the first returned.
                fetch = FakeFetch({qualified_url(ids[:40]): good, qualified_url(ids[40:]): bad})
                with self.assertRaises(SystemExit) as cm:
                    fetch_jobs.fetch_qualified(ids, fetch)
                self.assertIn(qualified_url(ids[40:]), str(cm.exception))
                self.assertIn("No output files were written", str(cm.exception))


class ResolveLocationsTest(unittest.TestCase):
    def test_only_multiple_locations_fetch_detail(self):
        models = [model(0), model(1, job_location="Multiple Locations"), model(2),
                  model(3, job_location="Multiple Locations")]
        url1, url3 = (DETAIL_URL.format(job_id=model(n)["job_id"], ocr="f") for n in (1, 3))
        fetch = FakeFetch({
            url1: {"location": [{"address_text": "New York, NY, USA"}, {"address_text": "San Francisco, CA, USA"}]},
            url3: {"location": []},
        })
        locations, calls = fetch_jobs.resolve_locations(models, fetch, "f")
        self.assertEqual(calls, 2)
        self.assertEqual(fetch.urls, [url1, url3])
        self.assertEqual(url1, HOST + f"/api/v3/jobs/{model(1)['job_id']}?ocr=f")
        self.assertEqual(locations, ["Waltham, Massachusetts, United States",
                                     "New York, NY, USA; San Francisco, CA, USA",
                                     "Waltham, Massachusetts, United States",
                                     "Multiple Locations"])


class WritersTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        desc = 'Line one, with "quotes" and commas, too.\n\n- item, two\n- caf\u00e9'
        self.records = [record(description=desc, compensation_from="140", compensation_to="280",
                               compensation_frequency="per day", qualified="Not Qualified"),
                        record(title="Analyst", employer="Beta", job_id="b" * 32, description="Second job text")]

    def test_csv_round_trip(self):
        path = self.dir / "jobs.csv"
        fetch_jobs.write_csv(self.records, path)
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            self.assertEqual(tuple(reader.fieldnames), fetch_jobs.FIELDS)
            self.assertEqual(list(reader), self.records)

    def test_markdown(self):
        path = self.dir / "jobs_for_review.md"
        fetch_jobs.write_markdown(self.records, path, fetch_jobs.QUALIFY)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Jobs I Qualify For (2 jobs)\n"))
        self.assertIn(f"Source: NUworks Jobs I Qualify For list ({HOST}/students/app/jobs/search?ocr=f).", text)
        self.assertIn("\n" + fetch_jobs.BLANK_NOTE + "\n", text)
        self.assertEqual([line for line in text.splitlines() if line.startswith("## ")],
                         ["## Engineer 1 at Acme 1", "## Analyst at Beta"])
        self.assertIn(self.records[0]["description"], text)
        self.assertIn("- compensation: 140 to 280 per day", text)
        first, second = text.split("\n## ")[1:]
        self.assertIn("- qualified status: Not Qualified\n", first)
        self.assertIn("- qualified status: (blank)\n", second)
        self.assertIn("### Description", second)
        self.assertNotIn("compensation", second)
        self.assertIn("- posting URL: " + self.records[1]["url"], second)

    def test_coop_markdown(self):
        path = self.dir / "jobs_for_review.md"
        fetch_jobs.write_markdown(self.records, path, fetch_jobs.COOP)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# All Job Listings, Co-op (2 jobs)\n"))
        self.assertIn("Source: NUworks All Job Listings list filtered to Position Type Co-op "
                      f"({HOST}/students/app/jobs/search?ocr=c).", text)


class FakeResponse:
    def __init__(self, status=200, text=""):
        self.status = status
        self.ok = 200 <= status < 300
        self._text = text

    def text(self):
        return self._text


class FetchJsonTest(unittest.TestCase):
    URL = LIST_URL

    def setUp(self):
        patcher = mock.patch.object(fetch_jobs, "REQUEST_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def api(self, **kw):
        return SimpleNamespace(get=mock.Mock(**kw))

    def test_returns_json_and_sends_headers(self):
        api = self.api(return_value=FakeResponse(200, '{"total": 1}'))
        self.assertEqual(fetch_jobs.fetch_json(api, self.URL, AUTH_HEADERS), {"total": 1})
        api.get.assert_called_once_with(self.URL, headers=AUTH_HEADERS)

    def test_errors_name_url_and_status_but_no_headers(self):
        # Real Playwright errors end in a call log like this. Cookies are not in our headers dict, so
        # only dropping the log keeps them out.
        call_log = (f"APIRequestContext.get: connect ECONNREFUSED\nCall log:\n  - authorization: {SECRET}\n"
                    "  - cookie: sid=cookie-secret")
        cases = [(self.api(return_value=FakeResponse(401, "denied")), "HTTP 401"),
                 (self.api(return_value=FakeResponse(200, "<html>login</html>")), "not JSON"),
                 (self.api(side_effect=Error(call_log)), "ECONNREFUSED"),
                 (self.api(side_effect=Error(f"odd first line {SECRET}")), "[hidden]")]
        for api, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(SystemExit) as cm:
                    fetch_jobs.fetch_json(api, self.URL, AUTH_HEADERS)
                message = str(cm.exception)
                self.assertIn(expected, message)
                self.assertIn(self.URL, message)
                self.assertNotIn(SECRET, message)
                self.assertNotIn("sentinel", message)
                self.assertNotIn("cookie-secret", message)
                if api.get.side_effect:
                    self.assertTrue(cm.exception.__suppress_context__)


# Stand-ins for the Playwright objects main() touches.
APP_HEADERS = dict(AUTH_HEADERS, cookie="sid=cookie-secret", accept="application/json")


class FakeAppRequest:
    def __init__(self, url, headers, status=200):
        self.url, self.headers, self.status = url, headers, status

    def all_headers(self):
        return dict(self.headers)

    def response(self):
        return FakeResponse(self.status)


class FakeExpect:
    def __init__(self, page, predicate):
        self.page, self.predicate = page, predicate

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *rest):
        if exc_type is None:
            self.value = next((r for r in self.page.sent if self.predicate(r)), None)
            if self.value is None:
                raise PlaywrightTimeoutError('Timeout 20000ms exceeded while waiting for event "request"')
        return False


# "sentinel" marks what must never be printed. Real IdP and Duo URLs carry session ids and SAML requests.
IDP_URL = ("https://neuidmsso.neu.edu/idp/profile/SAML2/Redirect/SSO;jsessionid=sentinel-jsid"
           "?SAMLRequest=sentinel-saml&execution=e1s1")
DUO_URL = "https://api-x.duosecurity.com/frame/v4/auth/prompt?sid=sentinel-duo-sid"
# Playwright call logs quote the URLs they navigated to.
CALL_LOG = f'\nCall log:\n  - navigated to "{IDP_URL}"'
# Pages after the SSO click, as (url, visible username/password boxes, child frame urls).
LOADING = (IDP_URL, 0, [])
PASSWORD = (IDP_URL, 2, [])
HOME = (HOST + "/students/", 0, [])


class FakePage:
    """Has no fill, type, press, check or keyboard, so any attempt to use them fails the test."""

    def __init__(self, logged_in=True, app_status=200, steps=(), login_url=fetch_jobs.LOGIN_URL, button=True):
        self.logged_in, self.app_status = logged_in, app_status
        self.steps, self.login_url, self.button = list(steps), login_url, button
        self.visited, self.sent, self.clicks, self.waits = [], [], [], []
        self.url, self.inputs, self.frame_urls = "about:blank", 0, []

    @property
    def frames(self):
        return [SimpleNamespace(url=url) for url in (self.url, *self.frame_urls)]

    def expect_request(self, predicate, timeout=None):
        return FakeExpect(self, predicate)

    def goto(self, url):
        self.visited.append(url)
        self.sent = []
        self.url = self.login_url if url == fetch_jobs.LOGIN_URL else url
        if url == fetch_jobs.SEARCH_URL and self.logged_in:
            # The SPA's other list request, without ocr=f, must not be picked.
            self.sent = [FakeAppRequest(HOST + "/api/v2/jobs?json_mode=read_only&enable_translation=false",
                                        dict(APP_HEADERS, authorization="wrong-list")),
                         FakeAppRequest(LIST_URL, APP_HEADERS, self.app_status)]

    def get_by_role(self, role, name, exact=None):
        return SimpleNamespace(click=lambda timeout: self._click(role, name, exact))

    def _click(self, role, name, exact):
        # AssertionError, not Error, so sso_login can't swallow it.
        assert fetch_jobs._host(self.url) == fetch_jobs.NUWORKS_HOST, "clicked off NUworks"
        self.clicks.append((role, name, exact))
        if not self.button:
            raise PlaywrightTimeoutError("Timeout 30000ms exceeded waiting for get_by_role" + CALL_LOG)

    def wait_for_url(self, predicate, wait_until, timeout):
        # The click's navigation lands here, so code that skips this wait still sees the login page.
        self._next()
        if not predicate(self.url):
            raise PlaywrightTimeoutError("Timeout 30000ms exceeded waiting for navigation" + CALL_LOG)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        self._next()

    def wait_for_load_state(self):
        pass

    def locator(self, selector):
        return SimpleNamespace(filter=lambda visible: SimpleNamespace(count=self._count))

    def _count(self):
        if isinstance(self.inputs, Exception):
            raise self.inputs
        return self.inputs

    def _next(self):
        # Stays on the last step once the list runs out.
        self.url, self.inputs, self.frame_urls = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        # Any NUworks page but the login page means SSO gave NUworks a session.
        if fetch_jobs._host(self.url) == fetch_jobs.NUWORKS_HOST and self.url != fetch_jobs.LOGIN_URL:
            self.logged_in = True


def log_in(page, prompt, script):
    page.logged_in = True


class GetHeadersTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("builtins.print")
        self.printed = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        printed = [str(arg) for c in self.printed.call_args_list for arg in (*c.args, *c.kwargs.values())]
        self.assertFalse([p for p in printed if "sentinel" in p], "a URL, error or header was printed")

    def get_headers(self, page):
        with mock.patch("fetch_jobs.wait_for_enter", side_effect=log_in) as self.prompt:
            return fetch_jobs.get_headers(page)

    def assert_prompted(self, page):
        self.prompt.assert_called_once_with(page, fetch_jobs.LOGIN_PROMPT, script="fetch_jobs.py")

    def assert_stopped_on_sight(self, page):
        # One poll on the first page, then the next page is recognized. The timeout would also end in a prompt.
        self.assertEqual(page.waits, [300])

    def test_keeps_only_auth_headers_from_the_ocr_request(self):
        self.assertEqual(fetch_jobs.capture_headers(FakePage()), AUTH_HEADERS)

    def test_rejected_or_missing_request_is_none(self):
        self.assertIsNone(fetch_jobs.capture_headers(FakePage(app_status=401)))
        self.assertIsNone(fetch_jobs.capture_headers(FakePage(logged_in=False)))
        page = FakePage()
        page.goto = mock.Mock(side_effect=Error("Navigation is interrupted by another navigation"))
        self.assertIsNone(fetch_jobs.capture_headers(page))

    def test_sso_session_valid_needs_no_prompt(self):
        page = FakePage(logged_in=False, steps=[LOADING, HOME])
        self.assertEqual(self.get_headers(page), AUTH_HEADERS)
        self.prompt.assert_not_called()
        self.assertEqual(page.visited, [fetch_jobs.SEARCH_URL, fetch_jobs.LOGIN_URL, fetch_jobs.SEARCH_URL])
        self.assertEqual(page.clicks, [("button", "Current Students and Alumni", True)])
        self.assertEqual(self.printed.call_args_list, [mock.call(fetch_jobs.SSO_MSG)])

    def test_sp_session_valid_redirects_straight_home(self):
        # /sso/ can 302 back to NUworks without any page off the host.
        page = FakePage(logged_in=False, steps=[HOME])
        self.assertEqual(self.get_headers(page), AUTH_HEADERS)
        self.prompt.assert_not_called()

    def test_back_on_login_page_prompts(self):
        page = FakePage(logged_in=False, steps=[LOADING, (fetch_jobs.LOGIN_URL, 2, [])])
        self.assertEqual(self.get_headers(page), AUTH_HEADERS)
        self.assert_prompted(page)
        self.assertEqual(page.visited, [fetch_jobs.SEARCH_URL, fetch_jobs.LOGIN_URL, fetch_jobs.SEARCH_URL,
                                        fetch_jobs.SEARCH_URL])

    def test_nuworks_password_box_after_return_is_not_a_prompt(self):
        page = FakePage(logged_in=False, steps=[LOADING, (HOST + "/students/", 2, [])])
        self.assertEqual(self.get_headers(page), AUTH_HEADERS)
        self.prompt.assert_not_called()

    def test_login_then_retry(self):
        page = FakePage(logged_in=False, steps=[LOADING, PASSWORD])
        self.assertEqual(self.get_headers(page), AUTH_HEADERS)
        self.assert_prompted(page)
        self.assert_stopped_on_sight(page)
        self.assertIn("Current Students and Alumni", fetch_jobs.LOGIN_PROMPT)
        # No navigation after the password page, so the user keeps their place.
        self.assertEqual(page.visited, [fetch_jobs.SEARCH_URL, fetch_jobs.LOGIN_URL, fetch_jobs.SEARCH_URL])
        self.assertEqual(len(page.clicks), 1)

    def test_duo_prompts(self):
        for step in ((IDP_URL, 0, [DUO_URL]), (DUO_URL, 0, [])):
            with self.subTest(step=step):
                page = FakePage(logged_in=False, steps=[LOADING, step])
                self.assertEqual(self.get_headers(page), AUTH_HEADERS)
                self.assert_prompted(page)
                self.assert_stopped_on_sight(page)

    def test_missing_button_prompts(self):
        page = FakePage(logged_in=False, button=False)
        self.assertEqual(self.get_headers(page), AUTH_HEADERS)
        self.assert_prompted(page)

    def test_login_url_off_nuworks_prompts_without_clicking(self):
        page = FakePage(logged_in=False, login_url=IDP_URL)
        self.assertEqual(self.get_headers(page), AUTH_HEADERS)
        self.assert_prompted(page)
        self.assertEqual(page.clicks, [])

    def test_unclassifiable_page_prompts_after_timeout(self):
        page = FakePage(logged_in=False, steps=[LOADING])
        with mock.patch.object(fetch_jobs, "SSO_TIMEOUT_MS", 900):
            self.assertEqual(self.get_headers(page), AUTH_HEADERS)
        self.assert_prompted(page)
        self.assertEqual(page.waits, [300] * 3)

    def test_navigation_error_mid_check_retries_next_poll(self):
        destroyed = Error("Execution context was destroyed, most likely because of a navigation" + CALL_LOG)
        page = FakePage(logged_in=False, steps=[(IDP_URL, destroyed, []), PASSWORD])
        self.assertEqual(self.get_headers(page), AUTH_HEADERS)
        self.assert_prompted(page)
        self.assert_stopped_on_sight(page)

    def test_still_logged_out_exits(self):
        page = FakePage(logged_in=False, steps=[LOADING, PASSWORD])
        with mock.patch("fetch_jobs.wait_for_enter"):
            with self.assertRaises(SystemExit) as cm:
                fetch_jobs.get_headers(page)
        self.assertEqual(str(cm.exception), fetch_jobs.NO_HEADERS_MSG)


class FakeAPI:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append((url, headers))
        return FakeResponse(200, json.dumps(self.responses[url]))


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, old)
        self.dir = Path(tmp.name)
        for target, value in (("fetch_jobs.REQUEST_DELAY", 0), ("sys.stdout", mock.MagicMock())):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, responses, argv=()):
        page = FakePage()
        self.api = FakeAPI(responses)
        self.context = SimpleNamespace(pages=[page], request=self.api, close=mock.Mock())
        chromium = SimpleNamespace(launch_persistent_context=lambda **kw: self.context)

        @contextmanager
        def fake_sync_playwright():
            yield SimpleNamespace(chromium=chromium)

        with mock.patch("fetch_jobs.sync_playwright", fake_sync_playwright), \
                mock.patch("builtins.print") as printed:
            try:
                fetch_jobs.main(list(argv))
            finally:
                self.output = "\n".join(" ".join(map(str, c.args)) for c in printed.call_args_list)

    def read_outputs(self):
        with open(self.dir / fetch_jobs.CSV_PATH, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        md = (self.dir / fetch_jobs.MD_PATH).read_text(encoding="utf-8")
        for text in (self.output, md, (self.dir / fetch_jobs.CSV_PATH).read_text(encoding="utf-8")):
            self.assertNotIn("sentinel", text)
        return rows, md

    def test_writes_both_files_without_the_auth_value(self):
        page1 = list_page(1, range(0, 20), 25)
        page1["models"][1]["job_location"] = "Multiple Locations"
        detail = DETAIL_URL.format(job_id=model(1)["job_id"], ocr="f")
        ids = [model(i)["job_id"] for i in range(25)]
        statuses = dict.fromkeys(ids, "")
        statuses[ids[0]] = "Not Qualified"
        self.run_main({LIST_URL: page1, LIST_URL + "&page=2": list_page(2, range(20, 25), 25),
                       detail: {"location": [{"address_text": "New York, NY, USA"}]},
                       qualified_url(ids): {"qualified": statuses}})
        self.assertEqual([url for url, _ in self.api.calls],
                         [LIST_URL, LIST_URL + "&page=2", detail, qualified_url(ids)])
        self.assertTrue(all(h == AUTH_HEADERS for _, h in self.api.calls))
        self.context.close.assert_called_once_with()
        rows, md = self.read_outputs()
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows[1]["location"], "New York, NY, USA")
        self.assertEqual((rows[0]["qualified"], rows[1]["qualified"]), ("Not Qualified", ""))
        self.assertTrue(md.startswith("# Jobs I Qualify For (25 jobs)\n"))
        self.assertEqual(sum(line.startswith("## ") for line in md.splitlines()), 25)
        self.assertEqual(md.count("- qualified status: (blank)\n"), 24)
        self.assertIn("Fetched 25 jobs", self.output)
        self.assertIn("Page 2 check: passed", self.output)
        self.assertIn("Detail calls: 1", self.output)
        self.assertIn("location", self.output)
        self.assertIn("Qualified-status calls: 1", self.output)

    def test_coop_mode(self):
        page1 = list_page(1, range(0, 20), 25)
        page1["models"][1]["job_location"] = "Multiple Locations"
        page1["models"][2]["job_type"] = ["Co-op", "Full Time / Part Time"]
        ids = [model(i)["job_id"] for i in range(25)]
        detail = HOST + f"/api/v3/jobs/{ids[1]}?ocr=c"
        statuses = dict.fromkeys(ids, "Not Qualified")
        statuses[ids[0]] = "Campus Interviews"
        self.run_main({COOP_LIST_URL: page1, COOP_LIST_URL + "&page=2": list_page(2, range(20, 25), 25),
                       detail: {"location": [{"address_text": "New York, NY, USA"}]},
                       qualified_url(ids): {"qualified": statuses}}, ["--coop"])
        self.assertTrue(COOP_LIST_URL.startswith(HOST + "/api/v2/jobs?ocr=c&job_type=5&"))
        self.assertEqual([url for url, _ in self.api.calls],
                         [COOP_LIST_URL, COOP_LIST_URL + "&page=2", detail, qualified_url(ids)])
        self.assertTrue(all(h == AUTH_HEADERS for _, h in self.api.calls))
        rows, md = self.read_outputs()
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows[1]["location"], "New York, NY, USA")
        self.assertEqual((rows[0]["qualified"], rows[1]["qualified"]), ("Campus Interviews", "Not Qualified"))
        self.assertTrue(rows[0]["url"].endswith("&ocr=c&currentJobId=" + ids[0]))
        self.assertTrue(md.startswith("# All Job Listings, Co-op (25 jobs)\n"))
        self.assertIn("- qualified status: Campus Interviews\n", md)
        self.assertIn("Fetched 25 jobs from All Job Listings, Co-op only.", self.output)
        self.assertIn("Qualified-status calls: 1", self.output)

    def test_unfiltered_coop_list_writes_nothing(self):
        page1 = list_page(1, range(0, 20), 25)
        page1["models"][5]["job_type"] = ["Internship"]
        with self.assertRaises(SystemExit) as cm:
            self.run_main({COOP_LIST_URL: page1}, ["--coop"])
        self.assertIn("list page 1", str(cm.exception))
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertNotIn("sentinel", self.output + str(cm.exception))

    def test_malformed_qualified_response_writes_nothing(self):
        ids = [model(i)["job_id"] for i in range(15)]
        with self.assertRaises(SystemExit) as cm:
            self.run_main({LIST_URL: list_page(1, range(0, 15), 15), qualified_url(ids): {"error": "oops"}})
        self.assertIn(qualified_url(ids), str(cm.exception))
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertNotIn("sentinel", self.output + str(cm.exception))

    def test_unknown_flag_exits_before_the_browser(self):
        with mock.patch("fetch_jobs.sync_playwright") as playwright, mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                fetch_jobs.main(["--co-op"])
        playwright.assert_not_called()

    def test_failed_page_check_writes_nothing(self):
        with self.assertRaises(SystemExit):
            self.run_main({LIST_URL: list_page(1, range(0, 20), 25),
                           LIST_URL + "&page=2": list_page(1, range(0, 20), 25)})
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertNotIn("sentinel", self.output)


if __name__ == "__main__":
    unittest.main()
