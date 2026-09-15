#!/usr/bin/env python3
"""Read-only census of the manga shelf: volumes, mapped ranges, chapters, leftovers.

This is the tool to run before `chapter_volume_reconcile.py --apply` ever touches the
shelf, and after, to see zero leftovers. It walks the same enumeration and the same
`plan_decisions` the reconciler uses, so the numbers cannot disagree with what an apply
would do -- a census with its own copy of the rules would be exactly the
`verify_arc_mapping.py` mistake (a tool whose output read like a verdict while measuring
the wrong thing).

Columns, per series that holds both tiers:
    vols    owned volume files
    mapped  owned volumes the cached provider map places
    chaps   owned chapter files
    cover   chapters a mapped, present volume covers (what --apply would remove)
    left    the census's headline: covered chapters STILL on the shelf
    keep    chapters kept, with the reason breakdown under --verbose
    uncov   chapters no mapped owned volume covers (legitimately retained)

Read-only: no network unless `--refresh`, no deletions ever. `--refresh` fetches the
stale/missing maps first (same fail-open path as the reconciler).

USAGE
    python3 scripts/audit_volume_chapter_coverage.py
    python3 scripts/audit_volume_chapter_coverage.py --series "Mashle" --verbose
    python3 scripts/audit_volume_chapter_coverage.py --refresh
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import manga_volume_map as mvm                                        # noqa: E402
import chapter_volume_reconcile as cvr                                # noqa: E402


def census(series: str | None = None, refresh: bool = False, verbose: bool = False) -> dict:
    owned_all = cvr.owned_manga(series=series)
    rows, totals = [], Counter()
    for label, files in sorted(owned_all.items()):
        volumes = {n for _r, (k, n, _c) in files.items() if k == "volume"}
        chapters = {n for _r, (k, n, _c) in files.items() if k == "chapter"}
        if not volumes or not chapters:
            continue
        policy = cvr.policy_for(label)
        entry = mvm.get(label, allow_network=refresh)
        mapped = set()
        unmapped = set()
        if entry:
            for v in volumes:
                if mvm.volume_allowed(entry, v):
                    mapped.add(v)
                else:
                    unmapped.add(v)
        purges, keeps = (cvr.plan_decisions(label, files, entry, policy)
                         if entry else ([], []))
        covered = len(purges)
        uncovered = sorted(n for n in chapters
                           if not any(n in (mvm.known_volume(entry, v) or [])
                                      for v in mapped))
        rows.append({
            "series": label,
            "policy": policy,
            "owned_volumes": len(volumes),
            "mapped_volumes": len(mapped),
            "unmapped_volumes": sorted(unmapped),
            "owned_chapters": len(chapters),
            "covered_chapters": covered,
            "leftovers": covered,
            "kept": len(keeps),
            "uncovered_chapters": uncovered,
        })
        totals["series"] += 1
        totals["volumes"] += len(volumes)
        totals["mapped"] += len(mapped)
        totals["chapters"] += len(chapters)
        totals["covered"] += covered
        totals["leftovers"] += covered
        if verbose:
            print(f"  {label} [policy={policy}] "
                  f"vols={len(volumes)} mapped={len(mapped)} unmapped={sorted(unmapped)} "
                  f"chaps={len(chapters)} covered={covered} uncovered={uncovered}")
            reasons = Counter(r for _rel, r in keeps)
            for reason, n in reasons.most_common():
                print(f"      keep x{n}: {reason}")
    if not verbose:
        print(f"{'series':<34} {'pol':<13} {'vols':>4} {'map':>4} {'chaps':>5} "
              f"{'cover':>5} {'left':>4} {'uncov':>5}")
        for r in rows:
            print(f"{r['series'][:33]:<34} {r['policy']:<13} {r['owned_volumes']:>4} "
                  f"{r['mapped_volumes']:>4} {r['owned_chapters']:>5} "
                  f"{r['covered_chapters']:>5} {r['leftovers']:>4} "
                  f"{len(r['uncovered_chapters']):>5}")
    return {"rows": rows, "totals": dict(totals)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Manga volume/chapter coverage census.")
    ap.add_argument("--series", help="one series folder name")
    ap.add_argument("--refresh", action="store_true", help="refresh stale/missing maps first")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.refresh:
        cvr.refresh_stale(allow_ai=True)
    res = census(series=args.series, refresh=False, verbose=args.verbose)
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        t = res["totals"]
        print(f"\nTOTAL: {t.get('series', 0)} series | {t.get('volumes', 0)} volumes "
              f"({t.get('mapped', 0)} mapped) | {t.get('chapters', 0)} chapters | "
              f"{t.get('leftovers', 0)} leftover(s) covered by a present volume")
        if t.get("leftovers"):
            print("Run `chapter_volume_reconcile.py --apply` after reviewing; "
                  "anything not covered or not mapped is kept either way.")
        else:
            print("Census is clean: no chapter is left behind a volume that covers it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
