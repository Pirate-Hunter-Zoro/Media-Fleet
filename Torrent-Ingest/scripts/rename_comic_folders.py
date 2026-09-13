#!/usr/bin/env python3
"""Rename a comic/manga folder on the POOL as well as locally, and fix the inventory.

WHY THIS EXISTS
    Owner rule, 2026-09-05: **a manga folder is named for the SERIES, never for the edition.**
    `Vinland Saga 2-in-1 Edition`, `Dragon Ball Colored`, `Ranma ½ 2-in-1 Edition` and
    `xxxHOLiC Omnibus Edition` are all the same series as their plain name, and labelling the
    folder with the edition splits one series across two folders the moment a volume arrives
    under the ordinary title. That is exactly what happened: `Vinland Saga v29.cbz` sat alone
    in `Vinland Saga/` beside a 14-volume `Vinland Saga 2-in-1 Edition/`, was mistaken for a
    redundant duplicate, and was deleted -- the one volume the 2-in-1 edition did not cover.

WHY IT IS NOT `mv`
    Comics live on the MEGA pool; the SSD is a cache and cold files are evicted. A local-only
    rename makes the new path "on no remote" so Media-Syncer re-uploads it, while write-once
    keeps the old copy and the download phase fetches the old path back. Every rename has to
    land on the remote too, and the inventory has to be rewritten, or the next scan undoes it.
    This is the same contract `migrate_comic_franchises.py` follows.

    Stop `mediasync` and `directingest` first -- `scripts/migrate_comics.sh` shows the shape.

    python3 scripts/rename_comic_folders.py            # plan only
    python3 scripts/rename_comic_folders.py --apply    # do it
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                          # noqa: E402

SYNCER = Path.home() / "Developer/Media-Syncer"
INVENTORY = SYNCER / "remote_inventory.json"
MANGA = "Comics/Manga/"
WORKERS = 5

# The edition/format labels that must never appear in a folder name. Ordered longest-first so
# "Full Color Collection" is stripped before "Color".
EDITION_RE = re.compile(
    r"\s*[-–:]?\s*("
    r"\d+(?:st|nd|rd|th)\s+Anniversary\s+Edition|Full\s+Colou?r\s+Collection|"
    r"Minimalist\s+Colou?r|Collector'?s\s+Edition|Eternal\s+Edition|VIZBIG\s+Edition|"
    r"Omnibus\s+Edition|Complete\s+Edition|Deluxe\s+Edition|Resurrected\s+Edition|"
    r"2[-\s]?in[-\s]?1\s+Edition|Colou?red|Omnibus|VIZBIG|Box\s+Set"
    r")\s*$", re.I)


def plain_name(name: str) -> str:
    prev = None
    out = name
    while prev != out:
        prev = out
        out = EDITION_RE.sub("", out).strip(" -–:")
    return out


def _queued_for_deletion() -> set[str]:
    out: set[str] = set()
    for n in ("mediafs_deletions.jsonl", "mediafs_deletions.jsonl.processing"):
        try:
            for line in (SYNCER / n).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        out.add(json.loads(line)["path"])
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass
    return out


def _rclone(args: list[str]) -> tuple[int, str]:
    p = subprocess.run([config.RCLONE_BIN, *args, "--config", str(config.RCLONE_CONFIG)],
                       capture_output=True, text=True, timeout=300)
    return p.returncode, (p.stdout + p.stderr).strip()


def build_plan() -> list[dict]:
    inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
    queued = _queued_for_deletion()
    rows, skipped = [], 0
    for key, val in inv.items():
        if not key.startswith(MANGA):
            continue
        rest = key[len(MANGA):]
        top = rest.split("/")[0]
        new_top = plain_name(top)
        if new_top == top or not new_top:
            continue
        if key in queued:                     # never move what the reaper is about to purge
            skipped += 1
            continue
        remote = val[0] if isinstance(val, list) and val else None
        if not remote:
            continue                          # a move with nowhere to go is not a move
        rows.append({"src": key, "dst": MANGA + new_top + rest[len(top):],
                     "remote": remote, "old": top, "new": new_top})
    if skipped:
        print(f"  skipped {skipped} file(s) queued for deletion")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    plan = build_plan()
    byfolder: dict[tuple[str, str], int] = defaultdict(int)
    for r in plan:
        byfolder[(r["old"], r["new"])] += 1
    print(f"\nfolders to rename: {len(byfolder)}   files to move: {len(plan)}")
    for (old, new), n in sorted(byfolder.items()):
        print(f"   {n:4d}  {old}  ->  {new}")
    if not args.apply:
        print("\n(dry run -- pass --apply to execute)")
        return 0

    ok = fail = 0
    succeeded: list[dict] = []
    def one(r):
        rc, out = _rclone(["moveto", f"{r['remote']}:{r['src']}", f"{r['remote']}:{r['dst']}",
                           "--timeout", "120s"])
        return r, rc == 0, out
    # One folder at a time; only files within a folder move concurrently. Parallel moves into
    # a destination that does not exist yet is what created duplicate MEGA directories before.
    for (old, new) in sorted(byfolder):
        rows = [r for r in plan if r["old"] == old]
        print(f"  {old} -> {new}  ({len(rows)} files)", flush=True)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for r, good, out in ex.map(one, rows):
                if good:
                    ok += 1
                    succeeded.append(r)
                else:
                    fail += 1
                    print(f"     FAILED {r['src']}: {out[:120]}")
    print(f"\nmoved {ok}, failed {fail}")

    if ok:
        inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
        # ONLY the moves that actually succeeded. Rewriting a key for a FAILED move points
        # the inventory at a path the pool does not have, so mediafs stops serving the file
        # and it reads as lost while sitting safely at its old name. That happened on the
        # first run of this script: 11 of 301 moves failed to an rclone.conf race and all
        # 301 keys were rewritten anyway.
        moved = {r["src"]: r["dst"] for r in succeeded}
        for src, dst in moved.items():
            if src in inv:
                inv[dst] = inv.pop(src)
        tmp = INVENTORY.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(inv))       # write-then-replace: a torn 16 MB read is a
        tmp.replace(INVENTORY)                # mount that looks empty (see purge_sweeper)
        print(f"  inventory: rewrote {len(moved)} key(s)")
        done_folders = {(r["old"], r["new"]) for r in succeeded}
        for (old, new) in sorted(done_folders):
            lo, ln = config.MEDIA_ROOT / MANGA / old, config.MEDIA_ROOT / MANGA / new
            if lo.is_dir():
                ln.mkdir(parents=True, exist_ok=True)
                for p in lo.rglob("*"):
                    if p.is_file():
                        t = ln / p.relative_to(lo)
                        t.parent.mkdir(parents=True, exist_ok=True)
                        if not t.exists():
                            p.rename(t)
                _prune_emptied(lo)
    return 0 if fail == 0 else 1


def _prune_emptied(local_dir: Path) -> None:
    """Remove the LOCAL source directory once its files have moved, deepest-first.

    Without this the emptied directory survives on the SSD, mediafs keeps presenting it as
    part of the merged library, and the owner sees a row of EMPTY manga folders in
    YacReader and Infuse for editions that were correctly consolidated months ago. Seven of
    them accumulated this way -- Akira 35th Anniversary Edition, Jojo's Bizarre Adventure
    Colored, Parasyte Full Color Collection and four more -- every one holding zero pool
    files while its 171 volumes sat correctly filed under the series folder.

    `rmdir`, never `rm -rf`: it refuses a directory that still holds anything, so a move
    that silently failed leaves its source standing instead of being tidied away with the
    content still in it. And it runs against `~/Media`, NOT the mount -- an unlink through
    the mount is a POOL DELETION (§ diagnosis 4.152), which is emphatically not what
    "remove an empty leftover directory" should mean.
    """
    for d in sorted((p for p in local_dir.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass                            # not empty, or gone already: leave it standing
    try:
        local_dir.rmdir()
        print(f"  pruned emptied local folder {local_dir.name}")
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
