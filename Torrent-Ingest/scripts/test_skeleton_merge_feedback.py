#!/usr/bin/env python3
"""The skeleton merge places what the model re-typed, and alternate cuts are computed.

HANDOFF 15.2 (Family Guy `S07E07`) and 15.3 (Friends' 32 Featurettes), plus 15.1's
identity guard. Three measured failures, one fixture per clause:

  * The model re-types `src` instead of copying the skeleton's absolute path (Friends:
    all 32 entries dropped the closing `)` of the torrent root). The merge matched
    exact strings and unique basenames only, lost the four files whose basenames repeat
    across season folders, and the coverage contract parked the whole release.
  * A pack ships one episode twice under one release `SxxEyy` (Family Guy: Uncensored +
    Bale Scene beside Uncensored + Commentary). The model names one; the other is an
    unresolved release file and the pack parks. The harness must compute the pair as
    alternates of ONE episode, rank the survivor and record the sibling as accounted.
  * An unresolved release file must be handed to the NEXT provider as a fixable
    rejection (with a `--require-list`), not parked on the first incomplete answer.

And the converse of 10.9 (15.1): a pack whose release numbering already MATCHES the
provider gets a computed agreement fact, and a plan that remaps a numbered episode onto
another season is refused.

    python3 scripts/test_skeleton_merge_feedback.py

Fixtures only, providers stubbed. Exit 0 = all checks passed.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import epguide                                                       # noqa: E402
import identify                                                      # noqa: E402
import journal                                                       # noqa: E402
import library                                                       # noqa: E402
import plan_coverage                                                 # noqa: E402

failures: list[str] = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


print("Part 1 -- alternate cuts of one episode (15.2)")
check("the two Family Guy cuts reduce to one core",
      journal.alternate_title_core(
          "Family Guy - S07E07 - Ocean's Three and a Half (Uncensored + Bale Scene).mkv")
      == journal.alternate_title_core(
          "Family Guy - S07E07 - Ocean's Three and a Half (Uncensored + Commentary "
          "Audio Track).mkv"))
check("the core is the episode title",
      journal.alternate_title_core(
          "Family Guy - S07E07 - Ocean's Three and a Half (Uncensored + Bale Scene).mkv")
      == "ocean s three and a half")
check("II and III are different cores",
      journal.alternate_title_core("Show S01E01 - Title II.mkv")
      != journal.alternate_title_core("Show S01E01 - Title III.mkv"))
check("Part 1 and Part 2 are different cores",
      journal.alternate_title_core("Show S01E01 - Title (Part 1).mkv")
      != journal.alternate_title_core("Show S01E01 - Title (Part 2).mkv"))
check("a release-version parenthetical is stripped from a bracket title",
      journal.alternate_title_core("Show S01E01 (Pilot) [Extended].mkv")
      == "pilot")
check("a bare-numbered name has no core", journal.alternate_title_core("E07.mkv") == "")

tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name)
    saved_mount, saved_shows, saved_media = (
        config.MEDIAFS_MOUNT, config.SHOWS_ROOT, config.MEDIA_ROOT)
    config.MEDIAFS_MOUNT, config.SHOWS_ROOT = root / "mount", root / "media" / "Shows"
    config.MEDIA_ROOT = root / "media"
    (config.MEDIAFS_MOUNT / "Shows").mkdir(parents=True)
    (config.MEDIA_ROOT / "Shows").mkdir(parents=True)

    two_cuts = [
        "Family Guy - Season 7/Family Guy - S07E07 - Ocean's Three and a Half "
        "(Uncensored + Bale Scene).mkv",
        "Family Guy - Season 7/Family Guy - S07E07 - Ocean's Three and a Half "
        "(Uncensored + Commentary Audio Track).mkv",
        "Family Guy - Season 7/Family Guy - S07E08 - Family Goy.mkv",
    ]
    for rel in two_cuts:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * (10 if "Bale" in rel else 20))
    abs_rels = [str(root / r) for r in two_cuts]
    skel = identify.plan_skeleton(abs_rels, title="Family Guy")
    by_basename = {Path(f["src"]).name: f for f in skel["files"]}
    check("the alternate pair is slotted by the harness",
          by_basename[Path(abs_rels[0]).name]["season"] == 7
          and by_basename[Path(abs_rels[0]).name]["episode"] == 7)
    check("both alternate entries are marked with their group",
          by_basename[Path(abs_rels[0]).name].get("_alternate") == "S07E07"
          and by_basename[Path(abs_rels[1]).name].get("_alternate") == "S07E07")
    check("an ordinary episode is not marked",
          "_alternate" not in by_basename[Path(abs_rels[2]).name])

    # Same release key, DIFFERENT cores: two parts of one story, not alternates.
    parts = []
    for n in (1, 2):
        rel = f"Show - Season 1/Show - S01E01 - The Big Story (Part {n}).mkv"
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        parts.append(rel)
    skel_parts = identify.plan_skeleton([str(root / r) for r in parts], title="Show")
    check("a multi-part pair is NOT slotted as alternates",
          all(f.get("season") is None and "_alternate" not in f
              for f in skel_parts["files"]))

    # The model answers with one file; the harness must complete the pair from the
    # skeleton and the validator must keep the ranked survivor.
    model = {"media_type": "show", "title": "Family Guy", "year": 1999, "tmdb_id": 1434,
             "files": [{"src": abs_rels[0],
                        "dst_rel": "Shows/Family Guy (1999)/Season 07/Family Guy (1999)"
                                   " - S07E07 - Ocean's Three and a Half.mkv",
                        "episode_title": "Ocean's Three and a Half"}]}
    merged, filled, unresolved = identify.merge_skeleton_plan(model, skel)
    check("the alternate sibling is not unresolved", unresolved == [])
    check("both cuts are in the merged plan", len(merged["files"]) == 3)
    out = library.validate_plan(merged, str(root))
    check("the validator keeps exactly one copy of the episode",
          len(out["files"]) == 2)
    dropped = out.get("_deduped_dropped") or []
    check("the dropped sibling is recorded as an alternate",
          len(dropped) == 1 and dropped[0]["reason"] == "alternate")
    survivor = [f for f in out["files"] if "S07E07" in f.get("dst_rel", "")]
    check("the larger cut survives", survivor
          and "Commentary" in Path(str(survivor[0]["src"])).name)
    check("the model's episode metadata is carried to the survivor",
          survivor and survivor[0].get("episode_title") == "Ocean's Three and a Half")
    gaps, _acct = plan_coverage.release_gaps(
        [(r, None) for r in two_cuts], out["files"], root,
        resolved_srcs=[d["src"] for d in dropped])
    check("the coverage contract accounts for the dropped alternate", gaps == [])

    # Two DIFFERENT episodes at one destination are still the disagreement shape: the
    # marked-alternate exemption must not swallow them.
    a = root / "X S01E05 (Alpha).mkv"; a.write_bytes(b"a")
    b = root / "X S01E05 (Beta).mkv"; b.write_bytes(b"b")
    skel_conflict = {"files": [{"src": str(a), "season": 1, "episode": 5},
                               {"src": str(b), "season": 1, "episode": 5}]}
    _m, _f, unres = identify.merge_skeleton_plan(
        {"media_type": "show", "title": "X", "year": 2019, "files": []},
        skel_conflict)
    check("an unmarked same-destination pair still parks both", len(unres) == 2)
finally:
    config.MEDIAFS_MOUNT, config.SHOWS_ROOT, config.MEDIA_ROOT = (
        saved_mount, saved_shows, saved_media)
    tmp.cleanup()

print("Part 2 -- path-tail attribution (15.3)")
tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name)
    saved_mount, saved_shows = config.MEDIAFS_MOUNT, config.SHOWS_ROOT
    config.MEDIAFS_MOUNT, config.SHOWS_ROOT = root / "mount", root / "media" / "Shows"
    (root / "mount" / "Shows").mkdir(parents=True)
    base = root / ("Friends (1994) Season 1-10 S01-S10 (1080p BluRay x265 HEVC 10bit "
                   "AAC 5.1 Silence)")
    for sub in ("Featurettes/Season 10", "Featurettes/Season 2"):
        p = base / sub / "Friends of Friends_new.mkv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    abs_rels = [str(base / "Featurettes/Season 10/Friends of Friends_new.mkv"),
                str(base / "Featurettes/Season 2/Friends of Friends_new.mkv")]
    skel = identify.plan_skeleton(abs_rels, title="Friends")
    # The model re-typed the root, dropping the closing `)` as it did live.
    retyped_root = str(base.parent / ("Friends (1994) Season 1-10 S01-S10 (1080p BluRay "
                                      "x265 HEVC 10bit AAC 5.1 Silence"))
    model = {"media_type": "show", "title": "Friends", "year": 1994, "tmdb_id": 1668,
             "files": [
                 {"src": f"{retyped_root}/Featurettes/Season {s}/Friends of Friends_new.mkv",
                  "dst_rel": f"Shows/Friends (1994)/Season 00/Friends (1994) - "
                             f"S00E0{i + 1}.mkv", "season": 0, "episode": i + 1,
                  "episode_title": f"Friends of Friends (Season {s})", "plot": "p"}
                 for i, s in enumerate((10, 2))]}
    merged, filled, unresolved = identify.merge_skeleton_plan(model, skel)
    check("an ambiguous basename is attributed by parent folder",
          unresolved == [] and len(merged["files"]) == 2)
    check("the skeleton's verified src wins over the re-typed one",
          {f["src"] for f in merged["files"]} == set(abs_rels))
    dsts = {Path(f["dst_rel"]).name for f in merged["files"]}
    check("the model's own destinations survive",
          dsts == {"Friends (1994) - S00E01.mkv", "Friends (1994) - S00E02.mkv"})

    # A genuinely ambiguous entry stays unresolved rather than guessing. Unslotted
    # skeleton entries (the harness could not compute a destination) are the shape the
    # model must answer for; two files sharing a parent tail give it nothing to match.
    c = root / "P/Q/E01.mkv"; c.parent.mkdir(parents=True); c.write_bytes(b"c")
    d = root / "R/Q/E01.mkv"; d.parent.mkdir(parents=True); d.write_bytes(b"d")
    skel_amb = {"files": [{"src": str(c), "season": None, "episode": None},
                          {"src": str(d), "season": None, "episode": None}]}
    _m, _f, unres2 = identify.merge_skeleton_plan(
        {"media_type": "show", "title": "P", "year": 2019, "files": []}, skel_amb)
    check("two files sharing a parent tail are not guessed", len(unres2) == 2)
finally:
    config.MEDIAFS_MOUNT, config.SHOWS_ROOT = saved_mount, saved_shows
    tmp.cleanup()

print("Part 3 -- an incomplete answer is a fixable rejection (15.3)")
err, missing = identify._incomplete_plan_feedback(
    ["Featurettes/Season 2/Friends of Friends_new.mkv",
     "Featurettes/Season 10/Friends of Friends_new.mkv",
     "Featurettes/Season 3/What's Up with Your Friends_new.mkv"])
check("the missing basenames are named, deduplicated", missing == [
    "Friends of Friends_new.mkv", "What's Up with Your Friends_new.mkv"])
check("the error says every release file must be placed",
      "Every release file must appear" in err and "could not place 3" in err)
check("the error names an example", "Friends of Friends_new.mkv" in err)

print("Part 4 -- release numbering agreements are refused a remap (15.1)")
GUIDE = [
    {"season": 10, "number": 6, "name": "Independent Movie"},
    {"season": 4, "number": 6, "name": "The 42-Year-Old Virgin"},
    {"season": 10, "number": 7, "name": "A Jones for a Smith"},
    {"season": 10, "number": 8, "name": "The People vs. Martin Sugar"},
]
old_eps = epguide.episodes
epguide.episodes = lambda _title: GUIDE
try:
    release = [
        "American Dad! - Season 10/American Dad! (2005) - S10E06 - Independent Movie.mkv",
        "American Dad! - Season 04/American Dad! (2005) - S04E06 - The 42-Year-Old "
        "Virgin.mkv",
        "American Dad! - Season 10/American Dad! (2005) - S10E07 - A Jones for a Smith.mkv",
        "American Dad! - Season 10/American Dad! (2005) - S10E08 - The People vs. Martin "
        "Sugar.mkv",
    ]
    imap, provider = identify.release_identity_map("/tmp/AD", release)
    check("an agreeing release yields the identity map",
          imap == {(10, 6): (10, 6), (4, 6): (4, 6), (10, 7): (10, 7), (10, 8): (10, 8)})
    check("the provider is reported", provider == "TVMaze")
    check("a reordered release yields no identity map",
          identify.release_identity_map("/tmp/AD", [
              "Show S01E01 (Pilot).mkv", "Show S01E02 (Second).mkv",
              "Show S01E03 (Third).mkv", "Show S01E04 (Fourth).mkv"]) == ({}, ""))
    check("the agreement block states the computed fact",
          "RELEASE NUMBERING CONFIRMED" in identify.numbering_agreement_block(imap, provider))

    t2 = tempfile.TemporaryDirectory()
    try:
        root2 = Path(t2.name)
        saved_mount2, saved_media2 = config.MEDIAFS_MOUNT, config.MEDIA_ROOT
        config.MEDIAFS_MOUNT, config.MEDIA_ROOT = root2 / "mount", root2 / "media"
        (config.MEDIAFS_MOUNT / "Shows").mkdir(parents=True)
        (config.MEDIA_ROOT / "Shows").mkdir(parents=True)
        src = root2 / "American Dad! (2005) - S10E06 - Independent Movie.mkv"
        src.write_bytes(b"x")

        def plan_at(dst, season, episode):
            entry = {"src": str(src), "dst_rel": dst, "season": season,
                     "episode": episode}
            if season == 0:                     # specials must carry real metadata
                entry["episode_title"] = "Independent Movie"
                entry["plot"] = "A special."
            return {"media_type": "show", "title": "American Dad!", "year": 2005,
                    "tmdb_id": 1433, "files": [entry]}

        bad = None
        try:
            library.validate_plan(
                plan_at("Shows/American Dad! (2005)/Season 04/"
                        "American Dad! (2005) - S04E06 - Independent Movie.mkv", 4, 6),
                str(root2), identity_map=imap)
        except library.PlanError as exc:
            bad = str(exc)
        check("a remap of an agreeing episode is refused",
              bad and "S10E06" in bad and "do not shift a season" in bad)
        ok = library.validate_plan(
            plan_at("Shows/American Dad! (2005)/Season 10/"
                    "American Dad! (2005) - S10E06 - Independent Movie.mkv", 10, 6),
            str(root2), identity_map=imap)
        check("the release's own slot is accepted", ok.get("title") == "American Dad!")
        # Season 00 is the library's own scheme (15.5): a TMDB number never constrains it.
        special = library.validate_plan(
            plan_at("Shows/American Dad! (2005)/Season 00/"
                    "American Dad! (2005) - S00E01 - Independent Movie.mkv", 0, 1),
            str(root2), identity_map=imap)
        check("a Season-00 destination is exempt from the identity guard",
              special.get("title") == "American Dad!")
    finally:
        config.MEDIAFS_MOUNT, config.MEDIA_ROOT = saved_mount2, saved_media2
        t2.cleanup()
finally:
    epguide.episodes = old_eps

print("Part 5 -- replay: the new rules reject no accepted historical plan")
jpath = Path(config.STATE_DIR) / "journal.jsonl"
plans_seen = identity_maps = contradictions = alternate_groups = 0
if jpath.exists():
    for line in jpath.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        files = [f for f in ((rec.get("plan") or {}).get("files") or [])
                 if isinstance(f, dict) and f.get("src")]
        # Identity rule: rebuild the guide from the plan's own broadcast slots (the
        # provider list the accepted plan is consistent with) and ask whether the
        # identity claims would now reject any destination it made.
        entries = identify.release_title_entries([f["src"] for f in files])
        dash = identify.release_dash_title_entries([f["src"] for f in files])
        guide, plan_slot = [], {}
        if len(entries) + len(dash) >= 4:
            for f in files:
                m = re.search(r"Season\s+(\d+)/.*?S(\d+)E(\d+)", f.get("dst_rel") or "")
                t = (identify.release_title_entries([f["src"]])
                     or identify.release_dash_title_entries([f["src"]]))
                if not m or not t:
                    continue
                guide.append({"season": int(m.group(2)), "number": int(m.group(3)),
                              "name": t[0][3]})
                plan_slot[(t[0][1], t[0][2])] = (int(m.group(2)), int(m.group(3)))
        if len(guide) >= 4:
            plans_seen += 1
            claims = identify._title_claims(entries, dash, guide)
            imap = {k: v for k, v in claims.items()
                    if tuple(v) == tuple(k) and k[0] >= 1}
            if imap:
                identity_maps += 1
            for key in imap:
                filed = plan_slot.get(key)
                if filed and tuple(filed) != tuple(key):
                    contradictions += 1
        # Alternate rule: a same-destination-episode group with equal cores WOULD be
        # collapsed. No accepted historical plan carries one (the same-slot guard has
        # always refused two files on one numbered episode); the replay proves the new
        # collapse changes no accepted plan.
        by_key = {}
        for f in files:
            rel = Path(f.get("dst_rel") or "")
            if rel.parts[:1] != ("Shows",) \
                    or rel.suffix.lower() not in config.VIDEO_EXTENSIONS:
                continue
            m = re.search(r"[Ss](\d{1,3})[Ee](\d{1,4})", rel.name)
            if not m:
                continue
            core = journal.alternate_title_core(Path(str(f.get("src") or "")).name)
            if not core:
                continue
            by_key.setdefault((rel.parts[1], int(m.group(1)), int(m.group(2))),
                              []).append(core)
        for cores in by_key.values():
            if len(cores) > 1 and len(set(cores)) == 1:
                alternate_groups += 1
    print(f"  replayed {plans_seen} titled plan(s): {identity_maps} identity map(s), "
          f"{contradictions} remap contradiction(s), {alternate_groups} historical "
          f"alternate collapse(s)")
    check("the identity guard contradicts no accepted historical plan",
          contradictions == 0)
    check("the alternate collapse changes no accepted historical plan",
          alternate_groups == 0)

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
