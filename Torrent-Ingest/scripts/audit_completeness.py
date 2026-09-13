#!/usr/bin/env python3
"""Is a show actually incomplete? Answer it from a PROVIDER, not from the filenames.

WHY THIS EXISTS
    Three times in one session a naive "gaps in the episode numbering" check reported shows
    as badly incomplete when they were whole, and each wrong answer nearly cost real content:

      * `Demon Slayer` looked like it was missing 9 episodes of Season 2. It is COMPLETE.
        The library files it by ARC -- S02 is the Mugen Train arc (7 episodes), S03 the
        Entertainment District arc, S04 Swordsmith Village, S05 Hashira Training -- while
        the provider merges the first two into one 18-episode season. A per-season
        `1..max(number)` range is meaningless against that layout.
      * `CatDog`, `Chowder`, `Codename - Kids Next Door` and `Phineas and Ferb` looked
        ~50% missing. Every one is COMPLETE: their files are `SxxEyy-Ezz` MULTI-EPISODE
        pairs, and reading only the first number counts one episode where the file holds
        two.
      * `Dr. STONE` looked like it was missing 36 episodes of "Season 5". Dr. Stone HAS no
        season 5 -- a single mis-filed duplicate created a phantom season, and the show is
        complete at 94.

    The lesson each time: **the filename numbering is a claim about layout, not a fact about
    coverage.** The only sound comparison is our episode COUNT against the provider's, plus
    a title check for the shows where we hold titles.

WHAT IT DOES
    For each show: count real episode files (expanding `E01-E03` ranges, excluding Season 00
    specials), look the show up on TVMaze, and compare against the provider's episode count.
    A show holding at least as many episodes as the provider lists is COMPLETE regardless of
    how its seasons are cut. Only a genuine shortfall is reported, with the count.

    Blocklisted titles are skipped: they are deliberately purged, and reporting them as
    "incomplete" every run is exactly the noise that makes an audit ignored.

    python3 scripts/audit_completeness.py            # every show
    python3 scripts/audit_completeness.py --show X   # one show
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

MOUNT = Path("/Users/mikeyferguson/MediaLibrary/Shows")
BLOCKLIST = Path("/Users/mikeyferguson/Developer/Torrent-Ingest/state/blocklist.json")
VID = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm"}
# `S01E01-E03` / `S01E01E02` / `S01E01`. The optional tail is what makes a multi-episode file
# count as the several episodes it actually contains.
EP_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})(?:\s*-\s*[Ee]?(\d{1,4})|[Ee](\d{1,4}))?")


def _norm(s: str) -> str:
    s = re.sub(r"\((?:19|20)\d{2}\)", " ", s or "")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _blocked() -> set[str]:
    try:
        raw = json.loads(BLOCKLIST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    names = raw.get("titles", []) if isinstance(raw, dict) else raw
    return {k for k in (_norm(n) for n in names if isinstance(n, str)) if k}


def _is_blocked(name: str, blocked: set[str]) -> bool:
    k = _norm(name)
    return bool(k) and any(k == b or k.startswith(b + " ") or f" {b} " in f" {k} "
                           for b in blocked)


def our_episode_count(d: Path) -> int:
    """Distinct (season, episode) pairs we hold, Season 00 excluded, ranges expanded."""
    seen: set[tuple[int, int]] = set()
    for p in d.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in VID:
            continue
        m = EP_RE.search(p.name)
        if not m:
            continue
        season = int(m.group(1))
        if season == 0:                       # specials are not part of the run
            continue
        first = int(m.group(2))
        last = int(m.group(3) or m.group(4) or first)
        for n in range(first, max(first, last) + 1):
            seen.add((season, n))
    return len(seen)


def provider_count(query: str, year: str | None) -> tuple[str, int] | None:
    """(provider name, episode count excluding specials), or None."""
    try:
        url = "https://api.tvmaze.com/search/shows?q=" + urllib.parse.quote(query)
        with urllib.request.urlopen(url, timeout=30) as r:
            res = json.load(r)
        if not res:
            return None
        shows = [s["show"] for s in res]
        if year:
            same = [s for s in shows if (s.get("premiered") or "").startswith(year)]
            if same:
                shows = same
        sh = shows[0]
        with urllib.request.urlopen(f"https://api.tvmaze.com/shows/{sh['id']}/episodes",
                                    timeout=30) as r:
            eps = json.load(r)
        return sh["name"], len([e for e in eps if (e.get("season") or 0) > 0])
    except Exception:                                                  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", help="audit one show folder")
    args = ap.parse_args()

    blocked = _blocked()
    dirs = sorted(d for d in MOUNT.iterdir() if d.is_dir())
    if args.show:
        dirs = [d for d in dirs if args.show.lower() in d.name.lower()]

    short, complete, unknown, skipped = [], 0, [], 0
    for d in dirs:
        if _is_blocked(d.name, blocked):
            skipped += 1
            continue
        ours = our_episode_count(d)
        if not ours:
            continue
        m = re.search(r"\((\d{4})\)\s*$", d.name)
        query = re.sub(r"\s*\(\d{4}\)\s*$", "", d.name)
        info = provider_count(query, m.group(1) if m else None)
        time.sleep(0.4)                       # TVMaze asks for a gentle rate
        if not info:
            unknown.append((d.name, ours))
            continue
        pname, pcount = info
        if ours >= pcount:
            complete += 1
        else:
            short.append((pcount - ours, ours, pcount, d.name, pname))

    short.sort(reverse=True)
    print(f"\nCOMPLETE: {complete}    short: {len(short)}    "
          f"provider unknown: {len(unknown)}    blocklisted (skipped): {skipped}\n")
    if short:
        print(f"{'missing':>7}  {'ours':>5}/{'provider':<8}  show")
        for miss, ours, pc, name, pname in short:
            print(f"{miss:7d}  {ours:5d}/{pc:<8d}  {name}   [{pname}]")
    if unknown:
        print(f"\nno provider match (audit by hand, do NOT assume incomplete):")
        for name, ours in unknown:
            print(f"   {ours:5d} episodes  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
