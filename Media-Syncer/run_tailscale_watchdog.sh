#!/bin/bash
set -euo pipefail

# Tailscale watchdog: relaunches Tailscale when the 100.x CGNAT address disappears. Without it
# a dead network extension idles Torrent-Ingest indefinitely (qBittorrent is bound to that
# address) and stalls every MEGA transfer.
PROJECT_DIR="/Users/mikeyferguson/Developer/Media-Fleet/Media-Syncer"
PYTHON_EXEC="/opt/homebrew/Caskroom/miniconda/base/envs/media_sync_env/bin/python"
MODULE_PATH="scripts.tailscale_watchdog"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin:$HOME/.local/bin"

[ -x "$PYTHON_EXEC" ] || { echo "Python not found: $PYTHON_EXEC"; exit 1; }
[ -d "$PROJECT_DIR/scripts" ] || { echo "Scripts directory not found in $PROJECT_DIR"; exit 1; }

cd "$PROJECT_DIR"
exec "$PYTHON_EXEC" -m "$MODULE_PATH"
