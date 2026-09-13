#!/bin/bash
# Launcher invoked by launchd. Pulls any config edits, then execs the daemon.
# The daemon runs its own loop, so launchd only needs to (re)start this if it dies.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GIT="$(command -v git || true)"
# Serialized: every launcher in the monorepo pulls the ONE working tree, so a bare pull
# here races the others on .git/FETCH_HEAD ("Cannot fast-forward to multiple branches").
. "$SCRIPT_DIR/scripts/git_pull_locked.sh"
if [ -n "$GIT" ] && "$GIT" -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] git pull --ff-only..."
    if ! git_pull_locked "$SCRIPT_DIR"; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] git pull failed; using local files."
    fi
fi

# Stdlib-only, so the system python works; fall back to Homebrew's.
PYTHON="$(command -v python3 || echo /opt/homebrew/bin/python3)"

exec "$PYTHON" "$SCRIPT_DIR/scout.py"
