#!/usr/bin/env python3
"""The arc->season mapping, and the two guards that enforce it. Read-only.

    python3 scripts/test_arc_mapping.py

WHAT IS BEING PROVED, in the order it matters:

  1. The release UNIT is the filename label, not the folder. Monogatari's five folders
     `05 - Nekomonogatari (White)` .. `10 - Koimonogatari` are ONE unit of 23, because
     every file in them is named `Monogatari Series Second Season - NN`. Folders would
     have split it five ways, which is exactly how four arcs ended up in Season 03.
  2. `arcmap.propose` returns the UNIQUE correct mapping for the hardest pack in the
     library, and it returns it as arithmetic -- no model involved.
  3. The two new guards reject the three seasons that were actually wrong on
     2026-09-12, reconstructed from the census, and reject them for the right reason.
  4. Neither guard rejects anything the library ever filed correctly. This is the part
     that costs real work to get right: §4.114's rule is that a guard rejecting real
     content is worse than the bug it fixes, and `test_placement_guards.py` exists
     because two earlier guards' first drafts each rejected ~50 correct plans.

Part 4 replays the journal, so it reads live mutable state; a plan that is re-ingested
later changes what it sees. That is why parts 1-3 are pinned to a `.torrent` on disk and
to reconstructed fixtures instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import arcmap                                                          # noqa: E402
import config                                                          # noqa: E402
import library                                                         # noqa: E402

MONOGATARI = "ff13439e7e644541b0434527cb379b5bfadb27e8"

# The provider's shape for Monogatari Series, frozen. Pinned rather than fetched so the
# test states an expectation instead of asserting that two live lookups agree.
MONO_SHAPE = {1: 15, 2: 11, 3: 23, 4: 13, 5: 6, 6: 14}

MONO_EXPECTED = {
    1: "Bakemonogatari",
    2: "Nisemonogatari",
    3: "Monogatari Series Second Season",
    4: "Owarimonogatari S1",
    5: "Zoku Owarimonogatari",
}
MONO_LEFTOVER = {"Kizumonogatari", "Nekomonogatari (Black)", "Hanamonogatari",
                 "Tsukimonogatari", "Koyomimonogatari", "Owarimonogatari S2"}


def _release_files():
    """Every path in the Monogatari `.torrent`, or None when it is not on disk."""
    p = config.STATE_DIR / "torrent_sources" / f"{MONOGATARI}.torrent"
    if not p.exists():
        return None
    import qbt
    meta, _ = qbt._bdecode(p.read_bytes(), 0)
    info = meta[b"info"]
    return ["/".join(x.decode("utf-8", "replace") for x in f[b"path"])
            for f in info[b"files"]]


def test_units() -> int:
    """A unit is a filename LABEL, and it may span many folders."""
    files = _release_files()
    if files is None:
        print("part 1 SKIPPED: the Monogatari .torrent is not in state/torrent_sources/")
        return 0
    us = arcmap.units(files)
    print(f"part 1: {len(files)} files -> {len(us)} release unit(s)")
    bad = 0
    by_label = {u.label: u for u in us}

    second = by_label.get("Monogatari Series Second Season")
    if second is None:
        print("  !! no 'Monogatari Series Second Season' unit")
        bad += 1
    else:
        if second.size != 23:
            print(f"  !! that unit holds {second.size} files, expected 23")
            bad += 1
        if len(second.folders) != 5:
            print(f"  !! it spans {len(second.folders)} folders, expected 5")
            bad += 1
        if not second.contiguous or second.numbers[0] != 1:
            print(f"  !! its numbers are not one run from 1: {second.numbers[:4]}...")
            bad += 1
        if not bad:
            print(f"  ok  one unit of 23 across 5 folders: {second.folders[0]} .. "
                  f"{second.folders[-1]}")

    # Release order must be the release's own order, because the cover search is monotone
    # in it. `01 - Bakemonogatari` first, `15 - Zoku Owarimonogatari` last.
    if us and (us[0].label != "Bakemonogatari"
               or us[-1].label != "Zoku Owarimonogatari"):
        print(f"  !! units are not in release order: {us[0].label!r} .. {us[-1].label!r}")
        bad += 1
    else:
        print("  ok  units are in release order")
    return 1 if bad else 0


def test_propose() -> int:
    """The mapping is unique, and it is the right one."""
    files = _release_files()
    if files is None:
        print("part 2 SKIPPED: the Monogatari .torrent is not in state/torrent_sources/")
        return 0
    p = arcmap.propose(files, MONO_SHAPE)
    print("\npart 2: arc -> season")
    if p is None or not p.settled:
        print(f"  !! no settled proposal ({p.why if p else 'None'})")
        return 1
    bad = 0
    got = {s: [u.label for u in us] for s, us in p.mapping.items()}
    for season, label in MONO_EXPECTED.items():
        if got.get(season) != [label]:
            print(f"  !! Season {season:02d} -> {got.get(season)}, expected [{label!r}]")
            bad += 1
        else:
            print(f"  ok  Season {season:02d} <- {label}")
    if {u.label for u in p.leftover} != MONO_LEFTOVER:
        print(f"  !! leftover arcs {sorted(u.label for u in p.leftover)}, "
              f"expected {sorted(MONO_LEFTOVER)}")
        bad += 1
    else:
        print(f"  ok  {len(p.leftover)} arc(s) left for the run to judge "
              f"(films/specials), none forced into a season")

    # Season 06 exists in the provider's shape and is NOT in this release. A search that
    # demanded every season be filled would find nothing at all here.
    if 6 in p.mapping:
        print("  !! Season 06 was filled; this release does not contain it")
        bad += 1
    else:
        print("  ok  Season 06 correctly left empty (not in this release)")

    # The file the census caught: the first `Nekomonogatari (Black)` file must NOT be
    # proposed into any numbered season.
    neko = [f for f in files if "Nekomonogatari (Black) - 01" in f]
    if neko and p.season_of(neko[0]) is not None:
        print(f"  !! {Path(neko[0]).name} proposed into Season {p.season_of(neko[0])}")
        bad += 1
    elif neko:
        print("  ok  Nekomonogatari (Black) is proposed into NO numbered season")
    return 1 if bad else 0


def _ep(src, season, number, folder="Monogatari Series (2009)"):
    dst = (config.SHOWS_ROOT / folder / f"Season {season:02d}"
           / f"{folder} - S{season:02d}E{number:02d}.mkv")
    return {"type": "episode", "src": f"/dl/{src}", "season": season, "number": number,
            "episode_title": "t", "plot": "p",
            "dst_rel": str(dst.relative_to(config.MEDIA_ROOT)), "_dst_abs": str(dst)}


def test_guards_fire() -> int:
    """The three seasons that were actually wrong must be rejected, for the right reason."""
    print("\npart 3: the 2026-09-12 census, reconstructed")
    bad = 0

    # Season 06 held all 4 of Otorimonogatari and ONE file of Tsukimonogatari -- the arc
    # torn between Season 03 (3 files) and Season 06 (1 file).
    torn = ([_ep(f"Tsukimonogatari - 0{n}.mkv", 3, n) for n in (1, 2, 3)]
            + [_ep("Tsukimonogatari - 04.mkv", 6, 4)])
    try:
        library._reject_arc_split_across_seasons(torn)
        print("  !! an arc split between Season 03 and Season 06 was ACCEPTED")
        bad += 1
    except library.PlanError as exc:
        if "one arc split across two seasons" not in str(exc):
            print(f"  !! rejected for the wrong reason: {exc}")
            bad += 1
        else:
            print("  ok  Tsukimonogatari torn between Seasons 03 and 06 -> rejected")

    # The same arc split between a season and SPECIALS is legitimate and must pass: a
    # provider carrying 12 where the release has 15 is saying the last three are specials.
    ok_split = ([_ep(f"Bakemonogatari - {n:02d}.mkv", 1, n) for n in range(1, 13)]
                + [_ep(f"Bakemonogatari - {n:02d}.mkv", 0, n - 12) for n in (13, 14, 15)])
    try:
        library._reject_arc_split_across_seasons(ok_split)
        print("  ok  an arc split between Season 01 and Season 00 is allowed")
    except library.PlanError as exc:
        print(f"  !! a season/specials split was rejected: {exc}")
        bad += 1

    # Season 05 held Koyomimonogatari (12) and Hanamonogatari (5) -- 17 episodes, neither
    # arc torn, in a season the provider says has 6.
    stacked = ([_ep(f"Koyomimonogatari - {n:02d}.mkv", 5, n) for n in range(1, 13)]
               + [_ep(f"Hanamonogatari - {n:02d}.mkv", 5, n) for n in range(13, 18)])
    if not (config.SHOWS_ROOT / "Monogatari Series (2009)").is_dir():
        print("  -- over-fill case SKIPPED: the show is not in the library, so the "
              "provider lookup its folder name drives cannot run")
    else:
        try:
            library._reject_season_over_provider_count({}, stacked)
            print("  !! 17 episodes in a 6-episode season were ACCEPTED")
            bad += 1
        except library.PlanError as exc:
            if "season over-filled" not in str(exc):
                print(f"  !! rejected for the wrong reason: {exc}")
                bad += 1
            else:
                print("  ok  17 files into a 6-episode Season 05 -> rejected")
    return 1 if bad else 0


def test_release_root_stripped() -> int:
    """A chunked torrent's paths are named from the TORRENT ROOT, and must still parse.

    This is the bug found on 2026-09-12: qBittorrent names a chunked torrent's files
    `<release>/<arc>/<file>` while the directory walk yields `<arc>/<file>`, and both go
    into the same argument. Reading `parts[0]` as the arc folder gave every file in a
    chunked pack the SAME folder -- so `_release_structure_block` saw one folder and
    returned "", and `arcmap` saw one arc. The block that names the split/numbering
    conflict was written for Monogatari and rendered nothing on Monogatari, every run,
    because Monogatari is always chunked.
    """
    print("\npart 5: a chunked torrent's release-root prefix")
    files = _release_files()
    if files is None:
        print("  SKIPPED: the Monogatari .torrent is not in state/torrent_sources/")
        return 0
    import identify
    rooted = [f"[MTBB] Monogatari Series (BD 1080p)/{f}" for f in files]
    bad = 0

    if len(arcmap.units(rooted)) != len(arcmap.units(files)):
        print("  !! rooted and un-rooted paths give different units")
        bad += 1
    else:
        print(f"  ok  both path shapes give {len(arcmap.units(files))} units")

    block = identify._release_structure_block("/dl/x", rooted)
    if "15 top-level folder(s)" not in block:
        print("  !! the release structure block does not see 15 arc folders:\n"
              f"     {block[:200]!r}")
        bad += 1
    else:
        print("  ok  the release structure block sees all 15 arc folders")
    if "SPLIT/NUMBERING CONFLICT" not in block:
        print("  !! the split/numbering conflict is not reported")
        bad += 1
    else:
        print("  ok  the split/numbering conflict is reported")

    p = arcmap.propose(rooted, MONO_SHAPE)
    if p is None or not p.settled or [u.label for u in p.mapping.get(3, [])] != \
            ["Monogatari Series Second Season"]:
        print("  !! the proposal from rooted paths is wrong")
        bad += 1
    else:
        print("  ok  the proposal is identical from rooted paths")

    # A release with no wrapper folder must be left exactly as it is.
    flat = ["01 - A/x - 01.mkv", "02 - B/y - 01.mkv"]
    if arcmap.strip_release_root(flat) != flat:
        print(f"  !! a release with no wrapper was stripped: "
              f"{arcmap.strip_release_root(flat)}")
        bad += 1
    else:
        print("  ok  a release with no wrapper folder is left alone")

    # One folder holding files directly IS the structure and must survive.
    one = ["Some Show S01/x - 01.mkv", "Some Show S01/x - 02.mkv"]
    if arcmap.strip_release_root(one) != one:
        print(f"  !! a single-folder release was flattened: "
              f"{arcmap.strip_release_root(one)}")
        bad += 1
    else:
        print("  ok  a single-folder release is not flattened")
    return 1 if bad else 0


def test_specials_metadata() -> int:
    """The specials' titles and plots must be HANDED to the run, and never guessed.

    The failure being fixed, measured 2026-09-12: a confirm-mode run spent all 19 of its
    turns web-searching for Season-0 titles, hit the loop breaker, wrote no plan, and
    burned 195,693 of groq's 200,000 daily tokens. `validate_plan` requires a real title
    AND plot on every Season-0 file, and the run had no other way to get one.

    The trap this guards is the opposite one: asserting a title that is WRONG. Matching
    each arc to the unique specials group of its own size pairs `Kizumonogatari` -- three
    films -- with `Owarimonogatari`'s three specials, because that is the only group of
    three. Order is what rules that out.
    """
    print("\npart 6: titles and plots for the specials")
    files = _release_files()
    if files is None:
        print("  SKIPPED: the Monogatari .torrent is not in state/torrent_sources/")
        return 0
    import epguide
    sp = epguide.specials("Monogatari Series")
    if not sp:
        print("  SKIPPED: the guide returned no specials (offline, or a miss cached)")
        return 0
    p = arcmap.propose(files, MONO_SHAPE)
    groups = arcmap.special_groups(sp)
    m = arcmap.match_arcs_to_specials(p.leftover, groups)
    got = {u.label: (groups[m[u.order]][0] if u.order in m else None) for u in p.leftover}
    want = {
        "Kizumonogatari": None,            # three FILMS -- not specials at all
        "Nekomonogatari (Black)": "Tsubasa Family",
        "Hanamonogatari": "Suruga Devil",
        "Tsukimonogatari": "Yotsugi Doll",
        "Koyomimonogatari": "Koyomimonogatari",
        "Owarimonogatari S2": None,        # 7 files vs 3 provider entries: stay silent
    }
    bad = 0
    for label, expect in want.items():
        if got.get(label) != expect:
            print(f"  !! {label!r} matched {got.get(label)!r}, expected {expect!r}")
            bad += 1
        else:
            print(f"  ok  {label:26s} -> {expect or '(unmatched, correctly)'}")

    block = arcmap.metadata_block(p, sp)
    # The hedged half: an arc the provider GROUPS where the release SPLITS. Its titles
    # belong in the prompt; a file-to-title mapping does not, because there isn't one.
    if "Owarimonogatari - Part 1 - Mayoi Hell" not in block:
        print("  !! the grouped-arc titles are missing; the run will web-search for them")
        bad += 1
    elif "groups what the release splits" not in block:
        print("  !! the grouped arc is asserted rather than hedged")
        bad += 1
    else:
        print("  ok  Owarimonogatari S2's titles are offered, hedged, unmapped to files")
    if "Kizumonogatari" in block:
        print("  !! the FILM arc was given titles; it is not a special at all")
        bad += 1
    else:
        print("  ok  the film arc is still given nothing")

    # The Season-00 numbering. Thirty-two files across five arcs must get thirty-two
    # DISTINCT numbers: two files at one destination fails the whole plan, not one file.
    import re as _re
    slots = [int(x) for x in _re.findall(r"->  S00E(\d\d)", block)]
    span = _re.search(r"Season 00 E(\d\d)-E(\d\d)", block)
    covered = set(slots) | (set(range(int(span.group(1)), int(span.group(2)) + 1))
                            if span else set())
    want_n = sum(u.size for u in p.leftover if u.label != "Kizumonogatari")
    if len(slots) != len(set(slots)):
        print("  !! the proposed S00 numbers collide")
        bad += 1
    elif covered != set(range(1, want_n + 1)):
        print(f"  !! the S00 numbering is not 1..{want_n}: {sorted(covered)[:6]}...")
        bad += 1
    else:
        print(f"  ok  S00E01-E{want_n:02d} proposed, distinct, no gaps, films excluded")

    if "Tsubasa Family - Part 1" not in block or "Koyomi Stone" not in block:
        print("  !! the block does not carry the matched titles")
        bad += 1
    elif "plot :" not in block:
        print("  !! the block carries titles but no plots; Season-0 needs both")
        bad += 1
    else:
        print(f"  ok  the block carries titles AND plots ({len(block)} chars)")

    # Squeezing the block must never leave a STUB plot. It is written into a LOCKED
    # sidecar, so "..." would be frozen into the library permanently -- worse than having
    # no plot at all, which merely leaves the run to supply one.
    squeezed = [arcmap.metadata_block(p, sp, max_plot=n) for n in (140, 90, 30, 0)]
    if any("plot : ..." in b or "plot :\n" in b for b in squeezed):
        print("  !! a squeezed block emits a stub plot")
        bad += 1
    elif "plot :" in squeezed[0] and "plot :" not in squeezed[-1]:
        print("  ok  squeezing shortens plots, then drops them -- never stubs them")
    else:
        print("  !! squeezing did not behave: plots present at 140? "
              f"{'plot :' in squeezed[0]}, absent at 0? {'plot :' not in squeezed[-1]}")
        bad += 1
    # Titles and the S00 numbers survive every squeeze; they are not negotiable.
    if not all("Tsubasa Family - Part 1" in b and "->  S00E01" in b for b in squeezed):
        print("  !! squeezing dropped a title or an S00 number")
        bad += 1
    else:
        print("  ok  titles and S00 numbers survive every squeeze")

    # Scoping to a wave must keep only that wave's files.
    one = [u for u in p.leftover if u.label == "Hanamonogatari"][0].paths[0]
    scoped = arcmap.metadata_block(p, sp, wave_paths=[one])
    if "Suruga Devil - Part 1" not in scoped or "Koyomi Stone" in scoped:
        print("  !! wave scoping did not narrow the block to the wave's files")
        bad += 1
    else:
        print(f"  ok  scoped to one wave file: {len(scoped)} chars, Hanamonogatari only")
    return 1 if bad else 0


def test_correct_plan_is_accepted() -> int:
    """The guards must ACCEPT the harness's own answer, not merely reject the wrong ones.

    Every other part here proves something is refused. That is half a guard: a check that
    rejects the three historical failures AND the correct placement is worse than no check
    at all, because it turns a fixable mis-file into a pack that can never be filed. So
    this builds the plan mechanically from `arcmap`'s mapping and its proposed Season-00
    numbers -- no model, no judgement -- and runs it through the real `validate_plan`.

    Self-contained: the release is mirrored as small files in a temp tree, so this keeps
    working after Monogatari is purged (which it is meant to be -- it is a stress test the
    owner does not want the content of).
    """
    print("\npart 7: the CORRECT plan passes every guard")
    files = _release_files()
    if files is None:
        print("  SKIPPED: the Monogatari .torrent is not in state/torrent_sources/")
        return 0
    import tempfile
    import epguide
    sp = epguide.specials("Monogatari Series")
    p = arcmap.propose(files, MONO_SHAPE)
    if p is None or not p.settled:
        print("  !! no settled proposal to build a plan from")
        return 1
    groups = arcmap.special_groups(sp)
    matched = arcmap.match_arcs_to_specials(p.leftover, groups) if sp else {}

    root = Path(tempfile.mkdtemp(prefix="arcmap-plan-")) / "release"
    for i, rel in enumerate(files):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * (1024 + i))       # distinct sizes: no accidental duplicates

    show = "Monogatari Series (2009)"
    plan_files = []
    for season, us in sorted(p.mapping.items()):
        n = 0
        for u in us:
            for rel in u.paths:
                n += 1
                plan_files.append({
                    "type": "episode", "season": season, "episode": n,
                    "src": str(root / rel),
                    "dst_rel": f"Shows/{show}/Season {season:02d}/"
                               f"{show} - S{season:02d}E{n:02d}.mkv"})
    s00 = 1
    for u in p.leftover:
        if u.label == "Kizumonogatari":
            continue                                   # films, numbered nowhere
        entries = groups[matched[u.order]][1] if u.order in matched else None
        for i, rel in enumerate(u.paths):
            title = (entries[i]["name"] if entries else f"{u.label} part {i + 1}")
            plot = ((entries[i].get("summary") if entries else "")
                    or f"A special of the Monogatari Series ({u.label}).")
            plan_files.append({
                "type": "episode", "season": 0, "episode": s00 + i,
                "src": str(root / rel), "episode_title": title, "plot": plot,
                "dst_rel": f"Shows/{show}/Season 00/{show} - "
                           f"S00E{s00 + i:02d}-{title}.mkv"})
        s00 += u.size
    kizu = next((u for u in p.leftover if u.label == "Kizumonogatari"), None)
    if kizu:
        names = ["Kizumonogatari I: Tekketsu-hen (2016)",
                 "Kizumonogatari II: Nekketsu-hen (2016)",
                 "Kizumonogatari III: Reiketsu-hen (2017)"]
        for i, rel in enumerate(kizu.paths):
            plan_files.append({"type": "movie", "src": str(root / rel),
                               "dst_rel": f"Movies/{names[i]}.mkv",
                               "tmdb_id": [342472, 414453, 443463][i]})

    plan = {"media_type": "mixed", "title": "Monogatari Series", "year": 2009,
            "owned": False, "anime": True, "tmdb_id": 46195, "existing_match": False,
            "reasoning": "built from arcmap's own mapping; no model involved",
            "files": plan_files}
    n_ep = sum(1 for f in plan_files if f["type"] == "episode")
    n_mv = sum(1 for f in plan_files if f["type"] == "movie")
    if len(plan_files) != len(files):
        print(f"  !! the plan covers {len(plan_files)} of {len(files)} release files")
        return 1
    print(f"  ok  the mapping covers all {len(files)} files ({n_ep} episodes, {n_mv} films)")
    try:
        library.validate_plan(plan, str(root))
    except library.PlanError as exc:
        print(f"  !! the CORRECT plan was rejected: {exc}")
        return 1
    except Exception as exc:                                          # noqa: BLE001
        print(f"  !! validation crashed on the correct plan: {type(exc).__name__}: {exc}")
        return 1
    print("  ok  validate_plan ACCEPTS it -- the guards do not refuse the right answer")
    return 0


def test_ownership_is_narrow() -> int:
    """Own only what Jellyfin's own provider cannot render -- never the whole plan.

    The owner's instruction, 2026-09-12: *"let TMDB still work for all the individual
    episodes/files it will work for - only own what is necessary."* Owning a file is not
    free: a locked sidecar is the fleet's word forever and Jellyfin can never improve on
    it. So this pins BOTH directions -- the ten that must be owned, and the ninety-three
    that must not.

    Two different failures have to be caught, and the second is the nastier:
      * TMDB has no such SLOT          -> blank episode. Obvious.
      * TMDB has the slot but its season is a DIFFERENT SHOW -> a plausible wrong title.
        `serves()` alone says yes to all six of those, which is exactly backwards.
    """
    print("\npart 8: own only what is necessary")
    files = _release_files()
    if files is None:
        print("  SKIPPED: the Monogatari .torrent is not in state/torrent_sources/")
        return 0
    p = arcmap.propose(files, MONO_SHAPE)
    bad = 0

    # Frozen TMDB shape for 46195, read from the API on 2026-09-12. Pinned rather than
    # fetched so this states an expectation instead of asserting two lookups agree.
    TMDB = {0: 49, 1: 12, 2: 11, 3: 23, 4: 12, 5: 15}
    NAMES = {1: "Bakemonogatari", 2: "Nisemonogatari",
             3: "Monogatari Series: Second Season", 4: "Owarimonogatari",
             5: "MONOGATARI Series OFF & MONSTER Season"}
    serves = lambda s, e: 1 <= e <= TMDB.get(s, 0)                     # noqa: E731
    # Season identity, frozen: only S05 is a different show (TMDB's S05 is the 2024
    # OFF & MONSTER Season; the fleet files Zoku Owarimonogatari, 6 episodes from 2019).
    same = lambda s, labs: s != 5                                      # noqa: E731

    block = arcmap.ownership_block(p, 46195, serves=serves, same_show=same)
    # A season TMDB simply lacks slots for must NOT be owned. Measured 2026-09-12: TMDB's
    # Bakemonogatari season has 12 episodes and the fleet filed 15, and Jellyfin rendered
    # all 15 anyway -- with real overviews and romanised titles -- because it scrapes a
    # PROVIDER CHAIN, not TMDB alone. Owning on a missing TMDB slot would freeze our text
    # over a provider that was about to do fine. It is also the recoverable case:
    # `media_doctor`'s `plot_blank` check finds a genuinely blank episode afterwards, with
    # evidence instead of a prediction.
    for s in ("Season 01", "Season 04"):
        if block and s in block:
            print(f"  !! {s} was flagged on a missing TMDB slot -- that premise is wrong "
                  f"(Jellyfin filled S01E13-15 from another provider)")
            bad += 1
    if not bad:
        print("  ok  a missing TMDB slot alone does NOT trigger ownership")
    for s in ("Season 02", "Season 03"):
        if block and s in block:
            print(f"  !! {s} was flagged; it is served correctly and must stay un-owned")
            bad += 1
    if not bad:
        print("  ok  correctly-served seasons are left alone")
    # And the plan-level flag must never be recommended.
    if not block:
        print("  !! no ownership block produced at all")
        return 1
    if "plan-level `owned`" not in block or "THESE FILES ONLY" not in block:
        print("  !! the block does not say to own per-FILE rather than the whole plan")
        bad += 1
    else:
        print("  ok  it asks for per-file ownership, not a plan-wide flag")

    # The identity check: TMDB HAS S05E01-06, and they are a different show entirely.
    import tmdbguide
    if tmdbguide.season_identity_matches(46195, 5, ["Zoku Owarimonogatari"], ) is not False:
        print("  -- S05 identity check SKIPPED (TMDB unreachable or uncached)")
    else:
        live = arcmap.ownership_block(p, 46195)
        if "Season 05" not in live or "DIFFERENT SHOW" not in live:
            print("  !! S05 (Zoku vs the 2024 OFF & MONSTER Season) was not caught")
            bad += 1
        else:
            print("  ok  S05 caught as a DIFFERENT SHOW, not merely a missing slot")

    # No tmdb id -> silent. A brand-new show has no established match to disagree with.
    if arcmap.ownership_block(p, None) != "":
        print("  !! it spoke without a TMDB id")
        bad += 1
    else:
        print("  ok  silent when the show has no pinned TMDB id")
    return 1 if bad else 0


def _journal_plans():
    by_ih = {}
    with (config.STATE_DIR / "journal.jsonl").open(encoding="utf-8",
                                                   errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            by_ih[r.get("info_hash") or id(r)] = r
    return by_ih


def _with_dst_abs(files):
    """Journal plans carry `dst_rel`; the guards read `_dst_abs`. Fill it in."""
    out = []
    for f in files:
        if not isinstance(f, dict):
            continue
        g = dict(f)
        if not g.get("_dst_abs") and g.get("dst_rel"):
            g["_dst_abs"] = str(config.MEDIA_ROOT / g["dst_rel"])
        out.append(g)
    return out


def test_no_false_positives() -> int:
    """Neither guard may reject a plan the library accepted correctly."""
    plans = _journal_plans()
    split_rej, over_rej = {}, {}
    total = 0
    for _ih, r in plans.items():
        plan = r.get("plan")
        if not isinstance(plan, dict) or not (plan.get("files") or []):
            continue
        files = _with_dst_abs(plan["files"])
        name = r.get("name") or "?"
        total += 1
        try:
            library._reject_arc_split_across_seasons(files)
        except library.PlanError as exc:
            split_rej[name] = str(exc)
        try:
            library._reject_season_over_provider_count(plan, files)
        except library.PlanError as exc:
            over_rej[name] = str(exc)

    print(f"\npart 4: {total} historical plans replayed")
    print(f"  arc-split guard      rejected {len(split_rej)}")
    for n, e in sorted(split_rej.items()):
        print(f"    !! {n[:60]}: {e[:120]}")
    print(f"  season-overfill guard rejected {len(over_rej)}")
    for n, e in sorted(over_rej.items()):
        print(f"    !! {n[:60]}: {e[:120]}")

    # Monogatari's own wrong plans are the ONE thing that may appear here: they are the
    # plans these guards were written for. Anything else is a false positive.
    unexpected = [n for n in list(split_rej) + list(over_rej)
                  if "monogatari" not in n.lower()]
    if unexpected:
        print(f"  FAIL: {len(unexpected)} plan(s) the library accepted are now rejected")
        return 1
    if split_rej or over_rej:
        print("  ok  the only rejections are Monogatari's own known-bad plans")
    else:
        print("  ok  no historical plan is rejected")
    return 0


if __name__ == "__main__":
    rc = (test_units() | test_propose() | test_guards_fire()
          | test_release_root_stripped() | test_specials_metadata()
          | test_correct_plan_is_accepted() | test_ownership_is_narrow()
          | test_no_false_positives())
    print("\nPASS" if rc == 0 else "\nFAIL")
    raise SystemExit(rc)
