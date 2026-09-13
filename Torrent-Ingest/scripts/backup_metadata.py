#!/usr/bin/env python3
"""Mirror the library's Jellyfin sidecars + this repo's state/ dir to MEGA.

Media-Syncer replicates only true media (video/subs/comics); it ignores every
`.nfo` and poster and knows nothing about this repo's `state/` dir. This backs
up the metadata this pipeline creates -- above all the OWNED/locked episode
`.nfo`, which by definition cannot be re-scraped -- so a lost library root can be
rebuilt with its exact layout.

Two rclone `sync` jobs (mirror semantics), each with `--backup-dir` pointed at a
timestamped folder, so an overwritten or deleted file is versioned aside rather
than destroyed -- preserving this project's "every change reversible" contract.
The backup lands on a distinct `metadata-backup/` prefix on one MEGA pool
account: Media-Syncer's purge/probe only ever touches Shows/Movies/Comics and
ignores non-media extensions, so this tree is invisible to the media pool.

Non-fatal by design: an rclone failure is logged and left for the next run.

Usage:
    python3 scripts/backup_metadata.py [--dry-run] [--verbose]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


def _log(msg: str) -> None:
    print(f"[backup_metadata] {msg}", flush=True)


def _rclone(args: list[str], timeout: int = 1800) -> subprocess.CompletedProcess | None:
    """Run an rclone command with the active config. None on a transport failure."""
    try:
        return subprocess.run(
            [config.RCLONE_BIN, *args, "--config", str(config.RCLONE_CONFIG)],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"rclone {args[0]} error: {e}")
        return None


def prune_versions(remote: str, versions: str, keep: int) -> None:
    """Delete version dirs beyond the newest `keep`, then empty the rubbish bin.

    MEGA's `use_trash = true` keeps every deletion consuming quota until the bin is
    emptied, so pruning alone frees nothing -- the follow-up `cleanup` is the part that
    actually reclaims space. This is the fix for the metadata-backup account filling up
    to "over quota" despite every byte being a live, tracked backup.
    """
    if keep <= 0:
        return
    r = _rclone(["lsf", f"{remote}:{versions}", "--dirs-only"])
    if r is None or r.returncode != 0:
        _log("could not list backup versions; leaving them in place")
        return
    dirs = sorted(x.strip().rstrip("/") for x in r.stdout.splitlines() if x.strip())
    stale = dirs[:-keep]
    if not stale:
        return
    _log(f"pruning {len(stale)} stale version dir(s) (keeping the newest {keep})")
    for d in stale:
        _rclone(["purge", f"{remote}:{versions}/{d}"], timeout=900)
    _rclone(["cleanup", f"{remote}:"], timeout=900)
    _log("rubbish bin emptied")


def _ensure_config() -> bool:
    """Make sure an rclone config carrying the MEGA creds is in place. Seeds it
    from the committed Media-Syncer conf if the machine-local one is absent.
    Returns False if no credentials can be found at all."""
    if config.RCLONE_CONFIG.exists():
        return True
    if config.MEDIA_SYNCER_RCLONE_CONF.exists():
        config.RCLONE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config.MEDIA_SYNCER_RCLONE_CONF, config.RCLONE_CONFIG)
        _log(f"seeded {config.RCLONE_CONFIG} from {config.MEDIA_SYNCER_RCLONE_CONF}")
        return True
    _log(
        f"no rclone config at {config.RCLONE_CONFIG} and no fallback at "
        f"{config.MEDIA_SYNCER_RCLONE_CONF}; cannot back up"
    )
    return False


def _rclone_sync(
    src: Path,
    dest: str,
    filters: list[str],
    backup_dir: str,
    dry_run: bool,
    verbose: bool,
) -> bool:
    if not src.exists():
        _log(f"source {src} missing; skipping")
        return True
    cmd = [
        config.RCLONE_BIN,
        "sync",
        str(src),
        dest,
        "--config", str(config.RCLONE_CONFIG),
        "--backup-dir", backup_dir,
        "--fast-list",
        "--transfers", "8",
        "--checkers", "16",
        "--retries", "3",
        "--low-level-retries", "10",
        "--stats-one-line",
    ]
    for rule in filters:
        cmd += ["--filter", rule]
    if dry_run:
        cmd.append("--dry-run")
    if verbose:
        cmd.append("-v")

    _log(f"{'DRY-RUN ' if dry_run else ''}sync {src} -> {dest}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    for line in (result.stdout + result.stderr).splitlines():
        if line.strip():
            _log(f"  rclone: {line}")
    if result.returncode != 0:
        _log(f"rclone sync to {dest} FAILED (exit {result.returncode})")
        return False
    _log(f"sync to {dest} ok")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Back up Jellyfin sidecars + state/ to MEGA.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would transfer, write nothing")
    ap.add_argument("--verbose", action="store_true", help="verbose rclone output")
    args = ap.parse_args()

    if not os.path.exists(config.RCLONE_BIN) and not shutil.which(config.RCLONE_BIN):
        _log(f"rclone not found at {config.RCLONE_BIN}; cannot back up")
        return 1
    if not _ensure_config():
        return 1

    remote = config.METADATA_BACKUP_REMOTE
    base = config.METADATA_BACKUP_BASE
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    versions = f"{remote}:{base}/_versions/{ts}"

    ok = True
    # 1) Library sidecars (.nfo + artwork) off the SSD.
    ok &= _rclone_sync(
        config.MEDIA_ROOT,
        f"{remote}:{base}/media",
        config.METADATA_BACKUP_FILTERS,
        f"{versions}/media",
        args.dry_run,
        args.verbose,
    )
    # 2) This repo's state/ audit trail (journal, decisions, plans, nfo backups).
    #    Skip only the single-instance lock.
    ok &= _rclone_sync(
        config.STATE_DIR,
        f"{remote}:{base}/state",
        ["- *.lock"],
        f"{versions}/state",
        args.dry_run,
        args.verbose,
    )

    # Retention + bin-emptying (skipped on --dry-run: it must not mutate the remote).
    if not args.dry_run:
        prune_versions(remote, f"{base}/_versions", config.METADATA_BACKUP_KEEP_VERSIONS)

    _log("done" if ok else "done WITH ERRORS (will retry next run)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
