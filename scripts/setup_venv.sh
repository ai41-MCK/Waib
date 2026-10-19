#!/usr/bin/env bash
# Create (or repair) <repo>/.venv and make sure Playwright's Chromium is there.
# Safe to run on every /waib start: it's a fast no-op when all is in place.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

# A venv hardcodes absolute paths, so it breaks when the folder moves (or a
# plugin update lands in a new folder). If the deps don't import, rebuild it.
if ! "$PY" -c 'import playwright, bs4, lxml' 2>/dev/null; then
    # The newest Python 3 on the machine; 3.9 (macOS's own) is the minimum.
    PYTHON=""
    for p in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PYTHON="$p"; break; fi
    done
    [ -n "$PYTHON" ] || { echo "ERROR: Python 3.9+ is needed as python3 — install it, then try again." >&2; exit 1; }
    echo "Building Python environment in $ROOT/.venv (one-time, ~1 min)..."
    rm -rf "$ROOT/.venv"
    "$PYTHON" -m venv "$ROOT/.venv"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet "playwright>=1.40,<2" "beautifulsoup4>=4.12,<5" "lxml>=5,<7"   # no untested major version
fi

# Installs the Chromium build matching this Playwright version; no-op if present.
"$PY" -m playwright install chromium

echo "Ready: $PY"
