#!/bin/bash
# Install + start Torrent-Ingest as a launchd background daemon.
#   1. verify tools
#   2. build the conda env and install deps
#   3. enable qBittorrent's WebUI (localhost, no auth) on the configured port
#   4. install and load ALL of this repo's launch agents (see the AGENTS list below)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONDA="/opt/homebrew/Caskroom/miniconda/base/bin/conda"
ENV_NAME="torrent_ingest_env"
ENV_PY="/opt/homebrew/Caskroom/miniconda/base/envs/$ENV_NAME/bin/python3"
WEBUI_PORT="8090"

echo "--- Torrent-Ingest setup ---"

echo "--> Checking tools..."
for tool in git python3 /usr/bin/brctl /opt/homebrew/bin/tailscale; do
    if ! command -v "$tool" >/dev/null 2>&1 && [ ! -x "$tool" ]; then
        echo "    Missing: $tool"; exit 1
    fi
done
[ -s "$HOME/.config/api-keys/openrouter_key" ] || echo "    WARNING: no OpenRouter key at ~/.config/api-keys/openrouter_key (every identify/judge/heal run needs it)."
[ -d "/Applications/qBittorrent.app" ] || { echo "    Missing qBittorrent.app"; exit 1; }
[ -x "$CONDA" ] || { echo "    Missing conda at $CONDA"; exit 1; }
echo "    Tools present."

echo "--> Creating conda env '$ENV_NAME' (Python 3.11) if needed..."
if ! "$CONDA" env list | grep -qE "/${ENV_NAME}$"; then
    "$CONDA" create -y -n "$ENV_NAME" python=3.11
fi
echo "--> Installing Python deps..."
"$ENV_PY" -m pip install --quiet --upgrade qbittorrent-api guessit requests

echo "--> Enabling qBittorrent WebUI on 127.0.0.1:$WEBUI_PORT (localhost auth bypassed)..."
if osascript -e 'application "qBittorrent" is running' 2>/dev/null | grep -q true; then
    echo "    Quitting qBittorrent to edit its config..."
    osascript -e 'quit app "qBittorrent"' 2>/dev/null || pkill -x qbittorrent || true
    for _ in $(seq 1 20); do
        pgrep -x qbittorrent >/dev/null 2>&1 || break
        sleep 0.5
    done
fi
"$ENV_PY" "$SCRIPT_DIR/scripts/enable_webui.py"
echo "    Relaunching qBittorrent..."
open -b org.qbittorrent.qBittorrent
echo "    Waiting for WebUI..."
for _ in $(seq 1 30); do
    if nc -z -w1 127.0.0.1 "$WEBUI_PORT" 2>/dev/null; then echo "    WebUI up."; break; fi
    sleep 1
done

echo "--> Making scripts executable..."
chmod +x "$SCRIPT_DIR"/run_*.sh "$SCRIPT_DIR"/scripts/*.sh "$SCRIPT_DIR/ingest.py" \
         "$SCRIPT_DIR/scripts/enable_webui.py"

# EVERY LaunchAgent this repo owns. This used to install three of them (torrentingest, getcomics,
# driveingest) and silently leave the other seven to be loaded by hand -- which is how
# `playlistautobuild` came to be documented as "run by launchd every 3h" while not actually being
# loaded on the box at all. If you add a plist to this repo, add its label here.
#
# Load order matters in one place: librarysupervisor is the SOLE launcher of Jellyfin/YacReader and
# stops them whenever the mediafs mount is not ready, so it goes last -- after the daemons whose
# work it gates on are already up.
AGENTS=(
    torrentingest        # the ingest state machine
    torrentreap          # queue-only remote deletion reaper
    torrentmetadata      # nightly .nfo/artwork backup to the metadata-backup remote
    directingest         # loose-file (comic/novel/video) ingester
    directingestbridge   # drains iCloud Torrents/DirectIngest into the local watch folder
    driveingest          # external-drive auto-organizer
    jellyfindbguardian   # continuous Jellyfin SQLite watcher (verified backups + auto-heal)
    mediadoctor          # library health daemon; writes library_health.txt
    fleethealth          # human-attention watchdog; writes fleet_health.txt (AI key, disk, MEGA)
    onepacethumbs        # generates episode thumbnails for shows with no image provider (One Pace)
    playlistcurator      # universal curated-playlist builder
    playlistautobuild    # session-independent playlist backfill (every 3h, no-ops when done)
    chapterreconcile     # 6-hourly manga chapter/volume reconcile (cached volume maps)
    gdrivesupervisor     # keeps the Google Drive app up so light novels can be placed
    librarysupervisor    # starts/stops Jellyfin + YacReader on mount readiness -- keep LAST
)

echo "--> Installing ${#AGENTS[@]} launch agents..."
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
UID_NUM="$(id -u)"
for svc in "${AGENTS[@]}"; do
    src="$SCRIPT_DIR/com.mikeyferguson.${svc}.plist"
    [ -f "$src" ] || { echo "    Error: missing plist $src"; exit 1; }
    cp "$src" "$HOME/Library/LaunchAgents/"
done
# bootout/bootstrap rather than the deprecated load/unload: on current macOS `launchctl load` can
# report success while doing nothing, which is precisely how an agent ends up silently uninstalled.
# No `launchctl start` -- these plists set RunAtLoad, so bootstrap starts them, and forcing `start`
# would also fire the interval/calendar jobs (torrentmetadata, playlistautobuild) off-schedule.
for svc in "${AGENTS[@]}"; do
    label="com.mikeyferguson.${svc}"
    launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null || true
    if launchctl bootstrap "gui/${UID_NUM}" "$HOME/Library/LaunchAgents/${label}.plist" 2>/dev/null; then
        echo "    loaded  ${label}"
    else
        echo "    FAILED  ${label} (check ~/Library/Logs)"
    fi
done

echo "--> Status (every agent should show a PID or a 0 exit):"
for svc in "${AGENTS[@]}"; do
    launchctl list | grep "com.mikeyferguson.${svc}$" || echo "    MISSING: com.mikeyferguson.${svc}"
done

# The plan API (library.validate_plan/apply_plan/verify_applied) has an out-of-repo
# caller: the YouTube ingest builds plans and hands them straight to it. A change here
# breaks that repo silently, so the contract is exercised for real at install time as well
# as at daemon start. Reported, never fatal -- torrents must ingest regardless.
# Run under the env python the daemons use, falling back to system python3 (contract.py
# imports only config/library/playlist, none of which need the env's extra packages).
echo "--> Plan API contract (what the YouTube ingest depends on):"
CONTRACT_PY="$ENV_PY"
[ -x "$CONTRACT_PY" ] || CONTRACT_PY="$(command -v python3)"
if ! "$CONTRACT_PY" "$SCRIPT_DIR/contract.py" 2>&1 | sed 's/^/    /'; then
    echo "    ^^ the YouTube ingest will refuse to place anything until this is fixed."
fi

echo "--- Setup complete. Drop .torrent files into the iCloud Torrents folder. ---"
echo "Engine log: $SCRIPT_DIR/torrent_ingest.log"
echo "Decisions:  $SCRIPT_DIR/state/decisions.log"
echo "launchd log: ~/Library/Logs/TorrentIngest.log / .err"
