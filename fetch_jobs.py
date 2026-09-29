"""Phase 2 for NUworks: pull every job in the "Jobs I Qualify For" list for review.

Run from this folder with `uv run python fetch_jobs.py`. Reuses the browser
profile in ./user_data. If the session has expired it signs in through
Northeastern SSO on its own and stops for you only when a page asks for a
username, password or Duo. It never fills in or submits a login form.
Writes jobs_for_review.md and jobs.csv, and only after every request succeeded.

The authorization header is copied from the app's own job list request and sent
back with ours. Its value is never printed, logged or written to disk.
"""

import csv
import json
import math
import re
import sys
import time
from datetime import datetime
from functools import partial
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error, sync_playwright

from recon import LOGIN_URL, wait_for_enter

HOST = "https://northeastern-csm.symplicity.com"
SEARCH_URL = HOST + "/students/app/jobs/search?ocr=f"
LIST_URL = HOST + "/api/v2/jobs?ocr=f&json_mode=read_only&enable_translation=false"
DETAIL_URL = HOST + "/api/v3/jobs/{job_id}?ocr=f"
# Verbatim from a Referer in recon.har. Only currentJobId changes.
POSTING_URL = HOST + "/students/app/jobs/search?perPage=20&page=1&sort=!postdate&ocr=f&currentJobId={job_id}"
KEEP_HEADERS = ("authorization", "x-requested-system-user")
HEADER_TIMEOUT_MS = 20_000
NUWORKS_HOST = urlsplit(HOST).hostname
SSO_BUTTON = "Current Students and Alumni"
SSO_TIMEOUT_MS = 30_000
# Boxes a person types a username or password into. Hidden inputs, like the IdP's auto-submit form, don't match.
PERSON_INPUTS = "input[type=password], input[type=email], input[type=text], input:not([type])"
REQUEST_DELAY = 1.0
MULTIPLE_LOCATIONS = "Multiple Locations"
MD_PATH = "jobs_for_review.md"
CSV_PATH = "jobs.csv"
FIELDS = ("title", "employer", "visual_id", "job_id", "location", "remote_onsite", "job_start", "job_end",
          "deadline", "compensation_from", "compensation_to", "compensation_frequency", "url", "description")
BLOCK_TAGS = {"p", "div", "ul", "ol", "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

SSO_MSG = "Session expired. Signing in through Northeastern SSO..."
LOGIN_PROMPT = """
The sign-in needs you. Finish it in the browser window:
  1. If the NUworks login page is still showing, click "Current Students and Alumni".
  2. Finish Northeastern SSO and Duo.
  3. Do NOT type into the NUworks password box. This script never needs your password.
Press Enter here once the NUworks student home page is showing."""

NO_HEADERS_MSG = ("Could not capture the job list request from the NUworks search page, even after "
                  "logging in. Check that the student home page loads, then run fetch_jobs.py again.")
PAGE_PARAM_MSG = ("The page param did not work: the request with &page=2 did not return page 2 with "
                  "new jobs. Stopping before the full fetch. No output files were written.")


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []  # (href, index into parts where the link text starts)
        self.item_start = False  # after "- " and before the item's first text

    def handle_starttag(self, tag, attrs):
        if tag in BLOCK_TAGS:
            # <li><p>text</p></li> is common. A break here would leave the "- " on a line of its own.
            if not self.item_start:
                self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
            self.item_start = True
        elif tag == "a":
            self.links.append(((dict(attrs).get("href") or "").strip(), len(self.parts)))

    def handle_endtag(self, tag):
        if tag in BLOCK_TAGS:
            self.parts.append("\n\n")
        elif tag == "a" and self.links:
            href, start = self.links.pop()
            if href and href != "".join(self.parts[start:]).strip():
                self.parts.append(f" ({href})")

    def handle_data(self, data):
        if self.item_start:
            # "- " already separates the marker from the text.
            data = data.lstrip()
            if not data:
                return
            self.item_start = False
        # Whitespace-only text between tags is source formatting. As one space it can't add blank lines to lists.
        self.parts.append(data if data.strip() else " ")


def html_to_text(html):
    """Plain text of a job_desc HTML string, with paragraphs, line breaks and "- " list items. Never truncates."""
    if not html:
        return ""
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    # Leading spaces come from &nbsp; and source indentation. Four of them would start a Markdown code block.
    text = "\n".join(line.strip() for line in text.split("\n") if line.strip() != "-")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _is_list_request(request):
    # The SPA also sends /api/v2/jobs? without ocr=f, which is a different list.
    return (request.url.startswith(HOST + "/api/v2/jobs?")
            and parse_qs(urlsplit(request.url).query).get("ocr") == ["f"])


def capture_headers(page):
    """Load the search page and copy KEEP_HEADERS from the app's own job list request. None if it never came."""
    try:
        with page.expect_request(_is_list_request, timeout=HEADER_TIMEOUT_MS) as info:
            page.goto(SEARCH_URL)
        request = info.value
        response = request.response()
    except Error:
        # Timeout, or a login redirect that interrupted the navigation. Both mean no usable session.
        return None
    headers = request.all_headers()
    # A rejected request means the session has expired, even though the SPA still sent it.
    if response is None or not response.ok or "authorization" not in headers:
        return None
    return {k: headers[k] for k in KEEP_HEADERS if k in headers}


def _host(url):
    return urlsplit(url).hostname or ""


def needs_person(page):
    """True if the page shows Duo or a visible username or password box. Only reads the page."""
    try:
        hosts = [_host(frame.url) for frame in page.frames]
        if any(h == "duosecurity.com" or h.endswith(".duosecurity.com") for h in hosts):
            return True
        return page.locator(PERSON_INPUTS).filter(visible=True).count() > 0
    except Error:
        # The page navigated mid-check. The next poll looks again.
        return False


def sso_login(page):
    """Click SSO_BUTTON on the NUworks login page and follow the redirects. True once back on NUworks.

    False as soon as a page asks for a person, and on a timeout or any Playwright error.
    The click on the login page is the only input this makes.
    """
    print(SSO_MSG)
    try:
        page.goto(LOGIN_URL)
        if _host(page.url) != NUWORKS_HOST:
            return False
        before = page.url
        # The click already waits for the navigation it starts to commit, so it needs the full SSO timeout.
        page.get_by_role("button", name=SSO_BUTTON, exact=True).click(timeout=SSO_TIMEOUT_MS)
        # Covers a navigation the click didn't wait for, and keeps the unchanged login page from counting as done.
        # Not "off NUworks": with a live SP session, /sso/ can redirect straight back without leaving the host.
        page.wait_for_url(lambda url: url != before, wait_until="commit", timeout=SSO_TIMEOUT_MS)
        for _ in range(SSO_TIMEOUT_MS // 300):
            # Host first, so a password box on a NUworks page never counts as a login prompt.
            if _host(page.url) == NUWORKS_HOST:
                page.wait_for_load_state()
                return True
            if needs_person(page):
                return False
            page.wait_for_timeout(300)
    except Error:
        # Closed tab, missing button, timeout or interrupted navigation. The manual prompt covers each one.
        return False
    # Timed out on a page it can't classify, like a consent screen.
    return False


def get_headers(page):
    """Headers for our API calls. On an expired session, tries SSO first, then asks for one manual login."""
    headers = capture_headers(page)
    if headers is None:
        if sso_login(page):
            headers = capture_headers(page)
        # Back on NUworks without a session still gets the manual login.
        if headers is None:
            wait_for_enter(page, LOGIN_PROMPT, script="fetch_jobs.py")
            headers = capture_headers(page)
    if headers is None:
        sys.exit(NO_HEADERS_MSG)
    return headers


def fetch_json(request_context, url, headers):
    """GET url with the context's cookies plus headers. Exits on failure with the URL and status, never headers."""
    time.sleep(REQUEST_DELAY)
    try:
        response = request_context.get(url, headers=headers)
        text = response.text()
    except Error as e:
        # Playwright appends a call log that lists every request header, authorization included.
        reason = str(e).split("\n", 1)[0]
        for value in filter(None, headers.values()):
            reason = reason.replace(value, "[hidden]")
        # from None, so a traceback printer never shows the original error as context.
        raise SystemExit(f"Request failed for {url}: {reason}") from None
    if not response.ok:
        sys.exit(f"Request failed: HTTP {response.status} for {url}")
    try:
        return json.loads(text)
    except ValueError:
        sys.exit(f"Response was not JSON (HTTP {response.status}) for {url}. The session may have expired.")


def fetch_models(fetch):
    """All list models, deduped by job_id in API order, plus a note on the page-2 check.

    The browser never sent a page param, so &page=2 is tested once before looping over the rest.
    """
    first = fetch(LIST_URL)
    per_page, total = first["perPage"], first["total"]
    pages = [first["models"]]
    if total <= per_page:
        note = f"skipped, total {total} fits on one page of {per_page}"
    else:
        second = fetch(f"{LIST_URL}&page=2")
        first_ids = {m["job_id"] for m in first["models"]}
        second_ids = {m["job_id"] for m in second["models"]}
        # An empty page 2 would pass the overlap test without proving anything.
        if second.get("page") != 2 or not second_ids or first_ids & second_ids:
            sys.exit(PAGE_PARAM_MSG)
        note = "passed, &page=2 returned page 2 with no job_ids from page 1"
        pages.append(second["models"])
        for n in range(3, math.ceil(total / per_page) + 1):
            models = fetch(f"{LIST_URL}&page={n}")["models"]
            if not models:
                break
            pages.append(models)
    unique = {}
    for model in (m for page in pages for m in page):
        unique.setdefault(model["job_id"], model)
    if len(unique) != total:
        print(f"Warning: got {len(unique)} unique jobs but the API reported a total of {total}.")
    return list(unique.values()), note


def resolve_locations(models, fetch):
    """Location per model. Only the "Multiple Locations" placeholder costs a detail call. Returns (locations, calls)."""
    locations, calls = [], 0
    for model in models:
        location = model.get("job_location") or ""
        if location == MULTIPLE_LOCATIONS:
            calls += 1
            detail = fetch(DETAIL_URL.format(job_id=model["job_id"]))
            addresses = [a.get("address_text") for a in detail.get("location") or []]
            location = "; ".join(filter(None, addresses)) or MULTIPLE_LOCATIONS
        locations.append(location)
    return locations, calls


def to_record(model, location):
    """Flat record with FIELDS as keys, in order."""
    times = model.get("jobs_iso_times") or {}
    return {
        "title": (model.get("job_title") or "").strip(),
        "employer": (model.get("name") or "").strip(),
        "visual_id": model.get("visual_id") or "",
        "job_id": model["job_id"],
        "location": location,
        "remote_onsite": (model.get("symp_remote_onsite") or {}).get("label") or "not specified",
        "job_start": times.get("job_start") or "",
        "job_end": times.get("job_end") or "",
        "deadline": model.get("deadline") or "",
        "compensation_from": model.get("compensation_from") or "",
        "compensation_to": model.get("compensation_to") or "",
        "compensation_frequency": model.get("compensation_frequency") or "",
        "url": POSTING_URL.format(job_id=model["job_id"]),
        "description": html_to_text(model.get("job_desc")),
    }


def format_compensation(record):
    """Like "140 to 280 per day". Empty when all three compensation fields are empty."""
    amount = " to ".join(filter(None, (record["compensation_from"], record["compensation_to"])))
    return " ".join(filter(None, (amount, record["compensation_frequency"])))


def write_csv(records, path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)


def write_markdown(records, path):
    generated = datetime.now().astimezone().isoformat(timespec="minutes")
    lines = [f"# Jobs I Qualify For ({len(records)} jobs)", "",
             f"Source: NUworks Jobs I Qualify For list ({SEARCH_URL}). Generated {generated}.", ""]
    for r in records:
        comp = format_compensation(r)
        lines += [
            "---", "",
            f"## {r['title']} at {r['employer']}", "",
            f"- visual_id: {r['visual_id']}",
            f"- job_id: {r['job_id']}",
            f"- location: {r['location']}",
            f"- remote/onsite: {r['remote_onsite']}",
            f"- job_start: {r['job_start']}",
            f"- job_end: {r['job_end']}",
            f"- application deadline: {r['deadline']}",
            *([f"- compensation: {comp}"] if comp else []),
            f"- posting URL: {r['url']}",
            "", "### Description", "",
            r["description"] or "(no description)", "",
        ]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def main():
    # Git Bash pipes stdout, so force line buffering and never crash on odd characters.
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(user_data_dir="./user_data", headless=False)
        page = context.pages[0]
        headers = get_headers(page)
        fetch = partial(fetch_json, context.request, headers=headers)
        models, page_check = fetch_models(fetch)
        locations, detail_calls = resolve_locations(models, fetch)
        context.close()

    records = [to_record(m, loc) for m, loc in zip(models, locations)]
    write_csv(records, CSV_PATH)
    write_markdown(records, MD_PATH)
    print(f"Fetched {len(records)} jobs.")
    print(f"Page 2 check: {page_check}.")
    print(f"Detail calls: {detail_calls}, all for the location field of \"{MULTIPLE_LOCATIONS}\" jobs.")
    print(f"Wrote {MD_PATH} and {CSV_PATH}.")


if __name__ == "__main__":
    main()
