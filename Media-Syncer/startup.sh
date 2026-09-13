#!/bin/bash
set -euo pipefail

echo "--- Beginning the Media Syncer Setup Ritual ---"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UID_NUM="$(id -u)"

# Every LaunchAgent this repo owns, in load order. mediafs goes FIRST so the mount is coming up
# while the rest load (the other daemons all tolerate a not-yet-ready mount and retry, so this is
# preference rather than a hard dependency).
#
# `com.mikeyferguson.splittunnel` is deliberately ABSENT. It is a root LaunchDaemon
# (/Library/LaunchDaemons, system domain) because it rewrites the routing table -- it is NOT a user
# LaunchAgent and must never be bootstrapped into the gui domain. Install it by hand with the sudo
# steps at the top of scripts/split_tunnel.sh; the reminder at the end of this script
# tells you whether it is currently active.
AGENTS=(
  mediafs                 # the virtual filesystem: mounts ~/MediaLibrary over ~/Media + the pool
  mediasync               # the sync loop itself (no KeepAlive by design -- see README)
  mediasyncwatchdog       # relaunches mediasync if it dies while NOT paused by the reaper
  predownload             # predictive pre-download / the SSD + drive space manager
  tailscalewatchdog       # relaunches Tailscale; without it a dead tunnel idles Torrent-Ingest
  mediasyncstatebackup    # hourly sync_state.json backup
)

echo "--> Placing system artifacts..."
mkdir -p "$HOME/.config/rclone" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$SCRIPT_DIR/rclone.conf" "$HOME/.config/rclone/rclone.conf"
chmod +x "$SCRIPT_DIR"/run_*.sh
for svc in "${AGENTS[@]}"; do
  src="$SCRIPT_DIR/com.mikeyferguson.${svc}.plist"
  [ -f "$src" ] || { echo "    Error: missing plist $src"; exit 1; }
  cp "$src" "$HOME/Library/LaunchAgents/"
done
echo "    System artifacts placed (${#AGENTS[@]} agents)."


echo "--> Preparing the Conda environment..."
CONDA_PATH="/opt/homebrew/bin/conda"
ENV_NAME="media_sync_env"
PYTHON_VERSION="3.11"

[ -x "$CONDA_PATH" ] || { echo "    Error: Conda not found at '$CONDA_PATH'"; exit 1; }

if ! "$CONDA_PATH" env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  echo "    Conda environment '$ENV_NAME' not found. Forging it now..."
  "$CONDA_PATH" create --name "$ENV_NAME" python="$PYTHON_VERSION" -y
else
  echo "    Conda environment '$ENV_NAME' already exists."
fi

# bootout/bootstrap, not the deprecated load/unload pair: on current macOS `launchctl load` can
# report success while doing nothing, which is exactly the failure that leaves an agent silently
# uninstalled. No `launchctl start` afterwards -- every plist here sets RunAtLoad, so bootstrap
# already starts it, and `start`/`kickstart` would additionally force the interval-driven jobs
# (mediasyncstatebackup) to fire immediately whether or not they were due.
echo "--> Awakening the background services..."
for svc in "${AGENTS[@]}"; do
  label="com.mikeyferguson.${svc}"
  launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null || true
  if launchctl bootstrap "gui/${UID_NUM}" "$HOME/Library/LaunchAgents/${label}.plist" 2>/dev/null; then
    echo "    loaded  ${label}"
  else
    echo "    FAILED  ${label} (already loaded, or check ~/Library/Logs)"
  fi
done

echo "--> Allowing the spirit to awaken (waiting 5 seconds)..."
sleep 5

echo "--> Displaying results..."
echo "--- AGENT STATUS (every agent should show a PID or a 0 exit) ---"
for svc in "${AGENTS[@]}"; do
  launchctl list | grep "com.mikeyferguson.${svc}$" || echo "    MISSING: com.mikeyferguson.${svc}"
done

echo "--- LAUNCHD SERVICE ERROR LOG (should be empty) ---"
tail "$HOME/Library/Logs/MediaSync.err" || true

echo "--- LAUNCHD SERVICE OUTPUT LOG (should show execution) ---"
tail "$HOME/Library/Logs/MediaSync.log" || true

echo "--- ENGINE'S OWN LOG (should show initialization) ---"
tail "$SCRIPT_DIR/media_sync.log" || true

echo "--- CHECKING FOR RUNNING PROCESS (should show a number) ---"
pgrep -f "scripts.media_sync" || true

echo "--- SPLIT TUNNEL (root LaunchDaemon; not installed by this script) ---"
if launchctl print system/com.mikeyferguson.splittunnel >/dev/null 2>&1; then
  echo "    active."
else
  echo "    NOT active. Optional -- the ROTATE_MIN_INTERVAL_SEC throttle already makes AI API"
  echo "    errors rare. To get full immunity, follow the sudo steps at the top of"
  echo "    scripts/split_tunnel.sh (it installs into /Library/LaunchDaemons)."
fi

echo "--- SETUP COMPLETE ---"
