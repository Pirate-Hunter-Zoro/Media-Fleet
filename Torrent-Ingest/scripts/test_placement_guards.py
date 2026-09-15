#!/usr/bin/env python3
"""Replay every historical plan through the placement guards. Read-only.

WHY THIS IS A CHECKED-IN TEST AND NOT A ONE-OFF
    Both guards it covers were written, measured, and then substantially NARROWED because
    the replay showed them rejecting work the library had accepted correctly:

      * the season guard's first draft rejected 55 of 747 historical plans; 54 were false
        positives (anime cours that TVMaze splits into separate shows, One Pace's own arc
        numbering, a legitimate absolute-to-seasonal remap of `Pocket.Monsters.S01E80`).
        Only after narrowing it to "the plan's season exceeds BOTH the provider ceiling AND
        the source's own highest season" did it come down to the 2 genuinely wrong plans.
      * the comic guard's first draft rejected 52 of 1510 historical comic filings; 45 were
        correct one-volume standalones like `Uzumaki (Deluxe Edition)` -> `Uzumaki v01`.

    A guard that rejects real content is worse than the bug it fixes, and neither of those
    would have been caught by reasoning -- only by running them over what actually
    happened. So the replay lives here and should be run before any change to either guard.

        python3 scripts/test_placement_guards.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                          # noqa: E402
import library                                                         # noqa: E402

# The plans that ARE wrong, held as a CHECKED-IN FIXTURE rather than looked up in the
# journal.
#
# They used to be named here and fetched from `journal.jsonl` at run time, and that quietly
# stopped working. The journal is keyed by info-hash and this replay reads the LAST record
# per hash, so when the Dawn of the Croods pack was re-ingested correctly on 2026-09-01 its
# wrong plan was overwritten by a right one -- and the test began reporting
# `known-bad plan(s) no longer rejected` for a guard that was working perfectly. A
# regression test whose inputs are live mutable state cannot tell a broken guard from a
# repaired record, and this one had no way to say which it was looking at.
#
# So the wrong plans are frozen on disk. They are also the only inputs that prove each check
# still fires: one per check, named in the fixture's `why`.
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "known_bad_plans.json"


def _journal_plans():
    by_ih = {}
    with (config.STATE_DIR / "journal.jsonl").open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            by_ih[r.get("info_hash") or id(r)] = r
    return by_ih


def _known_bad():
    try:
        return json.loads(FIXTURE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  FAIL: cannot read {FIXTURE}: {exc}")
        return None


def test_known_bad() -> int:
    """Every frozen wrong plan must still be rejected. One per check."""
    cases = _known_bad()
    if cases is None:
        return 1
    bad = 0
    print(f"known-bad fixture: {len(cases)} plan(s)")
    skipped = 0
    for case in cases:
        plan = case["plan"]
        # A frozen plan is only meaningful while the state its check reads still exists.
        # Check 3 compares against the seasons the show already holds ON DISK, so once that
        # show is purged the case cannot be evaluated at all -- and reporting "not rejected"
        # then blames a guard that is working. Skip it LOUDLY rather than fail or pass it
        # (§4.114: a test whose inputs are live mutable state cannot tell a broken guard from
        # a changed library).
        need = case.get("requires_library_show")
        if need and not (config.SHOWS_ROOT / need).is_dir():
            skipped += 1
            print(f"  --  SKIPPED (precondition gone): {case['name'][:56]}")
            print(f"        needs {need!r} in the library; it has been purged")
            continue
        try:
            library._reject_season_gap(plan, plan["files"], case.get("sibling_seasons"))
        except library.PlanError:
            print(f"  ok  {case['name'][:70]}")
            continue
        bad += 1
        print(f"  !!  NOT REJECTED: {case['name'][:60]}")
        print(f"        {case['why']}")
    if skipped:
        print(f"  {skipped} case(s) skipped: the library state their check reads is gone")
    if bad:
        print(f"  FAIL: {bad} known-bad plan(s) would now be accepted")
    return 1 if bad else 0


def test_seasons() -> int:
    """No plan the library accepted correctly may be rejected.

    The journal is the false-POSITIVE corpus only. What must stay rejected is the fixture's
    job (`test_known_bad`), because journal records get rewritten when a title is re-ingested
    and a test cannot be allowed to depend on that.
    """
    expected = {c["name"] for c in (_known_bad() or [])}
    rejected = {}
    total = 0
    for _ih, r in _journal_plans().items():
        plan = r.get("plan")
        if not isinstance(plan, dict) or not (plan.get("files") or []):
            continue
        total += 1
        try:
            library._reject_season_gap(plan, plan["files"], r.get("sibling_seasons"))
        except library.PlanError as exc:
            rejected[r.get("name") or "?"] = str(exc)

    print(f"\nseason guard: {total} historical plans replayed, {len(rejected)} rejected")
    unexpected = set(rejected) - expected
    for name in sorted(rejected):
        mark = "  ok " if name in expected else "  !! "
        print(f"{mark}{name[:76]}")
    if unexpected:
        print(f"  FAIL: {len(unexpected)} plan(s) the library accepted are now rejected")
    return 1 if unexpected else 0


def test_comics() -> int:
    """Every comic filing ever made must be judged; only franchise-root ones may fail."""
    pairs = []
    rx = re.compile(r"filed (.+?) -> (/Users/mikeyferguson/Media/.+)$")
    log = Path(__file__).resolve().parent.parent / "direct_ingest.log"
    if log.exists():
        for line in log.open(encoding="utf-8", errors="replace"):
            m = rx.search(line.strip())
            if m:
                pairs.append((m.group(1), m.group(2).split("/Media/", 1)[1]))
    for _ih, r in _journal_plans().items():
        for f in ((r.get("plan") or {}).get("files") or []):
            d = f.get("dst_rel") or ""
            if d.startswith("Comics/"):
                pairs.append(((f.get("src") or "").split("/")[-1], d))
    pairs = list(dict.fromkeys(pairs))

    rejected = []
    for src, dst in pairs:
        try:
            library._reject_comic_at_franchise_root([{"src": f"/x/{src}", "dst_rel": dst}])
        except library.PlanError:
            rejected.append((src, dst))
    print(f"\ncomic guard: {len(pairs)} historical filings replayed, "
          f"{len(rejected)} rejected")
    bad = [(s, d) for s, d in rejected if len(Path(d).parts) != 3 and len(Path(d).parts) != 4]
    for s, d in rejected[:6]:
        print(f"  ok  {s[:48]:50} -> {d}")
    if len(rejected) > 6:
        print(f"  ... and {len(rejected) - 6} more, all files loose at a franchise root")
    if bad:
        print(f"  FAIL: {len(bad)} rejection(s) are not franchise-root filings")
    return 1 if bad else 0


def test_same_episode() -> int:
    """The same-episode guard: catches the serial collapse, allows every accepted shape."""
    def entry(show, season, episode, name):
        return {"dst_rel": f"Shows/{show}/Season {season:02d}/{name}",
                "season": season, "episode": episode, "src": f"/dl/{name}"}

    # The 2026-09-15 Doctor Who (1963) shape, compressed: six parts of one serial, all
    # filed under the release's serial number.
    dw = [entry("Doctor Who (1963)", 1, 5,
                f"Doctor Who (1963) - S01E05 - The Keys of Marinus ({i}) - part.avi")
          for i in range(1, 7)]
    try:
        library._reject_same_episode({}, dw)
        print("  FAIL: a serial collapsed onto one episode was accepted")
        return 1
    except library.PlanError:
        print("  ok  a multi-part serial collapsed onto one episode is rejected")

    # The same episode number in two different SHOWS is a multi-show pack, not a collision.
    multi = [entry("Steins;Gate (2011)", 1, 1, "Steins;Gate (2011) - S01E01.mkv"),
             entry("Steins;Gate 0 (2018)", 1, 1, "Steins;Gate 0 (2018) - S01E01.mkv")]
    try:
        library._reject_same_episode({}, multi)
        print("  ok  one episode number in two different shows is allowed")
    except library.PlanError as exc:
        print(f"  FAIL: a multi-show pack was rejected: {exc}")
        return 1

    # A split special in Season 00 is accepted content (Kaguya-sama S00E06, in the library).
    special = [entry("Kaguya-sama - Love Is War (2019)", 0, 6,
                     "Kaguya-sama - Love Is War (2019) - S00E06-Stairway to Adulthood - part1.mkv"),
               entry("Kaguya-sama - Love Is War (2019)", 0, 6,
                     "Kaguya-sama - Love Is War (2019) - S00E06-Stairway to Adulthood - part2.mkv")]
    try:
        library._reject_same_episode({}, special)
        print("  ok  a two-part Season-00 special is allowed")
    except library.PlanError as exc:
        print(f"  FAIL: an accepted Season-00 special shape was rejected: {exc}")
        return 1

    # The library's own convention for regular multi-parters: consecutive numbers.
    consecutive = [entry("The Office (US) (2005)", 5, 14, "S05E14 - Lecture Circuit (1).mkv"),
                   entry("The Office (US) (2005)", 5, 15, "S05E15 - Lecture Circuit (2).mkv")]
    try:
        library._reject_same_episode({}, consecutive)
        print("  ok  consecutively numbered parts pass")
    except library.PlanError:
        print("  FAIL: consecutively numbered parts were rejected")
        return 1

    # False-positive half: no plan the library accepted may now be rejected.
    total = rejected = 0
    for _ih, r in _journal_plans().items():
        plan = r.get("plan")
        if not isinstance(plan, dict) or not (plan.get("files") or []):
            continue
        total += 1
        try:
            library._reject_same_episode(plan, plan["files"])
        except library.PlanError:
            rejected += 1
    print(f"\nsame-episode guard: {total} accepted plans replayed, {rejected} rejected")
    if rejected:
        print("  FAIL: plan(s) the library accepted are now rejected")
    return 1 if rejected else 0


if __name__ == "__main__":
    rc = test_known_bad() | test_seasons() | test_comics() | test_same_episode()
    print("\nPASS" if rc == 0 else "\nFAIL")
    raise SystemExit(rc)
