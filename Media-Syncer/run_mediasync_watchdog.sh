#!/bin/bash
set -euo pipefail

# Watchdog for the media_sync daemon. media_sync deliberately has no KeepAlive (the reaper
# stops it to purge deletions), so this relaunches it only when it is down AND not paused.
PROJECT_DIR="/Users/mikeyferguson/Developer/Media-Fleet/Media-Syncer"
PYTHON_EXEC="/opt/homebrew/Caskroom/miniconda/base/envs/media_sync_env/bin/python"
MODULE_PATH="scripts.mediasync_watchdog"

[ -x "$PYTHON_EXEC" ] || { echo "Python not found: $PYTHON_EXEC"; exit 1; }
[ -d "$PROJECT_DIR/scripts" ] || { echo "Scripts directory not found in $PROJECT_DIR"; exit 1; }

cd "$PROJECT_DIR"
exec "$PYTHON_EXEC" -m "$MODULE_PATH"
