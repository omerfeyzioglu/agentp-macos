#!/usr/bin/env python3
"""
qa.py — exploratory functional testing: "test example.com"

AgentP explores a site the way a QA engineer would, without a script:

  for each page (starting from the address you give, staying on that site):
    1. read the page            headings, links, buttons, fields (incl. below the fold)
    2. automatic checks         HTTP status, error pages, broken links, slow loads
    3. Gemini proposes tests    the page's core features: search, forms (valid + invalid input),
                                navigation, tabs, filters… each with steps and an expected result
    4. AgentP runs the steps    the normal agent loop (Jev picks every element)
    5. Jev checks the result    a yes/no question about the screen; Gemini decides when Jev is unsure
    6. go deeper                the most important unvisited pages Gemini picked

Safety: stays on the given site, uses made-up test data, never pays, buys, deletes, logs out or
contacts anyone. Form submission needs your OK (or --no-submit to only test validation).

The report (HTML, JSON, JUnit XML for CI, screenshots of failures) is written to ~/.agentp/qa/.

Usage:
  python3 qa.py example.com
  python3 qa.py https://shop.example.com --pages 8 --minutes 20 --no-submit
  ./run.sh --test example.com
  In the panel: "test example.com" or "go to example.com and test its features"
"""
import argparse
import html
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import agent

QA_DIR = agent.DATA_DIR / "qa"
PASS, FAIL = 0.75, 0.25            # Jev's probability that the expected result is on screen
CHECK_TIMEOUT = 8.0                 # seconds to wait for an expected result to appear
SLOW_PAGE_MS = 6000
MAX_LINKS = 80                      # same-site links checked for errors per session
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
ERROR_RE = re.compile(
    r"page not found|404 not found|error 404|\b404\b.{0,20}(page|error)|internal server error|error 500|"
    r"bad gateway|service unavailable|something went wrong|an (unexpected )?error (has )?occurred|"
    r"unexpected error|traceback \(most recent call last\)|uncaught (type|reference)error|"
    r"\[object object\]|undefined is not|cannot read propert", re.I)
HARD_STOP = re.compile(r"\b(pay|payment|buy|purchase|place order|checkout|check out|subscribe|unsubscribe|"
                       r"delete (my |your |the )?account|close (my |your )?account|deactivate|log ?out|sign ?out|"
                       r"send money|transfer)\b", re.I)
TEST_REQUEST = re.compile(r"^\s*(test|qa)\b|--test\b|\b(test|qa) (this|the|it|that|its|my|our)\b|"
                          r"\b(test|qa)\b.*\b(feature|function|functionality|site|website|page|form)s?\b|"
                          r"\btest (et|eder|edin|edebilir|yap)|\btestini|\btestlerini|fonksiyon(el)? test|"
                          r"functional test|\bqa\b", re.I)
URL_IN = re.compile(r"(https?://[^\s'’\"]+|\b[\w-]+(\.[\w-]+)+\.[a-z]{2,}(/[^\s'’\"]*)?|"
                    r"\b[\w-]+\.(com|net|org|io|ai|dev|app|co|tr|edu|gov)\b(/[^\s'’\"]*)?)", re.I)   # stops at "'un"
GENERIC_FOCUS = re.compile(r"^\s*((the )?(whole |entire )?(site|website)|everything|all( the)? features|"
                           r"(functional |function )?test(s|ing)?|functionality|features?)\s*$", re.I)


# ---------------------------------------------------------------- talking to the user
class QAIO:
    """The agent's output goes into the test record; only progress reaches the user.
    Confirmations follow the session's safety policy instead of interrupting."""

    def __init__(self, base, allow_submit: bool, in_scope):
        self.base, self.allow_submit, self.in_scope = base, allow_submit, in_scope
        self.lines: list[str] = []
        self.blocked: list[str] = []

    def progress(self, text: str):
        self.base.say(text)

    def say(self, text: str):
        self.lines.append(text)

    def stream(self, delta: str):
        self.lines.append(delta)

    def stream_end(self, *a, **k):
        pass

    def confirm(self, msg: str, default_yes: bool = False) -> bool:
        if msg.startswith("Send?") and self.allow_submit and self.in_scope():
            return True   # submitting a form on the site under test, with test data
        if msg.startswith("Press '") and self.in_scope() and not HARD_STOP.search(msg):
            return True   # "Delete" / "Remove" inside the site under test is a feature to test
        self.blocked.append(re.sub(r"\s*\[.*\]\s*$", "", msg))
        return False      # quitting apps, typing into terminals, risky buttons: never during tests

    def pick(self, question: str, options: list[str]) -> int | None:
        self.blocked.append(f"{question} ({len(options)} similar elements)")
        return None       # ambiguous: fail the step with a clear note instead of guessing


# ---------------------------------------------------------------- browser
def browser() -> str:
    return agent.default_browser()


def osa_get(script: str) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        return ""


class TestWindow:
    """A browser window opened for this test session. Everything happens in it: the user's own
    windows and tabs are never navigated, closed or read."""

    def __init__(self, app: str, url: str):
        self.app, self.safari = app, app == "Safari"
        if not agent.find_running(app):
            subprocess.run(["open", "-a", app], check=False)
            time.sleep(2.0)
        self.private = not self.safari
        self.id = self._open(url)

    def _open(self, url: str) -> str:
        """A new window at url. Chromium: incognito, so the site starts with no remembered state
        (forms that remember their step, logins, carts) and the user's own session is not used."""
        u = url.replace('"', '%22')
        if self.safari:
            wid = osa_get(f'tell application "Safari"\nmake new document with properties {{URL:"{u}"}}\n'
                          'return id of front window\nend tell')
        else:
            props = ' with properties {mode:"incognito"}' if self.private else ""
            wid = osa_get(f'tell application "{self.app}"\nset w to make new window{props}\n'
                          f'set URL of active tab of w to "{u}"\nreturn id of w\nend tell')
            if not wid.isdigit() and self.private:   # incognito disabled (e.g. by policy): a normal window
                self.private = False
                return self._open(url)
        if not wid.isdigit():
            raise RuntimeError(f"could not open a test window in {self.app} (allow AgentP to control {self.app} "
                               "in System Settings → Privacy & Security → Automation)")
        return wid

    def reset(self, url: str):
        """Start over with a clean site state: close this window and open a fresh private one. Incognito
        data is dropped when the last incognito window closes, so the old one is closed first."""
        osa_get(f'tell application "{self.app}" to close {self._ref()}')
        time.sleep(0.4)
        self.id = self._open(url)

    def _ref(self) -> str:
        return f"(first window whose id is {self.id})"

    def _tab(self) -> str:
        return f"{'current' if self.safari else 'active'} tab of {self._ref()}"

    def front(self):
        osa_get(f'tell application "{self.app}" to set index of {self._ref()} to 1')
        agent.activate(self.app, agent.Ctx(installed=[]))

    def goto(self, url: str):
        u = url.replace('"', '%22')
        osa_get(f'tell application "{self.app}" to set URL of {self._tab()} to "{u}"')

    def url(self) -> str:
        return osa_get(f'tell application "{self.app}" to get URL of {self._tab()}')

    def title(self) -> str:
        what = "name" if self.safari else "title"
        return osa_get(f'tell application "{self.app}" to get {what} of {self._tab()}')

    def focused(self) -> bool:
        """Is the test window the browser's focused window (the one the agent reads and clicks)?"""
        a = agent.find_running(self.app)
        if not a:
            return False
        win = agent.ax(agent.AXUIElementCreateApplication(a.processIdentifier()), "AXFocusedWindow")
        wtitle = str(agent.ax(win, "AXTitle") or "") if win is not None else ""
        mine = self.title()
        return bool(mine) and mine[:30] in wtitle

    def ensure_front(self):
        """Raise the test window. Reading no longer depends on focus (the window is found by title),
        but clicks by position and typed keys need it on top."""
        for _ in range(3):
            self.front()
            time.sleep(0.25)
            if self.focused():
                return True
        return False


WINDOW: TestWindow | None = None   # the session's test window


def current_url(app: str) -> str:
    if WINDOW is not None:
        return WINDOW.url()
    if app == "Safari":
        return osa_get('tell application "Safari" to get URL of front document')
    return osa_get(f'tell application "{app}" to get URL of active tab of front window')


def goto(app: str, url: str) -> float:
    """Load url in the test window. Returns the load time in ms: until the browser stops loading
    (Chromium) — not until the page stops moving, which carousels never do."""
    t = time.perf_counter()
    WINDOW.front()
    before = agent.screen_signature(agent.read_screen(app))
    WINDOW.goto(url)
    time.sleep(0.3)
    while agent.page_loading(app) and time.perf_counter() - t < 30:
        time.sleep(0.2)
    loaded = (time.perf_counter() - t) * 1000
    agent.wait_for_change(app, before, timeout=4.0, min_items=5)   # let the page settle, briefly
    for _ in range(2):   # consent banners often appear a moment after load
        if dismiss_overlays(app):
            agent.wait_for_change(app, "", timeout=1.5)
        else:
            break
    now = WINDOW.url()
    if now and norm(now).split("?")[0] != norm(url).split("?")[0] and domain(now) != domain(url):
        WINDOW.goto(url)   # something took the page away (e.g. a pressed link): back to the page under test
        agent.wait_for_change(app, "", timeout=4.0, min_items=5)
    return loaded


def malformed_url(u: str) -> bool:
    """'https://www.ford.com.trhttps//twitter.com/x': a second address glued into the host or path
    (query strings like ?url=https://… are fine)."""
    if not u.startswith("http"):
        return False
    p = urllib.parse.urlsplit(u)
    return bool(re.search(r"https?:?/", p.netloc + p.path[:1] if p.netloc else "") or re.search(r"https?:?/{1,2}", p.path)) \
        or bool(re.search(r"\.[a-z]{2,}https?", p.netloc, re.I))


def norm(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", p.query, ""))


def domain(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


def http_status(url: str) -> int | str:
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405, 501):
                continue   # some servers refuse HEAD: retry with GET
            return e.code
        except Exception as e:
            if method == "GET":
                return type(e).__name__
    return "unknown"


# ---------------------------------------------------------------- reading a page
def element_url(it: dict) -> str | None:
    if it.get("el") is None:
        return None
    u = agent.ax(it["el"], "AXURL")
    try:
        return str(u.absoluteString()) if u is not None else None
    except Exception:
        return str(u) if u else None


FIELD_ROLES = agent.TEXT_ROLES | {"AXPopUpButton", "AXCheckBox", "AXRadioButton"}


def field_kind(it: dict) -> str:
    el, role = it.get("el"), it["role"]
    sub = str(agent.ax(el, "AXSubrole") or "") if el is not None else ""
    desc = str(agent.ax(el, "AXRoleDescription") or "").casefold() if el is not None else ""
    text = (it["label"] + " " + desc).casefold()
    if role == "AXCheckBox":
        return "checkbox"
    if role == "AXRadioButton":
        return "radio"
    if role in ("AXPopUpButton", "AXComboBox"):
        return "select"
    if "secure" in sub.casefold() or "password" in text or "şifre" in text:
        return "password"
    if role == "AXTextArea":
        return "textarea"
    for kind, words in (("email", ("email", "e-mail", "e-posta")), ("phone", ("phone", "telefon", "tel")),
                        ("date", ("date", "tarih", "birthday")), ("number", ("number", "quantity", "age", "zip",
                                                                            "postal", "adet")),
                        ("search", ("search", "ara"))):
        if any(w in text for w in words):
            return kind
    return "text"


def form_fields(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        if not it.get("page") or it["role"] not in FIELD_ROLES:
            continue
        el = it.get("el")
        f = {"field": it["label"][:70], "kind": field_kind(it)}
        if el is not None and agent.ax(el, "AXRequired"):
            f["required"] = True
        if f["kind"] in ("select", "checkbox", "radio") and el is not None:
            v = agent.ax(el, "AXValue")
            if v is not None:
                f["current"] = str(v)[:40]
        out.append(f)
    return out[:40]


CONSENT_TEXT = re.compile(r"cookie|çerez", re.I)   # only an actual cookie notice, not a footer "Privacy" link
CONSENT_PREFER = [   # the most privacy-preserving choice first
    re.compile(r"^(reject|decline|deny)( all)?$|^tümünü reddet$|^reddet$|only (necessary|essential)|sadece (gerekli|zorunlu)|"
               r"necessary only|yalnızca zorunlu|zorunlu (çerezler|olanlar)", re.I),
    re.compile(r"^(accept|agree|allow|ok|okay|got it|i agree|kabul( et)?|tümünü kabul et|tamam|anladım|onayla|"
               r"accept all|allow all|accept cookies|çerezleri kabul et)( all)?[.!]?$", re.I),
    # no "X" / "Close": too easily a social link (X/Twitter) or something unrelated
]


def dismiss_overlays(app: str) -> str | None:
    """Close a cookie / consent banner if one is up (it hides the page from the accessibility tree).
    Prefers 'reject / only necessary'. Returns what was pressed."""
    items = agent.read_screen(app)
    page = [i for i in items if i.get("page")]
    notice = [i for i in page if i["role"] in agent.READ_ROLES and CONSENT_TEXT.search(i["label"])
              and len(i["label"]) >= 25]   # a sentence about cookies, not a link label
    if not notice:
        return None
    buttons = [i for i in page if i.get("pressable") and i["role"] == "AXButton"]   # buttons only, never links
    for pattern in CONSENT_PREFER:
        for b in buttons:
            if pattern.search(b["label"].split(" | ")[0].strip()):
                agent.press(b, agent.Ctx(installed=[]))
                time.sleep(0.8)
                return b["label"][:40]
    return None


def snapshot(app: str) -> dict:
    """Everything a QA engineer would note about the page, from its accessibility tree."""
    items = agent.read_screen(app)
    page = [i for i in items if i.get("page")]
    links, seen = [], set()
    for it in page:
        if it["role"] == "AXLink":
            u = element_url(it)
            key = (it["label"], u)
            if key not in seen:
                seen.add(key)
                links.append({"label": it["label"][:80], "url": u})
    title = WINDOW.title() if WINDOW is not None else ""
    return {
        "url": current_url(app), "title": title, "items": items,
        "headings": [i["label"][:100] for i in page if i["role"] == "AXHeading"][:30],
        "links": links[:120],
        "buttons": [i["label"][:60] for i in page if i["role"] in ("AXButton", "AXMenuButton", "AXPopUpButton",
                                                                   "AXCheckBox", "AXRadioButton", "AXTab")][:60],
        "fields": [i["label"][:60] for i in page if i["role"] in agent.TEXT_ROLES][:30],
        "form_fields": form_fields(items),
        "texts": [i["label"][:160] for i in page if i["role"] in agent.READ_ROLES][:200],
    }


def screen_for_check(app: str) -> dict:
    snap = snapshot(app)
    counts: dict[str, int] = {}
    for it in agent.read_screen(app, dedupe=False):   # "two Delete buttons" needs the duplicates
        if it.get("page") and it["role"] not in agent.READ_ROLES:
            k = f"{agent.ROLE_NAMES.get(it['role'], it['role'])}: {it['label'][:60]}"
            counts[k] = counts.get(k, 0) + 1
    controls = [f"{k} ×{n}" if n > 1 else k for k, n in counts.items()]
    return {"url": snap["url"], "title": snap["title"], "headings": snap["headings"][:20],
            "text_on_page": snap["texts"][:150], "controls_on_page": controls[:80], "fields": snap["fields"][:20]}


def error_texts(snap: dict) -> list[str]:
    found = []
    for t in snap["headings"] + snap["texts"][:120]:
        if ERROR_RE.search(t) and t not in found:
            found.append(t)
    return found[:3]


def screenshot(app: str, path: Path) -> str | None:
    """The browser window as PNG (needs Screen Recording permission for the terminal; else skipped)."""
    a = agent.find_running(app)
    if not a:
        return None
    win = agent.ax(agent.AXUIElementCreateApplication(a.processIdentifier()), "AXFocusedWindow")
    rect = agent.ax_rect(win) if win is not None else None
    args = ["screencapture", "-x", "-o"]
    if rect:
        args += ["-R", ",".join(str(int(v)) for v in rect)]
    try:
        subprocess.run(args + [str(path)], check=True, capture_output=True, timeout=10)
        return path.name if path.exists() and path.stat().st_size > 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------- checking a result
NEGATIVE = re.compile(r"does not (move|advance|proceed|go)|not (move|advance|proceed)|stays? on|remains? on", re.I)
NO_ADAPT = re.compile(r"no (dealers?|results?|items?|products?|matches|records?)|not found|nothing (is )?found|"
                      r"error|warning|invalid|validation|rejected|empty (result|list)", re.I)   # nothing to work around


def split_expectation(expect: str) -> tuple[str, str]:
    """'the form does not move on … and a message about Plaka is shown' → the outcome that decides the test,
    and the feedback that is good UX but not the function itself."""
    m = re.match(r"(.*?\b(?:does not|not) (?:move|advance|proceed|go)[^,;]*?)\s*(?:,|;| and )\s*(.+)$", expect, re.I)
    return (m.group(1).strip(), re.sub(r"^and\s+", "", m.group(2).strip())) if m else (expect, "")


def check(client, gem, app: str, expectation: str, before: dict | None = None) -> tuple[str, float, str]:
    """Is the expected result on screen? Jev answers yes/no with a probability; polled for a few
    seconds (results appear asynchronously); Gemini decides only when Jev stays unsure."""
    from typesafe_sdk import Noul
    t0, p, screen = time.time(), 0.5, {}
    while time.time() - t0 < CHECK_TIMEOUT:
        screen = screen_for_check(app)
        state = {"functional_test_check": expectation, "screen_now": screen}
        if before:   # "did the form move on?" needs the screen before the test
            state["screen_before_the_test"] = before
        a = agent.jev(client, state,
                      {"ok": Noul(instructions="The screen shows this expected result of the test: " + expectation)},
                      {}, "check")
        p = a["ok"].noul or 0
        if p >= PASS:
            return "passed", p, ""
        if p <= FAIL and time.time() - t0 > 3:
            break
        time.sleep(1.0)
    if gem is not None and FAIL < p < PASS:
        try:
            ok, reason = gem.judge(expectation, {"before_the_test": before, "now": screen} if before else screen)
            return ("passed" if ok else "failed"), p, f"second opinion: {reason}"
        except Exception:
            pass
    return "failed", p, "the expected result is not on screen"


# ---------------------------------------------------------------- one session
def test_account() -> dict | None:
    import os
    user, pw = os.environ.get("AGENTP_TEST_USER"), os.environ.get("AGENTP_TEST_PASSWORD")
    return {"username": user, "password": pw or ""} if user else None


def secret(text: str, account: dict | None) -> str:
    """Put the test account into planned text."""
    if account:
        text = text.replace("{{username}}", account["username"]).replace("{{password}}", account["password"])
    return text


def masked(text: str, account: dict | None) -> str:
    if account and account.get("password"):
        text = text.replace(account["password"], "••••••")
    return text


def reachable(url: str) -> bool:
    """Does the site's address exist (DNS)? Checked once, so a misheard name fails fast and clearly."""
    import socket
    host = urllib.parse.urlsplit(url).hostname or ""
    for h in (host, "www." + host if not host.startswith("www.") else host[4:]):
        try:
            socket.getaddrinfo(h, 443)
            return True
        except OSError:
            continue
    return False


def resolve_target(target: str, gem) -> str | None:
    target = target.strip().strip("'\"")
    if re.match(r"^https?://", target):
        return target
    if "." in target and " " not in target:
        return "https://" + target
    loc = local_parse(target)
    if loc["site"]:
        return "https://" + loc["site"]
    if gem is not None:   # a name ("hepsiburada", "the sakarya university site") → official website
        try:
            for f in gem.lookup(f"official website of {target}", []):
                site = (f.get("links") or {}).get("website")
                if site:
                    return site
        except Exception:
            pass
    return None


FOLLOW_UPS = 4      # tests on each new state a passing test reaches (e.g. step 2 of a form)
MAX_DEPTH = 3       # how many states deep (step 2, step 3, step 4)


def follow_up(client, gem, ctx, io, app, page_url, prefix, result, tested, visited, out_dir, account,
              allow_submit, deadline, stopped, depth):
    """A test reached a new step / state: plan tests for it and run them, each starting from that state."""
    if depth > MAX_DEPTH or time.time() > deadline or stopped():
        return
    snap = snapshot(app)   # the state the last test of `prefix` reached
    io.progress(f"  ↳ new state after '{prefix[-1]['title']}': testing it")
    try:
        ideas = gem.test_ideas(domain(page_url), {k: snap[k] for k in ("url", "title", "headings", "buttons", "form_fields")}
                               | {"links": [l["label"] for l in snap["links"]], "texts": snap["texts"][:60]},
                               tested, visited, FOLLOW_UPS, allow_submit, bool(account),
                               f"the state reached after: {' → '.join(t['title'] for t in prefix)} "
                               "(e.g. the next step of the form); test only what is new there, and include one "
                               "test that validly completes this state and moves on (advances=true)")
    except Exception as e:
        io.progress(f"  (could not plan follow-up tests: {str(e)[:60]})")
        return
    for test in ideas["tests"]:
        if time.time() > deadline or stopped():
            return
        goto(app, page_url)   # each follow-up starts clean: page, then the tests that led here
        rec = run_test(client, gem, ctx, io, app, page_url, test, out_dir, len(result["tests"]) + 1, account, prefix)
        result["tests"].append(rec)
        test["_adapted"] = bool(rec.get("adapted"))
        if rec.get("ux"):
            result["issues"].append({"severity": "low", "page": page_url, "what": "No feedback for invalid input",
                                     "detail": f"{test['title']}: {rec['ux']}", "screenshot": None})
        tested.append(f"{' → '.join(t['title'] for t in prefix)} → {test['title']}")
        if rec["status"] == "passed" and test.get("advances"):
            follow_up(client, gem, ctx, io, app, page_url, prefix + [test], result, tested, visited, out_dir,
                      account, allow_submit, deadline, stopped, depth + 1)


def run_session(target: str, client, base_io, pages: int = 6, minutes: float = 15, tests_per_page: int = 3,
                allow_submit: bool = True, out_dir: Path | None = None, account: dict | None = None,
                focus: str = "", page_hint: str = "", stop=None) -> dict:
    """account: {"username": ..., "password": ...} of a TEST account on the site under test. Gemini only
    sees the placeholders {{username}} / {{password}}; values are filled in locally and masked in reports."""
    account = account or test_account()
    t_start = time.time()
    ctx = agent.make_ctx(io=None, use_memory=False)
    gem = ctx.planner
    if gem is not None:
        gem.grounding = False   # test data like "QA Tester" must not be looked up on the web
    ctx.planner = None          # each test step is one UI action: Jev does it, nothing to plan
    ctx.qa_gem = gem            # for replaying tests that needed adapting
    url = resolve_target(target, gem)
    if not url:
        base_io.say(f"✗ Could not find the site '{target}'. Give its address, e.g. test example.com")
        return {}
    if not reachable(url):
        base_io.say(f"✗ {urllib.parse.urlsplit(url).hostname} does not exist. Check the address "
                    f"(dictation may have misheard it).")
        return {}
    global WINDOW
    app = browser()
    try:
        WINDOW = TestWindow(app, url)   # its own window: the user's tabs are never touched
        agent.PREFERRED_WINDOW = WINDOW.title   # read the test window even when another one has the focus
    except RuntimeError as e:
        base_io.say(f"✗ {e}")
        return {}
    scope = domain(url)
    in_scope = lambda: domain(current_url(app) or url).endswith(scope)  # noqa: E731
    io = QAIO(base_io, allow_submit, in_scope)
    ctx.io, ctx.record, ctx.active_app = io, False, app
    stopped = stop or (lambda: False)
    ctx.stop = stopped
    ctx.url_opener = lambda u: WINDOW.goto(u)
    ctx.blocked = frozenset({"open_app", "quit_app", "close_tab", "close_window", "new_window", "new_tab",
                             "reopen_tab", "minimize", "fullscreen", "system"})
    out_dir = out_dir or QA_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{scope}"
    out_dir.mkdir(parents=True, exist_ok=True)

    result = {"site": url, "scope": scope, "started": time.strftime("%Y-%m-%d %H:%M"), "browser": app,
              "allow_submit": allow_submit, "pages": [], "tests": [], "issues": []}
    issue = lambda sev, page, what, detail="", shot=None: result["issues"].append(  # noqa: E731
        {"severity": sev, "page": page, "what": what, "detail": detail, "screenshot": shot})
    what = f"{focus} on {scope}" if focus else (f"the {page_hint} page of {scope}" if page_hint else scope)
    io.progress(f"› Testing {what} in {app}" + (f" (up to {pages} pages, {minutes:.0f} min)" if pages > 1 else ""))
    for hop in [h for h in re.split(r"\s*>\s*", page_hint) if h] if page_hint else []:
        # "ford com tr > binek araçlar > kuga": click through, one named page at a time
        goto(app, url)
        found = pick_link(client, app, hop, snapshot(app))
        if not found:
            io.progress(f"  (no link to '{hop}' on {current_url(app) or url}; testing from here)")
            break
        st = http_status(found)
        if not isinstance(st, int) or st >= 400:
            io.progress(f"  ✗ '{hop}' leads to {found} which returns {st}; testing from {url} instead")
            issue("medium", url, f"Page '{hop}' not found ({st})", found)
            break
        io.progress(f"  → {hop}: {found}")
        url = found

    queue, visited, tested = [url], [], []
    link_pool = ThreadPoolExecutor(8)
    checked_links: dict[str, object] = {}
    malformed: set[str] = set()
    deadline = t_start + minutes * 60

    while queue and len(result["pages"]) < pages and time.time() < deadline and not stopped():
        page_url = queue.pop(0)
        if norm(page_url) in visited:
            continue
        visited.append(norm(page_url))
        load_ms = goto(app, page_url)
        snap = snapshot(app)
        landed = snap["url"] or page_url
        status = http_status(landed)
        page = {"url": landed, "title": snap["title"], "load_ms": round(load_ms), "status": status,
                "purpose": "", "tests": 0}
        result["pages"].append(page)
        io.progress(f"  Page: {snap['title'] or landed}  ({load_ms / 1000:.1f} s)")

        # ---- automatic checks
        if isinstance(status, int) and status >= 400 or not isinstance(status, int):
            issue("high", landed, f"Page returns HTTP {status}")
        for t in error_texts(snap):
            issue("high", landed, "Error shown on the page", t, screenshot(app, out_dir / f"page{len(result['pages'])}-error.png"))
        if load_ms > SLOW_PAGE_MS:
            issue("medium", landed, f"Slow page load: {load_ms / 1000:.1f} s")
        if not domain(landed).endswith(scope):
            issue("medium", page_url, f"Redirected off the site to {landed}")
            continue
        for link in snap["links"]:   # malformed addresses: a site bug whatever the target
            u = link.get("url") or ""
            if malformed_url(u) and u not in malformed:
                malformed.add(u)
                issue("medium", landed, f"Malformed link address: {link['label'][:40]}", u)
        for link in snap["links"]:   # broken links on the same site, checked in the background
            u = link.get("url")
            if u and u.startswith("http") and domain(u).endswith(scope) and norm(u) not in checked_links \
                    and len(checked_links) < MAX_LINKS:
                checked_links[norm(u)] = (link["label"], landed, link_pool.submit(http_status, u))

        # ---- tests proposed by Gemini, run by AgentP, checked by Jev
        ideas = {"page_purpose": "", "tests": [], "next_pages": []}
        if gem is not None:
            try:
                ideas = gem.test_ideas(scope, {k: snap[k] for k in ("url", "title", "headings", "buttons", "form_fields")}
                                       | {"links": [l["label"] for l in snap["links"]], "texts": snap["texts"][:60]},
                                       tested, visited, tests_per_page, allow_submit, bool(account),
                                       focus or (f"the {page_hint} page" if page_hint else ""))
            except Exception as e:
                io.progress(f"  (could not plan tests here: {str(e)[:80]})")
        page["purpose"] = ideas.get("page_purpose", "")
        for n, test in enumerate(ideas["tests"]):
            if time.time() > deadline or stopped():
                break
            if norm(current_url(app) or "") != norm(landed):
                goto(app, landed)   # back to the page under test (a heavy page is not reloaded for nothing)
            result["tests"].append(run_test(client, gem, ctx, io, app, landed, test, out_dir,
                                            len(result["tests"]) + 1, account))
            test["_adapted"] = bool(result["tests"][-1].get("adapted"))
            if result["tests"][-1]["status"] == "passed" and test.get("advances") and gem is not None:
                follow_up(client, gem, ctx, io, app, landed, [test], result, tested, visited, out_dir,
                          account, allow_submit, deadline, stopped, depth=1)
            tested.append(f"{snap['title']}: {test['title']}")
            page["tests"] += 1
            last = result["tests"][-1]
            if last.get("ux"):
                issue("low", landed, "No feedback for invalid input", f"{test['title']}: {last['ux']}")
            for s in last["agent_steps"] if not (NEGATIVE.search(test["expect"]) or last.get("adapted")) else []:
                if "no change on screen" in s:
                    issue("medium", landed, "A control did nothing when used", f"{test['title']}: {s}", last.get("screenshot"))
            if not in_scope():
                issue("low", landed, f"Test left the site: {current_url(app)}", test["title"])

        # ---- deeper: pages Gemini found most important, then other same-site links
        by_label = {l["label"].casefold(): l["url"] for l in snap["links"] if l.get("url")}
        nxt = [by_label.get(p["label"].casefold()) for p in ideas.get("next_pages", [])]
        nxt += [l["url"] for l in snap["links"] if l.get("url")]
        for u in nxt:
            if u and u.startswith("http") and domain(u).endswith(scope) and norm(u) not in visited \
                    and u not in queue and not re.search(r"logout|signout|sign-out|log-out|delete|unsubscribe", u, re.I):
                queue.append(u)
            if len(queue) > 30:
                break

    for key, (label, page_url, fut) in checked_links.items():
        try:
            st = fut.result(timeout=10)
        except Exception:
            st = "timeout"
        if isinstance(st, int) and st in (401, 403):
            continue   # needs a login: not a broken link
        if not isinstance(st, int) or st >= 400:
            issue("medium" if isinstance(st, int) and st == 404 else "low", page_url, f"Broken link ({st}): {label}", key)
    link_pool.shutdown(wait=False)

    result["stopped"] = stopped()
    if result["stopped"]:
        io.progress("  Stopped. Writing the report for what was tested so far.")
    result["duration_s"] = round(time.time() - t_start)
    result["links_checked"] = len(checked_links)
    paths = write_reports(result, out_dir)
    WINDOW = None   # the test window stays open for the user to look at; the session is over
    agent.PREFERRED_WINDOW = None
    passed = sum(t["status"] == "passed" for t in result["tests"])
    io.progress(f"  Done in {result['duration_s'] // 60} min {result['duration_s'] % 60} s: {len(result['pages'])} pages, "
                f"{passed}/{len(result['tests'])} tests passed, {len(result['issues'])} issues")
    io.progress(f"  Report: {paths['html']}")
    result["report"] = paths
    return result


def screen_note(app: str, fields: list[dict]) -> str:
    """What the agent actually saw, for the report (so a failure can be understood without guessing)."""
    items = agent.read_screen(app)
    a = agent.find_running(app)
    win = agent.ax(agent.AXUIElementCreateApplication(a.processIdentifier()), "AXFocusedWindow") if a else None
    title = str(agent.ax(win, "AXTitle") or "?") if win is not None else "?"
    page = [i["label"][:40] for i in items if i.get("page")][:12]
    return (f"Focused window: '{title[:60]}'"
            + ("" if WINDOW is None or WINDOW.focused() else " (not the test window)")
            + f"; fields seen: {[i['label'][:30] for i in fields][:10]}; page shows: {page}")


def best_field(name: str, fields: list[dict]) -> dict | None:
    """The form field whose label best matches the planned field name."""
    if not fields:
        return None
    want = [w for w in agent.tokens(name) if len(w) > 1]

    def score(it):
        have = agent.tokens(it["label"])
        overlap = sum(1 for w in want if any(h.startswith(w[:5]) or w.startswith(h[:5]) for h in have if len(h) > 1))
        coverage = overlap / len(want) if want else 0
        ratio = agent.difflib.SequenceMatcher(None, name.casefold(), it["label"].casefold()[:len(name) + 20]).ratio()
        # most of the planned name must be in the label ("Credit card number" is not "Mobile Number")
        return coverage + ratio if coverage >= 0.6 or ratio >= 0.85 else 0

    best = max(fields, key=score)
    return best if score(best) > 0 else None


def same_text(got, want: str) -> bool:
    """Does the field hold what was typed? Sites reformat input ('34ABC123' → '34 ABC 123',
    '5321234567' → '(532) 123 45 67'), so spaces, punctuation and case are ignored."""
    if not isinstance(got, str):
        return False
    norm_ = lambda t: re.sub(r"[\s\-().+_/]", "", t).casefold()  # noqa: E731
    return norm_(got) == norm_(want) or (norm_(want) != "" and norm_(want) in norm_(got))


def type_into(it: dict, value: str, ctx) -> str | None:
    """Type into a field the way a person does: click it (a real click: focuses the test window and the
    field whatever else holds the keyboard, e.g. a screen-sharing bar), select all, paste. Falls back to
    setting the value through the accessibility API. Returns None when the field holds the value,
    otherwise what the field shows."""
    el = it.get("el")
    if el is None or ctx is None or ctx.dry_run:
        return None
    agent.reveal(it)
    if not agent.mouse_click(it):
        agent.AXUIElementSetAttributeValue(el, "AXFocused", True)
    time.sleep(0.15)
    # keys go straight to the browser (not to whatever app is in front)
    if not agent.press_keys(ctx, agent.KEYCODES["a"], ["command"]):
        agent.keystroke(ctx, "a", ["command"])
    if value:
        agent.paste_text(value, ctx)
    elif not agent.press_keys(ctx, 51):
        agent.key(51, ctx)   # delete: leave it empty on purpose
    time.sleep(0.25)
    got = agent.ax(el, "AXValue")
    secure = "secure" in str(agent.ax(el, "AXSubrole") or "").casefold()
    if (secure and got is None) or same_text(got, value) or (value == "" and not (got or "").strip()):
        return None
    if agent.AXUIElementSetAttributeValue(el, "AXValue", value) == 0:   # no keyboard: set it directly
        time.sleep(0.15)
        got = agent.ax(el, "AXValue")
        if same_text(got, value) or (value == "" and not (got or "").strip()):
            return None
    return str(got)[:60] if got is not None else "(unreadable)"


def fill_form(fills: list[dict], ctx, app: str, account: dict | None) -> list[str]:
    """Fill fields straight from the plan (no decision loop per field). Returns what was done;
    raises RuntimeError when a field cannot be found or filled."""
    done = []
    for f in fills:
        name, value = f.get("field", ""), secret(str(f.get("value", "")), account)
        it, fields = None, []
        for attempt in range(3):   # the form may still be rendering, a banner may cover it, focus may be elsewhere
            fields = [i for i in agent.read_screen(app) if i.get("page") and i["role"] in FIELD_ROLES]
            it = best_field(name, fields)
            if it is not None:
                break
            if WINDOW is not None:
                WINDOW.ensure_front()
            dismiss_overlays(app)
            time.sleep(1.0)
        if it is None:
            raise RuntimeError(f"no field like '{name}' on the page. " + screen_note(app, fields))
        kind = field_kind(it)
        if kind in ("checkbox", "radio"):
            want = value.casefold() not in ("uncheck", "unchecked", "false", "no", "0", "off")
            on = str(agent.ax(it["el"], "AXValue")) in ("1", "True", "true")
            if want != on or kind == "radio":
                agent.press(it, ctx)
            done.append(f"{'checked' if want else 'unchecked'} '{it['label'][:40]}'")
        elif kind == "select":
            agent.press(it, ctx)
            time.sleep(0.6)
            opts = [i for i in agent.read_screen(app) if i.get("popup")]
            pick = max(opts, key=lambda o: agent.difflib.SequenceMatcher(None, value.casefold(),
                                                                          o["label"].casefold()).ratio(), default=None)
            if pick is not None and agent.difflib.SequenceMatcher(None, value.casefold(), pick["label"].casefold()).ratio() > 0.5:
                agent.press(pick, ctx)
            else:   # web selects without a readable list: type the option and confirm
                agent.paste_text(value, ctx)
                agent.key(36, ctx)
            done.append(f"selected '{value[:40]}' in '{it['label'][:40]}'")
        else:
            shows = type_into(it, value, ctx)
            if shows is not None:
                raise RuntimeError(f"could not type into '{it['label'][:40]}': it shows {shows!r}")
            if value == "":
                done.append(f"cleared '{it['label'][:40]}'")
                time.sleep(0.2)
                continue
            shown = "••••••" if kind == "password" or (account and value == account.get("password")) else value[:40]
            done.append(f"filled '{it['label'][:40]}' = {shown}")
        time.sleep(0.2)
    return done


Q_OPEN, Q_CLOSE = "\"“'‘", "\"”'’"
STEP_CLICK = re.compile(rf"^\s*(?:click|press|tap|select|open|toggle|expand|collapse|choose|check|uncheck)\b"
                        rf"[^{Q_OPEN}]*[{Q_OPEN}](?P<label>.+)[{Q_CLOSE}]", re.I)
STEP_TYPE = re.compile(rf"^\s*(?:type|enter|write|fill(?: in)?)\s+[{Q_OPEN}](?P<value>.*?)[{Q_CLOSE}]\s+"
                       rf"(?:in|into)\s+(?:the\s+)?(?:[{Q_OPEN}](?P<qfield>.+)[{Q_CLOSE}]|(?P<field>.+?))"
                       rf"(?:\s+(?:input\s+)?(?:field|box|input|area|textbox))?\s*$", re.I)


def label_candidates(text: str) -> list[str]:
    """'Kullanıcı Sözleşmesi'ni okudum' checkbox → the longest quoted span first, then the shortest
    (a label may contain an apostrophe; a step may quote two things)."""
    out = [text]
    m = re.match(rf"(.+?)[{Q_CLOSE}]", text)
    if m:
        out.append(m.group(1))
    return list(dict.fromkeys(t.strip() for t in out if t.strip()))


CONTROL_RANK = {"AXCheckBox": 5, "AXRadioButton": 5, "AXPopUpButton": 4, "AXButton": 4, "AXMenuItem": 4,
                "AXTab": 4, "AXLink": 3, "AXGroup": 3, "AXListItem": 3, "AXGenericElement": 3, "AXSection": 3,
                "AXArticle": 3, "AXCell": 2, "AXRow": 2, "AXStaticText": 0, "AXHeading": 0, "AXImage": 0}


def toggled(it: dict, before) -> bool:
    """Did a checkbox / radio / selectable card change its selected state?"""
    el = it.get("el")
    if el is None:
        return True
    for attr in ("AXValue", "AXSelected"):
        now = agent.ax(el, attr)
        if now is not None and before.get(attr) is not None and now != before.get(attr):
            return True
    return False


def direct_step(step: str, ctx, app: str) -> list[str] | None:
    """'click the 'İLERİ' button' / 'type 'x' into the Email field': done straight from the visible label,
    without planning or interpreting the step. None if the step is not that simple or the label is not
    clearly on the page (the agent loop then takes over)."""
    m = STEP_TYPE.match(step)
    if m:
        field = m.group("qfield") or m.group("field")
        for f in label_candidates(field):
            try:
                return fill_form([{"field": f, "value": m.group("value")}], ctx, app, None)
            except RuntimeError:
                continue
        return None
    m = STEP_CLICK.match(step)
    if not m:
        return None
    items = [i for i in agent.read_screen(app) if i.get("page") and (i.get("pressable") or i["role"] in FIELD_ROLES)]
    best = None
    for label in label_candidates(m.group("label")):
        def score(it, label=label):
            a, b = " ".join(fold(label)), " ".join(fold(it["label"].split(" | ")[0]))
            if not a or not b:
                return 0
            return 3 if a == b else 2 if b.startswith(a) or a.startswith(b) else 1 if a in b else 0
        # among clear matches, the control itself (checkbox, radio, card, button) beats its caption text
        # any control beats caption text; among controls an exact label beats a longer one, then the control kind
        cand = max(items, key=lambda i: (score(i) >= 2, CONTROL_RANK.get(i["role"], 0) > 0, score(i),
                                         CONTROL_RANK.get(i["role"], 0), -len(i["label"])), default=None)
        if cand is not None and score(cand) >= 2:
            best = cand
            break
    if best is None:
        return None
    before = agent.screen_signature(agent.read_screen(app))
    el = best.get("el")
    state0 = {a: agent.ax(el, a) for a in ("AXValue", "AXSelected")} if el is not None else {}
    selectable = best["role"] in ("AXCheckBox", "AXRadioButton") or any(v is not None for v in state0.values())
    agent.press(best, ctx)
    after = agent.wait_for_change(app, before, timeout=2.5)
    changed = agent.screen_signature(after) != before
    if (not changed or (selectable and not toggled(best, state0))) and agent.mouse_click(best):
        after = agent.wait_for_change(app, before, timeout=2.0)   # AXPress ignored: a real click
        changed = agent.screen_signature(after) != before
    note = ""
    if selectable:
        note = " (selected state changed)" if toggled(best, state0) else " (but its selected state did not change)"
    return [f"clicked '{best['label'][:50]}'" + ("" if changed else " (but no change on screen)") + note]


def start_markers(test: dict) -> list[str]:
    """What must be on screen for the test to start: its first fields and the labels its steps use."""
    marks = [f["field"] for f in (test.get("fill") or [])[:2] if f.get("field")]
    for step in test.get("steps", [])[:1]:
        m = STEP_CLICK.match(step)
        if m:
            marks.append(label_candidates(m.group("label"))[-1])
    return marks


def state_ready(test: dict, app: str) -> bool:
    """Look instead of assume: are the test's starting elements on screen?"""
    marks = start_markers(test)
    if not marks:
        return True
    items = [i for i in agent.read_screen(app) if i.get("page")]
    fields = [i for i in items if i["role"] in FIELD_ROLES]
    for mark in marks:
        if best_field(mark, fields) is None and not any(
                " ".join(fold(mark)) and " ".join(fold(mark)) in " ".join(fold(i["label"])) for i in items):
            return False
    return True


def prepare(test: dict, prefix: list[dict], page_url: str, client, ctx, app: str, account) -> str | None:
    """Bring the page to the state the test starts from. Returns None when ready, else why not.
    1) maybe it already is (sites remember form progress across reloads);
    2) else redo the tests that lead there; 3) else start over in a fresh private window and redo them."""
    if state_ready(test, app):
        return None
    for fresh in (False, True):
        if fresh:
            if WINDOW is None:
                break
            WINDOW.reset(page_url)
            goto(app, page_url)
        if prefix and not replay(prefix, client, ctx, app, account, getattr(ctx, "qa_gem", None)):
            continue
        if state_ready(test, app):
            return None
    return ("the page is not in the state this test starts from (looked for: "
            + ", ".join(start_markers(test)) + "). " + screen_note(app, []))


def stopped_now(ctx) -> bool:
    return bool(ctx.stop and ctx.stop())


def worked(steps: list[str]) -> bool:
    return any(s and "FAILED" not in s and not s.startswith("waited") for s in steps)


def adapt(client, gem, ctx, io, app: str, test: dict) -> list[str]:
    """The scripted steps did not reach the expected result. Do what a person would: look at the page
    and finish the goal with what it offers (pick from a suggestion list, choose an option, confirm).
    Uses the full agent (planner on) for a few steps; returns what it did."""
    goal = (f"On this page, reach this goal: {test['title']}. The expected result is: {test['expect']}. "
            "Some steps were already done: " + "; ".join(test.get("steps", [])) + ". Use what the page shows "
            "now (a suggestion list, an option, a button) to finish. Use made-up test data only.")
    saved, ctx.planner, ctx.max_steps = ctx.planner, gem, 4   # a few steps, not a new session
    io.progress("       ↺ adapting to the page")
    try:
        r = agent.handle(client, goal, ctx)
        return [s for s in r.get("steps", []) if s]
    except Exception:
        return []
    finally:
        ctx.planner, ctx.max_steps = saved, None


def replay(prefix: list[dict], client, ctx, app: str, account: dict | None, gem=None) -> bool:
    """Bring the page back to the state a follow-up test starts from: redo the tests that led there.
    A test that only passed by adapting is redone the same way (its steps alone do not get there)."""
    for t in prefix:
        try:
            if t.get("fill"):
                fill_form(t["fill"], ctx, app, account)
            for step in t["steps"]:
                step = secret(step, account)
                done = direct_step(step, ctx, app)
                if done is None and not agent.handle(client, step, ctx).get("ok"):
                    if t.get("_adapted") and gem is not None:
                        adapt(client, gem, ctx, ctx.io, app, t)
                        break
                    return False
        except Exception:
            return False
    time.sleep(1.0)
    return True


LOGIN_URL = re.compile(r"login|signin|sign-in|sign_in|/auth|oauth|sso\.|/giris|uye-girisi|account/log", re.I)


def run_test(client, gem, ctx, io, app: str, page_url: str, test: dict, out_dir: Path, number: int,
             account: dict | None = None, prefix: list[dict] | None = None) -> dict:
    t0 = time.time()
    io.lines, io.blocked = [], []
    ctx.history.clear()
    rec = {"page": page_url, "title": test["title"], "kind": test.get("kind", ""),
           "steps": [masked(secret(s, account), account) for s in test["steps"]],
           "expect": test["expect"], "status": "", "confidence": None, "note": "", "agent_steps": [],
           "screenshot": None}
    io.progress(f"    ▸ {test['title']}")
    if prefix:
        rec["after"] = " → ".join(t["title"] for t in prefix)
    why = prepare(test, prefix or [], page_url, client, ctx, app, account)
    if why:
        rec["status"], rec["note"] = "error", f"Could not start: {why}"
    rec["fill"] = [{"field": f.get("field"), "value": masked(secret(str(f.get("value", "")), account), account)}
                   for f in test.get("fill") or []]
    before = None
    if not rec["status"]:
        b = screen_for_check(app)
        before = {k: b[k] for k in ("url", "title", "headings")} | {"text_on_page": b["text_on_page"][:40]}
    if test.get("fill") and not rec["status"]:
        if WINDOW is not None:
            WINDOW.ensure_front()
        try:
            rec["agent_steps"] += [f"fill → {s}" for s in fill_form(test["fill"], ctx, app, account)]
        except Exception as e:
            rec["agent_steps"].append(f"fill → FAILED ({e})")
            rec["status"], rec["note"] = "error", f"Could not fill the form: {e}"
    for step in ([] if rec["status"] else test["steps"]):
        step = secret(step, account)
        if WINDOW is not None:
            WINDOW.ensure_front()   # the user may have switched windows: act only in the test window
        blocked_before = len(io.blocked)
        try:
            done = direct_step(step, ctx, app)   # simple steps by their visible label, no interpretation
            r = {"ok": True, "steps": done} if done is not None else agent.handle(client, step, ctx)
        except Exception as e:
            r = {"ok": False, "steps": [f"error: {e}"]}
        shown = masked(step, account)
        rec["agent_steps"] += [masked(f"{shown} → {s}", account) for s in r.get("steps", [])] or [f"{shown} → (nothing done)"]
        if len(io.blocked) > blocked_before and HARD_STOP.search(io.blocked[-1]):
            rec["status"] = "blocked"
            rec["note"] = f"Not run for safety: '{step}' needs '{io.blocked[-1][:80]}'"
            break
        if not r.get("ok"):
            why = io.blocked[-1] if io.blocked else (r.get("steps") or ["could not be done"])[-1]
            hard = split_expectation(test["expect"])[0]
            if gem is not None and not io.blocked and not (NEGATIVE.search(hard) or NO_ADAPT.search(hard)):
                adapted = adapt(client, gem, ctx, io, app, test)   # this step did not work: reach the goal another way
                if worked(adapted):
                    rec["agent_steps"] += [f"adapted → {x}" for x in adapted]
                    st, conf, _ = check(client, gem, app, hard, before)
                    if st == "passed":
                        rec["status"], rec["confidence"], rec["adapted"] = "passed", conf, True
                        rec["note"] = f"Step '{step}' did not work as written; reached by adapting: " + "; ".join(adapted[-4:])
                        break
            rec["status"] = "error"
            rec["note"] = masked(f"Step could not be completed: '{step}' — {why}", account)
            break
    if not rec["status"]:
        if WINDOW is not None:
            WINDOW.ensure_front()
        hard, soft = split_expectation(test["expect"])
        rec["status"], rec["confidence"], rec["note"] = check(client, gem, app, hard, before)
        if rec["status"] == "passed" and soft:   # the function works; is the user told why?
            st, _, _ = check(client, gem, app, soft, before)
            if st != "passed":
                rec["ux"] = f"No visible feedback: expected '{soft}'"
        elif rec["status"] == "failed" and gem is not None and not (NEGATIVE.search(hard) or NO_ADAPT.search(hard)) \
                and not stopped_now(ctx):
            adapted = adapt(client, gem, ctx, io, app, test)   # a person would use what the page shows
            if worked(adapted):
                rec["agent_steps"] += [f"adapted → {s}" for s in adapted]
                st, conf, note = check(client, gem, app, hard, before)
                if st == "passed":
                    rec["status"], rec["confidence"] = "passed", conf
                    rec["note"] = "Reached by adapting to the page: " + "; ".join(adapted[-4:])
                    rec["adapted"] = True
    now = current_url(app) or ""
    if rec["status"] in ("failed", "error") and LOGIN_URL.search(now) and not account:
        rec["status"] = "blocked"
        rec["note"] = f"Needs a login: it went to {urllib.parse.urlsplit(now).netloc} (set a test account to test it)"
    if rec["status"] != "passed":
        rec["screenshot"] = screenshot(app, out_dir / f"test{number}.png")
    rec["duration_s"] = round(time.time() - t0, 1)
    mark = {"passed": "✓", "failed": "✗", "error": "✗", "blocked": "–"}[rec["status"]]
    if rec.get("adapted"):
        rec["status_label"] = "passed (adapted)"
    conf = f" ({rec['confidence']:.2f})" if rec["confidence"] is not None else ""
    io.progress(f"     {mark} {rec.get('status_label', rec['status'])}{conf}"
                + (f" — {rec['note']}" if rec["status"] != "passed" else "")
                + (f" · {rec['ux']}" if rec.get("ux") else ""))
    return rec


# ---------------------------------------------------------------- reports
def write_reports(r: dict, out: Path) -> dict:
    (out / "results.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    # JUnit XML for CI
    cases = []
    for t in r["tests"]:
        body = ""
        if t["status"] == "failed":
            body = f'<failure message="{xml_escape(t["note"], {chr(34): "&quot;"})}">{xml_escape(chr(10).join(t["agent_steps"]))}</failure>'
        elif t["status"] == "blocked":
            body = f'<skipped message="{xml_escape(t["note"], {chr(34): "&quot;"})}"/>'
        elif t["status"] == "error":
            body = f'<error message="{xml_escape(t["note"], {chr(34): "&quot;"})}">{xml_escape(chr(10).join(t["agent_steps"]))}</error>'
        cases.append(f'  <testcase classname="{xml_escape(t["page"])}" name="{xml_escape(t["title"])}" '
                     f'time="{t.get("duration_s", 0)}">{body}</testcase>')
    fails = sum(t["status"] == "failed" for t in r["tests"])
    errors = sum(t["status"] == "error" for t in r["tests"])
    (out / "junit.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="AgentP · {xml_escape(r["scope"])}" '
        f'tests="{len(r["tests"])}" failures="{fails}" errors="{errors}" time="{r.get("duration_s", 0)}">\n'
        + "\n".join(cases) + "\n</testsuite>\n", encoding="utf-8")
    (out / "report.html").write_text(html_report(r), encoding="utf-8")
    return {"html": str(out / "report.html"), "json": str(out / "results.json"), "junit": str(out / "junit.xml")}


def html_report(r: dict) -> str:
    e = lambda s: html.escape(str(s or ""))  # noqa: E731
    passed = sum(t["status"] == "passed" for t in r["tests"])
    failed = sum(t["status"] in ("failed", "error") for t in r["tests"])
    sev_order = {"high": 0, "medium": 1, "low": 2}
    issues = sorted(r["issues"], key=lambda i: sev_order.get(i["severity"], 3))
    issue_rows = "".join(
        f'<tr><td><span class="sev {e(i["severity"])}">{e(i["severity"])}</span></td><td>{e(i["what"])}'
        f'{"<div class=muted>" + e(i["detail"]) + "</div>" if i["detail"] else ""}</td>'
        f'<td class="url">{e(i["page"])}</td>'
        f'<td>{"<a href=" + chr(34) + e(i["screenshot"]) + chr(34) + ">image</a>" if i.get("screenshot") else ""}</td></tr>'
        for i in issues) or '<tr><td colspan="4" class="muted">No issues found.</td></tr>'
    def group(t):
        return "After: " + t["after"] if t.get("after") else "Starting state"
    ordered = sorted(r["tests"], key=lambda t: (len(t.get("after", "").split(" → ")) if t.get("after") else 0, group(t)))
    heads, tests = set(), ""
    for t in ordered:
        if group(t) not in heads:
            heads.add(group(t))
            tests += f'<h3 class="state">{e(group(t))}</h3>'
        tests += one_test(t)
    tests = tests or '<p class="muted">No tests were run.</p>'
    pages = "".join(
        f'<tr><td>{e(p["title"] or p["url"])}<div class="muted url">{e(p["url"])}</div></td><td>{e(p["purpose"])}</td>'
        f'<td>{e(p["status"])}</td><td>{p["load_ms"] / 1000:.1f} s</td><td>{p["tests"]}</td></tr>' for p in r["pages"])
    return page_html(r, e, passed, failed, issue_rows, tests, pages)


def one_test(t: dict) -> str:
    e = lambda s: html.escape(str(s or ""))  # noqa: E731
    return (
        f'<details class="test {e(t["status"])}"><summary><span class="dot"></span><b>{e(t["title"])}</b>'
        f'<span class="muted"> · {e(t["kind"])} · {t.get("duration_s", 0)} s</span><span class="st">{e(t.get("status_label", t["status"]))}</span></summary>'
        f'<div class="body"><div class="muted url">{e(t["page"])}</div>'
        f'{"<p class=muted>Starts after: " + e(t["after"]) + "</p>" if t.get("after") else ""}'
        f'{"<h4>Form data</h4><ul>" + "".join("<li>" + e(f["field"]) + " = " + (e(f["value"]) or "<i>empty</i>") + "</li>" for f in t.get("fill", [])) + "</ul>" if t.get("fill") else ""}'
        f'<h4>Steps</h4><ul>{"".join("<li>" + e(s) + "</li>" for s in t["steps"])}</ul>'
        f'<h4>Expected</h4><p>{e(t["expect"])}</p>'
        f'{"<h4>Result</h4><p>" + e(t["note"]) + "</p>" if t["note"] else ""}'
        f'{"<h4>UX</h4><p>" + e(t["ux"]) + "</p>" if t.get("ux") else ""}'
        f'{"<p class=muted>Confidence " + format(t["confidence"], ".2f") + "</p>" if t["confidence"] is not None else ""}'
        f'<h4>What the agent did</h4><ul class="mono">{"".join("<li>" + e(s) + "</li>" for s in t["agent_steps"])}</ul>'
        f'{"<img src=" + chr(34) + e(t["screenshot"]) + chr(34) + ">" if t.get("screenshot") else ""}</div></details>')


def page_html(r: dict, e, passed: int, failed: int, issue_rows: str, tests: str, pages: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>QA report · {e(r['scope'])}</title>
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--card:#f5f5f7;--ok:#248a3d;--bad:#d70015;--warn:#b25000}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1c1e;--fg:#f5f5f7;--muted:#98989d;--line:#38383a;--card:#2c2c2e;--ok:#30d158;--bad:#ff453a;--warn:#ff9f0a}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}}
main{{max-width:980px;margin:0 auto;padding:40px 24px 80px}}h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:16px;margin:36px 0 12px}}
h4{{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:16px 0 4px}}
.muted{{color:var(--muted)}}.url{{font-size:12px;word-break:break-all}}.mono{{font:12px/1.5 ui-monospace,Menlo,monospace}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-top:24px}}
.card{{background:var(--card);border-radius:12px;padding:16px}}.card b{{display:block;font-size:26px;font-weight:600}}
.card.ok b{{color:var(--ok)}}.card.bad b{{color:var(--bad)}}.card.warn b{{color:var(--warn)}}
table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:12px;color:var(--muted);font-weight:500}}
.sev{{font-size:11px;font-weight:600;text-transform:uppercase;padding:2px 8px;border-radius:99px;background:var(--card)}}
.sev.high{{color:var(--bad)}}.sev.medium{{color:var(--warn)}}.sev.low{{color:var(--muted)}}
details.test{{border:1px solid var(--line);border-radius:12px;margin:8px 0;overflow:hidden}}
details.test summary{{list-style:none;cursor:pointer;padding:12px 16px;display:flex;align-items:center;gap:8px}}
details.test summary::-webkit-details-marker{{display:none}}.st{{margin-left:auto;font-size:12px;color:var(--muted)}}
.dot{{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}}
.passed .dot{{background:var(--ok)}}.failed .dot,.error .dot{{background:var(--bad)}}
h3.state{{font-size:13px;font-weight:600;color:var(--muted);margin:22px 0 6px}}
.body{{padding:0 16px 16px 32px}}.body img{{max-width:100%;border:1px solid var(--line);border-radius:8px;margin-top:12px}}
a{{color:inherit}}
</style></head><body><main>
<h1>{e(r['scope'])}</h1>
<div class="muted">Exploratory functional test by AgentP · {e(r['started'])} · {e(r['browser'])} ·
{r.get('duration_s', 0) // 60} min {r.get('duration_s', 0) % 60} s · form submission {'allowed' if r['allow_submit'] else 'off'}</div>
<div class="cards">
<div class="card"><span class="muted">Pages</span><b>{len(r['pages'])}</b></div>
<div class="card"><span class="muted">Tests</span><b>{len(r['tests'])}</b></div>
<div class="card ok"><span class="muted">Passed</span><b>{passed}</b></div>
<div class="card {'bad' if failed else ''}"><span class="muted">Failed</span><b>{failed}</b></div>
<div class="card {'warn' if r['issues'] else ''}"><span class="muted">Issues</span><b>{len(r['issues'])}</b></div>
<div class="card"><span class="muted">Links checked</span><b>{r.get('links_checked', 0)}</b></div>
</div>
<h2>Issues</h2><table><tr><th>Severity</th><th>Issue</th><th>Page</th><th></th></tr>{issue_rows}</table>
<h2>Tests</h2>{tests}
<h2>Pages</h2><table><tr><th>Page</th><th>Purpose</th><th>HTTP</th><th>Load</th><th>Tests</th></tr>{pages}</table>
</main></body></html>"""


# ---------------------------------------------------------------- entry points
TLD = r"(com|net|org|io|ai|dev|app|co|edu|gov|gen|info|biz|tv|me|bel|k12)"
CC = r"(tr|uk|de|us|fr|nl|eu|az|it|es|ru|ch|at|be|ca|au)"
SPOKEN_DOMAIN = re.compile(rf"\b([a-z0-9][a-z0-9-]*(?:[ .][a-z0-9][a-z0-9-]*)??)[ .]+{TLD}(?:[ .]+{CC})?\b", re.I)
FILLER = set("""test tests testi testini testleri testlerini testlerin et eder edin edebilir misin mısın yap yapar
yapın yapabilir fonksiyon fonksiyonel fonksiyonları fonksiyonlarını functional function functions feature features
özellik özellikleri özelliklerini the of on at in for and its please lütfen site sitesi sitesinin sitesinde
website web page sayfa sayfası sayfasını sayfasının sayfayı nin nın nun nün in ın un ün de da için ve qa check
kontrol tüm bütün whole all everything bu şu buradaki burada here this current these şuradaki go to it git gir open aç""".split())
PATH_SEP = re.compile(r"\s*(?:>|→|->|»| sonra | then )\s*", re.I)


CLI_NOISE = re.compile(r"(?:^|\s)(?:\.?/?run\.sh|python3?|\.venv/bin/python|qa\.py)(?=\s|$)|"
                       r"(?:^|\s)--[\w-]+(?:[ =]\d+(?:\.\d+)?)?(?=\s|$)", re.I)


def local_parse(cmd: str) -> dict:
    """No model: 'ford com tr kampanyalar sayfasını test et' → ford.com.tr + page 'kampanyalar'.
    'ford com tr > binek araçlar > kuga' → a path of pages to click through."""
    cmd = CLI_NOISE.sub(" ", cmd)   # "./run.sh --test ford.com.tr" pasted into the panel
    text = re.sub(r"\b(nokta|dot)\b", ".", cmd, flags=re.I)
    m = URL_IN.search(text)
    site, rest = None, text
    if m and "." in m.group(0):
        site, rest = m.group(0), text[:m.start()] + " " + text[m.end():]
    else:
        m = SPOKEN_DOMAIN.search(text)
        if m:
            words = [w for w in re.split(r"[ .]+", m.group(1)) if w]
            while len(words) > 1 and words[0].casefold() in FILLER | {"go", "open", "git", "aç", "to"}:
                words.pop(0)   # "test ford com tr": "test" is the command, not part of the name
            name = "".join(words)   # spoken names have no spaces in domains ("hepsi burada" → hepsiburada)
            site = ".".join(x for x in (name, m.group(2), m.group(3)) if x).lower()
            rest = text[:m.start()] + " " + text[m.end():]
    rest = re.sub(r"^['’]?\w{0,3}\b", "", rest.strip()) if rest.strip()[:1] in "'’" else rest   # "'nin"
    hops = []
    for part in PATH_SEP.split(rest):
        words = [w for w in re.findall(r"(?<![\w.])/[\w/-]+|[\w-]+", part) if w.casefold() not in FILLER]
        if words:
            hops.append(" ".join(words))
    return {"site": site, "hops": hops}


def parse_request(cmd: str, gem) -> dict | None:
    """A test request in the user's words → {site, this_page, page_hint, focus}. None if it is not one."""
    if not TEST_REQUEST.search(cmd):
        return None
    app = browser()
    open_url = current_url(app) if agent.find_running(app) else ""
    this_page = re.search(r"\b(this|current) (page|form|site)\b|\bbu (sayfa|form|site)|buradaki|şu an açık|açık olan", cmd, re.I)
    loc = local_parse(cmd)
    req = {"site": loc["site"] or "", "this_page": bool(this_page) and not loc["site"], "hops": loc["hops"],
           "page_hint": " > ".join(loc["hops"]), "focus": ""}
    if not req["site"] and not req["this_page"] and gem is not None:   # unusual wording: let Gemini read it
        try:
            g = gem.qa_request(cmd, open_url)
            req |= {"site": g.get("site", ""), "this_page": g.get("this_page", False),
                    "hops": [g["page_hint"]] if g.get("page_hint") else [], "page_hint": g.get("page_hint", ""),
                    "focus": g.get("focus", "")}
        except Exception:
            pass
    if GENERIC_FOCUS.match(req.get("focus") or ""):
        req["focus"] = ""   # "do the functional tests" = everything
    if req.get("site"):
        m = URL_IN.search(req["site"])
        req["site"] = m.group(0) if m else req["site"].split("'")[0].split("’")[0]
    if req.get("site") and urllib.parse.urlsplit(("https://" if "://" not in req["site"] else "") + req["site"]).path.strip("/") \
            and not req.get("hops"):
        req["single_page"] = True   # a full page address: test that page
    if req.get("this_page"):
        if not open_url.startswith("http"):
            return {"error": "No web page is open in the browser to test."}
        req["focus"] = req["focus"] or req["page_hint"]   # "the form here": it is on this page
        req["site"], req["page_hint"], req["hops"] = open_url, "", []
    if not req.get("site"):
        return {"error": "Which site? Say e.g. 'test example.com' or open the page and say 'test this page'."}
    return req


HOP_GENERIC = {"form", "formu", "forms", "page", "pages", "sayfa", "sayfası", "sayfasi", "bölüm", "bölümü", "section",
               "modül", "modülü", "module", "kısmı", "kismi", "tab", "sekme", "sekmesi"}


def fold(text: str) -> list[str]:
    """Words for matching, accents removed: 'İletişim' / 'iletisim', 'Araçlar' / 'araclar' are the same."""
    import unicodedata
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", plain.replace("ı", "i"))


def link_match(hint: str, link: dict) -> float:
    """How clearly a link is the page the user named: its label or its address contains the words."""
    want = [w for w in fold(hint) if len(w) > 1 and w not in {fold(g)[0] for g in HOP_GENERIC}]
    if not want:
        return 0.0
    path = urllib.parse.urlsplit(link["url"]).path.replace("-", " ").replace("_", " ").replace("/", " ")
    have = fold(link["label"] + " " + path)
    hit = sum(1 for w in want if any(h.startswith(w[:6]) or w.startswith(h[:6]) for h in have if len(h) > 2))
    return hit / len(want)


def pick_link(client, app: str, hint: str, snap: dict) -> str | None:
    """The link to the page the user named. A clear label / address match is taken at once (no model);
    otherwise Jev chooses among the links."""
    from typesafe_sdk import Choice
    if re.match(r"^/|^https?://", hint):   # an address or a path was given
        return urllib.parse.urljoin(snap["url"], hint)
    links = [l for l in snap["links"] if l.get("url")][:120]
    if not links:
        return None
    def rank(l):   # all words found; then found in the link's own text; then the higher-level page
        label_only = link_match(hint, {"label": l["label"], "url": "https://x/"})
        return (link_match(hint, l), label_only, -urllib.parse.urlsplit(l["url"]).path.count("/"), -len(l["label"]))
    best = max(links, key=rank)
    if link_match(hint, best) >= 0.99 or (link_match(hint, best) >= 0.6 and len(agent.tokens(hint)) > 1):
        return best["url"]
    opts = {f"l{i}": f"{l['label']} ({urllib.parse.urlsplit(l['url']).path})" for i, l in enumerate(links)}
    opts["none"] = "None of these"
    a = agent.jev(client, {"user_wants_page": hint, "site": snap["url"]},
                  {"link": Choice(instructions=f"Which link opens the '{hint}' page?", criteria=opts)}, {}, "link")
    ch = a["link"]
    return links[int(ch.choice[1:])]["url"] if ch.choice != "none" and ch.confidence >= 0.4 else None


def run_request(cmd: str, client, base_io, allow_submit: bool, gem=None, stop=None) -> dict:
    """Panel / terminal entry: understand the request, then test the site, a page of it, or this page."""
    req = parse_request(cmd, gem)
    if req is None:
        return {}
    if req.get("error"):
        base_io.say(f"✗ {req['error']}")
        return {}
    focused = bool(req.get("this_page") or req.get("page_hint") or req.get("focus") or req.get("single_page"))
    if req.get("hops") and not req.get("focus"):
        req["focus"] = req["hops"][-1]   # "kayıt formu" is also what to focus the tests on
    single = req.get("this_page") or req.get("page_hint") or req.get("single_page")
    return run_session(req["site"], client, base_io, pages=1 if single else 6,
                       tests_per_page=8 if focused else 3, allow_submit=allow_submit,
                       focus=req.get("focus", ""), page_hint=req.get("page_hint", ""), stop=stop)


def test_target(cmd: str) -> str | None:
    """'test example.com', 'go to example.com and test its features' → 'example.com'. None if not a test request."""
    if not TEST_REQUEST.search(cmd):
        return None
    m = URL_IN.search(cmd)
    if m:
        return m.group(0).rstrip(".,;")
    m = re.match(r"^\s*(?:test|qa)\s+(.+?)(?:\s+(?:site|website))?\s*$", cmd, re.I)
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser(description="AgentP exploratory functional testing")
    ap.add_argument("site", nargs="+", help="what to test, as in the panel: 'ford.com.tr', 'ford com tr kampanyalar', "
                                             "'ford.com.tr > binek araçlar > kuga', 'https://site.com/page'")
    ap.add_argument("--pages", type=int, default=6, help="pages to explore (default 6)")
    ap.add_argument("--minutes", type=float, default=15, help="time budget (default 15)")
    ap.add_argument("--tests", type=int, default=3, help="tests per page (default 3)")
    ap.add_argument("--no-submit", action="store_true", help="never submit forms, only test validation")
    ap.add_argument("--yes", action="store_true", help="do not ask before starting (CI)")
    ap.add_argument("--out", help="report directory (default ~/.agentp/qa/<time>-<site>)")
    ap.add_argument("--user", help="username / email of a TEST account on the site (for login tests)")
    ap.add_argument("--password", help="its password (asked hidden if --user is given without it)")
    args = ap.parse_args()
    agent.check_env()
    if not agent.AXIsProcessTrusted():
        sys.exit(agent.PERM_MSG)
    request = " ".join(args.site)
    allow = not args.no_submit
    if allow and not args.yes:
        a = input(f"AgentP will click through {request}, fill forms with made-up test data and submit them.\n"
                  "It never pays, buys, deletes, logs out or contacts anyone. Allow form submission? (Y/n) ")
        allow = a.strip().casefold() in ("", "y", "yes")
    account = None
    if args.user:
        from getpass import getpass
        account = {"username": args.user, "password": args.password or getpass("Test account password: ")}
    from typesafe_sdk import TypeSafeClient
    req = parse_request("test " + request, None) or {}
    if req.get("error") or not req.get("site"):
        sys.exit(req.get("error") or f"Could not tell which site to test from '{request}'.")
    single = req.get("this_page") or req.get("page_hint") or req.get("single_page")
    focus = req.get("focus") or (req["hops"][-1] if req.get("hops") else "")
    r = run_session(req["site"], TypeSafeClient(), agent.CliIO(), 1 if single and args.pages == 6 else args.pages,
                    args.minutes, 8 if single and args.tests == 3 else args.tests, allow,
                    Path(args.out) if args.out else None, account,
                    focus=focus, page_hint=req.get("page_hint", ""))
    if r.get("report"):
        subprocess.run(["open", r["report"]["html"]], check=False)
    bad = [t for t in r.get("tests", []) if t["status"] in ("failed", "error")]
    sys.exit(1 if (bad or any(i["severity"] == "high" for i in r.get("issues", []))) else 0)


if __name__ == "__main__":
    main()
