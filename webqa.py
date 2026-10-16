#!/usr/bin/env python3
"""
webqa.py — the web testing engine: exploratory tests run inside the browser itself (Playwright).

Same idea as qa.py (Gemini plans the tests, Jev picks the elements, a check decides pass / fail), but the
page is read and driven through the browser instead of the screen:

  read      one call returns every control on the page: links, buttons, tabs, option cards, fields with
            their kind, required flag, current value and select options, plus headings and visible text
  act       real browser events on the element itself (click, fill, select, check): works on custom
            React / Vue controls, never lands on another app, needs no window focus
  settle    waits until the page stops changing (DOM mutations and network requests), not fixed sleeps
  check     compares the page before and after the test (address, text that appeared / disappeared,
            controls whose state changed, requests sent): nothing changed → fail without asking a model;
            otherwise Jev decides, Gemini only when Jev is unsure
  issues    HTTP errors, failed requests and JavaScript errors of the site, slow loads, broken / malformed
            links, controls that did nothing

Each test starts clean (cookies and storage cleared) in the same window, then replays the tests that led
to its state (step 2 of a form). Safety is the same as qa.py: stays on the site, made-up data, never pays,
buys, deletes, logs out or contacts anyone; without allow_submit no form data is sent.
"""
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import agent
import qa

PASS, FAIL = qa.PASS, qa.FAIL
SETTLE_S = 3.0          # longest wait for the page to stop changing after an action
QUIET_S = 0.35          # no DOM change and no request for this long = settled
MAX_ELEMENTS = 500
FIELD_KINDS = {"text", "email", "phone", "password", "number", "date", "search", "url", "textarea", "select",
               "dropdown", "checkbox", "radio"}
TEXTISH = FIELD_KINDS - {"select", "dropdown", "checkbox", "radio"}
TRACKING = re.compile(r"/v1/t\b|/events/|/jserrors/|google-analytics|googletagmanager|doubleclick|facebook|hotjar|clarity\.ms|criteo|adservice|"
                      r"/collect\b|analytics|/track|beacon|pixel|/metrics|/rum\b|/log\b|newrelic|nr-data|segment\.io|"
                      r"tiktok|linkedin|yandex|adobedtm|demdex|omtrdc|optimizely|insider|useinsider", re.I)
CONSOLE_NOISE = re.compile(r"Failed to load resource|Permissions-Policy|sandboxed|Content Security Policy|"
                           r"third-party cookie|preloaded using link preload|deprecated|net::ERR_", re.I)
CLICK_RANK = {"checkbox": 5, "radio": 5, "button": 4, "tab": 4, "option": 4, "menuitem": 4, "dropdown": 4,
              "select": 4, "link": 3, "clickable": 3, "treeitem": 3}

WATCH_JS = """
window.__agentpMut = Date.now();
new MutationObserver(() => { window.__agentpMut = Date.now(); })
  .observe(document, {subtree: true, childList: true, characterData: true});
"""

# Every control on the page, with what a tester needs to know about it. Each gets a data-agentp id
# so it can be acted on directly.
SNAP_JS = r"""
(maxElements) => {
  document.querySelectorAll('[data-agentp]').forEach(e => e.removeAttribute('data-agentp'));
  document.querySelectorAll('[data-agentp-via]').forEach(e => e.removeAttribute('data-agentp-via'));
  const clean = s => (s || '').replace(/[-​-‏‪-‮]/g, '').replace(/\s+/g, ' ').trim();
  const shown = e => {
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity) > 0.05;
  };
  const isField = e => ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.tagName);
  const labelOf = e => {
    let t = e.getAttribute('aria-label') || '';
    const lb = e.getAttribute('aria-labelledby');
    if (!clean(t) && lb) t = lb.split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ');
    if (!clean(t) && isField(e)) {
      if (e.labels && e.labels.length) t = e.labels[0].innerText;
      if (!clean(t)) t = e.placeholder || e.getAttribute('title') || '';
      if (!clean(t) && ['submit', 'button', 'reset'].includes(e.type)) t = e.value;
      if (!clean(t)) {   // the text next to the field
        const p = e.closest('div,li,td,p,fieldset');
        const txt = p ? clean(p.innerText) : '';
        if (txt && txt.length < 80) t = txt;
      }
      if (!clean(t)) t = e.name || e.id || '';
    }
    if (!clean(t) && !isField(e)) t = e.innerText;
    if (!clean(t)) {
      const img = e.querySelector && e.querySelector('img[alt]');
      const svg = e.querySelector && e.querySelector('svg title');
      t = e.getAttribute('title') || (img && img.alt) || (svg && svg.textContent) || '';
    }
    return clean(t).slice(0, 120);
  };
  const ROLES = {checkbox: 'checkbox', switch: 'checkbox', menuitemcheckbox: 'checkbox', radio: 'radio',
                 menuitemradio: 'radio', combobox: 'dropdown', listbox: 'dropdown', tab: 'tab', option: 'option',
                 menuitem: 'menuitem', link: 'link', button: 'button', treeitem: 'treeitem', textbox: 'text',
                 searchbox: 'search', spinbutton: 'number', slider: 'clickable'};
  const kindOf = e => {
    const tag = e.tagName, role = (e.getAttribute('role') || '').toLowerCase(), type = (e.type || '').toLowerCase();
    if (tag === 'SELECT') return 'select';
    if (tag === 'TEXTAREA') return 'textarea';
    if (tag === 'INPUT') {
      if (type === 'hidden') return null;
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
      return {email: 'email', tel: 'phone', password: 'password', number: 'number', date: 'date',
              search: 'search', url: 'url'}[type] || 'text';
    }
    if (e.isContentEditable) return 'textarea';
    if (role) return ROLES[role] || null;
    if (tag === 'A') return 'link';
    if (tag === 'BUTTON' || tag === 'SUMMARY') return 'button';
    return 'clickable';
  };
  const near = e => {   // the heading of the section the element is in
    let p = e.parentElement;
    for (let i = 0; p && i < 8; i++, p = p.parentElement) {
      const h = p.querySelector('h1,h2,h3,h4,h5,legend,[role=heading]');
      if (h && h !== e && !e.contains(h)) return clean(h.innerText).slice(0, 60);
    }
    return '';
  };
  const cands = new Set(document.querySelectorAll(
    'a[href],button,input,select,textarea,summary,[role],[onclick],[tabindex]:not([tabindex="-1"]),[contenteditable="true"]'));
  // option cards and other clickable boxes: the outermost element with a pointer cursor
  for (const e of document.body.querySelectorAll('div,li,span,img,svg,section,article,td,p,label,h2,h3,h4,h5')) {
    if (cands.has(e)) continue;
    const c = getComputedStyle(e).cursor;
    if (c === 'pointer' && !(e.parentElement && getComputedStyle(e.parentElement).cursor === 'pointer')) cands.add(e);
  }
  const out = [];
  let n = 0;
  for (const e of cands) {
    if (out.length >= maxElements) break;
    const kind = kindOf(e);
    if (!kind) continue;
    const ariaHidden = e.closest('[aria-hidden="true"]');
    if (ariaHidden && !isField(e)) continue;
    let via = null;
    if (ariaHidden || !shown(e)) {   // a styled checkbox / radio / dropdown hides the input and shows its label
      const l = e.labels && e.labels[0];
      if (!(['checkbox', 'radio', 'select', 'text'].includes(kind) && l && shown(l))) continue;
      via = l;
    }
    if (kind === 'clickable' && e.tagName === 'LABEL' && e.control) continue;   // its input is listed
    const label = labelOf(e);
    if (!label && !isField(e)) continue;
    const r = (via || e).getBoundingClientRect();
    const cls = typeof e.className === 'string' ? e.className : '';
    const it = {id: n, kind, label, near: near(e), tag: e.tagName.toLowerCase(),
                inView: r.bottom > 0 && r.top < innerHeight,
                disabled: !!(e.disabled || e.getAttribute('aria-disabled') === 'true')};
    if (isField(e) || e.isContentEditable) {
      it.required = !!(e.required || e.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label));
      if (e.tagName === 'SELECT') {
        it.options = [...e.options].map(o => clean(o.text)).filter(Boolean).slice(0, 40);
        it.value = e.selectedIndex >= 0 ? clean(e.options[e.selectedIndex].text) : '';
      } else if (kind !== 'checkbox' && kind !== 'radio') {
        it.value = (e.value ?? e.innerText ?? '').toString().slice(0, 80);
      }
      if (e.maxLength > 0) it.maxlength = e.maxLength;
      if (e.pattern) it.pattern = e.pattern;
    }
    const sel = e.checked === true || e.getAttribute('aria-checked') === 'true' ||
                e.getAttribute('aria-selected') === 'true' || e.getAttribute('aria-pressed') === 'true' ||
                (e.getAttribute('aria-current') || 'false') !== 'false' ||
                /(^|[\s_-])(active|selected|checked|is-active|is-selected|current)($|[\s_-])/i.test(cls);
    it.selected = sel;
    const exp = e.getAttribute('aria-expanded');
    if (exp !== null) it.expanded = exp === 'true';
    if (e.tagName === 'A') it.href = e.href;
    e.setAttribute('data-agentp', n);
    if (via) via.setAttribute('data-agentp-via', n);
    out.push(it);
    n++;
  }
  const text = [];
  const seen = new Set();
  for (const line of (document.body.innerText || '').split('\n')) {
    const t = clean(line).slice(0, 200);
    if (t && !seen.has(t)) { seen.add(t); text.push(t); }
    if (text.length >= 400) break;
  }
  const headings = [...document.querySelectorAll('h1,h2,h3,[role=heading]')].filter(shown)
    .map(h => clean(h.innerText).slice(0, 100)).filter(Boolean).slice(0, 30);
  return {url: location.href, title: document.title, elements: out, text, headings};
}
"""


# ---------------------------------------------------------------- the browser
class Web:
    """One Chromium window for the whole session; the user's own browser is never touched."""

    def __init__(self, pw, scope: str, allow_submit: bool):
        self.scope, self.allow_submit = scope, allow_submit
        self.browser = pw.chromium.launch(headless=False, args=["--window-size=1400,950", "--window-position=60,40"])
        self.context = self.browser.new_context(no_viewport=True, locale=system_locale())
        self.context.add_init_script(WATCH_JS)
        self.new_pages: list = []
        self.context.on("page", self._popup)
        self.guard = False      # True while a test's steps run
        self.phones: set[str] = set()      # phone numbers the tests typed (digits)
        self.held: list[str] = []          # requests not sent because they carried one
        self.context.route("**/*", self._route)
        self.page = self.context.new_page()
        self.pending: set = set()          # the page's own requests still running (not trackers)
        self.sent: list[str] = []          # requests the page sent since mark()
        self.errors: list[dict] = []       # the site's failed requests and JavaScript errors since mark()
        self.popups: list[str] = []
        p = self.page
        p.on("request", self._request)
        p.on("requestfinished", self._done)
        p.on("requestfailed", self._failed)
        p.on("response", self._response)
        p.on("console", self._console)
        p.on("pageerror", lambda e: self.errors.append({"kind": "javascript", "text": str(e)[:200]}))

    # --- events
    def _popup(self, page):
        if getattr(self, "page", None) is not None and page is not self.page:
            self.new_pages.append(page)   # closed after the action (no browser calls inside an event)

    def close_popups(self):
        """A link opened a new tab: note where it went and close it; the test stays in its window."""
        while self.new_pages:
            p = self.new_pages.pop()
            try:
                p.wait_for_load_state("domcontentloaded", timeout=5000)
                self.popups.append(p.url)
                p.close()
            except Exception:
                pass

    def _request(self, r):
        if r.resource_type in ("xhr", "fetch", "document") and not TRACKING.search(r.url):
            self.pending.add(r)
            if r.is_navigation_request() and r.frame == self.page.main_frame:
                self.sent.append(f"opened {urllib.parse.urlsplit(r.url).path[:80]}")

    def _done(self, r):
        self.pending.discard(r)

    def _failed(self, r):
        self._done(r)
        if qa.domain(r.url).endswith(self.scope) and r.resource_type in ("document", "xhr", "fetch", "script") \
                and "ERR_ABORTED" not in (r.failure or "") and "net::ERR_FAILED" not in (r.failure or ""):
            self.errors.append({"kind": "request", "status": r.failure, "url": r.url, "method": r.method})

    def _response(self, r):
        req = r.request
        if r.status >= 400 and qa.domain(r.url).endswith(self.scope) \
                and req.resource_type in ("document", "xhr", "fetch", "script"):
            self.errors.append({"kind": "request", "status": r.status, "url": r.url, "method": req.method})
        if req.resource_type in ("xhr", "fetch") and not TRACKING.search(r.url):
            self.sent.append(f"{req.method} {urllib.parse.urlsplit(r.url).path[:80]} → {r.status}")

    def _route(self, route):
        """Validation only (no allow_submit): no form data leaves the browser during a test. Always: a
        request carrying a phone number a test typed is not sent: it would text or call whoever owns that
        number (there is no reserved fake range for phone numbers)."""
        req = route.request
        if self.guard and req.method in ("POST", "PUT", "PATCH", "DELETE"):
            if not self.allow_submit:
                return route.abort()
            if self.phones and qa.domain(req.url).endswith(self.scope) and not TRACKING.search(req.url):
                try:
                    body = re.sub(r"\D", "", (req.post_data or "") + urllib.parse.unquote(req.url))
                except Exception:
                    body = ""
                if any(p in body for p in self.phones):
                    self.held.append(f"{req.method} {urllib.parse.urlsplit(req.url).path[:60]}")
                    return route.abort()
        return route.continue_()

    def _console(self, m):
        if m.type == "error" and not CONSOLE_NOISE.search(m.text):
            self.errors.append({"kind": "console", "text": m.text[:200]})

    def mark(self):
        self.sent, self.errors, self.popups, self.held = [], [], [], []
        self.phones = set()
        self.pending.clear()

    # --- navigation
    def goto(self, url: str) -> tuple[float, int | str]:
        from playwright.sync_api import Error as BrowserError
        t0 = time.time()
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status = resp.status if resp else "no response"
        except BrowserError as e:   # the site did not load (timeout, DNS, connection refused)
            return (time.time() - t0) * 1000, str(e).split("\n")[0][:80]
        try:
            self.page.wait_for_load_state("load", timeout=8000)
        except Exception:
            pass
        ms = (time.time() - t0) * 1000
        self.settle(2.0)
        self.rendered()
        if self.dismiss_consent():
            self.rendered()
        return ms, status

    def rendered(self, timeout: float = 6.0):
        """A page built by JavaScript keeps adding controls after it has loaded: wait until their number
        stops growing (a carousel that never stops animating does not hold this up)."""
        count, same, t0 = -1, 0, time.time()
        while time.time() - t0 < timeout and (same < 4 or self.pending):
            try:
                n = self.page.evaluate("() => document.querySelectorAll('a,button,input,select,textarea,[role]').length")
            except Exception:
                n = -1
            same = same + 1 if n == count else 0
            count = n
            self.page.wait_for_timeout(250)

    def reset(self, url: str) -> float:
        """A fresh visitor: no cookies or storage left from the previous test (the site would remember
        the step a form was on), then the page."""
        try:
            self.page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
        except Exception:
            pass
        self.context.clear_cookies()
        return self.goto(url)[0]

    def url(self) -> str:
        return self.page.url

    def settle(self, timeout: float = SETTLE_S):
        """Wait until nothing on the page has changed for a moment and no request is pending."""
        self.close_popups()
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                quiet = self.page.evaluate("() => Date.now() - (window.__agentpMut || 0)") / 1000
            except Exception:   # navigating
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                continue
            if quiet >= QUIET_S and not self.pending:
                return
            self.page.wait_for_timeout(100)

    def dismiss_consent(self) -> str | None:
        snap = self.snapshot()
        if not any(qa.CONSENT_TEXT.search(t) for t in snap["text"][:400]):
            return None
        buttons = [e for e in snap["elements"] if e["kind"] in ("button", "link", "clickable") and len(e["label"]) < 40]
        for pattern in qa.CONSENT_PREFER:
            for b in buttons:
                if pattern.search(b["label"]):
                    if self.click(b):
                        self.settle(1.5)
                        return b["label"]
        return None

    # --- reading
    def snapshot(self) -> dict:
        for _ in range(3):
            try:
                s = self.page.evaluate(SNAP_JS, MAX_ELEMENTS)
                break
            except Exception:   # the page navigated while it was being read
                self.settle(2.0)
        else:
            s = {"url": self.page.url, "title": "", "elements": [], "text": [], "headings": []}
        els = s["elements"]
        options = {o for e in els for o in e.get("options") or []}
        s["text"] = [t for t in s["text"] if t not in options]   # a dropdown's options are not page text
        s["links"] = [{"label": e["label"][:80], "url": e["href"]} for e in els if e["kind"] == "link" and e.get("href")]
        s["fields"] = [e for e in els if e["kind"] in FIELD_KINDS - {"radio"} or
                       (e["kind"] == "radio" and e["tag"] == "input")]
        return s

    def screenshot(self, path: Path) -> str | None:
        try:
            self.page.screenshot(path=str(path))
            return path.name
        except Exception:
            return None

    # --- acting (each returns whether the action could be done)
    def loc(self, el: dict):
        via = self.page.locator(f'[data-agentp-via="{el["id"]}"]')
        own = self.page.locator(f'[data-agentp="{el["id"]}"]')
        return own, via

    def refind(self, el: dict) -> dict | None:
        """The page re-rendered (React / Vue) and the element lost its mark: the same control again."""
        self.settle(1.5)
        same = [e for e in self.snapshot()["elements"] if e["kind"] == el["kind"] and e["label"] == el["label"]]
        return next((e for e in same if e.get("near", "") == el.get("near", "")), same[0] if same else None)

    def click(self, el: dict, again: bool = True) -> bool:
        if self._click(el):
            return True
        el2 = self.refind(el) if again else None
        return el2 is not None and self._click(el2)

    def _click(self, el: dict) -> bool:
        own, via = self.loc(el)
        for target, force in ((own, False), (via, False), (own, True)):
            try:
                if target.count() == 0:
                    continue
                target.first.click(timeout=2500, force=force)
                return True
            except Exception:
                continue
        try:   # covered by something that does not go away: the element's own click handler
            own.first.dispatch_event("click", timeout=1500)
            return True
        except Exception:
            return False

    def fill(self, el: dict, value: str) -> bool:
        if self.loc(el)[0].count() == 0:
            el = self.refind(el) or el
        own, _ = self.loc(el)
        m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$", value.strip())
        if el["kind"] == "date" and m:   # a date input takes yyyy-mm-dd
            value = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 10 and (el["kind"] == "phone" or re.fullmatch(r"[\d\s()+-]+", value.strip())):
            self.phones.add(digits[-10:])
        try:
            if len(value) > 1 and el["kind"] not in ("date", "password"):
                # the last character as a real key press: suggestion lists and live search listen to keys
                own.first.fill(value[:-1], timeout=2500)
                own.first.press_sequentially(value[-1], delay=20, timeout=3000)
            else:
                own.first.fill(value, timeout=2500)
            got = own.first.input_value(timeout=1000) if el["tag"] in ("input", "textarea") else value
            if norm_text(got) != norm_text(value):   # an input mask rewrote it: type like a person
                own.first.fill("", timeout=1500)
                own.first.press_sequentially(value, delay=25, timeout=8000)
            own.first.evaluate("e => e.blur()")   # validation often runs when the field is left
            return True
        except Exception:
            return False

    def select(self, el: dict, value: str) -> str | None:
        """A native <select>: the option whose text matches (any real option when value is empty);
        returns the option chosen."""
        own, _ = self.loc(el)
        opts = el.get("options") or []
        if value.strip():
            best = best_label(value, [{"label": o} for o in opts])
            loose = [o for o in opts if label_score(value, o) >= 1]
            pick = best["label"] if best else loose[0] if len(loose) == 1 else None
        else:
            pick = next((o for o in opts if not PLACEHOLDER.match(o)), None)
        if pick is None:
            return None
        try:
            own.first.select_option(label=pick, timeout=2500, force=True)
            return pick
        except Exception:
            return None

    def set_checked(self, el: dict, on: bool) -> bool:
        own, via = self.loc(el)
        if bool(el.get("selected")) == on:
            return True
        if el["tag"] == "input":
            try:
                own.first.set_checked(on, timeout=2500, force=True)
                return True
            except Exception:
                pass
        return self.click(el)

    def close(self):
        try:
            self.browser.close()
        except Exception:
            pass


def system_locale() -> str:
    try:
        from AppKit import NSLocale
        return str(NSLocale.currentLocale().localeIdentifier()).replace("_", "-").split("@")[0]
    except Exception:
        return "en-US"


# ---------------------------------------------------------------- matching labels
def norm_text(s: str) -> str:
    return " ".join(qa.fold(s or ""))


def label_score(want: str, have: str) -> int:
    a, b = norm_text(want), norm_text(have)
    if not a or not b:
        return 0
    return 3 if a == b else 2 if b.startswith(a) or a.startswith(b) else 1 if a in b else 0


def best_label(want: str, items: list[dict]) -> dict | None:
    """The item whose label is the wanted text (exact, then starts with it)."""
    scored = [(label_score(want, i["label"]), -len(i["label"]), n) for n, i in enumerate(items)]
    top = max(scored, default=None)
    return items[top[2]] if top and top[0] >= 2 else None


def desc(e: dict) -> str:
    s = f"{e['kind']}: {e['label'] or '(no label)'}"
    if e.get("near") and norm_text(e["near"]) != norm_text(e["label"]):
        s += f"  — in '{e['near']}'"
    if e.get("selected"):
        s += "  [selected]"
    if e.get("value"):
        s += f"  [value: {e['value'][:30]}]"
    if e.get("disabled"):
        s += "  [disabled]"
    return s


def overlap(text: str, e: dict) -> int:
    want = {w for w in qa.fold(text) if len(w) > 2}
    have = set(qa.fold(e["label"] + " " + e.get("near", "")))
    return sum(1 for w in want if any(h.startswith(w[:5]) for h in have))


def jev_pick(client, step: str, cands: list[dict], snap: dict, what: str = "element") -> tuple[dict | None, float]:
    """Jev chooses the element a step means, among the page's controls."""
    from typesafe_sdk import Choice
    if not cands:
        return None, 0.0
    opts = {f"e{n}": desc(e) for n, e in enumerate(cands)}
    opts["none"] = "None of these"
    a = agent.jev(client, {"test_step": step, "page_title": snap["title"], "page_url": snap["url"],
                           "page_headings": snap["headings"][:10]},
                  {"el": Choice(instructions=f"Which {what} on the page must be used to do this test step? "
                                             f"Step: {step}", criteria=opts)}, {}, "pick")
    ch = a["el"]
    if ch.choice == "none" or ch.choice not in opts:
        return None, ch.confidence or 0.0
    return cands[int(ch.choice[1:])], ch.confidence or 0.0


def shortlist(text: str, elements: list[dict], kinds: set | None = None, n: int = 40) -> list[dict]:
    pool = [e for e in elements if (kinds is None or e["kind"] in kinds) and not e.get("disabled")]
    pool.sort(key=lambda e: (-overlap(text, e), not e.get("inView"), -CLICK_RANK.get(e["kind"], 0)))
    return pool[:n]


# ---------------------------------------------------------------- doing a test
def find_target(client, web: Web, step_label: str, step: str, snap: dict) -> tuple[dict | None, str]:
    """The control a click step names: its exact label (the control beats its caption text), several
    equal matches or none → Jev decides with the page context."""
    els = snap["elements"]
    for label in qa.label_candidates(step_label):
        scored = [(label_score(label, e["label"]), e) for e in els]
        best = max((s for s, _ in scored), default=0)
        if best < 2:
            continue
        tied = [e for s, e in scored if s == best]
        top_rank = max(CLICK_RANK.get(e["kind"], 0) for e in tied)
        tied = [e for e in tied if CLICK_RANK.get(e["kind"], 0) == top_rank]
        if len(tied) == 1:
            return tied[0], "label"
        visible = [e for e in tied if e.get("inView")]
        if len(visible) == 1:
            return visible[0], "label"
        e, conf = jev_pick(client, step, tied[:20], snap)   # e.g. two 'Next' buttons: which one this step means
        if e is not None:
            return e, f"Jev {conf:.2f}"
    e, conf = jev_pick(client, step, shortlist(step, els), snap)
    return (e, f"Jev {conf:.2f}") if e is not None and conf >= 0.3 else (None, "")


def find_field(client, name: str, value: str, snap: dict) -> tuple[dict | None, str]:
    fields = snap["fields"]
    for label in qa.label_candidates(name):
        e = best_label(label, fields)
        if e:
            return e, "label"
    # a field named by its place in a group ('Cinsiyet' → radio 'Kadın') or its placeholder
    near = [f for f in snap["elements"] if f["kind"] in FIELD_KINDS and label_score(name, f.get("near", "")) >= 2]
    if near and value:
        e = best_label(value, near)
        if e:
            return e, "group"
    e, conf = jev_pick(client, f"enter '{value}' into the '{name}' field", shortlist(name, fields, n=40) or fields,
                       snap, "form field")
    return (e, f"Jev {conf:.2f}") if e is not None and conf >= 0.3 else (None, "")


def blocked(e: dict, scope: str) -> str | None:
    if qa.HARD_STOP.search(e["label"]):
        return f"'{e['label'][:40]}' (never pressed during tests)"
    href = e.get("href") or ""
    if href.startswith("http") and not qa.domain(href).endswith(scope):
        return f"'{e['label'][:40]}' leads off the site ({qa.domain(href)})"
    if re.search(r"logout|signout|sign-out|log-out|unsubscribe", href, re.I):
        return f"'{e['label'][:40]}' logs out / unsubscribes"
    return None


def changed(web: Web, before: dict, sent0: int) -> bool:
    after = web.snapshot()
    return (after["url"] != before["url"] or after["text"] != before["text"] or len(web.sent) > sent0
            or state_of(after) != state_of(before))


def state_of(snap: dict) -> dict:
    """Each control's state, keyed by what it is (six slides all called 'Ford Capri 2026' stay apart)."""
    out, seen = {}, {}
    for e in snap["elements"]:
        k = (e["kind"], e["label"], e.get("near", ""))
        seen[k] = seen.get(k, 0) + 1
        out[k + (seen[k],)] = (e.get("selected"), e.get("expanded"), e.get("value"))
    return out


def do_fill(client, web: Web, entry: dict, account) -> tuple[str, dict | None]:
    """One {field, value} of a form test → what was done, and the action to replay it."""
    name, value = entry.get("field", ""), qa.secret(str(entry.get("value", "")), account)
    snap = web.snapshot()
    e, how = find_field(client, name, value, snap)
    shown = qa.masked(value, account)
    if e is None:
        # an option card or a radio named by its value ('Benzinli', 'Otomatik')
        opt = best_label(value, [x for x in snap["elements"] if x["kind"] in ("radio", "option", "clickable", "button", "tab")])
        if opt is None or not value:
            raise RuntimeError(f"no field '{name}' on the page")
        e = opt
    act = {"do": "fill", "field": name, "value": entry.get("value", ""), "kind": e["kind"], "label": e["label"],
           "near": e.get("near", "")}
    ok, note = run_action(client, web, e, value, snap)
    if not ok:
        raise RuntimeError(f"could not use '{e['label'][:40]}': {note}")
    return f"{name or e['label'][:40]} = '{shown}' → {note}", act


PLACEHOLDER = re.compile(r"^(|-+|seçiniz|seçin|lütfen seçiniz|select|choose|please select|select an option|"
                         r"--.*--|\.\.\.)$", re.I)


def complete_required(client, web: Web, skip: set | None = None) -> list[tuple[str, dict]]:
    """Required dropdowns and checkboxes still empty: the first real option / checked. Repeated, because
    one choice loads the next list (il → ilçe → bayi). Text fields are the plan's job (they need data)."""
    done, tried = [], set()
    for _ in range(6):
        snap = web.snapshot()
        todo = [f for f in snap["fields"] if f.get("required") and not f.get("disabled")
                and (f["kind"] in ("select", "dropdown") and PLACEHOLDER.match((f.get("value") or "").strip())
                     or f["kind"] == "checkbox" and not f.get("selected"))
                and f["label"] not in (skip or ())
                and (f["kind"], f["label"], len(f.get("options") or [])) not in tried]
        if not todo:
            break
        f = todo[0]
        tried.add((f["kind"], f["label"], len(f.get("options") or [])))   # tried again once its list has loaded
        if f["kind"] == "checkbox":
            ok, note = web.set_checked(f, True), "checked"
        elif f["kind"] == "select":
            got = web.select(f, "")
            ok, note = got is not None, f"'{got}'"
        else:
            ok, note = run_action(client, web, f, "", snap)
        web.settle()
        if ok:
            done.append((f"{f['label'][:40]} = {note}",
                         {"do": "fill", "field": f["label"], "value": "" if f["kind"] != "checkbox" else "check",
                          "kind": f["kind"], "label": f["label"], "near": f.get("near", "")}))
    return done


def run_action(client, web: Web, e: dict, value: str, snap: dict) -> tuple[bool, str]:
    kind, v = e["kind"], value.strip()
    if kind in ("select", "dropdown") and not v:   # left empty on purpose (a required-field test)
        return True, "left empty"
    if kind == "select":
        got = web.select(e, value)
        return got is not None, f"selected '{got}'" if got else "option not found"
    if kind in ("checkbox", "radio"):
        on = v.casefold() not in ("uncheck", "false", "no", "off", "0", "hayır")
        if kind == "radio" and v and v.casefold() not in ("check", "select", "true", "yes", "on") \
                and label_score(v, e["label"]) < 2:
            group = [x for x in snap["elements"] if x["kind"] == "radio" and x.get("near") == e.get("near")]
            e = best_label(v, group) or e
        ok = web.set_checked(e, on)
        return ok, ("checked" if on else "unchecked") + f" '{e['label'][:40]}'"
    if kind == "dropdown":   # a custom dropdown: open it, then the option
        if not web.click(e):
            return False, "could not open it"
        web.settle(1.5)
        opts = [x for x in web.snapshot()["elements"] if x["kind"] in ("option", "menuitem", "clickable", "button", "radio")]
        o = best_label(value, opts) if value else next((x for x in opts if x["kind"] == "option"
                                                        and not PLACEHOLDER.match(x["label"])), None)
        if o is None:
            web.page.keyboard.press("Escape")
            return False, f"no option '{value}'"
        return web.click(o), f"chose '{o['label'][:40]}'"
    if kind in FIELD_KINDS:
        return web.fill(e, value), "typed"
    return web.click(e), f"clicked '{e['label'][:40]}'"


def do_step(client, web: Web, step: str, scope: str, already: set | None = None) -> tuple[bool, str, dict | None]:
    """One planned step ('click the 'İLERİ' button', 'type 'x' into Email', or anything else) → done?, what
    happened, the action to replay it."""
    snap = web.snapshot()
    m = qa.STEP_TYPE.match(step)
    if m and already and (m.group("qfield") or m.group("field") or "").strip() in already:
        return True, "already filled by the form data", None
    if m:
        try:
            note, act = do_fill(client, web, {"field": m.group("qfield") or m.group("field"),
                                              "value": m.group("value")}, None)
            return True, note, act
        except RuntimeError as ex:
            return False, str(ex), None
    m = qa.STEP_CLICK.match(step)
    verb = step.split()[0].casefold() if step.split() else ""
    value = ""
    if m:
        label = m.group("label")
        for f in snap["fields"]:   # an option of a dropdown ('select 'Benzinli'')
            if f["kind"] == "select" and best_label(label, [{"label": o} for o in f.get("options") or []]):
                got = web.select(f, label)
                if got:
                    web.settle()
                    return True, f"selected '{got}' in '{f['label'][:40]}'", \
                        {"do": "step", "step": step, "kind": "select", "label": f["label"], "near": f.get("near", ""),
                         "value": got}
        e, how = find_target(client, web, label, step, snap)
    else:   # free wording: Jev picks the control; the step's quoted text is a value to enter
        e, conf = jev_pick(client, step, shortlist(step, snap["elements"]), snap)
        how = f"Jev {conf:.2f}"
        if conf < 0.3:
            e = None
        quoted = re.findall(r"[\"“'‘](.+?)[\"”'’]", step)
        value = quoted[-1] if quoted and e is not None and e["kind"] in FIELD_KINDS else ""
    if e is None:
        return False, "no such control on the page", None
    why = blocked(e, scope)
    if why:
        return False, f"blocked: {why}", None
    if e["kind"] in ("checkbox", "radio") and already and e["label"] in already:
        return True, f"'{e['label'][:40]}' was already set by the form data (not clicked again)", None
    sent0, before = len(web.sent), snap
    if e["kind"] in ("checkbox", "radio") and verb in ("check", "uncheck", "select", "choose"):
        ok, note = web.set_checked(e, verb != "uncheck"), f"{'unchecked' if verb == 'uncheck' else 'checked'} '{e['label'][:50]}'"
    elif value and e["kind"] in FIELD_KINDS:
        ok, note = run_action(client, web, e, value, snap)
    else:
        ok, note = web.click(e), f"clicked '{e['label'][:50]}'"
    if not ok:
        return False, f"could not use '{e['label'][:40]}' ({e['kind']})", None
    web.settle()
    if e["kind"] not in TEXTISH and not changed(web, before, sent0):
        note += " (nothing changed on the page)"
    if web.popups:
        note += f" (opened a new window: {web.popups[-1][:80]})"
    act = {"do": "step", "step": step, "kind": e["kind"], "label": e["label"], "near": e.get("near", ""),
           "value": value}
    return True, f"{note} [{e['kind']}, {how}]", act


def replay(client, web: Web, actions: list[dict], account) -> str | None:
    """The actions that led to a state (a passed test's steps), on a fresh page. None if all worked."""
    for a in actions:
        snap = web.snapshot()
        same = [e for e in snap["elements"] if e["kind"] == a["kind"] and e["label"] == a["label"]]
        e = next((x for x in same if x.get("near", "") == a.get("near", "")), same[0] if same else None)
        value = qa.secret(a.get("value", ""), account)
        if e is not None:
            ok, _ = run_action(client, web, e, value, snap)
        elif a["do"] == "fill":
            try:
                do_fill(client, web, {"field": a["field"], "value": a.get("value", "")}, account)
                ok = True
            except RuntimeError:
                ok = False
        else:
            ok = do_step(client, web, a["step"], web.scope)[0]
        if not ok:
            return f"could not redo '{a.get('label') or a.get('step')}'"
        web.settle()
    return None


# ---------------------------------------------------------------- checking a result
def evidence(before: dict, after: dict, web: Web) -> dict:
    """What the test changed on the page: the facts the verdict is based on."""
    b, a = set(before["text"]), set(after["text"])
    st0, st1 = state_of(before), state_of(after)
    controls, moved = [], False
    for key, now in st1.items():
        was = st0.get(key)
        if was is not None and was != now:
            kind, label, _, nth = key
            label = label if nth == 1 else f"{label} #{nth}"
            what = []
            if was[0] != now[0]:
                what.append("now selected" if now[0] else "no longer selected")
            if was[1] != now[1]:
                what.append("expanded" if now[1] else "collapsed")
            moved = moved or was[:2] != now[:2]
            if was[2] != now[2]:
                what.append(f"value '{(now[2] or '')[:40]}'")
            controls.append(f"{kind} '{label[:50]}': {', '.join(what)}")
    return {
        "address_before": before["url"], "address_now": after["url"],
        "title_now": after["title"], "headings_now": after["headings"][:15],
        "text_that_appeared": [t for t in after["text"] if t not in b][:40],
        "text_that_disappeared": [t for t in before["text"] if t not in a][:25],
        "controls_that_changed": controls[:30],
        "requests_sent": web.sent[-10:],
        "new_windows": web.popups[-3:],
        "page_text_now": after["text"][:80],
        "_selection_changed": moved,   # not just the values the test typed
    }


def nothing_happened(ev: dict) -> bool:
    """Beyond the values the test typed itself, the page did not react at all."""
    return (ev["address_before"] == ev["address_now"] and not ev["text_that_appeared"]
            and not ev["text_that_disappeared"] and not ev["requests_sent"] and not ev["new_windows"]
            and not ev["_selection_changed"])


def verdict(client, gem, expectation: str, ev: dict) -> tuple[str, float, str]:
    from typesafe_sdk import Noul
    if nothing_happened(ev) and not qa.NEGATIVE.search(expectation):
        return "failed", 0.0, "nothing on the page changed"
    ev = {k: v for k, v in ev.items() if not k.startswith("_")}
    state = {"functional_test_check": expectation, "what_the_test_changed": ev}
    a = agent.jev(client, state, {"ok": Noul(instructions="The page now shows this expected result of the test "
                                                           "(judge from what the test changed): " + expectation)},
                  {}, "check")
    p = a["ok"].noul or 0
    if p >= PASS:
        return "passed", p, ""
    if p <= FAIL or gem is None:
        return "failed", p, "the expected result is not on the page"
    try:
        ok, reason = gem.judge(expectation, ev)
        return ("passed" if ok else "failed"), p, f"second opinion: {reason}"
    except Exception:
        return "failed", p, "the expected result is not on the page"


# ---------------------------------------------------------------- one test
def run_test(client, gem, web: Web, io, page_url: str, test: dict, out_dir: Path, number: int,
             account: dict | None, prefix: list[dict], stopped) -> tuple[dict, dict | None]:
    t0 = time.time()
    rec = {"page": page_url, "title": test["title"], "kind": test.get("kind", ""),
           "steps": [qa.masked(qa.secret(s, account), account) for s in test["steps"]],
           "expect": test["expect"], "status": "", "confidence": None, "note": "", "agent_steps": [],
           "screenshot": None,
           "fill": [{"field": f.get("field"), "value": qa.masked(qa.secret(str(f.get("value", "")), account), account)}
                    for f in test.get("fill") or []]}
    if prefix:
        rec["after"] = " → ".join(t["title"] for t in prefix)
    io.progress(f"    ▸ {test['title']}")
    web.reset(page_url)
    for p in prefix:
        why = replay(client, web, p.get("_actions", []), account)
        if why:
            rec["status"], rec["note"] = "error", f"Could not reach the starting state: {why}"
            break
    before, after = web.snapshot(), None
    web.mark()
    web.guard = True
    actions: list[dict] = []
    filled: set[str] = set()
    if not rec["status"]:
        for entry in test.get("fill") or []:
            try:
                note, act = do_fill(client, web, entry, account)
                rec["agent_steps"].append(qa.masked(f"fill → {note}", account))
                actions.append(act)
                filled.update({act["label"], act["field"]})
            except RuntimeError as ex:
                rec["agent_steps"].append(f"fill → FAILED: {ex}")
                rec["status"], rec["note"] = "error", f"Could not fill the form: {ex}"
                break
    if not rec["status"] and (test.get("advances") or test.get("fill")):
        # one rule per test: the required fields the test is not about must be valid, or an invalid-email
        # test "passes" only because the city was empty
        for note, act in complete_required(client, web, filled):
            rec["agent_steps"].append(f"fill → {note} (required, left empty by the plan)")
            actions.append(act)
    for step in [] if rec["status"] else test["steps"]:
        if stopped():
            break
        ok, note, act = do_step(client, web, qa.secret(step, account), web.scope, filled)
        shown = qa.masked(step, account)
        rec["agent_steps"].append(qa.masked(f"{shown} → {note}", account))
        if act:
            actions.append(act)
        if not ok:
            if note.startswith("blocked"):
                rec["status"], rec["note"] = "blocked", f"Not run for safety: {note[9:]}"
            else:
                rec["status"], rec["note"] = "error", f"Step could not be done: '{shown}' — {note}"
            break
    if not rec["status"]:
        web.settle()
        after = web.snapshot()
        ev = evidence(before, after, web)
        if nothing_happened(ev):   # some pages react late: give them a few more seconds before calling it dead
            t_wait = time.time()
            while time.time() - t_wait < 5 and nothing_happened(ev):
                web.page.wait_for_timeout(500)
                web.settle(1.5)
                after = web.snapshot()
                ev = evidence(before, after, web)
        hard, soft = qa.split_expectation(test["expect"])
        rec["status"], rec["confidence"], rec["note"] = verdict(client, gem, hard, ev)
        if rec["status"] == "passed" and soft:   # the function works; is the user told why?
            st, _, _ = verdict(client, None, soft, ev)
            if st != "passed":
                rec["ux"] = f"No visible feedback: expected '{soft}'"
        rec["changed"] = {k: v for k, v in ev.items() if k in ("address_now", "text_that_appeared", "controls_that_changed",
                                                                  "requests_sent") and v}
        if rec["status"] == "failed" and qa.LOGIN_URL.search(after["url"]) and not account:
            rec["status"] = "blocked"
            rec["note"] = f"Needs a login: it went to {urllib.parse.urlsplit(after['url']).netloc} (set a test account)"
    web.guard = False
    if web.held:
        rec["status"] = "blocked"
        rec["note"] = ("Not sent: the form would send a code or message to the phone number typed "
                       f"({', '.join(web.held[:2])}). A real person may own that number.")
    if rec["status"] != "passed":
        rec["screenshot"] = web.screenshot(out_dir / f"test{number}.png")
    rec["site_errors"] = web.errors[:10]
    rec["duration_s"] = round(time.time() - t0, 1)
    test["_actions"] = actions
    mark = {"passed": "✓", "failed": "✗", "error": "✗", "blocked": "–"}[rec["status"]]
    conf = f" ({rec['confidence']:.2f})" if rec["confidence"] is not None else ""
    io.progress(f"     {mark} {rec['status']}{conf} · {rec['duration_s']:.1f} s"
                + (f" — {rec['note']}" if rec["status"] != "passed" else "")
                + (f" · {rec['ux']}" if rec.get("ux") else ""))
    return rec, after


def planner_page(snap: dict) -> dict:
    """The page as Gemini plans tests from it."""
    els = snap["elements"]
    form = []
    for f in snap["fields"][:40]:
        d = {"field": f["label"][:70], "kind": f["kind"]}
        if f.get("required"):
            d["required"] = True
        if f.get("options"):
            d["options"] = f["options"][:15]
        if f.get("near"):
            d["section"] = f["near"]
        if f.get("maxlength"):
            d["maxlength"] = f["maxlength"]
        form.append(d)
    controls = [f"{e['label']} [{e['kind']}" + (", selected" if e.get("selected") else "")
                + (", expanded" if e.get("expanded") else ", collapsed" if e.get("expanded") is False else "")
                + (f", in '{e['near']}'" if e.get("near") and e["near"] != e["label"] else "") + "]"
                for e in els if e["kind"] not in TEXTISH | {"select", "link"} and e["label"]]
    return {"url": snap["url"], "title": snap["title"], "headings": snap["headings"],
            "buttons": controls[:90], "form_fields": form,
            "links": [l["label"] for l in snap["links"]][:100], "texts": snap["text"][:60]}


# ---------------------------------------------------------------- a session
FOLLOW_UPS, MAX_DEPTH = qa.FOLLOW_UPS, qa.MAX_DEPTH


class Progress:
    def __init__(self, base):
        self.base = base

    def progress(self, text: str):
        self.base.say(text)


def run_session(target: str, client, base_io, pages: int = 6, minutes: float = 15, tests_per_page: int = 3,
                allow_submit: bool = True, out_dir: Path | None = None, account: dict | None = None,
                focus: str = "", page_hint: str = "", stop=None, gem=None) -> dict:
    from playwright.sync_api import sync_playwright
    account = account or qa.test_account()
    t_start = time.time()
    if gem is None:
        gem = agent.make_ctx(io=None, use_memory=False).planner
    if gem is not None:
        gem.grounding = False   # test data like "QA Tester" must not be looked up on the web
    url = qa.resolve_target(target, gem)
    if not url:
        base_io.say(f"✗ Could not find the site '{target}'. Give its address, e.g. test example.com")
        return {}
    if not qa.reachable(url):
        base_io.say(f"✗ {urllib.parse.urlsplit(url).hostname} does not exist. Check the address "
                    f"(dictation may have misheard it).")
        return {}
    stopped = stop or (lambda: False)
    io = Progress(base_io)
    scope = qa.domain(url)
    out_dir = out_dir or qa.QA_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{scope}"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"site": url, "scope": scope, "started": time.strftime("%Y-%m-%d %H:%M"), "browser": "Chromium (Playwright)",
              "allow_submit": allow_submit, "pages": [], "tests": [], "issues": []}
    seen_issues = set()

    def issue(sev, page, what, detail="", shot=None):
        key = (what, detail)
        if key not in seen_issues:
            seen_issues.add(key)
            result["issues"].append({"severity": sev, "page": page, "what": what, "detail": detail, "screenshot": shot})

    def site_errors(page, errors):
        for er in errors:
            if er["kind"] == "request":
                st = er["status"]
                sev = "high" if isinstance(st, int) and st >= 500 else "medium"
                if isinstance(st, int) and st in (401, 403):
                    continue
                issue(sev, page, f"Request failed ({st}): {er['method']} {urllib.parse.urlsplit(er['url']).path[:60]}",
                      er["url"])
            else:
                issue("low", page, "JavaScript error on the page", er["text"])

    what = f"{focus} on {scope}" if focus else (f"the {page_hint} page of {scope}" if page_hint else scope)
    io.progress(f"› Testing {what} in its own browser window" + (f" (up to {pages} pages, {minutes:.0f} min)" if pages > 1 else ""))
    deadline = t_start + minutes * 60
    checked_links: dict[str, tuple] = {}
    with sync_playwright() as pw:
        web = Web(pw, scope, allow_submit)
        try:
            for hop in [h for h in re.split(r"\s*>\s*", page_hint) if h] if page_hint else []:
                web.goto(url)
                found = qa.pick_link(client, None, hop, web.snapshot())
                if not found:
                    io.progress(f"  (no link to '{hop}' on {web.url()}; testing from here)")
                    break
                st = qa.http_status(found)
                if not isinstance(st, int) or st >= 400:
                    io.progress(f"  ✗ '{hop}' leads to {found} which returns {st}; testing from {url} instead")
                    issue("medium", url, f"Page '{hop}' not found ({st})", found)
                    break
                io.progress(f"  → {hop}: {found}")
                url = found

            queue, visited, tested = [url], [], []
            link_pool = ThreadPoolExecutor(8)

            def run_tests(page_url, tests, prefix, depth):
                for test in tests:
                    if time.time() > deadline or stopped():
                        return
                    web.mark()
                    rec, after = run_test(client, gem, web, io, page_url, test, out_dir, len(result["tests"]) + 1,
                                          account, prefix, stopped)
                    result["tests"].append(rec)
                    site_errors(page_url, rec["site_errors"])
                    tested.append(" → ".join([t["title"] for t in prefix] + [test["title"]]))
                    if rec.get("ux"):
                        issue("low", page_url, "No feedback for invalid input", f"{test['title']}: {rec['ux']}")
                    if rec["status"] == "failed" and not qa.NEGATIVE.search(test["expect"]):
                        for s in rec["agent_steps"]:
                            if "nothing changed on the page" in s and "clicked" in s:
                                issue("medium", page_url, "A control did nothing when used",
                                      f"{test['title']}: {s}", rec.get("screenshot"))
                    if rec["status"] == "passed" and test.get("advances") and gem is not None and after is not None \
                            and depth < MAX_DEPTH and time.time() < deadline and not stopped():
                        follow(page_url, prefix + [test], after, depth + 1)

            def follow(page_url, prefix, snap, depth):
                io.progress(f"  ↳ new state after '{prefix[-1]['title']}': testing it")
                try:
                    ideas = gem.test_ideas(scope, planner_page(snap), tested, visited, FOLLOW_UPS, allow_submit,
                                           bool(account),
                                           f"the state reached after: {' → '.join(t['title'] for t in prefix)} "
                                           "(e.g. the next step of the form); test only what is new there, and include "
                                           "one test that validly completes this state and moves on (advances=true)")
                except Exception as ex:
                    io.progress(f"  (could not plan follow-up tests: {str(ex)[:60]})")
                    return
                run_tests(page_url, ideas["tests"], prefix, depth)

            while queue and len(result["pages"]) < pages and time.time() < deadline and not stopped():
                page_url = queue.pop(0)
                if qa.norm(page_url) in visited:
                    continue
                visited.append(qa.norm(page_url))
                web.mark()
                load_ms, status = web.goto(page_url)
                snap = web.snapshot()
                landed = snap["url"] or page_url
                page = {"url": landed, "title": snap["title"], "load_ms": round(load_ms), "status": status,
                        "purpose": "", "tests": 0}
                result["pages"].append(page)
                io.progress(f"  Page: {snap['title'] or landed}  ({load_ms / 1000:.1f} s, {len(snap['elements'])} controls, "
                            f"{len(snap['fields'])} fields)")

                # ---- automatic checks
                if not isinstance(status, int) or status >= 400:
                    issue("high", landed, f"Page returns HTTP {status}")
                for t in qa.error_texts({"headings": snap["headings"], "texts": snap["text"]}):
                    issue("high", landed, "Error shown on the page", t,
                          web.screenshot(out_dir / f"page{len(result['pages'])}-error.png"))
                if load_ms > qa.SLOW_PAGE_MS:
                    issue("medium", landed, f"Slow page load: {load_ms / 1000:.1f} s")
                site_errors(landed, web.errors)
                if not qa.domain(landed).endswith(scope):
                    issue("medium", page_url, f"Redirected off the site to {landed}")
                    continue
                for link in snap["links"]:
                    u = link["url"]
                    if qa.malformed_url(u):
                        issue("medium", landed, f"Malformed link address: {link['label'][:40]}", u)
                    elif u.startswith("http") and qa.domain(u).endswith(scope) and qa.norm(u) not in checked_links \
                            and len(checked_links) < qa.MAX_LINKS:
                        checked_links[qa.norm(u)] = (link["label"], landed, link_pool.submit(qa.http_status, u))

                # ---- tests proposed by Gemini, run in the browser, checked against what changed
                ideas = {"page_purpose": "", "tests": [], "next_pages": []}
                if gem is not None:
                    t_plan = time.time()
                    try:
                        ideas = gem.test_ideas(scope, planner_page(snap), tested, visited, tests_per_page, allow_submit,
                                               bool(account), focus or (f"the {page_hint} page" if page_hint else ""))
                    except Exception as ex:
                        io.progress(f"  (could not plan tests here: {str(ex)[:80]})")
                    io.progress(f"  {len(ideas['tests'])} tests planned ({time.time() - t_plan:.1f} s)")
                page["purpose"] = ideas.get("page_purpose", "")
                n0 = len(result["tests"])
                run_tests(landed, ideas["tests"], [], 0)
                page["tests"] = len(result["tests"]) - n0

                by_label = {l["label"].casefold(): l["url"] for l in snap["links"]}
                nxt = [by_label.get(p["label"].casefold()) for p in ideas.get("next_pages", [])]
                nxt += [l["url"] for l in snap["links"]]
                for u in nxt:
                    if u and u.startswith("http") and qa.domain(u).endswith(scope) and qa.norm(u) not in visited \
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
                    continue
                if not isinstance(st, int) or st >= 400:
                    issue("medium" if st == 404 else "low", page_url, f"Broken link ({st}): {label}", key)
            link_pool.shutdown(wait=False)
        except Exception as ex:
            if "closed" not in str(ex).casefold():
                raise
            io.progress("  The test window was closed. Writing the report for what was tested so far.")
            result["stopped"] = True
        finally:
            web.close()

    for t in result["tests"]:
        t.pop("site_errors", None)
    result["stopped"] = result.get("stopped") or stopped()
    if result["stopped"]:
        io.progress("  Stopped. Writing the report for what was tested so far.")
    result["duration_s"] = round(time.time() - t_start)
    result["links_checked"] = len(checked_links)
    paths = qa.write_reports(result, out_dir)
    passed = sum(t["status"] == "passed" for t in result["tests"])
    io.progress(f"  Done in {result['duration_s'] // 60} min {result['duration_s'] % 60} s: {len(result['pages'])} pages, "
                f"{passed}/{len(result['tests'])} tests passed, {len(result['issues'])} issues")
    io.progress(f"  Report: {paths['html']}")
    result["report"] = paths
    return result


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False
