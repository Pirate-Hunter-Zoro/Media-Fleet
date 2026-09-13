#!/bin/bash
# Launcher invoked by launchd. Pulls any config edits, then execs the doctor.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GIT="$(command -v git || true)"
# Serialized: every launcher in the monorepo pulls the ONE working tree, so a bare pull
# here races the others on .git/FETCH_HEAD ("Cannot fast-forward to multiple branches").
. "$SCRIPT_DIR/scripts/git_pull_locked.sh"
if [ -n "$GIT" ] && "$GIT" -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_pull_locked "$SCRIPT_DIR" >/dev/null 2>&1 || true
fi

PYTHON="$(command -v python3 || echo /opt/homebrew/bin/python3)"
exec "$PYTHON" "$SCRIPT_DIR/doctor.py"
