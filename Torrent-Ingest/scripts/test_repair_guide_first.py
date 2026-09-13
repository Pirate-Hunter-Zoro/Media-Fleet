#!/usr/bin/env python3
"""Acceptance test for the deterministic-first metadata repair (§5b item 5, §4.146).

`repair_metadata.repair_show` went straight to a language model for every blank episode,
even though `epguide` -- key-less TVMaze, on-disk cached, already a dependency of
`library.py` in three places -- can answer most of them outright. None of the ~45 repairs
made on 2026-09-03 needed a model.

§4.146 sets the acceptance bar, and it is not a count: a past session shipped a repair tool
that REPORTED 250 repairs it never made. So this test never trusts the returned number. For
every episode the filler claims, it re-reads the sidecar off disk and asserts the file
actually stopped being blank; and it asserts the count matches the files that really
changed, so a filler that returns 5 while writing 3 fails here.

Both directions (§7), because "the AI got less work" and "the repair silently did nothing"
produce the same tidy log line:
  * an episode the guide covers is filled, verified on disk, and NOT handed to the AI;
  * an episode the guide does not cover, or covers with a title but no plot, is left in the
    residue for the AI rather than written half-resolved into a LOCKED sidecar;
  * a show the guide knows nothing about changes nothing and hands back every episode.

No network: `epguide.episodes` is stubbed. Fixtures are built and torn down here.

    python3 scripts/test_repair_guide_first.py

Exit 0 means every check passed.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import library  # noqa: E402
import epguide  # noqa: E402
import repair_metadata as repair  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}: {got!r}")
    if not ok:
        failures.append(label)


SHOW = "Fixture Show"
GUIDE = [
    {"season": 1, "number": 1, "name": "The First",  "summary": "A real synopsis."},
    {"season": 1, "number": 2, "name": "The Second", "summary": "Another synopsis."},
    # covered by name but with NO summary -> must NOT be written into a locked sidecar
    {"season": 1, "number": 3, "name": "The Third",  "summary": ""},
    # S01E04 is absent from the guide entirely -> residue
]


def build(tmp, episodes):
    todo = []
    for s, e in episodes:
        v = Path(tmp) / f"{SHOW} - S{s:02d}E{e:02d}.mkv"
        v.write_bytes(b"\0")
        todo.append({"video": str(v), "season": s, "episode": e, "abs": e})
    return todo


def plots_on_disk(todo):
    """The episodes whose sidecar actually stopped reading blank -- the only truth."""
    return {Path(e["video"]).name for e in todo
            if not library.episode_is_blank(Path(e["video"]), SHOW)}


tmp = tempfile.mkdtemp()
backup = Path(tempfile.mkdtemp())
try:
    # --- direction 1: the guide answers, and every claimed fix is real ---------
    print("a covered show fills deterministically, and each fill is verified on disk")
    epguide.episodes = lambda name: GUIDE
    todo = build(tmp, [(1, 1), (1, 2), (1, 3), (1, 4)])
    check("all four start blank", plots_on_disk(todo), set())

    fixed, residue = repair._fill_from_guide(SHOW, todo, backup)
    changed = plots_on_disk(todo)

    check("claimed count equals files that really changed", fixed, len(changed))
    check("exactly the two fully-covered episodes changed", sorted(changed),
          [f"{SHOW} - S01E01.mkv", f"{SHOW} - S01E02.mkv"])
    check("residue is what the AI must still do",
          sorted(Path(e["video"]).name for e in residue),
          [f"{SHOW} - S01E03.mkv", f"{SHOW} - S01E04.mkv"])

    nfo = Path(tmp) / f"{SHOW} - S01E01.nfo"
    body = nfo.read_text(encoding="utf-8")
    check("the sidecar carries the guide's title", "<title>The First</title>" in body, True)
    check("and the guide's plot", "<plot>A real synopsis.</plot>" in body, True)
    check("and is locked", "<lockdata>true</lockdata>" in body, True)
    check("a summary-less episode was NOT written",
          (Path(tmp) / f"{SHOW} - S01E03.nfo").exists(), False)

    # --- direction 2: no guide, nothing happens, nothing is claimed ------------
    print("\na show the guide does not know changes nothing")
    shutil.rmtree(tmp); tmp = tempfile.mkdtemp()
    epguide.episodes = lambda name: None
    todo2 = build(tmp, [(1, 1), (1, 2)])
    fixed2, residue2 = repair._fill_from_guide(SHOW, todo2, backup)
    check("nothing claimed", fixed2, 0)
    check("nothing written", plots_on_disk(todo2), set())
    check("every episode handed to the AI", len(residue2), 2)

    # --- direction 3: a guide that raises must not break a repair run ----------
    print("\na guide failure fails soft (a lookup must never block a repair)")

    def _boom(name):
        raise RuntimeError("tvmaze down")

    epguide.episodes = _boom
    todo3 = build(tmp, [(2, 1)])
    fixed3, residue3 = repair._fill_from_guide(SHOW, todo3, backup)
    check("nothing claimed", fixed3, 0)
    check("the episode still reaches the AI", len(residue3), 1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")
