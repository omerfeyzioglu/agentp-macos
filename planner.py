"""
planner.py — the "think once" part: a fast LLM (Gemini) that plans, writes and summarizes.

Jev chooses; it cannot plan a multi-step task or write text that is not in the
command. Gemini fills that gap and is called rarely:
  plan()       once per request: high-level steps + the exact text to type
  summarize()  for research questions: answer from the text visible on screen
  choose()     only for eval.py: Gemini makes the per-step choice Jev normally
               makes, so the two can be compared on the same recorded screens

Needs GEMINI_API_KEY. Model: GEMINI_MODEL (default gemini-flash-latest).
"""
import json
import os
import re
import urllib.parse

from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
LOOKUP_MODEL = os.environ.get("GEMINI_LOOKUP_MODEL", "gemini-flash-lite-latest")   # fast, with Google Search
ANSWER_MODEL = os.environ.get("GEMINI_ANSWER_MODEL", "gemini-flash-lite-latest")   # questions answered in words
USAGE = {"calls": 0, "input": 0, "output": 0}   # Gemini token usage (output includes thinking)

PLAN_KINDS = ["open_app", "open_url", "click", "type", "enter", "escape", "scroll", "back", "shortcut",
              "quit_app", "wait", "summarize", "read", "system"]

PLAN_SYSTEM = """You plan tasks for a macOS computer agent.
A fast executor model carries out each step by looking at the screen (accessibility elements), so
keep steps high level, one UI action each, e.g. "Open the chat with Mom", "Type the message",
"Press Enter to send". Return 1-8 steps.

Step kinds: open_app, open_url, click, type, enter, escape, scroll, back, shortcut, quit_app,
wait (until a page loads or an app finishes working), summarize (read the visible text and answer
or summarize it with AI), read, system.

What the agent can and cannot do: it presses on-screen elements (buttons, links, rows, fields),
types text, presses keys, scrolls and uses the shortcuts listed in available_shortcuts ("target"
of a shortcut step must be one of those names). It cannot draw or drag on a canvas, click at
arbitrary positions, or pick files in dialogs. If the request needs those, say so in "issue".

Rules:
- "type" steps: put the exact final text in "text". Write it naturally from the user's intent and
  fix obvious dictation errors; keep the user's language unless they ask otherwise.
  Use {answer} inside "text" to insert the result of the last summarize step.
- open_app: "target" is the app name exactly as in the installed app list.
- open_url: "target" is a full URL. For research, open https://www.google.com/search?q=... directly.
- To write into a notes or documents app (Notes, TextEdit, Pages, Word…), first create a new note or
  document with the shortcut "new_window" (⌘N), then type.
- Messaging means the desktop app (WhatsApp, Slack, Messages) when it is installed. To message
  someone: open the app, open the chat with them (search it if needed), type, press Enter.
- People the user messages, calls or emails are the user's own contacts: search them in the app's own
  chat / contact search (e.g. the DM "New message" search) with exactly the name the user said. Never
  use a web handle, profile URL or global site search for them, even if a famous person shares the name.
- Use only apps from installed_apps; names in parentheses are other names the app answers to
  (e.g. an app listed as "ChatGPT (openai codex)" IS the Codex app). If the app the user asks for
  is not installed, do not silently swap it: describe that in "issue" and, if a reasonable
  installed alternative exists, plan with it (the user will be asked to confirm).
- Never use a terminal / shell unless the user explicitly asks for one.
- The agent waits for pages and apps to load by itself: never add "wait" for loading. Use "wait"
  only after asking another app to do long work (e.g. a coding agent writing code).
- "Find / open / show me X" means reaching X itself: after searching, click the best result (or the
  newest video, the right profile…) instead of stopping on the results page. Summarize only when
  the user asks a question or for a summary.
- For a site, prefer the URL that lands closest to the goal (e.g. a profile, inbox or messages page).
- When several apps or sites could do the job (email: Mail or Gmail; music: Spotify or YouTube), prefer
  the one in apps_and_sites_this_user_uses.
- Calls in messaging apps: open the chat, then use its call button or the app's Call menu item.
- Do not add steps that the current screen shows are already done.
- Never invent steps the user did not ask for (no extra sending, deleting, buying).

Understanding what the user means:
- Read the request the way most people would mean it. A name, nickname or handle ("mkbhd", "mr
  beast", "the kafka docs") usually refers to the well-known person, creator, channel, brand or
  product of that name, not to a random account that happens to match the letters.
- Use the links in known_facts (they come from a web search). Otherwise never guess handles, profile
  URLs or IDs: plan a search on the site (or Google) and pick the official / verified / most followed
  result.
- If the request is genuinely ambiguous between a few different meanings and the screen and history do
  not settle it, do not guess: fill "clarify" with a short question and 2-3 options in the user's language."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": PLAN_KINDS},
                    "do": {"type": "string"},
                    "target": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["kind", "do"],
            },
        },
        "issue": {"type": "string", "description": "Empty unless the request cannot be done as asked."},
        "clarify": {"type": "object", "properties": {"question": {"type": "string"},
                                                      "options": {"type": "array", "items": {"type": "string"}}}},
    },
    "required": ["steps"],
}


class Gemini:
    def __init__(self, model: str = MODEL):
        self.client = genai.Client()   # reads GEMINI_API_KEY
        self.model = model
        self._thinking = "unknown"     # which thinking setting this model accepts
        self.resolved_model = model    # what the alias actually points to (from the first response)
        self.last_searches: list[str] = []   # Google searches made by the last lookup()
        self.grounding = os.environ.get("AGENTP_GROUNDING", "1") != "0"

    # ------------------------------------------------ low level
    def _configs(self, system: str, schema: dict | None):
        base = {"system_instruction": system, "temperature": 0.2}
        if schema:
            base |= {"response_mime_type": "application/json", "response_json_schema": schema}
        # fastest setting first; older/newer models accept different thinking knobs
        options = {
            "level": types.ThinkingConfig(thinking_level="low"),
            "budget": types.ThinkingConfig(thinking_budget=0),
            "default": None,
        }
        order = [self._thinking] if self._thinking in options else list(options)
        for name in order:
            cfg = dict(base)
            if options[name] is not None:
                cfg["thinking_config"] = options[name]
            yield name, types.GenerateContentConfig(**cfg)

    def _generate(self, system: str, prompt: str, schema: dict | None = None) -> str:
        err = None
        for name, cfg in self._configs(system, schema):
            try:
                r = self.client.models.generate_content(model=self.model, contents=prompt, config=cfg)
                self._thinking = name
                u = r.usage_metadata
                if u is not None:
                    USAGE["calls"] += 1
                    USAGE["input"] += u.prompt_token_count or 0
                    USAGE["output"] += (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
                if r.model_version:
                    self.resolved_model = r.model_version
                return r.text or ""
            except Exception as e:   # unsupported thinking setting → try the next one
                err = e
                if self._thinking == name:
                    raise
        raise err

    def _json(self, system: str, prompt: str, schema: dict) -> dict:
        return json.loads(self._generate(system, prompt, schema))

    # ------------------------------------------------ planning
    def plan(self, cmd: str, where: str, history: list, installed: list[str],
             done: list[str] | None = None, problem: str | None = None,
             shortcuts: list[str] | None = None, recent: list[str] | None = None,
             clarified: str | None = None, facts: list[dict] | None = None) -> tuple[list[dict], str, dict | None]:
        """(steps, issue). issue is non-empty when the request cannot be done exactly as asked."""
        prompt = {
            "user_request": cmd,
            "current_screen": where,
            "previous_requests": history,
            "installed_apps": installed,
            "available_shortcuts": shortcuts or [],
            "apps_and_sites_this_user_uses": recent or [],
        }
        if done:
            prompt["steps_already_done"] = done
        if problem:
            prompt["problem_so_far"] = problem + " — plan the remaining steps differently."
        if clarified:
            prompt["user_clarified_meaning"] = clarified
        if facts:
            prompt["known_facts"] = facts
        out = self._json(PLAN_SYSTEM, json.dumps(prompt, ensure_ascii=False), PLAN_SCHEMA)
        steps = [s for s in out.get("steps", []) if s.get("do")][:8]
        clarify = out.get("clarify") or None
        if clarify and len(clarify.get("options") or []) < 2:
            clarify = None
        return steps, (out.get("issue") or "").strip(), clarify

    def lookup(self, cmd: str, known: list[str]) -> list[dict]:
        """Resolve the specific people, creators, channels, brands, products or places the request names,
        as most people would mean them, with their official links. [] if it names none (no search then)."""
        if not self.grounding:
            return []
        system = ("You resolve names for a computer agent. Find the specific public figures, creators, "
                  "channels, brands, products, organisations or places named in the request (not generic "
                  "words). NEVER include a person the user wants to message, call, email, text, reply to, send "
                  "something to or open a chat with: that is the user's own contact, not a public figure, even "
                  "if a famous person has the same name. Still resolve the public names in the same request: in "
                  "'send mkbhd's latest video to Alex', resolve mkbhd and not Alex. Skip names in already_known. For each, search the web and identify the one most people mean "
                  "(the well-known one), with its official links. Answer with JSON only: "
                  '[{"name": "...", "about": "one short line", "links": {"youtube": "...", "instagram": "...", '
                  '"x": "...", "website": "..."}}]. Only include links you found. If there are no such names, '
                  "answer [] without searching.")
        prompt = json.dumps({"request": cmd, "already_known": known}, ensure_ascii=False)
        cfg = types.GenerateContentConfig(system_instruction=system, temperature=0.1,
                                          tools=[types.Tool(google_search=types.GoogleSearch())])
        r = self.client.models.generate_content(model=LOOKUP_MODEL, contents=prompt, config=cfg)
        u = r.usage_metadata
        if u is not None:
            USAGE["calls"] += 1
            USAGE["input"] += u.prompt_token_count or 0
            USAGE["output"] += (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
        meta = r.candidates[0].grounding_metadata if r.candidates else None
        self.last_searches = list(getattr(meta, "web_search_queries", None) or [])
        raw = (r.text or "").strip()
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end < start:
            return []
        found = json.loads(raw[start:end + 1])
        return [f for f in found if isinstance(f, dict) and f.get("name")]

    def answer(self, cmd: str, history: list, on_text=None,
               search: bool = True) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
        """Answer a question in words, streaming. With search=True Google Search is available (current or
        specific facts, ~2 s); without it the answer comes from the model alone (~0.6 s).
        on_text(delta) receives the text as it arrives.
        Returns (answer, [(source title, url)], [(Google Search suggestion, url)])."""
        system = ("Answer the user's question directly. Lead with the answer itself, no preamble. Be brief: "
                  "2-5 sentences, or a short list when the question asks for several things. Use the user's "
                  "language. Use Google Search for facts, people, prices, news and anything that may have "
                  "changed. Plain text: no markdown, no bold, no headings.")
        prompt = json.dumps({"question": cmd, "previous_requests": history}, ensure_ascii=False)
        cfg = types.GenerateContentConfig(system_instruction=system, temperature=0.3,
                                          tools=[types.Tool(google_search=types.GoogleSearch())] if search else None)
        text, sources, last = "", [], None
        for chunk in self.client.models.generate_content_stream(model=ANSWER_MODEL, contents=prompt, config=cfg):
            delta = chunk.text or ""
            if delta:
                text += delta
                if on_text:
                    on_text(delta)
            last = chunk
        u = getattr(last, "usage_metadata", None)
        if u is not None:
            USAGE["calls"] += 1
            USAGE["input"] += u.prompt_token_count or 0
            USAGE["output"] += (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
        meta = last.candidates[0].grounding_metadata if last is not None and last.candidates else None
        for ch in getattr(meta, "grounding_chunks", None) or []:
            web = getattr(ch, "web", None)
            if web is not None and web.uri and (web.title, web.uri) not in sources:
                sources.append((web.title or web.uri, web.uri))
        return text.strip(), sources[:4], search_suggestions(meta)

    # ------------------------------------------------ exploratory testing (qa.py)
    def test_ideas(self, site: str, page: dict, tested: list[str], visited: list[str],
                   max_tests: int, allow_submit: bool) -> dict:
        """Act as a QA engineer on one page: what does it do, which functional tests are worth
        running here (with concrete steps and the expected result), and which pages to explore next."""
        system = f"""You are a senior QA engineer doing exploratory functional testing of {site}.
A computer agent will carry out your test steps in a real browser, one plain command at a time, and a
checker will verify your expected result by looking at the screen afterwards.

Given the current page (title, URL, headings, links, buttons, fields), return:
- page_purpose: one short line.
- tests: at most {max_tests} functional tests that matter most on THIS page and were not already done
  (see already_tested). Cover the page's core features: search, filters, sorting, forms (one valid case
  and one invalid case such as empty required fields or a malformed email), navigation menus, tabs,
  accordions, pagination, add-to-cart / like / follow style buttons, login error handling.
  Each test: title; kind (navigation | form | search | interaction | content); steps — 1 to 5 short
  commands that refer to elements by their visible labels (e.g. "type 'qa.tester@example.com' into the
  Email field", "click the 'Subscribe' button"); expect — ONE observable outcome the checker can see on
  screen (e.g. "an error message about the email address is shown", "the results list shows items
  related to 'shoes'", "the Pricing page is open").
- next_pages: up to 3 links (exact labels from the list) leading to important unvisited pages, most
  important first.

Rules: stay on {site}. Use made-up test data only (names like "QA Tester", emails at example.com,
phone 5550000000). Never pay, buy, place orders, delete, unsubscribe, log out, change account settings,
post publicly, or contact real people. {"Submitting forms with test data is allowed." if allow_submit
else "Do not submit forms: test validation by leaving fields empty and observe inline messages only."}
Prefer tests whose outcome is visible without logging in. Do not spend tests on merely opening a link to
another page (the crawler visits pages by itself): test what the page DOES."""
        schema = {
            "type": "object",
            "properties": {
                "page_purpose": {"type": "string"},
                "tests": {"type": "array", "items": {"type": "object", "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["navigation", "form", "search", "interaction", "content"]},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "expect": {"type": "string"}},
                    "required": ["title", "kind", "steps", "expect"]}},
                "next_pages": {"type": "array", "items": {"type": "object", "properties": {
                    "label": {"type": "string"}, "why": {"type": "string"}}, "required": ["label"]}},
            },
            "required": ["page_purpose", "tests", "next_pages"],
        }
        prompt = json.dumps({"page": page, "already_tested": tested[-40:], "visited_pages": visited[-30:]},
                            ensure_ascii=False)
        out = self._json(system, prompt, schema)
        out["tests"] = [t for t in out.get("tests", []) if t.get("steps") and t.get("expect")][:max_tests]
        return out

    def judge(self, expectation: str, screen: dict) -> tuple[bool, str]:
        """Second opinion when Jev is unsure whether an expected result is on screen."""
        system = ("You check one expected result of a functional test against what is on the screen. "
                  "Answer with JSON: {\"passed\": true|false, \"reason\": \"one short sentence\"}.")
        schema = {"type": "object", "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
                  "required": ["passed", "reason"]}
        out = self._json(system, json.dumps({"expected": expectation, "screen": screen}, ensure_ascii=False), schema)
        return bool(out.get("passed")), out.get("reason", "")

    def summarize(self, cmd: str, where: str, texts: list[str]) -> str:
        system = ("Summarize what the screen text says about the topic of the user's request, or answer "
                  "the question in it, using only that text. Ignore the parts of the request about where to "
                  "save, note or send the result. Never talk about the screen, apps, or what is missing. "
                  "At most 4 sentences, plain text, no markdown, in the user's language. If nothing on the "
                  "screen is relevant, answer only: No relevant information found.")
        prompt = json.dumps({"user_request": cmd, "screen": where, "screen_text": texts[:400]},
                            ensure_ascii=False)
        return self._generate(system, prompt).strip()

    # ------------------------------------------------ eval: per-step choice
    def choose(self, state: dict, options: dict[str, dict]) -> dict:
        """Same decision Jev makes, from the same options. Returns option keys."""
        schema = {"type": "object", "properties": {}, "required": []}
        for name, opts in options.items():
            schema["properties"][name] = {"type": "string", "enum": list(opts)}
            schema["required"].append(name)
        schema["properties"]["final"] = {"type": "boolean"}
        schema["required"].append("final")
        system = ("You control a Mac. Pick the ONE next step for the user's request from the options. "
                  "'final' is true if the request is completely done after this step.")
        prompt = json.dumps({"state": state, "options": options}, ensure_ascii=False)
        return self._json(system, prompt, schema)


def search_suggestions(meta) -> list[tuple[str, str]]:
    """Google Search suggestions that must be shown with a grounded answer: the search queries, each
    linking to its Google results page (taken unchanged from the API's rendered chips when present)."""
    if meta is None:
        return []
    html = getattr(getattr(meta, "search_entry_point", None), "rendered_content", None) or ""
    chips = [(re.sub(r"<[^>]+>", "", text).strip(), href.replace("&amp;", "&"))
             for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)]
    chips = [(t, u) for t, u in chips if t and u]
    if chips:
        return chips[:4]
    queries = getattr(meta, "web_search_queries", None) or []
    return [(q, "https://www.google.com/search?q=" + urllib.parse.quote(q)) for q in queries[:4]]


def available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
