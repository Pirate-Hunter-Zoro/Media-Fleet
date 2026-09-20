#!/usr/bin/env python3
"""A same-slot file is only a duplicate when its CONTENT is the same episode (HANDOFF 10.9).

THE BLIND SPOT THIS CLOSES. `_collapse_existing_episode_collisions` scanned `~/Media`
(the SSD) only, and an EVICTED episode does not exist there (HANDOFF §2.1). The Smurfs'
40 old dvdrip S01 files were pool-only, so the replacement pack's S01 plan applied
beside them; the resulting same-stem pairs were then read as cleanup decisions and
media_doctor deleted the planner's copies. The scan now reads the MOUNT as well, and
when the journal records that the existing file's content is a DIFFERENT episode, the
plan PARKS instead of silently dropping the planned copy.

    python3 scripts/test_existing_collision_identity.py

Fixtures only. Exit 0 = all checks passed.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                          # noqa: E402
import journal                                                         # noqa: E402
import library                                                         # noqa: E402

failures: list[str] = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


REL_DIR = "Shows/A Show (2019)/Season 01"
EXISTING = "A Show (2019) - S01E31.mkv"


def run_case(root, existing_title):
    """Collide a planned S01E31 (The Astrosmurf) with an existing file; return outcome."""
    mount = root / "mount"
    ssd = root / "media"
    (mount / REL_DIR).mkdir(parents=True, exist_ok=True)
    (ssd / REL_DIR).mkdir(parents=True, exist_ok=True)
    (mount / REL_DIR / EXISTING).write_bytes(b"old")
    plan_file = {
        "src": "/releases/The Smurfs S01E06 (The Astrosmurf).mp4",
        "dst_rel": f"{REL_DIR}/A Show (2019) - S01E31.mp4",
        "season": 1, "episode": 31,
    }
    saved_mount, saved_media = config.MEDIAFS_MOUNT, config.MEDIA_ROOT
    saved_titles = journal.source_titles
    config.MEDIAFS_MOUNT, config.MEDIA_ROOT = mount, ssd
    if existing_title is None:
        journal.source_titles = lambda: {}
    else:
        journal.source_titles = lambda: {f"{REL_DIR}/{EXISTING}": existing_title}
    try:
        try:
            kept, dropped = library._collapse_existing_episode_collisions([plan_file])
            return ("kept" if kept else "dropped"), ""
        except library.PlanError as exc:
            return "parked", str(exc)
    finally:
        config.MEDIAFS_MOUNT, config.MEDIA_ROOT = saved_mount, saved_media
        journal.source_titles = saved_titles


print("Part 1 -- the mount is scanned, and identity decides")
tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name)
    outcome, detail = run_case(root, "The Smurfette")
    check("a pool-only existing file is seen at all", outcome != "kept")
    check("a contradictory identity PARKS the plan (no silent drop/pick)",
          outcome == "parked")
    check("the park names both identities",
          "The Smurfette" in detail and "The Astrosmurf" in detail)
    outcome, _d = run_case(root, "The Astrosmurf")
    check("the same episode is still collapsed as a duplicate", outcome == "dropped")
    outcome, _d = run_case(root, None)
    check("unknown identity keeps the historical drop (no new parks)",
          outcome == "dropped")
finally:
    tmp.cleanup()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
