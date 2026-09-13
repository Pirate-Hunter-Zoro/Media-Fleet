#!/usr/bin/env python3
"""Jellyfin DB guardian.

A standalone KeepAlive daemon that makes Jellyfin's SQLite library DB
crash-proof. It runs a tight, cheap loop and does three things:

1. **Watch, unobtrusively.** Poll the live DB's (mtime, size) signature. The DB
   churns constantly during a library scan, so we act only when it has SETTLED
   (signature stable for `DBG_QUIESCENT_SEC`) or when `DBG_MAX_BACKUP_INTERVAL_SEC`
   has passed while it keeps changing.

2. **Snapshot + verify, never blocking Jellyfin.** Take a snapshot with SQLite's
   online-backup API (page-batched with a sleep, so the live server is never
   locked), then run `integrity_check`/`quick_check` on the COPY -- never on the
   live file. A snapshot is promoted into the rotating backup store ONLY if it
   passes. So "the most recent backup" is by construction the most recent GOOD
   one, and a corrupt DB can never overwrite the last good copy -- the exact flaw
   a naive fixed-time cron has.

3. **Heal on corruption.** If a check finds the live DB corrupt, stop Jellyfin,
   move the corrupt files aside for forensics, restore the newest VERIFIED
   snapshot over the live DB, restart Jellyfin, and alert. It refuses to heal if
   no verified backup exists yet (never makes things worse), and backs off if
   corruption recurs within `DBG_HEAL_COOLDOWN_SEC` (a failing disk, not a
   one-off) rather than thrashing.

Newest verified snapshots are also pushed off-machine to the shared
metadata-backup MEGA remote, throttled (`DBG_REMOTE_PUSH_INTERVAL_SEC`).

Non-fatal by design: any unexpected error in a cycle is logged and the loop
continues. Runs as its own launchd user-agent (KeepAlive), independent of the
ingest daemon.

Usage:
    python3 db_guardian.py            # run the daemon loop
    python3 db_guardian.py --once     # one check/backup pass, then exit (for testing)
    python3 db_guardian.py --status   # print current state and exit
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
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

# Tracks the in-flight off-machine push so the loop never blocks on MEGA and never
# stacks concurrent uploads.
_push_thread: threading.Thread | None = None


# --- logging -----------------------------------------------------------------

def log(msg: str) -> None:
    line = f"[{config.log_stamp()}] {msg}"
    print(line, flush=True)
    try:
        config.rotate_log_if_large(config.DBG_LOG_FILE)
        with config.DBG_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _ts() -> str:
    # Local time, lexicographically sortable == chronological (drives rotation).
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# --- single-instance lock ----------------------------------------------------

def acquire_lock():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = config.DBG_LOCK_FILE.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Another db_guardian instance holds the lock; exiting.")
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh  # keep handle alive for process lifetime


# --- alerts ------------------------------------------------------------------

def alert(msg: str) -> None:
    log("ALERT: " + msg)
    try:
        config.DBG_ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with config.DBG_ALERT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except OSError:
        pass
    notify("Jellyfin DB Guardian", msg[:200])


def notify(title: str, msg: str) -> None:
    """Best-effort macOS notification (works from a user-session LaunchAgent)."""
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             f"display notification {json.dumps(msg)} with title {json.dumps(title)}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


# --- Jellyfin process control ------------------------------------------------

def jellyfin_running() -> bool:
    r = subprocess.run(["/usr/bin/pgrep", "-f", config.JELLYFIN_PROC_PATTERN],
                       capture_output=True, text=True)
    return r.returncode == 0


def stop_jellyfin() -> bool:
    """Quit Jellyfin gracefully, hard-kill as fallback. Returns True if it is down."""
    try:
        subprocess.run(["/usr/bin/osascript", "-e", f'quit app "{config.JELLYFIN_APP_NAME}"'],
                       capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        pass  # Jellyfin unresponsive to the quit Apple event; fall through to pkill
    for _ in range(15):
        if not jellyfin_running():
            return True
        time.sleep(1)
    subprocess.run(["/usr/bin/pkill", "-f", config.JELLYFIN_PROC_PATTERN],
                   capture_output=True, text=True)
    for _ in range(10):
        if not jellyfin_running():
            return True
        time.sleep(1)
    # A hung server can ignore SIGTERM; SIGKILL is the last resort. Safe here: every
    # caller either replaces the DB next (heal / gutted-restore) or is clearing a hung
    # process so a fresh one can start.
    subprocess.run(["/usr/bin/pkill", "-9", "-f", config.JELLYFIN_PROC_PATTERN],
                   capture_output=True, text=True)
    for _ in range(5):
        if not jellyfin_running():
            return True
        time.sleep(1)
    return not jellyfin_running()


def start_jellyfin() -> None:
    subprocess.run(["/usr/bin/open", "-a", config.JELLYFIN_APP_NAME],
                   capture_output=True, text=True)


# --- DB signature + snapshot/verify ------------------------------------------

_DB_SIDECARS = ("", "-wal", "-shm")
_CORRUPT_SIGNS = ("malformed", "not a database", "corrupt", "disk image is malformed",
                  "file is not a database")


def db_signature():
    """(suffix, mtime, size) for the DB and its WAL/SHM sidecars -- our cheap
    'did anything change' probe, and what tells us the DB has settled."""
    sig = []
    for suffix in _DB_SIDECARS:
        p = Path(str(config.JELLYFIN_DB) + suffix)
        try:
            st = p.stat()
            sig.append((suffix, int(st.st_mtime), st.st_size))
        except OSError:
            sig.append((suffix, 0, 0))
    return tuple(sig)


def _classify_error(err: Exception) -> str:
    s = str(err).lower()
    if any(sig in s for sig in _CORRUPT_SIGNS):
        return "corrupt"
    return "transient"   # locked / busy / timeout -> retry, do NOT heal


def snapshot_and_check(tmp_path: Path):
    """Online-backup the live DB to tmp_path (non-blocking) and integrity-check the
    COPY. Returns (status, detail) where status is 'good' | 'transient' | 'corrupt'.

    'transient' (lock/busy) means retry later. 'corrupt' means the live DB is
    genuinely damaged and a heal is warranted.
    """
    # 1) snapshot via the online-backup API, page-batched with a sleep so Jellyfin's
    #    writers are never blocked for the whole copy (WAL readers don't block
    #    writers anyway; this is belt-and-suspenders for "unobtrusive").
    try:
        src = sqlite3.connect(str(config.JELLYFIN_DB), timeout=config.DBG_SQLITE_TIMEOUT_SEC)
    except sqlite3.Error as e:
        return _classify_error(e), f"open live db: {e}"
    try:
        src.execute(f"PRAGMA busy_timeout={config.DBG_SQLITE_TIMEOUT_SEC * 1000}")
        dst = sqlite3.connect(str(tmp_path))
        try:
            src.backup(dst, pages=2000, sleep=0.05)
        finally:
            dst.close()
    except sqlite3.Error as e:
        return _classify_error(e), f"backup: {e}"
    finally:
        src.close()

    # 2) integrity-check the SNAPSHOT (never the live file). If the copy is itself
    #    unreadable/malformed, the live DB was corrupt at the pages we copied.
    try:
        chk = sqlite3.connect(str(tmp_path))
        try:
            quick = chk.execute("PRAGMA quick_check").fetchone()
            if not quick or quick[0] != "ok":
                return "corrupt", f"quick_check={quick}"
            integ = chk.execute("PRAGMA integrity_check").fetchall()
            if integ != [("ok",)]:
                return "corrupt", f"integrity_check={integ[:5]}"
        finally:
            chk.close()
    except sqlite3.Error as e:
        return _classify_error(e), f"check copy: {e}"
    return "good", "ok"


# --- backup store rotation ---------------------------------------------------

def _backups():
    if not config.DBG_BACKUP_DIR.exists():
        return []
    return sorted(config.DBG_BACKUP_DIR.glob("jellyfin-db-*.sqlite"))


def newest_good_backup():
    b = _backups()
    return b[-1] if b else None


def _item_count(dbpath) -> int | None:
    """BaseItems row count -- the signal for a gutted (empty-library-scan) DB."""
    try:
        c = sqlite3.connect(str(dbpath))
        try:
            row = c.execute("SELECT COUNT(*) FROM BaseItems").fetchone()
            return row[0] if row else None
        finally:
            c.close()
    except sqlite3.Error:
        return None


def _read_hwm() -> int:
    try:
        return int(config.DBG_HWM_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def _update_hwm(count: int) -> None:
    if count > _read_hwm():
        try:
            config.DBG_HWM_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.DBG_HWM_FILE.write_text(str(count))
        except OSError:
            pass


def promote(snapshot: Path) -> Path:
    config.DBG_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.DBG_BACKUP_DIR / f"jellyfin-db-{_ts()}.sqlite"
    shutil.move(str(snapshot), str(dest))
    # rotate: keep newest DBG_KEEP_LOCAL
    excess = _backups()[:-config.DBG_KEEP_LOCAL] if len(_backups()) > config.DBG_KEEP_LOCAL else []
    for p in excess:
        try:
            p.unlink()
        except OSError:
            pass
    return dest


# --- off-machine push (throttled) --------------------------------------------

def maybe_push_remote(backup: Path, state: dict) -> None:
    """Push the newest verified snapshot off-machine, in a BACKGROUND THREAD.

    MEGA is throttled and a 200 MB+ upload can take many minutes; running it
    inline would stall the guardian loop (and leave the DB unwatched) for the
    whole transfer. So it runs detached. `last_remote_push` is advanced up front
    so a slow/failed push doesn't re-fire every cycle -- it retries on the normal
    6-hour cadence.
    """
    global _push_thread
    if config.DBG_REMOTE_PUSH_INTERVAL_SEC <= 0:
        return
    now = time.time()
    if now - state.get("last_remote_push", 0.0) < config.DBG_REMOTE_PUSH_INTERVAL_SEC:
        return
    if _push_thread is not None and _push_thread.is_alive():
        return   # a previous push is still uploading
    if not (os.path.exists(config.RCLONE_BIN) or shutil.which(config.RCLONE_BIN)):
        return
    state["last_remote_push"] = now
    _push_thread = threading.Thread(target=_push_worker, args=(backup,), daemon=True)
    _push_thread.start()


def _push_worker(backup: Path) -> None:
    remote = f"{config.METADATA_BACKUP_REMOTE}:{config.METADATA_BACKUP_BASE}/{config.DBG_REMOTE_SUBPATH}"
    cmd = [
        config.RCLONE_BIN, "copy", str(backup), remote,
        "--config", str(config.RCLONE_CONFIG),
        "--transfers", "2", "--retries", "3", "--low-level-retries", "10",
        "--stats-one-line",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"remote push error: {e}")
        return
    if r.returncode != 0:
        log(f"remote push failed (exit {r.returncode}); will retry next interval")
        return
    log(f"pushed {backup.name} -> {remote}")
    _prune_remote(remote)


def _prune_remote(remote: str) -> None:
    try:
        r = subprocess.run(
            [config.RCLONE_BIN, "lsf", remote, "--config", str(config.RCLONE_CONFIG)],
            capture_output=True, text=True, timeout=120,
        )
        files = sorted(x.strip() for x in r.stdout.splitlines()
                       if x.strip().startswith("jellyfin-db-"))
        stale = files[:-config.DBG_KEEP_REMOTE] if len(files) > config.DBG_KEEP_REMOTE else []
        for name in stale:
            subprocess.run(
                [config.RCLONE_BIN, "deletefile", f"{remote}/{name}",
                 "--config", str(config.RCLONE_CONFIG)],
                capture_output=True, text=True, timeout=120,
            )
        if stale:
            # MEGA's use_trash parks deletes in the rubbish bin where they still count
            # against the account's quota. Empty it so the pruned snapshot actually frees
            # space -- otherwise the metadata-backup account drifts toward "over quota".
            subprocess.run(
                [config.RCLONE_BIN, "cleanup", f"{remote.split(':')[0]}:",
                 "--config", str(config.RCLONE_CONFIG)],
                capture_output=True, text=True, timeout=600,
            )
    except (OSError, subprocess.TimeoutExpired):
        pass


# --- heal --------------------------------------------------------------------

def heal(reason: str, state: dict) -> bool:
    good = newest_good_backup()
    if good is None:
        alert(f"CORRUPTION DETECTED ({reason}) but NO verified backup exists yet -- "
              f"refusing to touch the live DB. Manual intervention needed.")
        return False

    log(f"HEAL: live DB corrupt ({reason}); restoring {good.name}")
    if not stop_jellyfin():
        alert(f"CORRUPTION ({reason}): could not stop Jellyfin -- aborting heal to "
              f"avoid a half-restore. Manual intervention needed.")
        return False

    cdir = config.DBG_CORRUPT_DIR / f"corrupt-{_ts()}"
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        for suffix in _DB_SIDECARS:
            p = Path(str(config.JELLYFIN_DB) + suffix)
            if p.exists():
                shutil.move(str(p), str(cdir / p.name))
        shutil.copy2(good, config.JELLYFIN_DB)
    except OSError as e:
        alert(f"CORRUPTION ({reason}): restore FAILED mid-way ({e}); corrupt files in "
              f"{cdir}. Jellyfin left DOWN. Manual intervention needed.")
        return False
    finally:
        start_jellyfin()

    state["last_heal_time"] = time.time()
    alert(f"CORRUPTION HEALED ({reason}): restored {good.name}; corrupt files saved to "
          f"{cdir}. Jellyfin restarted.")
    return True


# --- one pass ----------------------------------------------------------------

ORPHANED_CHILDREN_SQL = """
SELECT COUNT(*) FROM BaseItems c JOIN BaseItems s ON s.Id = c.SeriesId
 WHERE (c.Type LIKE '%Episode%' OR c.Type LIKE '%Season%')
   AND c.SeriesId IS NOT NULL
   AND c.SeriesPresentationUniqueKey IS NOT NULL
   AND s.PresentationUniqueKey IS NOT NULL
   AND c.SeriesPresentationUniqueKey <> s.PresentationUniqueKey
"""

REPAIR_ORPHANED_CHILDREN_SQL = """
UPDATE BaseItems
   SET SeriesPresentationUniqueKey = (SELECT s.PresentationUniqueKey
                                        FROM BaseItems s WHERE s.Id = BaseItems.SeriesId)
 WHERE (Type LIKE '%Episode%' OR Type LIKE '%Season%')
   AND SeriesId IS NOT NULL
   AND SeriesPresentationUniqueKey IS NOT NULL
   AND EXISTS (SELECT 1 FROM BaseItems s
                WHERE s.Id = BaseItems.SeriesId
                  AND s.PresentationUniqueKey IS NOT NULL
                  AND s.PresentationUniqueKey <> BaseItems.SeriesPresentationUniqueKey)
"""


def reconcile_series_presentation_keys(state: dict) -> int:
    """Re-point season/episode rows at their series' CURRENT PresentationUniqueKey.

    THE BUG THIS FIXES -- "the show just doesn't appear in Jellyfin", with a healthy DB.
    `/Shows/{id}/Seasons` and `/Shows/{id}/Episodes` do not filter on SeriesId; they filter on
    `SeriesPresentationUniqueKey == series.PresentationUniqueKey`. That key is derived from the
    series' PROVIDER id, and Jellyfin stamps it into each child ONCE, when the child is created.
    So if a series' children are created BEFORE its identity is resolved, they capture the
    fallback key (the series' raw GUID); when the provider id later arrives, the series' key
    becomes `<tvdbid>-en-<hash>` and every child is stranded under the old one. The rows are
    otherwise perfect -- correct SeriesId, correct ParentId, correct AncestorIds -- so nothing
    reports damage, and the series simply serves ZERO episodes forever.

    Nothing self-heals it: a metadata refresh of the child does NOT recompute the key (verified),
    a library scan does not either, and the only Jellyfin-native cure would be deleting and
    recreating the children -- which for this fleet means deleting real FILES (see the landmine
    note in the README). So it is repaired here, in the daemon that already owns this database.

    Safe to do automatically: `SeriesPresentationUniqueKey` is a denormalized lookup column with
    no foreign keys pointing at it, the statement is idempotent (it only touches rows that
    disagree), and it runs immediately AFTER a verified backup has been promoted -- so there is
    always a known-good copy from moments earlier. Returns the number of rows repaired.

    (Seen 2026-08-04: The Eminence in Shadow served 0 of 32 episodes and Soul Eater NOT! 0 of 12,
    both fresh identities. 46 rows. Repeated refreshes and full library scans never touched it.)
    """
    if not getattr(config, "DBG_RECONCILE_PRESENTATION_KEYS", True):
        return 0
    try:
        con = sqlite3.connect(str(config.JELLYFIN_DB), timeout=config.DBG_SQLITE_TIMEOUT_SEC)
    except sqlite3.Error as exc:
        log(f"presentation-key reconcile: cannot open DB ({exc}); skipping")
        return 0
    try:
        stale = con.execute(ORPHANED_CHILDREN_SQL).fetchone()[0]
        if not stale:
            return 0
        con.execute("BEGIN")
        cur = con.execute(REPAIR_ORPHANED_CHILDREN_SQL)
        fixed = cur.rowcount
        con.commit()
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integ != "ok":
            alert(f"presentation-key reconcile: integrity_check={integ!r} AFTER repair -- "
                  f"restore the backup just promoted")
            return 0
        alert(f"repaired {fixed} orphaned season/episode row(s) whose "
              f"SeriesPresentationUniqueKey no longer matched their series -- those shows were "
              f"serving ZERO episodes despite being on disk and correctly linked")
        state["last_key_reconcile"] = time.time()
        return fixed
    except sqlite3.Error as exc:
        log(f"presentation-key reconcile failed: {exc}")
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        return 0
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


# Two DIFFERENT series sharing one PresentationUniqueKey. `/Shows/{id}/Episodes` filters on
# the key, not on SeriesId, so browsing either show serves BOTH shows' episodes -- Dr. STONE
# listed 146 episodes against 96 files on disk because "The Rising of the Shield Hero"'s
# tvshow.nfo carried Dr. STONE's tvdb id (and Goblin Slayer carried Demon Slayer's). The cause
# is one bad identify run stamping a single candidate onto two series, and the DB itself is
# undamaged, so no integrity check, no library scan and no metadata refresh ever mentions it.
#
# REPORTED, NEVER REPAIRED. Fixing it means deciding WHICH of the two series holds the wrong
# provider id and what the right one is -- an external lookup (TVMaze `externals`, Jellyfin
# `RemoteSearch/Series`) and a judgement call. Guessing here would re-stamp the wrong show and
# then replace its artwork under a wrong identity, which is the expensive direction. So this
# says exactly which shows collide and leaves the call to a human.
DUPLICATE_SERIES_KEY_SQL = """
SELECT PresentationUniqueKey, COUNT(*) AS n,
       GROUP_CONCAT(COALESCE(Name, '(unnamed)'), ' | ') AS names
  FROM BaseItems
 WHERE Type LIKE '%Series%'
   AND PresentationUniqueKey IS NOT NULL
 GROUP BY PresentationUniqueKey
HAVING COUNT(*) > 1
"""


def check_duplicate_series_keys(state: dict) -> int:
    """Report series rows that share one PresentationUniqueKey. Returns how many keys collide.

    A check that reports nothing must be able to prove it CAN report something (§ diagnosis 7):
    `scripts/test_duplicate_series_keys.py` runs this exact SQL against a fixture holding one
    planted collision and one clean pair, and asserts it finds the first and not the second.
    """
    try:
        con = sqlite3.connect(f"file:{config.JELLYFIN_DB}?mode=ro", uri=True,
                              timeout=config.DBG_SQLITE_TIMEOUT_SEC)
    except sqlite3.Error as exc:
        log(f"duplicate-series-key check: cannot open DB ({exc}); skipping")
        return 0
    try:
        rows = con.execute(DUPLICATE_SERIES_KEY_SQL).fetchall()
    except sqlite3.Error as exc:
        log(f"duplicate-series-key check failed: {exc}")
        return 0
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    state["last_dup_key_check"] = time.time()
    if not rows:
        log("duplicate-series-key check: 0 collisions")
        return 0
    for key, n, names in rows:
        alert(f"{n} DIFFERENT series share PresentationUniqueKey {key!r}: {names}. "
              f"Browsing either one serves the other's episodes. One of them has the wrong "
              f"provider id in its tvshow.nfo -- cross-check both against TVMaze externals "
              f"and correct the wrong one, then re-run db_guardian --once.")
    return len(rows)


def do_backup_or_heal(state: dict, sig, now: float) -> None:
    config.DBG_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.DBG_BACKUP_DIR / f".snapshot-{os.getpid()}.tmp"
    for junk in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        try:
            junk.unlink()
        except OSError:
            pass

    status, detail = snapshot_and_check(tmp)

    if status == "good":
        # Gut-guard: a DB Jellyfin gutted after scanning an empty (unmounted)
        # library is VALID (passes integrity_check) but nearly empty. Promoting it
        # would let a later heal restore the empty one. Refuse if the item count
        # collapsed below a fraction of the high-water mark.
        count = _item_count(tmp)
        hwm = _read_hwm()
        if count is not None and hwm and count < hwm * config.DBG_MIN_ITEM_FRACTION:
            alert(f"snapshot BaseItems={count} < {config.DBG_MIN_ITEM_FRACTION:.0%} of "
                  f"high-water-mark {hwm}: DB looks GUTTED (empty-library scan?). NOT "
                  f"promoting, so the last good backup survives.")
            try:
                tmp.unlink()
            except OSError:
                pass
            state["last_backup_sig"] = sig   # don't re-snapshot this same gutted state
            return
        dest = promote(tmp)
        if count is not None:
            _update_hwm(count)
        state["last_backup_sig"] = sig
        state["last_backup_time"] = now
        log(f"verified backup: {dest.name}" + (f" ({count} items)" if count else ""))
        maybe_push_remote(dest, state)
        # Only ever attempt a live repair with a just-verified backup on disk, so the worst case
        # is a restore of a DB that was good seconds ago.
        reconcile_series_presentation_keys(state)
        check_duplicate_series_keys(state)
        return

    # clean up the bad/partial snapshot
    for junk in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        try:
            junk.unlink()
        except OSError:
            pass

    if status == "transient":
        log(f"transient snapshot issue ({detail}); will retry next cycle")
        return   # do NOT advance last_backup_sig -> retried next cycle

    # status == 'corrupt'
    if now - state.get("last_heal_time", 0.0) < config.DBG_HEAL_COOLDOWN_SEC:
        alert(f"CORRUPTION again within cooldown ({detail}); NOT re-healing "
              f"(possible failing disk). Manual check needed.")
        return
    if heal(detail, state):
        # force a fresh baseline of the restored good DB on the next settle
        state["last_backup_sig"] = None
        state["last_sig"] = None
        state["sig_since"] = time.time()


def tick(state: dict) -> None:
    if not config.JELLYFIN_DB.exists():
        return
    sig = db_signature()
    now = time.time()

    if sig != state["last_sig"]:
        state["last_sig"] = sig
        state["sig_since"] = now
        return   # just changed -> let it settle before snapshotting

    if sig == state.get("last_backup_sig"):
        return   # this exact state is already backed up

    settled = (now - state["sig_since"]) >= config.DBG_QUIESCENT_SEC
    forced = (now - state["last_backup_time"]) >= config.DBG_MAX_BACKUP_INTERVAL_SEC
    if settled or forced:
        do_backup_or_heal(state, sig, now)


# --- entrypoints -------------------------------------------------------------

def _fresh_state() -> dict:
    now = time.time()
    return {
        "last_sig": None,        # signature seen last poll
        "sig_since": now,        # when the current signature first appeared
        "last_backup_sig": None, # signature we last verified+promoted
        "last_backup_time": now, # so 'forced' fires MAX_BACKUP_INTERVAL after start
        "last_remote_push": 0.0,
        "last_heal_time": 0.0,
        "last_dup_key_check": 0.0,
    }


def print_status() -> None:
    print(f"live DB:        {config.JELLYFIN_DB} "
          f"({'present' if config.JELLYFIN_DB.exists() else 'MISSING'})")
    b = _backups()
    print(f"local backups:  {len(b)} in {config.DBG_BACKUP_DIR}")
    if b:
        newest = b[-1]
        size_mb = newest.stat().st_size / 1024**2
        print(f"newest backup:  {newest.name} ({size_mb:.0f} MB)")
    print(f"Jellyfin:       {'running' if jellyfin_running() else 'not running'}")
    if config.DBG_ALERT_FILE.exists():
        print(f"ALERTS present: {config.DBG_ALERT_FILE}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Jellyfin DB guardian daemon.")
    ap.add_argument("--once", action="store_true",
                    help="run a single check/backup pass (ignores the settle gate) and exit")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    args = ap.parse_args()

    if args.status:
        print_status()
        return 0

    if args.once:
        acquire_lock()
        log("db_guardian --once")
        if not config.JELLYFIN_DB.exists():
            log("live DB missing; nothing to do")
            return 0
        do_backup_or_heal(_fresh_state(), db_signature(), time.time())
        return 0

    acquire_lock()
    log("db_guardian started")
    state = _fresh_state()
    while True:
        try:
            tick(state)
        except Exception as e:   # noqa: BLE001 -- daemon must never die on a cycle
            log(f"unexpected error: {e}")
        time.sleep(config.DBG_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
