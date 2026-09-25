#!/usr/bin/env python3
"""A same-slot file is only a duplicate when its CONTENT is the same episode (HANDOFF 10.9).

THE BLIND SPOT THIS CLOSES. `_collapse_existing_episode_collisions` scanned `~/Media`
(the SSD) only, and an EVICTED episode does not exist there (HANDOFF §2.1). The Smurfs'
40 old dvdrip S01 files were pool-only, so the replacement pack's S01 plan applied
beside them; the resulting same-stem pairs were then read as cleanup decisions and
media_doctor deleted the planner's copies. The scan now reads the MOUNT as well, and
when the journal records that the existing file's content is a DIFFERENT episode, the
plan PARKS instead of silently dropping the planned copy.

THE SECOND WITNESS (2026-09-24). A chunked wave files its episodes with `plan` null on
the record; the journal's plan-only index was blind to them, `_existing_episode_mismatch`
found no evidence, and the colliding planned file was silently dropped -- which the
coverage contract then parked as a terminal `failed` for the whole 700 GB pack (The
Simpsons: the model, working from an evicted-episodes-invisible digest, renumbered
S03E05 onto the occupied S03E03). Two additions:
  * `journal.source_titles()` indexes the record's `applied` entries too (the staging
    source path preserves the release filename), so a chunked filing HAS journal
    evidence;
  * when the journal is still silent, the existing file's OWN NAME is the witness, and
    the comparison runs on tag-cleaned titles -- two files of one release share their
    whole tag suffix, which inflates the raw similarity to 0.838, a hair under the
    same-episode bar.

    python3 scripts/test_existing_collision_identity.py

Fixtures only. Exit 0 = all checks passed.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                          # noqa: E402
import identify                                                        # noqa: E402
import ingest                                                          # noqa: E402
import journal                                                         # noqa: E402
import library                                                         # noqa: E402

failures: list[str] = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


REL_DIR = "Shows/A Show (2019)/Season 01"
EXISTING = "A Show (2019) - S01E31.mkv"
PLANNED_SRC = "/releases/The Smurfs S01E06 (The Astrosmurf).mp4"


_case_no = 0


def run_case(root, existing_title, existing_name=EXISTING,
             src_name="The Smurfs S01E06 (The Astrosmurf).mp4",
             dst_name="A Show (2019) - S01E31.mp4"):
    """Collide a planned S01E31 with an existing file; return the outcome.

    Each case gets its own root: cases differ exactly in what is already in the season
    folder, and a leftover file from a previous case would be a second collision."""
    global _case_no
    _case_no += 1
    mount = root / f"mount{_case_no}"
    ssd = root / f"media{_case_no}"
    (mount / REL_DIR).mkdir(parents=True, exist_ok=True)
    (ssd / REL_DIR).mkdir(parents=True, exist_ok=True)
    (mount / REL_DIR / existing_name).write_bytes(b"old")
    plan_file = {
        "src": f"/releases/{src_name}",
        "dst_rel": f"{REL_DIR}/{dst_name}",
        "season": 1, "episode": 31,
    }
    saved_mount, saved_media = config.MEDIAFS_MOUNT, config.MEDIA_ROOT
    saved_titles = journal.source_titles
    config.MEDIAFS_MOUNT, config.MEDIA_ROOT = mount, ssd
    if existing_title is None:
        journal.source_titles = lambda: {}
    else:
        journal.source_titles = lambda: {f"{REL_DIR}/{existing_name}": existing_title}
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

    print("Part 2 -- a silent journal names the episode from the existing FILE")
    tagged_existing = ("A Show (2019) - S01E31 - When Flanders Failed "
                       "[DSNP WEBDL-1080p][EAC3 5.1][h264]-HONE.mkv")
    tagged_src = ("A Show (2019) - S01E06 - Homer Defined "
                  "[DSNP WEBDL-1080p][EAC3 5.1][h264]-HONE.mkv")
    tagged_dst = ("A Show (2019) - S01E31 - Homer Defined "
                  "[DSNP WEBDL-1080p][EAC3 5.1][h264]-HONE.mkv")
    outcome, detail = run_case(root, None, existing_name=tagged_existing,
                               src_name=tagged_src, dst_name=tagged_dst)
    check("a different episode in the existing NAME parks the plan",
          outcome == "parked")
    check("the park names the CLEAN titles, not the shared tag tail",
          "When Flanders Failed" in detail and "Homer Defined" in detail)
    outcome, _d = run_case(
        root, None,
        existing_name="A Show (2019) - S01E31 - Homer Defined [720p]-OTHER.mkv",
        src_name=tagged_src, dst_name=tagged_dst)
    check("the same episode under DIFFERENT tags is still a duplicate",
          outcome == "dropped")
    outcome, _d = run_case(root, None,
                           existing_name="A Show (2019) - S01E31.mkv",
                           src_name=tagged_src, dst_name=tagged_dst)
    check("a bare-numbered existing name proves nothing -> historical drop",
          outcome == "dropped")

    print("Part 3 -- tag cleaning keeps a real title difference")
    check("identical tag-laden titles clean equal",
          library._clean_episode_title(
              "Homer Defined [DSNP WEBDL-1080p][EAC3 5.1][h264]-HONE")
          == "Homer Defined")
    check("a trailing -GROUP comes off",
          library._clean_episode_title("Marge vs. the Monorail -NTb")
          == "Marge vs. the Monorail")
    check("the shared-tag pair is far below the same-episode bar after cleaning",
          not library._titles_same_episode(
              library._clean_episode_title("Homer Defined [DSNP WEBDL-1080p][EAC3 5.1][h264]-HONE"),
              library._clean_episode_title("When Flanders Failed [DSNP WEBDL-1080p][EAC3 5.1][h264]-HONE")))

    print("Part 4 -- the journal's `applied` entries witness chunked filings")
    saved_journal = config.JOURNAL_FILE
    saved_media = config.MEDIA_ROOT
    config.MEDIA_ROOT = root / "media"
    config.JOURNAL_FILE = root / "journal.jsonl"
    staged = (f"/tmp/.ingest-staging/abc-3/{REL_DIR}/"
              f"A Show (2019) - S01E03 - Some Title [ABC].mkv")
    applied_dst = config.MEDIA_ROOT / REL_DIR / "A Show (2019) - S01E03 - Some Title [ABC].mkv"
    config.JOURNAL_FILE.write_text(json.dumps({
        "info_hash": "abc", "name": "A Show pack", "status": "downloading",
        "plan": None,                       # the chunked-wave shape
        "applied": [{"src": staged, "dst": str(applied_dst)}],
    }) + "\n", encoding="utf-8")
    journal._SOURCE_TITLES_CACHE.clear()
    book = journal.source_titles()
    check("a plan-less chunked record still names what it filed",
          book.get(f"{REL_DIR}/A Show (2019) - S01E03 - Some Title [ABC].mkv")
          == "Some Title [ABC]")
    check("a destination outside the media root is not invented",
          all(not k.startswith("/") for k in book))
    journal._SOURCE_TITLES_CACHE.clear()
    config.JOURNAL_FILE = saved_journal
    config.MEDIA_ROOT = saved_media

    print("Part 5 -- the per-file fallback PARKS a proven collision, never frees")
    # The raising twin of the `_collision_parked` check: `_ingest_one_file` used to
    # swallow a CollisionPark into `return []`, which keeps the bytes for
    # CHUNK_FILE_MAX_ATTEMPTS more cycles and then frees them UNFILED -- the one thing
    # HANDOFF 10.2 says a collision must never do.
    root5 = Path(tmp.name) / "case5"
    mount5, ssd5 = root5 / "mount", root5 / "media"
    (mount5 / REL_DIR).mkdir(parents=True, exist_ok=True)
    (ssd5 / REL_DIR).mkdir(parents=True, exist_ok=True)
    (mount5 / REL_DIR / "A Show (2019) - S01E31 - When Flanders Failed.mkv").write_bytes(b"old")
    download = root5 / "download"
    download.mkdir(parents=True, exist_ok=True)
    video = download / "A Show (2019) - S01E06 - Homer Defined.mp4"
    video.write_bytes(b"new")
    plan = {"media_type": "show", "title": "A Show", "year": 2019,
            "files": [{"src": str(video),
                       "dst_rel": f"{REL_DIR}/A Show (2019) - S01E31 - Homer Defined.mp4",
                       "season": 1, "episode": 31}]}
    saved_mount, saved_media = config.MEDIAFS_MOUNT, config.MEDIA_ROOT
    saved_run = identify.run_identify
    saved_load = identify.load_stored_plan
    saved_log = journal.log_decision
    config.MEDIAFS_MOUNT, config.MEDIA_ROOT = mount5, ssd5
    identify.run_identify = lambda *a, **k: (plan, "fixture")
    identify.load_stored_plan = lambda *a, **k: None
    journal.log_decision = lambda *a, **k: None
    try:
        verdict = ingest._ingest_one_file({"info_hash": "abc5", "name": "A Show S01"},
                                          video, "abc5-1", sibling_seasons={1})
    finally:
        identify.run_identify = saved_run
        identify.load_stored_plan = saved_load
        journal.log_decision = saved_log
        config.MEDIAFS_MOUNT, config.MEDIA_ROOT = saved_mount, saved_media
    check("a proven collision returns the PARK verdict", verdict is ingest._PARK)
    check("the download's only copy survives", video.exists())
finally:
    tmp.cleanup()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
