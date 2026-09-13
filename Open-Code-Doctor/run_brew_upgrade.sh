#!/bin/bash
# Launcher invoked by launchd. Pulls any change to the schedule, then execs the upgrader.
#
# Deliberately /usr/bin/python3, not the brew one: this job upgrades Homebrew's python,
# and the interpreter running the script should not be the file being replaced.
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

GIT="$(command -v git || true)"
# Serialized: every launcher in the monorepo pulls the ONE working tree, so a bare pull
# here races the others on .git/FETCH_HEAD ("Cannot fast-forward to multiple branches").
. "$SCRIPT_DIR/scripts/git_pull_locked.sh"
if [ -n "$GIT" ] && "$GIT" -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_pull_locked "$SCRIPT_DIR" >/dev/null 2>&1 || true
fi

exec /usr/bin/python3 "$SCRIPT_DIR/brew_upgrade.py"
