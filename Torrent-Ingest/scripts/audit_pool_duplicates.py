#!/usr/bin/env python3
"""Episodes the pool holds MORE THAN ONCE, split by how certain the duplication is.

Found by accident on 2026-09-06: the pool held every Hunter x Hunter (1999) special TWICE,
byte-identical, under two naming conventions -- one set filed with .nfo and thumbnails, the
other left over from an earlier release. A purge sweep had correctly queued the redundant
copies, and rescuing them (mistaking a de-duplication for a §4.16 mis-queue) would have left
permanent duplicates AND shown Jellyfin two of every special after the next mediafs restart.

That prompted this sweep. It reports two classes and never merges them, because they need
opposite judgements:

  A. BYTE-IDENTICAL -- same size for the same (title, season, episode) under different
     filenames. One file, two names. Retiring the copy the library does not present is safe,
     and §7.1 is how (or leave it: a purge sweep usually queues them already).

  B. DIFFERENT SIZES -- different encodes of the same episode. NOT waste by itself. The
     searcher has an upgrade path, so a second encode may be a deliberate better copy that
     simply never displaced the first. This is a REVIEW list, in the sense of §4.8: a row
     here is a question, not a fault.

Measured 2026-09-06: 94 duplicated episodes over 9 titles, 19.7 GB redundant -- 7.1 GB
byte-identical (4.3 of it the HxH specials, already queued) and 12.6 GB different encodes,
of which The Powerpuff Girls (1998) is 42 episodes and 7.6 GB.

READ THE FLAGS BEFORE ACTING. A title mid-ingest shows duplicates that are simply in flight
(The Office did), and a title whose copies are BOTH on the mount is visible to Jellyfin
today, which is a different problem from pool residue. Both are flagged per row.

    python3 scripts/audit_pool_duplicates.py [--all]

Read-only: reads remote_inventory.json, the mount and the deletion queues. Writes nothing.
"""

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

VIDEO = {".mkv", ".mp4", ".avi", ".m4v"}
MOUNT = Path(os.path.expanduser("~/MediaLibrary"))
QUEUE = Path(os.path.expanduser("~/Developer/Media-Syncer/mediafs_deletions.jsonl"))


def _queued() -> set:
    out = set()
    for p in (QUEUE, QUEUE.with_name(QUEUE.name + ".processing")):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        out.add(json.loads(line)["path"])
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="list every duplicated episode")
    args = ap.parse_args()

    inv_path = Path(os.path.expanduser("~/Developer/Media-Syncer/remote_inventory.json"))
    inv = json.loads(inv_path.read_text(encoding="utf-8"))
    queued = _queued()

    by_ep = collections.defaultdict(list)
    for key in inv:
        if not key.startswith("Shows/") or os.path.splitext(key)[1].lower() not in VIDEO:
            continue
        parts = key.split("/")
        if len(parts) < 4:
            continue
        m = re.search(r"S(\d{1,3})E(\d{1,4})", os.path.basename(key))
        if m:
            by_ep[(parts[1], m.group(1), m.group(2))].append(key)

    ident, diff = collections.defaultdict(list), collections.defaultdict(list)
    for ep, paths in by_ep.items():
        if len(paths) < 2:
            continue
        (ident if len({inv[p][2] for p in paths}) == 1 else diff)[ep[0]].append((ep, paths))

    def waste(groups):
        return sum(sum(sorted(inv[p][2] for p in ps)[:-1]) for _e, ps in groups)

    def render(title, buckets, note):
        total = sum(waste(g) for g in buckets.values())
        n = sum(len(g) for g in buckets.values())
        print(f"\n--- {title}: {n} episode(s), {total / 1e9:.1f} GB redundant ---")
        print(f"    {note}")
        if not buckets:
            print("    (none)")
            return
        for name, groups in sorted(buckets.items(), key=lambda kv: -waste(kv[1])):
            held = (MOUNT / "Shows" / name).is_dir()
            allq = all(p in queued for _e, ps in groups for p in ps)
            anyq = any(p in queued for _e, ps in groups for p in ps)
            flags = []
            flags.append("held" if held else "PURGED")
            if allq:
                flags.append("all queued — the drain handles it")
            elif anyq:
                flags.append("PARTLY queued")
            onmount = sum(1 for _e, ps in groups for p in ps if (MOUNT / p).exists())
            if onmount > len(groups):
                flags.append(f"{onmount} copies VISIBLE on the mount")
            print(f"    {len(groups):>4} ep  {waste(groups) / 1e9:>6.1f} GB  "
                  f"{name[:42]:<42} [{', '.join(flags)}]")
            if args.all:
                for _e, ps in sorted(groups):
                    for p in sorted(ps):
                        mark = "q" if p in queued else " "
                        print(f"           {mark} {inv[p][2] / 1e6:>8.1f} MB  {os.path.basename(p)}")

    print("=" * 78)
    print("POOL DUPLICATES — the same episode stored more than once")
    print("=" * 78)
    render("A. BYTE-IDENTICAL (one file, two names)", ident,
           "Safe to retire the copy the library does not present (§7.1).")
    render("B. DIFFERENT SIZES (different encodes)", diff,
           "A REVIEW list, not a fault list — a second encode may be a deliberate upgrade.")
    print("\nNothing was changed. A title mid-ingest shows duplicates that are in flight.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
