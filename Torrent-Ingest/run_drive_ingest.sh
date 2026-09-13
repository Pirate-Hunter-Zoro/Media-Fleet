#!/bin/bash
# Launcher for the drive-ingest daemon (auto-organizes a newly-attached drive's pre-existing
# media into <drive>/Media/ via the AI identify pipeline). Own loop; launchd restarts on death.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
GIT="$(command -v git || true)"
# Serialized: five sibling launchers pull this same working tree seconds apart at
# bootstrap, and concurrent pulls corrupt FETCH_HEAD. See scripts/git_pull_locked.sh.
source "$SCRIPT_DIR/scripts/git_pull_locked.sh"
if [ -n "$GIT" ] && "$GIT" -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_pull_locked "$SCRIPT_DIR" || true
fi
CONDA_PY="/opt/homebrew/Caskroom/miniconda/base/envs/torrent_ingest_env/bin/python3"
if [ -x "$CONDA_PY" ]; then PYTHON="$CONDA_PY"; else PYTHON="$(command -v python3)"; fi
exec "$PYTHON" "$SCRIPT_DIR/drive_ingest.py"
