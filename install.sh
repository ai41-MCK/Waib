#!/usr/bin/env bash
# Install /waib from this clone: link the skill into ~/.claude/skills/waib
# (so repo edits apply immediately) and build the Python environment.
# Undo with ./uninstall.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"   # the real path: the link and the check agree however the clone is reached
TARGET="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/waib"

if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
    echo "ERROR: $TARGET already exists and wasn't created by this installer." >&2
    echo "       Move it away, then re-run ./install.sh." >&2
    exit 1
fi

bash "$ROOT/scripts/setup_venv.sh"

mkdir -p "$(dirname "$TARGET")"
if [ -L "$TARGET" ]; then
    [ "$(cd "$TARGET" 2>/dev/null && pwd -P)" = "$ROOT/skills/waib" ] || echo "Replacing the link to $(readlink "$TARGET")"
    rm "$TARGET"
fi
ln -s "$ROOT/skills/waib" "$TARGET"

echo ""
echo "Installed: $TARGET -> $ROOT/skills/waib"
echo "In Claude Code, run:  /waib https://example.com"
