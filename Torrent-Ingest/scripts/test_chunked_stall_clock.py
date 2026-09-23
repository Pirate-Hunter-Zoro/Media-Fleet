#!/usr/bin/env python3
"""Regression test for the stall clock and the abandon deadline (2026-09-10, 2026-09-23).

WHAT WENT WRONG (1) -- THE CHUNKED CLOCK. A chunked pack is deliberately STOPPED between
waves while the finished wave is filed into the library -- so qBittorrent's `last_activity`,
which is the stall clock, goes stale by design, for as long as filing takes. With identify
capped that is hours. The next wave then resumed, `_abandon_stalled` read the clock left
over from before the pause, and destroyed the whole pack seconds later.

Measured on the live fleet: `[MTBB] Monogatari Series (BD 1080p)`, 103 files / 75 GB. Wave
enabled 09:11:55, reported "0%" at 09:12:16, and at 09:12:16 was failed as

    stalled 8h with no progress (no seeders/peers); abandoned to release the download budget

...while a tracker scrape at the same hour showed 450 seeders and adding the identical
`.torrent` by hand pulled 10 MB/s immediately. The swarm was never the problem. Worse, the
abandon path called `qbt.remove(delete_files=True)`, so every partly-fetched byte went too.

THE FIX (1). Each wave stamps `wave_started_at`, and the deadline runs from the later of
that and `last_activity` -- so it measures "this WAVE has been failing to fetch", not "this
pack has been idle".

WHAT WENT WRONG (2) -- THE "NO COMPLETE COPY" DEADLINE. `_abandon_stalled` also selected a
4h deadline whenever qBittorrent's `availability` was below 1, on the premise that this
means the swarm has no complete copy. It does not: availability is the pieces held by the
peers this client is CURRENTLY CONNECTED to plus our own, so during any stall it collapses
to our own completion fraction and reads < 1 even in a swarm full of seeders. Every stalled
torrent therefore got the 4h path. Four slow-but-alive Bob's Burgers packs (S01 at 45%,
S02 at 22%, S03 at 1%, S06 at 0%) were killed during an ordinary overnight lull on
2026-09-23, and `delete_files=True` deleted their partial payloads with the torrent, so
every re-drop restarted from zero and stalled again -- the "they'll likely fail again" loop
the owner reported.

THE FIX (2). One deadline (`STALL_ABANDON_SEC`, 24h) selected by peer activity alone, and
the abandon keeps the partial payload (`delete_files=False`) so a re-drop resumes. The
janitor reclaims an untried directory after its own 7-day grace.

BOTH DIRECTIONS (§4.5), because a stall guard that can no longer fire lets a genuinely dead
torrent pin the download budget forever, which is the deadlock it was written to break:

  Part 1 -- a freshly-resumed wave is NOT abandoned, however stale the inherited clock.
  Part 2 -- a wave that really has been failing past the deadline IS abandoned.
  Part 3 -- non-chunked behaviour, including the 2026-09-23 regression: a sub-24h stall
            with availability < 1 is KEPT, and every abandon keeps the partial bytes.

    python3 scripts/test_chunked_stall_clock.py

No qBittorrent, no network, no journal writes: every collaborator is stubbed.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    def __init__(self, state="stalledDL", last_activity_ago=None, availability=1.0,
                 num_complete=1, completed=0):
        self.state = state
        self.last_activity = (time.time() - last_activity_ago
                              if last_activity_ago is not None else None)
        self.availability = availability
        self.num_complete = num_complete
        self.completed = completed
        self.progress = 0.5


def would_abandon(record, torrent):
    """Run `_abandon_stalled` with every side effect stubbed. Returns (failed, delete_files)
    where `failed` is True if it failed the record -- i.e. if it would have removed the
    torrent -- and `delete_files` is what it asked qBittorrent to do with the payload."""
    seen = {"removed": False, "delete_files": None, "failed": False}

    real_remove, real_fail = ingest.qbt.remove, ingest._fail
    try:
        def fake_remove(*a, **k):
            seen["removed"] = True
            seen["delete_files"] = k.get("delete_files")
        ingest.qbt.remove = fake_remove
        ingest._fail = lambda *a, **k: seen.__setitem__("failed", True)
        ingest._STALL_SINCE.clear()
        out = ingest._abandon_stalled(dict(record), torrent, client=None)
    finally:
        ingest.qbt.remove, ingest._fail = real_remove, real_fail
    # the return value and the destructive call must agree
    if out != seen["failed"]:
        failures.append("abandon return value disagrees with _fail being called")
    return out, seen["delete_files"]


NOW = time.time()
CHUNKED = {"info_hash": "a" * 40, "name": "Big Pack", "chunked": True}
PLAIN = {"info_hash": "b" * 40, "name": "Ordinary Torrent"}

print("Part 1 -- a freshly-resumed wave survives a stale inherited clock")
# The 2026-09-10 live failure: 8h since the last wave's activity, wave enabled seconds
# ago, availability 0 because no peer has connected yet.
rec = dict(CHUNKED, wave_started_at=NOW - 21)
check("wave enabled 21s ago, last_activity 8h old, availability 0",
      would_abandon(rec, FakeTorrent(last_activity_ago=8 * HOUR, availability=0.0))[0], False)
check("wave enabled 2m ago, last_activity 30h old, availability 0",
      would_abandon(dict(CHUNKED, wave_started_at=NOW - 120),
                    FakeTorrent(last_activity_ago=30 * HOUR, availability=0.0))[0], False)
check("wave enabled 3h ago (inside the 24h deadline)",
      would_abandon(dict(CHUNKED, wave_started_at=NOW - 3 * HOUR),
                    FakeTorrent(last_activity_ago=40 * HOUR, availability=0.0))[0], False)

print("\nPart 2 -- a wave that really is failing is STILL abandoned")
check("wave enabled 25h ago, availability 0 (deadline is activity, not availability)",
      would_abandon(dict(CHUNKED, wave_started_at=NOW - 25 * HOUR),
                    FakeTorrent(last_activity_ago=25 * HOUR, availability=0.0))[0], True)
check("wave enabled 25h ago, availability 1",
      would_abandon(dict(CHUNKED, wave_started_at=NOW - 25 * HOUR),
                    FakeTorrent(last_activity_ago=25 * HOUR, availability=1.0))[0], True)
check("stopped between waves for 30h with no wave ever enabled",
      would_abandon(dict(CHUNKED), FakeTorrent(state="stoppedDL",
                                               last_activity_ago=30 * HOUR,
                                               availability=0.0))[0], True)

print("\nPart 3 -- non-chunked records: the 2026-09-23 regression")
check("plain torrent, 25h stalled -> abandoned",
      would_abandon(PLAIN, FakeTorrent(last_activity_ago=25 * HOUR,
                                       availability=1.0))[0], True)
check("plain torrent, 1h stalled -> kept",
      would_abandon(PLAIN, FakeTorrent(last_activity_ago=1 * HOUR,
                                       availability=1.0))[0], False)
# The exact shape that killed the four Bob's Burgers packs: hours of quiet with
# availability < 1 because no complete peer is connected, and partial bytes on disk.
check("plain torrent, 8h stalled, availability 0.45, num_complete 0 -> KEPT",
      would_abandon(PLAIN, FakeTorrent(last_activity_ago=8 * HOUR, availability=0.45,
                                       num_complete=0, completed=7_910_324_870))[0], False)
check("plain torrent, 8h stalled, no seeder known, 0 bytes fetched -> KEPT",
      would_abandon(PLAIN, FakeTorrent(last_activity_ago=8 * HOUR, availability=0.0,
                                       num_complete=0, completed=0))[0], False)

print("\nPart 4 -- an abandon never deletes the partial payload")
_, delete_files = would_abandon(PLAIN, FakeTorrent(last_activity_ago=25 * HOUR))
check("delete_files passed to qbt.remove", delete_files, False)
_, delete_files = would_abandon(dict(CHUNKED, wave_started_at=NOW - 30 * HOUR,
                                     chunk_active=[0]),
                                FakeTorrent(last_activity_ago=30 * HOUR))
check("chunked wave abandon keeps its bytes too", delete_files, False)

print("\nControl -- a torrent that is actually downloading is never touched")
for st in ("downloading", "forcedDL", "metaDL", "uploading"):
    check(f"state {st!r} -> never abandoned",
          would_abandon(dict(CHUNKED, wave_started_at=NOW - 99 * HOUR),
                        FakeTorrent(state=st, last_activity_ago=99 * HOUR,
                                    availability=0.0))[0], False)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
