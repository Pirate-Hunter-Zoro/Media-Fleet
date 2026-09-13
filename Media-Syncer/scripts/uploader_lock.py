"""Exclusive lock for anything that spends MEGA pool free space.

The upload phase's free-space ledger is arithmetic on a snapshot: seeded once, debited
locally as remotes are claimed. That is correct only while ONE process is spending. Two
uploaders each keep a private ledger from the same snapshot, both believe the same bytes are
free, and both spend them -- which pushes accounts past their quota with neither process
doing anything wrong on its own terms.

`REMOTE_FILL_MARGIN_BYTES` and the live resync bound how far that drifts but cannot prevent
it: two processes each seeing 5 GB free can each write 4 GB before either drops under the
resync threshold. The only real fix is to make concurrent uploading impossible.

Uses `flock`, so the lock is released automatically if the holder is killed -- important,
because media_sync is killed routinely (the reaper pauses it) and a lock that survived that
would wedge the fleet.

    from .uploader_lock import hold, acquire_or_die

    with hold() as got:              # daemon: skip the phase if something else is uploading
        if not got:
            return

    acquire_or_die("drain-drive")    # maintenance script: refuse to run, loudly
"""
from __future__ import annotations

import fcntl
import os
import sys
from contextlib import contextmanager

from . import config


def _open_lock():
    path = config.UPLOAD_LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "w")


def _holder_hint() -> str:
    """Best-effort description of who holds the lock, for an error message."""
    try:
        return config.UPLOAD_LOCK_PATH.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


@contextmanager
def hold(owner: str = "media_sync"):
    """Context manager yielding True if the lock was acquired, False if held elsewhere.

    Non-blocking: an uploader that cannot get the lock should do nothing this cycle rather
    than queue up behind another one, because by the time it acquired the lock its ledger
    snapshot would be stale anyway.
    """
    fh = _open_lock()
    got = False
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
            fh.write(f"{owner} pid={os.getpid()}\n")
            fh.flush()
        except OSError:
            got = False
        yield got
    finally:
        if got:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def acquire_or_die(owner: str):
    """For maintenance scripts: take the lock or exit with an explanation.

    Returns the open file handle, which the caller must keep alive for the lock to persist
    (closing it releases the lock).
    """
    fh = _open_lock()
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(
            f"Refusing to run '{owner}': another uploader holds {config.UPLOAD_LOCK_PATH} "
            f"({_holder_hint()}).\n"
            f"Two uploaders spending the same pool free space is what overfills accounts.\n"
            f"Stop the daemon first:\n"
            f"  launchctl bootout gui/$(id -u)/com.mikeyferguson.mediasyncwatchdog\n"
            f"  launchctl kill SIGTERM gui/$(id -u)/com.mikeyferguson.mediasync"
        )
    fh.write(f"{owner} pid={os.getpid()}\n")
    fh.flush()
    return fh
