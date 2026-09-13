#!/usr/bin/env python3
"""Regression test for a chunked pack's inherited progress (§4.31, 2026-09-09).

A chunked pack records per-file progress in `chunk_done` so a re-drop does not re-fetch
hundreds of GB it already filed. `chunk_done` is a claim about the PAST (§4.10 one layer
down), and the commonest reason to re-drop a pack BY HAND is that its content is gone --
purged, or never filed at all. Carrying the claim then turns the owner's re-acquisition
into a silent no-op that reports COMPLETED.

That is what happened to `[MTBB] Monogatari Series (BD 1080p)`: purged 2026-07-28, then
re-dropped on 09-03, 09-07 and 09-08, each time inheriting `chunk_done` = all 103 files
and completing in ~21 seconds having fetched, identified and filed nothing -- over a
library that has not held it since July. Four titles were lost the same way (Monogatari,
Higurashi, Bakugan Armored Alliance, Log Horizon); Young Sheldon S04 inherited the same
way and its content really WAS still there, which is why the fix must prove per file
rather than refuse to carry at all.

Three parts, one per thing that can silently rot:

**Part 1 -- what is carried is what can be proven.** An index is carried only when the
record can name where it landed AND that destination is still in the library, or when the
pack deliberately declined the file. Asserts BOTH directions (§4.5): a proven index IS
carried, a gone one is NOT, and a legacy record that can prove nothing carries nothing.

**Part 2 -- presence is judged on the MOUNT.** `~/Media` missing means EVICTED, not
deleted (§4.1), so the SSD cannot answer "is this still in the library". Asserts the mount
is consulted, that the SSD is only ever a second YES, and that a mount which cannot answer
narrows what is provable instead of inverting it.

**Part 3 -- a pack that proved nothing must not read as success.** `_finish_chunked` used
to check only `chunk_failed`, so "every file accounted for" passed as "all waves
ingested". Asserts a pack that can name neither a destination it filed nor a file it
declined FAILS, and -- the control, because a gate that always fires is not a gate -- that
a pack which filed something, and one whose files were all legitimately declined, still
COMPLETE.

    python3 scripts/test_chunk_progress_proof.py

Read-only: touches no journal, no library, no daemon. Exit 0 means every check passed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                    # noqa: E402
import ingest                                                    # noqa: E402
import journal                                                   # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def carry(old):
    """_carry_chunk_progress against a fresh record, as registration calls it."""
    fresh: dict = {}
    n = ingest._carry_chunk_progress(old, fresh)
    return n, fresh


# A real library-relative path and one that is certainly absent. The present one is
# discovered rather than hard-coded: a fixture naming a title the owner may purge would
# rot into a test that passes because it can no longer find anything (§4.5).
def _a_real_relpath():
    shows = config.MEDIAFS_MOUNT / "Shows"
    for title in sorted(shows.iterdir()) if shows.is_dir() else []:
        if not title.is_dir():
            continue
        for season in sorted(title.iterdir()):
            if not season.is_dir():
                continue
            for f in sorted(season.iterdir()):
                if f.suffix.lower() in config.REAP_VIDEO_EXTENSIONS:
                    return str(f.relative_to(config.MEDIAFS_MOUNT))
    return None


PRESENT = _a_real_relpath()
ABSENT = "Shows/__no_such_title__ (1899)/Season 01/__no_such_title__ - S01E01.mkv"

print("Part 0: fixtures")
if PRESENT is None:
    print("  FAIL  could not find any real episode on the mount to test against")
    failures.append("fixture")
    print("\nFAILED")
    raise SystemExit(1)
print(f"  ok    present fixture: {PRESENT[:70]}")
check("absent fixture really is absent", (config.MEDIAFS_MOUNT / ABSENT).exists(), False)

# --------------------------------------------------------------------------------------
print("\nPart 1: what is carried is what can be proven")

# The Monogatari record exactly as the journal held it: chunk_done full, no per-file
# evidence of any kind. It must carry NOTHING and re-fetch.
n, fresh = carry({"name": "legacy pack", "chunked": True,
                  "chunk_done": list(range(103))})
check("legacy record (no chunk_filed) carries nothing", n, 0)
check("legacy record leaves chunk_done unset", "chunk_done" in fresh, False)
check("legacy record still marks chunk_intent", fresh.get("chunk_intent"), True)

# Per-file, in one record: index 0 provable, index 1 gone.
n, fresh = carry({"name": "half-gone pack", "chunked": True, "chunk_done": [0, 1],
                  "chunk_filed": {"0": PRESENT, "1": ABSENT}})
check("a provable index is carried", n, 1)
check("only the provable index is carried", fresh.get("chunk_done"), [0])
check("carried chunk_filed is pruned to match", sorted(fresh.get("chunk_filed", {})), ["0"])

# A deliberate decline needs no library check: re-fetching a creditless opening to decline
# it again buys nothing, and it will never be "in the library" to find.
n, fresh = carry({"name": "pack with junk", "chunked": True, "chunk_done": [0, 1, 2],
                  "chunk_filed": {"0": PRESENT}, "chunk_dropped": [1, 2]})
check("deliberate declines are carried", n, 3)
check("declines survive as declines", fresh.get("chunk_dropped"), [1, 2])

# A file given up on UNFILED is missing from the library and getting it is the point of
# the retry -- it must never come back as done, even with a filed record naming it.
n, fresh = carry({"name": "pack with a failure", "chunked": True, "chunk_done": [0, 1],
                  "chunk_filed": {"0": PRESENT, "1": PRESENT},
                  "chunk_failed_idx": [1]})
check("chunk_failed_idx is never carried", fresh.get("chunk_done"), [0])

# chunk_filed can only ever ADD confidence; it can never promote an index the old record
# did not consider done in the first place.
n, fresh = carry({"name": "over-claiming filed map", "chunked": True, "chunk_done": [0],
                  "chunk_filed": {"0": PRESENT, "7": PRESENT}})
check("chunk_filed cannot promote an index outside chunk_done", fresh.get("chunk_done"), [0])

check("a non-chunked record is untouched",
      carry({"name": "whole-torrent", "chunked": False, "chunk_done": [0, 1]}), (0, {}))

# Garbage in the map must not crash registration, and must not be trusted either.
n, _ = carry({"name": "corrupt map", "chunked": True, "chunk_done": [0, 1],
              "chunk_filed": {"nope": PRESENT, "1": ""}})
check("unparseable / empty chunk_filed entries carry nothing", n, 0)

# --------------------------------------------------------------------------------------
print("\nPart 2: presence is judged on the mount, not the SSD")

check("a file on the mount reads present", ingest._still_in_library(PRESENT)[0], True)
check("a file on neither root reads absent", ingest._still_in_library(ABSENT)[0], False)
check("a live mount is reported live", ingest._still_in_library(PRESENT)[1], True)

# EVICTION: the whole point of §4.1. A file the pool holds but the SSD does not must read
# PRESENT. Proven by construction rather than by hunting for a live evicted file: point
# MEDIA_ROOT at an empty dir so only the mount can answer, and the answer must not change.
real_media_root = ingest.config.MEDIA_ROOT
try:
    ingest.config.MEDIA_ROOT = Path("/nonexistent-ssd-root")
    check("an evicted file (mount yes, SSD no) still reads present",
          ingest._still_in_library(PRESENT)[0], True)
finally:
    ingest.config.MEDIA_ROOT = real_media_root

# A mount that cannot answer must narrow what is provable, never invert it: absent, and
# SAID to be unverified, so the caller re-fetches instead of assuming either way.
real_mount = ingest.config.MEDIAFS_MOUNT
try:
    ingest.config.MEDIAFS_MOUNT = Path("/nonexistent-mount-root")
    present, alive = ingest._still_in_library(ABSENT)
    check("with the mount down, absence is not claimed as fact", (present, alive),
          (False, False))
    # ...and the SSD still gets to say yes, so a mount blip does not re-fetch everything.
    ingest.config.MEDIA_ROOT = real_mount
    check("with the mount down the SSD can still prove presence",
          ingest._still_in_library(PRESENT)[0], True)
finally:
    ingest.config.MEDIAFS_MOUNT = real_mount
    ingest.config.MEDIA_ROOT = real_media_root

check("a destination outside MEDIA_ROOT is not recorded",
      ingest._media_relpath("/tmp/elsewhere/foo.mkv"), None)
check("a destination under MEDIA_ROOT records its relpath",
      ingest._media_relpath(str(config.MEDIA_ROOT / "Shows/X/Season 01/X - S01E01.mkv")),
      "Shows/X/Season 01/X - S01E01.mkv")

# --------------------------------------------------------------------------------------
print("\nPart 3: a pack that proved nothing must not read as success")


class _FakeFile:
    def __init__(self, i):
        self.index = i
        self.name = f"f{i}.mkv"


def settle(record):
    """_finish_chunked with the world stubbed out; returns the status it settled on."""
    removed = []
    real = (ingest.qbt.remove, ingest.journal.write_record,
            ingest._file_torrent_finished, ingest._fail)
    try:
        ingest.qbt.remove = lambda *a, **k: removed.append(True)
        ingest.journal.write_record = lambda r: None

        def _fake_fail(rec, msg):
            rec["status"] = journal.FAILED
            rec["error"] = msg

        ingest._file_torrent_finished = lambda r: None
        ingest._fail = _fake_fail
        ingest._finish_chunked(record, object(), [_FakeFile(i) for i in range(3)])
    finally:
        (ingest.qbt.remove, ingest.journal.write_record,
         ingest._file_torrent_finished, ingest._fail) = real
    return record.get("status"), record.get("error") or "", bool(removed)

# The Monogatari shape: every file "done", nothing filed, nothing declined.
status, err, removed = settle({"info_hash": "h0", "name": "proved nothing", "chunk_done": [0, 1, 2]})
check("a pack that proved nothing FAILS", status, journal.FAILED)
check("...and says why in words the owner can act on",
      ("inherited progress" in err and "Re-drop" in err), True)
check("...and the torrent is still removed (its bytes are gone either way)", removed, True)

# The controls. A gate that fires on everything is not a gate (§4.29).
status, _, _ = settle({"info_hash": "h1", "name": "filed something", "chunk_done": [0, 1, 2],
                       "chunk_filed": {"0": PRESENT}})
check("CONTROL: a pack that filed something COMPLETES", status, journal.COMPLETED)

status, _, _ = settle({"info_hash": "h2", "name": "all declined", "chunk_done": [0, 1, 2],
                       "chunk_dropped": [0, 1, 2]})
check("CONTROL: a pack whose files were all declined COMPLETES", status, journal.COMPLETED)

# chunk_failed still wins: it is the more specific, older complaint.
status, err, _ = settle({"info_hash": "h3", "name": "has failures", "chunk_done": [0, 1, 2],
                         "chunk_filed": {"0": PRESENT}, "chunk_failed": ["f1.mkv"]})
check("an unfiled-file failure still reports as such", status, journal.FAILED)
check("...naming the unfiled file, not the inherited-progress reason",
      "could not be filed" in err, True)

# --------------------------------------------------------------------------------------
print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("All checks passed.")
