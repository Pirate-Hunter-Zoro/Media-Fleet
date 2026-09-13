#!/bin/bash
set -euo pipefail

# The path to the root of your project, containing the 'scripts' directory.
PROJECT_DIR="/Users/mikeyferguson/Developer/Media-Syncer"
PYTHON_EXEC="/opt/homebrew/Caskroom/miniconda/base/envs/media_sync_env/bin/python"

# The module path to execute.
MODULE_PATH="scripts.media_sync"

[ -x "$PYTHON_EXEC" ] || { echo "Python not found: $PYTHON_EXEC"; exit 1; }
[ -d "$PROJECT_DIR/scripts" ] || { echo "Scripts directory not found in $PROJECT_DIR"; exit 1; }

# Change to the project directory so Python can find the module.
cd "$PROJECT_DIR"

# Execute the script as a module.
exec "$PYTHON_EXEC" -m "$MODULE_PATH"