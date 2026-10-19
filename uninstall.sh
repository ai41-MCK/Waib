#!/usr/bin/env bash
# Undo install.sh: remove the skill link and the repo's Python environment.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"   # the real path: the link and the check agree however the clone is reached
TARGET="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/waib"

# Only remove the link if it points at THIS clone (another clone may own it).
if [ -L "$TARGET" ] && [ "$(cd "$TARGET" 2>/dev/null && pwd -P)" = "$ROOT/skills/waib" ]; then
    rm "$TARGET"
    echo "Removed $TARGET"
fi
# An app still running from this clone would keep going until its idle stop. Match its script
# (macOS's own python3 shows up as Python.app, not as .venv/bin/python), by the real path.
# pkill reads a pattern: a folder like "Waib (copy)" must match literally
pkill -f "$(printf '%s' "$ROOT/scripts/app.py" | sed 's/[][\\.*^$()+?{}|]/\\&/g')" 2>/dev/null || true
rm -rf "$ROOT/.venv"
echo "Removed $ROOT/.venv"
echo "Playwright's shared browser cache (~/Library/Caches/ms-playwright on macOS,"
echo "~/.cache/ms-playwright on Linux) was left in place; delete it if nothing else uses it."
