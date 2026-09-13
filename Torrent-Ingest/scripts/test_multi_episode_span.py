#!/usr/bin/env python3
"""Regression test for multi-episode coverage in the duplicate/misfiled check (2026-09-10).

A multi-episode file (`SxxE01-E02 - A & B.mkv`) carries ONE `.nfo`, and by Jellyfin's
convention that sidecar names only the FIRST episode inside it. The coverage check read
that bare number as the file's whole claim, so every SECOND slot of every pair file looked
like a placement fault: slot E02 asked "do you claim E02?", `S01E01-E02`'s nfo said "I am
E01", and the check told the owner to re-file a file that was exactly where it belonged.
On The Powerpuff Girls that was ~37 of 75 NEEDS REVIEW items, all false.

This is §4.26 one level deeper. That lesson fixed a check that read the FILENAME when it
should have read the file's own record; this fixes the same check reading that record
without understanding what it is a record OF.

Both directions, because a check that stops crying wolf is worthless if it also stops
barking (§4.5):

  Part 1 -- a legitimate multi-episode file is NOT a placement fault, at either slot.
  Part 2 -- a genuinely misfiled file is STILL caught, including one whose `.nfo` puts it
            in a different SEASON entirely (the real Helluva Boss S02E05 -> S01E08 case).
  Part 3 -- two files that both genuinely cover one slot are still reported as duplicates.

Fixtures only: builds .mkv/.nfo pairs in a temp dir. Touches no real media.

    python3 scripts/test_multi_episode_span.py

Exit 0 means every check passed.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import media_doctor as md                                            # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def make(tmp, name, season, episode):
    """A video file plus the .nfo the fleet would have written beside it."""
    v = Path(tmp) / f"{name}.mkv"
    v.write_bytes(b"\x00" * 16)
    (Path(tmp) / f"{name}.nfo").write_text(
        f"<episodedetails><season>{season}</season>"
        f"<episode>{episode}</episode></episodedetails>", encoding="utf-8")
    return v


def covers(video, slot_season, slot_ep):
    """What the patched logic concludes for one file at one slot."""
    span = md._parse_span(video.name)
    nfo_span = md._nfo_covered_span(video, span)
    return md._nfo_covers(nfo_span, slot_season, slot_ep)


with tempfile.TemporaryDirectory() as tmp:
    print("Part 1 -- a legitimate multi-episode file covers its WHOLE span")
    pair = make(tmp, "Show (1998) - S01E01-E02 - Monkey See & Mommy Fearest", 1, 1)
    check("pair file covers its first slot  E01", covers(pair, 1, 1), True)
    check("pair file covers its second slot E02", covers(pair, 1, 2), True)
    check("pair file does NOT cover E03",        covers(pair, 1, 3), False)

    triple = make(tmp, "Show (1998) - S02E05-E07 - A & B & C", 2, 5)
    check("three-episode file covers E05", covers(triple, 2, 5), True)
    check("three-episode file covers E06", covers(triple, 2, 6), True)
    check("three-episode file covers E07", covers(triple, 2, 7), True)
    check("three-episode file does NOT cover E08", covers(triple, 2, 8), False)

    print("\nPart 2 -- a genuinely misfiled file is still caught")
    # The real Helluva Boss case: filename says S02E05, its own .nfo says S01E08.
    misfiled = make(tmp, "Helluva Boss (2020) - S02E05", 1, 8)
    check("wrong-season file does NOT cover its filename slot",
          covers(misfiled, 2, 5), False)
    check("...and it DOES cover what its nfo names", covers(misfiled, 1, 8), True)
    # A one-off shift inside the same season.
    shifted = make(tmp, "Helluva Boss (2020) - S02E06", 2, 5)
    check("shifted file does NOT cover its filename slot", covers(shifted, 2, 6), False)
    # A multi-episode file that is ALSO misfiled must not be excused by its width.
    bad_pair = make(tmp, "Show (1998) - S03E01-E02 - A & B", 3, 7)
    check("a misfiled PAIR file is not excused by its span",
          covers(bad_pair, 3, 1), False)

    print("\nPart 3 -- a real single-slot duplicate is still a duplicate")
    single_a = make(tmp, "Show (1998) - S01E01", 1, 1)
    check("single file covers its own slot", covers(single_a, 1, 1), True)
    check("...and so does the pair file overlapping it", covers(pair, 1, 1), True)
    check("both cover E01, so this is a DUPLICATE, not a misfiling",
          covers(single_a, 1, 1) and covers(pair, 1, 1), True)

    print("\nControl -- a file with no .nfo yields no claim either way")
    bare = Path(tmp) / "Show (1998) - S09E09.mkv"
    bare.write_bytes(b"\x00")
    check("no sidecar -> no span", md._nfo_covered_span(bare, md._parse_span(bare.name)),
          None)
    check("no span -> covers nothing", md._nfo_covers(None, 9, 9), False)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
