#!/usr/bin/env python3
"""Recurring failed/stale-artifact janitor (diagnosis §5 items 1 + 9).

The disk-full → "everything is DEFERRED" deadlock used to only drain by hand. This
daemon-scheduled pass turns that into a self-healing one. On each run it:

  1. deletes `failed/` `.torrent` files (and their `state/torrent_sources/` mirror)
     once they are older than a grace window;
  2. deletes orphaned `.parts` partials and leftover download dirs in
     `~/Downloads/.torrent-ingest` -- keyed off the ingest JOURNAL (a `.parts` or a
     download DIRECTORY whose torrent is FAILED/REFUSED/COMPLETED is dead; an ACTIVE one
     is never touched, and a path any live record still claims wins), never off mtime
     alone. A dead directory is removed even when it still holds content: its bytes were
     either lost or already filed into ~/Media, and until this was implemented the code
     removed only EMPTY skeletons while the docstring claimed otherwise;
  3. removes empty directory skeletons in the download area, and dirs holding nothing but
     dead `.parts`;
  4. prunes the searcher's `state/magnets/*.magnet` records after N days (a magnet is a
     manual-import fallback, not durable state);
  5. evicts Media-Syncer's `backfill_replacements` "downgrade"/"stale" local files --
     delete local, keep the better/newer MEGA copy -- so the SSD stops carrying a worse
     copy the tier engine then has to keep around.

Write-once is respected throughout: eviction is a LOCAL cache delete only; the MEGA copy
is the durable one and is never touched. Dry-run by default; pass --apply to delete.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

# This repo's own directory goes FIRST on sys.path. Torrent-Ingest and Torrent-Searcher
# both ship modules named `config.py`, `library.py` and `ingest.py`, and both repos are on
# `sys.path` in some processes -- so a bare `import config` resolves to whichever repo the
# launcher happened to put first. That is how `directingest` died at import on 2026-08-27,
# reading Torrent-Ingest's `config` through Torrent-Searcher's `ingest` (§5 item 4a). The
# pin makes the resolution a property of the FILE rather than of how it was launched.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import config
import journal

# --- grace windows ------------------------------------------------------------

# A FAILED/COMPLETED torrent's source `.torrent` and `.parts` are dead the moment the
# journal says so; keep a short grace so a just-failed wave (or an iCloud still
# materializing the failed/ file) is not deleted out from under the daemon.
FAILED_ARTIFACT_GRACE_SEC = 7 * 24 * 3600

# A download file with NO journal record at all (a crash before registration, or a
# hand-added qBittorrent torrent) is conservatively kept much longer -- we cannot prove
# it is dead, only that it is old.
ORPHAN_GRACE_SEC = 30 * 24 * 3600

# A recorded magnet link is a manual-import fallback; prune it only after it has clearly
# outlived its usefulness.
MAGNET_GRACE_SEC = 30 * 24 * 3600

# Active journal states whose download bytes must NEVER be deleted.
_ACTIVE_STATES = {
    journal.QUEUED, journal.DOWNLOADING, journal.DOWNLOADED,
    journal.IDENTIFIED, journal.STAGED, journal.VERIFIED,
}


def _state_dir() -> Path:
    """Ingest's own state dir. The recorded-magnet store used to live in the searcher;
    it moved here when the searcher was removed (2026-09-10)."""
    return Path.home() / "Developer" / "Media-Fleet" / "Torrent-Ingest" / "state"


def _media_syncer_dir() -> Path:
    return Path.home() / "Developer" / "Media-Fleet" / "Media-Syncer"


def _is_older(p: Path, grace_sec: int, now: float) -> bool:
    try:
        return (now - p.stat().st_mtime) >= grace_sec
    except OSError:
        return False


def _journal_sets() -> tuple[set[str], set[str], set[Path], set[Path]]:
    """(active_hashes, dead_hashes, active_content_paths, dead_content_paths)."""
    active_hashes: set[str] = set()
    dead_hashes: set[str] = set()
    active_paths: set[Path] = set()
    dead_paths: set[Path] = set()
    for h, rec in journal.load_records().items():
        cp = rec.get("content_path")
        try:
            rp = Path(cp).resolve() if cp else None
        except OSError:
            rp = None
        if rec.get("status") in _ACTIVE_STATES:
            active_hashes.add(h)
            if rp is not None:
                active_paths.add(rp)
        else:
            dead_hashes.add(h)
            if rp is not None:
                dead_paths.add(rp)
    # A path is only DEAD if no live record still claims it. Two records can share a
    # content path (a re-drop of the same release), and an active claim always wins --
    # otherwise a retried download is deleted out from under the daemon that is using it.
    return active_hashes, dead_hashes, active_paths, dead_paths - active_paths


def _is_within_active(p: Path, active_paths: set[Path]) -> bool:
    rp = p.resolve()
    for ap in active_paths:
        try:
            if ap == rp or ap in rp.parents or rp in ap.parents:
                return True
        except OSError:
            continue
    return False


def _infohash_of_torrent(path: Path) -> str | None:
    """SHA1 of a .torrent's info dict (bencode walker, no external dependency)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data or data[:1] != b"d":
        return None

    def skip(idx: int) -> int:
        c = data[idx:idx + 1]
        if c == b"d":
            idx += 1
            while idx < len(data) and data[idx:idx + 1] != b"e":
                idx = skip(idx)
                idx = skip(idx)
            return idx + 1
        if c == b"l":
            idx += 1
            while idx < len(data) and data[idx:idx + 1] != b"e":
                idx = skip(idx)
            return idx + 1
        if c == b"i":
            return data.index(b"e", idx) + 1
        if c.isdigit():
            colon = data.index(b":", idx)
            n = int(data[idx:colon])
            return colon + 1 + n
        raise ValueError("bad bencode")

    try:
        i = 1
        while i < len(data) and data[i:i + 1] != b"e":
            kstart = i
            i = skip(i)
            k = data[kstart:i]
            vstart = i
            i = skip(i)
            if k == b"info":
                return hashlib.sha1(data[vstart:i]).hexdigest()
    except (ValueError, IndexError):
        return None
    return None


def clean_failed_torrents(apply: bool, grace_sec: int, now: float) -> int:
    removed = 0
    if not config.FAILED_DIR.is_dir():
        return 0
    for p in config.FAILED_DIR.iterdir():
        if p.suffix.lower() != ".torrent":
            continue
        if not _is_older(p, grace_sec, now):
            continue
        ih = _infohash_of_torrent(p)
        if apply:
            try:
                p.unlink()
                removed += 1
                if ih:
                    (config.TORRENT_SOURCE_MIRROR / f"{ih}.torrent").unlink(missing_ok=True)
                print(f"  removed failed .torrent {p.name}")
            except OSError as e:
                print(f"  SKIP failed .torrent {p.name}: {e}")
        else:
            print(f"  would remove failed .torrent {p.name}")
    return removed


def clean_incoming_partials(apply: bool, now: float,
                            active_hashes: set[str],
                            dead_hashes: set[str],
                            active_paths: set[Path],
                            dead_paths: set[Path] | None = None,
                            grace_dead: int = FAILED_ARTIFACT_GRACE_SEC) -> tuple[int, int]:
    """Delete orphaned `.parts`/download dirs; return (parts_removed, dirs_removed)."""
    dead_paths = dead_paths or set()
    parts_removed = dirs_removed = 0
    incoming = config.INCOMING_DIR
    if not incoming.is_dir():
        return 0, 0
    for p in list(incoming.iterdir()):
        if p.name == ".DS_Store":
            continue
        if p.is_file() and p.name.endswith(".parts"):
            # `.INFOHASH.parts` -> infohash is the 40-hex stem after the leading dot.
            ih = p.name[1:-len(".parts")]
            if ih in active_hashes:
                continue                       # in-flight -> never touch
            grace = FAILED_ARTIFACT_GRACE_SEC if ih in dead_hashes else ORPHAN_GRACE_SEC
            if not _is_older(p, grace, now):
                continue
            if apply:
                try:
                    p.unlink()
                    parts_removed += 1
                    print(f"  removed orphaned partial {p.name}")
                except OSError as e:
                    print(f"  SKIP partial {p.name}: {e}")
            else:
                print(f"  would remove orphaned partial {p.name}")
        elif p.is_dir():
            if _is_within_active(p, active_paths):
                continue
            # A dir the journal says is DEAD (failed/refused/completed -- its bytes are
            # either lost or already filed into ~/Media) is reapable even when it still
            # holds real files, and this is the case the docstring promised and the code
            # did not implement: the rule below removes only an EMPTY skeleton, so a
            # terminal download that still held content was kept forever.
            #
            # Scale, measured when this was written: only 1 dir (2.5 GB) was in that
            # state, because `_advance_cleanup` already removes a download after a
            # successful apply -- so this is a correctness hole, NOT the reason the SSD
            # was full. Do not read it as one. The 90 GB in `~/Downloads/.torrent-ingest`
            # was 62 GB of DOWNLOADED records legitimately waiting on identify plus 32 GB
            # in flight; the disk itself was held by 183 GB of un-evictable anime upgrades
            # in ~/Media (§4.134). This closes the path that leaks when cleanup does not
            # run (§7: implement what the docstring promises, or delete the promise).
            if _is_within_active(p, dead_paths) and _is_older(p, grace_dead, now):
                size = sum(f.stat().st_size for f in p.rglob("*")
                           if f.is_file()) if p.exists() else 0
                if apply:
                    try:
                        shutil.rmtree(p, ignore_errors=True)
                        if not p.exists():
                            dirs_removed += 1
                            print(f"  removed dead download dir {p.name} "
                                  f"({size >> 20} MB, journal says terminal)")
                            continue
                    except OSError as e:
                        print(f"  SKIP dead dir {p.name}: {e}")
                        continue
                else:
                    print(f"  would remove dead download dir {p.name} "
                          f"({size >> 20} MB, journal says terminal)")
                    continue
            # Otherwise remove only an EMPTY skeleton, or a dir that holds nothing but
            # dead .parts (which the file loop above will/does remove). A non-empty dir
            # the journal cannot call dead is a mid-flight download -> leave it.
            live_files = [f for f in p.rglob("*")
                          if f.is_file() and not f.name.endswith(".parts")]
            if live_files:
                continue
            has_parts = any(f.is_file() and f.name.endswith(".parts") for f in p.rglob("*"))
            if has_parts and not _is_older(p, ORPHAN_GRACE_SEC, now):
                continue
            if apply:
                try:
                    shutil.rmtree(p, ignore_errors=True)
                    if not p.exists():
                        dirs_removed += 1
                        print(f"  removed empty download dir {p.name}")
                except OSError as e:
                    print(f"  SKIP dir {p.name}: {e}")
            else:
                print(f"  would remove empty download dir {p.name}")
    return parts_removed, dirs_removed


def prune_magnets(apply: bool, now: float) -> int:
    removed = 0
    mag_dir = _state_dir() / "magnets"
    if not mag_dir.is_dir():
        return 0
    for p in mag_dir.iterdir():
        if p.suffix.lower() != ".magnet":
            continue
        if not _is_older(p, MAGNET_GRACE_SEC, now):
            continue
        if apply:
            try:
                p.unlink()
                removed += 1
                print(f"  removed stale magnet {p.name}")
            except OSError:
                pass
        else:
            print(f"  would remove stale magnet {p.name}")
    return removed


def evict_downgrades(apply: bool) -> int:
    """Run Media-Syncer's evict_downgrades.py (delete local, keep the better MEGA copy).
    Returns its exit code (best-effort; a failure is logged, not fatal)."""
    ms = _media_syncer_dir()
    script = ms / "scripts" / "evict_downgrades.py"
    if not script.exists():
        print("  evict_downgrades.py not found; skipping downgrade/stale eviction")
        return 0
    cmd = [sys.executable, str(script)] + (["--apply"] if apply else [])
    print(f"  -> {' '.join(cmd)}")
    return os.system(" ".join(cmd))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    args = ap.parse_args()

    now = time.time()
    active_hashes, dead_hashes, active_paths, dead_paths = _journal_sets()
    print(f"janitor: {len(active_hashes)} active torrent(s) protected, "
          f"{len(dead_hashes)} dead")

    clean_failed_torrents(args.apply, FAILED_ARTIFACT_GRACE_SEC, now)
    parts, dirs = clean_incoming_partials(args.apply, now, active_hashes, dead_hashes,
                                          active_paths, dead_paths)
    print(f"partials to remove: {parts}, download dirs to remove: {dirs}")
    prune_magnets(args.apply, now)
    evict_downgrades(args.apply)

    if not args.apply:
        print("DRY RUN -- pass --apply to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
