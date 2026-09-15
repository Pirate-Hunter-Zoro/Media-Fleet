"""Read-only queries about YacReader's index: can the app load it, and is it current?

Two questions, both of which the fleet learned to ask the hard way:

  * CAN THE APP LOAD IT? `FolderModel::createModelData` walks the `folder` table
    `ORDER BY parentId,name` and does `itemsLookup.value(parentId)->appendChild(...)`
    with NO null check (YACReader 9.16.3, `folder_model.cpp`). One row whose parent
    is absent from the tree -- a dangling parent, a cycle, a missing root -- is an
    instant SIGSEGV, which is exactly the `FolderModel::reload` crash of 2026-09-13
    (and why the app sat up for ten hours with a stale index instead of scanning).
    `load_order_faults()` reproduces that walk and names the row BEFORE the app
    dereferences it. A library that cannot load is worse than a stale one.

  * IS IT CURRENT? YacReader never notices the filesystem by itself; a comic is in
    the grid only if a library update put it in the index. Comparing the pool and
    the mount against `comic.path` gives the one signal the owner lacked when the
    ElfQuest volumes were filed and stayed invisible: "N files on the shelf are not
    in the index". Read-only, so it is safe with the app up.
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from pathlib import Path

import config


def _nfc(s: str) -> str:
    """Comparison form for a comic path. NFC, because the two sides are written by
    different producers and neither is wrong: the YacReader index stores COMPOSED
    names (`Nausicaä` = U+00E4, written by the Qt app) while APFS/FUSE and the pool
    inventory hand out DECOMPOSED ones (`a` + U+0308). Comparing the raw strings
    reports `Nausicaä of the Valley of the Wind v01.cbr` as missing from an index
    that lists it, forever -- which is a false positive that bounced the reader
    every 15 minutes (fleet_doctor kept "fixing" a shelf that was already indexed).
    Same normalization and same reason as `ingest._path_key`."""
    return unicodedata.normalize("NFC", s)


def load_order_faults(db: Path | str) -> list[dict]:
    """Rows that make YACReader's `createModelData` crash, in the order it would hit them.

    Each fault is `{"kind", "detail", "id"}`. `kind` is one of:
      * `no-root`     -- the `folder` row id=1 is missing, so `createRoot` returns null
      * `dangling`    -- a row names a parentId no row has
      * `cycle`       -- following parentId never reaches id=1
      * `load-order`  -- the row sorts before its parent in `ORDER BY parentId,name`
      * `unreadable`  -- the database or the folder table cannot be read at all

    The simulation is the app's exact walk: seed with id=1, then visit
    `ORDER BY parentId,name` and require that every row's parent has already been
    seen. `dangling`/`cycle` are reported separately only to make the repair
    deterministic; the `load-order` list is the literal crash order.
    """
    faults: list[dict] = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [{"kind": "unreadable", "detail": str(exc), "id": None}]
    try:
        try:
            has_root = con.execute("SELECT 1 FROM folder WHERE id=1").fetchone() is not None
            rows = con.execute(
                "SELECT id,parentId,name,path FROM folder WHERE id<>1 "
                "ORDER BY parentId,name").fetchall()
        except sqlite3.DatabaseError as exc:
            return [{"kind": "unreadable", "detail": str(exc), "id": None}]

        if not has_root:
            faults.append({"kind": "no-root", "id": 1,
                           "detail": "folder row id=1 is missing; createRoot() returns null"})

        ids = {r[0] for r in rows} | {1}
        seen = {1}
        for fid, pid, _name, path in rows:
            if pid not in seen:
                faults.append({"kind": "load-order", "id": fid,
                               "detail": f"row id={fid} ({path!r}) is loaded before its "
                                         f"parent id={pid}"})
            seen.add(fid)

        # Independent classification for the repairer: a missing parent and a cycle are
        # fixed differently, and a row can be both malformed and late in the order.
        for fid, pid, _name, path in rows:
            if pid not in ids:
                faults.append({"kind": "dangling", "id": fid,
                               "detail": f"row id={fid} ({path!r}) names missing parent "
                                         f"id={pid}"})
                continue
            walk, node, steps = set(), fid, 0
            while node != 1 and node not in walk and steps < 10_000:
                walk.add(node)
                row = con.execute("SELECT parentId FROM folder WHERE id=?", (node,)).fetchone()
                if row is None:
                    break
                node = row[0]
                steps += 1
            if node != 1:
                faults.append({"kind": "cycle", "id": fid,
                               "detail": f"row id={fid} ({path!r}) is in a parent cycle"})
    finally:
        con.close()
    return faults


def index_names(db: Path | str) -> set[str]:
    """Every comic path in the index, relative to the Comics root (`ElfQuest/v01.cbr`)."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        rows = con.execute("SELECT path FROM comic WHERE path IS NOT NULL").fetchall()
    except sqlite3.DatabaseError:
        return set()
    finally:
        con.close()
    return {_nfc(str(r[0]).lstrip("/")) for r in rows if r[0]}


def shelf_names(inventory_path: Path | str, mount_root: Path | str,
                include_mount: bool = True) -> set[str]:
    """Comic files the fleet HOLDS, relative to the Comics root.

    The union of the pool inventory (authoritative; eviction does not remove a comic)
    and the mounted tree (a just-filed file may not be uploaded yet). Never the SSD
    alone -- that is the HANDOFF's first rule.

    `include_mount=False` skips the FUSE walk: a 5-minute health daemon must not rglob
    thousands of comics through the mount. The pool view can only UNDER-report (a file
    filed seconds ago and not yet uploaded), never over-report, so the periodic check
    stays conservative; the refresh marker covers the fresh-filing case.
    """
    exts = tuple(config.COMIC_EXTENSIONS)
    out: set[str] = set()
    try:
        pool = json.loads(Path(inventory_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pool = {}
    if isinstance(pool, dict):
        for rel in pool:
            parts = str(rel).split("/")
            if len(parts) >= 3 and parts[0] == "Comics" and parts[-1].lower().endswith(exts):
                out.add(_nfc("/".join(parts[1:])))
    if not include_mount:
        return out
    root = Path(mount_root)
    try:
        for p in root.rglob("*"):
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in exts:
                try:
                    out.add(_nfc(str(p.relative_to(root))))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def unindexed_files(db: Path | str, inventory_path: Path | str,
                    mount_root: Path | str, include_mount: bool = True) -> list[str]:
    """Shelf comic files YacReader's index does not know about, sorted.

    An empty result is the only honest "the reader is current". A non-empty one means
    the app has not run a library update since those files landed -- the state the
    ElfQuest drop sat in, with every file present and nothing in the grid.
    """
    return sorted(shelf_names(inventory_path, mount_root, include_mount) - index_names(db))
