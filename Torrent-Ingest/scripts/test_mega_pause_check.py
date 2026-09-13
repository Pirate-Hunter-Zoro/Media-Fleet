#!/usr/bin/env python3
"""Regression test for `check_mega`'s reaper-pause reasoning (2026-09-10).

The MEGA free-space cache goes stale whenever Media-Syncer stops writing it. The check
existed to catch a stuck sync loop -- but the reaper KILLS Media-Syncer for the whole of a
purge, and a drain has run for five days, so for days at a time the check reported a fault
that was in fact the fleet working exactly as designed. A warning that is always present is
a warning nobody reads, which is how a real one would have been missed.

`check_mega` now subtracts the pause, and asserts what the pause could be hiding:

  * marker present, reaper ALIVE -> silent. Expected.
  * marker present, reaper GONE  -> ACTION. A LEAKED pause: `mediasync` carries no
    KeepAlive and its watchdog stands down while the marker exists, so replication stays
    stopped forever. This fault previously had NO detector -- it was invisible behind the
    warning it now replaces.
  * no marker, cache stale       -> WARN. The original fault, still caught.
  * no marker, cache fresh       -> silent.

All four asserted, because a check that only ever goes quiet is indistinguishable from one
that cannot speak (§4.5). Every branch is driven with a FAKE marker path and a FAKE reaper
probe: nothing here reads the real fleet state, kills anything, or touches the pool.

    python3 scripts/test_mega_pause_check.py

Read-only. Exit 0 means every check passed.
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                                                        # noqa: E402
import fleet_health as fh                                            # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def run_case(cache_age_sec, marker_present, reaper_alive, tmp):
    """Drive check_mega with every input faked; returns [(severity, message), ...]."""
    cache = Path(tmp) / "mega_free_space.txt"
    cache.write_text("fake")
    ts = time.time() - cache_age_sec
    import os
    os.utime(cache, (ts, ts))

    marker = Path(tmp) / "reap_ms_paused"
    if marker_present:
        marker.write_text("2026-09-10T03:42:52+00:00")
    elif marker.exists():
        marker.unlink()

    saved = (fh.FREE_SPACE_FILE, config.REAP_PAUSED_MARKER, fh._reaper_is_draining)
    try:
        fh.FREE_SPACE_FILE = cache
        config.REAP_PAUSED_MARKER = marker
        fh._reaper_is_draining = lambda: reaper_alive
        return fh.check_mega()
    finally:
        fh.FREE_SPACE_FILE, config.REAP_PAUSED_MARKER, fh._reaper_is_draining = saved


STALE = fh.MEGA_STALE_SEC + 3600      # comfortably past the threshold
FRESH = 60

with tempfile.TemporaryDirectory() as tmp:
    print("the pause is subtracted -- but only while the reaper is really there")
    r = run_case(STALE, marker_present=True, reaper_alive=True, tmp=tmp)
    check("stale cache + paused + reaper draining -> silent", r, [])

    print("\na LEAKED pause is an ACTION (this fault had no detector before)")
    r = run_case(STALE, marker_present=True, reaper_alive=False, tmp=tmp)
    check("stale cache + paused + NO reaper -> exactly one issue", len(r), 1)
    check("  ...and it is an ACTION", r[0][0] if r else None, "ACTION")
    check("  ...naming the leaked pause", "leaked" in (r[0][1] if r else ""), True)
    # a FRESH cache with a leaked marker is still a leak: replication is still stopped,
    # the cache is just not old enough to have shown it yet.
    r = run_case(FRESH, marker_present=True, reaper_alive=False, tmp=tmp)
    check("fresh cache + paused + NO reaper -> still an ACTION",
          bool(r) and r[0][0] == "ACTION", True)

    print("\nthe ORIGINAL fault is still caught (the check can still speak)")
    r = run_case(STALE, marker_present=False, reaper_alive=False, tmp=tmp)
    check("stale cache + no pause -> exactly one issue", len(r), 1)
    check("  ...and it is a WARN", r[0][0] if r else None, "WARN")
    check("  ...naming the stale cache", "stale" in (r[0][1] if r else "").lower()
          or "old" in (r[0][1] if r else "").lower(), True)

    print("\nand a healthy fleet is silent")
    r = run_case(FRESH, marker_present=False, reaper_alive=False, tmp=tmp)
    check("fresh cache + no pause -> silent", r, [])
    r = run_case(FRESH, marker_present=True, reaper_alive=True, tmp=tmp)
    check("fresh cache + paused + reaper draining -> silent", r, [])

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
