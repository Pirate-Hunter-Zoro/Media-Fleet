#!/usr/bin/env bash
# ===========================================================================
#  scripts/ship.sh -- commit, push and restart the whole fleet.
#
#      bash scripts/ship.sh ["commit message"]
#      bash scripts/ship.sh --restart-only
#
#  One working tree, one deploy path: the root `ship-fleet.sh`. This is the
#  canonical entry point; the per-project scripts restart only their own daemons.
#
#  No trailers, no co-authors, no attribution to any assistant: the work is the
#  repository owner's and the history should say only that.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec bash "$HERE/ship-fleet.sh" "$@"
