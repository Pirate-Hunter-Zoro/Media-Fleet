#!/usr/bin/env python3
"""Reap the empty shells a purge leaves behind, and the Jellyfin rows that resurrect them.

THE LOOP THIS BREAKS
    Purging a title deletes its media, but the directory survives on both trees until the
    reaper has purged every pool copy and pruned the inventory -- hours, for a large purge.
    Jellyfin's libraries point at the mediafs MOUNT and run with `SaveLocalMetadata: True`,
    so every scan in that window re-indexes the surviving directory as a series and writes
    `tvshow.nfo` + `folder.jpg` back into it. The write lands in ~/Media, the directory is
    non-empty again, and the next scan finds it again. The owner sees purged shows in Infuse
    and reasonably concludes the purge did not work.

    Turning `SaveLocalMetadata` off would break the loop and also break the fleet: the
    per-episode `.nfo` sidecars are the metadata store, they survive eviction when the video
    does not, and `media_doctor` relies on Jellyfin re-saving them after a POST. So the
    sidecars stay, and this sweeper removes the shells instead.

WHAT COUNTS AS A SHELL, AND WHY IT IS THE MOUNT THAT DECIDES
    A show whose video was evicted has NO local media and is perfectly healthy -- 511 of 518
    show folders were in that state when this was written. Local emptiness therefore proves
    nothing. The pool is the truth, and the mount is the pool's view, so a directory is a
    shell only when the MOUNT copy holds zero media files. Anything with a single video,
    archive or subtitle on the mount is left completely alone.

    Once the reaper finishes a title, its mount directory holds nothing, this removes it from
    both trees and deletes the matching Jellyfin item, and it stops coming back.

    python3 scripts/purge_sweeper.py            # report only (default)
    python3 scripts/purge_sweeper.py --apply    # remove the shells and their Jellyfin rows
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.request
from pathlib import Path

MOUNT = Path("/Users/mikeyferguson/MediaLibrary")
LOCAL = Path("/Users/mikeyferguson/Media")
MEDIA_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm",
             ".cbz", ".cbr", ".pdf", ".epub", ".srt", ".ass"}
# Where a title directory lives, and how deep the title sits under the root.
TITLE_ROOTS = [("Shows", 1), ("Movies", 1), ("Comics/Manga", 1), ("Comics", 1)]


def _has_media(d: Path) -> bool:
    """True if any real media file lives under `d`. Sidecars alone do not count."""
    try:
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if not x.startswith(".")]
            for f in filenames:
                if f.startswith("."):
                    continue
                if os.path.splitext(f)[1].lower() in MEDIA_EXT:
                    return True
    except OSError:
        # Unreadable is not empty. Never treat an I/O failure as "nothing here" -- that is
        # the mistake that turns an unavailable mount into a deletion order.
        return True
    return False


def _jellyfin(url: str, key: str, path: str, method: str = "GET"):
    req = urllib.request.Request(f"{url}{path}{'&' if '?' in path else '?'}api_key={key}",
                                 method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r) if method == "GET" else None


def _norm(s: str) -> str:
    s = re.sub(r"\((?:19|20)\d{2}\)", " ", s or "")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _blocked_norms() -> set:
    """The owner's purge blocklist, from Torrent-Searcher. Empty set if unreadable."""
    p = Path("/Users/mikeyferguson/Developer/Media-Fleet/Torrent-Ingest/state/blocklist.json")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    names = raw.get("titles", []) if isinstance(raw, dict) else raw
    return {k for k in (_norm(n) for n in names if isinstance(n, str)) if k}


def _is_blocked(name: str, blocked: set) -> bool:
    k = _norm(name)
    return bool(k) and any(k == b or k.startswith(b + " ") or f" {b} " in f" {k} "
                           for b in blocked)


def _mark_blocked_pending() -> int:
    """`.ignore` every blocked title that still holds media, so Jellyfin stops re-adding it
    while the reaper drains its pool copies. Idempotent."""
    blocked = _blocked_norms()
    if not blocked:
        return 0
    n = 0
    for rel, _d in TITLE_ROOTS:
        root = MOUNT / rel
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            if rel == "Comics" and d.name == "Manga":
                continue
            if not _is_blocked(d.name, blocked) or not _has_media(d):
                continue
            marker = d / ".ignore"
            if marker.exists():
                continue
            try:
                marker.write_text("")
                n += 1
            except OSError:
                pass
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually remove (default: report)")
    args = ap.parse_args()

    if not MOUNT.is_dir() or not any((MOUNT / "Shows").iterdir()):
        print("mediafs mount is absent or empty; refusing to act (an unavailable mount and "
              "an empty one are indistinguishable, and 'empty' invites destruction).")
        return 1

    # ONLY blocklisted titles are ever removed. This is the load-bearing safety rule and it
    # was learned the expensive way: an earlier version removed ANY directory that looked
    # empty on the mount, and "looks empty" is not the same fact as "was purged". A mount
    # directory reads as empty whenever its `remote_inventory.json` keys are momentarily
    # absent -- during an inventory rewrite, a mediafs reload, a partial read -- and removing
    # it through the mount does not merely tidy a shell: mediafs turns that unlink into
    # DELETION-QUEUE entries and the reaper then purges every pool copy. That is how Boruto,
    # Ghost in the Shell SAC, Dragon Ball, Dragon Ball Super, Legend of the Galactic Heroes,
    # Vinland Saga, Laid-Back Camp and eleven other KEPT shows -- 1,205 files -- ended up
    # queued for destruction by a script whose entire job was tidying up. They were caught in
    # the queue before the reaper reached them; the margin was luck, not design.
    #
    # An empty directory that is NOT blocklisted is therefore a SYMPTOM, never an
    # instruction. It is reported and left completely alone.
    blocked = _blocked_norms()
    shells: list[Path] = []
    unexpected: list[Path] = []
    for rel, _depth in TITLE_ROOTS:
        root = MOUNT / rel
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if rel == "Comics" and d.name == "Manga":
                continue
            if _has_media(d):
                continue
            (shells if _is_blocked(d.name, blocked) else unexpected).append(d)

    if unexpected:
        print(f"\n!! {len(unexpected)} EMPTY director(ies) that are NOT blocklisted -- NOT "
              f"touching them. An empty non-blocklisted title means something is wrong "
              f"(a stale inventory, a mid-write read), not that it was purged:")
        for d in unexpected[:20]:
            print(f"     {d.relative_to(MOUNT)}")

    # A blocked title whose pool copies have NOT drained yet is not a shell -- it still has
    # media -- so the sweeper cannot remove it, and Jellyfin keeps re-indexing it on every
    # scan for as long as the drain takes (hours). Drop a `.ignore` marker in those, which
    # Jellyfin honours by skipping the folder outright. The marker is a dotfile, so it never
    # counts as media here and the directory still reads as a shell once the drain finishes.
    blocked_pending = _mark_blocked_pending()
    if blocked_pending:
        print(f"wrote .ignore into {blocked_pending} blocked director(ies) still draining")

    print(f"empty shells on the mount: {len(shells)}")
    for d in shells:
        print(f"   {d.relative_to(MOUNT)}")
    if not shells:
        return 0

    url, key = os.environ.get("JELLYFIN_URL"), os.environ.get("JELLYFIN_API_KEY")
    names = {_norm(d.name) for d in shells}
    jf_hits = []
    if url and key:
        try:
            for kind in ("Series", "Movie"):
                items = _jellyfin(url, key, f"/Items?Recursive=true&IncludeItemTypes={kind}"
                                            f"&Limit=5000")["Items"]
                jf_hits += [i for i in items if _norm(i["Name"]) in names]
            print(f"matching Jellyfin items: {len(jf_hits)}")
        except Exception as exc:                                       # noqa: BLE001
            print(f"jellyfin unreachable ({exc}); will still remove the directories")
    else:
        print("JELLYFIN_URL / JELLYFIN_API_KEY not set; skipping the Jellyfin half")

    if not args.apply:
        print("\nREPORT ONLY. Re-run with --apply to remove them.")
        return 0

    # Jellyfin first: deleting the row before the directory means a scan racing this cannot
    # re-save a sidecar into a directory we are about to remove.
    for i in jf_hits:
        try:
            _jellyfin(url, key, f"/Items/{i['Id']}", method="DELETE")
        except Exception as exc:                                       # noqa: BLE001
            print(f"  jellyfin delete failed for {i['Name']}: {exc}")
    removed = 0
    for d in shells:
        if _has_media(d):            # re-check: the tier engine may have restored something
            continue
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(LOCAL / d.relative_to(MOUNT), ignore_errors=True)
        removed += 1
    print(f"removed {removed} shell(s); deleted {len(jf_hits)} Jellyfin item(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
