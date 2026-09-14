#!/usr/bin/env python3
"""Names (and repairs) the index rows that crash YACReader's folder loader.

THE CRASH THIS EXISTS FOR
    `FolderModel::createModelData` walks `folder` rows `ORDER BY parentId,name` and calls
    `itemsLookup.value(parentId)->appendChild(...)` with no null check (YACReader 9.16.3).
    One row whose parent is missing from the map -- a dangling parent, a cycle, a missing
    root, or a parent row that sorts after its child -- is an immediate SIGSEGV inside
    `FolderModel::reload` (`LibraryWindow::reloadCurrentLibrary`), which is the crash of
    2026-09-13 that left the app up for ten hours with a stale index. YACReader can
    rebuild an index by rescanning; it cannot survive trying to load a malformed one.

    The repair is deliberately conservative and deterministic:
      * a missing root row is recreated (`id=1`), because every child may point at it;
      * a row whose parent is gone/cyclic/reordered is re-parented to the row its own
        `path` names, but ONLY when that parent id sorts before its own (the loader's
        invariant). Otherwise it is attached to the root -- the nesting can be restored
        by the next full scan, and loadability is the one thing that cannot wait.
    `collection`-level content is untouched; no comic rows are deleted.

    Read-only by default. `--apply` takes the index lock (app stopped), backs the index
    up under a name that PASSES integrity_check, edits, and verifies afterwards. The
    supervisor restarts YacReader when the lock is released.

    python3 scripts/yacreader_index_repair.py
    python3 scripts/yacreader_index_repair.py --apply
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                        # noqa: E402
import yacreader_db                                                  # noqa: E402
import yacreader_index                                               # noqa: E402


def _repair(con: sqlite3.Connection) -> dict:
    """Fix what can be fixed safely; returns counts. Runs with the app stopped."""
    res = {"root": 0, "reparented": 0, "to_root": 0}
    if con.execute("SELECT 1 FROM folder WHERE id=1").fetchone() is None:
        con.execute("INSERT INTO folder (id,parentId,name,path) VALUES (1,1,'root','/')")
        res["root"] = 1

    ids = {r[0] for r in con.execute("SELECT id FROM folder")}
    rows = con.execute("SELECT id,parentId,path FROM folder WHERE id<>1").fetchall()
    for fid, pid, path in rows:
        # The loader's invariant: the parent sorts first, which (parents are created
        # before children) means parentId < id, and the chain must reach the root.
        if pid in ids and pid < fid and _reaches_root(con, fid):
            continue
        parent = None
        if path and str(path).startswith("/") and str(path) != "/":
            parent_path = str(Path(str(path)).parent)
            if parent_path != str(path):
                found = con.execute("SELECT id FROM folder WHERE path=? AND id<>?",
                                    (parent_path, fid)).fetchone()
                if found and found[0] < fid:      # keeps the loader's sort invariant
                    parent = found[0]
        new_pid = parent if parent is not None else 1
        con.execute("UPDATE folder SET parentId=? WHERE id=?", (new_pid, fid))
        if new_pid == 1:
            res["to_root"] += 1
        else:
            res["reparented"] += 1
    return res


def _reaches_root(con: sqlite3.Connection, fid: int) -> bool:
    seen: set[int] = set()
    node = fid
    while node != 1 and node not in seen:
        seen.add(node)
        row = con.execute("SELECT parentId FROM folder WHERE id=?", (node,)).fetchone()
        if row is None:
            return False
        node = row[0]
    return node == 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Repair YacReader index tree shapes.")
    ap.add_argument("--apply", action="store_true", help="actually edit (default: report)")
    args = ap.parse_args()

    db = config.YACREADER_DB
    if not db.exists():
        print(f"no YacReader index at {db}")
        return 1

    faults = yacreader_index.load_order_faults(db)
    if not faults:
        print(f"{db}\nload-order: OK -- no row can crash FolderModel::createModelData")
        return 0

    print(f"{db}\nload-order: {len(faults)} fault(s)")
    for f in faults[:40]:
        print(f"   [{f['kind']:10}] {f['detail']}")
    if len(faults) > 40:
        print(f"   ... and {len(faults) - 40} more")

    if not args.apply:
        print("\n(report only -- pass --apply to repair under the index lock)")
        return 1

    try:
        with yacreader_db.db_lock("yacreader_index_repair"):
            con = sqlite3.connect(str(db))
            try:
                integ = con.execute("PRAGMA integrity_check").fetchone()[0]
                if integ != "ok":
                    print(f"index is ALREADY damaged ({integ}); refusing to edit it. Restore "
                          f"the newest backup that PASSES integrity_check first "
                          f"(scripts/yacreader_index_health.py).")
                    return 1
                backup = db.with_name(f"{db.name}.bak-indexrepair-{os.getpid()}")
                shutil.copy2(db, backup)
                con.execute("BEGIN")
                res = _repair(con)
                con.commit()
                integ = con.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                con.close()
            remaining = yacreader_index.load_order_faults(db)
    except (yacreader_db.LockUnavailable, RuntimeError) as exc:
        print(f"\n{exc}")
        return 1

    print(f"\nbacked up to {backup.name}; root rows={res['root']}, re-parented by path="
          f"{res['reparented']}, attached to root={res['to_root']}")
    print(f"integrity after repair: {integ}")
    if remaining:
        print(f"{len(remaining)} fault(s) remain -- the app may still crash on load:")
        for f in remaining[:10]:
            print(f"   [{f['kind']:10}] {f['detail']}")
        return 1
    print("load-order: OK. The supervisor restarts YacReader once the lock is released.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
