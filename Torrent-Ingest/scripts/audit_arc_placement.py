#!/usr/bin/env python3
"""Which SOURCE ARC did every filed episode actually come from? Read-only.

THE POINT. `verify_arc_mapping.py` shows the provider's own titles and synopses beside
the arcs a season drew on, so a human can eyeball whether they describe the same story.
It USED to sample one file per season, and on Monogatari (2026-09-12) that single sample
said Season 01 came from `09 - Onimonogatari` when all 15 of Season 01's files came from
`01 - Bakemonogatari`; it now censuses every file, so the two tools can no longer
contradict each other. They still answer different questions: that one reads the JOURNAL
and asks "do these read like the same arc?", this one matches bytes ON DISK back to the
`.torrent` and answers pass/fail. Where they disagree, believe this one -- the journal
describes what was filed, and only a size census sees what survived.

This is the census. It matches EVERY filed episode back to its source path **by file
size against the .torrent's own file list**, because:

  * library filenames carry no arc (they are `Show - SxxEyy.mkv` by the time they land);
  * `state/decisions.log` is cumulative across every historical run of every release, so
    it cannot tell you what is on disk NOW; and
  * the journal keeps only the LAST record per info-hash, so completed waves are gone.

File size is effectively unique inside one release, and it survives the rename. Where two
files share a size the entry is reported as AMBIGUOUS rather than guessed.

WHAT A CLEAN RESULT LOOKS LIKE depends on the show, and this tool deliberately does not
decide for you:

  * Some shows map one arc to one season (`03 - Nisemonogatari` -> Season 02).
  * Some legitimately map SEVERAL arcs into one provider season -- TVDB's "Monogatari
    Series Second Season" is one season holding six arcs.

So a season drawing on several arcs is not automatically wrong. What IS wrong is a season
drawing on arcs that do not belong together, and the way you see that here is that the
arcs in a season are not consecutive in the release's own numbering. The tool flags that
as a hint (`arcs are not consecutive`) and says so is a hint, not a verdict.

    python3 scripts/audit_arc_placement.py --show "Monogatari Series (2009)" \\
        --hash ff13439e7e644541b0434527cb379b5bfadb27e8

`--torrent PATH` takes a .torrent directly. With neither, it searches
`state/torrent_sources/` and the iCloud Torrents folder for a matching hash.
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                          # noqa: E402
import qbt                                                             # noqa: E402

ARC_RE = re.compile(r"^(\d+)\s*-\s*(.+)$")


def _torrent_files(torrent_path):
    """[(path_inside_release, size)] for every file in a .torrent."""
    meta, _ = qbt._bdecode(Path(torrent_path).read_bytes(), 0)
    info = meta[b"info"]
    name = info[b"name"].decode("utf-8", "replace")
    out = []
    if b"files" in info:
        for f in info[b"files"]:
            rel = "/".join(p.decode("utf-8", "replace") for p in f[b"path"])
            out.append((rel, int(f[b"length"])))
    else:
        out.append((name, int(info[b"length"])))
    return out


def _find_torrent(info_hash):
    cands = [config.STATE_DIR / "torrent_sources" / f"{info_hash}.torrent"]
    icloud = Path.home() / ("Library/Mobile Documents/com~apple~CloudDocs/Torrents")
    if icloud.is_dir():
        cands += sorted(icloud.rglob("*.torrent"))
    for c in cands:
        try:
            if c.is_file() and qbt.info_hash_from_file(c).lower() == info_hash.lower():
                return c
        except Exception:                                             # noqa: BLE001
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", required=True, help="library show folder name")
    ap.add_argument("--hash", dest="info_hash", help="info-hash of the source torrent")
    ap.add_argument("--torrent", help="path to the .torrent (skips the hash lookup)")
    args = ap.parse_args()

    tp = Path(args.torrent) if args.torrent else (
        _find_torrent(args.info_hash) if args.info_hash else None)
    if not tp or not tp.is_file():
        sys.exit("could not locate the .torrent (pass --torrent PATH)")

    entries = _torrent_files(tp)
    by_size = collections.defaultdict(list)
    for rel, size in entries:
        by_size[size].append(rel)

    show_dir = Path.home() / "MediaLibrary" / "Shows" / args.show
    if not show_dir.is_dir():
        sys.exit(f"show folder not found: {show_dir}")

    print(f"release : {tp.name}  ({len(entries)} files)")
    print(f"show    : {show_dir}")

    dist = collections.defaultdict(collections.Counter)
    unmatched, ambiguous = [], []
    for p in sorted(show_dir.rglob("*")):
        if p.suffix.lower() not in config.VIDEO_EXTENSIONS or not p.is_file():
            continue
        season = p.parent.name
        cands = by_size.get(p.stat().st_size)
        if not cands:
            unmatched.append(p.name)
            dist[season]["(not from this release)"] += 1
            continue
        arcs = {c.split("/")[0] if "/" in c else "(release root)" for c in cands}
        if len(arcs) > 1:
            ambiguous.append((p.name, sorted(arcs)))
            dist[season]["(AMBIGUOUS: same size in 2+ arcs)"] += 1
            continue
        dist[season][arcs.pop()] += 1

    mixed = 0
    for season in sorted(dist):
        arcs = dist[season]
        total = sum(arcs.values())
        real = [a for a in arcs if not a.startswith("(")]
        nums = sorted(int(m.group(1)) for a in real
                      if (m := ARC_RE.match(a)) is not None)
        consecutive = (len(nums) <= 1
                       or nums == list(range(min(nums), min(nums) + len(nums))))
        flag = ""
        if len(real) > 1:
            flag = ("  <- several arcs, consecutive in the release"
                    if consecutive else
                    "  <- several arcs, NOT consecutive in the release")
            if not consecutive:
                mixed += 1
        print(f"\n{season}  ({total} file(s)){flag}")
        for arc, n in arcs.most_common():
            print(f"   {n:3d}  <- {arc}")

    print()
    if unmatched:
        print(f"{len(unmatched)} file(s) in this show did NOT come from this release "
              f"(other releases file into the same show; not an error): "
              f"{unmatched[:3]}{' ...' if len(unmatched) > 3 else ''}")
    if ambiguous:
        print(f"{len(ambiguous)} file(s) AMBIGUOUS (same byte size in 2+ arcs); "
              f"not guessed: {ambiguous[:3]}")
    status = _verdict(dist, entries, args.show)
    if mixed and status in ("FAIL", "UNAVAILABLE"):
        print(f"\n{mixed} season(s) draw on NON-CONSECUTIVE arcs -- worth looking at, "
              f"given the verdict above. Note it is only a HINT: a correct season can be "
              f"non-consecutive (this pack's Season 03 is folders 05, 06, 08, 09, 10, "
              f"skipping 07 because Hanamonogatari aired a year later).")
    elif mixed:
        print(f"\n({mixed} season(s) draw on non-consecutive arcs. That is expected here "
              f"and the verdict above accounts for it -- do not re-file on it alone.)")
    else:
        print("\nNo season draws on non-consecutive arcs.")
    return 0 if status in ("PASS", "CORRECT-SO-FAR") else 1


def _verdict(dist, entries, show):
    """PASS/FAIL against `arcmap`'s computed mapping -- the thing the hint cannot judge.

    WHY THIS REPLACED EYEBALLING. The consecutive-arcs hint above cries wolf on the CORRECT
    answer for this very pack: Monogatari's Season 03 is release folders 05, 06, 08, 09 and
    10, skipping 07 because Hanamonogatari aired a year later and belongs in specials. A
    reader who trusts the headline re-files a season that was right.

    So the verdict is computed the only way that means anything: rebuild the harness's own
    arc->season mapping from the `.torrent` and the provider, and compare it to what is
    actually on disk, season by season.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import arcmap
        import epguide
        series = re.sub(r"\s*\(\d{4}\)\s*$", "", show).strip()
        shape = epguide.season_shape(series)
        proposal = arcmap.propose([rel for rel, _sz in entries], shape) if shape else None
    except Exception as exc:                                          # noqa: BLE001
        print(f"\nverdict: UNAVAILABLE ({type(exc).__name__}: {exc}) -- judge by hand "
              f"against HANDOFF §1's table.")
        return "UNAVAILABLE"
    if proposal is None or not proposal.settled:
        print("\nverdict: UNAVAILABLE -- the harness has no settled mapping for this "
              "release, so there is nothing to compare against. Judge by hand.")
        return "UNAVAILABLE"

    want = {s: sorted({f for u in us for f in u.folders})
            for s, us in proposal.mapping.items()}
    print("\n--- verdict: on-disk placement vs the harness's computed mapping ---")
    bad = pending = 0
    for season in sorted(want):
        key = f"Season {season:02d}"
        got = {a for a in dist.get(key, {}) if not a.startswith("(")}
        label = "/".join(u.label for u in proposal.mapping[season])[:42]
        if not got:
            print(f"  S{season:02d}  {label:44s} not filed yet")
            pending += 1
        elif got == set(want[season]):
            print(f"  S{season:02d}  {label:44s} CORRECT")
        else:
            extra = sorted(got - set(want[season]))
            print(f"  S{season:02d}  {label:44s} WRONG -- also holds {extra}")
            bad += 1
    # Season 00 is the leftovers, and it is legitimate for several arcs to share it.
    s00 = {a for a in dist.get("Season 00", {}) if not a.startswith("(")}
    leftover = {f for u in proposal.leftover for f in u.folders}
    if s00:
        stray = sorted(s00 - leftover)
        print(f"  S00  {'specials (leftover arcs)':44s} "
              + ("CORRECT" if not stray else f"WRONG -- holds {stray}"))
        bad += bool(stray)

    if bad:
        print(f"\nVERDICT: FAIL -- {bad} season(s) do not match the computed mapping. "
              f"HANDOFF §3 says purge the show and restart from zero; do not patch.")
        return "FAIL"
    elif pending:
        print(f"\nVERDICT: CORRECT SO FAR -- every filed season matches; {pending} "
              f"season(s) still to arrive. Not yet a pass.")
        return "CORRECT-SO-FAR"
    else:
        print("\nVERDICT: PASS -- every season matches the computed mapping.")
        return "PASS"


if __name__ == "__main__":
    main()
