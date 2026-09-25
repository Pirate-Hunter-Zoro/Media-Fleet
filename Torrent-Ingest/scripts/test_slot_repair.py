#!/usr/bin/env python3
"""The computed slot repairs: American Dad's remap and Doctor Who's specials scheme.

HANDOFF 15.1 and 15.5. Two library files sit at the wrong slot; both were filed by the
fleet. The repair path must COMPUTE the true slot -- from TMDB for a numbered episode,
from the library's own locked Season-00 scheme for a special -- and re-file through
`refile_season`-class machinery, then rewrite the sidecar to match the destination.

  * American Dad! (2005) `S04E06 - Independent Movie`: the release (and TMDB) say S10E06.
  * Doctor Who (2005) `S00E04 The End Of Time Part 1`: the shelf is era-ordered and the
    file belongs at S00E23, after The Waters Of Mars; its sidecar's `<episode>16</episode>`
    is TMDB's number. Its S00E04 shelf-mate *The Return Of Doctor Mysterio* is CORRECT
    there and only its sidecar (`<episode>149</episode>`) needs correcting.

Also pins the media_doctor guard (`_slot_disagreement`) and replays the live doctor
worklist when it is present.

    python3 scripts/test_slot_repair.py

Fixtures only, providers stubbed. Exit 0 = all checks passed.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import library                                                       # noqa: E402
import tmdbguide                                                     # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "repair_slots", str(Path(__file__).resolve().parent / "repair_slots.py"))
repair_slots = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair_slots)

_msg = importlib.util.spec_from_file_location(
    "media_doctor", str(Path(__file__).resolve().parent / "media_doctor.py"))
media_doctor = importlib.util.module_from_spec(_msg)
_msg.loader.exec_module(media_doctor)

failures: list[str] = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


def _write_fixture(root, show, season, name, title, plot, nfo_season=None,
                   nfo_episode=None):
    d = root / "Shows" / show / f"Season {season:02d}"
    d.mkdir(parents=True, exist_ok=True)
    video = d / name
    video.write_bytes(name.encode())
    if title is not None:
        video.with_suffix(".nfo").write_text(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            "<episodedetails>\n"
            f"  <title>{title}</title>\n"
            f"  <season>{season if nfo_season is None else nfo_season}</season>\n"
            f"  <episode>{season if nfo_episode is None else nfo_episode}</episode>\n"
            "  <lockdata>true</lockdata>\n"
            f"  <plot>{plot}</plot>\n"
            "</episodedetails>\n", encoding="utf-8")
    return video


print("Part 1 -- the specials scheme reads the FILENAME slot, never the nfo number")
tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name)
    saved = (config.MEDIAFS_MOUNT, config.MEDIA_ROOT, config.SHOWS_ROOT,
             library._SPECIALS_SCHEME_FILE)
    config.MEDIAFS_MOUNT, config.MEDIA_ROOT = root, root
    config.SHOWS_ROOT = root / "Shows"
    library._SPECIALS_SCHEME_FILE = root / "specials_schemes.json"

    show = "Doctor Who (2005)"
    _write_fixture(root, show, 0, "Doctor Who (2005) - S00E04 The Return Of Doctor "
                   "Mysterio.mp4", "The Return Of Doctor Mysterio", "A special.",
                   nfo_episode=149)
    scheme = library.specials_scheme(show)
    check("one special found", len(scheme) == 1)
    check("the slot is the filename's", scheme and scheme[0]["slot"] == 4)
    check("the title is the sidecar's",
          scheme and scheme[0]["title"] == "The Return Of Doctor Mysterio")
    persisted = json.loads(library._SPECIALS_SCHEME_FILE.read_text(encoding="utf-8"))
    check("the scheme is persisted for the fleet",
          show in persisted and persisted[show]["scheme"][0]["slot"] == 4)

    print("Part 2 -- American Dad's numbered remap (15.1)")
    ad = "American Dad! (2005)"
    bad = _write_fixture(root, ad, 4, "American Dad! (2005) - S04E06 - Independent "
                          "Movie [WEBDL-1080p][EAC3 5.1][h265]-playWEB.mkv",
                          "Independent Movie", "The movie.", nfo_episode=6)
    good = _write_fixture(root, ad, 10, "American Dad! (2005) - S10E07 - A Jones for a "
                           "Smith.mkv", "A Jones for a Smith", "An episode.",
                           nfo_episode=7)
    guide = [
        {"season": 10, "number": 6, "name": "Independent Movie"},
        {"season": 4, "number": 6, "name": "The 42-Year-Old Virgin"},
        {"season": 10, "number": 7, "name": "A Jones for a Smith"},
        {"season": 10, "number": 8, "name": "The People vs. Martin Sugar"},
    ]
    old_eps = tmdbguide.episode_names
    tmdbguide.episode_names = lambda _tid: guide
    try:
        repairs = repair_slots.compute_numbered_repairs(root / "Shows" / ad, 1433,
                                                        [bad, good])
        check("exactly one repair computed", len(repairs) == 1)
        r = repairs[0] if repairs else {}
        check("the wrong-slot file moves a season up",
              r.get("old", "").endswith("Season 04/American Dad! (2005) - S04E06 - "
                                        "Independent Movie [WEBDL-1080p][EAC3 5.1]"
                                        "[h265]-playWEB.mkv")
              and "Season 10" in r.get("new", "")
              and "S10E06" in r.get("new", ""))
        check("the tag tail is preserved", "playWEB" in r.get("new", ""))
        check("the correct file is untouched", len(repairs) == 1)
        # An unknown title proves nothing and must not move.
        mystery = _write_fixture(root, ad, 10, "American Dad! (2005) - S10E09 - "
                                 "Unknown Episode.mkv", "Unknown Episode", "x")
        check("an unmatched title is left alone",
              repair_slots.compute_numbered_repairs(root / "Shows" / ad, 1433,
                                                    [mystery]) == [])
        # A named file whose sidecar is MISSING gets one authored from TMDB; a file
        # whose sidecar already agrees gets none.
        no_nfo = root / "Shows" / ad / "Season 10" / \
            "American Dad! (2005) - S10E08 - The People vs. Martin Sugar.mkv"
        no_nfo.write_bytes(b"x")
        fixes = repair_slots.compute_numbered_nfo_fixes(root / "Shows" / ad, 1433,
                                                        [no_nfo, good])
        check("a missing sidecar is queued for a TMDB-authored nfo",
              len(fixes) == 1 and fixes[0]["episode_title"] == "The People vs. "
              "Martin Sugar")
        check("an agreeing sidecar is left alone",
              repair_slots.compute_numbered_nfo_fixes(root / "Shows" / ad, 1433,
                                                      [good]) == [])
    finally:
        tmdbguide.episode_names = old_eps

    print("Part 3 -- Doctor Who's specials scheme (15.5)")
    dw = root / "Shows" / show
    shutil.rmtree(dw)
    air = {
        "The Day Of The Doctor": ("2013-11-23", 1),
        "The Time Of The Doctor": ("2013-12-25", 2),
        "The Husbands Of River Song": ("2015-12-25", 3),
        "The Return Of Doctor Mysterio": ("2016-12-25", 149),
        "Twice Upon A Time": ("2017-12-25", 5),
        "The Christmas Invasion": ("2005-12-25", 17),
        "Planet Of The Dead": ("2009-04-11", 21),
        "The Waters Of Mars": ("2009-11-15", 22),
        "The End Of Time Part 1": ("2009-12-25", 16),
    }
    for title, (date, num) in air.items():
        slot = 4 if title in ("The Return Of Doctor Mysterio", "The End Of Time Part 1") \
            else {"The Christmas Invasion": 17, "Planet Of The Dead": 21,
                  "The Waters Of Mars": 22}.get(title, num)
        nfo_ep = 149 if title == "The Return Of Doctor Mysterio" else (
            16 if title == "The End Of Time Part 1" else slot)
        _write_fixture(root, show, 0, f"Doctor Who (2005) - S00E{slot:02d} {title}.mp4",
                       title, "A special.", nfo_episode=nfo_ep)
    index = [{"number": num, "name": title, "air_date": date, "overview": ""}
             for title, (date, num) in air.items()]
    import identify
    old_specials = tmdbguide.specials_index
    tmdbguide.specials_index = lambda _tid: index
    try:
        repairs, fixes = repair_slots.compute_specials_repairs(dw, 57243)
        moves = {Path(r["old"]).name: r for r in repairs}
        check("End Of Time moves to the next free era slot",
              any("End Of Time Part 1" in k and "S00E23" in v["new"] and v["episode"] == 23
                  for k, v in moves.items()))
        check("Return Of Doctor Mysterio does NOT move",
              not any("Mysterio" in k for k in moves))
        check("Return's sidecar is queued for the slot rewrite",
              any("Mysterio" in Path(f["rel"]).name and f["episode"] == 4
                  for f in fixes))

        print("Part 4 -- the doctor worklist replay")
        wl_path = Path(config.STATE_DIR) / "doctor_worklist.json"
        replayed = 0
        if wl_path.exists():
            try:
                wl = json.loads(wl_path.read_text(encoding="utf-8"))
            except ValueError:
                wl = []
            for show_entry in wl:
                if show_entry.get("show") != show:
                    continue
                for prob in show_entry.get("problems") or []:
                    names = [Path(p).name for p in prob.get("files") or []]
                    live = [n for n in names if (dw / "Season 00" / n).exists()]
                    if not live:
                        continue
                    replayed += 1
                    resolved = {Path(r["old"]).name for r in repairs} | {
                        Path(f["rel"]).name for f in fixes}
                    check("the worklist's misfiled files are computed by the tool",
                          all(n in resolved for n in live))
        print(f"  worklist items replayed: {replayed}")

        # Simulate the move and the nfo rewrite; the shelf must have one file per slot
        # with sidecars that agree.
        r = [x for x in repairs if "End Of Time" in x["old"]][0]
        old_p = root / r["old"]
        new_p = root / r["new"]
        new_p.parent.mkdir(parents=True, exist_ok=True)
        old_p.rename(new_p)
        for side in old_p.parent.glob(old_p.stem + "*"):
            if side != old_p:
                side.unlink()
        written = repair_slots._write_dest_nfos(dw, repairs)
        check("the destination nfo is rewritten", written == 1)
        tags = library._read_text(new_p.with_suffix(".nfo")) or ""
        check("the nfo now carries the destination slot",
              "<episode>23</episode>" in tags and "<season>0</season>" in tags
              and "<lockdata>true</lockdata>" in tags)
        slots = []
        for p in (dw / "Season 00").glob("*.mp4"):
            slots.append(int(__import__("re").search(r"S00E(\d+)", p.name).group(1)))
        check("the shelf holds one file per slot", len(slots) == len(set(slots)))
        md = media_doctor
        video = dw / "Season 00" / "Doctor Who (2005) - S00E04 The Return Of Doctor " \
                                  "Mysterio.mp4"
        span = md._parse_span(video.name)
        check("the doctor guard sees the foreign nfo number",
              md._slot_disagreement(video, span) == (0, 149))

        # A collision whose order is ambiguous (BOTH files fit their neighbours) is
        # left untouched for review, never guessed.
        other = "Slot Fixture (2005)"
        for slot, title in ((1, "Alpha Special"), (3, "Gamma Special"),
                            (2, "Beta One"), (2, "Beta Two")):
            _write_fixture(root, other, 0, f"{other} - S00E{slot:02d} {title}.mp4",
                           title, "x", nfo_episode=slot)
        index2 = index + [
            {"number": 101, "name": "Alpha Special", "air_date": "2010-01-01",
             "overview": ""},
            {"number": 102, "name": "Beta One", "air_date": "2011-01-01",
             "overview": ""},
            {"number": 103, "name": "Beta Two", "air_date": "2011-06-01",
             "overview": ""},
            {"number": 104, "name": "Gamma Special", "air_date": "2012-01-01",
             "overview": ""},
        ]
        tmdbguide.specials_index = lambda _tid: index2
        repairs2, fixes2 = repair_slots.compute_specials_repairs(
            root / "Shows" / other, 999, None)
        check("an ambiguous collision refuses rather than guessing",
              repairs2 == [] and fixes2 == [])
    finally:
        tmdbguide.specials_index = old_specials

    config.MEDIAFS_MOUNT, config.MEDIA_ROOT, config.SHOWS_ROOT = saved[:3]
    library._SPECIALS_SCHEME_FILE = saved[3]
finally:
    tmp.cleanup()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
