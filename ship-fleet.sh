#!/usr/bin/env bash
# ===========================================================================
#  ship-fleet.sh -- commit, push and restart the WHOLE fleet, in a safe order.
#
#      bash ~/Developer/Media-Fleet/ship-fleet.sh "commit message"
#      bash ~/Developer/Media-Fleet/ship-fleet.sh --restart-only
#
#  Each repository already has `scripts/ship.sh`, which ships that repo and
#  bounces the daemons that run its code. That is the right tool for a change
#  confined to one repo. It is the wrong tool for a change that spans several,
#  because it restarts each repo's daemons the moment that repo is pushed --
#  so the fleet spends the middle of the deploy running a MIX of old and new
#  modules that were never tested together. Torrent-Ingest's placement rules
#  and Media-Syncer's replication rules are exactly that kind of pair.
#
#  So this pushes everything FIRST and restarts everything AFTER, and it
#  restarts nothing at all if any push failed -- the daemons staying on the old
#  code is the safe outcome when the new code is not saved anywhere.
#
#  Commits are authored by whoever `git config user.name` says. No trailers, no
#  co-authors, no attribution to any assistant: the work is the repository
#  owner's and the history should say only that.
# ===========================================================================
set -uo pipefail

# The Developer root. This script lives AT that root, and since the monorepo
# (2026-09-13) that root is the single repository every project lives in -- so the
# deploy tool IS tracked, at the top of the thing it deploys, which is the one place it
# cannot be confused for a single project's property. (Before the merge it was deliberately
# untracked, because owning it from any one repo was a risk.)
#
# DEV is simply this file's own directory. The symlink walk is kept because it costs
# nothing and keeps the script correct if it is ever reached through one again.
_self="${BASH_SOURCE[0]}"
while [ -L "$_self" ]; do _self="$(readlink "$_self")"; case "$_self" in /*) ;; *) _self="$HOME/Developer/Media-Fleet/$_self";; esac; done
DEV="$(cd "$(dirname "$_self")" && pwd)"
if [ ! -d "$DEV/Torrent-Ingest" ]; then
  echo "ship-fleet: cannot locate the Developer root (tried $DEV)" >&2
  exit 1
fi
# There is ONE repository now. The named sub-directories are projects inside it, not
# repositories; a push is a single commit at the root.

# Every launchd label the fleet runs, restarted after all pushes land. Ordered
# so the things others depend on come back first: the mount and the syncer
# before the daemons that read and write through them.
# `torrentsearcher` is NOT here because it no longer exists: torrent and comic discovery
# was removed from the fleet on 2026-09-10, repo and all. The bootstrap fallback below
# searches ~/Developer/Media-Fleet/*/ for a matching plist, so there is nothing left for it to revive.
#
# `fleetdoctor` IS here now. It carries KeepAlive and is a persistent daemon, but it was
# missing from this list for weeks -- so every deploy left it running the pre-deploy code
# and the documented deploy command silently did not deploy it (§4.30d). That is the whole
# reason this list is dangerous: it is hand-maintained, and an omission is invisible.
#
# A label with a dot in it is used verbatim; anything else is prefixed with
# `com.mikeyferguson.`, which is what nearly every job here is called.
LABELS=(
  mediafs mediasync predownload torrentreap
  torrentingest directingest driveingest
  titlescout youtubesync
  librarysupervisor gdrivesupervisor megasupervisor megatrash
  jellyfindbguardian mediadoctor fleethealth fleetdoctor onepacethumbs
  tailscalewatchdog mediasyncwatchdog
  opencodedoctor
)

# Periodic one-shots. Same fleet, different verb: these are checked for being LOADED and
# bootstrapped if they are not, but never kickstarted.
#
# `launchctl kickstart -k` does not "restart" a scheduled job, it FIRES it -- so putting
# these in LABELS would mean every deploy also starts a full metadata backup, an
# off-schedule janitor sweep, a playlist rebuild and a multi-gigabyte brew upgrade, all
# at once, as a side effect of a push. None of them need it either: every launcher does
# its own `git pull --ff-only` before exec, so the next scheduled tick is already on the
# pushed code. That is the whole difference between the two lists -- membership of the
# fleet is identical.
PERIODIC=(
  mediasyncstatebackup                        # hourly inventory/state backup
  playlistcurator playlistautobuild           # 3-hourly playlist builds
  artifactjanitor                             # 03:00 + 15:00 failed-artifact sweep
  torrentmetadata                             # nightly .nfo/artwork backup
  purgesweeper                                # purge sweeper (see the note below)
  brewupgrade                                 # daily brew update && brew upgrade
  com.mikey.jellyfin-db-backup                # 04:00 Jellyfin DB backup (not a com.mikeyferguson label)
)

# EXCLUDED ON PURPOSE, and not an oversight in either list:
#   `splittunnel`  -- it rewrites the routing table, so it is a root LaunchDaemon in
#     /Library/LaunchDaemons (system domain), where it is installed and running. It must
#     NEVER be bootstrapped into the gui domain: without root the script cannot write
#     routes, and Media-Syncer's README documents the resulting failure exactly -- the
#     daemon dies while the routes still *look* fine. Install it by hand with the sudo
#     steps in Media-Syncer/scripts/split_tunnel.sh (lines 57-63); Media-Syncer's startup.sh reports
#     whether it is active. A user-domain copy of its plist also sits in the Media-Syncer
#     repo, which is why it looks like a missing agent from here. It is not.
#   `torrentsearcher` -- removed from the fleet entirely on 2026-09-10, along with its
#     repo. Nothing searches and nothing drops; the owner hand-drops every `.torrent`.
#
# TWO OF THESE HAVE NO PLIST IN THE REPO: `purgesweeper` and `com.mikey.jellyfin-db-backup`
# exist only in ~/Library/LaunchAgents, so they are registered here and resolve from
# there. That is enough to keep them running and enough to notice when they stop, but it
# is not a backup: boot one out and delete that copy and it is gone, because there is no
# tracked original to reinstall from. purgesweeper runs
# Torrent-Ingest/scripts/purge_sweeper.py --apply and its plist belongs in Torrent-Ingest
# next to the script it launches; the Jellyfin backup's plist and script both live under
# ~/Library/Application Support/jellyfin-backups. Check them in when you next touch them;
# there is one repository to check them into now.

RESTART_ONLY=0
MSG="fleet updates"
if [ "${1:-}" = "--restart-only" ]; then RESTART_ONLY=1; else MSG="${1:-fleet updates}"; fi

fail=0
if [ $RESTART_ONLY -eq 0 ]; then
  echo "=============== PUSH ==============="
  echo
  echo "-- $(basename "$DEV") (monorepo)"
  bash "$DEV/scripts/save-and-push.sh" "$MSG" || fail=1

  if [ $fail -ne 0 ]; then
    echo
    echo "at least one push failed, so NOTHING has been restarted." >&2
    echo "The fleet is still on the old code, which is where it should be while" >&2
    echo "the new code is not saved anywhere." >&2
    exit 1
  fi
fi

echo
echo "============== RESTART ============="
# A dotted entry is a full label; a bare one is one of ours.
qualify() { case "$1" in *.*) printf '%s' "$1";; *) printf 'com.mikeyferguson.%s' "$1";; esac; }

# A DRAINING REAPER IS NEVER RESTARTED (§4.6). `purge_batch` consumes its `.processing`
# file as ONE batch and unlinks it only at the end, and the expensive part is a MEGA
# account probe that a restart throws away -- a drain 34 h in went back to the start.
# The hand-off has said "DO NOT RESTART IT" in capitals for days while THIS script has
# had `torrentreap` in LABELS the whole time, so the documented deploy command was one
# `ship-fleet.sh` away from destroying the thing the whole queue is waiting on. §4.12: a
# rule that has to be remembered every time has not been fixed, only written down. The
# reaper is left alone here and picks the new code up when the batch ends and launchd
# restarts it -- its launcher pulls before exec, like every other one.
#
# It FAILS CLOSED, which is the whole point: the question is "may I restart the reaper?",
# and the only answer that permits it is a working `pgrep` that positively reports no
# reaper. A missing or broken `pgrep` returns "unknown", and unknown must not mean yes --
# a guard whose failure mode is to do the destructive thing anyway is not a guard.
# `reap_running` echoes yes / no / unknown and never lets an error read as "no".
reap_running() {
  local out rc
  command -v pgrep >/dev/null 2>&1 || { printf 'unknown'; return; }
  out="$(pgrep -f 'Torrent-Ingest/reap.py' 2>/dev/null)"; rc=$?
  case "$rc" in
    0) [ -n "$out" ] && printf 'yes' || printf 'unknown' ;;
    1) printf 'no' ;;                 # pgrep's documented "no process matched"
    *) printf 'unknown' ;;            # any other exit is an error, not an absence
  esac
}

missing=0
for label in "${LABELS[@]}"; do
  full="$(qualify "$label")"
  if [ "$full" = "com.mikeyferguson.torrentreap" ]; then
    state="$(reap_running)"
    if [ "$state" != "no" ]; then
      if [ "$state" = "yes" ]; then
        why="pid $(pgrep -f 'Torrent-Ingest/reap.py' | head -1) is mid-drain"
      else
        why="could not tell whether it is running, so it is left alone"
      fi
      echo "  SKIPPED torrentreap        $why (§4.6). It picks up the new code when the"
      echo "                             batch ends; restarting it here would discard the"
      echo "                             MEGA probe and start the drain over."
      continue
    fi
  fi
  # `kickstart` restarts a LOADED job. A job that was booted OUT -- which is what a
  # migration or a hand-run does -- is not loaded at all, and kickstart cannot revive it.
  # Skipping those was how a restart pass reported success while leaving mediasync,
  # torrentingest, directingest and torrentsearcher down. Bootstrap them instead.
  # EXACT label match, not a substring one. `grep -q com.mikeyferguson.mediasync` also
  # matches `...mediasyncwatchdog` and `...mediasyncstatebackup`, so mediasync was scored
  # as loaded when it was not, and the pass tried to kickstart a service that did not
  # exist and reported a failure it could not explain.
  if ! launchctl list | awk -v L="$full" '$3==L {f=1} END{exit !f}'; then
    plist="$HOME/Library/LaunchAgents/$full.plist"
    [ -f "$plist" ] || plist="$(ls "$DEV"/*/"$full.plist" 2>/dev/null | head -1)"
    if [ -n "$plist" ] && [ -f "$plist" ] \
       && launchctl bootstrap gui/501 "$plist" >/dev/null 2>&1; then
      printf "  bootstrapped %-19s" "$label"
      sleep 1
      pid="$(launchctl list | awk -v l="$full" '$3==l {print $1}')"
      echo "pid ${pid:-none}"
    else
      echo "  $label: not loaded and could not be bootstrapped" >&2
      missing=1
    fi
    continue
  fi
  if launchctl kickstart -k "gui/501/$full" >/dev/null 2>&1; then
    printf "  restarted %-22s" "$label"
    # A daemon that comes back with no PID did not come back.
    sleep 1
    pid="$(launchctl list | awk -v l="$full" '$3==l {print $1}')"
    if [ "$pid" = "-" ] || [ -z "$pid" ]; then
      echo "(loaded, not currently running -- normal for a periodic job)"
    else
      echo "pid $pid"
    fi
  else
    echo "  FAILED to restart $label" >&2
    missing=1
  fi
done

# The periodic jobs: loaded is the whole requirement. A job showing "-" for its PID is
# not down, it is between runs, which is what a scheduled one-shot looks like all day.
for label in "${PERIODIC[@]}"; do
  full="$(qualify "$label")"
  if launchctl list | awk -v L="$full" '$3==L {f=1} END{exit !f}'; then
    printf "  scheduled %-22s (loaded; next tick runs the pushed code)\n" "$label"
    continue
  fi
  plist="$HOME/Library/LaunchAgents/$full.plist"
  [ -f "$plist" ] || plist="$(ls "$DEV"/*/"$full.plist" 2>/dev/null | head -1)"
  if [ -n "$plist" ] && [ -f "$plist" ] && launchctl bootstrap gui/501 "$plist" >/dev/null 2>&1; then
    printf "  bootstrapped %-19s (was not loaded)\n" "$label"
  else
    echo "  $label: not loaded and could not be bootstrapped" >&2
    missing=1
  fi
done

echo
echo "Now confirm nothing is throwing. Tracebacks in the last 200 lines:"
for f in "$DEV"/Torrent-Ingest/torrent_ingest.log "$DEV"/Torrent-Ingest/direct_ingest.log \
         "$DEV"/Media-Syncer/media_sync.log; do
  [ -f "$f" ] || continue
  n="$(tail -200 "$f" | grep -c 'Traceback (most recent call last)')"
  printf "  %-52s %s\n" "$(basename "$f")" "$n traceback(s)"
done

[ $missing -eq 0 ] && echo && echo "fleet shipped." || exit 1
