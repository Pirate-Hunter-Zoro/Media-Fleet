#!/usr/bin/env python3
"""The stall clock: a week to prove the swarm can serve it, then patience forever.

THE POLICY (owner decision, 2026-09-26). A torrent gets `STALL_FIRST_PROGRESS_GRACE_SEC`
-- one week -- to fetch its FIRST byte. A torrent that has fetched anything is NEVER
abandoned, however long its swarm goes quiet: a public/DHT-only pack that stalled at 70%
has proven it can be served, and its partial payload is the one thing a retry cannot
recreate cheaply. The owner's words: "give a torrent a week to start, and if it makes no
progress by then, at THAT point we can kill it. But once it makes progress, give it all
the time in the world."

WHY THE OLD CLOCK WENT AWAY. Two wrong rules preceded this one, both measured live:

  * THE "NO COMPLETE COPY" 4h DEADLINE selected by qBittorrent's `availability < 1`.
    Availability is the pieces held by the peers THIS client is currently connected to
    plus our own, so during any stall it collapses to our own completion fraction and
    reads < 1 even in a swarm full of seeders. Four slow-but-alive Bob's Burgers packs
    (S01 45%, S02 22%, S03 1%, S06 0% at failure, 2026-09-23) were killed during an
    ordinary overnight lull.

  * THE 24h "NO PEER ACTIVITY" DEADLINE on `last_activity`. It destroyed the partial
    payloads of those four packs (`delete_files=True`), so every re-drop restarted from
    zero -- and on 2026-09-26 it killed Bob's Burgers S01 at 70% after a 28h seeder gap,
    which is exactly the slow-but-alive shape the owner says to wait out.

The abandon that remains is ONLY for a torrent that has never fetched a byte, because
that one can never advance and keeps its unfetched bytes reserved in `_remaining_budget`
-- for a chunked pack, the whole active wave -- so it holds the admission queue forever.
Its clock is qBittorrent's own `added_on`, so a daemon restart cannot re-arm it. Every
abandon keeps the bytes (`delete_files=False`): nothing is ever destroyed by this path.

    python3 scripts/test_chunked_stall_clock.py

No qBittorrent, no network, no journal writes: every collaborator is stubbed.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                       # noqa: E402
import ingest                                                        # noqa: E402

failures: list[str] = []
DAY = 86400.0


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


class FakeTorrent:
    def __init__(self, state="stalledDL", added_days_ago=None, progress=0.0,
                 downloaded=0, completed=0):
        self.state = state
        self.added_on = (time.time() - added_days_ago * DAY
                         if added_days_ago is not None else None)
        self.progress = progress
        self.downloaded = downloaded
        self.completed = completed


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


with_bytes = dict(
    info_hash="a" * 40, name="Slow But Alive",
    chunked=True, chunk_done=[0, 1, 2],
    created_at="2026-09-01T00:00:00+00:00",
)
fresh = dict(info_hash="b" * 40, name="Fresh And Stalled",
             created_at="2026-09-26T00:00:00+00:00")
plain = dict(info_hash="c" * 40, name="Ordinary Torrent",
             created_at="2026-09-26T00:00:00+00:00")

print("Part 1 -- a torrent that has fetched anything is NEVER abandoned")
# The 2026-09-26 live failure: Bob's Burgers S01 at 70%, 28h of no peer activity, stopped
# and would have been failed by the old 24h clock.
check("70% on disk, stalled a month -> kept",
      would_abandon(with_bytes, FakeTorrent(added_days_ago=30, progress=0.70))[0], False)
check("a single fetched byte is enough",
      would_abandon(dict(with_bytes, chunk_done=[]),
                    FakeTorrent(added_days_ago=99, downloaded=1))[0], False)
check("qBittorrent's completed counter alone keeps it",
      would_abandon(dict(with_bytes, chunk_done=[]),
                    FakeTorrent(added_days_ago=99, completed=5))[0], False)
check("record evidence survives a torrent re-add that reset the live counters",
      would_abandon(with_bytes, FakeTorrent(added_days_ago=99))[0], False)
check("a chunked pack stopped between waves for a month is kept",
      would_abandon(with_bytes, FakeTorrent(state="stoppedDL", added_days_ago=30))[0],
      False)

print("\nPart 2 -- a torrent that has fetched NOTHING drains after the week")
check("zero bytes, added 8 days ago -> abandoned",
      would_abandon(fresh, FakeTorrent(added_days_ago=8))[0], True)
check("zero bytes, added 8 days ago, stopped between waves -> abandoned",
      would_abandon(fresh, FakeTorrent(state="stoppedDL", added_days_ago=8))[0], True)
check("zero bytes, added 8 days ago, errored -> abandoned",
      would_abandon(fresh, FakeTorrent(state="error", added_days_ago=8))[0], True)
check("zero bytes, added 6 days ago -> kept (inside the grace)",
      would_abandon(fresh, FakeTorrent(added_days_ago=6))[0], False)
check("the grace window really is a week",
      config.STALL_FIRST_PROGRESS_GRACE_SEC, 7 * 24 * 3600)
# No `added_on` and no record creation time: the in-memory anchor is the fallback, and it
# must not read as "already a week old" on its first look.
check("a clockless torrent is not failed on first sight",
      would_abandon({"info_hash": "d" * 40, "name": "Clockless"},
                    FakeTorrent(added_days_ago=None))[0], False)

print("\nPart 3 -- a torrent that is actually downloading is never touched")
for st in ("downloading", "forcedDL", "metaDL", "uploading"):
    check(f"state {st!r} -> never abandoned",
          would_abandon(fresh, FakeTorrent(state=st, added_days_ago=99))[0], False)

print("\nPart 4 -- an abandon never deletes any payload")
# The never-started case has no bytes today, but the same path serves a re-drop whose
# bytes arrived after the clock started; deleting them is the one thing it must not do.
_, delete_files = would_abandon(fresh, FakeTorrent(added_days_ago=8))
check("delete_files passed to qbt.remove", delete_files, False)
_, delete_files = would_abandon(dict(fresh, chunk_active=[0]),
                                FakeTorrent(added_days_ago=8, state="stoppedDL"))
check("a chunked wave abandon keeps its bytes too", delete_files, False)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
