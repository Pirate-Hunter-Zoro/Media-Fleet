#!/usr/bin/env python3
"""Did the coordinates we chose resolve to the episode we MEANT?

"The numbering resolves" and "it resolves to the right episode" are different claims, and
only the second one matters. A season boundary that is off by one ARC still resolves
perfectly: every episode gets a real title and a full plot, they are simply the wrong arc's,
and nothing downstream can tell. Jellyfin is happy. The owner is not.

Measured 2026-09-10 on `[MTBB] Monogatari Series (BD 1080p)`: Bakemonogatari -> Season 01
(15 eps) and Nisemonogatari -> Season 02 (11 eps) were exactly right, and the four
`04 - Nekomonogatari (Black)` files went to Season 03 -- where TheTVDB keeps
*Nekomonogatari (White) / Tsubasa Tiger*. Those episodes scraped clean titles and full
plots belonging to a different arc entirely.

WHAT THIS CAN AND CANNOT DO. It cannot decide the answer: an anime release filename
(`[MTBB] Nekomonogatari (Black) - 01v2`) and a provider episode title (`Tsubasa Tiger (1)`)
never match textually, so no string comparison settles it. What it CAN do is put the two
side by side -- the SOURCE ARC each file came from, against the title and synopsis the
provider actually returned for the coordinate it was filed at -- so a mismatch is obvious
to a person in one glance instead of invisible forever.

    python3 scripts/verify_arc_mapping.py --show "Monogatari Series (2009)"
    python3 scripts/verify_arc_mapping.py --hash <info_hash>

Read-only: reads the ingest journal and queries Jellyfin. Writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402

JOURNAL = Path.home() / "Developer" / "Torrent-Ingest" / "state" / "journal.jsonl"


def _jf(path: str, params: dict) -> dict:
    base = (config.JELLYFIN_URL or "").rstrip("/")
    key = config.JELLYFIN_API_KEY or ""
    if not base or not key:
        raise RuntimeError("JELLYFIN_URL / JELLYFIN_API_KEY are not set")
    params = dict(params, api_key=key)
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=240) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _torrent_file_names(torrent_path: str) -> list:
    """The release's file paths, in index order, straight from the `.torrent`."""
    def dec(b, i=0):
        c = b[i:i + 1]
        if c == b"d":
            i += 1; d = {}
            while b[i:i + 1] != b"e":
                k, i = dec(b, i); v, i = dec(b, i); d[k] = v
            return d, i + 1
        if c == b"l":
            i += 1; out = []
            while b[i:i + 1] != b"e":
                v, i = dec(b, i); out.append(v)
            return out, i + 1
        if c == b"i":
            j = b.index(b"e", i); return int(b[i + 1:j]), j + 1
        j = b.index(b":", i); n = int(b[i:j]); return b[j + 1:j + 1 + n], j + 1 + n
    try:
        meta, _ = dec(Path(torrent_path).read_bytes())
    except Exception:                                                # noqa: BLE001
        return []
    files = (meta.get(b"info") or {}).get(b"files") or []
    return ["/".join(x.decode("utf-8", "replace") for x in f[b"path"]) for f in files]


def filed_sources(show: str | None, info_hash: str | None) -> dict:
    """(season, episode) -> source path, from what the journal recorded as filed.

    Two record shapes, because a chunked pack is filed differently from a whole torrent:
    a normal plan carries `files[].src`, while a chunked record carries `chunk_filed` as
    {torrent file INDEX -> destination}. The index is resolved against the `.torrent`'s own
    file list, which is the only place the source name still exists once the wave's bytes
    have been filed and unlinked.
    """
    import re
    out: dict = {}
    if not JOURNAL.exists():
        return out

    # LATEST RECORD PER TORRENT ONLY. The journal is append-only, so a torrent that was
    # filed, purged and re-filed has several records -- and the older ones describe a
    # placement that no longer exists. Reading them all made this tool report seasons that
    # are not on disk, mixing a previous run's mistakes into a report about this one.
    latest: dict = {}
    for line in JOURNAL.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:                                            # noqa: BLE001
            continue
        h = rec.get("info_hash")
        if not h or (info_hash and h != info_hash):
            continue
        latest[h] = rec

    for rec in latest.values():

        def keep(dst_rel, src):
            parts = str(dst_rel).split("/")
            if len(parts) < 4 or parts[0] != "Shows":
                return
            if show and parts[1] != show:
                return
            m = re.search(r"S(\d{1,3})E(\d{1,4})", parts[-1])
            if m:
                out[(int(m.group(1)), int(m.group(2)))] = str(src or "")

        for f in (rec.get("plan") or {}).get("files") or []:
            keep(f.get("dst_rel"), f.get("src"))

        cf = rec.get("chunk_filed") or {}
        if cf:
            names = _torrent_file_names(rec.get("torrent_path") or "")
            for idx, dst_rel in cf.items():
                try:
                    src = names[int(idx)]
                except (ValueError, IndexError):
                    src = f"<torrent index {idx}>"
                keep(dst_rel, src)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", help="library show folder name")
    ap.add_argument("--hash", dest="info_hash", help="limit to one torrent")
    args = ap.parse_args()
    if not args.show:
        print("--show is required"); return 2

    sources = filed_sources(args.show, args.info_hash)
    if not sources:
        print(f"no journal record of files filed under {args.show!r}")
        return 1

    # Search on the FIRST WORD and match the full name ourselves. Jellyfin's SearchTerm is
    # fuzzy in ways that vary by version, and a multi-word term has silently returned
    # nothing for a series a one-word term finds immediately.
    term = args.show.split(" (")[0].split()[0]
    series = _jf("/Items", {"Recursive": "true", "IncludeItemTypes": "Series",
                            "SearchTerm": term})
    items = series.get("Items", [])
    match = next((i for i in items if i["Name"] == args.show), None)
    if not match:
        match = next((i for i in items
                      if i["Name"].startswith(args.show.split(" (")[0])), None)
    if not match:
        print(f"Jellyfin has no series named {args.show!r} yet (has it scanned?)")
        return 1

    eps = _jf("/Items", {"ParentId": match["Id"], "Recursive": "true",
                         "IncludeItemTypes": "Episode", "Fields": "Overview"})
    by_slot = {(e.get("ParentIndexNumber") or 0, e.get("IndexNumber") or 0): e
               for e in eps.get("Items", [])}

    # EVERY file per season, not a sample. The single-sample version of this tool reported
    # Season 01 as coming from `09 - Onimonogatari` when all 15 of its files came from
    # `01 - Bakemonogatari` (HANDOFF §6) -- one sample cannot see a season that MIXES arcs,
    # and a mixed season is the whole failure this is here to catch.
    per_season: dict = defaultdict(list)
    for (s, e), src in sources.items():
        per_season[s].append((e, src))

    def arc_of(src: str) -> str:
        return Path(src).parts[0] if "/" in src else Path(src).stem

    print(f"=== {args.show} — source arc vs. what the provider returned ===\n")
    unchecked = 0
    mixed: list = []
    for s in sorted(per_season):
        files = sorted(per_season[s])
        arcs: dict = defaultdict(list)
        for e, src in files:
            arcs[arc_of(src)].append(e)

        print(f"  Season {s:02d}  ({len(files)} file(s) filed, {len(arcs)} source arc(s))")
        for arc in sorted(arcs, key=lambda a: min(arcs[a])):
            eps = sorted(arcs[arc])
            span = f"E{eps[0]:02d}" if len(eps) == 1 else f"E{eps[0]:02d}-E{eps[-1]:02d}"
            print(f"     {len(eps):4d} file(s)  {span:<12} <- {arc}")
        if len(arcs) > 1:
            mixed.append(s)

        # Show the provider's own words for the FIRST episode of EACH arc in the season,
        # so a mixed season exposes every arc's titles rather than only the first one's.
        for arc in sorted(arcs, key=lambda a: min(arcs[a])):
            first_e = min(arcs[arc])
            jf = by_slot.get((s, first_e))
            if not jf:
                unchecked += 1
                continue
            title = jf.get("Name") or "(untitled)"
            plot = (jf.get("Overview") or "").strip().replace("\n", " ")
            print(f"       {arc}")
            print(f"         provider S{s:02d}E{first_e:02d}: {title!r}")
            if plot:
                print(f"         synopsis: {plot[:150]}")
        print("     -> do the arc names and the provider titles describe the SAME story?")
        print()

    if mixed:
        print(f"HINT: season(s) {', '.join(f'{s:02d}' for s in mixed)} draw on MORE THAN ONE "
              f"source arc.")
        print("      That is not automatically wrong -- TVDB's 'Monogatari Series Second")
        print("      Season' is one season holding six arcs. It is wrong when the arcs do")
        print("      not belong together. `audit_arc_placement.py` is the pass/fail census;")
        print("      this tool shows you the provider's words beside them.")
        print()
    if unchecked:
        print(f"NOTE: {unchecked} arc(s) are not in Jellyfin yet; re-run after a scan.")
    print("This tool cannot decide for you: an anime release filename and a provider")
    print("episode title never match textually. It puts them side by side so a wrong")
    print("boundary is visible in one glance instead of invisible forever.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
