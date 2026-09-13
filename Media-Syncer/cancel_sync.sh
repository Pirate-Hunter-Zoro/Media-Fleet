#!/bin/bash
set -euo pipefail

# This pattern matches the way the media syncing script is executed.
PROCESS_PATTERN="bin/python -m scripts.media_sync"

echo "Beginning the ritual of banishment..."

echo "--> Hunting for the running engine process..."
PID=""

# Find the process using the module execution pattern.
if pgrep -f "$PROCESS_PATTERN" >/dev/null 2>&1; then
  PID=$(pgrep -f "$PROCESS_PATTERN")
fi

if [ -n "${PID:-}" ]; then
  echo "    Process found with PID $PID. Terminating it now."
  kill -9 "$PID" || true
else
  echo "    No running process found. Nothing to terminate."
fi