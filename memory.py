"""
memory.py — AgentP's memory (~/.agentp/memory.json)

Two kinds of entries:
  picks : The element the user chose when asked "Not sure — which one?".
          A similar request in the same app will not ask again.
  paths : Steps of requests that finished successfully. For a similar request
          they are given to Jev as a hint ("last time, this step was …"); when
          the step sequence matches, the extra "done?" call is skipped.
"""
import difflib
import json
import re
from pathlib import Path

PATH = Path.home() / ".agentp" / "memory.json"
MAX_PATHS = 300
MAX_PICKS = 300


def _norm(s: str) -> str:
    return " ".join(re.findall(r"\w+", s.casefold()))


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


class Memory:
    def __init__(self, path: Path = PATH, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self.data = {"picks": [], "paths": []}
        if enabled and path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

    def _save(self):
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------ user picks
    def add_pick(self, app: str | None, cmd: str, role: str, label: str):
        self.data["picks"].append({"app": app, "cmd": cmd, "role": role, "label": label})
        self.data["picks"] = self.data["picks"][-MAX_PICKS:]
        self._save()

    def find_pick(self, app: str | None, cmd: str, items: list[dict]) -> dict | None:
        """If the user picked an element for a similar request in this app and it is on screen, return it."""
        if not self.enabled:
            return None
        by_label = {(i["role"], i["label"]): i for i in items}
        best, best_sim = None, 0.75
        for p in reversed(self.data["picks"]):
            if p["app"] != app:
                continue
            sim = similarity(p["cmd"], cmd)
            it = by_label.get((p["role"], p["label"]))
            if it and sim > best_sim:
                best, best_sim = it, sim
        return best

    # ------------------------------------------------ successful paths
    def add_path(self, cmd: str, steps: list[dict]):
        if not steps:
            return
        self.data["paths"].append({"cmd": cmd, "steps": steps})
        self.data["paths"] = self.data["paths"][-MAX_PATHS:]
        self._save()

    def forget_last_path(self, cmd: str):
        for i in range(len(self.data["paths"]) - 1, -1, -1):
            if self.data["paths"][i]["cmd"] == cmd:
                del self.data["paths"][i]
                self._save()
                return

    # ------------------------------------------------ who / what names refer to
    def add_facts(self, facts: list[dict]):
        ent = self.data.setdefault("entities", {})
        for f in facts:
            ent[f["name"].casefold()] = {"name": f["name"], "about": f.get("about", ""), "links": f.get("links", {})}
        self._save()

    def facts_for(self, cmd: str) -> list[dict]:
        """Remembered facts about names that appear in the command ('mkbhd' → Marques Brownlee, @mkbhd)."""
        if not self.enabled:
            return []
        low = " " + re.sub(r"[^\w]+", " ", cmd.casefold()) + " "
        return [f for key, f in self.data.get("entities", {}).items()
                if " " + re.sub(r"[^\w]+", " ", key).strip() + " " in low]

    def recent_targets(self, n: int = 15) -> list[str]:
        """Apps and sites the user's successful requests used, most recent first (e.g. Gmail vs Mail)."""
        seen = []
        for p in reversed(self.data["paths"]):
            for s in p["steps"]:
                t = s.get("target")
                if s["action"] == "open_url" and t:
                    t = re.sub(r"^https?://(www\.)?", "", t).split("/")[0]   # domain is enough
                if s["action"] in ("open_app", "open_url") and t and t not in seen:
                    seen.append(t)
        return seen[:n]

    def find_path(self, cmd: str, min_sim: float = 0.6) -> tuple[dict | None, float]:
        if not self.enabled:
            return None, 0.0
        best, best_sim = None, min_sim
        for p in self.data["paths"]:
            sim = similarity(p["cmd"], cmd)
            if sim > best_sim:
                best, best_sim = p, sim
        return best, (best_sim if best else 0.0)
