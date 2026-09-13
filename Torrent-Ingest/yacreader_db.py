"""Mutual exclusion for YacReader's SQLite index.

WHY SQLITE'S OWN LOCKING DOES NOT PROTECT THIS FILE
    YacReader's registered library root is the mediafs MOUNT
    (`~/MediaLibrary/Comics`), so the app opens and WRITES
    `~/MediaLibrary/Comics/.yacreaderlibrary/library.ydb` through FUSE. Every fleet
    tool opens the SAME PHYSICAL FILE directly on the SSD at
    `~/Media/Comics/.yacreaderlibrary/library.ydb` -- identical bytes, identical
    inode, reached by two different filesystems.

    `mediafs.py` implements no `lock` operation. A byte-range lock taken through the
    mount and one taken on the SSD path therefore live in different domains and cannot
    see each other. SQLite believes it holds the database exclusively in both processes
    at once, and two concurrent writers with no mutual exclusion is exactly how this
    index has been corrupted repeatedly (§ diagnosis 4.185, 4.187): a doubly-referenced
    btree page, rowids out of order, and `comic_info` rows missing from their own
    autoindex.

    So the exclusion has to be built one level up, and this module is it. A tool that
    wants the index takes the lock here and the helper stops the app; `library_supervisor`
    -- the declared authority on when the library apps may run -- refuses to start
    YacReader while the lock is held and stops it if it is already up. When the tool
    lets go, the supervisor's next tick brings the app straight back, so no caller has to
    bootout/bootstrap launchd by hand (a runbook step that leaves Jellyfin unsupervised
    too if a session dies half way through it).

WHY THE LOCK FILE IS ON THE SSD
    It is `state/yacreader_db.lock`, a normal local file. Putting the lock anywhere under
    the mount would reintroduce the very boundary it exists to bridge.

    from yacreader_db import db_lock
    with db_lock("comic_shelf_audit"):
        ...          # the app is down and will stay down for this block
"""
from __future__ import annotations

import errno
import fcntl
import os
import subprocess
import time
from contextlib import contextmanager

# This repo's own directory goes FIRST on sys.path -- Torrent-Ingest and Torrent-Searcher
# both ship a `config.py`, and a bare `import config` otherwise resolves to whichever repo
# the launcher happened to put first (see library_supervisor.py).
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import config


class LockUnavailable(RuntimeError):
    """Another holder kept the index lock for longer than the caller was willing to wait."""


def app_running() -> bool:
    return subprocess.run(["/usr/bin/pgrep", "-f", config.YACREADER_PROC_PATTERN],
                          capture_output=True).returncode == 0


def stop_app() -> bool:
    """Ask YacReader to quit, then insist. Returns True if it is down when we return.

    The graceful quit is an AppleEvent, and YacReader stops answering AppleEvents while
    it is scanning a library -- which is precisely when a tool most wants it gone. So the
    timeout is expected, not exceptional, and we escalate to SIGTERM (which Qt handles as
    a clean shutdown) rather than treating a stuck `osascript` as a failure.
    """
    if not app_running():
        return True
    subprocess.run(["/usr/bin/osascript", "-e", f'quit app "{config.YACREADER_APP_NAME}"'],
                   capture_output=True, timeout=10, check=False)
    deadline = time.time() + config.YACREADER_STOP_TIMEOUT_SEC
    while time.time() < deadline:
        if not app_running():
            return True
        time.sleep(1)
    subprocess.run(["/usr/bin/pkill", "-f", config.YACREADER_PROC_PATTERN],
                   capture_output=True, check=False)
    deadline = time.time() + config.YACREADER_STOP_TIMEOUT_SEC
    while time.time() < deadline:
        if not app_running():
            return True
        time.sleep(1)
    return not app_running()


def is_held() -> bool:
    """True if some process currently holds the index lock.

    Probed by taking the lock non-blockingly and dropping it again, so the answer is a
    real test of the same primitive the holders use rather than a pid file that outlives
    a crash. A missing lock file means nobody has ever taken it -- not held.
    """
    try:
        fh = open(config.YACREADER_DB_LOCK_FILE, "a+")
    except OSError:
        return False
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return True
        return False
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


def holder() -> str:
    """Whatever the current holder wrote about itself, for logs. '' if unheld/unknown."""
    try:
        return config.YACREADER_DB_LOCK_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@contextmanager
def db_lock(purpose: str, stop_app_first: bool = True):
    """Hold the index lock for the duration of the block, with YacReader stopped.

    The app is NOT restarted on the way out. `library_supervisor` sees the lock released
    on its next poll and starts it, which keeps one component in charge of when the
    library apps run instead of two disagreeing.
    """
    config.YACREADER_DB_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(config.YACREADER_DB_LOCK_FILE, "a+")
    deadline = time.time() + config.YACREADER_DB_LOCK_TIMEOUT_SEC
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                fh.close()
                raise
            if time.time() >= deadline:
                who = holder()
                fh.close()
                raise LockUnavailable(
                    f"the YacReader index lock is still held{' by ' + who if who else ''} "
                    f"after {config.YACREADER_DB_LOCK_TIMEOUT_SEC}s; nothing was changed")
            time.sleep(1)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} purpose={purpose} since={config.log_stamp()}\n")
        fh.flush()
        if stop_app_first and not stop_app():
            raise RuntimeError(
                "could not stop YACReaderLibrary; refusing to touch its index while it "
                "is running (its writes go through FUSE and cannot be locked against)")
        yield
    finally:
        try:
            fh.seek(0)
            fh.truncate()
            fh.flush()
        except OSError:
            pass
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()
