#!/bin/bash
# Install + start the YouTube ingest as a launchd background agent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.mikeyferguson.youtubesync.plist"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
TORRENT_INGEST_DIR="${TORRENT_INGEST_DIR:-$HOME/Developer/Media-Fleet/Torrent-Ingest}"
COOKIES_FILE="${YOUTUBE_COOKIES_FILE:-$HOME/.config/youtube-sync/cookies.txt}"

echo "--- YouTube ingest setup ---"

echo "--> Checking tools..."
for tool in yt-dlp ffmpeg ffprobe python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "    Missing: $tool. Install via Homebrew (brew install yt-dlp ffmpeg)."
        exit 1
    fi
done
if [ ! -s "$HOME/.config/api-keys/openrouter_key" ]; then
    echo "    Missing: the OpenRouter key at ~/.config/api-keys/openrouter_key (the identify"
    echo "             and soundtrack-classification steps both need it)."
    exit 1
fi
echo "    All tools present."

# This repo is a source for Torrent-Ingest's pipeline, not a standalone downloader:
# placement, validation and the locked-.nfo writers are imported from it.
echo "--> Checking the Torrent-Ingest pipeline..."
if [ ! -f "$TORRENT_INGEST_DIR/library.py" ]; then
    echo "    Not found at $TORRENT_INGEST_DIR."
    echo "    Set TORRENT_INGEST_DIR to the Torrent-Ingest repo and re-run."
    exit 1
fi
echo "    Found at $TORRENT_INGEST_DIR."

echo "--> Checking YouTube authentication..."
if [ -s "$COOKIES_FILE" ]; then
    echo "    Cookies present at $COOKIES_FILE."
else
    echo "    NO COOKIES at $COOKIES_FILE."
    echo "    Discovery needs your account to see which playlists you have saved."
    echo "    The agent will install and run, log an alert, and ingest nothing until"
    echo "    you export them. See the README, 'What you need to do'."
fi

echo "--> Preparing state dir..."
mkdir -p "$SCRIPT_DIR/state" "$(dirname "$COOKIES_FILE")"
chmod 700 "$(dirname "$COOKIES_FILE")" 2>/dev/null || true

echo "--> Making scripts executable..."
chmod +x "$SCRIPT_DIR/run_youtube_sync.sh" "$SCRIPT_DIR/youtube_sync.py"

echo "--> Installing launch agent..."
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$PLIST_SRC" "$PLIST_DST"

echo "--> (Re)loading agent..."
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
launchctl start com.mikeyferguson.youtubesync || true

echo "--> Status:"
launchctl list | grep youtubesync || echo "    (not yet listed; give it a moment)"

echo "--- Setup complete. Runs now and every 5 minutes after. ---"
echo "Check state:  ./run_youtube_sync.sh --status"
echo "Logs:         ~/Library/Logs/YouTubeSync.log  and  ./youtube_sync.log"
