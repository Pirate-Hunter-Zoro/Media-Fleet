"""Backfill the replacements queue for local files that replaced their MEGA copy.

The replacements queue (`replacements.jsonl`) is normally populated by Torrent-Ingest when
it performs an anime quality upgrade, and drained by `media_sync.drain_replacements_queue`
(overwrite-in-place on the same remote + empty the rubbish bin). A file replaced by any
OTHER path -- a bulk re-compression, an upgrade performed before the queue mechanism
existed, a manual swap -- leaves its local bytes at a size that no longer matches the
remote, and the queue never learns about it. Those files then block eviction forever: the
inventory guard refuses to delete a file whose size is not proven on a remote, and the
upload phase skips it because its path is already in the remote index.

This tool re-derives that queue from first principles and is therefore safe and idempotent:

  * a candidate must be IN the inventory (so a remote copy exists to overwrite), at a
    DIFFERENT size (so the local content actually changed), and the local mtime must be
    NEWER than the remote mtime (a deliberate replacement, not a stale local copy that a
    re-download should overwrite);
  * stale locals (local mtime older than remote) are REPORTED, never queued -- overwriting
    the remote with them would regress the pool;
  * paths already in the queue are never duplicated.

Dry-run by default; pass --apply to write the queue. The write is append-only against the
existing queue so an in-flight drain is never clobbered.
"""
from __future__ import annotations

import argparse
import json
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import config  # noqa: E402
from . import tier    # noqa: E402


def _parse_remote_mtime(iso: str) -> float:
    try:
        s = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _inventory_nfc() -> dict:
    inv = tier.load_inventory()
    return {unicodedata.normalize("NFC", k): v for k, v in inv.items()}


def candidates(inv_nfc: dict, root: Path) -> tuple[list[dict], list[dict], list[dict]]:
    """Scan `root` for media files whose size differs from the inventory.

    Returns (upgrades, downgrades, stale), each {path, local_size, remote_size, remote}:

      * `upgrades`  -- local newer AND larger: a quality upgrade that must be re-uploaded
        so the pool tracks the new version (the safe replacements-queue case).
      * `downgrades`-- local newer AND smaller: an SD/compressed replacement of a better
        remote copy. These must NOT be re-uploaded (that would regress the pool); they
        are reported so they can be evicted (the remote copy is the better one).
      * `stale`     -- local older than the remote: the remote already has a newer copy;
        report only, evict locally.
    """
    upgrades: list[dict] = []
    downgrades: list[dict] = []
    stale: list[dict] = []
    exts = config.MEDIAFS_PAYLOAD_EXTENSIONS
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in exts:
                continue
            fp = Path(dirpath) / name
            try:
                st = fp.stat()
            except OSError:
                continue
            rel = str(fp.relative_to(root))
            entry = inv_nfc.get(unicodedata.normalize("NFC", rel))
            if entry is None or entry[2] == st.st_size:
                continue
            rec = {"path": rel, "local_size": st.st_size, "remote_size": entry[2],
                   "remote": entry[0]}
            if st.st_mtime > _parse_remote_mtime(entry[1]):
                if st.st_size > entry[2]:
                    upgrades.append(rec)
                else:
                    downgrades.append(rec)
            else:
                stale.append(rec)
    upgrades.sort(key=lambda r: r["path"])
    downgrades.sort(key=lambda r: r["path"])
    stale.sort(key=lambda r: r["path"])
    return upgrades, downgrades, stale


def _existing_queue_paths() -> set:
    q = config.REPLACEMENTS_QUEUE
    if not q.exists():
        return set()
    out = set()
    for line in q.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and e.get("path"):
            out.add(e["path"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the queue (default: dry-run)")
    ap.add_argument("--root", default=None, help="library root (default: config.SSD_LIBRARY_ROOT)")
    args = ap.parse_args()

    root = Path(args.root) if args.root else config.SSD_LIBRARY_ROOT
    inv_nfc = _inventory_nfc()
    upgrades, downgrades, stale = candidates(inv_nfc, root)

    total_bytes = sum(r["local_size"] for r in upgrades)
    print(f"root {root}")
    print(f"upgrades (local-newer, larger) to queue: {len(upgrades)} "
          f"({total_bytes / 2**30:.1f} GB)")
    print(f"downgrades (local-newer, smaller -- report only): {len(downgrades)} "
          f"({sum(r['local_size'] for r in downgrades) / 2**30:.1f} GB)")
    print(f"stale locals (report only, NOT queued): {len(stale)} "
          f"({sum(r['local_size'] for r in stale) / 2**30:.1f} GB)")
    if downgrades:
        for r in downgrades[:15]:
            print(f"  SD     {r['path']}  local={r['local_size']} remote={r['remote_size']}")
        if len(downgrades) > 15:
            print(f"  ... and {len(downgrades) - 15} more SD/downgrade file(s)")
    if stale:
        for r in stale[:15]:
            print(f"  STALE  {r['path']}  local={r['local_size']} remote={r['remote_size']}")
        if len(stale) > 15:
            print(f"  ... and {len(stale) - 15} more stale file(s)")

    if args.apply:
        existing = _existing_queue_paths()
        new = [r for r in upgrades if r["path"] not in existing]
        if not new:
            print("queue already up to date.")
            return 0
        q = config.REPLACEMENTS_QUEUE
        q.parent.mkdir(parents=True, exist_ok=True)
        with q.open("a", encoding="utf-8") as fh:
            for r in new:
                fh.write(json.dumps({"path": r["path"]}) + "\n")
        print(f"appended {len(new)} path(s) to {q}")
    else:
        print("DRY RUN -- pass --apply to write the queue.")
        for r in upgrades[:20]:
            print(f"  {r['path']}  local={r['local_size']} remote={r['remote_size']} -> {r['remote']}")
        if len(upgrades) > 20:
            print(f"  ... and {len(upgrades) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
