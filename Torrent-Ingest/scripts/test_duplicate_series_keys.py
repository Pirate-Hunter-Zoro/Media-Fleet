#!/usr/bin/env python3
"""Prove `db_guardian.DUPLICATE_SERIES_KEY_SQL` can report a collision AND stay quiet without one.

§ diagnosis 7: "A FILTER THAT CAN NEVER BE TRUE READS EXACTLY LIKE A CLEAN RESULT." The
duplicate-provider-id check exists because Dr. STONE served 146 episodes for 96 files and
nothing said so for weeks; a version of it that structurally could not match would read the
same as the healthy library it is meant to watch. So both directions are asserted here,
against a FIXTURE database -- never the live one, whose contents are mutable state (§4.114).
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db_guardian                                                  # noqa: E402

# The columns the SQL touches, in Jellyfin's shape.
SCHEMA = """
CREATE TABLE BaseItems (
    Id TEXT PRIMARY KEY,
    Type TEXT,
    Name TEXT,
    PresentationUniqueKey TEXT
);
"""

ROWS = [
    # Two DIFFERENT series stamped with ONE provider key -- the real 2026-08-04 incident.
    ("1", "MediaBrowser.Controller.Entities.TV.Series", "Dr. STONE", "355774-en-hash"),
    ("2", "MediaBrowser.Controller.Entities.TV.Series",
     "The Rising of the Shield Hero", "355774-en-hash"),
    # A clean pair: distinct series, distinct keys. Must NOT be reported.
    ("3", "MediaBrowser.Controller.Entities.TV.Series", "One Piece", "81797-en-hash"),
    ("4", "MediaBrowser.Controller.Entities.TV.Series", "Dr. STONE: New World", "999-en-hash"),
    # Episodes of the colliding series share the key too, and are NOT series rows: the
    # check must not count them, or every healthy show would look like a collision.
    ("5", "MediaBrowser.Controller.Entities.TV.Episode", "Ep 1", "355774-en-hash"),
    ("6", "MediaBrowser.Controller.Entities.TV.Episode", "Ep 2", "355774-en-hash"),
    # A series with no key at all must not group with the other keyless rows.
    ("7", "MediaBrowser.Controller.Entities.TV.Series", "Unidentified Show", None),
    ("8", "MediaBrowser.Controller.Entities.TV.Series", "Another Unidentified", None),
]


def _fixture(rows):
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO BaseItems VALUES (?,?,?,?)", rows)
    return con


def main() -> int:
    failures = []

    # Direction 1: it FINDS the planted collision, and only that one.
    con = _fixture(ROWS)
    found = con.execute(db_guardian.DUPLICATE_SERIES_KEY_SQL).fetchall()
    con.close()
    if len(found) != 1:
        failures.append(f"expected exactly 1 colliding key, got {len(found)}: {found}")
    else:
        key, n, names = found[0]
        if key != "355774-en-hash" or n != 2:
            failures.append(f"wrong collision reported: {found[0]}")
        if "Dr. STONE" not in names or "Shield Hero" not in names:
            failures.append(f"collision names do not name both shows: {names!r}")
        print(f"  ok  found the planted collision: {n} series share {key!r} -> {names}")

    # Direction 2: a library with no collision reports NOTHING.
    clean = [r for r in ROWS if r[3] != "355774-en-hash"]
    con = _fixture(clean)
    found = con.execute(db_guardian.DUPLICATE_SERIES_KEY_SQL).fetchall()
    con.close()
    if found:
        failures.append(f"clean library reported a collision: {found}")
    else:
        print("  ok  a clean library reports 0 collisions "
              "(keyless series and shared-key EPISODES do not false-positive)")

    if failures:
        for f in failures:
            print(f"  FAIL {f}")
        return 1
    print("duplicate-series-key check: OK (both directions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
