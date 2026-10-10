"""
config.py — the user's own API keys and settings.

AgentP ships without any keys. Each user brings their own two:
  TYPESAFE_API_KEY   Jev, which makes every on-screen decision
  GEMINI_API_KEY     planning, writing text, answers, name lookups

Keys are stored only on this Mac, in ~/.agentp/config.json (readable by the user
only). Environment variables, when set, take precedence over the file.
"""
import json
import os
from getpass import getpass
from pathlib import Path

PATH = Path.home() / ".agentp" / "config.json"

KEYS = {   # name: (label, required, where to get one)
    "TYPESAFE_API_KEY": ("TypeSafe API key (Jev)", True, "https://docs.typesafe.ai"),
    "GEMINI_API_KEY": ("Google Gemini API key", True, "https://aistudio.google.com/apikey"),
}


def load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def apply():
    """Put stored keys into the environment (without overriding variables that are already set)."""
    for name, value in load().items():
        if name in KEYS and value and not os.environ.get(name):
            os.environ[name] = value


def save(values: dict):
    """Store keys (empty values are removed) and apply them to this process."""
    data = load()
    for name, value in values.items():
        if name not in KEYS:
            continue
        value = (value or "").strip()
        if value:
            data[name] = value
            os.environ[name] = value
        else:
            data.pop(name, None)
            os.environ.pop(name, None)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # owner-only from the start
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.chmod(PATH, 0o600)


def missing_required() -> list[str]:
    return [n for n, (_, required, _) in KEYS.items() if required and not os.environ.get(n)]


def mask(value: str | None) -> str:
    if not value:
        return "not set"
    return value[:6] + "…" + value[-4:] if len(value) > 12 else "set"


def setup_cli():
    """Ask for the keys in the terminal. Input is hidden; Enter keeps the current value."""
    print("AgentP uses your own API keys. They are stored only on this Mac, in", PATH)
    values = {}
    for name, (label, required, url) in KEYS.items():
        current = os.environ.get(name)
        hint = f"current: {mask(current)}, Enter to keep" if current else ("required" if required else "optional")
        print(f"\n{label}  ({hint})\n  get one at {url}")
        entered = getpass("  paste it here: ").strip()
        values[name] = entered or current or ""
    save(values)
    print(f"\nSaved. TypeSafe: {mask(os.environ.get('TYPESAFE_API_KEY'))}, "
          f"Gemini: {mask(os.environ.get('GEMINI_API_KEY'))}\n")
