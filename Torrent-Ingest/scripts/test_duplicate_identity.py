#!/usr/bin/env python3
"""A same-stem duplicate is only a duplicate when its identity is PROVEN (10.9 follow-up).

THE SMURFS LOSS, 2026-09-20. The replacement pack's `S01E01.mp4` (content *The
Astrosmurf*, the slot's real episode) and the old dvdrip's `S01E01.mkv` (content *The
Smurfette*, which belongs at S01E31) shared a stem -- so they shared ONE `.nfo`, and the
sidecar named the slot's correct episode for both files. `media_doctor`'s duplicate rule
saw "same title, keep the higher-quality container" and deleted the planner's `.mp4` at
38 S01 slots; the older wrong-slot `.mkv` survived. The owner lost the pack's Season 1.

The fix asks the ingest JOURNAL, which records the SOURCE filename each destination was
filed from: a same-stem pair whose source titles NAME DIFFERENT EPISODES is a placement
fault to re-file, never a duplicate to delete. Only when both files are proven the same
episode does the mechanical keep-the-better-copy deletion run at all.

    python3 scripts/test_duplicate_identity.py

Fixtures only. Exit 0 = all checks passed.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import media_doctor as md                                             # noqa: E402

failures: list[str] = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


SHOW_REL = "Shows/A Show (2019)/Season 01/"


def classify(root, mkv_title, mp4_title):
    """Run the classifier with a journal book naming each file's content."""
    prev = root / f"A Show (2019) - S01E07.mkv"
    v = root / f"A Show (2019) - S01E07.mp4"
    prev.write_bytes(b"m" * 100)
    v.write_bytes(b"p" * 50)
    book = {}
    if mkv_title:
        book[SHOW_REL + prev.name] = mkv_title
    if mp4_title:
        book[SHOW_REL + v.name] = mp4_title
    saved_book = md._journal_source_titles
    saved_rel = md._rel_from_library
    md._journal_source_titles = lambda: book
    md._rel_from_library = lambda p: SHOW_REL + Path(p).name
    try:
        return md._classify_slot_collision(prev, v, 1, 7)
    finally:
        md._journal_source_titles = saved_book
        md._rel_from_library = saved_rel


print("Part 1 -- the classifier demands identity evidence")
tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name)
    kind, detail, auto = classify(root, "The Smurfette", "The Astrosmurf")
    check("different journal identities -> misfiled_episode, not a duplicate",
          kind == "misfiled_episode" and auto is False)
    check("the detail names both identities and forbids deletion",
          "The Smurfette" in detail and "The Astrosmurf" in detail
          and "do not delete" in detail)
    kind, _d, auto = classify(root, "The Astrosmurf", "The Astrosmurf")
    check("the same proven episode -> a mechanical duplicate", kind == "duplicate_episode"
          and auto is True)
    kind, _d, auto = classify(root, None, "The Astrosmurf")
    check("one missing identity -> reported, never auto-deleted",
          kind == "duplicate_episode" and auto is False)
    kind, _d, auto = classify(root, None, None)
    check("no journal evidence at all -> reported, never auto-deleted",
          kind == "duplicate_episode" and auto is False)

    print("Part 2 -- apply_auto_fixes refuses the unproven deletion")
    mkv = root / "A Show (2019) - S01E07.mkv"
    mp4 = root / "A Show (2019) - S01E07.mp4"
    mkv.write_bytes(b"m" * 100)
    mp4.write_bytes(b"p" * 50)
    probs = {"show": "A Show (2019)", "path": str(root), "sid": "1", "sig": "x",
             "age": 0.0,
             "problems": [{"kind": "duplicate_episode", "detail": "unproven",
                           "auto": False, "files": [str(mkv), str(mp4)]}]}
    acted = md.apply_auto_fixes(probs, jf=None, state={}, dry_run=False,
                                cycle_budget=0)
    check("an unproven duplicate is not acted on", acted == [])
    check("both files survive", mkv.exists() and mp4.exists())

    probs["problems"][0]["auto"] = True
    acted = md.apply_auto_fixes(probs, jf=None, state={}, dry_run=False,
                                cycle_budget=0)
    check("a proven duplicate still deletes the lower-ranked copy",
          len(acted) == 1 and mp4.exists() is False and mkv.exists() is True)
finally:
    tmp.cleanup()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
