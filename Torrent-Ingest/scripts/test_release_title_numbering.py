#!/usr/bin/env python3
"""Release-order packs compute their broadcast numbering from their own titles (10.9).

THE SMURFS. The replacement pack names every episode `The Smurfs S01E01 (The
Smurfette).mp4` -- release order, which is NOT broadcast order (*The Smurfette* is
broadcast S01E31). The deleted dvdrip was filed positionally and 40 S01 files sat in
the wrong slots; a complete plan written off the release's own numbers would repeat
the fault at full scale. This is the `serial_release_map` class: the harness computes
the mapping and `validate_plan` refuses a plan that contradicts it.

It also pins the plan-ASSEMBLY half: above the size floor the harness hands the model
a deterministic skeleton covering every release file, and `ai_client` can name the
exact slice a truncated plan left out before the run ends.

    python3 scripts/test_release_title_numbering.py

Fixtures only, provider stubbed. Exit 0 = all checks passed.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import identify                                                      # noqa: E402
import library                                                       # noqa: E402
import epguide                                                       # noqa: E402
import ai_client                                                     # noqa: E402

failures: list[str] = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


RELEASE = [
    ("The Smurfs - Season 1/The Smurfs S01E01 (The Smurfette).mp4", (1, 31)),
    ("The Smurfs - Season 1/The Smurfs S01E02 (The Smurf Apprentice).mp4", (1, 2)),
    ("The Smurfs - Season 1/The Smurfs S01E03 (Vanity Fair).mp4", (2, 5)),
    ("The Smurfs - Season 1/The Smurfs S01E04 (King Smurf).mp4", (1, 4)),
    ("The Smurfs - Season 1/The Smurfs S01E05 (Jokey's Medicine).mp4", (5, 9)),
]
GUIDE = [
    {"season": 1, "number": 31, "name": "The Smurfette"},
    {"season": 1, "number": 2, "name": "The Smurf Apprentice"},
    {"season": 2, "number": 5, "name": "Vanity Fair"},
    {"season": 1, "number": 4, "name": "King Smurf"},
    {"season": 5, "number": 9, "name": "Jokey's Medicine"},
]

old_eps = epguide.episodes
epguide.episodes = lambda _title: GUIDE
try:
    fmap = identify.release_title_map("/tmp/The Smurfs", [r for r, _ in RELEASE])
    check("the computed map covers every release entry",
          fmap == {(1, 1): (1, 31), (1, 2): (1, 2), (1, 3): (2, 5),
                   (1, 4): (1, 4), (1, 5): (5, 9)})
    block, bmap = identify.title_numbering_block("/tmp/The Smurfs",
                                                 [r for r, _ in RELEASE])
    check("the block states the release-order rule", "RELEASE-ORDER NUMBERING" in block)
    check("the block names a computed slot", "Episode 31" in block and "The Smurfette" in block)
    check("the block carries the map back to the caller", bmap == fmap)

    # An ordinary pack whose numbers already match produces no block (no scary warning).
    epguide.episodes = lambda _title: [
        {"season": 1, "number": 1, "name": "Pilot"},
        {"season": 1, "number": 2, "name": "Second"},
        {"season": 1, "number": 3, "name": "Third"},
        {"season": 1, "number": 4, "name": "Fourth"},
    ]
    same = ["Show S01E01 (Pilot).mkv", "Show S01E02 (Second).mkv",
            "Show S01E03 (Third).mkv", "Show S01E04 (Fourth).mkv"]
    check("an ordinary pack gets no map and no block",
          identify.release_title_map("/tmp/Show", same) == {}
          and identify.title_numbering_block("/tmp/Show", same)[0] == "")
finally:
    epguide.episodes = old_eps

print("Part 2 -- validate_plan refuses the copied release number")
tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name)
    src = root / "The Smurfs S01E01 (The Smurfette).mp4"
    src.write_bytes(b"x")
    base = {"media_type": "show", "title": "The Smurfs", "year": 1981, "tmdb_id": 5687}

    def plan_at(dst, season, episode):
        p = dict(base)
        p["files"] = [{"src": str(src), "dst_rel": dst, "season": season,
                       "episode": episode}]
        return p

    bad = None
    try:
        library.validate_plan(
            plan_at("Shows/The Smurfs (1981)/Season 01/The Smurfs (1981) - S01E01.mp4",
                    1, 1), str(root), title_map=fmap)
    except library.PlanError as exc:
        bad = str(exc)
    check("copying the release number is refused", bad and "S01E31" in bad)
    ok = library.validate_plan(
        plan_at("Shows/The Smurfs (1981)/Season 01/The Smurfs (1981) - S01E31.mp4",
                1, 31), str(root), title_map=fmap)
    check("the computed slot is accepted", ok.get("title") == "The Smurfs")
    # An unmatched file is unconstrained (fail open).
    other = root / "The Smurfs S09E99 (Unknown Episode).mp4"
    other.write_bytes(b"x")
    open_plan = {"media_type": "show", "title": "The Smurfs", "year": 1981,
                 "tmdb_id": 5687,
                 "files": [{"src": str(other),
                            "dst_rel": "Shows/The Smurfs (1981)/Season 09/The Smurfs (1981) - S09E99.mp4",
                            "season": 9, "episode": 99}]}
    free = library.validate_plan(open_plan, str(root), title_map=fmap)
    check("a file the map does not cover is not constrained", free.get("title") == "The Smurfs")

    print("Part 3 -- the skeleton and the truncation check")
    skel = identify.plan_skeleton([r for r, _ in RELEASE], title_map=fmap,
                                  title="The Smurfs")
    check("the skeleton enumerates every release file", len(skel["files"]) == len(RELEASE))
    check("the skeleton pre-fills the computed slots",
          skel["files"][0]["season"] == 1 and skel["files"][0]["episode"] == 31)
    check("the skeleton leaves dst_rel for the model", skel["files"][0]["dst_rel"] == "")

    truncated = root / "plan.json"
    truncated.write_text(json.dumps({"files": [
        {"src": str(src)},
        {"src": str(other)},
    ]}), encoding="utf-8")
    names = [Path(r).name for r, _ in RELEASE]
    missing = ai_client._plan_missing(str(truncated), names)
    check("the truncation check names the missing slice", len(missing) == 4)
    tuned = root / "plan2.json"
    tuned.write_text(json.dumps({"files": [{"src": f"/x/{n}"} for n in names]}),
                     encoding="utf-8")
    check("a complete plan has nothing missing",
          ai_client._plan_missing(str(tuned), names) == [])
    broken = root / "plan3.json"
    broken.write_text("{not json", encoding="utf-8")
    check("an unreadable plan is left to the parse path (fail open)",
          ai_client._plan_missing(str(broken), names) == [])
finally:
    tmp.cleanup()

print("Part 4 -- a partial model plan is completed from the skeleton")
tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name)
    saved_mount, saved_shows = config.MEDIAFS_MOUNT, config.SHOWS_ROOT
    config.MEDIAFS_MOUNT, config.SHOWS_ROOT = root / "mount", root / "media" / "Shows"
    (root / "mount" / "Shows").mkdir(parents=True)
    rels = [r for r, _ in RELEASE]
    for r, _ in RELEASE:
        (root / r).parent.mkdir(parents=True, exist_ok=True)
        (root / r).write_bytes(b"x")
    abs_rels = [str(root / r) for r in rels]  # production builds absolute srcs
    skel = identify.plan_skeleton(abs_rels, title_map=fmap, title="Fixture Show")
    partial = {"media_type": "mixed", "title": "Fixture Show", "year": 2019,
               "tmdb_id": 5687,
               "files": [{"src": str(root / rels[0])}]}
    merged, filled, unresolved = identify.merge_skeleton_plan(partial, skel)
    by_src = {f["src"]: f for f in merged["files"]}
    check("every episode entry got a destination", len(merged["files"]) == len(RELEASE))
    check("the destinations use the computed broadcast slots",
          by_src[str(root / rels[0])]["dst_rel"].endswith("S01E31.mp4")
          and by_src[str(root / rels[2])]["dst_rel"].endswith("S02E05.mp4"))
    check("the folder follows the library naming",
          "Shows/Fixture Show (2019)/Season 01/" in by_src[str(root / rels[0])]["dst_rel"])
    check("nothing unresolved for episode-only releases", unresolved == [])
    check("the plan validates as a whole",
          library.validate_plan(merged, str(root), title_map=fmap)["title"] == "Fixture Show")

    # A movie the model did not place stays unresolved and is left OUT, so the coverage
    # guard parks the release rather than guessing a destination.
    skel2 = identify.plan_skeleton(abs_rels + [str(root / "Xtras" / "Movie.mp4")],
                                   title_map=fmap, title="Fixture Show")
    (root / "Xtras").mkdir(exist_ok=True)
    (root / "Xtras" / "Movie.mp4").write_bytes(b"x")
    merged2, filled2, unresolved2 = identify.merge_skeleton_plan(partial, skel2)
    check("a slot-less file the model omitted is named unresolved", len(unresolved2) == 1)
    check("and it is not invented into the plan",
          len(merged2["files"]) == len(RELEASE))
    # A destination two sources claim parks BOTH: the size-based duplicate collapse
    # must never silently choose (measured on the Smurfs pack, 2026-09-20).
    a = root / "A Show S01E05 (Alpha).mkv"; a.write_bytes(b"a")
    b = root / "A Show S01E06 (Beta).mkv"; b.write_bytes(b"b")
    skel3 = {"files": [{"src": str(a), "season": 1, "episode": 5},
                       {"src": str(b), "season": 1, "episode": 5}]}
    partial3 = {"media_type": "show", "title": "A Show", "year": 2019,
                "files": [{"src": str(a), "dst_rel": "Shows/A Show (2019)/Season 01/"
                                                   "A Show (2019) - S01E05.mkv"}]}
    _m3, _f3, unres3 = identify.merge_skeleton_plan(partial3, skel3)
    check("a destination two sources claim parks both",
          len(unres3) == 2)
    config.MEDIAFS_MOUNT, config.SHOWS_ROOT = saved_mount, saved_shows
finally:
    tmp.cleanup()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
