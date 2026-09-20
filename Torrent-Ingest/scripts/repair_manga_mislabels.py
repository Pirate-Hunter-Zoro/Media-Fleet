#!/usr/bin/env python3
"""Rename `vNNNN` shelf files whose CONTENTS are chapters, through the purge machinery.

HANDOFF 10.0 row 3 / 10.5c. Seven One Piece files were filed as volumes by number
alone -- `One Piece v1078.cbz`, `v1151.cbz`, `v1152.cbz`, `v1161.cbz`, `v1162.cbz`,
`v1171.cbz`, `v1176.cbz` -- and there are not that many One Piece volumes. Their
archives say what they are: 12-16 pages of chapter `d1078`, `op_1151_t_012`, `1176-001`.
`comicfacts` reads that from the entries; a filename alone cannot (which is why the
model kept incrementing and nothing could refuse it, 10.5a).

THIS IS A RENAME, NOT A MOVE OR AN MV. The bytes are read through the mount (hydrates
an evicted copy), written to the correct `cNNNN.cbz` beside them, and the old path is
then superseded exactly as a purge is: `library.supersede_paths` unlinks it through
the mount and queues the remote purge, `dbhook.record_purge` supersedes its DB rows,
and `dbhook.record_plan` records the chapter under its real name. The reaper then
removes the old pool object; mediafs uploads the new one. A bare `mv` would leave the
pool keyed to the old name and the reaper blind.

    python3 scripts/repair_manga_mislabels.py --series "One Piece"           # dry
    python3 scripts/repair_manga_mislabels.py --series "One Piece" --apply

Read-only without `--apply`. Exit 0 = every check passed (or a dry run ran clean).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import comicfacts                                                    # noqa: E402
import config                                                        # noqa: E402
import dbhook                                                        # noqa: E402
import journal                                                       # noqa: E402
import library                                                       # noqa: E402
import manga_volume_map as mvm                                       # noqa: E402
import chapter_volume_reconcile as cvr                               # noqa: E402


def _abs(rel: str) -> Path | None:
    for root in (config.MEDIAFS_MOUNT, config.MEDIA_ROOT):
        p = root / rel
        if p.exists():
            return p
    return None


def candidates(series=None, series_dir=None):
    """`[(label, rel, new_rel, number, colored)]` for `vNNNN` files that are chapters.

    The number comes from the archive's entries (`comicfacts`), never from the name:
    `v1078` whose entries are `d1078` chapter pages is chapter 1078. A file whose
    entries associate the chapters with its volume number is left alone.
    """
    out = []
    owned = cvr.owned_manga(series=series, series_dir=series_dir)
    for label, files in sorted(owned.items()):
        for rel, (mtype, number, colored) in sorted(files.items()):
            name = rel.rsplit("/", 1)[-1]
            if mtype != "chapter" or not dbhook._VOL.search(name):
                continue
            p = _abs(rel)
            if p is None:
                continue
            f = comicfacts.facts(p, name_hint=name)
            if not f or f.get("kind") != "chapter" or f.get("volume") is not None:
                continue
            ch = next((c for c in (f.get("chapters") or []) if c), None)
            if not ch:
                continue
            ext = Path(name).suffix.lower() or ".cbz"
            # KEEP THE SERIES IN THE NAME. The first cut wrote `c{ch:04d}` alone, so the
            # seven `vNNNN` mislabels became `c1078.cbz`..`c1176.cbz` with no series --
            # exactly the bare markers the owner found beside their canonical copies on
            # 2026-09-20 (and the repair's own output created them). Replace the volume
            # marker in the stem, and prefix the series label when nothing else names it.
            stem = Path(name).stem
            newstem = dbhook._VOL.sub(f"c{int(ch):04d}", stem, count=1).strip()
            if library._BARE_MARKER_STEM.match(newstem):
                newstem = f"{label.strip()} {newstem}".strip()
            new_rel = f"{rel.rsplit('/', 1)[0]}/{newstem}{ext}"
            if new_rel == rel or _abs(new_rel) is not None:
                continue
            out.append((label, rel, new_rel, int(ch), colored))
    return out


def apply_rename(label, rel, new_rel, number, log_fn=print) -> bool:
    """Copy the bytes to the real name, then supersede the old path. True on success."""
    src = _abs(rel)
    if src is None:
        log_fn(f"{label}: {rel}: source vanished; skipped")
        return False
    data = src.read_bytes()
    dst = config.MEDIAFS_MOUNT / new_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dst)
    if dst.stat().st_size != len(data):
        log_fn(f"{label}: {new_rel}: size mismatch after write; old file left in place")
        return False
    library.supersede_paths([rel])
    try:
        dbhook.record_purge([rel])
    except Exception as exc:                                            # noqa: BLE001
        log_fn(f"{label}: {rel}: library.db purge mirror failed: {exc}")
    plan = {
        "media_type": "comic", "title": label,
        "files": [{"src": str(dst), "dst_rel": new_rel,
                   "type": "chapter", "number": number}],
    }
    try:
        dbhook.record_plan(plan)
    except Exception as exc:                                            # noqa: BLE001
        log_fn(f"{label}: {new_rel}: library.db plan record failed: {exc}")
    journal.log_decision("", label,
                         f"manga mislabel repaired: {rel} -> {new_rel} "
                         f"(archive contents are chapter {number})")
    log_fn(f"{label}: renamed {rel} -> {new_rel}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Rename vNNNN files whose contents are chapters.")
    ap.add_argument("--series", help="one series folder name")
    ap.add_argument("--all", action="store_true", help="every series on the manga shelf")
    ap.add_argument("--apply", action="store_true", help="rename + supersede (default dry)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not args.series and not args.all:
        ap.error("give --series NAME or --all")

    series = [args.series] if args.series else None
    found = candidates(series=series[0] if series else None)
    if args.json:
        print(json.dumps([{"series": s, "old": o, "new": n, "chapter": c}
                          for s, o, n, c, _col in found], indent=2))
    else:
        for label, rel, new_rel, number, colored in found:
            print(f"{label}: {rel} -> {new_rel}  (chapter {number}"
                  f"{', colored' if colored else ''})")
        print(f"{len(found)} mislabel(s) to repair")
    if not args.apply or not found:
        return 0
    ok = sum(1 for label, rel, new_rel, number, _c in found
             if apply_rename(label, rel, new_rel, number))
    print(f"{ok}/{len(found)} renamed")
    return 0 if ok == len(found) else 1


if __name__ == "__main__":
    raise SystemExit(main())
