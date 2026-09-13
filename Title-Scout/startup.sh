#!/bin/bash
# Install the Title-Scout launch agent (one-time setup).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO/com.mikeyferguson.titlescout.plist"
DST="$HOME/Library/LaunchAgents/com.mikeyferguson.titlescout.plist"

chmod +x "$REPO/run_scout.sh" "$REPO/cancel_scout.sh"

mkdir -p "$HOME/Library/LaunchAgents"
cp "$SRC" "$DST"

launchctl unload "$DST" 2>/dev/null || true
launchctl load "$DST"

echo "Installed. Logs: ~/Library/Logs/TitleScout.log"
echo "Stop with: $REPO/cancel_scout.sh"
