#!/usr/bin/env python3
"""Regression test for the chunked stall clock (2026-09-10).

WHAT WENT WRONG. A chunked pack is deliberately STOPPED between waves while the finished
wave is filed into the library -- so qBittorrent's `last_activity`, which is the stall
clock, goes stale by design, for as long as filing takes. With identify capped that is
hours. The next wave then resumed, `_abandon_stalled` read the clock left over from before
the pause, and destroyed the whole pack seconds later.

Measured on the live fleet: `[MTBB] Monogatari Series (BD 1080p)`, 103 files / 75 GB. Wave
enabled 09:11:55, reported "0%" at 09:12:16, and at 09:12:16 was failed as

    stalled 8h with no progress (no seeders/peers); abandoned to release the download budget

...while a tracker scrape at the same hour showed 450 seeders and adding the identical
`.torrent` by hand pulled 10 MB/s immediately. The swarm was never the problem. Worse, the
abandon path calls `qbt.remove(delete_files=True)`, so every partly-fetched byte went too,
and `_abandon_stalled`'s own comment asserted chunked records could not even reach it.

THE FIX. Each wave stamps `wave_started_at`, and the deadline runs from the later of that
and `last_activity` -- so it measures "this WAVE has been failing to fetch", not "this pack
has been idle".

BOTH DIRECTIONS (§4.5), because a stall guard that can no longer fire lets a genuinely dead
torrent pin the download budget forever, which is the deadlock it was written to break:

  Part 1 -- a freshly-resumed wave is NOT abandoned, however stale the inherited clock.
  Part 2 -- a wave that really has been failing for longer than the deadline IS abandoned.
  Part 3 -- non-chunked behaviour is untouched, in both directions.

    python3 scripts/test_chunked_stall_clock.py

No qBittorrent, no network, no journal writes: every collaborator is stubbed.
"""

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import ingest                                                        # noqa: E402

failures: list[str] = []
HOUR = 3600.0


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


class FakeTorrent:
    def __init__(self, state="stalledDL", last_activity_ago=None, availability=1.0):
        self.state = state
        self.last_activity = (time.time() - last_activity_ago
                              if last_activity_ago is not None else None)
        self.availability = availability
        self.progress = 0.5


def would_abandon(record, torrent):
    """Run `_abandon_stalled` with every side effect stubbed. Returns True if it failed
    the record -- i.e. if it would have deleted the payload."""
    removed = {"called": False}
    failed = {"called": False}

    real_remove, real_fail = ingest.qbt.remove, ingest._fail
    try:
        ingest.qbt.remove = lambda *a, **k: removed.__setitem__("called", True)
        ingest._fail = lambda *a, **k: failed.__setitem__("called", True)
        ingest._STALL_SINCE.clear()
        out = ingest._abandon_stalled(dict(record), torrent, client=None)
    finally:
        ingest.qbt.remove, ingest._fail = real_remove, real_fail
    # the return value and the destructive call must agree
    if out != failed["called"]:
        failures.append("abandon return value disagrees with _fail being called")
    return out


NOW = time.time()
CHUNKED = {"info_hash": "a" * 40, "name": "Big Pack", "chunked": True}
PLAIN = {"info_hash": "b" * 40, "name": "Ordinary Torrent"}

print("Part 1 -- a freshly-resumed wave survives a stale inherited clock")
# The exact live failure: 8h since the last wave's activity, wave enabled seconds ago,
# availability 0 because no peer has connected yet (the 4h threshold, the harsher one).
rec = dict(CHUNKED, wave_started_at=NOW - 21)
check("wave enabled 21s ago, last_activity 8h old, availability 0",
      would_abandon(rec, FakeTorrent(last_activity_ago=8 * HOUR, availability=0.0)), False)
check("wave enabled 2m ago, last_activity 30h old, availability 0",
      would_abandon(dict(CHUNKED, wave_started_at=NOW - 120),
                    FakeTorrent(last_activity_ago=30 * HOUR, availability=0.0)), False)
check("wave enabled 3h ago (under the 4h no-complete deadline)",
      would_abandon(dict(CHUNKED, wave_started_at=NOW - 3 * HOUR),
                    FakeTorrent(last_activity_ago=40 * HOUR, availability=0.0)), False)

print("\nPart 2 -- a wave that really is failing is STILL abandoned")
check("wave enabled 5h ago, no complete copy (4h deadline)",
      would_abandon(dict(CHUNKED, wave_started_at=NOW - 5 * HOUR),
                    FakeTorrent(last_activity_ago=5 * HOUR, availability=0.0)), True)
check("wave enabled 25h ago, complete copy present (24h deadline)",
      would_abandon(dict(CHUNKED, wave_started_at=NOW - 25 * HOUR),
                    FakeTorrent(last_activity_ago=25 * HOUR, availability=1.0)), True)
check("stopped between waves for 30h with no wave ever enabled",
      would_abandon(dict(CHUNKED), FakeTorrent(state="stoppedDL",
                                               last_activity_ago=30 * HOUR,
                                               availability=0.0)), True)

print("\nPart 3 -- non-chunked records behave exactly as before")
check("plain torrent, 25h stalled, complete copy -> abandoned",
      would_abandon(PLAIN, FakeTorrent(last_activity_ago=25 * HOUR, availability=1.0)), True)
check("plain torrent, 1h stalled -> kept",
      would_abandon(PLAIN, FakeTorrent(last_activity_ago=1 * HOUR, availability=1.0)), False)
check("plain torrent, 5h stalled, no complete copy -> abandoned",
      would_abandon(PLAIN, FakeTorrent(last_activity_ago=5 * HOUR, availability=0.0)), True)

print("\nControl -- a torrent that is actually downloading is never touched")
for st in ("downloading", "forcedDL", "metaDL", "uploading"):
    check(f"state {st!r} -> never abandoned",
          would_abandon(dict(CHUNKED, wave_started_at=NOW - 99 * HOUR),
                        FakeTorrent(state=st, last_activity_ago=99 * HOUR,
                                    availability=0.0)), False)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
