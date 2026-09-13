#!/bin/bash
# Launcher invoked by launchd. Pulls config edits, then execs the MEGA rubbish-bin
# emptier under its conda env. Runs its own loop; KeepAlive restarts it only if it dies.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin:$HOME/.local/bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GIT="$(command -v git || true)"
source "$SCRIPT_DIR/scripts/git_pull_locked.sh"
if [ -n "$GIT" ] && "$GIT" -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_pull_locked "$SCRIPT_DIR" || true
fi

CONDA_PY="/opt/homebrew/Caskroom/miniconda/base/envs/torrent_ingest_env/bin/python3"
if [ -x "$CONDA_PY" ]; then PYTHON="$CONDA_PY"; else PYTHON="$(command -v python3)"; fi

exec "$PYTHON" "$SCRIPT_DIR/mega_trash_daemon.py"
