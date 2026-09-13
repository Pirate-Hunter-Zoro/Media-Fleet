#!/usr/bin/env python3
"""Regression test: a One Pace re-cut REPLACES the episode it supersedes, even when
its title changed (2026-09-12).

THE CASE. One Pace is the library's lone churn class: unlike every other show
(write-once), a repeat drop under `Shows/One Pace (2013)/` is meant to replace the cut
on disk. `_overwrites_preexisting` grants that, and `apply_plan` does it gaplessly.

But `_collapse_existing_episode_collisions` (added 2026-08-26 to stop the SAME episode
accumulating under two filenames) had no One Pace carve-out. One Pace filenames carry
the episode title, so a re-cut whose title changed produces a DIFFERENT destination at
the same slot -- and the guard dropped it as a duplicate before a byte was written. The
replacement then silently never landed: no error, nothing on disk, nothing to notice.

That is not hypothetical. One Pace re-released ch. 141-145 "Quack Doctor" as ch. 140-145
"Inherited Will" at S13E05, and both cuts sat in Season 13 for a month.

`prompts/identify.md` does tell the run to reuse the existing filename. That is an
instruction to a free model inside a 67,000-character prompt, and this fleet's standing
lesson is that the harness must COMPUTE what it needs rather than ask for it. So the
collapse guard now retargets a One Pace collision onto the existing path instead of
dropping it, and apply_plan's replace branch overwrites it.

BOTH DIRECTIONS (§4.5) -- the second and third parts are what keep the carve-out from
becoming a hole in the write-once invariant:
  Part 1 -- a One Pace re-cut under a NEW title is retargeted onto the existing file
            and replaces it, instead of being dropped.
  Part 2 -- a One Pace re-cut reusing the SAME filename is untouched (apply_plan's own
            preexisting/replace path already handles that one).
  Part 3 -- a NON-One-Pace same-slot duplicate is still DROPPED. This is the guard's
            original job and the carve-out must not widen to it.
  Part 4 -- a One Pace slot ALREADY holding two files is ambiguous (replacing one
            would leave the duplicate standing), so it still drops and reports.
  Part 5 -- a planned file whose slot is genuinely free is never touched.

    python3 scripts/test_onepace_recut_replaces.py

Writes only inside a temp dir. Exit 0 means every check passed.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                          # noqa: E402
import library                                                         # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


OP = "Shows/One Pace (2013)/Season 13"
QUACK = "One Pace (2013) - S13E05 - Quack Doctor.mkv"
INHERIT = "One Pace (2013) - S13E05 - Inherited Will.mkv"

tmp = Path(tempfile.mkdtemp(prefix="onepace-recut-test-")).resolve()
saved_root = config.MEDIA_ROOT
try:
    config.MEDIA_ROOT = tmp

    def seed(rel_dir, *names):
        d = tmp / rel_dir
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"x")
        return d

    def run(dst_rel, season, episode, with_abs=True):
        f = {"dst_rel": dst_rel, "season": season, "episode": episode}
        if with_abs:
            f["_dst_abs"] = str(tmp / dst_rel)
        kept, dropped = library._collapse_existing_episode_collisions([f])
        return kept, dropped, f

    # ----------------------------------------------------------------------
    print("Part 1 -- a re-cut under a NEW title is retargeted, not dropped")
    seed(OP, QUACK)
    kept, dropped, f = run(f"{OP}/{INHERIT}", 13, 5)
    check("it is kept", len(kept), 1)
    check("nothing was dropped", len(dropped), 0)
    check("dst_rel now points at the file it replaces", f["dst_rel"], f"{OP}/{QUACK}")
    check("_dst_abs was retargeted too (apply_plan reads this one)",
          Path(f["_dst_abs"]).name, QUACK)
    check("and apply_plan is allowed to overwrite that path",
          library._overwrites_preexisting(f["dst_rel"]), True)

    # ----------------------------------------------------------------------
    print("\nPart 2 -- a re-cut reusing the SAME filename is left alone")
    kept, dropped, f = run(f"{OP}/{QUACK}", 13, 5)
    check("it is kept", len(kept), 1)
    check("dst_rel is unchanged", f["dst_rel"], f"{OP}/{QUACK}")

    # ----------------------------------------------------------------------
    print("\nPart 3 -- a NON-One-Pace same-slot duplicate is STILL dropped")
    HB = "Shows/Helluva Boss (2020)/Season 02"
    seed(HB, "Helluva Boss (2020) - S02E05.mkv")
    kept, dropped, f = run(f"{HB}/Helluva Boss (2020) - S02E05 - Unhappy Campers.mkv", 2, 5)
    check("it is dropped", len(dropped), 1)
    check("and not kept", len(kept), 0)

    # a show whose name merely CONTAINS "One Pace" must not inherit the carve-out
    FAKE = "Shows/One Pace Behind The Scenes (2020)/Season 01"
    seed(FAKE, "One Pace Behind The Scenes (2020) - S01E01.mkv")
    kept, dropped, f = run(f"{FAKE}/One Pace Behind The Scenes (2020) - S01E01 - Pilot.mkv", 1, 1)
    check("a different show under a similar name is still dropped", len(dropped), 1)

    # ----------------------------------------------------------------------
    print("\nPart 4 -- a One Pace slot already holding TWO files is ambiguous")
    seed(OP, "One Pace (2013) - S13E05 - Another Cut.mkv")     # now QUACK + Another
    kept, dropped, f = run(f"{OP}/One Pace (2013) - S13E05 - Third Cut.mkv", 13, 5)
    check("it is dropped rather than guessing which to replace", len(dropped), 1)
    check("and not kept", len(kept), 0)

    # ----------------------------------------------------------------------
    print("\nPart 5 -- a genuinely free slot is untouched")
    kept, dropped, f = run(f"{OP}/One Pace (2013) - S13E09 - Brand New.mkv", 13, 9)
    check("it is kept", len(kept), 1)
    check("nothing was dropped", len(dropped), 0)
    check("dst_rel is unchanged", f["dst_rel"], f"{OP}/One Pace (2013) - S13E09 - Brand New.mkv")

finally:
    config.MEDIA_ROOT = saved_root
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for x in failures:
        print(f"  - {x}")
    sys.exit(1)
print("One Pace re-cuts replace: all checks passed.")
