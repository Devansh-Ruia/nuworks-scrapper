"""Phase 1 recon for NUworks: log in by hand, then record the job search traffic.

Run from this folder with `uv run python recon.py`. Writes recon.har and
recon_summary.txt. Login traffic is never recorded and no password is ever
asked for or typed by this script.

In recon.har, cookie values and the values of auth, session, CSRF and token
headers are replaced with salted hashes, and password posts are redacted.
Response bodies, URLs, query strings and other post bodies stay raw because
API discovery needs them. They may hold personal data, so review before sharing.
"""

import base64
import hashlib
import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error, sync_playwright

LOGIN_URL = "https://northeastern-csm.symplicity.com/students/?signin_tab=0"
HAR_PATH = "recon.har"
SUMMARY_PATH = "recon_summary.txt"
# Anchored on the host so the IdP and the shibboleth- host never land in the HAR.
HAR_FILTER = re.compile(r"^https://northeastern-csm\.symplicity\.com/")
COOKIE_FIELDS = ("name", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
SECRET_HEADER_PARTS = ("auth", "session", "csrf", "xsrf", "token")
# Match "auth" but hold no secret: the host pseudo-header and the 401 auth scheme.
NOT_SECRET_HEADERS = (":authority", "www-authenticate")
BODY_PREVIEW = 500
# Per-run salt: equal values still match within one HAR, short values can't be brute-forced.
SALT = os.urandom(16)
WARNING = ("Warning: response bodies, URLs and query strings in recon.har are raw "
           "and may include personal data. Review before sharing.")

STEP1 = """
Step 1 of 2: log in (nothing is being recorded yet)
  1. In the browser window, click "Current Students and Alumni".
  2. Finish Northeastern SSO and Duo.
  3. Do NOT type into the NUworks password box on the first page. That is a
     separate local login. This script never needs your password.
  4. Keep this first tab open.
  If you are already logged in from an earlier run, just press Enter.
Press Enter here once the NUworks student home page is showing."""

STEP2 = """
Step 2 of 2: recording NUworks traffic now
  1. Run a co-op job search.
  2. Click through two pages of results.
  3. Open one job detail page.
  4. Keep this first tab open.
Press Enter here when done."""

CLOSED_MSG = "The browser tab was closed before Enter was pressed. Nothing was written. Run {script} again."
EOF_MSG = "Could not read terminal input. Run {script} from an interactive terminal."


def _hash(value):
    return "sha256:" + hashlib.sha256(SALT + str(value).encode("utf-8")).hexdigest()[:8]


def _scrub_cookie_header(value):
    out = []
    for pair in value.split(";"):
        name, eq, val = pair.strip().partition("=")
        if name or val:
            out.append(f"{name}={_hash(val)}" if eq else _hash(name))
    return "; ".join(out)


def _scrub_set_cookie(value):
    # Hash only name=value, keep Max-Age/Expires/Path etc. Some HARs join several cookies with newlines.
    lines = []
    for line in value.split("\n"):
        first, semi, attrs = line.partition(";")
        name, eq, val = first.partition("=")
        lines.append((f"{name}={_hash(val)}" if eq else _hash(first)) + semi + attrs)
    return "\n".join(lines)


def _scrub_header(name, value):
    n = name.lower()
    if n == "cookie":
        return _scrub_cookie_header(value)
    if n == "set-cookie":
        return _scrub_set_cookie(value)
    if n not in NOT_SECRET_HEADERS and any(part in n for part in SECRET_HEADER_PARTS):
        return _hash(value)
    return value


def scrub_har(har):
    """Hash cookie, auth, session, CSRF and token values and redact password posts, in place. Returns har."""
    for entry in har.get("log", {}).get("entries", []):
        for part in (entry.get("request", {}), entry.get("response", {})):
            for h in part.get("headers", []):
                h["value"] = _scrub_header(h.get("name", ""), h.get("value", ""))
            for c in part.get("cookies", []):
                c["value"] = _hash(c.get("value", ""))
        post = entry.get("request", {}).get("postData")
        if post and "password" in (post.get("text") or "").lower():
            post["text"] = "[redacted]"
            for p in post.get("params", []):
                p["value"] = ""
    return har


def scrub_har_file(path, stop=lambda: None):
    """Call stop() (which writes the raw HAR), then scrub path in place. Deletes the file on any failure."""
    path = Path(path)
    try:
        stop()
        har = scrub_har(json.loads(path.read_text(encoding="utf-8")))
        path.write_text(json.dumps(har, indent=1), encoding="utf-8")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return har


def _key(entry):
    req = entry.get("request", {})
    return (req.get("method"), req.get("url"))


def _header(headers, name):
    return next((h.get("value", "") for h in headers if h.get("name", "").lower() == name), "")


def _body_preview(content):
    text = content.get("text")
    if text is None:
        return ["  body: (no body in HAR)"]
    if content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8", errors="replace")
        except ValueError:
            return ["  body: (not valid base64)"]
    lines = [f"  body (first {BODY_PREVIEW} chars):", text[:BODY_PREVIEW]]
    if len(text) > BODY_PREVIEW:
        lines.append(f"  (truncated, {len(text)} chars total)")
    return lines


def summarize(har, xhr_keys):
    """Describe the XHR/fetch entries of har, in HAR order."""
    entries = [e for e in har.get("log", {}).get("entries", []) if _key(e) in xhr_keys]
    lines = [f"XHR/fetch entries in HAR: {len(entries)}"]
    missing = set(xhr_keys) - {_key(e) for e in entries}
    if missing:
        hosts = sorted({urlsplit(url).netloc for _, url in missing})
        lines.append(f"Tagged XHR/fetch requests not in the HAR: {len(missing)} (hosts: {', '.join(hosts)})")
    if not entries:
        lines.append("No XHR/fetch captured on northeastern-csm.symplicity.com. "
                     "Check that the job search ran after the first Enter.")
        return "\n".join(lines)
    for i, e in enumerate(entries, 1):
        req, resp = e.get("request", {}), e.get("response", {})
        content = resp.get("content", {})
        ctype = _header(resp.get("headers", []), "content-type") or content.get("mimeType", "")
        names = sorted({h.get("name", "").lower() for h in req.get("headers", [])})
        lines += [
            "",
            f"[{i}] {req.get('method')} {req.get('url')}",
            f"  status: {resp.get('status')}",
            f"  content-type: {ctype or '(none)'}",
            f"  request headers: {', '.join(names) or '(none)'}",
        ]
        if "json" in ctype.lower():
            lines += _body_preview(content)
        else:
            lines.append("  body: skipped (not JSON)")
    return "\n".join(lines)


def _utc(expires):
    if expires == -1:
        return "session"
    try:
        when = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=expires)
    except (OverflowError, TypeError, ValueError):
        return "unknown"
    return when.strftime("%Y-%m-%d %H:%M:%S UTC")


def cookie_table(cookies):
    """Text table of cookie metadata. Reads only COOKIE_FIELDS, never the value."""
    if not cookies:
        return "Cookies: none"
    head = ("name", "domain", "path", "expires", "expires_utc", "httpOnly", "secure", "sameSite")
    rows = [head]
    for c in sorted(cookies, key=lambda c: (c.get("domain", ""), c.get("name", ""))):
        exp = c.get("expires", -1)
        rows.append((c.get("name", ""), c.get("domain", ""), c.get("path", ""),
                     round(exp) if isinstance(exp, (int, float)) else exp, _utc(exp),
                     c.get("httpOnly", ""), c.get("secure", ""), c.get("sameSite", "")))
    rows = [[str(v) for v in r] for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    table = "\n".join("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in rows)
    return f"Cookies ({len(cookies)}, values never stored):\n{table}"


def wait_for_enter(page, prompt, script="recon.py"):
    """Block until Enter while keeping Playwright events flowing. A bare input() would stall them."""
    print(prompt)
    done = threading.Event()
    state = {}

    def read():
        try:
            input()
        except Exception:
            state["failed"] = True
        finally:
            done.set()

    threading.Thread(target=read, daemon=True).start()
    while not done.is_set():
        if page.is_closed():
            sys.exit(CLOSED_MSG.format(script=script))
        try:
            page.wait_for_timeout(300)
        except Error:
            if page.is_closed():
                sys.exit(CLOSED_MSG.format(script=script))
            raise
    if state.get("failed"):
        sys.exit(EOF_MSG.format(script=script))


def main():
    # Git Bash pipes stdout, so force line buffering and never crash on odd characters.
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    xhr_keys = set()

    def on_request(request):
        if request.resource_type in ("xhr", "fetch"):
            xhr_keys.add((request.method, request.url))

    har = None
    stopping = False
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(user_data_dir="./user_data", headless=False)
            page = context.pages[0]
            page.goto(LOGIN_URL)
            wait_for_enter(page, STEP1)

            # Only now, after login, start listening and recording.
            context.on("request", on_request)
            context.tracing.start_har(HAR_PATH, content="embed", url_filter=HAR_FILTER)
            wait_for_enter(page, STEP2)
            context.remove_listener("request", on_request)
            # Scrub before anything else can fail. No finally with close(): close() would flush a raw HAR on abort.
            stopping = True
            har = scrub_har_file(HAR_PATH, context.tracing.stop_har)

            cookies = [{k: c[k] for k in COOKIE_FIELDS if k in c} for c in context.cookies()]
            context.close()
    finally:
        # If stop_har() was interrupted, the driver can still write recon.har after scrub_har_file
        # deleted it. Leaving the with block waits for the driver to exit, so delete again here.
        # Only after a failed stop, so an earlier run's scrubbed HAR survives other aborts.
        if stopping and har is None:
            Path(HAR_PATH).unlink(missing_ok=True)

    summary = WARNING + "\n\n" + summarize(har, xhr_keys) + "\n\n" + cookie_table(cookies) + "\n"
    Path(SUMMARY_PATH).write_text(summary, encoding="utf-8")
    print()
    print(summary)
    print(f"Wrote {SUMMARY_PATH} and {HAR_PATH}. In the HAR, cookie values and auth, session, "
          "CSRF and token header values are hashed, and password posts are redacted.")
    print(WARNING)


if __name__ == "__main__":
    main()
