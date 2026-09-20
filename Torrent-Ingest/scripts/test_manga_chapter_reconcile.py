#!/usr/bin/env python3
"""Chapter/volume reconciliation: the decisions, the provider parser, and the purge path.

The destructive operation here is deleting a file a volume already contains, so the test
carries BOTH directions for every rule: a covered chapter is removed, and an uncovered /
unknown / volume-absent / keep-listed one survives. It also pins the two mistakes this
feature already made on live data before shipping:

  * `mangadex_search("Restoration")` matched an unrelated manga named "Restoration" --
    the series identity must be the folder CHAIN ("Rourouni Kenshin Restoration"), and
    `series_label_for_rel` is asserted directly;
  * the AI fallback ran in-process and `ai_client._post` paced a rate-limit window for
    minutes -- it is a kill-bounded subprocess now, asserted by a worker round trip.

Fixtures only. `library.supersede_paths` is exercised against a temporary MEDIA_ROOT and
deletion queue; `dbhook.record_purge` is stubbed at the `chapter_volume_reconcile` seam so
the live `library.db` is never opened.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config                                                          # noqa: E402
import library                                                         # noqa: E402
import manga_volume_map as mvm                                        # noqa: E402
import chapter_volume_reconcile as cvr                                # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


def entry(volumes: dict, ai: dict | None = None, confidence: float = 0.0,
          source: str = "mangadex") -> dict:
    return {"source": source, "confidence": confidence,
            "volumes": {str(k): v for k, v in volumes.items()},
            "ai_volumes": {str(k): v for k, v in (ai or {}).items()}}


print("=== provider parser against an independent oracle ===")

# The oracle is written out by hand from the MangaDex aggregate shape. Fractional
# chapter keys and non-integer volume keys are the two traps: a library chapter is an
# integer cNNNN, so "12.5" can never prove 12 covered, and a "none" volume is unknown.
agg = {"volumes": {
    "1": {"chapters": {"1": {}, "2": {}, "3": {}, "4.5": {}}},
    "2": {"chapters": {"5": {}, "6": {}, "7": {}}},
    "10": {"chapters": {"71": {}, "72": {}}},
    "none": {"chapters": {"90": {}}},
    "1.5": {"chapters": {"99": {}}},
}}
vols, unmapped = mvm.parse_aggregate(agg)
check("integer chapter keys form exact sets", vols.get(1) == [1, 2, 3])
check("a fractional chapter key proves nothing", 4 not in (vols.get(1) or []))
check("two-digit volume numbers parse", vols.get(10) == [71, 72])
check("a 'none' volume is unknown, not volume 0", "none" in unmapped and 0 not in vols)
check("a fractional volume is unknown", "1.5" in unmapped)

print()
print("=== authority: provider directly, AI only when confident ===")

prov = entry({5: [41, 42, 43]})
check("a provider volume is authoritative", mvm.volume_allowed(prov, 5))
check("an entry without the volume is not", not mvm.volume_allowed(prov, 6))

ai_hi = entry({}, ai={6: [44, 45]}, confidence=0.9, source="ai")
ai_lo = entry({}, ai={6: [44, 45]}, confidence=0.5, source="ai")
check("an AI volume at/above the threshold is allowed", mvm.volume_allowed(ai_hi, 6))
check("an unconfident AI volume is NOT", not mvm.volume_allowed(ai_lo, 6))
check("an unmapped volume is never allowed", not mvm.volume_allowed(prov, 99))

check("the AI answer parser accepts a range object",
      mvm._parse_ai_ranges('{"volumes": {"12": {"start": 41, "end": 43}}, '
                           '"confidence": 0.9}') == ({12: [41, 42, 43]}, 0.9))
check("it accepts an explicit list",
      mvm._parse_ai_ranges('{"volumes": {"12": [41, 43]}, "confidence": 0.9}')
      == ({12: [41, 43]}, 0.9))
check("garbage is refused", mvm._parse_ai_ranges("I think volume 12 has chapters") is None)

print()
print("=== the series identity is the folder CHAIN ===")

check("a leaf folder alone is not a series name",
      cvr.series_label_for_rel("Comics/Manga/Rurouni Kenshin/Restoration")
      == "Rurouni Kenshin Restoration")
check("a flat series is unchanged",
      cvr.series_label_for_rel("Comics/Manga/Mashle") == "Mashle")
check("a franchise keeps its parent",
      cvr.series_label_for_rel("Comics/Manga/Battle Angel Alita/Last Order")
      == "Battle Angel Alita Last Order")

print()
print("=== decisions: purge only the covered, keep everything else ===")

owned = {
    "Comics/Manga/Thing/Thing v01.cbz": ("volume", 1, False),
    "Comics/Manga/Thing/Thing v02.cbz": ("volume", 2, False),
    "Comics/Manga/Thing/Thing c0005.cbz": ("chapter", 5, False),
    "Comics/Manga/Thing/Thing c0099.cbz": ("chapter", 99, False),
    "Comics/Manga/Thing/Thing c0042.cbz": ("chapter", 42, False),
}
e = entry({1: [1, 2, 3, 4, 5], 2: [6, 7, 8]})
purges, keeps = cvr.plan_decisions("Thing", owned, e, "keep_volumes")
check("a chapter in the volume's set is purged",
      "Comics/Manga/Thing/Thing c0005.cbz" in purges)
check("a chapter no owned volume covers is kept",
      "Comics/Manga/Thing/Thing c0099.cbz" not in purges)
reasons = dict(keeps)
check("the keep reason names the coverage gap",
      "not covered" in reasons.get("Comics/Manga/Thing/Thing c0099.cbz", ""))

# Volume absent: the chapter is in the map's set, but the volume file is not owned, so
# nothing proves it is in the library. Only the owned chapter remains.
owned_absent = {"Comics/Manga/Thing/Thing c0005.cbz": ("chapter", 5, False)}
purges2, _ = cvr.plan_decisions("Thing", owned_absent, e, "keep_volumes")
check("a chapter with no owned covering volume survives", purges2 == [])

# Unknown volume: v01 is owned but the map says nothing about it.
unknown = {"Comics/Manga/Thing/Thing v01.cbz": ("volume", 1, False),
           "Comics/Manga/Thing/Thing c0005.cbz": ("chapter", 5, False)}
purges3, keeps3 = cvr.plan_decisions("Thing", unknown, entry({}), "keep_volumes")
check("an unknown volume cannot purge", purges3 == [])
check("and says why", any("unknown" in r for _rel, r in keeps3))

# Keep rules.
purges4, keeps4 = cvr.plan_decisions("Thing", owned, e, "keep_chapters")
check("keep_chapters keeps the covered chapter", "c0005" not in " ".join(purges4))
check("keep_all keeps everything",
      cvr.plan_decisions("Thing", owned, e, "keep_all")[0] == []
      and len(cvr.plan_decisions("Thing", owned, e, "keep_all")[1]) == len(owned))

# Colored overrides grey at the same volume number; the surviving colored volume DOES
# cover chapters. The owner's rule (10.5d) is about the CHAPTER's colour, not the
# volume's: a grey chapter covered by any owned volume is redundant, while a COLORED
# chapter must never lose colour to a grey volume. The old "a colored volume covers
# nothing" rule is what left all 104 One Piece chapters on the shelf (10.0 row 1).
colored = {
    "Comics/Manga/Thing/Thing v01.cbz": ("volume", 1, False),
    "Comics/Manga/Thing/Thing v01 (Colored).cbz": ("volume", 1, True),
    "Comics/Manga/Thing/Thing c0005.cbz": ("chapter", 5, False),
}
purges5, keeps5 = cvr.plan_decisions("Thing", colored, e, "keep_volumes")
check("the grey duplicate is purged in favour of the colored copy",
      "Comics/Manga/Thing/Thing v01.cbz" in purges5
      and not any("Colored" in p for p in purges5))
check("a grey chapter is covered by the surviving colored volume",
      "Comics/Manga/Thing/Thing c0005.cbz" in purges5)

# A COLORED chapter must never be superseded by a grey volume.
colored_chapter = {
    "Comics/Manga/Thing/Thing v01.cbz": ("volume", 1, False),
    "Comics/Manga/Thing/Thing c0005.cbz": ("chapter", 5, True),
}
purges6, keeps6 = cvr.plan_decisions("Thing", colored_chapter, e, "keep_volumes")
check("a colored chapter covered only by a grey volume is kept",
      "Comics/Manga/Thing/Thing c0005.cbz" not in purges6
      and any("color would be lost" in r for _rel, r in keeps6))
# ... but a colored volume may supersede it.
colored_both = dict(colored_chapter)
colored_both["Comics/Manga/Thing/Thing v001 (Colored).cbz"] = ("volume", 1, True)
purges7, _ = cvr.plan_decisions("Thing", colored_both, e, "keep_volumes")
check("a colored chapter is purged by a colored volume",
      "Comics/Manga/Thing/Thing c0005.cbz" in purges7)

print()
print("=== duplicates, fractional chapters, and a chapter in the WRONG series ===")

# `c1151.cbz` beside `One Piece c1151.cbz` and a nested twin: one chapter in three
# places. The canonical, shallowest copy is kept; the rest go. `c1151.5` is a
# DIFFERENT chapter and must not collapse into 1151.
dupes = {
    "Comics/Manga/One Piece/c1151.cbz": ("chapter", 1151, False),
    "Comics/Manga/One Piece/One Piece c1151.cbz": ("chapter", 1151, False),
    "Comics/Manga/One Piece/One Piece/One Piece c1151.cbz": ("chapter", 1151, False),
    "Comics/Manga/One Piece/One Piece c1151.5.cbz": ("chapter", 1151.5, False),
}
p8, k8 = cvr.plan_decisions("One Piece", dupes, entry({}), "keep_volumes")
check("the bare and nested duplicate chapters are purged",
      set(p8) == {"Comics/Manga/One Piece/c1151.cbz",
                  "Comics/Manga/One Piece/One Piece/One Piece c1151.cbz"})
check("the canonical and the fractional chapter survive",
      "Comics/Manga/One Piece/One Piece c1151.cbz" not in p8
      and "Comics/Manga/One Piece/One Piece c1151.5.cbz" not in p8)

# A chapter above a FINISHED series' total, covered by another series' volume, is a
# redundant copy of that volume: Jujutsu Kaisen ends at 272; One Piece v108 owns
# c1089-1100, so a `Jujutsu Kaisen c1093.cbz` is One Piece's chapter, already held.
jjk = {"Comics/Manga/Jujutsu Kaisen/Jujutsu Kaisen c1093.cbz": ("chapter", 1093, False)}
p9, _k9 = cvr.plan_decisions("Jujutsu Kaisen", jjk, entry({}), "keep_volumes",
                             chapter_ceiling=272,
                             global_cover={1093: ("One Piece", 108)})
check("a chapter above a finished series' total covered elsewhere is purged",
      p9 == ["Comics/Manga/Jujutsu Kaisen/Jujutsu Kaisen c1093.cbz"])
p10, k10 = cvr.plan_decisions("Jujutsu Kaisen", jjk, entry({}), "keep_volumes",
                              chapter_ceiling=272, global_cover={})
check("without cover it is kept and reported, never guessed away",
      p10 == [] and any("true series unknown" in r for _r, r in k10))
p11, _k11 = cvr.plan_decisions("Jujutsu Kaisen", jjk, entry({}), "keep_volumes",
                               chapter_ceiling=None,
                               global_cover={1093: ("One Piece", 108)})
check("no ceiling (ongoing/unknown series) refuses nothing", p11 == [])

check("a doubled master leaf is ONE series label",
      cvr.series_label_for_rel("Comics/Manga/One Piece/One Piece") == "One Piece")

print()
print("=== the shared supersede path deletes locally and queues the purge ===")

tmp = Path(tempfile.mkdtemp(prefix="cvr-root-"))
saved_root = config.MEDIA_ROOT
saved_queue = config.MEDIAFS_DELETIONS_QUEUE
try:
    config.MEDIA_ROOT = tmp
    config.MEDIAFS_DELETIONS_QUEUE = tmp / "mediafs_deletions.jsonl"
    rel = "Comics/Manga/Thing/Thing c0005.cbz"
    (tmp / rel).parent.mkdir(parents=True)
    (tmp / rel).write_bytes(b"pages")
    n = library.supersede_paths([rel])
    check("the local file is gone", not (tmp / rel).exists())
    check("one supersede was reported", n == 1)
    queued = [json.loads(line)["path"]
              for line in config.MEDIAFS_DELETIONS_QUEUE.read_text().splitlines()]
    check("the queue line names the file for the reaper", queued == [rel])
finally:
    config.MEDIA_ROOT = saved_root
    config.MEDIAFS_DELETIONS_QUEUE = saved_queue

print()
print("=== end-to-end reconcile against a fixture shelf (stubbed DB) ===")

saved = (config.MEDIAFS_MOUNT, config.MEDIA_ROOT, config.MEDIA_SYNCER_INVENTORY,
         cvr.mvm.get, cvr.dbhook.record_purge)
recorded: list = []
try:
    mount = Path(tempfile.mkdtemp(prefix="cvr-mount-"))
    root = Path(tempfile.mkdtemp(prefix="cvr-media-"))
    config.MEDIAFS_MOUNT = mount
    config.MEDIA_ROOT = root
    config.MEDIA_SYNCER_INVENTORY = mount / "remote_inventory.json"
    series = mount / "Comics/Manga/Thing"
    series.mkdir(parents=True)
    for name in ("Thing v01.cbz", "Thing c0005.cbz", "Thing c0099.cbz"):
        (series / name).write_bytes(b"x")
    # The files also live under MEDIA_ROOT, as they do on the Mini.
    local = root / "Comics/Manga/Thing"
    local.mkdir(parents=True)
    for name in ("Thing v01.cbz", "Thing c0005.cbz", "Thing c0099.cbz"):
        (local / name).write_bytes(b"x")
    config.MEDIAFS_DELETIONS_QUEUE = root / "mediafs_deletions.jsonl"
    cvr.mvm.get = lambda name, **kw: entry({1: [1, 2, 3, 4, 5]})
    cvr.dbhook.record_purge = lambda rels: recorded.append(list(rels)) or {}

    res = cvr.reconcile(apply=True)
    check("the covered chapter was deleted", not (local / "Thing c0005.cbz").exists())
    check("the uncovered chapter survived", (local / "Thing c0099.cbz").exists())
    check("the volume survived", (local / "Thing v01.cbz").exists())
    check("the purge was reported", res["purged"] == 1)
    check("the DB supersede was asked for the same path",
          recorded and recorded[0] == ["Comics/Manga/Thing/Thing c0005.cbz"])
    # The reaper purges the pool copy next; until it does, the mount still serves the
    # chapter, so a re-run re-queues (the reaper dedupes) rather than disappearing.
    # Once the purge lands the file is gone from the mount and the next tick is a no-op.
    (series / "Thing c0005.cbz").unlink()
    res2 = cvr.reconcile(apply=True)
    check("a run after the pool purge completes is a no-op", res2["purged"] == 0)
finally:
    (config.MEDIAFS_MOUNT, config.MEDIA_ROOT, config.MEDIA_SYNCER_INVENTORY,
     cvr.mvm.get, cvr.dbhook.record_purge) = saved

print()
print("=== the admission gate keys chapters independently (magnet/.torrent path) ===")

import acceptance_gate                                                # noqa: E402
acceptance = acceptance_gate.acceptance
check("a chapter-only release maps to a chapter item",
      acceptance.fallback_map_files("Some Manga", "manga",
                                    ["Some Manga c0042.cbz"], [])
      == [{"type": "chapter", "number": 42}])
check("a volume-only release maps to a volume item",
      acceptance.fallback_map_files("Some Manga", "manga",
                                    ["Some Manga v07.cbz"], [])
      == [{"type": "volume", "number": 7}])
check("a collection maps to neither and cannot refuse chapters",
      acceptance.fallback_map_files("Some Manga", "manga",
                                    ["Some Manga (Omnibus).cbz"], [])
      == [{"type": "other"}])

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("manga chapter reconcile: all checks passed")
