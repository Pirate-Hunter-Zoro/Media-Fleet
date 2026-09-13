#!/bin/bash
# Launcher invoked by launchd. Keeps the engine's environment sane.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GIT="$(command -v git || true)"

# Pull any config edits made from another machine before syncing. Non-fatal: if the
# network is down or the pull can't fast-forward, log it and sync with what is local.
#
# This is NOT load-bearing for content any more. Adding something to the library does not
# involve this repo at all -- save the playlist on YouTube and the next cycle finds it.
# The pull only matters for an actual code/config change.
source "$SCRIPT_DIR/git_pull_locked.sh"
if [ -n "$GIT" ] && "$GIT" -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] git pull --ff-only..."
    if ! git_pull_locked "$SCRIPT_DIR"; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] git pull failed; using local config."
    fi
fi

# Run under Torrent-Ingest's conda env, not `command -v python3`. This repo imports
# Torrent-Ingest's `config`/`library`/`classify` modules, so that env already has the
# dependencies -- and it is the interpreter that already carries the Full Disk Access
# grant (iCloud Drive + Downloads + volumes). `command -v python3` resolves to Homebrew's
# python@3.14, whose TCC grants silently lapse on every `brew upgrade python`.
CONDA_PY="/opt/homebrew/Caskroom/miniconda/base/envs/torrent_ingest_env/bin/python3"
if [ -x "$CONDA_PY" ]; then
    PYTHON="$CONDA_PY"
else
    PYTHON="$(command -v python3 || echo /opt/homebrew/bin/python3)"
fi

exec "$PYTHON" "$SCRIPT_DIR/youtube_sync.py" "$@"
