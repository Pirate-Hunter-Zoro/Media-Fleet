#!/usr/bin/env python3
"""Regression test: a stored searcher mapping is evidence, not an instruction (2026-09-10).

THE BUG THIS CLOSES. `_settled_block` rendered the searcher's stored file->item mapping
into the identify prompt under the words:

    "reuse this mapping by filename; do NOT re-derive the season/episode/volume numbering"

That was sound while a live searcher produced those maps. It stopped being sound the moment
the searcher was removed, because nothing produces or re-checks them any more -- and at
least one of them is wrong.

`[MTBB] Monogatari Series (BD 1080p)` has a stored plan that puts its five "Monogatari
Series Second Season" arcs into seasons 4, 5, 7, 8 and 9 while KEEPING the filenames'
absolute numbers -- season 5 starting at episode 6, season 7 at 10, season 8 at 14,
season 9 at 18 -- and strands episode 22 alone in season 10. That is precisely the shape
`library._reject_absolute_run_split` exists to refuse, and precisely what was filed. The
model did not reason its way there; the most authoritative-sounding line in a
90,000-character prompt told it not to think about it.

BOTH DIRECTIONS (§4.5):
  Part 1 -- the real stored Monogatari plan is judged incoherent and WITHHELD.
  Part 2 -- a coherent stored plan is still offered, because dropping every stored map
            would throw away the work the mechanism exists to save.
  Part 3 -- what IS offered is worded as evidence, never as "do not re-derive".

    python3 scripts/test_stored_plan_coherence.py

Read-only. Exit 0 means every check passed.
"""

import sys
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


def plan(spans):
    """spans: [(season, first, last), ...] -> a stored-plan dict."""
    files = []
    for sn, lo, hi in spans:
        for e in range(lo, hi + 1):
            files.append({"src": f"{sn}/{e}.mkv", "type": "episode",
                          "season": sn, "number": e})
    return {"series": "X", "kind": "anime", "files": files}


print("Part 1 -- an incoherent stored mapping is withheld")
# the real Monogatari stored plan's shape, verbatim
mono = plan([(1, 1, 15), (2, 1, 11), (3, 1, 4), (4, 1, 5), (5, 6, 9),
             (6, 1, 5), (7, 10, 13), (8, 14, 17), (9, 18, 23), (10, 22, 22)])
check("the real Monogatari stored shape is incoherent",
      identify._stored_plan_is_incoherent(mono), True)
block = identify._settled_block(mono)
check("it is withheld, not rendered", "->  S" in block, False)
check("and the run is told why", "internally inconsistent" in block, True)
check("and told to derive it itself", "Derive the numbering yourself" in block, True)

print("\nPart 2 -- a coherent stored mapping is STILL offered")
good = plan([(1, 1, 12), (2, 1, 13), (3, 1, 12), (4, 1, 10)])
check("ordinary per-season numbering is coherent",
      identify._stored_plan_is_incoherent(good), False)
gblock = identify._settled_block(good)
check("it is rendered", "->  S1E1" in gblock.replace("S01E01", "S1E1"), True)
check("a two-season plan can never be judged incoherent",
      identify._stored_plan_is_incoherent(plan([(1, 1, 5), (2, 6, 10)])), False)
check("one chained pair is not enough",
      identify._stored_plan_is_incoherent(
          plan([(1, 1, 5), (2, 6, 10), (3, 1, 5)])), False)
check("an empty plan renders nothing", identify._settled_block({"files": []}), "")
check("a missing plan renders nothing", identify._settled_block(None), "")

print("\nPart 3 -- what is offered is EVIDENCE, not an instruction")
check("the old 'do NOT re-derive' wording is gone",
      "do NOT re-derive" in gblock, False)
check("it says to treat it as evidence", "EVIDENCE, not as an instruction" in gblock, True)
check("it licenses overriding the mapping", "override it where it does not" in gblock, True)

print("\nControl -- the live stored plan for the real torrent")
live = identify.load_stored_plan("ff13439e7e644541b0434527cb379b5bfadb27e8")
if live:
    check("the real one on disk is judged incoherent",
          identify._stored_plan_is_incoherent(live), True)
else:
    print("  --    no live stored plan (already purged); fixtures above still cover it")

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
