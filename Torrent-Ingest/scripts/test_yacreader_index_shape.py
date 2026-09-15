#!/usr/bin/env python3
"""The index shapes that crash YACReader, and the freshness the reader cannot feel.

`FolderModel::createModelData` (YACReader 9.16.3) dereferences the parent it looks up
`ORDER BY parentId,name` with NO null check, so a dangling parent, a cycle, a missing
root, or a parent row that sorts after its child is a SIGSEGV inside the app -- the
`FolderModel::reload` crash of 2026-09-13. This proves the detector names each shape
(the load-order simulation, not a guess), that the repair leaves a loadable tree, and
that `unindexed_files` reports exactly the shelf files the index lacks.

Fixtures only: the live index is mutable state and cannot prove a detector can fire.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.yacreader_index_repair as repair                      # noqa: E402
import yacreader_index                                               # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


SCHEMA = """
CREATE TABLE folder (
    id INTEGER PRIMARY KEY, parentId INTEGER NOT NULL, name TEXT NOT NULL,
    path TEXT NOT NULL, finished BOOLEAN DEFAULT 0, completed BOOLEAN DEFAULT 1,
    numChildren INTEGER, firstChildHash TEXT, customImage TEXT,
    manga BOOLEAN DEFAULT 0, type INTEGER DEFAULT 0, added INTEGER, updated INTEGER,
    FOREIGN KEY(parentId) REFERENCES folder(id) ON DELETE CASCADE);
CREATE TABLE comic (
    id INTEGER PRIMARY KEY, parentId INTEGER NOT NULL, comicInfoId INTEGER NOT NULL,
    fileName TEXT NOT NULL, path TEXT,
    FOREIGN KEY(parentId) REFERENCES folder(id) ON DELETE CASCADE);
"""
TMP = Path(tempfile.mkdtemp(prefix="yac-shape-"))


def build(name: str, folders: list[tuple], comics: list[tuple] = ()) -> Path:
    p = TMP / name
    con = sqlite3.connect(str(p))
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO folder (id,parentId,name,path) VALUES (?,?,?,?)", folders)
    if comics:
        con.executemany("INSERT INTO comic (id,parentId,comicInfoId,fileName,path) "
                        "VALUES (?,?,?,?,?)", comics)
    con.commit()
    con.close()
    return p


def kinds(db: Path) -> set[str]:
    return {f["kind"] for f in yacreader_index.load_order_faults(db)}


print("=== index load-order faults ===")

healthy = build("healthy.ydb", [
    (1, 1, "root", "/"),
    (2, 1, "ElfQuest", "/ElfQuest"),
    (3, 2, "The Final Quest", "/ElfQuest/The Final Quest"),
])
check("a normal tree has no faults", yacreader_index.load_order_faults(healthy) == [])

no_root = build("no-root.ydb", [(2, 1, "ElfQuest", "/ElfQuest")])
check("a missing root row is named", "no-root" in kinds(no_root))

dangling = build("dangling.ydb", [
    (1, 1, "root", "/"),
    (5, 99, "Orphan", "/Orphan"),
])
check("a dangling parent is named as such", "dangling" in kinds(dangling))
check("...and as the load-order crash it causes", "load-order" in kinds(dangling))

cycle = build("cycle.ydb", [
    (1, 1, "root", "/"),
    (2, 3, "A", "/A"),
    (3, 2, "B", "/B"),
])
check("a cycle is named", "cycle" in kinds(cycle))

late = build("late-parent.ydb", [
    (1, 1, "root", "/"),
    (5, 7, "P", "/P"),        # parent Q id=7 sorts AFTER its child C id=10
    (7, 1, "Q", "/Q"),
    (10, 5, "C", "/P/C"),
])
check("a parent that sorts after its child is a load-order fault",
      "load-order" in kinds(late))

garbage = TMP / "garbage.ydb"
garbage.write_bytes(b"not a database at all")
check("an unreadable file is reported, not raised",
      kinds(garbage) == {"unreadable"})

print()
print("=== repair leaves a loadable tree ===")

repairable = build("repair.ydb", [
    (1, 1, "root", "/"),
    (3, 1, "Manga", "/Manga"),
    (4, 3, "Old", "/Manga/Old"),
    (5, 99, "Nested", "/Manga/Old/Nested"),   # parent by path exists and sorts first
    (6, 99, "Gone", "/Vanished/Gone"),        # no usable parent -> root
])
con = sqlite3.connect(str(repairable))
res = repair._repair(con)
con.commit()
con.close()
remaining = yacreader_index.load_order_faults(repairable)
check("repair clears every fault", remaining == [])
check("a path-parent is reattached", res["reparented"] == 1)
check("an unresolvable row is attached to root", res["to_root"] == 1)
con = sqlite3.connect(str(repairable))
check("the reattached row now points at /Manga/Old",
      con.execute("SELECT parentId FROM folder WHERE id=5").fetchone()[0] == 4)
check("the unresolvable row points at the root",
      con.execute("SELECT parentId FROM folder WHERE id=6").fetchone()[0] == 1)

rootless = build("rootless.ydb", [(2, 1, "ElfQuest", "/ElfQuest")])
con = sqlite3.connect(str(rootless))
repair._repair(con)
con.commit()
con.close()
check("a missing root is recreated", "no-root" not in kinds(rootless))
check("...and the tree loads", yacreader_index.load_order_faults(rootless) == [])

latefix = build("latefix.ydb", [
    (1, 1, "root", "/"),
    (5, 7, "P", "/P"),
    (7, 1, "Q", "/Q"),
    (10, 5, "C", "/P/C"),
])
con = sqlite3.connect(str(latefix))
repair._repair(con)
con.commit()
con.close()
check("a late-sorting parent is repaired", yacreader_index.load_order_faults(latefix) == [])

print()
print("=== freshness: what the index lacks ===")

fresh = build("fresh.ydb", [
    (1, 1, "root", "/"),
    (2, 1, "ElfQuest", "/ElfQuest"),
    (3, 1, "Manga", "/Manga"),
], comics=[(1, 2, 1, "ElfQuest v01.cbr", "/ElfQuest/ElfQuest v01.cbr")])
inv = TMP / "remote_inventory.json"
inv.write_text(json.dumps({
    "Comics/ElfQuest/ElfQuest v01.cbr": {},
    "Comics/ElfQuest/ElfQuest v02.cbr": {},
    "Comics/Manga/Some Series/Some Series v01.cbz": {},
    "Comics/ElfQuest/notes.txt": {},
}))
mount = TMP / "mount" / "Comics"
(mount / "ElfQuest").mkdir(parents=True)
(mount / "ElfQuest" / "ElfQuest v03.cbr").write_bytes(b"x")
(mount / "ElfQuest" / ".DS_Store").write_bytes(b"x")
missing = yacreader_index.unindexed_files(fresh, inv, mount)
check("indexed file is NOT reported", "ElfQuest/ElfQuest v01.cbr" not in missing)
check("pool-only file IS reported", "ElfQuest/ElfQuest v02.cbr" in missing)
check("a locally present file IS reported", "ElfQuest/ElfQuest v03.cbr" in missing)
check("a non-comic inventory key is ignored", "ElfQuest/notes.txt" not in missing)
check("dotfiles are ignored", not any(m.startswith(".") for m in missing))
check("manga paths are reported too", "Manga/Some Series/Some Series v01.cbz" in missing)
check("the report is sorted and complete", missing == sorted(missing) and len(missing) == 3)

no_inv = yacreader_index.unindexed_files(fresh, TMP / "missing-inventory.json", mount)
check("an unreadable inventory degrades to the mount, never raises",
      "ElfQuest/ElfQuest v03.cbr" in no_inv
      and "ElfQuest/ElfQuest v02.cbr" not in no_inv)

print()
print("=== freshness: NFC/NFD paths are the same file ===")

# The index stores COMPOSED paths (Qt writes them), while APFS/FUSE and the pool
# inventory hand out DECOMPOSED ones. Raw set comparison reported an already-indexed
# `Nausicaä v01.cbr` as missing forever -- which drove fleet_doctor to bounce the reader
# every 15 minutes and the supervisor to activate it every minute (2026-09-15).
nfc = "Manga/Nausicaä of the Valley of the Wind/Nausicaä of the Valley of the Wind v01.cbr"
nfd = unicodedata.normalize("NFD", nfc)
check("the fixture really is composed vs decomposed", nfc != nfd)
empty_mount = TMP / "empty-mount"

nfc_db = build("nfc-index.ydb", [
    (1, 1, "root", "/"),
    (2, 1, "Manga", "/Manga"),
], comics=[(1, 2, 1, "v01.cbr", "/" + nfc)])


def _inv(name, *paths):
    p = TMP / name
    p.write_text(json.dumps({f"Comics/{x}": {} for x in paths}))
    return p


check("NFC index vs NFD shelf is NOT reported missing",
      yacreader_index.unindexed_files(nfc_db, _inv("nfd.json", nfd), empty_mount,
                                      include_mount=False) == [])

nfd_db = build("nfd-index.ydb", [
    (1, 1, "root", "/"),
    (2, 1, "Manga", "/Manga"),
], comics=[(1, 2, 1, "v01.cbr", "/" + nfd)])
check("NFD index vs NFC shelf is NOT reported missing (both ways)",
      yacreader_index.unindexed_files(nfd_db, _inv("nfc.json", nfc), empty_mount,
                                      include_mount=False) == [])

check("a genuinely missing file is still reported",
      yacreader_index.unindexed_files(
          nfc_db, _inv("other.json", "Manga/Some Series/Some Series v01.cbz"),
          empty_mount, include_mount=False) == ["Manga/Some Series/Some Series v01.cbz"])
check("index_names normalizes to NFC",
      all(unicodedata.is_normalized("NFC", n) for n in yacreader_index.index_names(nfd_db)))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("YacReader index shape: all checks passed")
