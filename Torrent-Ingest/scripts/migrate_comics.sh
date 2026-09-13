#!/usr/bin/env bash
# ===========================================================================
#  migrate_comics.sh -- move already-filed comics into the franchise layout,
#  with the daemons stopped around it.
#
#      bash scripts/migrate_comics.sh            # plan only, touches nothing
#      bash scripts/migrate_comics.sh --apply    # do it
#
#  The Python does the work (scripts/migrate_comic_franchises.py, which carries
#  the reasoning). This wrapper exists because the procedure AROUND it is the
#  part that gets forgotten, and forgetting it is what duplicates and
#  resurrects files:
#
#    * mediasync snapshots the remote index once at cycle start and holds it
#      for a multi-hour cycle, so moves made while it runs are invisible to it
#      and it will happily re-upload the new path and re-download the old one.
#      It has to be DOWN, not merely idle.
#    * directingest files new comics, so leaving it up races the migration for
#      the same folders.
#
#  Both are brought back at the end whatever happens, including on Ctrl-C.
# ===========================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="/opt/homebrew/Caskroom/miniconda/base/envs/torrent_ingest_env/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
DAEMONS=(mediasync directingest)

restart() {
  echo
  echo "-- bringing the daemons back"
  for d in "${DAEMONS[@]}"; do
    if launchctl kickstart -k "gui/501/com.mikeyferguson.$d" >/dev/null 2>&1; then
      echo "   restarted $d"
    else
      # kickstart fails when the job was booted OUT rather than merely stopped.
      plist="$HOME/Library/LaunchAgents/com.mikeyferguson.$d.plist"
      [ -f "$plist" ] || plist="$(ls "$HOME/Developer"/*/com.mikeyferguson."$d".plist 2>/dev/null | head -1)"
      if [ -n "$plist" ] && launchctl bootstrap gui/501 "$plist" >/dev/null 2>&1; then
        echo "   bootstrapped $d"
      else
        echo "   FAILED to restart $d -- do it by hand before walking away" >&2
      fi
    fi
  done
}

if [ "${1:-}" != "--apply" ]; then
  echo "== plan only (no daemon is stopped, nothing is touched) =="
  exec "$PY" "$HERE/scripts/migrate_comic_franchises.py"
fi

trap restart EXIT INT TERM

echo "-- stopping the daemons that touch the comic library"
for d in "${DAEMONS[@]}"; do
  launchctl bootout "gui/501/com.mikeyferguson.$d" 2>/dev/null \
    && echo "   stopped $d" || echo "   $d was not running"
done
sleep 3

echo
"$PY" "$HERE/scripts/migrate_comic_franchises.py" --apply
rc=$?
echo
if [ $rc -ne 0 ]; then
  echo "MIGRATION DID NOT FULLY SUCCEED (exit $rc)." >&2
  echo "It is idempotent -- an absent source counts as already moved -- so re-run" >&2
  echo "  bash scripts/migrate_comics.sh --apply" >&2
  echo "State keys were NOT rewritten while anything was outstanding." >&2
fi
exit $rc
