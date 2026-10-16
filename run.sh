#!/usr/bin/env bash
# AgentP launcher: finds Python 3.10+, sets up .venv on first run, then opens the panel.
#   ./run.sh              open the panel (⌥Space)
#   ./run.sh --terminal   use the terminal interface instead
#   ./run.sh --test SITE  exploratory functional test of a website (report in ~/.agentp/qa)
# Extra arguments are passed through to ui.py / agent.py.
set -euo pipefail
cd "$(dirname "$0")"

find_python() {
  for py in python3.13 python3.12 python3.11 python3.10 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$py" >/dev/null 2>&1 &&
       "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$py"
      return 0
    fi
  done
  return 1
}

if [ "$(uname)" != "Darwin" ]; then
  echo "AgentP runs on macOS only." >&2
  exit 1
fi

# 1. a virtual environment with the dependencies (re-installed when requirements.txt changes)
stamp=".venv/.requirements"
if [ ! -x .venv/bin/python ] || ! cmp -s requirements.txt "$stamp"; then
  if ! PY=$(find_python); then
    echo "AgentP needs Python 3.10 or newer (macOS ships 3.9)." >&2
    if command -v brew >/dev/null 2>&1; then
      echo "Install it with:  brew install python@3.12   then run ./run.sh again." >&2
    else
      echo "Install it from https://www.python.org/downloads/macos/ then run ./run.sh again." >&2
    fi
    exit 1
  fi
  echo "Setting up AgentP with $("$PY" --version) (first run only)…"
  rm -rf .venv
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  cp requirements.txt "$stamp"
  echo "Done."
fi

# 2. Accessibility permission for the app this runs in (Terminal, iTerm, …)
if ! .venv/bin/python -c 'from ApplicationServices import AXIsProcessTrusted; import sys; sys.exit(0 if AXIsProcessTrusted() else 1)'; then
  host="${TERM_PROGRAM:-your terminal}"
  [ "$host" = "Apple_Terminal" ] && host="Terminal"
  echo
  echo "One permission needed (once): in the System Settings window that just opened,"
  echo "turn on \"$host\". Then quit $host (⌘Q), reopen it and run ./run.sh again."
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" || true
  echo
fi

# 3. run (the panel asks for your API keys on first launch)
if [ "${1:-}" = "--test" ]; then
  shift
  exec .venv/bin/python qa.py "$@"
fi
if [ "${1:-}" = "--terminal" ]; then
  shift
  exec .venv/bin/python agent.py "$@"
fi
exec .venv/bin/python ui.py "$@"
