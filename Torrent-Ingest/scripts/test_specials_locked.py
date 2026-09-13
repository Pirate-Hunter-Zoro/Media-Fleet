#!/usr/bin/env python3
"""Regression test: a Season-0 special's LOCK must survive contact with Jellyfin
(2026-09-11).

THE CASE. `library.validate_plan` refuses a Season-0 file that carries no
`episode_title`, and says why: provider Season-0 ordering is unreliable, so an
un-owned special gets mis-scraped. `_write_owned_nfo` holds up the other half —
it writes `<lockdata>true</lockdata>` for EVERY Season-0 special regardless of the
plan's `owned` flag, and has since the initial commit.

Both halves worked. The specials were still wrong.

WHAT WAS ACTUALLY BROKEN — measured on 2026-09-11, not inferred. Every Season-0
sidecar under `Mushi-Shi (2005)` and `Monogatari Series (2009)` was Jellyfin-authored
(a BOM, `<dateadded>`, `<fileinfo><streamdetails>` — none of which this code emits)
and every one said `lockdata=false`, carrying scraped titles offset from the files
they sat beside. Jellyfin's DB agreed: `LockedFields` empty on all six items.

The Shows library runs `SaveLocalMetadata=True` with `EnableRealtimeMonitor=True`.
Jellyfin decides whether an item is locked from the .nfo it finds AT THE MOMENT it
first indexes the video, and then writes its own .nfo back over the path. apply_plan
moved the video in Phase 2 and wrote the sidecar in Phase 3 — so for the whole of a
long multi-file Phase 2 the video sat there uncovered. Jellyfin indexed it, created
the item unlocked, scraped it, and clobbered the sidecar we were about to write. The
lock was lost silently, and nothing re-reads a sidecar Jellyfin has already replaced.

So the fix is an ORDERING one: the locked sidecar goes down BEFORE its video
(Phase 2a). A sidecar with no video beside it is inert to Jellyfin, so landing it
early costs nothing, and by the time the video appears the lock is already there to
be read.

BOTH DIRECTIONS (§4.5):
  Part 1 -- the per-file lock decision fires on the two classes that must be locked,
            and does NOT fire on an ordinary un-owned episode or a non-show file.
  Part 2 -- the sidecar it writes really does carry lockdata=true, title and plot.
  Part 3 -- THE REGRESSION GUARD. Run a real apply_plan against a temp media root
            with `os.replace` instrumented, and assert that at the instant each
            video lands its locked sidecar is ALREADY on disk. This is the only part
            that would have caught the live bug; Parts 1 and 2 passed throughout it.
  Part 4 -- a plain un-owned episode still gets NO sidecar, so Jellyfin keeps
            scraping the main series exactly as before.

    python3 scripts/test_specials_locked.py

Writes only inside a temp dir. Exit 0 means every check passed.
"""

import os
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


# --------------------------------------------------------------------------
print("Part 1 -- which files get a LOCKED sidecar, decided per file")

_ROOT = config.MEDIA_ROOT.resolve()


def locks(owned, season, rel):
    return library._locks_episode_nfo({"owned": owned}, {"season": season}, _ROOT / rel)


SHOW = "Shows/Some Show (2009)/Season 00/Some Show (2009) - S00E01.mkv"
EP = "Shows/Some Show (2009)/Season 01/Some Show (2009) - S01E01.mkv"

check("a Season-0 special in an UN-OWNED plan is locked (the whole point)",
      locks(False, 0, SHOW), True)
check("a Season-0 special in an owned plan is locked",
      locks(True, 0, SHOW), True)
check("season 0 as a STRING is still a special",
      locks(False, "0", SHOW), True)
check("an ordinary episode of an OWNED show is locked",
      locks(True, 1, EP), True)

check("an ordinary episode of an UN-OWNED show is NOT locked",
      locks(False, 1, EP), False)
check("a movie is never locked by the episode writer",
      locks(True, 0, "Movies/Some Film (2009)/Some Film (2009).mkv"), False)
check("a comic is never locked by the episode writer",
      locks(True, 0, "Comics/Manga/Something/Something v01.cbz"), False)
check("a show-shaped path that is not a video is not locked",
      locks(True, 0, "Shows/Some Show (2009)/Season 00/Some Show (2009) - S00E01.txt"),
      False)
check("a missing/unparseable season is not a special",
      locks(False, None, SHOW), False)


# --------------------------------------------------------------------------
print("\nPart 2 -- the sidecar it writes carries the lock, the title and the plot")

f = {"season": 0, "episode": 2, "episode_title": "Path of Thorns",
     "plot": "Ginko walks a road nobody finishes."}
xml = library._episode_nfo_xml(f, "Some Show")
check("lockdata=true", "<lockdata>true</lockdata>" in xml, True)
check("the supplied title is used", "<title>Path of Thorns</title>" in xml, True)
check("the supplied plot is used",
      "<plot>Ginko walks a road nobody finishes.</plot>" in xml, True)
check("season 0 is written as 0", "<season>0</season>" in xml, True)


# --------------------------------------------------------------------------
print("\nPart 3 -- THE GUARD: the sidecar is on disk BEFORE its video lands")

# .resolve() matters: on macOS the temp dir is /var/... which is a symlink to
# /private/var/..., and apply_plan compares destinations against a RESOLVED root.
tmp = Path(tempfile.mkdtemp(prefix="specials-lock-test-")).resolve()
saved = (config.MEDIA_ROOT, config.NOVELS_ROOT, os.replace,
         library.dbhook.record_plan)
# When the video appears: was its .nfo already there, and was it locked?
seen: list[tuple[str, bool, bool]] = []


def _watching_replace(src, dst):
    # `_atomic_write` uses os.replace too, so this hook sees the sidecar writes as
    # well. Only a VIDEO landing is the moment under test -- that is when Jellyfin
    # can first index the item and decide whether it is locked.
    dst_p = Path(dst)
    if dst_p.suffix.lower() in config.VIDEO_EXTENSIONS:
        nfo = dst_p.with_suffix(".nfo")
        present = nfo.exists()
        locked = present and "<lockdata>true</lockdata>" in nfo.read_text(encoding="utf-8")
        seen.append((dst_p.name, present, locked))
    return saved[2](src, dst)


try:
    config.MEDIA_ROOT = tmp / "Media"
    config.NOVELS_ROOT = tmp / "Novels"
    library.os.replace = _watching_replace
    library.dbhook.record_plan = lambda plan: None          # never touch library.db

    src_dir = tmp / "src"
    src_dir.mkdir(parents=True)
    show = "Monogatari Series (2009)"

    # An UN-OWNED plan (`owned: False`) placing three Season-0 specials and one
    # ordinary episode -- the exact shape that lost its lock on the live library.
    files = []
    for i, (season, ep, title) in enumerate([
            (0, 1, "Tsubasa Family"),
            (0, 2, "Tsubasa Cat (3)"),
            (0, 3, "Tsubasa Cat (4)"),
            (1, 1, "Hitagi Crab (1)")]):
        sd = "Season 00" if season == 0 else "Season 01"
        name = f"{show} - S{season:02d}E{ep:02d}.mkv"
        src = src_dir / f"in{i}.mkv"
        src.write_bytes(b"x" * (4096 + i))
        dst = config.MEDIA_ROOT / "Shows" / show / sd / name
        files.append({
            "dst_rel": f"Shows/{show}/{sd}/{name}",
            "season": season, "episode": ep, "episode_title": title,
            "plot": f"A real plot for {title}.",
            "_src_abs": str(src), "_dst_abs": str(dst),
            "_src_size": src.stat().st_size,
        })

    plan = {"title": show, "year": 2009, "owned": False, "files": files}
    library.apply_plan(plan, "testhash0000")

    check("every file in the plan was placed", len(seen), 4)
    specials = [s for s in seen if "S00E" in s[0]]
    check("all three specials were seen landing", len(specials), 3)
    for name, present, locked in specials:
        check(f"{name}: its sidecar existed before the video landed", present, True)
        check(f"{name}: and that sidecar was already LOCKED", locked, True)

    # ...and the sidecars are still right once the whole plan has been applied.
    for fl in files:
        nfo = Path(fl["_dst_abs"]).with_suffix(".nfo")
        if fl["season"] == 0:
            txt = nfo.read_text(encoding="utf-8")
            check(f"S00E{fl['episode']:02d}: final sidecar is locked",
                  "<lockdata>true</lockdata>" in txt, True)
            check(f"S00E{fl['episode']:02d}: final sidecar keeps OUR title",
                  f"<title>{fl['episode_title']}</title>" in txt, True)

    # ----------------------------------------------------------------------
    print("\nPart 4 -- an ordinary un-owned episode is still left to the scraper")
    ep1 = next(fl for fl in files if fl["season"] == 1)
    check("no sidecar was written for the un-owned Season-1 episode",
          Path(ep1["_dst_abs"]).with_suffix(".nfo").exists(), False)
    name, present, locked = next(s for s in seen if "S01E" in s[0])
    check("and none existed when it landed either", present, False)

finally:
    config.MEDIA_ROOT, config.NOVELS_ROOT = saved[0], saved[1]
    library.os.replace = saved[2]
    library.dbhook.record_plan = saved[3]
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for x in failures:
        print(f"  - {x}")
    sys.exit(1)
print("specials stay locked: all checks passed.")
