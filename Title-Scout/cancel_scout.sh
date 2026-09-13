#!/bin/bash
# Stop the Title-Scout daemon (without uninstalling the launch agent).
set -euo pipefail

PLIST_DST="$HOME/Library/LaunchAgents/com.mikeyferguson.titlescout.plist"

echo "--> Stopping daemon..."
launchctl stop com.mikeyferguson.titlescout 2>/dev/null || true
if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
fi
pkill -f "Title-Scout/scout.py" 2>/dev/null || true
echo "    Stopped. Re-enable with: launchctl load \"$PLIST_DST\""
