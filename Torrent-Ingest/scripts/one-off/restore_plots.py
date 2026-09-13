#!/usr/bin/env python3
"""Undo the unintended half of the 2026-09-03 synopsis pass.

WHAT WENT WRONG. `fill_synopses.py` was meant to fill only BLANK synopses. Its
"is it blank?" test read `Overview` off the objects returned by
`media_doctor.Jellyfin.episodes()` -- and that method requests
`Fields="Path,ParentIndexNumber,IndexNumber"`, so `Overview` is never present and the
test was always true. The sidecar guard held (the script itself never overwrote a
populated `<plot>`), but the script ALSO POSTed the provider plot to Jellyfin, and
Jellyfin's Nfo metadata saver then wrote that straight back down into the sidecar --
defeating the guard through a path that was not anticipated.

Net effect, measured against the pre-run backup: **0 blank synopses filled**, 221 already
correct plots replaced with TVMaze's wording for the same episode, and `Overview` locked
on 250. The replacements are for the RIGHT episodes -- the season mapping was
anchor-proven -- so nothing is mis-described. But it is not a repair and it was not asked
for, so it goes back.

This restores every sidecar byte-for-byte from `nfo-backup2-20260903.tgz`, pushes the
restored plot back into Jellyfin, and UNLOCKS Overview so a future scrape can improve it
again.

    python3 restore_plots.py          # dry run
    python3 restore_plots.py --apply
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

BACKUP = Path(__file__).resolve().parent / "cmp"
LIVE = Path.home() / "Media" / "Shows"
JF_URL = os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8096")
JF_KEY = os.environ.get("JELLYFIN_API_KEY", "")
_UID = None


def jf(path, method="GET", body=None, **params):
    params = dict(params)
    params["api_key"] = JF_KEY
    url = f"{JF_URL}/{path.lstrip('/')}?{urllib.parse.urlencode(params, doseq=True)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Emby-Token", JF_KEY)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()
        if raw and r.headers.get("content-type", "").startswith("application/json"):
            return json.loads(raw)
        return None


def uid():
    global _UID
    if _UID is None:
        _UID = jf("Users")[0]["Id"]
    return _UID


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    # Jellyfin episode item ids, keyed by the file path they point at. Built from the
    # library-wide Episode query rather than /Shows/{id}/Episodes: that endpoint filters on
    # SeriesPresentationUniqueKey and can hand back another series' episodes entirely (it is
    # what made Dr. STONE serve 146 episodes for 96 files).
    by_path = {}
    for e in jf("Items", Recursive="true", IncludeItemTypes="Episode",
                Fields="Path", userId=uid(), enableImages="false")["Items"]:
        if e.get("Path"):
            by_path[e["Path"]] = e["Id"]

    restored = synced = 0
    for f in sorted(BACKUP.rglob("*.nfo")):
        rel = f.relative_to(BACKUP)
        cur = LIVE / rel
        if not cur.is_file() or filecmp.cmp(f, cur, shallow=False):
            continue
        plot = (re.search(r"<plot>(.*?)</plot>",
                          f.read_text(encoding="utf-8", errors="replace"), re.S)
                or [None, ""])[1].strip()
        if args.apply:
            shutil.copy2(f, cur)
        restored += 1
        # The mount path is what Jellyfin stores, not the SSD path.
        mount = str(Path.home() / "MediaLibrary" / "Shows" / rel)
        for ext in (".mkv", ".mp4", ".avi"):
            item = by_path.get(mount[:-4] + ext)
            if item:
                break
        else:
            print(f"  no Jellyfin item for {rel}")
            continue
        dto = jf(f"Items/{item}", userId=uid())
        if dto is None:
            continue
        dto["Overview"] = plot
        # Unlock it again: the lock was part of the unintended change, and leaving it on
        # would stop any future scrape from ever improving these descriptions.
        dto["LockedFields"] = [x for x in (dto.get("LockedFields") or []) if x != "Overview"]
        if args.apply:
            jf(f"Items/{item}", method="POST", body=dto)
        synced += 1
    print(f"\n{restored} sidecar(s) {'restored' if args.apply else 'would be restored'}, "
          f"{synced} Jellyfin item(s) {'reverted + unlocked' if args.apply else 'would be reverted'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
