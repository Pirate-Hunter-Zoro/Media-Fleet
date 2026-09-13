#!/usr/bin/env python3
"""A verified purge must stop library.db claiming the purged items.

    python3 scripts/test_purge_db_sync.py

WHY THIS EXISTS

    Nothing updated library.db when the reaper deleted content: `reconcile_media` was
    orphaned with the searcher on 2026-09-10, so every purge left its owned rows behind.
    By 2026-09-13 that was 212 series / 5,769 rows, each a re-drop the acceptance gate
    would refuse as "already owned" (OPERATING §8). `dbhook.record_purge` is the fix; the
    reaper calls it with the purged paths it VERIFIED gone.

WHAT IS PROVED HERE

  1. An episode path supersedes exactly that episode of that show -- not the sibling
     episode, not another series.
  2. A duplicate series (the same norm under both `anime` and `tv`, e.g. Gundam 00) has
     its row superseded in BOTH, because either one blocks the re-drop.
  3. A loose film and a foldered film both match by title/stem.
  4. A comic is matched by its folder chain (franchise nesting, flat folder, hyphenated
     name, a bare `cNNN` chapter) and, last, by its stem with the marker stripped
     (`Darth Vader v01.cbz` under a Star Wars path). A collection is superseded only
     when exactly one row could be meant; two is a refusal, not a guess.
  5. An unknown title is a clean no-op.

Uses a fixture database; the live library.db is never touched.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dbhook                                                          # noqa: E402

librarydb = dbhook.librarydb
failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


tmp = tempfile.mkdtemp(prefix="purge-db-sync-")
conn = librarydb.connect(str(Path(tmp) / "library.db"))

show = librarydb.add_series(conn, "Show (2001)", "anime", source="ingest")
librarydb.upsert_media(conn, show, "episode", 1, 1, title="e1")
librarydb.upsert_media(conn, show, "episode", 1, 2, title="e2")
librarydb.upsert_media(conn, show, "episode", 2, 1, title="s2e1")

dup_a = librarydb.add_series(conn, "Gundam 00", "anime", source="new.txt")
dup_t = librarydb.add_series(conn, "Gundam 00", "tv", source="new.txt")
librarydb.upsert_media(conn, dup_a, "episode", 1, 1, title="e1")
librarydb.upsert_media(conn, dup_t, "episode", 1, 1, title="e1")

film = librarydb.add_series(conn, "Some Film (2000)", "movie", source="library")
librarydb.upsert_media(conn, film, "movie", None, None, title="Some Film (2000).mkv")

foldered = librarydb.add_series(conn, "Another Film (1999)", "movie", source="library")
librarydb.upsert_media(conn, foldered, "movie", None, None, title="Another Film (1999).mkv")

# Comics: the franchise layout puts the series at an unpredictable depth, so matching is
# by folder chain (longest first), then the file stem. Every shape below is a real one.
fcc = librarydb.add_series(conn, "Parasyte Full Color Collection", "manga", source="ingest")
librarydb.upsert_media(conn, fcc, "volume", None, 1, title="Parasyte Full Color Collection v01.cbz")
librarydb.upsert_media(conn, fcc, "volume", None, 2, title="Parasyte Full Color Collection v02.cbz")
reversi = librarydb.add_series(conn, "Parasyte Reversi", "manga", source="ingest")
librarydb.upsert_media(conn, reversi, "volume", None, 2, title="Parasyte Reversi v02.cbz")
noragami = librarydb.add_series(conn, "Noragami - Stray God", "manga", source="ingest")
librarydb.upsert_media(conn, noragami, "volume", None, 14, title="Noragami - Stray God v14.cbz")
one_piece = librarydb.add_series(conn, "One Piece", "manga", source="new.txt")
librarydb.upsert_media(conn, one_piece, "chapter", None, 424, title="One Piece Colored c0424.cbz")
darth = librarydb.add_series(conn, "Darth Vader", "comic", source="ingest")
librarydb.upsert_media(conn, darth, "volume", None, 1, title="Darth Vader v01.cbz")
moon = librarydb.add_series(conn, "Through the Moon", "comic", source="ingest")
librarydb.upsert_media(conn, moon, "collection", None, None, title="Through the Moon.cbz")
twice = librarydb.add_series(conn, "Two Collections", "comic", source="ingest")
librarydb.add_media(conn, twice, "collection", None, None, title="a.cbz")
librarydb.add_media(conn, twice, "collection", None, None, title="b.cbz")

res = dbhook.supersede_purged(conn, {
    "Shows/Show (2001)/Season 01/Show (2001) - S01E01.mkv",
    "Shows/Gundam 00/Season 01/Gundam 00 - S01E01.mkv",
    "Movies/Some Film (2000).mkv",
    "Movies/Another Film (1999)/Another Film (1999).mkv",
    # franchise nesting -> the joined chain names the series
    "Comics/Manga/Parasyte/Full Color Collection/Parasyte Full Color Collection v01.cbz",
    # flat series folder
    "Comics/Manga/Parasyte Reversi/Parasyte Reversi v02.cbz",
    "Comics/Manga/Noragami - Stray God/Noragami - Stray God v14.cbz",
    # bare cNNN chapter marker, one tier under Manga
    "Comics/Manga/One Piece/One Piece Colored c0424.cbz",
    # western comics nest under their own category; the stem names the series
    "Comics/Star Wars Comics/Omnibuses/Rebellion/Darth Vader v01.cbz",
    # a collection with exactly one candidate row can be named; two cannot
    "Comics/Through the Moon.cbz",
    "Comics/Two Collections/whatever.cbz",
    "Shows/Never Heard Of It/Season 01/Never Heard Of It - S01E01.mkv",
})
conn.commit()


def status(sid, season, number, mtype):
    q, args = "SELECT status FROM media WHERE series_id=? AND mtype=?", [sid, mtype]
    if season is not None:
        q += " AND season=?"
        args.append(season)
    if number is not None:
        q += " AND number=?"
        args.append(number)
    row = conn.execute(q, args).fetchone()
    return row["status"] if row else None


check("the purged episode is superseded", status(show, 1, 1, "episode") == "superseded")
check("its sibling stays owned", status(show, 1, 2, "episode") == "owned")
check("another season stays owned", status(show, 2, 1, "episode") == "owned")
check("the duplicate anime row is superseded", status(dup_a, 1, 1, "episode") == "superseded")
check("the duplicate tv row is superseded too", status(dup_t, 1, 1, "episode") == "superseded")
check("a loose film is superseded", status(film, None, None, "movie") == "superseded")
check("a foldered film is superseded", status(foldered, None, None, "movie") == "superseded")

check("a franchise-nested comic is superseded", status(fcc, None, 1, "volume") == "superseded")
check("a flat manga folder is superseded", status(reversi, None, 2, "volume") == "superseded")
check("a hyphenated manga folder is superseded",
      status(noragami, None, 14, "volume") == "superseded")
check("a bare cNNN chapter is superseded",
      status(one_piece, None, 424, "chapter") == "superseded")
check("a western comic found by its stem is superseded",
      status(darth, None, 1, "volume") == "superseded")
check("a lone collection is superseded", status(moon, None, None, "collection") == "superseded")
check("one of two collections is NOT guessed at",
      conn.execute("SELECT COUNT(*) FROM media WHERE series_id=? AND status='owned'",
                   (twice,)).fetchone()[0] == 2)
check("a sibling volume of the same series stays owned",
      status(fcc, None, 2, "volume") == "owned")
check("counts: superseded rows", res["superseded"] == 11)

second = dbhook.supersede_purged(conn, {"Shows/Show (2001)/Season 01/Show (2001) - S01E01.mkv"})
check("re-running is idempotent (0 new supersedes)", second["superseded"] == 0)

src = (Path(__file__).resolve().parent.parent / "reap.py").read_text()
check("the reaper calls the sweep", "dbhook.record_purge(" in src)
check("the sweep is handed only verified-purged paths", "_sweep_library_db(purged_ok)" in src)

conn.close()
print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("purge -> library.db sync: all checks passed")
