#!/usr/bin/env python3
"""Is YacReader's index intact, and is there a backup that could actually restore it?

WHY THIS IS A CHECK AND NOT A RUNBOOK STEP
    YacReader's index corrupts in a way that is PARTIAL and therefore quiet. On
    2026-09-05 the `folder` table answered `191` perfectly while `comic_info` rows were
    missing from their own autoindex; on 2026-09-04 `folder` answered while every `comic`
    query failed outright. A tool that happens to read the healthy table first reports a
    healthy library. Nothing in the fleet ran `PRAGMA integrity_check`, so the only
    detector was the owner noticing black-X covers -- which is how two separate
    corruptions went unnoticed for four weeks (§ diagnosis 4.185).

    The second half matters as much as the first. `--apply` runs of the shelf audit have
    historically snapshotted the index on the way INTO a repair, so a corrupt index
    produced a corrupt backup, and "restore the newest backup" restored the damage. This
    prints every backup with its OWN verdict, so the newest CLEAN one is a fact on the
    screen rather than a guess.

    Read-only. It opens each database `mode=ro` and never writes, so it is safe to run
    with the app up -- though a verdict taken while the app is mid-write is a snapshot of
    a moving target, which the output says out loud.

    python3 scripts/yacreader_index_health.py
    python3 scripts/yacreader_index_health.py --quiet   # exit status only, for verify_fleet
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
import yacreader_db


def verdict(path: Path) -> tuple[str, dict[str, int | None]]:
    """('ok' | the first integrity complaint | an error, {table: row count or None})."""
    counts: dict[str, int | None] = {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return f"unopenable: {exc}", counts
    try:
        try:
            integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            integ = f"malformed: {exc}"
        # Count every table separately: the damage is routinely confined to one of them,
        # and a single failing count is the shape of the fault.
        for table in ("folder", "comic", "comic_info"):
            try:
                counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.DatabaseError:
                counts[table] = None
    finally:
        con.close()
    return integ, counts


def _fmt(counts: dict[str, int | None]) -> str:
    return " ".join(f"{t}={'ERR' if counts.get(t) is None else counts[t]}"
                    for t in ("folder", "comic", "comic_info"))


# Exit statuses. NO_BACKUP is separated from DAMAGED because the two need opposite
# responses: a damaged index with a clean backup is a five-second `cp -p`, while a healthy
# index with no clean backup is not yet a problem at all -- it is the state in which the
# NEXT corruption becomes permanent. Collapsing them into "1" would have made the second
# one read as an emergency and get dismissed as a false alarm.
OK, DAMAGED, NO_BACKUP = 0, 1, 2

BACKUP_GLOB = "library.ydb.bak-*"


def census(db: Path, backup_glob: str = BACKUP_GLOB) -> dict:
    """Every verdict this tool reports, as data, for an arbitrary index path.

    Split out of `main` so the fixtures in `test_yacreader_backup_census.py` can drive it
    against databases they built and damaged themselves. The live index is mutable state
    and cannot tell a broken census from a healthy library (§4.114), and the NO_BACKUP
    branch in particular has never fired in production -- 6 of 7 backups are clean today --
    so a fixture is the only way to know it can (§ diagnosis 7).
    """
    live, live_counts = verdict(db)
    backups = []
    newest_clean = None
    for b in sorted(db.parent.glob(backup_glob), key=lambda p: p.stat().st_mtime,
                    reverse=True):
        v, c = verdict(b)
        if v == "ok" and newest_clean is None:
            newest_clean = b
        backups.append((b, v, c))
    return {"db": db, "live": live, "live_counts": live_counts, "healthy": live == "ok",
            "backups": backups, "newest_clean": newest_clean}


def status_of(c: dict) -> int:
    """OK / DAMAGED / NO_BACKUP for a census. A damaged index outranks a missing backup:
    it is the more urgent fact, and the census prints both either way."""
    if not c["healthy"]:
        return DAMAGED
    return OK if c["newest_clean"] is not None else NO_BACKUP


def main() -> int:
    ap = argparse.ArgumentParser(description="YacReader index integrity + backup census.")
    ap.add_argument("--quiet", action="store_true", help="print nothing unless something is wrong")
    args = ap.parse_args()

    db = config.YACREADER_DB
    out: list[str] = []
    if not db.exists():
        print(f"no YacReader index at {db}")
        return 1

    c = census(db)
    live, live_counts, healthy = c["live"], c["live_counts"], c["healthy"]
    out.append(f"index:   {db}")
    out.append(f"         integrity={live.splitlines()[0] if live else '?'}  {_fmt(live_counts)}")
    if yacreader_db.app_running():
        out.append("         NOTE: YACReaderLibrary is running, so this is a snapshot of a "
                   "moving target.")
    lock = yacreader_db.holder() if yacreader_db.is_held() else ""
    out.append(f"lock:    {'HELD by ' + (lock or '?') if lock or yacreader_db.is_held() else 'free'}")

    newest_clean = c["newest_clean"]
    out.append(f"backups: {len(c['backups'])}")
    for b, v, counts in c["backups"]:
        out.append(f"   [{'CLEAN' if v == 'ok' else 'MALFORMED':9}] {b.name}  {_fmt(counts)}")
    if newest_clean is None:
        out.append("   *** NO CLEAN BACKUP EXISTS -- a restore has nothing to restore from.")
    else:
        out.append(f"   newest CLEAN backup: {newest_clean.name}")

    # The app reaches this same physical file through the FUSE mount, where its locks and
    # ours are in different domains. Report the boundary so a reader of this output knows
    # why an overlap is fatal rather than merely untidy (§ diagnosis 4.187).
    if config.YACREADER_DB_MOUNT.exists():
        out.append(f"paths:   app writes {config.YACREADER_DB_MOUNT} (through FUSE)")
        out.append(f"         tools write {db} (SSD) -- same file, locks do not cross")

    status = status_of(c)

    if status == DAMAGED:
        out.append("")
        out.append("THE LIVE INDEX IS DAMAGED. Restore the newest backup that PASSES "
                   "integrity_check (never simply the newest):")
        if newest_clean is not None:
            out.append(f"   cp -p '{newest_clean}' '{db}'")
            out.append("   python3 scripts/comic_shelf_audit.py --apply   # re-drop dead rows")
        else:
            out.append("   -- and there is NO CLEAN BACKUP to restore from. Do not run "
                       "comic_shelf_audit --apply against this index; it refuses a damaged "
                       "one precisely so a repair cannot snapshot the damage (§4.187).")
    elif status == NO_BACKUP:
        # The index is fine, so nothing is broken yet -- which is the entire point. Every
        # recovery this fleet has performed was `cp -p` from a backup that happened to be
        # clean; with none, the next corruption is permanent and the index is rebuilt only
        # by a full YacReader rescan of ~2,700 comics through FUSE. Say so while it is
        # still cheap to fix (§5b item 14).
        out.append("")
        out.append("THE LIVE INDEX IS INTACT, BUT NOTHING COULD RESTORE IT. Every backup "
                   "present fails integrity_check, so the next corruption is PERMANENT.")
        out.append("Take one now, while the index is still clean -- under the lock, so the "
                   "app cannot be mid-write through FUSE (§4.187):")
        out.append("   python3 -c \"import sys; sys.path.insert(0,'.'); import yacreader_db, "
                   "shutil, time; \\")
        out.append("     lock=yacreader_db.db_lock(purpose='health-backup'); lock.__enter__(); "
                   "\\")
        out.append(f"     shutil.copy2('{db}', '{db}.bak-clean-%s' % time.strftime('%Y%m%d-%H%M%S')); "
                   "lock.__exit__(None,None,None)\"")

    if not args.quiet or status != OK:
        print("\n".join(out))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
