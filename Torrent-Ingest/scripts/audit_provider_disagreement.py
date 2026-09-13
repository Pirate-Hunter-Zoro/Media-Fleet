#!/usr/bin/env python3
"""Where do TVMaze and TMDB disagree about a season? Read-only. Reports; never decides.

HANDOFF §6 carried this as unfixable: "TVMaze and TMDB disagree about this show, and
Jellyfin uses TMDB... a placement that is perfect against the fleet's own provider will
still show wrong titles in Jellyfin for those two arcs unless the plan is `owned`...
closing it means teaching `epguide` to speak TMDB -- which needs an API key the fleet does
not have."

TWO THINGS IN THAT WERE WRONG, and one was right.

  * The fleet HAS a TMDB key (`~/.config/api-keys/tmdb_api_key`) and `tmdbguide` already
    speaks TMDB -- `season_shape` returns TMDB's own per-season counts AND names. Nothing
    had to be taught.
  * So the disagreement is MEASURABLE, and this prints it.
  * What was right is that it cannot be closed automatically, though not for the stated
    reason. See below -- that finding is the useful part of this file.

WHY THIS IS A REPORT AND NOT A GUARD, measured rather than assumed. It was first written
as a blocking check in `validate_plan` that forced `owned` on any disagreeing season, and
replayed over all 790 historical plans the way §1's arc guards were:

    comparing episode COUNTS         -> 47 of 790 rejected, all false positives. The two
                                        providers routinely group one run differently
                                        (InuYasha S01: TVMaze 27, TMDB 167; Spawn 42 vs 6)
                                        while describing the same story at that slot.
    + requiring a name MISMATCH      -> 15 rejected, still all false positives: TMDB names
                                        most seasons the generic "Season 2", which
                                        tokenizes to {"2"} and matches nothing, ever.
    + requiring a DISTINCTIVE name   -> 2 rejected, and BOTH were still false positives:
                                        TMDB calls Bakugan S04 'Mechtanium Surge' and the
                                        release IS Mechtanium Surge; TMDB calls Cells at
                                        Work S02 'Cells at Work!!' and the release is
                                        'Hataraku Saibou S2'. The same show, in two
                                        languages.

That last one is the wall. The only automatic signal available is token overlap between a
provider's season name and a release's own words, and across romaji/English/alternate
titles -- most of this library -- absence of overlap means nothing. A guard on it would
fail real ingests to catch a case it cannot distinguish, which is precisely the failure
§6 records has already shipped four times.

So this reports, and a person decides. The decision it supports is narrow and cheap: for a
season listed here, file it `owned` so the fleet's own metadata is locked over the scrape.

    python3 scripts/audit_provider_disagreement.py
    python3 scripts/audit_provider_disagreement.py --show "Monogatari Series (2009)"
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import epguide                                                       # noqa: E402
import tmdbguide                                                     # noqa: E402


def pinned_tmdb_id(show_folder: str):
    """The id Jellyfin will actually scrape with, straight from `tvshow.nfo`."""
    try:
        text = (config.SHOWS_ROOT / show_folder / "tvshow.nfo").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"<tmdbid>\s*(\d+)\s*</tmdbid>", text)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", help="limit to one library show folder")
    ap.add_argument("--counts", action="store_true",
                    help="also list count-only differences (noisy; see the docstring)")
    args = ap.parse_args()

    root = config.SHOWS_ROOT
    if not root.is_dir():
        print(f"{root} is not readable")
        return 1

    shows = [args.show] if args.show else sorted(
        p.name for p in root.iterdir() if p.is_dir())

    named = []      # the shape worth acting on: both providers name the slot, differently
    counts = []     # count-only: informational, usually a grouping convention
    no_id = 0
    checked = 0

    for show in shows:
        tid = pinned_tmdb_id(show)
        if not tid:
            no_id += 1
            continue
        try:
            tm = tmdbguide.season_shape(tid)
            tv = epguide.season_shape(show)
        except Exception:                                            # noqa: BLE001
            continue
        if not tm or not tv:
            continue
        checked += 1
        for season in sorted(set(tv) & {k for k in tm if k > 0}):
            tv_count = tv.get(season)
            tm_entry = tm.get(season) or {}
            tm_count = tm_entry.get("count")
            tm_name = (tm_entry.get("name") or "").strip()
            distinctive = {w for w in tmdbguide.re_split(tm_name.lower())
                           if w and w not in tmdbguide._GENERIC and not w.isdigit()}
            if distinctive:
                named.append((show, season, tv_count, tm_count, tm_name))
            elif tv_count and tm_count and tv_count != tm_count:
                counts.append((show, season, tv_count, tm_count))

    print("=== TMDB vs the fleet's guide (TVMaze), per season ===\n")
    print(f"{checked} show(s) compared; {no_id} skipped (no pinned <tmdbid> in tvshow.nfo)\n")

    print(f"-- seasons TMDB gives a DISTINCTIVE NAME ({len(named)}) --")
    print("   These are the ones worth a look: TMDB is asserting what lives at this slot.")
    print("   Most will be right (a real sub-title for a real season). The one that bites")
    print("   is where the name belongs to a DIFFERENT show -- TMDB's Monogatari Season 05")
    print("   is the 2024 OFF & MONSTER Season; the fleet's is Zoku Owarimonogatari.\n")
    for show, season, tvc, tmc, name in named:
        flag = "  <-- counts differ too" if (tvc and tmc and tvc != tmc) else ""
        print(f"   {show}")
        print(f"      S{season:02d}  TVMaze {tvc} ep / TMDB {tmc} ep   TMDB calls it "
              f"{name!r}{flag}")

    if args.counts:
        print(f"\n-- count-only differences ({len(counts)}) --")
        print("   Usually a grouping convention (absolute vs seasonal, OVAs folded in),")
        print("   not a wrong slot. Listed only because you asked.\n")
        for show, season, tvc, tmc in counts:
            print(f"   {show}  S{season:02d}  TVMaze {tvc} / TMDB {tmc}")
    else:
        print(f"\n({len(counts)} count-only difference(s) hidden; --counts to see them. "
              f"They are\n usually grouping conventions and were 47/790 false positives "
              f"as a guard.)")

    print("\nWHAT TO DO WITH A REAL ONE: file that season `owned` with episode_title+plot,")
    print("so apply_plan LOCKS the fleet's own metadata and the TMDB scrape cannot win.")
    print("This tool never decides that for you -- see the module docstring for why.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
