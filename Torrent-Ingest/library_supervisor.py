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
  * YacReader is held to the same freshness contract as the rest of the fleet: its
    index only updates when the APP updates it, so this supervisor patches the
    scan-at-startup flags before every start, bounces a running app whose flags have
    drifted, and consumes `record_plan`'s refresh marker so comics filed while the
    app was up are indexed instead of waiting for a random restart. A crashed app
    (it has crashed on library reloads) is restarted with a backoff instead of a
    tight loop, and the crash is alerted.

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
    # `-g` launches WITHOUT bringing the app to the foreground. The scan does not need
    # the window to exist (the startup update was observed running windowless), and the
    # owner does not want a reader he did not open covering whatever he was doing.
    subprocess.run(["/usr/bin/open", "-g", "-a", config.YACREADER_APP_NAME],
                   capture_output=True)


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

def _start_yacreader_with_scan(state: dict) -> None:
    """Start the app with the auto-update flags ON -- the only way its index refreshes.

    The flags are an enforced invariant (config.YACREADER_SCAN_SETTINGS): with both off
    the app runs for days while every newly filed comic stays invisible, which is the
    2026-09-14 ElfQuest report. The ini is patched while the app is DOWN because
    YacReader rewrites the file itself on exit.

    The window is hidden later, once the update is underway (see `_hide_yacreader`):
    hiding BEFORE the library window exists suppresses its creation entirely.
    """
    if yacreader_db.ensure_scan_settings():
        log("YacReader ini: re-enabled scan-at-startup (auto-update flags had drifted)")
    log("starting YacReader (mount primed)")
    start_yacreader()
    _arm_hide(state)
    state["yac_started_at"] = time.time()
    state["yac_stopped_by_us"] = False
    state["yac_index_checked_at"] = 0        # probe the open index on the next tick
    state["yac_update_seen"] = False         # no proof of an open library yet


def _arm_hide(state: dict) -> None:
    """Arm the window hide for a fleet-started/activated reader.

    Fires on the first tick where either the library update is underway (`init()` has
    run) or the settle window has passed -- whichever comes first. See `_hide_yacreader`.
    """
    state["yac_hide_pending"] = True
    state["yac_hide_arm_at"] = time.time()


def _hide_yacreader(state: dict, why: str) -> None:
    """Hide the reader's window, best-effort, and stop touching it.

    WHEN, exactly. Hiding is armed at every fleet start/activate and on supervisor
    restart, and fires on the first tick where EITHER:

      * `update_in_progress()` is true -- proof `LibrariesUpdateCoordinator::init()` has
        run, so a library window exists and a Cmd-H cannot interrupt the SQLite
        transaction; or
      * the settle window (`SUPERVISOR_YAC_HIDE_SETTLE_SEC`) has passed -- the window is
        created at launch, so after that it either exists (hide it) or never will. This
        is what covers an app parked on the library CHOOSER, which never starts an
        update at all (measured 2026-09-19) and whose window is exactly what the owner
        does not want on screen.

    Once hidden, pending clears, so a reader the owner opens himself is never fought.
    `hide_app` failing (AppKit and System Events both refused) only means the window
    shows; it must never turn into a failed start or a crash-loop, so this logs and
    continues.
    """
    if not yacreader_db.hide_app():
        log(f"YacReader window could not be hidden after {why} (System Events "
            f"refused); it may show while it scans")
    else:
        log(f"hid YacReader's window ({why}); its library update continues")


def _yacreader_tick(state: dict) -> None:
    """Mount primed and no tool holds the index lock: enforce freshness + crash policy."""
    now = time.time()

    if state.get("yac_backoff_until"):
        if now < state["yac_backoff_until"]:
            return
        log("YacReader crash backoff expired; trying it again")
        state["yac_backoff_until"] = None
        state["yac_crashes"] = 0
        state["yac_backoff_alerted"] = False

    if yacreader_running():
        started = state.get("yac_started_at")
        if started is None:
            state["yac_started_at"] = started = now
        elif now - started >= config.SUPERVISOR_YAC_CRASH_WINDOW_SEC:
            state["yac_crashes"] = 0
        # Hide a fleet-started reader: the moment its library update is underway, or
        # once the settle window has passed for an app that never starts one (see
        # `_hide_yacreader`). Once hidden, pending clears and the owner's own use of the
        # reader is never touched.
        if state.get("yac_hide_pending"):
            updating = yacreader_db.update_in_progress()
            settled = (now - state.get("yac_hide_arm_at", now)
                       >= config.SUPERVISOR_YAC_HIDE_SETTLE_SEC)
            if updating or settled:
                _hide_yacreader(state, "update" if updating else "settle")
                state["yac_hide_pending"] = False
        # 1. Drift in the scan flags is the "new comics never appear" fault and the app
        #    is already up: it must be bounced for the patch AND for the startup scan.
        if not yacreader_db.scan_settings_ok():
            log("YacReader auto-update flags are OFF -> restarting it with a scan")
            state["yac_stopped_by_us"] = True
            stop_yacreader()
            _start_yacreader_with_scan(state)
            state["yac_last_refresh"] = now
            return
        # 2. Comics were filed while the app was up (record_plan's marker). The marker is
        #    CONSUMED, not acted on. Restarting was the old refresh trigger, but a restart
        #    lands YacReader on its library CHOOSER -- it never re-opens a library by
        #    itself (measured 2026-09-19: quit+relaunch, `open -a`, CLI args and `open`
        #    document events all leave it on the chooser) -- so every filing left the
        #    reader not scanning until a human clicked Comics, and took the owner's screen
        #    every time. The library's own periodic update is enabled (30 minutes,
        #    `UPDATE_LIBRARIES_PERIODICALLY` + interval index 0), so a filed comic indexes
        #    on its own and the open session is never interrupted. Owner decision,
        #    recorded in HANDOFF 10.6/OPERATING 5c.
        try:
            pending = config.YACREADER_REFRESH_MARKER.exists()
        except OSError:
            pending = False
        if pending:
            try:
                config.YACREADER_REFRESH_MARKER.unlink(missing_ok=True)
            except OSError:
                pass
            log("comics were filed; YacReader's periodic update will index them "
                "(no restart -- the app cannot re-open its library by itself)")
        # 3. Up but never opened its library: the startup update cannot run, so nothing
        #    scans until a human clicks Comics. Only a JUST-STARTED app can be judged --
        #    a healthy one begins its startup update within seconds (the transaction
        #    journal appears), while a long-running app between periodic scans is
        #    indistinguishable from one parked on the chooser. Activation is BOUNDED and
        #    only brings the window forward (which is immediately re-hidden): a restart
        #    does not make the chooser open the library, so the ALERT is the remedy that
        #    matters and the supervisor then leaves the app alone until its next start.
        #
        #    Activation is BOUNDED, not repeated: it steals focus, and the app it is
        #    "repairing" may simply have nothing to scan. On 2026-09-15 a stale-index
        #    false positive (the NFC/NFD comparison in yacreader_index, since fixed) had
        #    the doctor bouncing the reader every 15 minutes while this branch activated
        #    it every 60s for hours -- attempt 89 and counting -- so the owner's screen
        #    was repeatedly taken over by a reader with nothing wrong with it.
        if now - state.get("yac_index_checked_at", 0) >= config.SUPERVISOR_YAC_INDEX_CHECK_SEC:
            state["yac_index_checked_at"] = now
            if yacreader_db.update_in_progress():
                state["yac_activate_attempts"] = 0
                state["yac_activate_alerted"] = False
                # An update in flight is PROOF the library is open. Remember it for the
                # rest of this app run: a startup update can finish in seconds (nothing
                # new to index), and without this the next 60 s check sees "no update"
                # and alerts a perfectly healthy open library -- which is exactly what
                # fired at 15:01:54 on 2026-09-19, with `hid ... (update)` in the log
                # seconds earlier. Once seen, the app is left alone until its next start.
                state["yac_update_seen"] = True
            elif not state.get("yac_update_seen") and started is not None \
                    and now - started <= config.SUPERVISOR_YAC_ACTIVATE_WINDOW_SEC:
                attempts = state.get("yac_activate_attempts", 0)
                if attempts < config.SUPERVISOR_YAC_ACTIVATE_MAX_ATTEMPTS:
                    state["yac_activate_attempts"] = attempts + 1
                    log("YacReader just started and no update is running -> activating it "
                        "so the library window (and its startup update) exist (attempt "
                        f"{state['yac_activate_attempts']})")
                    yacreader_db.activate_app()
                    _arm_hide(state)              # hide once its update runs (or settles)
                if state["yac_activate_attempts"] >= config.SUPERVISOR_YAC_ACTIVATE_MAX_ATTEMPTS \
                        and not state.get("yac_activate_alerted"):
                    alert("YacReaderLibrary started but has opened no library; the "
                          "startup update cannot run and new comics will not be "
                          "indexed until the Comics library is opened manually.")
                    state["yac_activate_alerted"] = True
        return

    # Down. A start that did not survive the crash window is a crash, not a quit; enough
    # of them means the scan is crashing the app and restarting it is only thrashing.
    started = state.get("yac_started_at")
    if started is not None:
        if not state.get("yac_stopped_by_us") \
                and now - started < config.SUPERVISOR_YAC_CRASH_WINDOW_SEC:
            state["yac_crashes"] = state.get("yac_crashes", 0) + 1
        else:
            state["yac_crashes"] = 0
        state["yac_started_at"] = None

    if state.get("yac_crashes", 0) >= config.SUPERVISOR_YAC_CRASH_LIMIT:
        alert(f"YacReaderLibrary crashed {state['yac_crashes']} times within "
              f"{config.SUPERVISOR_YAC_CRASH_WINDOW_SEC}s; holding it down for "
              f"{config.SUPERVISOR_YAC_BACKOFF_SEC}s instead of restarting it")
        state["yac_backoff_until"] = now + config.SUPERVISOR_YAC_BACKOFF_SEC
        state["yac_backoff_alerted"] = True
        state["yac_crashes"] = 0
        return

    # On the way up there is nothing to bounce: the startup update IS the refresh. The
    # marker is consumed here because the scan that is about to run covers it.
    _start_yacreader_with_scan(state)
    state["yac_last_refresh"] = now
    try:
        config.YACREADER_REFRESH_MARKER.unlink(missing_ok=True)
    except OSError:
        pass


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
            state["yac_stopped_by_us"] = True
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
            state["yac_stopped_by_us"] = True
            stop_yacreader()
        return

    _yacreader_tick(state)


def print_status() -> None:
    print(f"mount:      {config.MEDIAFS_MOUNT} healthy={mount_healthy()} mounted={mount_is_mounted()}")
    print(f"Jellyfin:   {'up' if db_guardian.jellyfin_running() else 'down'} "
          f"episodes={jellyfin_episode_count()}")
    print(f"YacReader:  {'up' if yacreader_running() else 'down'}  "
          f"scan-at-startup={'on' if yacreader_db.scan_settings_ok() else 'OFF'}")
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
             "scan_since": None, "scan_pct": None,
             # YacReader freshness + crash policy (see _yacreader_tick)
             "yac_started_at": None, "yac_stopped_by_us": False, "yac_crashes": 0,
             "yac_backoff_until": None, "yac_backoff_alerted": False,
             "yac_last_refresh": 0.0, "yac_index_checked_at": 0,
             "yac_activate_attempts": 0, "yac_activate_alerted": False,
             "yac_update_seen": False,
             # Hide a reader that was already up and visible when this supervisor
             # (re)started -- a login auto-relaunch, or a deploy. It fires once the
             # app's library update is underway, or after the settle window.
             "yac_hide_pending": True, "yac_hide_arm_at": time.time()}
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
