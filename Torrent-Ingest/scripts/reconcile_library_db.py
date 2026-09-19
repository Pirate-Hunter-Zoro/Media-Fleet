#!/usr/bin/env python3
"""Make `library.db` stop over-claiming. Read-only unless you pass --apply.

HANDOFF §6 carried this as an accepted limit: "`library.db` over-claims and its reconcile
tool went with the searcher. If a re-drop completes having filed nothing and you know the
content is absent, delete that title's rows by hand."

Both halves of that are now fixable, and they are DIFFERENT problems:

1. DUPLICATE ROWS -- the big one, and it was never diagnosed. `librarydb.add_media` is a
   bare INSERT with no uniqueness on (series_id, mtype, season, number), so every re-file
   of an episode appends ANOTHER owned row. Measured 2026-09-12 on the live DB: 41,586
   owned rows against 30,831 distinct items -- 10,755 redundant (25.9%) -- with
   `That '70s Show` holding 3,120 rows for 209 distinct episodes, up to 59 rows for a
   single episode. No status flip can fix this, because every duplicate is a row for an
   item that genuinely IS owned.

2. STALE ROWS -- `librarydb.reconcile_media` already handles these, marking owned rows
   whose item is absent from the inventory `superseded` and restoring ones that came back.
   It was never dead code by intent: the SEARCHER called it, and when discovery was removed
   on 2026-09-10 the function was orphaned with no caller. Nothing has reconciled since.

   It needs an `inv` that the searcher used to build, so this rebuilds it from the two
   sources that still exist: the MOUNT and Media-Syncer's `remote_inventory.json`.

**THE MOUNT, NEVER THE SSD.** HANDOFF's first rule: a file missing from `~/Media` has been
EVICTED to the pool, not lost. Reading the SSD here would mark every evicted episode a
stale lie and supersede a correct library wholesale. The inventory is the UNION of the
mount and the pool, which is exactly "what the fleet still owns".

    python3 scripts/reconcile_library_db.py             # report only (default)
    python3 scripts/reconcile_library_db.py --apply     # write, after backing the DB up
    python3 scripts/reconcile_library_db.py --apply --include-requested
        # also sweep absent new.txt/watchlist/ingest series that still hold owned rows
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import dbhook                                                        # noqa: E402

librarydb = dbhook.librarydb

REMOTE_INVENTORY = Path.home() / "Developer" / "Media-Orchestrator" / "Media-Syncer" / "remote_inventory.json"

_EP_RE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,4})")
_VOL_RE = re.compile(r"\bv(\d{1,4})\b", re.IGNORECASE)
_CH_RE = re.compile(r"\bc(\d{1,4})\b", re.IGNORECASE)
_VIDEO = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm")
_BOOK = (".cbz", ".cbr", ".cb7", ".pdf", ".epub")


def _norm(s: str) -> str:
    return librarydb._normalize(s)


def _add_path(inv: dict, rel: str) -> None:
    """Fold one library-relative path into the inventory structure.

    The path shape is identical on the mount and in the pool inventory
    (`Shows/<Name>/Season NN/<Name> - S01E02.mkv`), which is what lets one parser serve
    both and is why the union is cheap.
    """
    parts = rel.split("/")
    if len(parts) < 2:
        return
    top, name = parts[0], parts[1]
    leaf = parts[-1]
    ext = Path(leaf).suffix.lower()

    if top == "Shows" and ext in _VIDEO:
        m = _EP_RE.search(leaf)
        if not m:
            return
        season, ep = int(m.group(1)), int(m.group(2))
        show = inv["shows"].setdefault(_norm(name), {"seasons": defaultdict(set)})
        show["seasons"][season].add(ep)
    elif top == "Movies" and ext in _VIDEO:
        # A movie is either `Movies/<Title>/<file>` or `Movies/<file>`.
        title = name if len(parts) > 2 else Path(leaf).stem
        inv["movies"].setdefault(_norm(title), {})
    elif top == "Comics" and ext in _BOOK:
        comic = inv["comics"].setdefault(_norm(name), {"volumes": {}, "chapters": set()})
        mv = _VOL_RE.search(leaf)
        mc = _CH_RE.search(leaf)
        if mv:
            comic["volumes"][int(mv.group(1))] = 1
        elif mc:
            comic["chapters"].add(int(mc.group(1)))
    elif top in ("Books", "Novels") and ext in _BOOK:
        novel = inv["novels"].setdefault(_norm(name), {"volumes": set()})
        mv = _VOL_RE.search(leaf)
        if mv:
            novel["volumes"].add(int(mv.group(1)))


def build_inventory() -> tuple[dict, dict]:
    """The union of what is on the MOUNT and what is in the pool. Returns (inv, stats)."""
    inv = {"shows": {}, "movies": {}, "comics": {}, "novels": {}}
    stats = {"mount_files": 0, "pool_files": 0}

    root = Path(config.LIBRARY_ROOT) if hasattr(config, "LIBRARY_ROOT") else None
    mount = Path.home() / "MediaLibrary"
    if root is not None and str(root).startswith(str(Path.home() / "Media")) \
            and "MediaLibrary" not in str(root):
        # config.LIBRARY_ROOT is the SSD. Deliberately not used -- see the module docstring.
        pass
    if mount.is_dir():
        for p in mount.rglob("*"):
            if p.is_file():
                try:
                    rel = str(p.relative_to(mount))
                except ValueError:
                    continue
                _add_path(inv, rel)
                stats["mount_files"] += 1

    if REMOTE_INVENTORY.exists():
        try:
            pool = json.loads(REMOTE_INVENTORY.read_text())
        except Exception as e:                                       # noqa: BLE001
            print(f"WARNING: could not read {REMOTE_INVENTORY}: {e}")
            pool = {}
        for key in pool:
            _add_path(inv, key)
            stats["pool_files"] += 1

    # reconcile_media wants plain containers, and `seasons` as {season: iterable}.
    for show in inv["shows"].values():
        show["seasons"] = {s: sorted(eps) for s, eps in show["seasons"].items()}
    for comic in inv["comics"].values():
        comic["chapters"] = sorted(comic["chapters"])
    for novel in inv["novels"].values():
        novel["volumes"] = sorted(novel["volumes"])
    return inv, stats


def find_duplicates(conn) -> list[tuple]:
    """Owned rows sharing (series_id, mtype, season, number). Returns the rows to DROP.

    Keeps the LOWEST id of each group -- the first time the fleet recorded the item -- and
    drops the rest. Which survivor is kept barely matters (every duplicate describes the
    same item); keeping the oldest makes the choice deterministic and replayable.
    """
    rows = conn.execute(
        "SELECT id, series_id, mtype, season, number FROM media WHERE status = 'owned' "
        "ORDER BY id").fetchall()
    seen: dict = {}
    drop: list = []
    for r in rows:
        k = (r["series_id"], r["mtype"], r["season"], r["number"])
        if k in seen:
            drop.append((r["id"], k))
        else:
            seen[k] = r["id"]
    return drop


# Kinds this tool is willing to SUPERSEDE on. An episode or a movie has an unambiguous
# identity in the tree (`SxxEyy`, a Movies folder) that the inventory parser recovers
# reliably. A manga does not: comics nest at two different depths and most filenames carry
# no `vNN`/`cNNN` marker at all, so the parser sees 8 comics in a 2,692-comic library.
#
# Superseding a manga row on that evidence would be a FALSE POSITIVE of exactly the kind
# HANDOFF §6 warns has already shipped four times -- and an expensive one, because a
# superseded row reads as "not owned" and invites a re-download of content already held.
# So the stale pass declines to judge them, out loud, rather than guessing.
VERIFIABLE_KINDS = {"anime", "tv", "movie"}


def stale_pass(conn, inv: dict, include_requested: bool = False) -> dict:
    """Supersede owned rows whose item is absent from the inventory; restore ones that returned.

    Deliberately NOT `librarydb.reconcile_media`, which supersedes EVERY owned row of any
    library-sourced series whose normalized name is missing from `inv` -- including the
    manga and light-novel series this inventory cannot see. That function is still correct
    for the caller it was written for (the searcher, which built a full four-category
    inventory); it is wrong to hand it a shows-and-movies-only inventory.

    `include_requested` extends the absent-series rule to `new.txt`/`watchlist`/`ingest`
    sources -- but only when the series has owned rows. The default spares those sources
    because a fresh acquisition may be mid-filing; an owned row is proof files DID land,
    so a series absent from the inventory is an over-claim whichever list admitted it,
    and a re-drop the acceptance gate would refuse as already owned. Every row is
    recoverable: the restore branch flips it back if the content reappears.
    """
    owned: dict = {}
    for norm, cov in (inv.get("shows") or {}).items():
        keys = set()
        for s, eps in (cov.get("seasons") or {}).items():
            for ep in eps:
                keys.add(librarydb.item_key("episode", int(s), ep))
        owned[(norm, "anime")] = keys
        owned[(norm, "tv")] = keys
    for norm in (inv.get("movies") or {}):
        owned[(norm, "movie")] = {librarydb.item_key("movie", None, None)}

    superseded = restored = skipped = 0
    for row in conn.execute("SELECT id, norm, kind, source FROM series").fetchall():
        sid, norm, kind, source = row["id"], row["norm"], row["kind"], row["source"]
        if kind not in VERIFIABLE_KINDS:
            skipped += 1
            continue
        keys = owned.get((norm, kind))
        if keys is None:
            # The series is not in the inventory at all. A series SEEDED FROM THE
            # LIBRARY is judged outright. A new.txt/watchlist/ingest one is spared by
            # default (it may be mid-filing) -- unless `include_requested` is on and it
            # has owned rows, which is evidence files landed and are now gone.
            rows = librarydb.owned_media(conn, sid)
            if source != "library" and not (include_requested and rows):
                continue
            for m in rows:
                conn.execute("UPDATE media SET status='superseded' WHERE id=?", (m["id"],))
                superseded += 1
            continue
        for m in librarydb.owned_media(conn, sid):
            if librarydb.item_key(m["mtype"], m["season"], m["number"]) not in keys:
                conn.execute("UPDATE media SET status='superseded' WHERE id=?", (m["id"],))
                superseded += 1
        # RESTORE, without re-creating the duplicates the pass above just collapsed.
        # A superseded row and an owned row can both exist for one item (the item was
        # filed twice, then one copy went missing and was superseded). Flipping the
        # superseded one back without checking hands the table a fresh duplicate --
        # measured: 18 rows across 12 items, all of them Peacemaker and The Office,
        # reappearing immediately after the first --apply run.
        live = {librarydb.item_key(m["mtype"], m["season"], m["number"])
                for m in librarydb.owned_media(conn, sid)}
        for m in conn.execute("SELECT id, mtype, season, number FROM media WHERE "
                              "series_id=? AND status='superseded'", (sid,)).fetchall():
            k = librarydb.item_key(m["mtype"], m["season"], m["number"])
            if k not in keys or k in live:
                continue
            conn.execute("UPDATE media SET status='owned' WHERE id=?", (m["id"],))
            live.add(k)
            restored += 1
    return {"superseded": superseded, "restored": restored, "skipped_series": skipped}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default: report only)")
    ap.add_argument("--skip-stale", action="store_true",
                    help="collapse duplicates only; do not run the stale/restore pass")
    ap.add_argument("--include-requested", action="store_true",
                    help="also supersede absent new.txt/watchlist/ingest series when they "
                         "have owned rows (a settled purge, not an in-flight acquisition)")
    args = ap.parse_args()

    print("=== library.db reconcile ===\n")

    inv, stats = build_inventory()
    print(f"inventory: {stats['mount_files']} file(s) on the mount + "
          f"{stats['pool_files']} pool key(s)")
    print(f"           {len(inv['shows'])} show(s), {len(inv['movies'])} movie(s), "
          f"{len(inv['comics'])} comic(s), {len(inv['novels'])} novel(s)")

    if not inv["shows"] and not inv["movies"]:
        print("\nREFUSING TO ACT: the inventory is empty. That means the mount is not up "
              "or\nremote_inventory.json is unreadable -- not that the library is gone. "
              "Superseding\nevery row against an empty inventory is exactly the damage "
              "this check exists to stop.")
        return 1

    conn = librarydb.connect()
    owned_before = conn.execute(
        "SELECT COUNT(*) FROM media WHERE status = 'owned'").fetchone()[0]
    distinct = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT series_id, mtype, season, number "
        "FROM media WHERE status = 'owned')").fetchone()[0]

    drop = find_duplicates(conn)
    print(f"\nduplicates: {owned_before} owned row(s), {distinct} distinct item(s) "
          f"-> {len(drop)} redundant "
          f"({100.0 * len(drop) / owned_before:.1f}%)" if owned_before else "")

    if drop:
        worst: dict = defaultdict(int)
        for _id, k in drop:
            worst[k[0]] += 1
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM series")}
        print("  worst offenders:")
        for sid, n in sorted(worst.items(), key=lambda x: -x[1])[:5]:
            print(f"    {n:6d} redundant row(s)  {names.get(sid, f'series {sid}')!r}")

    if not args.apply:
        print("\n--- REPORT ONLY. Re-run with --apply to write. ---")
        if not args.skip_stale:
            print("(the stale/restore pass is not simulated here: it needs the write "
                  "connection\n reconcile_media opens. --apply reports its counts.)")
        return 0

    backup = config.STATE_DIR / f"library.db.bak-reconcile-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(librarydb.path(), backup)
    print(f"\nbacked up to {backup.name}")

    if drop:
        conn.executemany("DELETE FROM media WHERE id = ?", [(d[0],) for d in drop])
        conn.commit()
        print(f"collapsed {len(drop)} duplicate row(s)")

    if not args.skip_stale:
        res = stale_pass(conn, inv, include_requested=args.include_requested)
        # Comics get their own pass: their inventory nests at unknown depth and only the
        # POOL can say which root a series belongs to, so the parser lives in dbhook next
        # to the purge matcher. It folds comic/manga kind-split pairs (the `record_plan`
        # bug) and supersedes numbered rows the pool no longer holds. Fail-open.
        rec = dbhook.reconcile_comics(conn, REMOTE_INVENTORY)
        conn.commit()
        print(f"stale pass: {res['superseded']} row(s) superseded, "
              f"{res['restored']} restored, {res['skipped_series']} series skipped "
              f"(unverifiable kind)"
              + ("  [--include-requested]" if args.include_requested else ""))
        print(f"comic pass: {rec['folded']} kind-split series folded, "
              f"{rec['superseded']} absent row(s) superseded, "
              f"{rec['dropped']} duplicate(s) dropped, {rec['skipped']} pair(s) skipped"
              + (f"  [{rec['note']}]" if rec.get("note") else ""))

    owned_after = conn.execute(
        "SELECT COUNT(*) FROM media WHERE status = 'owned'").fetchone()[0]
    print(f"\nowned rows: {owned_before} -> {owned_after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
