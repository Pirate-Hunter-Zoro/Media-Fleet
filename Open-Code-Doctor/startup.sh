#!/bin/bash
# Install this repo's launch agents (one-time setup; safe to re-run).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UID_NUM="$(id -u)"

# EVERY LaunchAgent this repo owns. If you add a plist here, add its label to this list
# and to LABELS in scripts/ship.sh -- an agent that is only half-registered is one that
# looks installed in the README and is not running on the box.
AGENTS=(
    opencodedoctor   # every minute: keeps opencode + the `coder` command healthy
    brewupgrade      # hourly wake, gated to once a day: brew update && brew upgrade
)

chmod +x "$REPO"/run_*.sh "$REPO"/cancel_*.sh "$REPO/doctor.py" "$REPO/brew_upgrade.py"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
for svc in "${AGENTS[@]}"; do
    src="$REPO/com.mikeyferguson.${svc}.plist"
    [ -f "$src" ] || { echo "    Error: missing plist $src"; exit 1; }
    cp "$src" "$HOME/Library/LaunchAgents/"
done

# bootout/bootstrap rather than the deprecated load/unload: on current macOS
# `launchctl load` can report success while doing nothing at all.
for svc in "${AGENTS[@]}"; do
    label="com.mikeyferguson.${svc}"
    launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null || true
    if launchctl bootstrap "gui/${UID_NUM}" "$HOME/Library/LaunchAgents/${label}.plist" 2>/dev/null; then
        echo "    loaded  ${label}"
    else
        echo "    FAILED  ${label} (check ~/Library/Logs)"
    fi
done

echo
echo "Logs: ~/Library/Logs/OpenCodeDoctor.log, ~/Library/Logs/BrewUpgrade.log"
echo "Stop with: $REPO/cancel_doctor.sh / $REPO/cancel_brew_upgrade.sh"
