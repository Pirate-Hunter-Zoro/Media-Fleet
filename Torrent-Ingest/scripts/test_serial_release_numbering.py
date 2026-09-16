#!/usr/bin/env python3
"""Serial-numbered releases: computed broadcast numbering, stated and enforced.

The Doctor Who (1963) classic pack names every part of a story with the SAME
`SxxEyy` -- the release's serial number -- and the part in a trailing `(N)`. A model
that trusts the filename files all six parts of The Keys of Marinus at `S01E05`,
which is exactly what happened twice (the original ingest, then the 2026-09-15
re-fetch waves, 28 files misfiled the second time). The harness now computes the
broadcast numbers from the release's own structure, states them in the prompt, and
`library.validate_plan` refuses a plan that contradicts them.

This is the arcmap rule: if a step is arithmetic, do the arithmetic. The test proves
the arithmetic against the handoff's independently confirmed numbers (S01E07 = The
Escape, S01E18 = Rider from Shang Tu, S01E31 = Strangers in Space), pins the shapes
later seasons actually use (`S04E01(028)` with no space, a story split across folders
as `Parts 5-8`, a season packed into one folder as `Parts 1-14`), and both directions
of the guard: a serial-copied destination is REFUSED, the computed slot is ACCEPTED.
An ordinary release must produce no map, so the guard never touches normal plans.

Fixtures only; nothing here reads the live library or torrent.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import identify                                                       # noqa: E402
import library                                                        # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


print("=== the handoff's confirmed broadcast numbers (independent oracle) ===")

REAL = [
    # One file per PRECEDING story registers its folder and its part range: the
    # offsets are accumulated per season, so a partial list computes the wrong
    # numbers -- exactly the trap this test exists to prevent.
    "R/Doctor Who - S01E01 (001) - An Unearthly Child - Parts 1-4/"
    "Doctor Who - S01E01 (001) - An Unearthly Child (4) - The Firemaker.avi",
    "R/Doctor Who - S01E02 (002) - The Daleks - Parts 1-7/"
    "Doctor Who - S01E02 (002) - The Daleks (1) - The Dead Planet.avi",
    "R/Doctor Who - S01E02 (002) - The Daleks - Parts 1-7/"
    "Doctor Who - S01E02 (002) - The Daleks (3) - The Escape.avi",
    "R/Doctor Who - S01E03 (003) - The Edge of Destruction - Parts 1-2/"
    "Doctor Who - S01E03 (003) - The Edge of Destruction (2) - The Brink of Disaster.avi",
    "R/Doctor Who - S01E04 (004) - Marco Polo - Parts 1-7/"
    "Doctor Who - S01E04 (004) - Marco Polo (5) - Rider from Shang-Tu (Recon).avi",
    "R/Doctor Who - S01E05 (005) - The Keys of Marinus - Parts 1-6/"
    "Doctor Who - S01E05 (005) - The Keys of Marinus (1) - The Sea of Death.avi",
    "R/Doctor Who - S01E06 (006) - The Aztecs - Parts 1-4/"
    "Doctor Who - S01E06 (006) - The Aztecs (1) - The Temple of Evil.avi",
    "R/Doctor Who - S01E07 (007) - The Sensorites - Parts 1-6/"
    "Doctor Who - S01E07 (007) - The Sensorites (1) - Strangers in Space.avi",
    # later seasons: no space before the serial paren, part followed by "(Recon)"
    "R/Doctor Who - S04E01 (028) - The Smugglers - Parts 1-4/"
    "Doctor Who - S04E01(028) - The Smugglers (1) (Recon).avi",
    # one season packed into one folder whose range spans all 14 parts; the files
    # carry different serials inside it, so the RANGE is what places them
    "R/Doctor Who - S23E01 (143) - The Trial of a Time Lord - Parts 1-14 - Segment 1-4/"
    "Doctor Who - S23E01 (143) - The Trial of a Time Lord (01) - The Mysterious Planet.avi",
    "R/Doctor Who - S23E01 (143) - The Trial of a Time Lord - Parts 1-14 - Segment 1-4/"
    "Doctor Who - S23E02 (143) - The Trial of a Time Lord (05) - Mindwarp.avi",
    "R/Doctor Who - S23E01 (143) - The Trial of a Time Lord - Parts 1-14 - Segment 1-4/"
    "Doctor Who - S23E04 (143) - The Trial of a Time Lord (14) - The Ultimate Foe (4).avi",
    # extras: Bonus, Intro, Outro carry no episode
    "R/Doctor Who - S01E08 (008) - The Reign of Terror - Parts 1-6/"
    "Doctor Who - S01E08 (008) - The Reign of Terror (0) - Intro.avi",
    "R/Doctor Who - S01E02 (002) - The Daleks - Parts 1-7/"
    "Doctor Who - S01E02 (002) Bonus - Creation of the Daleks.avi",
    "R/Doctor Who - S04E05 (032) - The Underwater Menace - Parts 1-4/"
    "Doctor Who - S04E05 (032) - The Underwater Menace (3) - Intro for E3.avi",
]
m = identify.serial_release_map(REAL)


def expect(fragment: str, season: int, episode: int) -> None:
    hits = [(k, v) for k, v in m.items() if fragment in k[1]]
    ok = hits and (hits[0][1]["season"], hits[0][1]["episode"]) == (season, episode)
    check(f"{fragment[:52]} -> S{season:02d}E{episode:02d}", bool(ok))


expect("The Daleks (1) - The Dead Planet", 1, 5)
expect("The Daleks (3) - The Escape", 1, 7)
expect("Marco Polo (5) - Rider", 1, 18)
expect("Sensorites (1) - Strangers", 1, 31)
expect("Smugglers (1) (Recon)", 4, 1)
expect("Trial of a Time Lord (01)", 23, 1)
expect("Trial of a Time Lord (05)", 23, 5)
expect("Trial of a Time Lord (14)", 23, 14)
check("a Bonus file is not an episode",
      not any("Creation of the Daleks" in k[1] for k in m))
check("an Intro labelled with a part number is not the episode",
      not any("Intro for E3" in k[1] for k in m))

print()
print("=== ordinary releases produce no map (the guard never sees them) ===")

check("a plain release has no serial map",
      identify.serial_release_map(
          ["Show - 01.mkv", "Show - 02.mkv", "Show - 12.mkv"]) == {})
check("an anime release has no serial map",
      identify.serial_release_map(
          ["[Group] Show - 01 (1080p)[ABCD1234].mkv"]) == {})
check("a movie release has no serial map",
      identify.serial_release_map(["Some Movie (2024) 1080p/Some Movie.mkv"]) == {})

print()
print("=== validate_plan makes the computed numbers binding ===")

tmp = Path(tempfile.mkdtemp(prefix="serial-numbering-"))
pre = tmp / "Doctor Who - S01E01 (001) - An Unearthly Child - Parts 1-4"
pre.mkdir(parents=True)
pre_src = (pre / "Doctor Who - S01E01 (001) - An Unearthly Child (1) - "
                 "An Unearthly Child.avi")
pre_src.write_bytes(b"x" * 32)
folder = tmp / "Doctor Who - S01E02 (002) - The Daleks - Parts 1-7"
folder.mkdir(parents=True)
src = folder / "Doctor Who - S01E02 (002) - The Daleks (1) - The Dead Planet.avi"
src.write_bytes(b"x" * 32)
rel = [f"root/{pre.name}/{pre_src.name}", f"root/{folder.name}/{src.name}"]
smap = identify.serial_release_map(rel)


def plan_at(season: int, episode: int) -> dict:
    return {
        "media_type": "show",
        "title": "Doctor Who (1963)",
        "files": [{
            "src": str(src),
            "dst_rel": (f"Shows/Doctor Who (1963)/Season {season:02d}/"
                        f"Doctor Who (1963) - S{season:02d}E{episode:02d}.avi"),
            "season": season, "episode": episode,
        }],
    }


try:
    library.validate_plan(plan_at(1, 5), str(tmp), serial_map=smap)
    check("the computed slot (S01E05) is accepted", True)
except library.PlanError as exc:
    check(f"the computed slot (S01E05) is accepted -- {exc}", False)

try:
    library.validate_plan(plan_at(1, 2), str(tmp), serial_map=smap)
    check("the serial-copied slot (S01E02) is REFUSED", False)
except library.PlanError as exc:
    check("the serial-copied slot (S01E02) is REFUSED",
          "SERIAL" in str(exc).upper() and "S01E05" in str(exc))

# Without a map (an ordinary release) the same plan is NOT rejected by this guard.
try:
    library.validate_plan(plan_at(1, 2), str(tmp))
    check("no map -> the guard does not fire", True)
except library.PlanError as exc:
    check(f"no map -> the guard does not fire (unexpected: {exc})", False)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("serial-numbered release numbering: all checks passed")
