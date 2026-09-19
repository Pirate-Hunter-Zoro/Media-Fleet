#!/usr/bin/env python3
"""Blocklist entries that match a title the library STILL HOLDS. Read-only.

§8.1 step 5 has always said to add a purged title to the blocklist "with its ROMAJI form,
never as a bare franchise word, and audit against titles HOLDING MEDIA." Nothing
implemented that audit, and the cost showed up on 2026-09-05:

    Ranma ½ (1989)        173 episodes on the mount, invisible in Jellyfin
    Sailor Moon (1992)    203 episodes on the mount, invisible in Jellyfin
    Rurouni Kenshin (1996) 103 episodes on the mount, invisible in Jellyfin

All three are fully intact and none is queued for deletion. What was purged was the
REMAKE -- `Ranma ½ (2024)`, `Sailor Moon Crystal (2014)`, `Rurouni Kenshin (2023)` -- but
the blocklist also carries the bare franchise words `Ranma ½`, `Sailor Moon` and
`Rurouni Kenshin`, and `purge_sweeper._mark_blocked_pending` matches on a normalized prefix.
So it wrote a `.ignore` marker into each ORIGINAL series' folder, Jellyfin stopped indexing
them, and 479 episodes the owner owns quietly left the app. §4.154 explicitly lists Ranma ½
among the works KEPT ON JUDGEMENT, so at least one of these is a straightforward mistake.

The failure is silent in BOTH directions, which is why it needs a report of its own: the
blocklist looks correct (those titles really were purged), the library looks correct (the
files are all there), the reaper looks correct (it never queued them), and Jellyfin looks
correct (it is honouring a marker it was handed). Only the JOIN is wrong.

DELIBERATELY REPORT-ONLY. Whether a held title should be un-blocked is a content decision
about what the owner wants to keep, and §4.167 is the standing reminder of what happens
when a cleanup tool makes that call itself. The remedy, when the owner chooses it, is:
narrow or remove the blocklist entry, delete the `.ignore` marker, and rescan Jellyfin.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                       # noqa: E402

MOUNT = config.MEDIAFS_MOUNT
LOCAL = config.MEDIA_ROOT
BLOCKLIST = config.STATE_DIR / "blocklist.json"
DELETIONS = config.MEDIA_SYNCER_DIR / "mediafs_deletions.jsonl"
MEDIA_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm", ".cbz", ".cbr",
             ".pdf", ".epub"}
ROOTS = ["Shows", "Movies", "Comics/Manga", "Comics"]


def _norm(s: str) -> str:
    s = re.sub(r"\((?:19|20)\d{2}\)", " ", s or "")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _blocked():
    try:
        raw = json.loads(BLOCKLIST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    names = raw.get("titles", []) if isinstance(raw, dict) else raw
    return {n: _norm(n) for n in names if isinstance(n, str) and _norm(n)}


def _matches(title_norm: str, blocked_norm: str) -> bool:
    """The SAME rule `purge_sweeper._is_blocked` applies, so this reports what that does."""
    return (title_norm == blocked_norm
            or title_norm.startswith(blocked_norm + " ")
            or f" {blocked_norm} " in f" {title_norm} ")


def _media_count(d: Path) -> int:
    n = 0
    try:
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if not x.startswith(".")]
            n += sum(1 for f in filenames
                     if not f.startswith(".")
                     and os.path.splitext(f)[1].lower() in MEDIA_EXT)
    except OSError:
        return -1                       # unreadable is not empty; flag it, do not count it
    return n


def _queued_for_deletion():
    """Top-level title directories that already have pool deletions queued."""
    out = set()
    try:
        with DELETIONS.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    p = json.loads(line).get("path") or ""
                except ValueError:
                    continue
                parts = Path(p).parts
                if len(parts) >= 2:
                    out.add("/".join(parts[:3] if parts[0] == "Comics" else parts[:2]))
    except OSError:
        pass
    return out


def main() -> int:
    if not MOUNT.is_dir() or not any((MOUNT / "Shows").iterdir()):
        print("the mediafs mount is absent or empty; refusing to judge anything.")
        return 1
    blocked = _blocked()
    if not blocked:
        print(f"cannot read {BLOCKLIST}; nothing to audit.")
        return 1
    queued = _queued_for_deletion()

    findings = []
    for root in ROOTS:
        base = MOUNT / root
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if root == "Comics" and d.name == "Manga":
                continue
            tnorm = _norm(d.name)
            hits = [orig for orig, bn in blocked.items() if _matches(tnorm, bn)]
            if not hits:
                continue
            n = _media_count(d)
            if n <= 0:
                continue                       # already drained, or unreadable: not this bug
            rel = f"{root}/{d.name}"
            findings.append((rel, n, hits, rel in queued,
                             (LOCAL / root / d.name / ".ignore").exists()))

    print(f"blocklist entries: {len(blocked)}   titles still HOLDING MEDIA that they "
          f"match: {len(findings)}\n")
    if not findings:
        print("No collision. Every blocklisted title is drained or gone.")
        return 0

    total = 0
    for rel, n, hits, is_queued, ignored in sorted(findings, key=lambda x: -x[1]):
        total += n
        state = ("queued for deletion -- this is a purge in progress, not a mistake"
                 if is_queued else
                 "NOT queued for deletion -- the media is being KEPT and hidden")
        print(f"  {n:5d} file(s)  {rel}")
        print(f"          matched by: {', '.join(repr(h) for h in hits)}")
        print(f"          .ignore marker present: {ignored}   ({state})")
    keep = sum(n for _r, n, _h, q, _i in findings if not q)
    print(f"\n{total} media file(s) matched, {keep} of them in titles with NO pool deletion "
          f"queued.")
    if keep:
        print("\nThose are the ones to look at. A blocklist entry that is a bare FRANCHISE "
              "word matches the original series as well as the remake that was purged, and "
              "purge_sweeper then writes a .ignore into the original's folder -- so Jellyfin "
              "stops indexing content that is intact and wanted. Narrow the entry (add the "
              "year), delete the .ignore, and rescan Jellyfin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
