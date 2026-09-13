#!/bin/bash
# Scheduled by launchd (com.mikeyferguson.mediasyncstatebackup): back up the
# load-bearing state files to the metadata-backup MEGA remote. One-shot, not a loop.
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONDA_PY="/opt/homebrew/Caskroom/miniconda/base/envs/media_sync_env/bin/python3"
if [ -x "$CONDA_PY" ]; then PYTHON="$CONDA_PY"; else PYTHON="$(command -v python3)"; fi

exec "$PYTHON" -m scripts.backup_state
