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
    # validate_plan's same-slot collision scan reads the library roots; own them so the
    # live Smurfs shelf cannot decide this fixture's verdict.
    saved_mount, saved_media = config.MEDIAFS_MOUNT, config.MEDIA_ROOT
    config.MEDIAFS_MOUNT = root / "mount"
    config.MEDIA_ROOT = root / "media"
    (config.MEDIAFS_MOUNT / "Shows").mkdir(parents=True, exist_ok=True)
    (config.MEDIA_ROOT / "Shows").mkdir(parents=True, exist_ok=True)
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
    # The DESTINATION decides. The same correct placement with the release's numbers
    # left in the model's `season`/`episode` fields must also be accepted -- reading
    # those fields rejected the harness's own computed slot live on 2026-09-20.
    field_echo = plan_at(
        "Shows/The Smurfs (1981)/Season 01/The Smurfs (1981) - S01E31.mp4", 1, 1)
    ok2 = library.validate_plan(field_echo, str(root), title_map=fmap)
    check("a correct destination with release-number fields is accepted",
          ok2.get("title") == "The Smurfs")
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
    config.MEDIAFS_MOUNT, config.MEDIA_ROOT = saved_mount, saved_media
    tmp.cleanup()

print("Part 3b -- a dot-titled pack's own titles confirm its numbering there")
# THE GUMBALL AMZN PACK (2026-09-26). Frozen TMDB S01 for The Amazing World of Gumball
# (37606). The release's E01/E02 titles are swapped relative to TMDB; E03-E32 match at
# their own keys. The run filed E01-E32 at S01E16-E47 ("continue the absolute
# numbering"), which is what parked the other in-flight Gumball pack on the collisions.
GUMBALL_GUIDE = [
    {"season": 1, "number": 1, "name": "The DVD"},
    {"season": 1, "number": 2, "name": "The Responsible"},
    {"season": 1, "number": 3, "name": "The Third"},
    {"season": 1, "number": 4, "name": "The Debt"},
    {"season": 1, "number": 5, "name": "The End"},
    {"season": 1, "number": 6, "name": "The Dress"},
    {"season": 1, "number": 7, "name": "The Quest"},
    {"season": 1, "number": 8, "name": "The Spoon"},
    {"season": 1, "number": 9, "name": "The Pressure"},
    {"season": 1, "number": 10, "name": "The Painting"},
    {"season": 1, "number": 11, "name": "The Laziest"},
    {"season": 1, "number": 12, "name": "The Ghost"},
    {"season": 1, "number": 13, "name": "The Mystery"},
    {"season": 1, "number": 14, "name": "The Prank"},
    {"season": 1, "number": 15, "name": "The Gi"},
    {"season": 1, "number": 16, "name": "The Kiss"},
    {"season": 1, "number": 17, "name": "The Party"},
    {"season": 1, "number": 18, "name": "The Refund"},
    {"season": 1, "number": 19, "name": "The Robot"},
    {"season": 1, "number": 20, "name": "The Picnic"},
    {"season": 1, "number": 21, "name": "The Goons"},
    {"season": 1, "number": 22, "name": "The Secret"},
    {"season": 1, "number": 23, "name": "The Sock"},
    {"season": 1, "number": 24, "name": "The Genius"},
    {"season": 1, "number": 25, "name": "The Poltergeist"},
    {"season": 1, "number": 26, "name": "The Mustache"},
    {"season": 1, "number": 27, "name": "The Date"},
    {"season": 1, "number": 28, "name": "The Club"},
    {"season": 1, "number": 29, "name": "The Wand"},
    {"season": 1, "number": 30, "name": "The Ape"},
    {"season": 1, "number": 31, "name": "The Car"},
    {"season": 1, "number": 32, "name": "The Curse"},
    {"season": 1, "number": 33, "name": "The Microwave"},
    {"season": 1, "number": 34, "name": "The Meddler"},
    {"season": 1, "number": 35, "name": "The Helmet"},
    {"season": 1, "number": 36, "name": "The Fight"},
]
_amzn = {1: "The Responsible", 2: "The DVD"}
AMZN_SRCS = ["The.Amazing.World.of.Gumball.S01E%02d.%s.1080p.AMZN.WEB-DL.mkv"
             % (n, _amzn.get(n, GUMBALL_GUIDE[n - 1]["name"]).replace(" ", "."))
             for n in range(1, 33)]

old_guide_for = identify._guide_for
identify._guide_for = lambda _title, _tid=None: (GUMBALL_GUIDE, "TMDB")
try:
    dots = identify.release_dot_title_entries(AMZN_SRCS)
    check("every dot-titled release file is read", len(dots) == 32)
    check("a two-title combined file states no slot",
          identify.release_dot_title_entries(
              ["Show.S01E16.The.Car.-.The.Curse.1080p.mkv"]) == [])
    check("bracket and dash names are left to their own witnesses",
          identify.release_dot_title_entries(
              ["Show S01E01 (Pilot).mkv", "Show S01E01 - Pilot.mkv"]) == [])
    agreement = identify.release_episode_agreement(
        AMZN_SRCS, show_hint="The Amazing World of Gumball")
    check("the agreement confirms the 30 files whose own key the guide matches",
          len(agreement) == 30 and (1, 3) in agreement and (1, 32) in agreement)
    check("the two swapped titles are NOT confirmed (their claim moves)",
          (1, 1) not in agreement and (1, 2) not in agreement)
    identify._guide_for = lambda _title, _tid=None: (None, "")
    check("no guide -> no agreement (fail open)",
          identify.release_episode_agreement(
              AMZN_SRCS, show_hint="The Amazing World of Gumball") == {})
    identify._guide_for = lambda _title, _tid=None: (GUMBALL_GUIDE, "TMDB")

    tmp2 = tempfile.TemporaryDirectory()
    try:
        root2 = Path(tmp2.name)
        saved_m, saved_mr = config.MEDIAFS_MOUNT, config.MEDIA_ROOT
        config.MEDIAFS_MOUNT, config.MEDIA_ROOT = root2 / "mount", root2 / "media"
        (config.MEDIAFS_MOUNT / "Shows").mkdir(parents=True, exist_ok=True)
        (config.MEDIA_ROOT / "Shows").mkdir(parents=True, exist_ok=True)
        src = root2 / AMZN_SRCS[2]                 # S01E03 The Third
        src.write_bytes(b"x")

        def amzn_plan(dst, season, episode):
            return {"media_type": "show", "title": "The Amazing World of Gumball",
                    "year": 2011, "owned": True, "tmdb_id": 37606,
                    "files": [{"src": str(src), "dst_rel": dst, "season": season,
                               "episode": episode, "episode_title": "t", "plot": "p"}]}

        shifted = None
        try:
            library.validate_plan(
                amzn_plan("Shows/The Amazing World of Gumball (2011)/Season 01/"
                          "The Amazing World of Gumball (2011) - S01E18.mkv", 1, 18),
                str(root2), episode_agreement=agreement)
        except library.PlanError as exc:
            shifted = str(exc)
        check("keeping the season and shifting the episode is refused",
              shifted and "S01E03" in shifted)
        ok = library.validate_plan(
            amzn_plan("Shows/The Amazing World of Gumball (2011)/Season 01/"
                      "The Amazing World of Gumball (2011) - S01E03.mkv", 1, 3),
            str(root2), episode_agreement=agreement)
        check("the confirmed own slot is accepted", ok.get("title").endswith("Gumball"))
        # A deliberate cross-season renumber is a library layout (absolute runs, merged
        # cours) and must stay accepted; the guard is called directly so the fixture's
        # guide ceiling cannot speak instead.
        moved = {"src": str(src),
                 "dst_rel": "Shows/The Amazing World of Gumball (2011)/Season 02/"
                            "The Amazing World of Gumball (2011) - S02E03.mkv",
                 "season": 2, "episode": 3}
        library._reject_same_season_episode_shift([moved], agreement)
        check("a cross-season renumber is not constrained by this guard", True)
        special = {"src": str(src),
                   "dst_rel": "Shows/The Amazing World of Gumball (2011)/Season 00/"
                              "The Amazing World of Gumball (2011) - S00E03.mkv",
                   "season": 0, "episode": 3}
        library._reject_same_season_episode_shift([special], agreement)
        check("a special is the library's own scheme, not the guide's", True)
        unconfirmed = {"src": str(root2 / AMZN_SRCS[0]),   # S01E01, not confirmed
                       "dst_rel": "Shows/The Amazing World of Gumball (2011)/Season 01/"
                                  "The Amazing World of Gumball (2011) - S01E18.mkv",
                       "season": 1, "episode": 18}
        (root2 / AMZN_SRCS[0]).write_bytes(b"x")
        library._reject_same_season_episode_shift([unconfirmed], agreement)
        check("a file the agreement does not cover is not constrained", True)
    finally:
        config.MEDIAFS_MOUNT, config.MEDIA_ROOT = saved_m, saved_mr
        tmp2.cleanup()
finally:
    identify._guide_for = old_guide_for

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

print("Part 5 -- the map follows the provider Jellyfin SCRAPES (TMDB), not TVMaze")
import tmdbguide                                                    # noqa: E402
import re as _re                                                    # noqa: E402

# The Smurfs S07 disagreement is real (measured 2026-07-XX/2026-09-20): TVMaze files
# *Locomotive Smurfs* at E41, TMDB -- and Jellyfin -- at E43. The map must be computed
# against the list the owner's UI shows.
TMDB_GUIDE = [
    {"season": 7, "number": 43, "name": "Locomotive Smurfs"},
    {"season": 7, "number": 1, "name": "Smurf On The Wild Side (1)"},
    {"season": 7, "number": 2, "name": "Smurf On The Wild Side (2)"},
    {"season": 9, "number": 1, "name": "The Smurfs That Time Forgot (1)"},
    {"season": 9, "number": 2, "name": "The Smurfs That Time Forgot (2)"},
    {"season": 9, "number": 3, "name": "The Smurfs That Time Forgot (3)"},
    {"season": 6, "number": 40, "name": "I Smurf To The Trees"},
    {"season": 1, "number": 31, "name": "The Smurfette"},
    {"season": 1, "number": 35, "name": "Smurf-Colored Glasses"},
]
TVMAZE_GUIDE = [
    {"season": 7, "number": 41, "name": "Locomotive Smurfs"},
    {"season": 7, "number": 42, "name": "Little Big Smurf"},
    {"season": 9, "number": 1, "name": "The Smurfs That Time Forgot"},
    {"season": 9, "number": 3, "name": "Cave Smurfs"},
    {"season": 1, "number": 31, "name": "The Smurfette"},
]
old_tmdb_names, old_eps = tmdbguide.episode_names, epguide.episodes
tmdbguide.episode_names = lambda _tid: TMDB_GUIDE
epguide.episodes = lambda _title: TVMAZE_GUIDE
try:
    title_release = [
        "The Smurfs S07E01 (Locomotive Smurfs).mp4",
        "The Smurfs S07E12 (A Smurf on the Wild Side - pt1).mp4",
        "The Smurfs S07E16 (A Smurf on the Wild Side - pt2).mp4",
        "The Smurfs S09E01 (Smurfs that Time Forgot).mp4",
        "The Smurfs S06E06 (I Smurf to All Trees.mp4",
        "The Smurfs S01E01 (The Smurfette).mp4",
        "The Smurfs S03E24 (Smurfs Halloween).mp4",
    ]
    by_provider = identify.release_title_map("/tmp/The Smurfs", title_release,
                                             show_hint="The Smurfs", tmdb_id=5687)
    check("the TMDB guide wins when the show pins an id",
          by_provider.get((7, 1)) == (7, 43))
    check("the provider's part names resolve by the query's own digit",
          by_provider.get((7, 12)) == (7, 1) and by_provider.get((7, 16)) == (7, 2))
    check("a digitless title against several parts gets no claim",
          (9, 1) not in by_provider)
    check("a title missing its closing bracket still matches",
          by_provider.get((6, 6)) == (6, 40))
    check("a renamed title gets no claim", (3, 24) not in by_provider)
    check("without a pinned id the TVMaze fallback is chosen",
          identify._guide_for("The Smurfs")[1] == "TVMaze" and
          identify._match_titles(identify.release_title_entries(title_release),
                                 TVMAZE_GUIDE).get((7, 1)) == (7, 41))
    # The skeleton may not fall back to the release number when that number is a
    # computed slot of another file: that is the 2026-09-20 silent-collapse shape.
    fmap_small = {(1, 1): (1, 31), (1, 2): (1, 35)}
    skel_col = identify.plan_skeleton(
        ["S01E01 (The Smurfette).mp4", "S01E02 (Smurf Colored Glasses).mp4",
         "S01E31 (Some Unmatched Episode).mp4"],
        title_map=fmap_small, title="The Smurfs")
    entry = skel_col["files"][2]
    check("an unmatched file colliding with a computed slot is marked needs-mapping",
          entry["season"] is None and entry["episode"] is None)
finally:
    tmdbguide.episode_names, epguide.episodes = old_tmdb_names, old_eps

print("Part 6 -- a bracket-less dash title is a WEAK witness (Nobody Smurf)")
# `The Smurfs S07E49 - Nobody Smurf.mp4` carries the real episode title after a dash
# while its release number, S07E49, is another episode's broadcast slot. The dash form
# was rejected wholesale on 2026-09-20 because a loose capture swept scene tags and
# multi-episode markers; what ships is exact-and-unique or nothing.
DASH_GUIDE = [
    {"season": 7, "number": 27, "name": "Nobody Smurf"},
    {"season": 1, "number": 31, "name": "The Smurfette"},
    {"season": 1, "number": 2, "name": "The Smurf Apprentice"},
    {"season": 1, "number": 3, "name": "Vanity Fair"},
]
DASH_BRACKETED = [
    "The Smurfs S01E01 (The Smurfette).mp4",
    "The Smurfs S01E02 (The Smurf Apprentice).mp4",
    "The Smurfs S01E03 (Vanity Fair).mp4",
]
old_tmdb_dash, old_eps_dash = tmdbguide.episode_names, epguide.episodes
tmdbguide.episode_names = lambda _tid: DASH_GUIDE
epguide.episodes = lambda _title: DASH_GUIDE
try:
    dmap = identify.release_title_map(
        "/tmp/The Smurfs", DASH_BRACKETED + ["The Smurfs S07E49 - Nobody Smurf.mp4"],
        show_hint="The Smurfs", tmdb_id=5687)
    check("an exact unique dash title claims its real slot", dmap.get((7, 49)) == (7, 27))
    scene = identify.release_title_map(
        "/tmp/The Smurfs",
        DASH_BRACKETED + [
            "The Smurfs S01E04 (King Smurf).mp4",
            "The Smurfs S07E50 - Nobody Smurf 1080p WEB-DL x264.mp4"],
        show_hint="The Smurfs", tmdb_id=5687)
    check("a scene-tagged dash title gets no claim", (7, 50) not in scene)
    multi = identify.release_title_map(
        "/tmp/The Smurfs",
        DASH_BRACKETED + [
            "The Smurfs S01E04 (King Smurf).mp4",
            "The Smurfs S03E49-E40 - Vanity Fair.mp4"],
        show_hint="The Smurfs", tmdb_id=5687)
    check("a multi-episode dash shape gets no claim", (3, 49) not in multi)
    # A bracket title is not re-decided by the weak witness.
    both = identify._title_claims(
        identify.release_title_entries(["The Smurfs S01E01 (The Smurfette).mp4"]),
        identify.release_dash_title_entries(
            ["The Smurfs S01E01 - Nobody Smurf.mp4"]), DASH_GUIDE)
    check("the dash witness cannot overrule the bracket witness",
          both.get((1, 1)) == (1, 31))
finally:
    tmdbguide.episode_names, epguide.episodes = old_tmdb_dash, old_eps_dash

print("Part 7 -- a computed map forces the skeleton, whatever the size")
small = [f"Show S01E{i:02d} (Ep {i}).mkv" for i in range(1, 6)]
check("a small reordered release gets the skeleton",
      identify._skeleton_needed(small, {(1, 1): (1, 9)}) is True)
check("a small ordinary release does not",
      identify._skeleton_needed(small, {}) is False)
big = [f"Show S01E{i:03d}.mkv" for i in range(200)]
check("a large release gets it with no map",
      identify._skeleton_needed(big, {}) is True)
check("no release files, no skeleton",
      identify._skeleton_needed([], {(1, 1): (1, 2)}) is False)
# In a reordered pack an unmatched file's release number is not a destination: the
# pack's own order is measured to disagree with the guide, and `Nobody Smurf` would
# otherwise be filed at another episode's S07E49.
skel_u = identify.plan_skeleton(
    ["Show S01E01 (Alpha).mkv", "Show S01E02 (Beta).mkv", "Show S01E30 (Gamma).mkv"],
    title_map={(1, 1): (1, 9), (1, 2): (1, 2)}, title="Show")
entry_u = skel_u["files"][2]
check("an unmatched file in a reordered pack is needs-mapping",
      entry_u["season"] is None and entry_u["episode"] is None)
skel_n = identify.plan_skeleton(
    ["Show S01E01 (Alpha).mkv", "Show S01E02 (Beta).mkv", "Show S01E30 (Gamma).mkv"],
    title_map=None, title="Show")
entry_n = skel_n["files"][2]
check("without a map the release number is still kept",
      (entry_n["season"], entry_n["episode"]) == (1, 30))
# A computed Season-0 target makes the file a SPECIAL; `validate_plan` refuses a special
# with no episode_title/plot, so the harness supplies the title from the release name
# (`The Smurfs S01E40 (The Smurfs Springtime Special).mp4` -> S00E01) and names the file
# to the model for its plot.
skel_s = identify.plan_skeleton(
    ["Show S01E01 (Alpha).mkv", "Show S01E02 (Beta).mkv",
     "Show S01E03 (Special Time).mkv"],
    title_map={(1, 1): (1, 1), (1, 2): (1, 2), (1, 3): (0, 1)}, title="Show")
entry_s = skel_s["files"][2]
check("a Season-0 target gets the release title pre-filled",
      entry_s.get("episode_title") == "Special Time"
      and (entry_s["season"], entry_s["episode"]) == (0, 1))
merged_s, _filled_s, _unres_s = identify.merge_skeleton_plan(
    {"media_type": "show", "title": "Show", "year": 2019,
     "files": [{"src": "Show S01E01 (Alpha).mkv"}]}, skel_s)
check("the merge keeps the pre-filled special title",
      any(f.get("episode_title") == "Special Time" for f in merged_s["files"]))

print("Part 8 -- release paths resolve on disk; the model cannot clobber a verified src")
# A chunked wave's file list carries qBittorrent's root-prefixed names while the
# content path IS that root: joining blindly doubled it and every merged plan died on
# "src does not exist" (the live American Dad run, 2026-09-23). Names not on disk are
# dropped -- a wave may not promise files it does not hold.
tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name) / "American Dad! (2005)"
    (root / "Season 01").mkdir(parents=True)
    real = root / "Season 01" / "American Dad! (2005) - S01E02.mkv"
    real.write_bytes(b"x")
    single = Path(tmp.name) / "Movie.mkv"
    single.write_bytes(b"x")
    rels = ["American Dad! (2005)/Season 01/American Dad! (2005) - S01E02.mkv",
            "American Dad! (2005)/Season 09/Not Downloaded Yet.mkv",
            "Season 01/American Dad! (2005) - S01E02.mkv"]
    resolved = identify._resolve_release_abs(root, rels)
    check("a root-prefixed name resolves without doubling the root",
          resolved == [str(real)])
    check("a mirror-style name (no root prefix) resolves to the same file",
          identify._resolve_release_abs(
              root, ["Season 01/American Dad! (2005) - S01E02.mkv"]) == [str(real)])
    check("a file that is not on disk is dropped",
          len(identify._resolve_release_abs(root, rels)) == 1)
    check("a single-file torrent resolves to the file itself",
          identify._resolve_release_abs(single, ["Movie.mkv"]) == [str(single)])
    # The merge keeps the harness's verified src even when the model echoes a path that
    # does not exist but matches the basename.
    skel_p = identify.plan_skeleton([str(real)], {(1, 2): (1, 2)}, title="American Dad!")
    bad_src = str(root / "American Dad! (2005)" / "Season 01" / real.name)
    merged_p, _f, _u = identify.merge_skeleton_plan(
        {"media_type": "show", "title": "American Dad!", "year": 2005,
         "files": [{"src": bad_src, "episode_title": "Threat Levels"}]},
        skel_p)
    check("the model cannot clobber a verified src",
          merged_p["files"][0]["src"] == str(real))
    check("the model's metadata still lands on the entry",
          merged_p["files"][0].get("episode_title") == "Threat Levels")
finally:
    tmp.cleanup()

print("Part 9 -- replay: the matcher never contradicts an accepted historical plan")
jpath = Path(config.STATE_DIR) / "journal.jsonl"
plans_seen = contradictions = dash_claimed = 0
if jpath.exists():
    for line in jpath.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        files = [f for f in ((rec.get("plan") or {}).get("files") or [])
                 if isinstance(f, dict) and f.get("src")]
        entries = identify.release_title_entries([f["src"] for f in files])
        dash = identify.release_dash_title_entries([f["src"] for f in files])
        if len(entries) + len(dash) < 4:
            continue
        guide, plan_slot = [], {}
        for f in files:
            m = _re.search(r"Season\s+(\d+)/.*?S(\d+)E(\d+)", f.get("dst_rel") or "")
            t = (identify.release_title_entries([f["src"]])
                 or identify.release_dash_title_entries([f["src"]]))
            if not m or not t:
                continue
            key = (t[0][1], t[0][2])
            guide.append({"season": int(m.group(2)), "number": int(m.group(3)),
                          "name": t[0][3]})
            plan_slot[key] = (int(m.group(2)), int(m.group(3)))
        if len(guide) < 4:
            continue
        plans_seen += 1
        claims = identify._title_claims(entries, dash, guide)
        strong = identify._match_titles(entries, guide)
        dash_claimed += len([k for k in claims if k not in strong])
        for e in entries + dash:
            claimed = claims.get((e[1], e[2]))
            filed = plan_slot.get((e[1], e[2]))
            if claimed and filed and tuple(claimed) != tuple(filed):
                contradictions += 1
    print(f"  replayed {plans_seen} titled-release plan(s): "
          f"{contradictions} contradiction(s), {dash_claimed} dash-only claim(s)")
    check("the matcher contradicts no accepted historical plan", contradictions == 0)

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
