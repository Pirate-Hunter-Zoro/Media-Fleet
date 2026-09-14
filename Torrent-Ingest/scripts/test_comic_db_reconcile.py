#!/usr/bin/env python3
"""Comic/manga kind splits in library.db, and the cleanup that must never overshoot.

`record_plan` used to file EVERY comic as kind `manga`, so each library-seeded western
series acquired a duplicate `manga` twin -- 25 live norm pairs by 2026-09-14, and the
reason the identify model split the ElfQuest re-acquisition across `Comics/ElfQuest` and
`Comics/Manga/ElfQuest`. The reaper now folds the pairs and supersedes absent numbered
items after every purge (`dbhook.reconcile_comics`); this asserts the fold follows the
POOL (the only witness that says which root a series belongs to), and that the passes
REFUSE whenever the evidence is not unambiguous -- a false supersede reads as "not
owned" and invites a re-download of content already held.

Fixtures only; the live library.db is never touched.
"""
from __future__ import annotations

import json
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


def fresh_db(name: str):
    tmp = Path(tempfile.mkdtemp(prefix="comic-reconcile-")) / name
    return librarydb.connect(str(tmp))


def status(conn, sid, mtype, number):
    row = conn.execute(
        "SELECT status FROM media WHERE series_id=? AND mtype=? AND number IS ?",
        (sid, mtype, number)).fetchone()
    return row["status"] if row else None


print("=== comic inventory from the pool ===")

inv_file = Path(tempfile.mkdtemp(prefix="comic-inv-")) / "remote_inventory.json"
inv_file.write_text(json.dumps({
    "Comics/ElfQuest/ElfQuest v01.cbr": {},
    "Comics/ElfQuest/The Final Quest/The Final Quest v01.cbr": {},
    "Comics/Manga/Some Series/Some Series c0007.cbz": {},
    "Comics/Star Wars Comics/Modern Era/Darth Vader/Darth Vader v02.cbz": {},
    "Comics/ElfQuest/cover.jpg": {},
}))
inv = dbhook.comic_inventory(inv_file)
check("a western series is classified comic", inv["elfquest"]["kinds"] == {"comic"})
check("its volume number is read", inv["elfquest"]["volumes"] >= {1})
check("a nested subseries is attributed to the parent chain too",
      "the final quest" in inv and inv["the final quest"]["kinds"] == {"comic"})
check("a manga is classified manga", inv["some series"]["kinds"] == {"manga"})
check("a chapter marker is read as a chapter", inv["some series"]["chapters"] == {7})
check("deep franchise nesting resolves to the series name",
      "darth vader" in inv and inv["darth vader"]["volumes"] == {2})
check("non-comic extensions are ignored", "ElfQuest cover" not in inv)

print()
print("=== fold: the pool decides the kind ===")

conn = fresh_db("fold.db")
# Western pair: the library seeded `comic`, the ingest created the `manga` twin that
# actually owns the volume. The pool says western.
c = librarydb.add_series(conn, "Darth Vader", "comic", source="library")
m = librarydb.add_series(conn, "Darth Vader", "manga", source="ingest")
librarydb.add_media(conn, m, "volume", None, 1, title="Darth Vader v01.cbz")
res = dbhook.fold_comic_pairs(conn, {"darth vader": {"kinds": {"comic"},
                                                     "volumes": {1}, "chapters": set()}})
check("the pair folded once", res["folded"] == 1)
check("the owned row moved to the western series",
      status(conn, c, "volume", 1) == "owned")
check("the manga twin is gone",
      conn.execute("SELECT COUNT(*) FROM series WHERE norm='darth vader'").fetchone()[0] == 1)

# Manga pair: files under Manga/, so the manga twin survives.
conn2 = fresh_db("fold-manga.db")
mc = librarydb.add_series(conn2, "Some Series", "comic", source="library")
mm = librarydb.add_series(conn2, "Some Series", "manga", source="ingest")
librarydb.add_media(conn2, mc, "volume", None, 1, title="stale western")
librarydb.add_media(conn2, mm, "volume", None, 1, title="Some Series v01.cbz")
res2 = dbhook.fold_comic_pairs(conn2, {"some series": {"kinds": {"manga"},
                                                       "volumes": {1}, "chapters": set()}})
check("a manga pair folds to manga",
      conn2.execute("SELECT kind FROM series WHERE norm='some series'").fetchone()[0] == "manga")
check("its one item is owned exactly once",
      conn2.execute("SELECT COUNT(*) FROM media WHERE mtype='volume' AND number=1 "
                    "AND status='owned'").fetchone()[0] == 1)

# Ownership must not be lost when the survivor's copy was superseded and the loser's is
# owned: the survivor's row becomes owned before the duplicate is dropped.
conn3 = fresh_db("fold-ownership.db")
c3 = librarydb.add_series(conn3, "ElfQuest", "comic", source="library")
m3 = librarydb.add_series(conn3, "ElfQuest", "manga", source="ingest")
librarydb.add_media(conn3, c3, "volume", None, 1, title="old", status="superseded")
librarydb.add_media(conn3, m3, "volume", None, 1, title="ElfQuest v01.cbr", status="owned")
dbhook.fold_comic_pairs(conn3, {"elfquest": {"kinds": {"comic"},
                                             "volumes": {1}, "chapters": set()}})
check("ownership survives the fold", status(conn3, c3, "volume", 1) == "owned")
check("and only one row remains",
      conn3.execute("SELECT COUNT(*) FROM media WHERE mtype='volume' AND number=1"
                    ).fetchone()[0] == 1)

# Refusals: no inventory, and a norm placed under BOTH roots, must fold nothing.
conn4 = fresh_db("fold-refuse.db")
librarydb.add_series(conn4, "Batman", "comic", source="library")
librarydb.add_series(conn4, "Batman", "manga", source="ingest")
librarydb.add_series(conn4, "Dual", "comic", source="library")
librarydb.add_series(conn4, "Dual", "manga", source="ingest")
res4 = dbhook.fold_comic_pairs(conn4, {"dual": {"kinds": {"comic", "manga"},
                                                "volumes": {1}, "chapters": set()}})
check("every unresolvable pair is reported as skipped",
      res4["skipped"] == 2 and res4["folded"] == 0)
check("an ambiguous pair is skipped", res4["folded"] == 0)
check("both twins survive a refusal",
      conn4.execute("SELECT COUNT(*) FROM series WHERE norm IN ('batman','dual')"
                    ).fetchone()[0] == 4)

print()
print("=== supersede absent numbered items (fail-open) ===")

conn5 = fresh_db("absent.db")
sid = librarydb.add_series(conn5, "Thing", "comic", source="ingest")
librarydb.add_media(conn5, sid, "volume", None, 1, title="v1")
librarydb.add_media(conn5, sid, "volume", None, 2, title="v2")
librarydb.add_media(conn5, sid, "volume", None, 3, title="v3")
librarydb.add_media(conn5, sid, "collection", None, None, title="Extras.cbz")
librarydb.add_series(conn5, "Unnumbered", "comic", source="ingest")
u = librarydb.get_series_id(conn5, "Unnumbered", "comic")
librarydb.add_media(conn5, u, "volume", None, 1, title="no marker in the pool")
librarydb.add_series(conn5, "Not In Pool", "comic", source="ingest")
n = librarydb.get_series_id(conn5, "Not In Pool", "comic")
librarydb.add_media(conn5, n, "volume", None, 1, title="keep me")
res5 = dbhook.supersede_absent_comics(conn5, {
    "thing": {"kinds": {"comic"}, "volumes": {1, 2}, "chapters": set()},
    "unnumbered": {"kinds": {"comic"}, "volumes": set(), "chapters": set()},
})
check("an absent numbered volume is superseded", status(conn5, sid, "volume", 3) == "superseded")
check("present volumes stay owned", status(conn5, sid, "volume", 1) == "owned"
      and status(conn5, sid, "volume", 2) == "owned")
check("a collection row is never judged", status(conn5, sid, "collection", None) == "owned")
check("a series with no numbered items is skipped", status(conn5, u, "volume", 1) == "owned")
check("a series the pool does not list is skipped", status(conn5, n, "volume", 1) == "owned")
check("only the verifiable series was judged", res5["judged_series"] == 1)

print()
print("=== end to end + plan kind ===")

conn6 = fresh_db("e2e.db")
librarydb.add_series(conn6, "ElfQuest", "comic", source="library")
librarydb.add_series(conn6, "ElfQuest", "manga", source="ingest")
m6 = librarydb.get_series_id(conn6, "ElfQuest", "manga")
librarydb.add_media(conn6, m6, "volume", None, 1, title="ElfQuest v01.cbr")
e2e_inv = Path(tempfile.mkdtemp(prefix="comic-e2e-")) / "inv.json"
e2e_inv.write_text(json.dumps({"Comics/ElfQuest/ElfQuest v01.cbr": {}}))
rec = dbhook.reconcile_comics(conn6, e2e_inv)
conn6.commit()
check("reconcile folds and reports", rec["folded"] == 1)
check("the manga twin is gone after the end-to-end run",
      conn6.execute("SELECT COUNT(*) FROM series WHERE norm='elfquest' AND kind='manga'"
                    ).fetchone()[0] == 0)

check("a comic plan under Comics/Manga/ records as manga",
      dbhook._plan_kind({"media_type": "comic",
                         "files": [{"dst_rel": "Comics/Manga/X/X v01.cbz"}]}) == "manga")
check("a comic plan under Comics/ records as comic",
      dbhook._plan_kind({"media_type": "comic",
                         "files": [{"dst_rel": "Comics/ElfQuest/ElfQuest v01.cbr"}]}) == "comic")
check("a mixed plan whose comics are western records as comic",
      dbhook._plan_kind({"media_type": "mixed",
                         "files": [{"dst_rel": "Comics/X/X v01.cbz"},
                                   {"dst_rel": "Novels/Y/Y.epub"}]}) == "comic")
check("a show plan is untouched",
      dbhook._plan_kind({"media_type": "show",
                         "files": [{"dst_rel": "Shows/X/Season 01/X - S01E01.mkv"}]}) == "anime")

# The refresh marker: comics filed -> marker; anything else -> no marker.
marker = Path(tempfile.mkdtemp(prefix="comic-marker-")) / "refresh"
saved = dbhook.config.YACREADER_REFRESH_MARKER
try:
    dbhook.config.YACREADER_REFRESH_MARKER = marker
    dbhook._request_yacreader_refresh(
        {"files": [{"dst_rel": "Comics/ElfQuest/ElfQuest v01.cbr"}]})
    check("filing a comic drops the refresh marker", marker.exists())
    marker.unlink()
    dbhook._request_yacreader_refresh(
        {"files": [{"dst_rel": "Shows/X/Season 01/X - S01E01.mkv"}]})
    check("filing a show does not", not marker.exists())
finally:
    dbhook.config.YACREADER_REFRESH_MARKER = saved

reap_src = (Path(__file__).resolve().parent.parent / "reap.py").read_text()
check("the reaper runs the comic reconcile after a purge", "dbhook.reconcile_comics()" in reap_src)
rec_src = (Path(__file__).resolve().parent / "reconcile_library_db.py").read_text()
check("the manual reconcile exposes the same pass", "dbhook.reconcile_comics(conn" in rec_src)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("comic kind reconcile: all checks passed")
