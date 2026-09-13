#!/usr/bin/env python3
"""Prove the YacReader backup census can report every state it claims to detect.

§ diagnosis 7: "WHEN A CHECK REPORTS NOTHING, PROVE IT CAN REPORT SOMETHING." The census
in `yacreader_index_health.py` has one branch that has NEVER fired in production -- today
6 of 7 backups are clean, so `NO CLEAN BACKUP EXISTS` has never once been printed. That is
exactly the branch a corruption depends on, and exactly the branch most likely to be
quietly broken: every recovery this fleet has performed was a `cp -p` from a backup that
happened to be clean, and with none the next corruption is permanent.

The live index cannot test this. Its contents are mutable state, and a census that could
never say NO_BACKUP would read identically to the healthy library it is watching (§4.114).
So every verdict is asserted against databases this test builds and damages itself.

BOTH DIRECTIONS, for each thing the census claims (§4.174): a clean database is called
CLEAN *and* a corrupt one is called MALFORMED; a clean backup present yields OK *and* its
absence yields NO_BACKUP; a damaged index yields DAMAGED *and* outranks a missing backup.
"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.yacreader_index_health as health                     # noqa: E402

# YacReader's tables, reduced to what `verdict()` counts.
SCHEMA = """
CREATE TABLE folder (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE comic (id INTEGER PRIMARY KEY, parentId INTEGER, fileName TEXT);
CREATE TABLE comic_info (id INTEGER PRIMARY KEY, title TEXT);
"""


def _clean_db(path: Path) -> Path:
    path.unlink(missing_ok=True)          # rebuild in place: direction 5 damages a live db
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO folder VALUES (?,?)", [(i, f"f{i}") for i in range(5)])
    con.executemany("INSERT INTO comic VALUES (?,?,?)",
                    [(i, 1, f"c{i}.cbz") for i in range(20)])
    con.executemany("INSERT INTO comic_info VALUES (?,?)",
                    [(i, f"t{i}") for i in range(20)])
    con.commit()
    con.close()
    return path


def _corrupt_db(path: Path) -> Path:
    """A real partially-corrupt SQLite file, not a truncated one.

    Truncation is the easy case and NOT the one that hurt: the 2026-09-05 damage left
    `folder` answering `191` while the file was structurally broken, so row counts read
    healthy and only `integrity_check` caught it. This reproduces that shape by scribbling
    over a B-tree page in the middle of the file, leaving page 1 and the schema intact.
    """
    _clean_db(path)
    data = bytearray(path.read_bytes())
    page = 4096
    if len(data) < page * 3:                      # make sure there IS a later page to hurt
        con = sqlite3.connect(path)
        con.executemany("INSERT INTO comic VALUES (?,?,?)",
                        [(i, 1, "x" * 200) for i in range(1000, 2000)])
        con.commit()
        con.close()
        data = bytearray(path.read_bytes())
    start = page * 2
    data[start:start + page] = b"\xde\xad\xbe\xef" * (page // 4)
    path.write_bytes(bytes(data))
    return path


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ---- direction 1: verdict() tells a clean database from a corrupt one ----------
        good, bad = tmp / "good.db", tmp / "bad.db"
        _clean_db(good)
        _corrupt_db(bad)
        gv, _gc = health.verdict(good)
        bv, _bc = health.verdict(bad)
        if gv != "ok":
            failures.append(f"a clean database was not called ok: {gv!r}")
        if bv == "ok":
            failures.append("a CORRUPT database passed integrity_check -- the fixture no "
                            "longer corrupts anything, so every other assertion here is "
                            "vacuous")

        # ---- direction 2: a clean backup present => OK --------------------------------
        live = tmp / "library.ydb"
        _clean_db(live)
        shutil.copy2(good, tmp / "library.ydb.bak-clean-1")
        c = health.census(live)
        if health.status_of(c) != health.OK:
            failures.append(f"clean index + clean backup should be OK, got "
                            f"{health.status_of(c)}")
        if c["newest_clean"] is None:
            failures.append("a clean backup was present but newest_clean is None")

        # ---- direction 3: ONLY corrupt backups => NO_BACKUP (the branch that never fired)
        (tmp / "library.ydb.bak-clean-1").unlink()
        shutil.copy2(bad, tmp / "library.ydb.bak-rot-1")
        shutil.copy2(bad, tmp / "library.ydb.bak-rot-2")
        c = health.census(live)
        if health.status_of(c) != health.NO_BACKUP:
            failures.append(f"clean index + only-corrupt backups should be NO_BACKUP, got "
                            f"{health.status_of(c)}")
        if c["newest_clean"] is not None:
            failures.append(f"no backup passes integrity_check, but newest_clean is "
                            f"{c['newest_clean']}")
        if len(c["backups"]) != 2:
            failures.append(f"expected 2 backups in the census, got {len(c['backups'])}")

        # ---- direction 4: no backups AT ALL is also NO_BACKUP, not a crash -------------
        for p in tmp.glob("library.ydb.bak-*"):
            p.unlink()
        if health.status_of(health.census(live)) != health.NO_BACKUP:
            failures.append("an index with zero backups should be NO_BACKUP")

        # ---- direction 5: a damaged index is DAMAGED, and outranks a missing backup ----
        _corrupt_db(live)
        c = health.census(live)
        if health.status_of(c) != health.DAMAGED:
            failures.append(f"a corrupt index should be DAMAGED, got {health.status_of(c)}")
        if c["healthy"]:
            failures.append("a corrupt index reported healthy=True")
        # ...and still DAMAGED, not OK, once a clean backup exists beside it.
        shutil.copy2(good, tmp / "library.ydb.bak-clean-2")
        c = health.census(live)
        if health.status_of(c) != health.DAMAGED:
            failures.append("a corrupt index with a clean backup should still be DAMAGED")
        if c["newest_clean"] is None:
            failures.append("the clean backup beside a damaged index was not found -- this "
                            "is the value the restore command is built from")

    for f in failures:
        print(f"FAIL: {f}")
    print("yacreader backup census: "
          + ("every verdict reachable in both directions."
             if not failures else f"{len(failures)} assertion(s) FAILED."))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
