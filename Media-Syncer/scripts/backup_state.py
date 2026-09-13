"""Off-machine backup of Media-Syncer's load-bearing state files.

Since the virtual-library cutover, `remote_inventory.json` is not just a cache --
mediafs presents the entire library from it and eviction/hydration resolve through
it. Losing it (dead Mini/SSD) would cost a multi-hour fleet rescan to rebuild.
So this copies it (and `sync_state.json`) to the shared `metadata-backup` MEGA
remote, versioned with `--backup-dir` so an overwritten copy is set aside rather
than lost.

Deliberately NOT git: these files change every sync cycle (high-churn machine
state, not config), and are single-host, so git -- which is for propagating
`rclone.conf` across machines -- is the wrong tool. `free_space.json` is pure cache
and is not backed up.

Run periodically by launchd (com.mikeyferguson.mediasyncstatebackup). Non-fatal:
a failure is logged and left for the next run.

    python3 -m scripts.backup_state [--dry-run]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime

from . import config


def _log(msg: str) -> None:
    print(f"[backup_state] {msg}", flush=True)


def _rclone(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess | None:
    """Run an rclone command against the machine-local config (which carries the live
    MEGA session tokens). Returns None on a transport failure; callers treat that as
    "leave things in place" rather than erroring the whole run."""
    try:
        return subprocess.run(
            [config.RCLONE_PATH, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"rclone {args[0]} error: {e}")
        return None


def prune_state_versions(remote: str, versions: str, keep: int) -> None:
    """Delete version dirs beyond the newest `keep`, then empty the rubbish bin.

    MEGA's `use_trash = true` parks every delete in the rubbish bin, which still counts
    against quota, so pruning alone reclaims nothing -- the follow-up `cleanup` is what
    actually frees the space. This is the fix for the account filling up to "over quota"
    while every byte was a live, tracked backup.
    """
    if keep <= 0:
        return
    r = _rclone(["lsf", f"{remote}:{versions}", "--dirs-only"])
    if r is None or r.returncode != 0:
        _log("could not list state versions; leaving them in place")
        return
    dirs = sorted(x.strip().rstrip("/") for x in r.stdout.splitlines() if x.strip())
    stale = dirs[:-keep]
    if not stale:
        return
    _log(f"pruning {len(stale)} stale state version(s) (keeping the newest {keep})")
    for d in stale:
        _rclone(["purge", f"{remote}:{versions}/{d}"], timeout=600)
    _rclone(["cleanup", f"{remote}:"], timeout=600)
    _log("rubbish bin emptied")


def main() -> int:
    ap = argparse.ArgumentParser(description="Back up Media-Syncer state to MEGA.")
    ap.add_argument("--dry-run", action="store_true", help="show what would transfer")
    args = ap.parse_args()

    remote = config.METADATA_BACKUP_REMOTE
    dest = f"{remote}:{config.METADATA_BACKUP_BASE}/{config.STATE_BACKUP_SUBPATH}"
    # Timestamped version dir so an overwritten copy is preserved, not destroyed.
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    versions = f"{dest}/_versions/{ts}"

    ok = True
    for path in config.STATE_BACKUP_FILES:
        if not path.exists():
            _log(f"{path.name} missing; skipping")
            continue
        cmd = [
            config.RCLONE_PATH, "copy", str(path), dest,
            "--backup-dir", versions,
            "--retries", "3", "--low-level-retries", "10", "--stats-one-line",
        ]
        if args.dry_run:
            cmd.append("--dry-run")
        _log(f"{'DRY-RUN ' if args.dry_run else ''}copy {path.name} -> {dest}")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as e:
            _log(f"{path.name} backup error: {e}")
            ok = False
            continue
        if r.returncode != 0:
            _log(f"{path.name} backup FAILED (exit {r.returncode}): {r.stderr.strip()[:200]}")
            ok = False
        else:
            _log(f"{path.name} backed up ok")

    # Retention + bin-emptying. Skipped on --dry-run: we must not mutate the remote on a
    # dry run, and the cleanup is as real a mutation as the copy.
    if not args.dry_run:
        prune_state_versions(
            remote,
            f"{config.METADATA_BACKUP_BASE}/{config.STATE_BACKUP_SUBPATH}/_versions",
            config.STATE_BACKUP_KEEP_VERSIONS,
        )

    _log("done" if ok else "done WITH ERRORS (will retry next run)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
