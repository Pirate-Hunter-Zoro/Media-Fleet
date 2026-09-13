#!/bin/bash
set -euo pipefail

# Predictive pre-download daemon: keeps the local cache full of what you're likely to
# watch/read next (driven by Jellyfin watch-state + mediafs access log), bounded by the
# storage budget. Reads instant from disk; on-demand streaming is only the cold-miss path.
PROJECT_DIR="/Users/mikeyferguson/Developer/Media-Fleet/Media-Syncer"
PYTHON_EXEC="/opt/homebrew/Caskroom/miniconda/base/envs/media_sync_env/bin/python"
MODULE_PATH="scripts.predownload"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin:$HOME/.local/bin"

[ -x "$PYTHON_EXEC" ] || { echo "Python not found: $PYTHON_EXEC"; exit 1; }
[ -d "$PROJECT_DIR/scripts" ] || { echo "Scripts directory not found in $PROJECT_DIR"; exit 1; }

cd "$PROJECT_DIR"
exec "$PYTHON_EXEC" -m "$MODULE_PATH"
