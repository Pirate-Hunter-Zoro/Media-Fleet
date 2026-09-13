#!/usr/bin/env bash
# ===========================================================================
#  scripts/ship.sh -- commit, push and restart the whole fleet.
#
#      bash scripts/ship.sh ["commit message"]
#      bash scripts/ship.sh --restart-only
#
#  Since the monorepo (2026-09-13) there is one working tree and one deploy
#  path: the root `ship-fleet.sh`. This wrapper exists so the old per-project
#  habit (`bash <project>/scripts/ship.sh`) has a canonical entry point. The
#  per-project scripts still work and restart only their own daemons.
#
#  No trailers, no co-authors, no attribution to any assistant: the work is the
#  repository owner's and the history should say only that.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec bash "$HERE/ship-fleet.sh" "$@"
