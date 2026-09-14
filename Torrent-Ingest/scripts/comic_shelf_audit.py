#!/usr/bin/env python3
"""Empty comic folders on the shelf, and the YacReader rows that outlive their files.

WHAT THE OWNER SEES, AND WHY NOTHING ELSE CATCHES IT
    Two different faults render the same way in YacReader -- a folder that opens onto
    nothing, or a cover that is black with an X:

      * An EMPTY SHELF. A consolidation moved a series' files to their proper folder
        (`Comics/Manga/Akira/Akira 35th Anniversary Edition v01.cbr`) and left the
        emptied edition directory standing on the SSD. mediafs merges ~/Media with the
        pool, so the empty directory keeps appearing in the merged library forever. Seven
        of these had accumulated by 2026-09-05, holding zero pool files between them while
        all 171 of their volumes sat correctly filed under the series folders.
    * A STALE YACREADER ROW. YacReader keeps its OWN SQLite index
      (`~/Media/Comics/.yacreaderlibrary/library.ydb`) and does not drop a folder whose
      directory has gone. A purged series stays in the grid, and its comics render as
      the not-recognised placeholder.
    * A MISSING YACREADER ROW. The opposite silence: a file that IS on the shelf but
      was never indexed, because the app only updates its index when it runs a library
      update. Filed comics then simply do not exist to the reader (the ElfQuest drop,
      2026-09-14) -- reported here, fixed by a rescan rather than a DB edit.

    `purge_sweeper.py` cannot help with any of these: it only ever removes BLOCKLISTED
    titles, which is the safety rule §4.167 was paid for, and none of these is
    blocklisted.

WHAT MAKES THIS SAFE TO ACT ON
    A directory is a shell only when the MOUNT says so AND `remote_inventory.json` records
    ZERO pool files under it. Local emptiness proves nothing at all -- the library is
    evicted, so nearly every comic folder is empty on the SSD. Both conditions must hold.

    Removal is `rmdir` against ~/Media, never `rm -rf` and never through the mount. rmdir
    refuses a non-empty directory, and an unlink through the mount is a POOL DELETION
    (§ diagnosis 4.152) -- exactly what "tidy an empty leftover" must not mean.

    --apply takes the index lock (`yacreader_db`), which stops YACReaderLibrary and keeps
    the library supervisor from restarting it until the edit is done. That is not just to
    stop the app writing its in-memory index back over the edit: the app reaches the same
    physical file THROUGH THE FUSE MOUNT, where its locks and ours are in different
    domains, so an overlap corrupts the database outright (§ diagnosis 4.187). No
    launchctl bootout/bootstrap is needed any more -- releasing the lock is enough.

    python3 scripts/comic_shelf_audit.py            # report only (default)
    python3 scripts/comic_shelf_audit.py --apply    # remove the shells and the stale rows
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yacreader_db
import yacreader_index

MOUNT = Path("/Users/mikeyferguson/MediaLibrary/Comics")
LOCAL = Path("/Users/mikeyferguson/Media/Comics")
INVENTORY = Path("/Users/mikeyferguson/Developer/Media-Fleet/Media-Syncer/remote_inventory.json")
YAC_DB = LOCAL / ".yacreaderlibrary" / "library.ydb"
# Where a comic series directory lives, relative to the Comics root.
SERIES_ROOTS = ("Manga", "")


def _inventory_prefixes():
    """Every inventory key, so a directory's pool contents can be counted by prefix."""
    try:
        return list(json.loads(INVENTORY.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _mount_ready() -> bool:
    """An unavailable mount and an empty one are indistinguishable, and 'empty' invites
    destruction (§ diagnosis 4.108). Refuse to judge anything unless it is clearly up."""
    try:
        return MOUNT.is_dir() and any(MOUNT.iterdir())
    except OSError:
        return False


def find_shells(keys):
    """[(rel_path, pool_file_count)] for every comic directory empty on the MOUNT."""
    out = []
    for root in SERIES_ROOTS:
        base = MOUNT / root if root else MOUNT
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if not root and d.name == "Manga":
                continue                              # walked as its own root
            try:
                if any(x for x in d.iterdir() if not x.name.startswith(".")):
                    continue
            except OSError:
                continue                              # unreadable is NOT empty
            rel = f"Comics/{root}/{d.name}" if root else f"Comics/{d.name}"
            n = sum(1 for k in keys if k.startswith(rel + "/")) if keys is not None else -1
            out.append((rel, n))
    return out


def find_stale_yac_rows():
    """(folder_rows, comic_rows) whose recorded path no longer exists on the mount."""
    if not YAC_DB.exists():
        return [], []
    con = sqlite3.connect(f"file:{YAC_DB}?mode=ro", uri=True)
    try:
        folders = con.execute("SELECT id,path FROM folder WHERE id!=1").fetchall()
        comics = con.execute("SELECT id,parentId,path FROM comic").fetchall()
    finally:
        con.close()
    dead_f = [f for f in folders if not os.path.isdir(str(MOUNT) + f[1])]
    dead_c = [c for c in comics if not os.path.exists(str(MOUNT) + c[2])]
    return dead_f, dead_c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually remove (default: report)")
    args = ap.parse_args()

    if not _mount_ready():
        print("the mediafs Comics mount is absent or empty; refusing to act.")
        return 1

    keys = _inventory_prefixes()
    if keys is None:
        print(f"cannot read {INVENTORY}; refusing to act (a directory cannot be called "
              f"empty without checking the pool).")
        return 1

    shells = find_shells(keys)
    holding = [(r, n) for r, n in shells if n > 0]
    empty = [(r, n) for r, n in shells if n == 0]

    print(f"comic directories EMPTY on the mount: {len(shells)}")
    for rel, n in shells:
        mark = "SHELL" if n == 0 else f"HAS {n} POOL FILE(S) -- LEAVE ALONE"
        print(f"   [{mark}] {rel}")
    if holding:
        print("\n  A directory that reads empty on the mount while the inventory records "
              "pool files under it is an inventory/mount fault, NOT a leftover. Rescan "
              "Media-Syncer's inventory; do not remove it.")

    dead_f, dead_c = find_stale_yac_rows()
    print(f"\nYacReader rows whose path is GONE: {len(dead_f)} folder(s), "
          f"{len(dead_c)} comic(s)")
    for fid, fp in dead_f[:40]:
        print(f"   folder {fid}: {fp}")
    if len(dead_f) > 40:
        print(f"   ... and {len(dead_f) - 40} more")

    missing = yacreader_index.unindexed_files(YAC_DB, INVENTORY, MOUNT) if YAC_DB.exists() else []
    print(f"\nShelf comic files NOT in the YacReader index: {len(missing)}")
    for m in missing[:40]:
        print(f"   {m}")
    if len(missing) > 40:
        print(f"   ... and {len(missing) - 40} more")
    if missing:
        print("   (the app indexes only when it runs a library update -- the supervisor "
              "restarts it when comics are filed, or run scripts/yacreader_rescan.py "
              "--apply now)")

    if not args.apply:
        print("\n(report only -- pass --apply to remove the shells and the stale rows)")
        return 0

    try:
        with yacreader_db.db_lock("comic_shelf_audit"):
            return _apply(empty)
    except (yacreader_db.LockUnavailable, RuntimeError) as exc:
        print(f"\n{exc}")
        return 1


def _apply(empty) -> int:
    """Remove the shells and the stale rows. Runs holding the index lock, app stopped."""
    # Re-read the rows now that the app is down: quitting flushes its in-memory index, so
    # the set computed for the report above is one write out of date by construction.
    dead_f, dead_c = find_stale_yac_rows()

    removed = 0
    for rel, n in empty:
        local = LOCAL / Path(rel).relative_to("Comics")
        try:
            local.rmdir()                     # refuses a non-empty directory, by design
            removed += 1
            print(f"  removed {local}")
        except OSError as exc:
            print(f"  kept {local}: {exc}")

    rows = 0
    if dead_f or dead_c:
        # Check integrity BEFORE taking the backup. A backup of a corrupt database is a
        # corrupt backup, and §4.185 found two of six on-disk backups malformed precisely
        # because they were taken on the way into a repair rather than out of a healthy
        # index -- which is what made "restore the newest backup" the wrong instruction.
        con = sqlite3.connect(f"file:{YAC_DB}?mode=ro", uri=True)
        try:
            integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
        if integ != "ok":
            print(f"  YacReader index is ALREADY damaged ({integ}); refusing to edit it "
                  f"or to back it up. Restore the newest backup that PASSES "
                  f"integrity_check first: python3 scripts/yacreader_index_health.py")
            return 1
        backup = YAC_DB.with_suffix(f".ydb.bak-shelfaudit-{os.getpid()}")
        shutil.copy2(YAC_DB, backup)
        print(f"  YacReader DB backed up to {backup.name} (verified clean)")
        con = sqlite3.connect(str(YAC_DB))
        try:
            # A dead folder can have CHILD folder rows. Without the schema's ON DELETE
            # CASCADE enforced, deleting the parent leaves them dangling -- and a
            # dangling parent is a SIGSEGV inside FolderModel::createModelData the next
            # time YacReader loads the library (scripts/yacreader_index_repair.py).
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("BEGIN")
            for cid, _p, _q in dead_c:
                con.execute("DELETE FROM comic_label WHERE comic_id=?", (cid,))
                con.execute("DELETE FROM comic WHERE id=?", (cid,))
                rows += 1
            for fid, _p in dead_f:
                con.execute("DELETE FROM comic WHERE parentId=?", (fid,))
                con.execute("DELETE FROM folder WHERE id=?", (fid,))
                rows += 1
            con.commit()
            integ = con.execute("PRAGMA integrity_check").fetchone()[0]
            print(f"  YacReader integrity_check after edit: {integ}")
            if integ != "ok":
                print(f"  RESTORE {backup.name} -- the edit left the index damaged.")
                return 1
        finally:
            con.close()

    print(f"\nremoved {removed} shell(s) and {rows} stale YacReader row(s). "
          f"The library supervisor restarts YacReader once the index lock is released.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
