#!/usr/bin/env python3
"""The manga tiers are computed from the archives themselves (HANDOFF 10.5a-d).

THE OWNER'S FAULTS THESE PIN.
  row 1: 104 One Piece chapters already covered by owned volumes still sat on the
         shelf because the map only knew v1/v2/v10/v11 and a colored volume could
         not author a purge.
  row 2: a grey volume never yielded to the coloured copy: colour was read from the
         FILENAME (`dbhook._COLOR`), and those filenames carry none -- the archives'
         own entries do (`[Digital CC] [PZG]` vs `[VIZ Media] [1r0n]`).
  row 3: `One Piece v1176` should be `c1176`; no volume ceiling existed anywhere,
         so nothing could refuse the next "volume".

This exercises the whole computed chain, offline:
  Part 1 -- `comicfacts` reads colour, volume association and chapter markers from
            crafted archives, including the bare-number chapter pages.
  Part 2 -- the persisted ceiling is the larger of AniList's total and the shelf's.
  Part 3 -- `validate_plan` refuses `vNNNN` above the ceiling when the archive's
            entries are chapters, accepts a real volume above it, and fails open
            when no ceiling is known. It also refuses a grey copy superseding a
            coloured file.
  Part 4 -- the colour-aware DB keeps grey and coloured rows apart and a supersede
            with unknown colour takes only the grey row.
  Part 5 -- the repair tool finds exactly the mislabels and renames through the
            supersede machinery (mount write + purge + DB), never `mv`.

    python3 scripts/test_manga_mislabels.py

Fixtures only; the live library and DB are never touched. Exit 0 = all checks.
"""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import comicfacts                                                    # noqa: E402
import config                                                        # noqa: E402
import library                                                       # noqa: E402
import librarydb                                                     # noqa: E402

failures: list[str] = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


def cbz(path, names):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, b"page")
    return path


GREY = ["One Piece - c0001 (v001) - p002 [VIZ Media] [Digital] [1r0n].png",
        "One Piece - c0002 (v001) - p010 [VIZ Media] [Digital] [1r0n].png"]
COLORED = ["One Piece v001 (Colored) (Digital) (PZG)/One Piece - c0001 (v001) - p000 [Digital CC] [PZG].webp",
           "One Piece v001 (Colored) (Digital) (PZG)/One Piece - c0002 (v001) - p006 [Digital CC] [PZG].webp"]
MISLABEL = [f"1176-{n:03d}.png" for n in range(1, 16)]
CHAPTER = ["One Piece - d1077 (NA) - p000 [web] [VIZ Media] [suidana]{LQ}.jpg"]

print("Part 1 -- what the archives say")
tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name)
    g = cbz(root / "One Piece v001.cbz", GREY)
    c = cbz(root / "One Piece v01.cbz", COLORED)
    m = cbz(root / "One Piece v1176.cbz", MISLABEL)
    ch = cbz(root / "One Piece c1077.cbz", CHAPTER)
    fg, fc = comicfacts.facts(g), comicfacts.facts(c)
    fm, fch = comicfacts.facts(m), comicfacts.facts(ch)
    check("grey volume: entries name the volume and its chapters",
          fg["volume"] == 1 and fg["chapters"] == [1, 2] and fg["colored"] is False)
    check("colored volume: entries name the edition",
          fc["colored"] is True)
    check("mislabel: bare chapter pages are a chapter, not volume 1176",
          fm["volume"] is None and fm["kind"] == "chapter" and fm["chapters"] == [1176])
    check("a c/d marker alone is a chapter",
          fch["volume"] is None and fch["kind"] == "chapter" and fch["chapters"] == [1077])
    sm = comicfacts.shelf_map(str(root), persist=False)
    check("shelf map merges the editions to one volume entry",
          sm.get(1, {}).get("chapters") == [1, 2]
          and sm.get(1, {}).get("colored") is True)
    check("shelf map claims no volume 1176", 1176 not in sm)
    check("shelf ceiling is 1", comicfacts.ceiling_from_shelf(str(root)) == 1)
finally:
    tmp.cleanup()

print("Part 2 -- the persisted ceiling")
tmp = tempfile.TemporaryDirectory()
try:
    map_path = Path(tmp.name) / "manga_volume_map.json"
    map_path.write_text(json.dumps({"version": 1, "series": {
        "one piece": {"total_volumes": 108, "shelf_ceiling": 111, "volumes": {}}}}))
    saved = comicfacts.VOLUME_MAP_PATH
    comicfacts.VOLUME_MAP_PATH = map_path
    check("the larger of AniList and the shelf wins (111, not 108)",
          comicfacts.ceiling_for("One Piece") == 111)
    check("an unknown series has no ceiling",
          comicfacts.ceiling_for("Nothing Here") is None)
    comicfacts.VOLUME_MAP_PATH = saved
finally:
    tmp.cleanup()

print("Part 3 -- validate_plan refuses the mislabel, fails open otherwise")
tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name)
    src = cbz(root / "One Piece v1176.cbz", MISLABEL)
    real = cbz(root / "One Piece v112.cbz",
               ["One Piece - c1124 (v112) - p001 [VIZ Media] [Digital] [1r0n].png"])
    shelf = root / "shelf"
    cbz(shelf / "One Piece v111.cbz",
        ["One Piece - c1123 (v111) - p001 [VIZ Media] [Digital] [1r0n].png"])
    saved_comics = config.COMICS_ROOT
    config.COMICS_ROOT = root
    # config.COMICS_ROOT is <media>/Comics, so the franchise master is root/Manga/One Piece.
    cbz(root / "Manga/One Piece/One Piece v001.cbz", GREY)

    def plan_for(source, dst):
        return {"media_type": "comic", "title": "One Piece",
                "year": 1997, "files": [{"src": str(source), "dst_rel": dst}]}

    saved_map = comicfacts.VOLUME_MAP_PATH
    comicfacts.VOLUME_MAP_PATH = root / "map.json"
    comicfacts.VOLUME_MAP_PATH.write_text(json.dumps({"version": 1, "series": {
        "one piece": {"total_volumes": 111, "shelf_ceiling": 111, "volumes": {}}}}))
    rejected = None
    try:
        library.validate_plan(
            plan_for(src, "Comics/Manga/One Piece/One Piece v1176.cbz"), str(root))
    except library.PlanError as exc:
        rejected = str(exc)
    check("v1176 whose pages are chapter 1176 is refused",
          rejected and "There is no volume 1176" in rejected)
    ok = library.validate_plan(
        plan_for(real, "Comics/Manga/One Piece/One Piece v112.cbz"), str(root))
    check("a real volume above the ceiling (entries associate v112) is accepted",
          ok.get("media_type") == "comic")
    # A chapter destination is always fine.
    chapter_src = cbz(root / "One Piece c1177.cbz",
                      ["One Piece - d1177 (NA) - p000 [web].jpg"])
    acc = library.validate_plan(
        plan_for(chapter_src, "Comics/Manga/One Piece/One Piece c1177.cbz"), str(root))
    check("the same content as c1177 is accepted",
          acc.get("media_type") == "comic")
    # A chapter-only SOURCE (`Chapter 1133.zip`) names no series; the destination does,
    # and the flat master accepts it. (This exact shape was rejected live by the first
    # cut of the guard, which read only the source name.)
    chap_only = cbz(root / "Chapter 1133.cbz", ["1133-001.png"])
    ch_ok = library.validate_plan(
        plan_for(chap_only, "Comics/Manga/One Piece/One Piece c1133.cbz"), str(root))
    check("a chapter-only source files into the flat master",
          ch_ok.get("media_type") == "comic")
    # A source that NAMES another series at a franchise root is still refused.
    sw = root / "Star Wars Comics"
    cbz(sw / "Star Wars v01.cbz", ["Star Wars/001.png"])
    darth = cbz(root / "Darth Vader v01.cbz", ["Darth Vader/001.png"])
    fran = {"media_type": "comic", "title": "Darth Vader", "year": 2017,
            "files": [{"src": str(darth),
                       "dst_rel": "Comics/Star Wars Comics/Darth Vader v01.cbz"}]}
    refused = None
    try:
        library.validate_plan(fran, str(root))
    except library.PlanError as exc:
        refused = str(exc)
    check("another series at the franchise root is still refused",
          refused and "SUB-FOLDERS only" in refused)
    # Unknown ceiling -> fail open.
    comicfacts.VOLUME_MAP_PATH.write_text(json.dumps({"version": 1, "series": {}}))
    open_ok = library.validate_plan(
        plan_for(src, "Comics/Manga/One Piece/One Piece v1176.cbz"), str(root))
    check("no persisted ceiling -> no refusal (fail open)",
          open_ok.get("media_type") == "comic")
    comicfacts.VOLUME_MAP_PATH = saved_map

    # Grey superseding a coloured shelf file is refused. The grey copy is filed at its
    # own destination (a same-path filing is refused earlier as "also being written"),
    # and the coloured file it wants to replace is on the mount.
    comicfacts.VOLUME_MAP_PATH = root / "map2.json"
    comicfacts.VOLUME_MAP_PATH.write_text(json.dumps({"version": 1, "series": {}}))
    shelf = root / "mount"
    cbz(shelf / "Comics/Thing/Thing v01.cbz", COLORED)
    old_mount, old_media = config.MEDIAFS_MOUNT, config.MEDIA_ROOT
    config.MEDIAFS_MOUNT, config.MEDIA_ROOT = shelf, shelf
    try:
        grey_src = cbz(root / "grey v001.cbz", GREY)
        plan = {"media_type": "comic", "title": "Thing", "year": 2000,
                "files": [{"src": str(grey_src),
                           "dst_rel": "Comics/Thing/Thing v01 (grey).cbz"}],
                "supersedes": ["Comics/Thing/Thing v01.cbz"]}
        bad = None
        try:
            library.validate_plan(plan, str(root))
        except library.PlanError as exc:
            bad = str(exc)
        check("a grey copy may not supersede the coloured file",
              bad and "COLOURED" in bad)
        # ... but superseding it with a coloured copy is fine.
        colored_src = cbz(root / "colored v01.cbz", COLORED)
        plan2 = {"media_type": "comic", "title": "Thing", "year": 2000,
                 "files": [{"src": str(colored_src),
                            "dst_rel": "Comics/Thing/Thing v01 (alt).cbz"}],
                 "supersedes": ["Comics/Thing/Thing v01.cbz"]}
        fine = library.validate_plan(plan2, str(root))
        check("a coloured copy may supersede the coloured file",
              fine.get("media_type") == "comic")
    finally:
        config.MEDIAFS_MOUNT, config.MEDIA_ROOT = old_mount, old_media
        config.COMICS_ROOT = saved_comics
        comicfacts.VOLUME_MAP_PATH = saved_map
finally:
    tmp.cleanup()

print("Part 4 -- the DB keeps both editions, supersede is colour-precise")
tmp = tempfile.TemporaryDirectory()
try:
    conn = librarydb.connect(str(Path(tmp.name) / "lib.db"))
    sid = librarydb.add_series(conn, "One Piece", "manga")
    librarydb.upsert_media(conn, sid, "volume", None, 1, colored=False)
    librarydb.upsert_media(conn, sid, "volume", None, 1, colored=True)
    rows = conn.execute("SELECT number, colored, status FROM media WHERE series_id=? "
                        "ORDER BY colored", (sid,)).fetchall()
    check("grey and coloured v01 are two rows",
          [(r["number"], r["colored"]) for r in rows] == [(1, 0), (1, 1)])
    # A re-filing of the grey copy must not create a third row or flip the coloured one.
    librarydb.upsert_media(conn, sid, "volume", None, 1, colored=False)
    n = conn.execute("SELECT COUNT(*) FROM media WHERE series_id=?", (sid,)).fetchone()[0]
    check("re-filing the grey copy is idempotent", n == 2)
    # Unknown-colour supersede takes grey only.
    librarydb.mark_superseded(conn, sid, "volume", None, 1, 1, colored=None)
    st = dict(conn.execute("SELECT colored, status FROM media WHERE series_id=?",
                           (sid,)).fetchall())
    check("unknown colour supersedes grey only",
          st[0] == "superseded" and st[1] == "owned")
    # A known coloured supersede takes the coloured row.
    librarydb.mark_superseded(conn, sid, "volume", None, 1, 1, colored=True)
    st = dict(conn.execute("SELECT colored, status FROM media WHERE series_id=?",
                           (sid,)).fetchall())
    check("coloured colour supersedes the coloured row", st[1] == "superseded")
    conn.close()
finally:
    tmp.cleanup()

print("Part 5 -- the repair tool renames through the supersede machinery")
import repair_manga_mislabels as rmm                                 # noqa: E402
import chapter_volume_reconcile as cvr                               # noqa: E402

tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name)
    mount = root / "mount"
    media = root / "media"
    shelf = mount / "Comics/Manga/One Piece"
    cbz(shelf / "One Piece v1078.cbz", ["One Piece - d1078 (NA) - p000 [web].jpg"])
    cbz(shelf / "One Piece v1176.cbz", MISLABEL)
    cbz(shelf / "One Piece v001.cbz", GREY)
    old_mount, old_media = config.MEDIAFS_MOUNT, config.MEDIA_ROOT
    config.MEDIAFS_MOUNT, config.MEDIA_ROOT = mount, media
    saved_owned = cvr.owned_manga
    cvr.owned_manga = lambda series=None, series_dir=None: {"One Piece": {
        "Comics/Manga/One Piece/One Piece v1078.cbz": ("chapter", 1078, False),
        "Comics/Manga/One Piece/One Piece v1176.cbz": ("chapter", 1176, None),
        "Comics/Manga/One Piece/One Piece v001.cbz": ("volume", 1, False),
    }}
    try:
        found = rmm.candidates(series="One Piece")
        check("only the v-mislabels are candidates",
              sorted(f[2] for f in found) ==
              ["Comics/Manga/One Piece/c1078.cbz", "Comics/Manga/One Piece/c1176.cbz"])
        superseded, purged, planned, logged = [], [], [], []
        saved_sp, saved_rp, saved_rpl = (library.supersede_paths, __import__("dbhook").record_purge,
                                         __import__("dbhook").record_plan)
        import dbhook
        library.supersede_paths = lambda rels: superseded.extend(rels)
        dbhook.record_purge = lambda rels: purged.extend(rels)
        dbhook.record_plan = lambda plan: planned.append(plan)
        import journal
        saved_log = journal.log_decision
        journal.log_decision = lambda *a, **k: logged.append(a)
        try:
            ok = all(rmm.apply_rename(*f[:4]) for f in found)
        finally:
            library.supersede_paths = saved_sp
            dbhook.record_purge = saved_rp
            dbhook.record_plan = saved_rpl
            journal.log_decision = saved_log
        check("renames applied", ok)
        check("the old vNNNN paths were superseded",
              sorted(superseded) == ["Comics/Manga/One Piece/One Piece v1078.cbz",
                                     "Comics/Manga/One Piece/One Piece v1176.cbz"])
        check("the DB purge mirror saw the old paths", sorted(purged) == sorted(superseded))
        check("the new cNNNN files exist with bytes",
              (shelf / "c1078.cbz").stat().st_size > 0
              and (shelf / "c1176.cbz").stat().st_size > 0)
        check("the volume file was left alone", (shelf / "One Piece v001.cbz").exists())
        check("the renames are recorded for the next reader", len(logged) == 2)
    finally:
        cvr.owned_manga = saved_owned
        config.MEDIAFS_MOUNT, config.MEDIA_ROOT = old_mount, old_media
finally:
    tmp.cleanup()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
