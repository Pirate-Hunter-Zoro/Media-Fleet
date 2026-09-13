#!/usr/bin/env python3
"""The YacReader index lock, both directions, against a temporary lock file.

WHY BOTH DIRECTIONS
    `is_held()` is the predicate the library supervisor uses to decide whether YacReader
    may run. A predicate that can never be true reads exactly like a permanently-free
    lock, and the supervisor would then cheerfully start the app on top of a tool editing
    the index -- the corruption this whole mechanism exists to prevent, with no symptom
    except a database that goes bad occasionally (§ diagnosis 7, 4.174, 4.187).

    So this asserts BOTH that a held lock reads held and that a free one reads free, that
    a second holder cannot take it, and that it is released when the block exits -- and it
    does all of it against a temp file, never the live state (§4.114: a regression test
    whose inputs are live mutable state cannot tell a broken guard from a repaired record).

    Never stops the real app: every case passes stop_app_first=False.

    python3 scripts/test_yacreader_lock.py
"""
from __future__ import annotations

import multiprocessing
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
import yacreader_db

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


def _hold(path_str: str, ready, release) -> None:
    """Hold the lock in a SEPARATE PROCESS -- flock is per-open-file-description, so a
    same-process probe would not exercise the exclusion the supervisor depends on."""
    config.YACREADER_DB_LOCK_FILE = Path(path_str)
    with yacreader_db.db_lock("test-holder", stop_app_first=False):
        ready.set()
        release.wait(30)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        lock_path = Path(td) / "yacreader_db.lock"
        config.YACREADER_DB_LOCK_FILE = lock_path

        check("a lock nobody has taken reads FREE", yacreader_db.is_held(), False)

        with yacreader_db.db_lock("test-self", stop_app_first=False):
            check("holder writes its purpose into the file",
                  "purpose=test-self" in yacreader_db.holder(), True)
        check("released after the block exits", yacreader_db.is_held(), False)

        ctx = multiprocessing.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_hold, args=(str(lock_path), ready, release))
        proc.start()
        try:
            if not ready.wait(30):
                print("  FAIL  holder process never acquired the lock")
                FAILURES.append("holder startup")
            else:
                check("a lock held by ANOTHER process reads HELD",
                      yacreader_db.is_held(), True)
                check("the holder's identity is readable",
                      "purpose=test-holder" in yacreader_db.holder(), True)

                # A second holder must be refused rather than allowed to overlap.
                config.YACREADER_DB_LOCK_TIMEOUT_SEC = 2
                t0 = time.time()
                try:
                    with yacreader_db.db_lock("test-second", stop_app_first=False):
                        check("a second holder was REFUSED", "acquired", "refused")
                except yacreader_db.LockUnavailable:
                    check("a second holder was REFUSED", "refused", "refused")
                check("refusal waited for the timeout rather than failing instantly",
                      time.time() - t0 >= 1.5, True)
        finally:
            release.set()
            proc.join(30)
            if proc.is_alive():
                proc.terminate()
                proc.join(10)

        check("free again once the holder exits", yacreader_db.is_held(), False)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} case(s): {', '.join(FAILURES)}")
        return 1
    print("all YacReader index-lock cases passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
