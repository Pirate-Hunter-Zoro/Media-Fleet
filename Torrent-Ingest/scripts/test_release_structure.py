#!/usr/bin/env python3
"""Regression test for the release-structure analyser (2026-09-10).

WHY IT EXISTS. `[MTBB] Monogatari Series (BD 1080p)` was filed with one 26-episode arc
spread across six season folders as absolute episodes 1-23 -- Season 09 left holding
18,19,20,21,23 and Season 10 holding only 22. Every file carried a correct title and plot;
the PLAN was incoherent.

The cause is a real conflict the release contains: it splits one broadcast season into
named ARC folders while the filenames inside number episodes absolutely ACROSS them. The
model had to spot that from 103 paths inside a 90,000-character prompt, and did not.

It should not have had to. Which folders share a filename label, and whether their numbers
form one run, is arithmetic. The harness computes it and states it as a fact, leaving the
model the judgement that is actually left: one season, or a season per arc.

BOTH DIRECTIONS (§4.5) -- a detector that cannot stay quiet is as useless as one that
cannot speak:
  Part 1 -- it FIRES on the real Monogatari layout, naming the right folders and run.
  Part 2 -- it stays SILENT on ordinary releases: a flat season pack, per-season folders
            that each restart at 1, a single folder, a single file.

    python3 scripts/test_release_structure.py

Fixtures only: builds empty files in a temp tree. Reads no real media.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import identify                                                      # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def tree(paths):
    """A temp dir containing every path as an empty .mkv; returns the analyser's output."""
    td = tempfile.mkdtemp()
    for rel in paths:
        f = Path(td) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\0")
    return identify._release_structure_block(td)


print("Part 1 -- the real Monogatari shape is detected")
MONO = []
for folder, label, lo, hi in [
        ("01 - Bakemonogatari", "Bakemonogatari", 1, 15),
        ("02 - Kizumonogatari", "Kizumonogatari", 1, 3),
        ("03 - Nisemonogatari", "Nisemonogatari", 1, 11),
        ("04 - Nekomonogatari (Black)", "Nekomonogatari (Black)", 1, 4),
        ("05 - Nekomonogatari (White)", "Monogatari Series Second Season", 1, 5),
        ("06 - Kabukimonogatari", "Monogatari Series Second Season", 6, 9),
        ("07 - Hanamonogatari", "Hanamonogatari", 1, 5),
        ("08 - Otorimonogatari", "Monogatari Series Second Season", 10, 13),
        ("09 - Onimonogatari", "Monogatari Series Second Season", 14, 17),
        ("10 - Koimonogatari", "Monogatari Series Second Season", 18, 23),
        ("11 - Tsukimonogatari", "Tsukimonogatari", 1, 4)]:
    for n in range(lo, hi + 1):
        MONO.append(f"{folder}/[MTBB] {label} - {n:02d} [ABCD1234].mkv")

out = tree(MONO)
check("a conflict is reported", "SPLIT/NUMBERING CONFLICT" in out, True)
check("it names the shared label",
      "'Monogatari Series Second Season'" in out, True)
check("it reports the run as 01-23", "01-23" in out, True)
check("it lists the 5 offending folders",
      all(f in out for f in ("05 - Nekomonogatari (White)", "06 - Kabukimonogatari",
                             "08 - Otorimonogatari", "09 - Onimonogatari",
                             "10 - Koimonogatari")), True)
check("it does NOT accuse the standalone arcs",
      "Bakemonogatari', and their" not in out and "'Hanamonogatari', and their" not in out,
      True)
check("it offers both legal resolutions",
      "ONE season holding all" in out and "RENUMBERED from 01" in out, True)

print("\nPart 1b -- a chunked WAVE still sees the whole release")
# The failure this parameter exists for: a chunked pack is identified one wave at a time
# and only the wave's files are on disk. Monogatari's first wave is 32 of 103 files and
# contains NONE of the later conflicting folders, so from disk the conflict is invisible.
wave_only = [p for p in MONO if p.startswith(("01 - ", "02 - ", "03 - ", "04 - "))]
check("the wave alone shows no conflict (that is the trap)",
      "SPLIT/NUMBERING CONFLICT" in tree(wave_only), False)
check("but the full release list does, even with only the wave on disk",
      "SPLIT/NUMBERING CONFLICT" in identify._release_structure_block(
          "/nonexistent-path", release_files=MONO), True)
check("and it says so, so the model files only what is on disk",
      "ENTIRE release" in identify._release_structure_block(
          "/nonexistent-path", release_files=MONO), True)

print("\nPart 2 -- ordinary releases produce NO conflict")
per_season = [f"Season {s:02d}/Show - S{s:02d}E{e:02d}.mkv"
              for s in (1, 2, 3) for e in range(1, 13)]
check("per-season folders that each restart at 1",
      "SPLIT/NUMBERING CONFLICT" in tree(per_season), False)

# folders whose labels differ: two genuinely separate shows in one pack
two_shows = ([f"Show A/[G] Show A - {n:02d} [X].mkv" for n in range(1, 13)]
             + [f"Show B/[G] Show B - {n:02d} [X].mkv" for n in range(1, 13)])
check("two different shows, each numbered from 1",
      "SPLIT/NUMBERING CONFLICT" in tree(two_shows), False)

# one label but numbers that OVERLAP rather than chain -- duplicates, not a split run
overlap = ([f"Disc 1/[G] Show - {n:02d} [X].mkv" for n in range(1, 7)]
           + [f"Disc 2/[G] Show - {n:02d} [X].mkv" for n in range(1, 7)])
check("same label, overlapping numbers (not a chain)",
      "SPLIT/NUMBERING CONFLICT" in tree(overlap), False)

check("a single folder yields no structure block", tree(
    [f"Season 01/Show - S01E{e:02d}.mkv" for e in range(1, 13)]) == "", True)
check("a flat pack yields no structure block",
      tree([f"Show - S01E{e:02d}.mkv" for e in range(1, 13)]) == "", True)

print("\nControl -- the chain detector CAN fire on a minimal two-folder case")
minimal = ([f"Part 1/[G] Run - {n:02d} [X].mkv" for n in range(1, 5)]
           + [f"Part 2/[G] Run - {n:02d} [X].mkv" for n in range(5, 9)])
check("two chained folders are reported",
      "SPLIT/NUMBERING CONFLICT" in tree(minimal), True)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
