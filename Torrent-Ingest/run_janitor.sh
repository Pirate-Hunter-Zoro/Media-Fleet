#!/bin/bash
# Launcher invoked by launchd. Pulls any config edits, then runs the artifact janitor
# once (it is a scheduled one-shot, not a daemon loop). --apply deletes; the janitor
# is a dry-run without it, so the flag is what actually performs the cleanup.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GIT="$(command -v git || true)"
source "$SCRIPT_DIR/scripts/git_pull_locked.sh"
if [ -n "$GIT" ] && "$GIT" -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] git pull --ff-only..."
    git_pull_locked "$SCRIPT_DIR" || \
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] git pull failed; using local files."
fi

CONDA_PY="/opt/homebrew/Caskroom/miniconda/base/envs/torrent_ingest_env/bin/python3"
if [ -x "$CONDA_PY" ]; then
    PYTHON="$CONDA_PY"
else
    PYTHON="$(command -v python3 || echo /opt/homebrew/bin/python3)"
fi

exec "$PYTHON" "$SCRIPT_DIR/janitor.py" --apply
