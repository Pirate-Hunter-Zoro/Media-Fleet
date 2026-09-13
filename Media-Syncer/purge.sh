#!/bin/bash
set -euo pipefail

echo "--- BEGINNING FINAL PURGE AND RE-BINDING ---"

cd "$HOME/Developer/Media-Syncer"

echo "\n--> Step 1: Banishing the old service..."
bash ./cancel_sync.sh

echo "\n--> Step 2: Ensuring the old territory is destroyed..."
/opt/homebrew/bin/conda env remove --name media_sync_env -y || true