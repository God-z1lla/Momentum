#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "Python 3 is required. Install it with Homebrew or python.org, then try again." >&2
    exit 1
}

"$PYTHON_BIN" -m venv .venv
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -r requirements.txt

echo
echo "Installation complete."
echo "Start the app with: .venv/bin/python run.py"
