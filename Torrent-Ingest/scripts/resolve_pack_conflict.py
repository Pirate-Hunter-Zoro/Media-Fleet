#!/usr/bin/env python3
"""Resolve a blocked pack against a duplicate pack whose library footprint is displaced.

    python3 scripts/resolve_pack_conflict.py --record <info_hash>          # dry run
    python3 scripts/resolve_pack_conflict.py --record <info_hash> --apply  # supersede
    python3 scripts/resolve_pack_conflict.py --scan                        # every record

WHAT THIS IS. The daemon calls the same computation automatically when a chunked wave
parks (`ingest._park_chunked_unfiled` -> `pack_conflict.resolve_parked`). This CLI is the
operator's window into it: `--scan` lists every record that currently has a resolvable
displaced-duplicate conflict, `--record` shows one, and `--apply` performs the supersede
for a record whose park predates the daemon hook (or that the daemon has not reached).

WHAT IT WILL NOT DO. It never guesses between two candidate duplicates, never touches a
pack whose copies are not proven a uniform same-season episode shift, and never supersedes
content the blocked release's own filenames do not name. Read-only unless `--apply`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import journal                                                       # noqa: E402
import pack_conflict                                                 # noqa: E402


def _show(plan):
    if "blocker" not in plan:
        print(f"  no resolution: {plan.get('reason')}")
        return
    blocker = plan["blocker"]
    print(f"  blocked pack  : {blocker.get('name')}  [{plan['blocker_hash'][:12]}]")
    print(f"  show folder   : {plan['folder']}  (guide via {plan.get('provider') or '?'})")
    print(f"  covered slots : {len(plan['content_slots'])} ({_span(plan['content_slots'])})")
    print(f"  would purge   : {len(plan['copies'])} file(s)")
    for c in plan["copies"][:6]:
        print(f"      {c['rel']}")
    if len(plan["copies"]) > 6:
        print(f"      ... and {len(plan['copies']) - 6} more")


def _span(slots):
    if not slots:
        return ""
    seasons = sorted({s for s, _e in slots})
    return f"S{seasons[0]:02d}E{min(e for s, e in slots):02d}..S{seasons[-1]:02d}E{max(e for s, e in slots):02d}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--record", help="info_hash of the blocked pack to resolve")
    ap.add_argument("--apply", action="store_true",
                    help="perform the supersede (default: dry run)")
    ap.add_argument("--scan", action="store_true",
                    help="dry-run every record and list the resolvable ones")
    args = ap.parse_args()
    records = journal.load_records()

    if args.scan:
        found = 0
        for h, rec in sorted(records.items(),
                             key=lambda kv: (kv[1].get("created_at") or "")):
            if rec.get("status") in journal.TERMINAL and not (
                    rec.get("chunk_unfiled") or rec.get("error")):
                continue
            plan = pack_conflict.plan_resolution(rec, records)
            if "blocker" in plan:
                found += 1
                print(f"{h[:12]}  {rec.get('name')}")
                _show(plan)
        print(f"scan: {found} resolvable displaced-duplicate conflict(s)")
        return 0

    if not args.record:
        ap.error("--record is required (or use --scan)")
    rec = records.get(args.record)
    if rec is None:
        print(f"no record for {args.record}")
        return 1
    print(f"{args.record[:12]}  {rec.get('name')}  [{rec.get('status')}]")
    plan = pack_conflict.plan_resolution(rec, records)
    _show(plan)
    if "blocker" not in plan or not args.apply:
        if "blocker" in plan:
            print("  (dry run; pass --apply to supersede)")
        return 0
    import qbt                                                       # noqa: E402
    client = None
    try:
        client = qbt.connect(launch_if_needed=False)
    except Exception as exc:                                         # noqa: BLE001
        print(f"  (qBittorrent unavailable: {exc}; the record will still be retired)")
    summary = pack_conflict.apply_resolution(plan, client)
    print(f"  superseded {len(summary['purged'])} path(s); library.db rows: "
          f"{summary['db'].get('superseded', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
