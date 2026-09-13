#!/usr/bin/env bash
# ===========================================================================
#  ship.sh -- commit and push this repository, then put its daemons on the new
#  code.
#
#      bash scripts/ship.sh ["commit message"]
#
#  A launchd daemon reads its Python once, when it starts. Pushing a change and
#  walking away therefore does nothing to the fleet: the repository looks
#  updated while every running daemon is still executing the old module. That is
#  invisible from the outside and it is exactly how a "fixed" bug goes on
#  happening for another day. So shipping is one act, not two: commit, push, and
#  bounce what is running.
#
#  Each launcher does a serialized `git pull --ff-only` before exec, so the
#  restart is what actually pulls the pushed commit onto the running daemon.
#
#  The commit is authored by whoever `git config user.name` says. No trailers,
#  no co-authors, no attribution to any assistant -- the work belongs to the
#  person whose repository this is and the history should say only that.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE" || { echo "cannot enter $HERE" >&2; exit 1; }

MSG="${1:-fleet updates}"

# The launchd labels whose processes run this repository's code.
LABELS=(mediasync predownload mediafs)

echo "== $(basename "$HERE") =="
bash "$HERE/scripts/save-and-push.sh" "$MSG"
status=$?
if [ $status -ne 0 ]; then
  echo
  echo "push did not succeed, so nothing has been restarted." >&2
  echo "The daemons are still on the old code, which is the safe place for them" >&2
  echo "to be while the change is not saved anywhere." >&2
  exit $status
fi

echo
failed=0
for label in "${LABELS[@]}"; do
  full="com.mikeyferguson.$label"
  # `launchctl print`, not `launchctl list | grep -q`. The pipeline version is wrong
  # under `set -o pipefail` in the one case that matters: grep -q exits the moment it
  # matches, launchctl dies of SIGPIPE (141), pipefail reports 141 for the pipeline, and
  # the test reads a LOADED daemon as "not loaded". It then skips the restart and still
  # prints "shipped." -- the exact silent no-op this script exists to prevent.
  if ! launchctl print "gui/$(id -u)/$full" >/dev/null 2>&1; then
    echo "  $label is not loaded; skipping (start it with launchctl bootstrap)"
    continue
  fi
  if launchctl kickstart -k "gui/$(id -u)/$full" >/dev/null 2>&1; then
    echo "  restarted $label"
  else
    echo "  FAILED to restart $label" >&2
    failed=1
  fi
done

if [ $failed -ne 0 ]; then
  echo
  echo "at least one daemon did not come back; check ~/Library/Logs and the" >&2
  echo "repo-root *.log before trusting the change is live." >&2
  exit 1
fi

echo
echo "shipped."
