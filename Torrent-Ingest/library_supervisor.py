"""Library app supervisor.

Jellyfin and YacReader read their libraries from the mediafs mount at
`~/MediaLibrary`. At boot they race the mount (and the SSD under it): macOS's
"reopen apps at login" relaunches them before mediafs has mounted, so they come up
pointed at an EMPTY directory -- showing no shows/comics, and (worse) a Jellyfin
scan of an empty library can gut its own DB. login-item settings don't stop the
reopen, so this supervisor is the authority on when they may run.

Each cycle:
  * If the mount is NOT healthy (not mounted, or Shows/Movies/Comics empty) for a
    couple of polls -> STOP Jellyfin and YacReader, so neither serves nor scans an
    empty library. mediafs's own KeepAlive+watchdog brings the mount back.
  * If the mount IS healthy -> ensure both apps are running (start whatever is
    down). And sanity-check Jellyfin: if it's up but its episode count has
    collapsed (mount healthy yet library near-empty) for a couple of polls, its DB
    was gutted -- restore db_guardian's last-good backup and restart, preserving
    watch state. Separately, if Jellyfin is up but its authenticated API goes
    UNANSWERED for long enough (the hang where every api_key request times out
    while unauthenticated ones answer), restart it.

Runs as its own KeepAlive user-agent, a sibling of db_guardian (whose Jellyfin
control + verified backups it reuses).

    python3 library_supervisor.py            # daemon loop
    python3 library_supervisor.py --status   # print state and exit
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# This repo's own directory goes FIRST on sys.path. Torrent-Ingest and Torrent-Searcher
# both ship modules named `config.py`, `library.py` and `ingest.py`, and both repos are on
# `sys.path` in some processes -- so a bare `import config` resolves to whichever repo the
# launcher happened to put first. That is how `directingest` died at import on 2026-08-27,
# reading Torrent-Ingest's `config` through Torrent-Searcher's `ingest` (§5 item 4a). The
# pin makes the resolution a property of the FILE rather than of how it was launched.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import config
import db_guardian   # reuse jellyfin control + verified-backup restore
import yacreader_db  # index lock + app control (its writes cross the FUSE boundary)


def log(msg: str) -> None:
    line = f"[{config.log_stamp()}] {msg}"
    print(line, flush=True)
    try:
        config.rotate_log_if_large(config.SUPERVISOR_LOG_FILE)
        with config.SUPERVISOR_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def alert(msg: str) -> None:
    log("ALERT: " + msg)
    try:
        config.SUPERVISOR_ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with config.SUPERVISOR_ALERT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except OSError:
        pass
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        f"display notification {json.dumps(msg[:200])} with title "
                        f"{json.dumps('Library Supervisor')}"],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def acquire_lock():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = config.SUPERVISOR_LOCK_FILE.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Another supervisor holds the lock; exiting.")
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


# --- mount health ------------------------------------------------------------

def mount_is_mounted() -> bool:
    try:
        r = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f" {config.MEDIAFS_MOUNT} (" in r.stdout


def _content_check() -> bool:
    """Each library dir lists at least one entry. May be slow under heavy FUSE load."""
    for d in config.MEDIAFS_HEALTH_DIRS:
        p = config.MEDIAFS_MOUNT / d
        if not p.is_dir():
            return False
        with os.scandir(p) as it:
            if next(it, None) is None:
                return False
    return True


def mount_healthy() -> bool:
    """Healthy if mounted AND the library dirs are non-empty. The mounted check is
    fast (parses `mount`, no FUSE I/O) and catches the boot case. The content check
    runs with a hard timeout: if it doesn't finish in time -- the mount is busy
    under load, not down -- we do NOT demote to unhealthy (killing the apps for a
    slow readdir under a scan is exactly the bug this avoids). Only a fast, definite
    'empty dirs' result counts as unhealthy."""
    if not mount_is_mounted():
        return False
    result: list = [None]   # None = indeterminate (timed out / errored) -> trust mount

    def run():
        try:
            result[0] = _content_check()
        except OSError:
            result[0] = None

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(config.MEDIAFS_CONTENT_TIMEOUT_SEC)
    return True if result[0] is None else result[0]


# --- app control -------------------------------------------------------------

# App control lives in yacreader_db so the tools that take the index lock and the
# supervisor that honours it stop the app exactly the same way.
yacreader_running = yacreader_db.app_running
stop_yacreader = yacreader_db.stop_app


def start_yacreader() -> None:
    subprocess.run(["/usr/bin/open", "-a", config.YACREADER_APP_NAME], capture_output=True)


def jellyfin_episode_count() -> int | None:
    url, key = config.JELLYFIN_URL, config.JELLYFIN_API_KEY
    if not url or not key:
        return None
    try:
        with urllib.request.urlopen(f"{url}/Items/Counts?api_key={key}", timeout=10) as r:
            return json.load(r).get("EpisodeCount")
    except Exception:   # noqa: BLE001 -- unreachable/booting Jellyfin is not a count of 0
        return None


def jellyfin_scan_progress() -> float | None:
    """Progress percent of a RUNNING library scan, or None if no scan is running.

    Deliberately a DIFFERENT endpoint from `jellyfin_episode_count`. `/Items/Counts`
    runs a DB query that a real FUSE scan starves past its timeout, which is exactly why
    a scanning Jellyfin was indistinguishable from a hung one. `/ScheduledTasks` is served
    from memory and keeps answering under load, so it can still tell us a scan is the
    reason the other endpoint went quiet.

    Returns the percentage (0.0 when a scan is running but has not reported one yet) so
    the caller can tell a scan that is MOVING from one that is wedged. None means "no
    scan running" -- including when this probe itself fails, because an unanswerable
    /ScheduledTasks is evidence of a hang, not of a scan.
    """
    url, key = config.JELLYFIN_URL, config.JELLYFIN_API_KEY
    if not url or not key:
        return None
    try:
        with urllib.request.urlopen(f"{url}/ScheduledTasks?api_key={key}", timeout=10) as r:
            tasks = json.load(r)
    except Exception:   # noqa: BLE001 -- see docstring: treat as "no scan", not "scanning"
        return None
    for t in tasks or []:
        if t.get("State") == "Running" and t.get("Key") in config.SUPERVISOR_SCAN_TASK_KEYS:
            return float(t.get("CurrentProgressPercentage") or 0.0)
    return None


def restore_gutted_jellyfin() -> None:
    good = db_guardian.newest_good_backup()
    if good is None:
        alert("Jellyfin DB gutted but NO verified backup exists; manual fix needed.")
        return
    if not db_guardian.stop_jellyfin():
        alert("Jellyfin DB gutted; could not stop Jellyfin to restore. Manual fix needed.")
        return
    cdir = config.DBG_CORRUPT_DIR / f"gutted-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(config.JELLYFIN_DB) + suffix)
            if p.exists():
                shutil.move(str(p), str(cdir / p.name))
        shutil.copy2(good, config.JELLYFIN_DB)
    except OSError as e:
        alert(f"Gutted-DB restore FAILED mid-way ({e}); Jellyfin left down. Manual fix needed.")
        return
    finally:
        db_guardian.start_jellyfin()
    alert(f"Jellyfin DB was gutted; restored {good.name} (gutted copy in {cdir}).")


# --- one cycle ---------------------------------------------------------------

def tick(state: dict) -> None:
    healthy = mount_healthy()

    if not healthy:
        # The mount is NOT ready (not mounted, or a library dir is definitively empty --
        # mount_healthy() already returns True on an ambiguous slow-readdir under load, so
        # a False here is always a real not-ready). The apps must NEVER run over this: a
        # not-yet-mounted ~/MediaLibrary makes YacReader pop "library folder doesn't exist"
        # and makes a Jellyfin scan gut its DB. So stop them IMMEDIATELY -- no debounce --
        # and reset the primed streak, so bringing them back requires confirmed readiness.
        state["ready"] = 0
        state["gutted"] = 0
        state["unresponsive_since"] = None
        if db_guardian.jellyfin_running():
            log("mount not ready -> stopping Jellyfin (must not serve/scan an unprimed library)")
            db_guardian.stop_jellyfin()
        if yacreader_running():
            log("mount not ready -> stopping YacReader (must not read an unmounted library)")
            stop_yacreader()
        return

    # Mount reports healthy. Require it to STAY healthy for SUPERVISOR_READY_DEBOUNCE
    # consecutive polls before starting anything, so a momentary blip during boot/remount
    # can never flash the apps up over a half-ready mount. Only sustained readiness counts
    # as "primed" -- the certainty the apps need.
    state["ready"] += 1
    if state["ready"] < config.SUPERVISOR_READY_DEBOUNCE:
        log(f"mount healthy ({state['ready']}/{config.SUPERVISOR_READY_DEBOUNCE}) -- "
            f"confirming it is primed before starting apps")
        return
    if state["ready"] == config.SUPERVISOR_READY_DEBOUNCE:
        log("mount confirmed primed; apps may run")

    if not db_guardian.jellyfin_running():
        log("starting Jellyfin (mount primed)")
        db_guardian.start_jellyfin()
        state["gutted"] = 0
        state["unresponsive_since"] = None
    else:
        count = jellyfin_episode_count()
        if count is None:
            # Running but the authenticated API won't answer. A brief None is a boot
            # in progress (connection refused, fast); a SUSTAINED None is the hang
            # where every api_key request times out while unauthenticated ones answer.
            # Time it on the wall clock so a slow boot is never mistaken for a hang.
            if state["unresponsive_since"] is None:
                state["unresponsive_since"] = time.time()
                log("Jellyfin running but API unreachable; starting unresponsive timer")
            elif time.time() - state["unresponsive_since"] >= config.SUPERVISOR_UNRESPONSIVE_SEC:
                # A SCAN IS NOT A HANG. Ask the in-memory task endpoint before restarting:
                # a running scan starves /Items/Counts and used to look identical to the
                # hang this branch exists for (HANDOFF S6 -- 17 restarts).
                pct = jellyfin_scan_progress()
                if pct is None:
                    log("Jellyfin API unreachable for too long; restarting Jellyfin")
                    db_guardian.stop_jellyfin()
                    db_guardian.start_jellyfin()
                    state["unresponsive_since"] = None
                    state["scan_since"] = None
                    state["scan_pct"] = None
                else:
                    # Scanning. Defer -- but only while the scan is actually MOVING, and
                    # only up to the grace. A wedged scan reports Running forever.
                    now = time.time()
                    if state.get("scan_since") is None or pct > (state.get("scan_pct") or -1.0):
                        if state.get("scan_since") is None:
                            log(f"Jellyfin API busy but a library scan is RUNNING ({pct:.1f}%); "
                                f"deferring restart up to {config.SUPERVISOR_SCAN_GRACE_SEC}s")
                        state["scan_since"] = now
                        state["scan_pct"] = pct
                        state["unresponsive_since"] = now   # scan is progressing: reset the hang clock
                    elif now - state["scan_since"] >= config.SUPERVISOR_SCAN_GRACE_SEC:
                        log(f"library scan stuck at {pct:.1f}% for "
                            f"{config.SUPERVISOR_SCAN_GRACE_SEC}s; restarting Jellyfin")
                        db_guardian.stop_jellyfin()
                        db_guardian.start_jellyfin()
                        state["unresponsive_since"] = None
                        state["scan_since"] = None
                        state["scan_pct"] = None
        else:
            state["unresponsive_since"] = None
            state["scan_since"] = None
            state["scan_pct"] = None
            if count < config.JELLYFIN_MIN_EPISODES:
                state["gutted"] += 1
                log(f"Jellyfin episode count {count} < floor {config.JELLYFIN_MIN_EPISODES} "
                    f"({state['gutted']}/{config.SUPERVISOR_GUTTED_DEBOUNCE})")
                if state["gutted"] >= config.SUPERVISOR_GUTTED_DEBOUNCE:
                    restore_gutted_jellyfin()
                    state["gutted"] = 0
            else:
                state["gutted"] = 0

    # YacReader writes its SQLite index through the FUSE mount while every fleet tool
    # writes the same physical file on the SSD, and mediafs implements no `lock`, so the
    # two cannot exclude each other (yacreader_db). A tool holding the index lock is
    # therefore the one condition under which the app must NOT run even over a primed
    # mount. Releasing the lock is all a tool has to do -- the next tick starts the app.
    if yacreader_db.is_held():
        if yacreader_running():
            log(f"YacReader index lock held ({yacreader_db.holder()}) -> stopping YacReader")
            stop_yacreader()
        return

    if not yacreader_running():
        log("starting YacReader (mount primed)")
        start_yacreader()


def print_status() -> None:
    print(f"mount:      {config.MEDIAFS_MOUNT} healthy={mount_healthy()} mounted={mount_is_mounted()}")
    print(f"Jellyfin:   {'up' if db_guardian.jellyfin_running() else 'down'} "
          f"episodes={jellyfin_episode_count()}")
    print(f"YacReader:  {'up' if yacreader_running() else 'down'}")
    held = yacreader_db.is_held()
    print(f"index lock: {'HELD by ' + (yacreader_db.holder() or '?') if held else 'free'}"
          f"{'  (YacReader is held down until it is released)' if held else ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Library app supervisor.")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    ap.add_argument("--once", action="store_true", help="one cycle then exit")
    args = ap.parse_args()

    if args.status:
        print_status()
        return 0

    acquire_lock()
    log("library_supervisor started")
    state = {"ready": 0, "gutted": 0, "unresponsive_since": None,
             "scan_since": None, "scan_pct": None}
    if args.once:
        tick(state)
        return 0
    while True:
        try:
            tick(state)
        except Exception as e:   # noqa: BLE001 -- daemon must never die on a cycle
            log(f"unexpected error: {e}")
        time.sleep(config.SUPERVISOR_POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
