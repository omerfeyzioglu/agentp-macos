#!/usr/bin/env python3
"""
eval.py — Measures the agent's decision accuracy on recorded screens.

At every step the agent writes the screen and its decision to ~/.agentp/records/.
Steps taken as correct (gold data):
  * successful steps of requests that finished, were not cancelled and were
    not marked with '!wrong'
  * elements the user chose when asked "Not sure — which one?"

For every gold step decide() is called again with the same screen and history;
no app is touched. This shows whether a change (instructions, element count,
thresholds) improves accuracy or not.

Usage:
  python3 eval.py                     # all records
  python3 eval.py --limit 50 -v       # last 50 steps, show the misses
  python3 eval.py --memory            # memory on (optimistic: it was learned from these records)
  python3 eval.py --engine gemini     # Gemini makes the per-step choice instead of Jev
"""
import argparse
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from typesafe_sdk import TypeSafeClient

import agent
from memory import Memory



class NoPlanner:
    """Stands in for the planner during eval so 'summarize' stays a valid choice (never called)."""
    model = "eval"


class EvalIO:
    """No user during eval: questions get no answer (counted as wrong)."""

    def __init__(self):
        self.asked = False

    def say(self, text):
        pass

    def confirm(self, msg, default_yes=False):
        return False

    def pick(self, question, options):
        self.asked = True
        return None


def load(paths=None) -> list[dict]:
    files = sorted(agent.RECORDS_DIR.glob("*.jsonl")) if paths is None else paths
    rows = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def gold_steps(rows: list[dict]) -> list[dict]:
    outcome = {r["cmd_id"]: r for r in rows if r.get("type") == "outcome"}
    bad = {r["cmd_id"] for r in rows if r.get("type") == "mark" and r.get("bad")}
    gold = []
    for r in rows:
        if r.get("type") != "step" or r["cmd_id"] in bad:
            continue
        o = outcome.get(r["cmd_id"])
        user_pick = r["decision"].get("elem_source") == "user" and r["decision"].get("label")
        if user_pick or (o and o["ok"] and not o["cancelled"] and r.get("step_ok")):
            gold.append(r)
    return gold


def same(pred: dict, want: dict) -> dict:
    act_ok = pred["action"] == want["action"]
    res = {"action": act_ok}
    if want["action"] in ("click", "type", "read"):
        el = pred.get("element")
        res["element"] = act_ok and el is not None and \
            (el["role"], el["label"]) == (want["role"], want["label"])
    if want["action"] == "type":
        res["text"] = act_ok and pred.get("text") == want.get("text")
    if want["action"] == "open_app":
        res["target"] = act_ok and pred.get("app") == want.get("target")
    if want["action"] == "open_url":
        res["target"] = act_ok and pred.get("site") == want.get("target")
    res["all"] = all(res.values())
    return res


def plan_position(rec: dict) -> int | None:
    """Index of the first plan step not done yet, as recorded."""
    return rec.get("plan_at")   # None for records from before plan tracking: replayed unrestricted


def run_one(client, rec: dict, installed: list, use_memory: bool) -> dict:
    io = EvalIO()
    ctx = agent.Ctx(installed=installed, io=io, memory=Memory(enabled=use_memory), record=False,
                    planner=NoPlanner() if rec.get("plan") else None)
    ctx.active_app = rec["app"]
    ctx.history = rec["history"]
    items = [{**i, "el": None, "pressable": True} for i in rec["items"]]
    timings = {}
    t = time.perf_counter()
    try:
        pred = agent.decide(client, rec["cmd"], ctx, items, rec["steps_before"], set(), timings, rec["screen"],
                            None, rec.get("plan"), plan_position(rec))
    except Exception as e:
        return {"rec": rec, "error": str(e)}
    return {"rec": rec, "pred": pred, "score": same(pred, rec["decision"]), "asked": io.asked,
            "ms": (time.perf_counter() - t) * 1000, "jev_calls": len(timings)}


def run_gemini(gem, rec: dict, installed: list) -> dict:
    """The same per-step choice, made by Gemini from the same options."""
    ctx = agent.Ctx(installed=installed, io=EvalIO(), memory=Memory(enabled=False), record=False,
                    planner=NoPlanner() if rec.get("plan") else None)
    ctx.active_app, ctx.history = rec["app"], rec["history"]
    items = [{**i, "el": None, "pressable": True} for i in rec["items"]]
    plan = rec.get("plan")
    built = agent.build_options(rec["cmd"], ctx, items, set(), plan, plan_position(rec))
    o = built["opts"]
    state = agent.build_state(rec["cmd"], ctx, items, rec["steps_before"], rec["screen"], None, plan,
                              plan_position(rec))
    t = time.perf_counter()
    try:
        g = gem.choose(state, {k: o[k] for k in ("action", "element", "text", "app", "site", "system", "shortcut")})
    except Exception as e:
        return {"rec": rec, "error": str(e)}
    key = lambda name: g.get(name) if g.get(name) in o[name] and g.get(name) != "none" else None  # noqa: E731
    pred = {"action": g.get("action"),
            "element": items[int(key("element")[1:])] if key("element") else None,
            "text": agent.quoted(rec["cmd"]) or (o["text"][key("text")] if key("text") else None),
            "app": built["apps"][int(key("app")[1:])] if key("app") else None,
            "site": o["site"][key("site")] if key("site") else None}
    return {"rec": rec, "pred": pred, "score": same(pred, rec["decision"]), "asked": False,
            "ms": (time.perf_counter() - t) * 1000, "jev_calls": 1}


def describe(d: dict) -> str:
    if isinstance(d.get("element"), dict):
        tgt = d["element"]["label"][:50]
    else:
        tgt = d.get("label") or d.get("target") or d.get("app") or d.get("site") or ""
    txt = f" ← {d['text']!r}" if d.get("action") == "type" and d.get("text") else ""
    return f"{d['action']} {tgt}{txt}".strip()


def main():
    ap = argparse.ArgumentParser(description="AgentP decision accuracy")
    ap.add_argument("--limit", type=int, default=0, help="Last N gold steps (0 = all)")
    ap.add_argument("--memory", action="store_true", help="Measure with memory on")
    ap.add_argument("--workers", type=int, default=4, help="Parallel Jev calls")
    ap.add_argument("-v", "--verbose", action="store_true", help="Show the misses")
    ap.add_argument("--engine", choices=("jev", "gemini"), default="jev", help="Who makes the per-step choice")
    args = ap.parse_args()
    agent.check_env()   # platform + the user's keys

    gold = gold_steps(load())
    if args.limit:
        gold = gold[-args.limit:]
    if not gold:
        print(f"No recorded steps yet. Run AgentP with AGENTP_RECORD=1 to record them under {agent.RECORDS_DIR}.")
        return
    print(f"Measuring {len(gold)} gold steps with {args.engine} (memory {'on' if args.memory else 'off'})…")

    installed = agent.installed_apps()
    if args.engine == "gemini":
        import planner
        gem = planner.Gemini()
        run = lambda r: run_gemini(gem, r, installed)  # noqa: E731
    else:
        client = TypeSafeClient()
        run = lambda r: run_one(client, r, installed, args.memory)  # noqa: E731
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(run, gold))

    errors = [r for r in results if "error" in r]
    done = [r for r in results if "error" not in r]
    by_action = defaultdict(lambda: [0, 0])
    for r in done:
        a = r["rec"]["decision"]["action"]
        by_action[a][0] += r["score"]["all"]
        by_action[a][1] += 1

    n = len(done) or 1
    acc = sum(r["score"]["all"] for r in done) / n
    act_acc = sum(r["score"]["action"] for r in done) / n
    asked = sum(r["asked"] for r in done) / n
    ms = sorted(r["ms"] for r in done)
    print(f"\nFully correct    : {acc:6.1%}   ({sum(r['score']['all'] for r in done)}/{len(done)})")
    print(f"Action correct   : {act_acc:6.1%}")
    print(f"Asked the user   : {asked:6.1%}")
    if ms:
        print(f"Decision time    : median {ms[len(ms) // 2]:.0f} ms, worst 10% {ms[int(len(ms) * 0.9)]:.0f} ms")
    usage = agent.USAGE
    if args.engine == "gemini":
        import planner
        usage = planner.USAGE
        print(f"Model            : {gem.resolved_model}")
    if usage["calls"]:
        print(f"Model calls/step : {usage['calls'] / n:.2f}")
        print(f"Tokens / step    : input {usage['input'] / n:.0f}, output {usage['output'] / n:.0f}"
              + ("  (output includes thinking)" if args.engine == "gemini" else ""))
    if errors:
        print(f"Errors           : {len(errors)} (first: {errors[0]['error']})")
    print("\nBy action:")
    for a, (good, total) in sorted(by_action.items(), key=lambda kv: -kv[1][1]):
        print(f"  {a:9s} {good / total:6.1%}  ({good}/{total})")

    if args.verbose:
        print("\nMisses:")
        for r in done:
            if not r["score"]["all"]:
                rec = r["rec"]
                print(f"  • '{rec['cmd']}' (step {rec['step']}, {rec['app']})")
                print(f"      expected: {describe(rec['decision'])}")
                print(f"      agent   : {describe(r['pred'])}")


if __name__ == "__main__":
    main()
