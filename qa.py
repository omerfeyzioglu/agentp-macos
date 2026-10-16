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
TEST_REQUEST = re.compile(r"^\s*(test|qa)\s+(\S+)|"
                          r"\b(test|qa)\b.*\b(feature|function|functionality|site|website|page)s?\b|"
                          r"fonksiyon(el)? test|functional test", re.I)
URL_IN = re.compile(r"(https?://\S+|\b[\w-]+(\.[\w-]+)+\.[a-z]{2,}\S*|\b[\w-]+\.(com|net|org|io|ai|dev|app|co|tr|edu|gov)\b\S*)", re.I)


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


def current_url(app: str) -> str:
    if app == "Safari":
        return osa_get('tell application "Safari" to get URL of front document')
    return osa_get(f'tell application "{app}" to get URL of active tab of front window')


def goto(app: str, url: str) -> float:
    """Load url in the current tab (no tab pile-up). Returns the load time in ms."""
    t = time.perf_counter()
    before = agent.screen_signature(agent.read_screen(app)) if agent.find_running(app) else ""
    if not agent.find_running(app):
        subprocess.run(["open", "-a", app, url], check=False)
    elif app == "Safari":
        osa_get(f'tell application "Safari" to set URL of front document to "{url}"')
    else:
        done = osa_get(f'tell application "{app}" to set URL of active tab of front window to "{url}"\nreturn "ok"')
        if not done:
            subprocess.run(["open", "-a", app, url], check=False)
    agent.activate(app, agent.Ctx(installed=[]))
    agent.wait_for_change(app, before, timeout=15.0, min_items=5)
    return (time.perf_counter() - t) * 1000


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
    title = osa_get(f'tell application "{app}" to get title of active tab of front window') if app != "Safari" \
        else osa_get('tell application "Safari" to get name of front document')
    return {
        "url": current_url(app), "title": title, "items": items,
        "headings": [i["label"][:100] for i in page if i["role"] == "AXHeading"][:30],
        "links": links[:120],
        "buttons": [i["label"][:60] for i in page if i["role"] in ("AXButton", "AXMenuButton", "AXPopUpButton",
                                                                   "AXCheckBox", "AXRadioButton", "AXTab")][:60],
        "fields": [i["label"][:60] for i in page if i["role"] in agent.TEXT_ROLES][:30],
        "texts": [i["label"][:160] for i in page if i["role"] in agent.READ_ROLES][:200],
    }


def screen_for_check(app: str) -> dict:
    snap = snapshot(app)
    return {"url": snap["url"], "title": snap["title"], "headings": snap["headings"][:20],
            "text_on_page": snap["texts"][:150], "buttons": snap["buttons"][:40], "fields": snap["fields"][:20],
            "links": [l["label"] for l in snap["links"][:60]]}


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
def check(client, gem, app: str, expectation: str) -> tuple[str, float, str]:
    """Is the expected result on screen? Jev answers yes/no with a probability; polled for a few
    seconds (results appear asynchronously); Gemini decides only when Jev stays unsure."""
    from typesafe_sdk import Noul
    t0, p, screen = time.time(), 0.5, {}
    while time.time() - t0 < CHECK_TIMEOUT:
        screen = screen_for_check(app)
        a = agent.jev(client, {"functional_test_check": expectation, "screen": screen},
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
            ok, reason = gem.judge(expectation, screen)
            return ("passed" if ok else "failed"), p, f"second opinion: {reason}"
        except Exception:
            pass
    return "failed", p, "the expected result is not on screen"


# ---------------------------------------------------------------- one session
def resolve_target(target: str, gem) -> str | None:
    target = target.strip().strip("'\"")
    if re.match(r"^https?://", target):
        return target
    if "." in target and " " not in target:
        return "https://" + target
    if gem is not None:   # a name ("hepsiburada", "the sakarya university site") → official website
        try:
            for f in gem.lookup(f"official website of {target}", []):
                site = (f.get("links") or {}).get("website")
                if site:
                    return site
        except Exception:
            pass
    return None


def run_session(target: str, client, base_io, pages: int = 6, minutes: float = 15, tests_per_page: int = 3,
                allow_submit: bool = True, out_dir: Path | None = None) -> dict:
    t_start = time.time()
    ctx = agent.make_ctx(io=None, use_memory=False)
    gem = ctx.planner
    if gem is not None:
        gem.grounding = False   # test data like "QA Tester" must not be looked up on the web
    url = resolve_target(target, gem)
    if not url:
        base_io.say(f"✗ Could not find the site '{target}'. Give its address, e.g. test example.com")
        return {}
    app = browser()
    scope = domain(url)
    in_scope = lambda: domain(current_url(app) or url).endswith(scope)  # noqa: E731
    io = QAIO(base_io, allow_submit, in_scope)
    ctx.io, ctx.record, ctx.active_app = io, False, app
    out_dir = out_dir or QA_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{scope}"
    out_dir.mkdir(parents=True, exist_ok=True)

    result = {"site": url, "scope": scope, "started": time.strftime("%Y-%m-%d %H:%M"), "browser": app,
              "allow_submit": allow_submit, "pages": [], "tests": [], "issues": []}
    issue = lambda sev, page, what, detail="", shot=None: result["issues"].append(  # noqa: E731
        {"severity": sev, "page": page, "what": what, "detail": detail, "screenshot": shot})
    io.progress(f"› Testing {scope} in {app} (up to {pages} pages, {minutes:.0f} min)")

    queue, visited, tested = [url], [], []
    link_pool = ThreadPoolExecutor(8)
    checked_links: dict[str, object] = {}
    deadline = t_start + minutes * 60

    while queue and len(result["pages"]) < pages and time.time() < deadline:
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
        for link in snap["links"]:   # broken links on the same site, checked in the background
            u = link.get("url")
            if u and u.startswith("http") and domain(u).endswith(scope) and norm(u) not in checked_links \
                    and len(checked_links) < MAX_LINKS:
                checked_links[norm(u)] = (link["label"], landed, link_pool.submit(http_status, u))

        # ---- tests proposed by Gemini, run by AgentP, checked by Jev
        ideas = {"page_purpose": "", "tests": [], "next_pages": []}
        if gem is not None:
            try:
                ideas = gem.test_ideas(scope, {k: snap[k] for k in ("url", "title", "headings", "buttons", "fields")}
                                       | {"links": [l["label"] for l in snap["links"]], "texts": snap["texts"][:60]},
                                       tested, visited, tests_per_page, allow_submit)
            except Exception as e:
                io.progress(f"  (could not plan tests here: {str(e)[:80]})")
        page["purpose"] = ideas.get("page_purpose", "")
        for n, test in enumerate(ideas["tests"]):
            if time.time() > deadline:
                break
            if n > 0 or norm(current_url(app) or "") != norm(landed):
                goto(app, landed)   # every test starts from the page itself
            result["tests"].append(run_test(client, gem, ctx, io, app, landed, test, out_dir, len(result["tests"]) + 1))
            tested.append(f"{snap['title']}: {test['title']}")
            page["tests"] += 1
            last = result["tests"][-1]
            for s in last["agent_steps"]:
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
        if not isinstance(st, int) or st >= 400:
            issue("medium" if isinstance(st, int) and st == 404 else "low", page_url, f"Broken link ({st}): {label}", key)
    link_pool.shutdown(wait=False)

    result["duration_s"] = round(time.time() - t_start)
    result["links_checked"] = len(checked_links)
    paths = write_reports(result, out_dir)
    passed = sum(t["status"] == "passed" for t in result["tests"])
    io.progress(f"  Done in {result['duration_s'] // 60} min {result['duration_s'] % 60} s: {len(result['pages'])} pages, "
                f"{passed}/{len(result['tests'])} tests passed, {len(result['issues'])} issues")
    io.progress(f"  Report: {paths['html']}")
    result["report"] = paths
    return result


def run_test(client, gem, ctx, io, app: str, page_url: str, test: dict, out_dir: Path, number: int) -> dict:
    t0 = time.time()
    io.lines, io.blocked = [], []
    ctx.history.clear()
    rec = {"page": page_url, "title": test["title"], "kind": test.get("kind", ""), "steps": test["steps"],
           "expect": test["expect"], "status": "", "confidence": None, "note": "", "agent_steps": [],
           "screenshot": None}
    io.progress(f"    ▸ {test['title']}")
    for step in test["steps"]:
        try:
            r = agent.handle(client, step, ctx)
        except Exception as e:
            r = {"ok": False, "steps": [f"error: {e}"]}
        rec["agent_steps"] += [f"{step} → {s}" for s in r.get("steps", [])] or [f"{step} → (nothing done)"]
        if not r.get("ok"):
            rec["status"] = "error"
            why = io.blocked[-1] if io.blocked else (r.get("steps") or ["could not be done"])[-1]
            rec["note"] = f"Step could not be completed: '{step}' — {why}"
            break
    if not rec["status"]:
        rec["status"], rec["confidence"], rec["note"] = check(client, gem, app, test["expect"])
    if rec["status"] != "passed":
        rec["screenshot"] = screenshot(app, out_dir / f"test{number}.png")
    rec["duration_s"] = round(time.time() - t0, 1)
    mark = {"passed": "✓", "failed": "✗", "error": "✗"}[rec["status"]]
    conf = f" ({rec['confidence']:.2f})" if rec["confidence"] is not None else ""
    io.progress(f"     {mark} {rec['status']}{conf}" + (f" — {rec['note']}" if rec["status"] != "passed" else ""))
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
    failed = len(r["tests"]) - passed
    sev_order = {"high": 0, "medium": 1, "low": 2}
    issues = sorted(r["issues"], key=lambda i: sev_order.get(i["severity"], 3))
    issue_rows = "".join(
        f'<tr><td><span class="sev {e(i["severity"])}">{e(i["severity"])}</span></td><td>{e(i["what"])}'
        f'{"<div class=muted>" + e(i["detail"]) + "</div>" if i["detail"] else ""}</td>'
        f'<td class="url">{e(i["page"])}</td>'
        f'<td>{"<a href=" + chr(34) + e(i["screenshot"]) + chr(34) + ">image</a>" if i.get("screenshot") else ""}</td></tr>'
        for i in issues) or '<tr><td colspan="4" class="muted">No issues found.</td></tr>'
    tests = "".join(
        f'<details class="test {e(t["status"])}"><summary><span class="dot"></span><b>{e(t["title"])}</b>'
        f'<span class="muted"> · {e(t["kind"])} · {t.get("duration_s", 0)} s</span><span class="st">{e(t["status"])}</span></summary>'
        f'<div class="body"><div class="muted url">{e(t["page"])}</div>'
        f'<h4>Steps</h4><ul>{"".join("<li>" + e(s) + "</li>" for s in t["steps"])}</ul>'
        f'<h4>Expected</h4><p>{e(t["expect"])}</p>'
        f'{"<h4>Result</h4><p>" + e(t["note"]) + "</p>" if t["note"] else ""}'
        f'{"<p class=muted>Confidence " + format(t["confidence"], ".2f") + "</p>" if t["confidence"] is not None else ""}'
        f'<h4>What the agent did</h4><ul class="mono">{"".join("<li>" + e(s) + "</li>" for s in t["agent_steps"])}</ul>'
        f'{"<img src=" + chr(34) + e(t["screenshot"]) + chr(34) + ">" if t.get("screenshot") else ""}</div></details>'
        for t in r["tests"]) or '<p class="muted">No tests were run.</p>'
    pages = "".join(
        f'<tr><td>{e(p["title"] or p["url"])}<div class="muted url">{e(p["url"])}</div></td><td>{e(p["purpose"])}</td>'
        f'<td>{e(p["status"])}</td><td>{p["load_ms"] / 1000:.1f} s</td><td>{p["tests"]}</td></tr>' for p in r["pages"])
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
    ap.add_argument("site", help="address or name of the site to test")
    ap.add_argument("--pages", type=int, default=6, help="pages to explore (default 6)")
    ap.add_argument("--minutes", type=float, default=15, help="time budget (default 15)")
    ap.add_argument("--tests", type=int, default=3, help="tests per page (default 3)")
    ap.add_argument("--no-submit", action="store_true", help="never submit forms, only test validation")
    ap.add_argument("--yes", action="store_true", help="do not ask before starting (CI)")
    ap.add_argument("--out", help="report directory (default ~/.agentp/qa/<time>-<site>)")
    args = ap.parse_args()
    agent.check_env()
    if not agent.AXIsProcessTrusted():
        sys.exit(agent.PERM_MSG)
    allow = not args.no_submit
    if allow and not args.yes:
        a = input(f"AgentP will click through {args.site}, fill forms with made-up test data and submit them.\n"
                  "It never pays, buys, deletes, logs out or contacts anyone. Allow form submission? (Y/n) ")
        allow = a.strip().casefold() in ("", "y", "yes")
    from typesafe_sdk import TypeSafeClient
    r = run_session(args.site, TypeSafeClient(), agent.CliIO(), args.pages, args.minutes, args.tests, allow,
                    Path(args.out) if args.out else None)
    if r.get("report"):
        subprocess.run(["open", r["report"]["html"]], check=False)
    bad = [t for t in r.get("tests", []) if t["status"] != "passed"]
    sys.exit(1 if (bad or any(i["severity"] == "high" for i in r.get("issues", []))) else 0)


if __name__ == "__main__":
    main()
