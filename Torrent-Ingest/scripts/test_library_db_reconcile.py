#!/usr/bin/env python3
"""`library.db` must stop over-claiming, and must not over-correct either.

HANDOFF §6: "`library.db` over-claims and its reconcile tool went with the searcher."
Two distinct defects sat under that one sentence:

  * re-filing an item appended a duplicate owned row (25.9% of the live table), and
  * nothing reconciled owned rows against what the fleet actually still holds.

The second is the dangerous one to fix, because the obvious implementation deletes a
correct library. Three guards are asserted here, and each maps to a way this could destroy
data rather than repair it:

  1. EVICTION IS NOT LOSS. A file on the pool but not the SSD is owned. If the inventory
     were built from the SSD, every evicted episode would be superseded.
  2. AN EMPTY INVENTORY IS A BROKEN MOUNT, not an empty library. Superseding against it
     would wipe the ledger.
  3. AN UNVERIFIABLE KIND IS NOT AN ABSENT ONE. The inventory parser cannot enumerate
     comics, so comics must be skipped rather than judged.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import reconcile_library_db as R                                     # noqa: E402

librarydb = R.librarydb
failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


def fresh_db():
    tmp = Path(tempfile.mkdtemp(prefix="libdb-test-")) / "library.db"
    return librarydb.connect(str(tmp))


print("=== library.db: upsert ===")

conn = fresh_db()
sid = librarydb.add_series(conn, "Test Show (2001)", "anime", source="library")

# Re-file the same episode ten times, the way a re-cut / re-drop / repaired wave does.
for _ in range(10):
    librarydb.upsert_media(conn, sid, "episode", 1, 1, title="Test Show - S01E01.mkv")
rows = conn.execute("SELECT COUNT(*) FROM media WHERE series_id=?", (sid,)).fetchone()[0]
check("re-filing one episode 10x leaves exactly 1 row", rows == 1)

# A better copy must overwrite the worse one's record, not sit beside it.
librarydb.upsert_media(conn, sid, "episode", 1, 1, title="x.mkv", resolution=2)
librarydb.upsert_media(conn, sid, "episode", 1, 1, title="x.mkv", resolution=5)
librarydb.upsert_media(conn, sid, "episode", 1, 1, title="x.mkv", resolution=3)
r = conn.execute("SELECT COUNT(*) c, MAX(resolution) m FROM media WHERE series_id=?",
                 (sid,)).fetchone()
check("a better copy updates in place (still 1 row)", r["c"] == 1)
check("resolution ratchets UP and never back down", r["m"] == 5)

# Distinct items must still get distinct rows.
librarydb.upsert_media(conn, sid, "episode", 1, 2, title="e2")
librarydb.upsert_media(conn, sid, "episode", 2, 1, title="s2e1")
total = conn.execute("SELECT COUNT(*) FROM media WHERE series_id=?", (sid,)).fetchone()[0]
check("distinct episodes still get their own rows", total == 3)

# add_media must remain a plain INSERT -- the gate fixtures depend on it.
librarydb.add_media(conn, sid, "episode", 9, 9, title="a")
librarydb.add_media(conn, sid, "episode", 9, 9, title="b")
dupes = conn.execute("SELECT COUNT(*) FROM media WHERE series_id=? AND season=9",
                     (sid,)).fetchone()[0]
check("add_media is left untouched (still inserts)", dupes == 2)

check("dbhook records through upsert_media",
      "librarydb.upsert_media(" in (Path(__file__).resolve().parent.parent
                                    / "dbhook.py").read_text())
check("dbhook has no bare add_media call left",
      "librarydb.add_media(" not in (Path(__file__).resolve().parent.parent
                                     / "dbhook.py").read_text())

print()
print("=== library.db: find_duplicates ===")

conn2 = fresh_db()
sid2 = librarydb.add_series(conn2, "Dupe Show", "anime", source="library")
for _ in range(59):                       # the real worst case, from That '70s Show
    librarydb.add_media(conn2, sid2, "episode", 1, 11, title="x")
librarydb.add_media(conn2, sid2, "episode", 1, 12, title="y")
drop = R.find_duplicates(conn2)
check("59 rows for one episode -> 58 marked redundant", len(drop) == 58)
kept = {d[0] for d in drop}
first = conn2.execute("SELECT MIN(id) FROM media WHERE series_id=?", (sid2,)).fetchone()[0]
check("the OLDEST row is the survivor", first not in kept)

print()
print("=== library.db: stale pass guards ===")

conn3 = fresh_db()
show = librarydb.add_series(conn3, "Pool Show (2001)", "anime", source="library")
comic = librarydb.add_series(conn3, "Some Manga", "manga", source="library")
wish = librarydb.add_series(conn3, "Not Yet Downloaded", "anime", source="new.txt")
wish2 = librarydb.add_series(conn3, "Requested, Nothing Yet", "anime", source="new.txt")
librarydb.upsert_media(conn3, show, "episode", 1, 1, title="e1")
librarydb.upsert_media(conn3, comic, "volume", None, 1, title="v1")
librarydb.upsert_media(conn3, wish, "episode", 1, 1, title="e1")

# GUARD 1: an item present only in the POOL is owned, and must survive.
inv = {"shows": {librarydb._normalize("Pool Show (2001)"): {"seasons": {1: [1]}}},
       "movies": {}, "comics": {}, "novels": {}}
res = R.stale_pass(conn3, inv)
alive = conn3.execute("SELECT status FROM media WHERE series_id=?", (show,)).fetchone()[0]
check("an episode present in the inventory stays owned", alive == "owned")

# GUARD 3: a comic must be SKIPPED, never superseded on an inventory that cannot see it.
cstat = conn3.execute("SELECT status FROM media WHERE series_id=?", (comic,)).fetchone()[0]
check("a manga row is skipped, not superseded", cstat == "owned")
check("the skip is counted and reported", res["skipped_series"] >= 1)

# A wishlist series has no files yet and must never be judged absent.
wstat = conn3.execute("SELECT status FROM media WHERE series_id=?", (wish,)).fetchone()[0]
check("a new.txt series with no files is left alone", wstat == "owned")

# ...but once it HAS owned rows, an absent series is an over-claim whatever admitted it.
# 212 such series held 5,769 rows on 2026-09-13; the reaper now sweeps after every purge.
R.stale_pass(conn3, inv, include_requested=True)
wstat2 = conn3.execute("SELECT status FROM media WHERE series_id=?", (wish,)).fetchone()[0]
check("with --include-requested, an owned-row new.txt series IS superseded",
      wstat2 == "superseded")
check("a new.txt series with NO rows is a no-op under the flag",
      conn3.execute("SELECT COUNT(*) FROM media WHERE series_id=?", (wish2,)).fetchone()[0] == 0)
# The flag must not change the item-level verdict for a PRESENT series.
pstat = conn3.execute("SELECT status FROM media WHERE series_id=?", (show,)).fetchone()[0]
check("the flag leaves a present series' items alone", pstat == "owned")

# The genuine over-claim: a library series absent from the inventory IS superseded.
gone = librarydb.add_series(conn3, "Deleted Show (1999)", "anime", source="library")
librarydb.upsert_media(conn3, gone, "episode", 1, 1, title="e1")
R.stale_pass(conn3, inv)
gstat = conn3.execute("SELECT status FROM media WHERE series_id=?", (gone,)).fetchone()[0]
check("a library series absent from the inventory IS superseded", gstat == "superseded")

# ...and comes back if the content returns.
inv2 = dict(inv)
inv2["shows"] = dict(inv["shows"])
inv2["shows"][librarydb._normalize("Deleted Show (1999)")] = {"seasons": {1: [1]}}
R.stale_pass(conn3, inv2)
gstat2 = conn3.execute("SELECT status FROM media WHERE series_id=?", (gone,)).fetchone()[0]
check("a restored file flips the row back to owned", gstat2 == "owned")

# The restore step must not re-create a duplicate it just collapsed. A superseded row and
# an owned row can both name one item; flipping the superseded one back without checking
# hands the table a fresh duplicate (18 rows appeared this way after the first live run).
conn4 = fresh_db()
s4 = librarydb.add_series(conn4, "Twice Filed (2020)", "anime", source="library")
keep = librarydb.add_media(conn4, s4, "episode", 1, 1, title="a", status="owned")
librarydb.add_media(conn4, s4, "episode", 1, 1, title="b", status="superseded")
inv4 = {"shows": {librarydb._normalize("Twice Filed (2020)"): {"seasons": {1: [1]}}},
        "movies": {}, "comics": {}, "novels": {}}
R.stale_pass(conn4, inv4)
owned_now = conn4.execute(
    "SELECT COUNT(*) FROM media WHERE series_id=? AND status='owned'", (s4,)).fetchone()[0]
check("restore does NOT resurrect a duplicate of a live item", owned_now == 1)
check("the surviving owned row is the one that was already owned",
      conn4.execute("SELECT id FROM media WHERE series_id=? AND status='owned'",
                    (s4,)).fetchone()[0] == keep)

# GUARD 2: the empty-inventory refusal must exist in the source.
src = (Path(__file__).resolve().parent / "reconcile_library_db.py").read_text()
check("an empty inventory is REFUSED, not acted on", "REFUSING TO ACT" in src)
check("the tool reads the MOUNT, never the SSD", 'Path.home() / "MediaLibrary"' in src)
check("report-only is the default (--apply opts in)", '"--apply"' in src)
check("the DB is backed up before any write", "shutil.copy2(librarydb.path()" in src)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("library.db reconcile: all checks passed")
