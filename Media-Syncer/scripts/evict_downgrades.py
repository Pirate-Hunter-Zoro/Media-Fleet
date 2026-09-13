"""One-off eviction of SD-downgrade + stale local files (remote copy is better/newer).

§ diagnosis 4.12b. `backfill_replacements.candidates` classifies every size-mismatched
local media file into upgrades (re-upload), downgrades (local SD/compressed, remote HD --
DO NOT re-upload) and stale (local older, remote newer). This script evicts the
`downgrades` and `stale` classes: it deletes the LOCAL copy only, so the better/newer
remote copy streams back through mediafs. It never touches `upgrades` (those are queued
for re-upload by `backfill_replacements --apply`).

Safe by construction: every path it deletes is IN the remote inventory (a remote copy
exists, at a different -- better/newer -- size), so a local delete can never remove the
last copy. Direct filesystem unlink is the same path the tier engine's own eviction uses,
and the reaper only acts on through-the-mount deletes, so nothing propagates outward.

Dry-run by default; pass --apply to delete.
"""

import argparse
import os
import sys
from pathlib import Path

MS = "/Users/mikeyferguson/Developer/Media-Syncer"
sys.path.insert(0, MS)

from scripts import config            # noqa: E402
from scripts.backfill_replacements import candidates, _inventory_nfc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument("--root", default=None)
    args = ap.parse_args()

    root = Path(args.root) if args.root else config.SSD_LIBRARY_ROOT
    inv_nfc = _inventory_nfc()
    upgrades, downgrades, stale = candidates(inv_nfc, root)

    targets = downgrades + stale
    total = sum(r["local_size"] for r in targets)
    print(f"root {root}")
    print(f"upgrades (leave alone; queue via --apply on backfill_replacements): {len(upgrades)}")
    print(f"downgrades (SD/compressed -> evict): {len(downgrades)} "
          f"({sum(r['local_size'] for r in downgrades) / 2**30:.1f} GB)")
    print(f"stale (older local -> evict): {len(stale)} "
          f"({sum(r['local_size'] for r in stale) / 2**30:.1f} GB)")
    print(f"TOTAL to evict: {len(targets)} files ({total / 2**30:.1f} GB)")

    if not args.apply:
        print("DRY RUN -- pass --apply to delete.")
        for r in targets[:40]:
            print(f"  {r['path']}  local={r['local_size']} remote={r['remote_size']}")
        if len(targets) > 40:
            print(f"  ... and {len(targets) - 40} more")
        return 0

    freed = 0
    deleted = 0
    errors = 0
    for r in targets:
        fp = root / r["path"]
        try:
            fp.unlink()
            freed += r["local_size"]
            deleted += 1
            print(f"  evicted {r['path']} ({r['local_size'] / 1024**2:.0f} MB)")
        except OSError as e:
            errors += 1
            print(f"  EVICT FAILED {r['path']}: {e}")
    print(f"\nevicted {deleted} files, freed {freed / 2**30:.2f} GB "
          f"({errors} error(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
