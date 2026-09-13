#!/bin/bash
# Stop the daily brew-upgrade daemon (without uninstalling the launch agent).
set -euo pipefail

PLIST_DST="$HOME/Library/LaunchAgents/com.mikeyferguson.brewupgrade.plist"

echo "--> Stopping daemon..."
launchctl stop com.mikeyferguson.brewupgrade 2>/dev/null || true
if [ -f "$PLIST_DST" ]; then
    launchctl bootout "gui/$(id -u)/com.mikeyferguson.brewupgrade" 2>/dev/null \
        || launchctl unload "$PLIST_DST" 2>/dev/null || true
fi
# Leave any brew mid-upgrade alone -- killing it half-linked is worse than letting it
# finish. Only the wrapper is stopped; the lock it holds goes stale on its own.
pkill -f "Open-Code-Doctor/brew_upgrade.py" 2>/dev/null || true
echo "    Stopped. Re-enable with: launchctl bootstrap gui/$(id -u) \"$PLIST_DST\""
