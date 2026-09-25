#!/usr/bin/env python3
"""A show summary counts every episode the library HOLDS, evicted ones included.

THE PARKING THIS PREVENTS (2026-09-24). `show_metadata_summary` walked the SSD
(`~/Media`) only. Eviction is the normal state for anything not recently added, so it
counted the locally-cached SUBSET of a show -- and those counts are handed to the
identify run as ground truth ("USE IT to place a new drop"). Measured: The Simpsons
held 40 episodes and Season 03 held 4, while the SSD read said "Season 03 (2 eps)"
with no other seasons. The run renumbered the next wave's S03E05-S03E24 down by two
to fill the phantom gap, onto slots S03E03/E04 already held; the collision guard
dropped those planned files and the coverage contract parked the whole 700 GB pack
as FAILED. American Dad! was parked the same hour by the same stale picture (the SSD
said no seasons at all; the library held 34 episodes).

The fix folds Media-Syncer's remote inventory -- the COMPLETE view, the same source
`_comics_coverage` already reads -- into the episode enumeration. `.nfo` sidecars
survive eviction, so the metadata halves of the summary stay local.

    python3 scripts/test_show_summary_inventory.py

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
import library                                                         # noqa: E402

failures: list[str] = []
SHOW = "A Show (2001)"
SEASON1 = f"Shows/{SHOW}/Season 01"
SEASON2 = f"Shows/{SHOW}/Season 02"
E01 = f"{SEASON1}/{SHOW} - S01E01.mkv"
E02 = f"{SEASON2}/{SHOW} - S02E01.mkv"


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


def nfo_text(plot=True, locked=False):
    body = "<episodetitle>A Real Episode</episodetitle>"
    if plot:
        body += "<plot>A real synopsis.</plot>"
    if locked:
        body = body.replace("</episodetitle>",
                            "</episodetitle><lockdata>true</lockdata>")
    return f"<?xml version=\"1.0\"?><episodedetails>{body}</episodedetails>"


tmp = Path(tempfile.mkdtemp(prefix="show-summary-")).resolve()
media = tmp / "media"                       # MEDIA_ROOT, the SSD cache
mount = tmp / "mount"                       # MEDIAFS_MOUNT, the FUSE view
inventory = tmp / "remote_inventory.json"
cache_file = tmp / "library_summary.json"

saved = (config.MEDIA_ROOT, config.MEDIAFS_MOUNT, config.SHOWS_ROOT,
         config.MEDIA_SYNCER_INVENTORY, library._SUMMARY_CACHE_FILE)
config.MEDIA_ROOT = media
config.MEDIAFS_MOUNT = mount
config.SHOWS_ROOT = media / "Shows"
config.MEDIA_SYNCER_INVENTORY = inventory
library._SUMMARY_CACHE_FILE = cache_file
library._INVENTORY_EPISODES_CACHE.clear()


def write_inventory(paths):
    inventory.write_text(json.dumps({p: ["acct", "2026-09-24T00:00:00-05:00", 10]
                                     for p in paths}), encoding="utf-8")


def reset_cache():
    library._INVENTORY_EPISODES_CACHE.clear()
    if cache_file.exists():
        cache_file.unlink()


# S01E01 is local (video + sidecar); S02E01's video was EVICTED -- only its sidecar
# remains, and only the inventory still names the video.
(media / E01).parent.mkdir(parents=True, exist_ok=True)
(media / E01).write_bytes(b"v" * 10)
(media / E01).with_suffix(".nfo").write_text(nfo_text(plot=True, locked=True),
                                             encoding="utf-8")
(media / E02).parent.mkdir(parents=True, exist_ok=True)
(media / E02).with_suffix(".nfo").write_text(nfo_text(plot=True, locked=True),
                                             encoding="utf-8")
# The mount is the merged view on the real box; the fixture only needs the local side.
write_inventory([E01, E02])

print("Part 1 -- the inventory's evicted episode is counted")
reset_cache()
s = library.show_metadata_summary(media / "Shows" / SHOW)
check("both episodes are counted (SSD alone would say 1)", s["episodes"] == 2)
check("Season 02 is visible as an owned season",
      s["season_counts"].get("2") == 1 and s["season_counts"].get("1") == 1)
check("they number as two real seasons", s["numbering"] == "seasoned (2 seasons)")
check("the evicted episode's local sidecar still counts as locked",
      s["locked"] == 2)

print("Part 2 -- the digest line the identify run reads carries the whole picture")
reset_cache()
digest = library.build_library_digest(None, None, sections=("shows",),
                                      title_hint=SHOW)
line = next((l for l in digest.splitlines() if l.strip().startswith(f"- {SHOW}")), "")
check("the show gets a detail line", bool(line))
check("Season 02 (1 eps) is stated", "Season 02 (1 eps)" in line)
check("2/2 locked is stated", "2/2 locked" in line)

print("Part 3 -- a pre-fix cache entry cannot serve its subset")
# Simulate the old code's entry: correct key-era version, matching directory signature,
# but a summary that knew only about the local episode. A cached reading of a subset is
# not a reading of the library, so the versioned cache must refuse it.
reset_cache()
show_dir = media / "Shows" / SHOW
stale = {"A Show (2001)": {
    "sig": library._show_signature(show_dir),
    "summary": {"episodes": 1, "locked": 1, "blank": 0, "numbering": "seasoned",
                "seasons": [1], "season_gap": False, "season_counts": {"1": 1}},
}}
cache_file.write_text(json.dumps(stale), encoding="utf-8")
loaded = library._summary_cache_load()
summ, _changed = library._cached_show_summary(show_dir, loaded)
check("the legacy entry is not loaded", "A Show (2001)" not in loaded)
check("the rescan still sees both episodes", summ["episodes"] == 2)

print("Part 4 -- inventory unavailable -> the disk walk is the fallback")
reset_cache()
config.MEDIA_SYNCER_INVENTORY = tmp / "does-not-exist.json"
s = library.show_metadata_summary(media / "Shows" / SHOW)
check("the local episode is still counted", s["episodes"] == 1)
config.MEDIA_SYNCER_INVENTORY = inventory
reset_cache()

print("Part 5 -- fixture directories outside the roots keep the plain walk")
outside = tmp / "fixtures" / SHOW / "Season 01"
outside.mkdir(parents=True, exist_ok=True)
(outside / f"{SHOW} - S01E01.mkv").write_bytes(b"v" * 10)
(outside / f"{SHOW} - S01E01.nfo").write_text(nfo_text(), encoding="utf-8")
s = library.show_metadata_summary(tmp / "fixtures" / SHOW)
check("the fixture episode is counted once", s["episodes"] == 1)
check("no season-2 phantom leaked in from the live inventory",
      s["season_counts"] == {"1": 1})

print("Part 6 -- the same inventory does not bleed into another show")
reset_cache()
write_inventory([E01, E02,
                 f"Shows/Other Show (2010)/Season 05/Other Show (2010) - S05E09.mkv"])
(media / "Shows" / "Other Show (2010)").mkdir(parents=True, exist_ok=True)
s = library.show_metadata_summary(media / "Shows" / "Other Show (2010)")
check("the other show sees exactly its own one episode", s["episodes"] == 1)
check("and its own season", s["season_counts"] == {"5": 1})

(config.MEDIA_ROOT, config.MEDIAFS_MOUNT, config.SHOWS_ROOT,
 config.MEDIA_SYNCER_INVENTORY, library._SUMMARY_CACHE_FILE) = saved
library._INVENTORY_EPISODES_CACHE.clear()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
