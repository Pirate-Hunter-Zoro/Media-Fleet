#!/usr/bin/env python3
"""Regression test: a sidecar carrying a DIFFERENT episode's title (2026-09-10).

THE CASE. One Pace S13E05 held two files -- `S13E05 - Inherited Will.mkv` and
`S13E05 - Quack Doctor.mkv` -- and BOTH sidecars said "Quack Doctor". The collision check
could only report "two files cover this slot" and could not say which was wrong.

The fleet's own decisions log had the answer: "Quack Doctor" is One Pace's OLDER cut of
that arc position (ch. 141-145); "Inherited Will" is the newer one (ch. 140-145). Filing
the newer cut into the slot inherited the pre-seeded sidecar's title instead of giving it
its own. The filename was right; the sidecar named the episode it replaced.

`_title_is_janky` cannot catch this: "Quack Doctor" is a perfectly good title, just the
wrong one. What gives it away is that it contradicts the filename.

WHY THIS DOES NOT INVERT §4.26. That lesson (believe the .nfo over the filename) stands.
A sidecar can be overwritten by a later repair or inherited from a pre-seeded one, while
the filename is written once by `apply_plan` from the identify plan -- so the tie is broken
by a THIRD witness, the ingest journal, not by preferring one guess. Filename and journal
agreeing against the sidecar is evidence and is repaired; the filename alone is reported
for review and never silently rewritten.

BOTH DIRECTIONS (§4.5), and Part 2 is the one that keeps the library safe -- a detector
this eager, applied to sanitisation variants, would rewrite hundreds of correct titles:
  Part 1 -- it fires on genuinely different titles, including the real One Pace pair.
  Part 2 -- it does NOT fire on the ways a correct title legitimately differs from its
            filename: sanitised characters, truncation, punctuation, case.
  Part 3 -- the journal gate decides repair vs review.

    python3 scripts/test_sidecar_title_contradiction.py

Read-only. Exit 0 means every check passed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import media_doctor as md                                            # noqa: E402
import library                                                       # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


print("Part 1 -- a sidecar naming a different episode IS a contradiction")
CONTRADICTS = [
    ("Inherited Will", "Quack Doctor", "the real One Pace S13E05 pair"),
    ("Tin Plate Wapol", "Adventure in a Nameless Country", "two real neighbours"),
    ("The Summit", "Enter Tony Tony Chopper", "adjacent episodes"),
    ("Unhappy Campers", "Queen Bee", "the Helluva Boss shift shape"),
]
for fn, nfo, why in CONTRADICTS:
    check(f"{why}: {fn!r} vs {nfo!r}",
          md._title_contradicts_filename(fn, nfo), True)

print("\nPart 2 -- the ways a CORRECT title legitimately differs are NOT contradictions")
SAME = [
    ("Ghostf--kers", "Ghostf**kers", "illegal characters sanitised for the filesystem"),
    ("The Full Moon", "The Full Moon", "identical"),
    ("Mammon's Magnificent Musical Mid-Season Special",
     "Mammon's Magnificent Musical Mid-Season Special (ft Fizzarolli)", "truncated filename"),
    ("Monkey See, Doggie Do", "Monkey See, Doggie Do", "punctuation"),
    ("the skies of drum", "The Skies of Drum", "case only"),
    ("Hiriluk's Cherry Blossoms", "Hiriluks Cherry Blossoms", "apostrophe stripped"),
    ("Boogie Frights & Abracadaver", "Boogie Frights and Abracadaver", "ampersand"),
    ("", "Quack Doctor", "no title in the filename"),
    ("Inherited Will", "", "no title in the sidecar"),
    ("", "", "neither"),
]
for fn, nfo, why in SAME:
    check(f"{why}", md._title_contradicts_filename(fn, nfo), False)

print("\nPart 2b -- an episode MARKER is not a title (a real false positive)")
# `Psych (2006) - S07E15-E16.mkv` carries no title at all, but the text after the last
# " - " is "E16", which reads as a title to anything looking for one -- and then
# contradicts the sidecar's real title, reporting a correctly-named multi-episode file
# as a fault. This is what the check did the first time it ran over the live library.
MARKERS = [
    ("E16", "Psych: The Musical", "the real false positive"),
    ("S07E15-E16", "Psych: The Musical", "a full SxxExx-Exx marker"),
    ("E15-E16", "Something Real", "an episode range"),
    ("16", "Something Real", "a bare number"),
    ("15-16", "Something Real", "a bare range"),
    ("Part 2", "Something Real", "a part marker"),
    ("Vol. 3", "Something Real", "a volume marker"),
    ("Chapter 5", "Something Real", "a chapter marker"),
]
for fn, nfo, why in MARKERS:
    check(f"{why}: {fn!r}", md._title_contradicts_filename(fn, nfo), False)

print("\nPart 2c -- MULTI-EPISODE files, the second false-positive class")
# A multi-episode filename leaves its second marker glued to the front of the title
# (`S03E10-E11 - The Day of Black Sun` -> "E11 - The Day of Black Sun"), and its sidecar
# carries BOTH episodes' titles joined ("The Boiling Rock (1) / The Boiling Rock (2)").
# Both describe the same thing. These are the exact strings the live library produced.
MULTI = [
    ("E15", "The More You Moe, The Moe You Know (1) / The More You Moe, The Moe You Know (2)",
     "Adventure Time: a bare marker against a joined sidecar"),
    ("E11 - The Day of Black Sun & The Day of Black Sun",
     "The Day of Black Sun: The Invasion (1) / The Day of Black Sun: The Eclipse (2)",
     "Avatar: marker+title against two joined subtitles"),
    ("E15 - The Boiling Rock", "The Boiling Rock (1) / The Boiling Rock (2)",
     "Avatar: the same name twice, parenthesised"),
]
for fn, nfo, why in MULTI:
    check(why, md._title_contradicts_filename(fn, nfo), False)
check("and a real contradiction still survives all of that stripping",
      md._title_contradicts_filename("E05 - Inherited Will", "Quack Doctor"), True)

print("\nPart 2d -- a LOCKED sidecar is exempt, the third false-positive class")
# `Made in Abyss (2017) - S00E05-Papa to Issho.mkv` against a sidecar saying
# "Together with Papa" -- the romaji title and its English translation, the SAME
# episode. The sidecar is already lockdata=true, i.e. deliberately authored and
# marked never-scrape, and changing either side would make the library worse.
#
# No string comparison can separate a translation from a different episode, so the
# decision is made on LOCK STATE instead of on the strings: a locked sidecar is a
# choice somebody made, an unlocked one is whatever Jellyfin scraped -- and every
# real fault this check has found was the unlocked kind.
check("the romaji/translation pair still LOOKS like a contradiction to the matcher",
      md._title_contradicts_filename("Papa to Issho", "Together with Papa"), True)

LOCKED = '<episodedetails><title>Together with Papa</title>' \
         '<lockdata>true</lockdata></episodedetails>'
UNLOCKED = '<episodedetails><title>Together with Papa</title>' \
           '<lockdata>false</lockdata></episodedetails>'
check("...but the sidecar is locked, so the check is suppressed",
      library.nfo_is_locked(LOCKED), True)
check("an UNLOCKED sidecar is not exempt -- the check still fires there",
      library.nfo_is_locked(UNLOCKED), False)
check("a sidecar with no lockdata at all is not exempt",
      library.nfo_is_locked("<episodedetails><title>x</title></episodedetails>"), False)
check("an empty/missing sidecar is not exempt", library.nfo_is_locked(""), False)
check("the lock test is case- and whitespace-tolerant",
      library.nfo_is_locked("<LOCKDATA>TRUE</LOCKDATA>"), True)

print("\nPart 3 -- the journal is the third witness")
dsts = md._journal_destinations()
check("the journal yields real destinations", len(dsts) > 100, True)
# a destination the journal really recorded must be confirmable
sample = next((d for d in dsts if d.endswith(".mkv")), None)
check("a recorded destination is confirmed",
      md._journal_confirms_filename(config.MEDIAFS_MOUNT / sample)
      if sample else True, True)
check("an invented destination is NOT confirmed",
      md._journal_confirms_filename(
          config.MEDIAFS_MOUNT / "Shows/No Such Show (1999)/Season 01/"
          "No Such Show (1999) - S01E01 - Nothing.mkv"), False)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
