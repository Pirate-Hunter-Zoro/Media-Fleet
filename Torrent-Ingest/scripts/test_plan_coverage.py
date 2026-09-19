#!/usr/bin/env python3
"""The plan-coverage contract and the release-identity guard (HANDOFF 10.1/10.2).

WHY THIS IS A CHECKED-IN TEST, in one sentence: the Smurfs lost 365 files / ~31 GB
to a 40-file plan whose cleanup deleted the whole download root, and Doctor Who (2005)
lost 38 files / 60.9 GB to a wave whose "not in plan (junk/duplicate)" branch freed
them. Both are the SAME missing seam -- a plan that names PART of a release is treated
as permission to delete the rest -- and a guard for it has to be measured against the
library's real history before anyone trusts it, because five guards before this one
shipped with false positives only a replay could see (HANDOFF §5).

The parts, and what each half protects:

  Part 1 -- the classifier both ways. A media file the plan does not name is
            UNRESOLVED (parks the release); a release .nfo, a sample, a creditless
            OP, a small anonymous video, a subtitle beside a planned video, and a
            whole planned DIRECTORY of loose pages are all ACCOUNTED FOR (exist
            legitimately in packs and must not park one).
  Part 2 -- the collision is parked, not deduped. A planned episode colliding with a
            differently-named file at the same slot lands in `_collision_parked`, and
            the release file it stands for stays unresolved -- the Doctor Who (2005)
            shape. The intra-torrent duplicate half stays `_deduped_dropped`, because
            its surviving copy IS in the plan.
  Part 3 -- the identity guard both ways. A release whose name states 2005 may not be
            filed into a `(1963)` folder; a remaster name carrying two years still
            passes if ANY year matches; a yearless release is never checked.
  Part 4 -- the re-arm path. `_rearm_indices` clears exactly the named indices from
            every proof list, so a re-drop re-fetches exactly the re-armed set --
            _carry_chunk_progress's own docstring says a dropped file is a deliberate
            verdict, and this is what makes the verdict reversible.
  Part 5 -- the journal replay. Every historical plan that still has its `.torrent`
            mirror is replayed through the classifier and the identity guard, and the
            would-park / would-reject counts are printed. Not an assertion: it is the
            measurement that says whether a rule tuned against one incident is about
            to reject hundreds of correct ones.

    python3 scripts/test_plan_coverage.py

Writes only inside temp dirs. Exit 0 means every check passed.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                          # noqa: E402
import ingest                                                          # noqa: E402
import library                                                         # noqa: E402
import plan_coverage                                                   # noqa: E402
import qbt                                                             # noqa: E402

scripts = str(Path(__file__).resolve().parent)
if scripts not in sys.path:
    sys.path.insert(0, scripts)
import refile_season                                                   # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


# ---------------------------------------------------------------------------
print("\nPart 1 -- the classifier: filed, junk, or unresolved")
# ---------------------------------------------------------------------------

tmp = Path(tempfile.mkdtemp(prefix="plan-coverage-test-")).resolve()

MEDIA = 200 * 1024 * 1024           # a real episode size
SMALL = 8 * 1024 * 1024             # a sample size

release = [
    ("Season 01/Show S01E01.mkv", MEDIA),
    ("Season 01/Show S01E02.mkv", MEDIA),
    ("Season 01/Show S01E03.mkv", MEDIA),
    ("Season 01/Show S01E01.srt", 40 * 1024),
    ("Season 01/Show S01E01.1080p.sample.mkv", SMALL),
    ("Show.S01E01.mkv", MEDIA),          # a differently-placed duplicate copy
    ("Subs/Show S01E02.en.srt", 40 * 1024),
    ("release.nfo", 5 * 1024),
    ("RARBG.txt", 100),
    ("Art/cover.jpg", 300 * 1024),
    ("Extras/creditless OP.mkv", 300 * 1024 * 1024),
]
planned = [
    {"src": str(tmp / "Season 01/Show S01E01.mkv")},
    {"src": str(tmp / "Season 01/Show S01E02.mkv")},
    {"src": str(tmp / "Season 01/Show S01E03.mkv")},
    {"src": str(tmp / "Show.S01E01.mkv")},     # the intra-torrent duplicate survivor
]
resolved_dup = [str(tmp / "Season 01/Show S01E01.mkv")]
unresolved, accounted = plan_coverage.release_gaps(
    release, planned, tmp, resolved_srcs=resolved_dup)
check("every file in this release is accounted for", unresolved, [])
check("the .nfo and .txt are accounted for", "release.nfo" in accounted, True)
check("the sample-sized video is accounted for", any("sample" in a for a in accounted), True)
check("the subtitle beside a planned video is accounted for",
      "Season 01/Show S01E01.srt" in accounted, True)
check("the subtitle for a planned video in another dir is accounted for",
      "Subs/Show S01E02.en.srt" in accounted, True)
check("the cover art is accounted for", "Art/cover.jpg" in accounted, True)
check("a creditless OP is accounted for",
      "Extras/creditless OP.mkv" in accounted, True)

# The Smurfs shape: one big episode missing from the plan is unresolved.
bad = [("Season 01/Show S01E01.mkv", MEDIA), ("Season 01/Show S01E04.mkv", MEDIA)]
unresolved, _ = plan_coverage.release_gaps(
    bad, [{"src": str(tmp / "Season 01/Show S01E01.mkv")}], tmp)
check("the Smurfs shape -- 2 files, 1 planned -- is unresolved", unresolved,
      ["Season 01/Show S01E04.mkv"])

# A planned DIRECTORY covers every page under it (the loose-pages -> .cbz case).
pages = [("Chapter 01/page01.jpg", 1_000_000), ("Chapter 01/page02.jpg", 1_000_000)]
unresolved, accounted = plan_coverage.release_gaps(
    pages, [{"src": str(tmp / "Chapter 01")}], tmp)
check("a planned loose-pages directory covers its pages", unresolved, [])
check("both pages are accounted for", len(accounted), 2)

# Fail-open: nothing to enumerate is not a park.
check("no release listing is a no-op", plan_coverage.release_gaps([], planned, tmp), ([], []))

# Replay-style matching: no content root, plan srcs point at a dead download root.
unresolved, _ = plan_coverage.release_gaps(
    [("Season 01/Show S01E01.mkv", MEDIA), ("Season 01/Show S01E09.mkv", MEDIA)],
    [{"src": "/gone/download-root/Season 01/Show S01E01.mkv"}], None,
    basename_fallback=True)
check("basename fallback (replay) still finds the unplanned file",
      unresolved, ["Season 01/Show S01E09.mkv"])

# ---------------------------------------------------------------------------
print("\nPart 2 -- a collision parks; an intra-torrent duplicate is accounted for")
# ---------------------------------------------------------------------------

saved_root = config.MEDIA_ROOT
try:
    lib = tmp / "library"
    (lib / "Shows/Example (2001)/Season 01").mkdir(parents=True)
    (lib / "Shows/Example (2001)/Season 01/Example (2001) - S01E01 - Old.mkv").write_bytes(b"x")
    config.MEDIA_ROOT = lib

    dl = tmp / "download"
    dl.mkdir()
    (dl / "Example S01E01 New.mkv").write_bytes(b"y")
    plan = {
        "media_type": "show",
        "files": [{
            "src": str(dl / "Example S01E01 New.mkv"),
            "dst_rel": "Shows/Example (2001)/Season 01/Example (2001) - S01E01 - New.mkv",
            "type": "episode", "season": 1, "episode": 1,
        }],
    }
    library.validate_plan(plan, str(dl), release_name="Example S01E01 New.mkv")
    check("the colliding file is parked, not deduped", len(plan.get("_collision_parked") or []), 1)
    check("nothing was written to _deduped_dropped", plan.get("_deduped_dropped"), None)
    unresolved, _ = plan_coverage.release_gaps(
        [("Example S01E01 New.mkv", MEDIA)], plan["files"], dl)
    check("the release file behind the collision stays unresolved", unresolved,
          ["Example S01E01 New.mkv"])

    # The intra-torrent duplicate keeps its old home: its survivor IS in the plan.
    (dl / "Example S01E02 alt.mkv").write_bytes(b"z")
    (dl / "Example S01E02.mkv").write_bytes(b"zz")
    plan2 = {
        "media_type": "show",
        "files": [
            {"src": str(dl / "Example S01E02.mkv"),
             "dst_rel": "Shows/Example (2001)/Season 01/Example (2001) - S01E02.mkv",
             "type": "episode", "season": 2, "episode": 2},
            {"src": str(dl / "Example S01E02 alt.mkv"),
             "dst_rel": "Shows/Example (2001)/Season 01/Example (2001) - S01E02.mkv",
             "type": "episode", "season": 2, "episode": 2},
        ],
    }
    library.validate_plan(plan2, str(dl), release_name="Example S01E02 pack")
    deduped = plan2.get("_deduped_dropped") or []
    check("the intra-torrent duplicate is deduped", len(deduped), 1)
    check("its drop reason is 'duplicate'", deduped[0].get("reason"), "duplicate")
    unresolved, _ = plan_coverage.release_gaps(
        [("Example S01E02 alt.mkv", MEDIA), ("Example S01E02.mkv", MEDIA)],
        plan2["files"], dl, resolved_srcs=[d.get("src") for d in deduped])
    check("the deduped copy does not park the release", unresolved, [])
finally:
    config.MEDIA_ROOT = saved_root

# ---------------------------------------------------------------------------
print("\nPart 3 -- the release-identity guard")
# ---------------------------------------------------------------------------


def identity_raises(release_name, folder, plan_year=None):
    plan = {"media_type": "show", "files": [{
        "dst_rel": f"Shows/{folder}/Season 01/x.mkv",
        "src": "/dl/x.mkv", "type": "episode", "season": 1, "episode": 1,
    }]}
    if plan_year is not None:
        plan["year"] = plan_year
        plan["title"] = folder.rsplit(" (", 1)[0]
    try:
        library._reject_release_identity(plan, plan["files"], release_name)
        return False
    except library.PlanError:
        return True


check_true("Doctor Who 2005 into 'Doctor Who (1963)' is refused",
           identity_raises("Doctor Who 2005 Season 1 1080p", "Doctor Who (1963)"))
check_true("The Twilight Zone 2019 into 'The Twilight Zone (1959)' is refused",
           identity_raises("The Twilight Zone 2019 Complete", "The Twilight Zone (1959)"))
check("a two-year remaster name passes on ANY matching year",
      identity_raises("Dragon Ball Z 1989 2007 Remaster Complete Series",
                      "Dragon Ball Z (1989)"), False)
check("a yearless release is never checked",
      identity_raises("The Smurfs (Complete cartoon series in MP4 format.)",
                      "The Smurfs (1981)"), False)
check("a correct same-year release passes",
      identity_raises("One Piece 1999 Season 1", "One Piece (1999)"), False)
check("unrelated names with a stray year stay silent",
      identity_raises("Some Random Collection 2020 BDRip", "Bleach (2004)"), False)
check_true("a plan whose own year contradicts the folder is refused",
           identity_raises(None, "Doctor Who (1963)", plan_year=2005))
check("a plan year within one of the folder passes",
      identity_raises(None, "Battlestar Galactica (2004)", plan_year=2003), False)

# ---------------------------------------------------------------------------
print("\nPart 4 -- re-arm: a re-drop re-fetches exactly the re-armed set")
# ---------------------------------------------------------------------------

saved_still = ingest._still_in_library
try:
    ingest._still_in_library = lambda rel: (True, True)   # every filed path proven
    # `chunk_dropped` is a DELIBERATE verdict: index 4 was declined by a plan. `chunk_filed`
    # proves only 1. Index 4 is in both done and dropped, so a plain re-drop carries it and
    # the release reports completed without re-fetching.
    old = {
        "chunked": True,
        "chunk_done": [1, 4],
        "chunk_dropped": [4],
        "chunk_failed_idx": [],
        "chunk_filed": {"1": "Shows/X/Season 01/a.mkv"},
    }
    fresh: dict = {}
    carried = ingest._carry_chunk_progress(old, fresh)
    check("a plain re-drop carries the proven file and the dropped verdict", carried, 2)
    check("the dropped verdict is carried", fresh.get("chunk_dropped"), [4])

    rec = json.loads(json.dumps(old))
    cleared = refile_season._rearm_indices(rec, [4])
    check("re-arm clears exactly the named index", cleared, [4])
    fresh2: dict = {}
    carried2 = ingest._carry_chunk_progress(rec, fresh2)
    check("after re-arm only the untouched index is carried", carried2, 1)
    check("the re-armed dropped index is not carried", fresh2.get("chunk_dropped"), [])
    check("the re-armed index is gone from chunk_done", fresh2.get("chunk_done"), [1])

    # Control: an index whose bytes are FILED is never re-armed -- re-fetching it would
    # duplicate content already in the library.
    rec2 = json.loads(json.dumps(old))
    check("re-arming a filed index is refused", refile_season._rearm_indices(rec2, [1]), [])
    check("its chunk_filed claim survives", rec2["chunk_filed"].get("1"),
          "Shows/X/Season 01/a.mkv")
finally:
    ingest._still_in_library = saved_still

# ---------------------------------------------------------------------------
print("\nPart 5 -- journal replay: would the new rules have rejected history?")
# ---------------------------------------------------------------------------

records = {}
journal_file = config.STATE_DIR / "journal.jsonl"
if journal_file.exists():
    with journal_file.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            records[r.get("info_hash") or id(r)] = r

mirror = config.TORRENT_SOURCE_MIRROR
would_park, would_reject, replayed, no_source, chunked = [], [], 0, 0, 0
for h, r in records.items():
    plan = r.get("plan")
    if not isinstance(plan, dict) or not (plan.get("files") or []):
        continue
    try:
        library._reject_release_identity(plan, plan["files"], r.get("name"))
    except library.PlanError as exc:
        would_reject.append((r.get("name") or "?", str(exc).split(":")[0]))
    if r.get("chunked"):
        # A chunked record's `plan` is its LAST WAVE, not the release: comparing it to
        # the whole `.torrent` would report every other wave's files as unplanned. The
        # production contract runs per-wave against what is on disk; the whole-release
        # replay is the whole-torrent path's (which is where the Smurfs damage was).
        chunked += 1
        continue
    src = mirror / f"{h}.torrent"
    if not src.exists():
        no_source += 1
        continue
    release = qbt.file_list_from_file(src)
    if not release:
        no_source += 1
        continue
    replayed += 1
    resolved = [d.get("src") for d in (plan.get("_deduped_dropped") or [])
                if isinstance(d, dict)]
    unresolved, _ = plan_coverage.release_gaps(
        release, plan["files"], r.get("content_path"), resolved_srcs=resolved,
        basename_fallback=True)
    if unresolved:
        would_park.append((r.get("name") or "?", len(unresolved), unresolved[:3]))

print(f"  replayed {replayed} whole-torrent plans with a surviving .torrent "
      f"({no_source} without one, {chunked} chunked records skipped by design)")
print(f"  would-park (unresolved media in the plan): {len(would_park)}")
for name, n, sample in sorted(would_park, key=lambda x: -x[1])[:12]:
    print(f"    {n:5d}  {name[:56]:58s} e.g. {sample}")
if len(would_park) > 12:
    print(f"    ... and {len(would_park) - 12} more")
print(f"  identity-guard rejects: {len(would_reject)}")
for name, why in would_reject[:12]:
    print(f"          {name[:70]:72s} {why}")
if len(would_reject) > 12:
    print(f"    ... and {len(would_reject) - 12} more")

# A replay that parks MOST of history is a rule that would have stopped the fleet
# filing anything; the guard is wrong, not history. This is a tripwire, not a tuning
# target -- if it fires, read the list above before changing the threshold.
if replayed and len(would_park) > replayed * 0.25:
    print(f"  FAIL: {len(would_park)}/{replayed} historical plans would park; the "
          f"classifier is rejecting real work")
    failures.append("replay would-park rate")

print()
if failures:
    print(f"FAIL: {len(failures)} check(s) failed")
    raise SystemExit(1)
print("PASS")
