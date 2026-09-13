#!/usr/bin/env python3
"""Regression test for the absolute-run-split placement guard (2026-09-10).

WHAT IT CATCHES. One continuous episode run (1..26 for a whole franchise) chopped across
several season folders. The signature is CHAINING -- season N+1's first episode being
exactly season N's last plus one, repeatedly. Season-relative numbering restarts near 1
every season, so seasons overlapping each other's ranges is the normal case and proves
nothing; chaining is what a model produces when it knows a franchise has several arcs but
cannot map arcs onto provider seasons.

WHERE IT CAME FROM. `[MTBB] Monogatari Series (BD 1080p)`, 103 files, run deliberately as
the hardest naming case in the library. The free identify chain filed the single
26-episode arc "Monogatari Series Second Season" across Seasons 04, 05, 07, 08, 09 and 10
as episodes 1-23 -- leaving Season 09 holding 18,19,20,21,23 and Season 10 holding only 22.
A permanent hole in a season the owner watches, authored confidently, with correct titles
and plots on every single file. Nothing in the plan was malformed; it was incoherent.

BOTH DIRECTIONS (§4.5), and the second one is the expensive half:

  Part 1 -- it FIRES on the real Monogatari shape and on synthetic chained runs.
  Part 2 -- it does NOT fire on ordinary libraries: per-season numbering, two-season
            shows, specials, single absolute-numbered seasons, partial waves.
  Part 3 -- ZERO REGRESSIONS over every completed plan in the real journal. This is the
            claim that matters: a placement guard that rejects real history would stop
            the fleet filing anything, and 92 plans is what the FIRST version of this rule
            would have rejected before it was narrowed to chaining.

    python3 scripts/test_absolute_run_split.py

Read-only. Exit 0 means every check passed.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import library                                                       # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def rejects(files):
    """True if the guard rejects this file list."""
    try:
        library._reject_absolute_run_split(files)
        return False
    except library.PlanError:
        return True


def plan(*specs):
    """specs are 'Show/S01E02' style shorthands."""
    out = []
    for sp in specs:
        show, tag = sp.split("/")
        out.append({"dst_rel": f"Shows/{show}/Season {tag[1:3]}/{show} - {tag}.mkv"})
    return out


print("Part 1 -- it fires on a split absolute run")
# The real Monogatari shape: S04=1-5, S05=6-9, S07=10-13, S08=14-17, S09=18-23, S10=22
mono = plan(*[f"Mono/S04E0{n}" for n in range(1, 6)],
            *[f"Mono/S05E0{n}" for n in range(6, 10)],
            *[f"Mono/S07E{n:02d}" for n in range(10, 14)],
            *[f"Mono/S08E{n:02d}" for n in range(14, 18)],
            *[f"Mono/S09E{n:02d}" for n in (18, 19, 20, 21, 23)],
            "Mono/S10E22")
check("the real Monogatari placement is rejected", rejects(mono), True)
check("a clean three-season chain 1-5/6-10/11-15 is rejected",
      rejects(plan(*[f"X/S01E{n:02d}" for n in range(1, 6)],
                   *[f"X/S02E{n:02d}" for n in range(6, 11)],
                   *[f"X/S03E{n:02d}" for n in range(11, 16)])), True)

print("\nPart 1c -- a run split across WAVES is caught (the on-disk half)")
# A chunked pack is filed a wave at a time, so a split run can be split across waves too:
# seasons 4 and 5 in one plan, 7, 8 and 9 in the next, each too small to trip the test on
# its own. The guard merges what is already on disk for the show before judging.
import tempfile, os
import config as _cfg

def with_disk(existing, plan_files):
    """existing: {season: (lo,hi)} already on disk for 'Mono'; returns rejects(plan)."""
    td = tempfile.mkdtemp()
    root = Path(td) / "Shows" / "Mono"
    for season, (lo, hi) in existing.items():
        d = root / f"Season {season:02d}"
        d.mkdir(parents=True, exist_ok=True)
        for e in range(lo, hi + 1):
            (d / f"Mono - S{season:02d}E{e:02d}.mkv").write_bytes(b"\0")
    saved = _cfg.MEDIA_ROOT
    try:
        _cfg.MEDIA_ROOT = Path(td)
        return rejects(plan_files)
    finally:
        _cfg.MEDIA_ROOT = saved

wave2 = plan(*[f"Mono/S07E{n:02d}" for n in range(10, 14)],
             *[f"Mono/S08E{n:02d}" for n in range(14, 18)])
check("wave 2 alone is too small to trip the test", rejects(wave2), False)
check("but with waves 1-2 already on disk, the chain is caught",
      with_disk({4: (1, 5), 5: (6, 9)}, wave2), True)
check("and a clean library plus a clean wave is still fine",
      with_disk({1: (1, 12), 2: (1, 12)},
                plan(*[f"Mono/S03E{n:02d}" for n in range(1, 13)])), False)

print("\nPart 2 -- it does NOT fire on ordinary libraries")
check("normal per-season numbering (every season starts at 1)",
      rejects(plan(*[f"Y/S01E{n:02d}" for n in range(1, 27)],
                   *[f"Y/S02E{n:02d}" for n in range(1, 25)],
                   *[f"Y/S03E{n:02d}" for n in range(1, 23)])), False)
check("a two-season show can never trip it (needs 3+ seasons)",
      rejects(plan(*[f"Z/S01E{n:02d}" for n in range(1, 6)],
                   *[f"Z/S02E{n:02d}" for n in range(6, 11)])), False)
check("one chained pair is not enough",
      rejects(plan(*[f"W/S01E{n:02d}" for n in range(1, 6)],
                   *[f"W/S02E{n:02d}" for n in range(6, 11)],
                   *[f"W/S03E{n:02d}" for n in range(1, 6)])), False)
check("a single absolute-numbered season is fine",
      rejects(plan(*[f"V/S01E{n:03d}" for n in range(1, 120)])), False)
check("specials (Season 00) are exempt",
      rejects(plan(*[f"U/S00E{n:02d}" for n in range(1, 6)],
                   *[f"U/S01E{n:02d}" for n in range(6, 11)],
                   *[f"U/S02E{n:02d}" for n in range(11, 16)],
                   *[f"U/S03E{n:02d}" for n in range(1, 5)])), False)
check("an empty plan is fine", rejects([]), False)
check("a movies-only plan is fine",
      rejects([{"dst_rel": "Movies/A Film (2001)/A Film (2001).mkv"}]), False)

print("\nPart 3 -- ZERO regressions over the real journal")
jp = Path.home() / "Developer/Media-Fleet/Torrent-Ingest/state/journal.jsonl"
records = {}
if jp.exists():
    for line in jp.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:                                            # noqa: BLE001
            continue
        if r.get("info_hash"):
            records[r["info_hash"]] = r

completed = [r for r in records.values() if r.get("status") == "completed"]
examined, rejected, names = 0, 0, []
for r in completed:
    files = (r.get("plan") or {}).get("files") or r.get("applied") or []
    if not files:
        continue
    examined += 1
    if rejects(files):
        rejected += 1
        names.append(r.get("name", "")[:70])
check("the corpus is real and non-trivial", examined > 300, True)
if rejected:
    for n in names[:10]:
        print(f"        would reject: {n}")
check(f"none of {examined} completed plans is rejected", rejected, 0)

# ...and the corpus must be able to produce a rejection, or Part 3 proves nothing (§4.5).
check("the guard CAN still fire over corpus-shaped input",
      rejects(mono + [{"dst_rel": "Shows/Other/Season 01/Other - S01E01.mkv"}]), True)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
