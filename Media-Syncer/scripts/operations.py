# operations.py

"""
The Grimoire of Techniques.
"""

import configparser
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path

from . import config
from .config import (
    RCLONE_CONF_PATH,
    RCLONE_PATH,
    scan_dirs,
    VIDEO_EXTENSIONS,
    COMICS_EXTENSIONS,
    REMOTE_CAP_BYTES,
)
from .utils import repeat_command, write_json_atomic


def _capped_free(used: int, total: int) -> int:
    """Usable free space for a remote, capping its capacity at REMOTE_CAP_BYTES so a
    temporary bonus (e.g. a 25 GB account) is never treated as real, durable space.
    Also guards MEGA's garbage max-int 'used' when an account is full."""
    capped_total = min(total, REMOTE_CAP_BYTES)
    return 0 if used >= capped_total else (capped_total - used)


def inventory_bytes_by_remote(remote_index=None) -> dict:
    """{remote: bytes the fleet has PLACED there}, summed from the path -> remote inventory.

    This is a SECOND, independent measure of how full a remote is, and the only one that is
    current the instant a file lands: a row is added to the inventory inside the upload
    worker's critical section, while free_space.json's `used` comes from an `rclone about`
    sweep that runs once per REMOTE_RESCAN_SEC. Between two sweeps the `about` figure is a
    lower bound on reality by exactly however much has been uploaded since, which is what
    `usable_free` exists to correct for.

    Errs toward reporting a remote FULLER than it is, which is the safe direction: a remote
    the reaper has deleted from keeps its inventory rows until the next rescan, so a little
    capacity is stranded for at most one rescan interval. A missing or unreadable inventory
    yields an empty dict, which makes `usable_free` fall back to the `about` figure alone.
    """
    if remote_index is None:
        try:
            with open(config.REMOTE_INVENTORY_PATH, 'r') as f:
                remote_index = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    placed = {}
    for entry in remote_index.values():
        try:
            remote, _mtime, size = entry[0], entry[1], entry[2]
        except (IndexError, TypeError):
            continue
        placed[remote] = placed.get(remote, 0) + (size or 0)
    return placed


def usable_free(entry, placed: int = 0) -> int:
    """Usable free bytes on one remote: the STINGIER of the two signals we have.

    `entry` is that remote's free_space.json row (None/absent -> 0 free, so a remote nothing
    has measured yet is skipped rather than assumed empty). `placed` is its byte total from
    `inventory_bytes_by_remote`.

    Taking the minimum is the load-bearing part, because the two signals fail in opposite
    directions and each covers the other's blind spot:

    * The `about` figure misses everything uploaded since the last sweep. A sweep runs every
      REMOTE_RESCAN_SEC (6 h) while an upload phase ends and restarts as often as the local
      backlog allows, so several phases in a row can reseed their ledger from ONE snapshot
      and each spend the same bytes. That is how a fresh account reached 23.5 GiB against a
      20 GiB quota: eight phases in three and a half hours all believed it had 10.2 GiB free,
      and the ninth put a 7.8 GiB film on top of the 15.6 GiB already there.
    * The inventory figure misses bytes that are on the remote but not in the path -> remote
      map -- an orphaned duplicate node, or an ad-hoc upload outside the library namespace.
      The `about` figure counts those, because MEGA charges for them.
    """
    if not isinstance(entry, dict):
        return 0
    by_about = _capped_free(entry.get('used', 0), entry.get('total', 0))
    by_inventory = max(0, REMOTE_CAP_BYTES - max(0, placed))
    return min(by_about, by_inventory)


def placeable_free(entry, placed: int = 0) -> int:
    """Bytes a remote can still actually accept for new uploads.

    `usable_free` is the raw free space, but every claim reserves
    REMOTE_FILL_MARGIN_BYTES before placing anything (see `_claim` in media_sync.py), so a
    remote holding `margin` free can accept no file at all. This is the figure provisioning
    must budget against: `max(0, usable_free - margin)` per remote, summed across the pool,
    is the real headroom before the pool fragments into slivers too small to place anything.

    Without it, a pool of a few hundred accounts each ~1/2 GB from full reports plenty of
    aggregate free space while the allocator finds "no suitable remote found" for every file
    -- the exact state that let the pool stall with 224 GB "free" and ~1 GB placeable.
    """
    return max(0, usable_free(entry, placed) - config.REMOTE_FILL_MARGIN_BYTES)

def find_mega_remotes():
    if not RCLONE_CONF_PATH.exists():
        logging.error(f"rclone config file not found at {RCLONE_CONF_PATH}")
        return []
    config = configparser.ConfigParser()
    try:
        config.read(RCLONE_CONF_PATH)
    except Exception as e:
        logging.error(f"Failed to parse rclone config file: {e}")
        return []
    
    mega_remotes = [
        section for section in config.sections()
        if (config.has_option(section, 'type') and\
            config.get(section, 'type') == 'mega')
    ]
    
    # Sort remotes to ensure predictable, uniform filling
    mega_remotes.sort()
    
    return mega_remotes

def get_remote_free_space(remote, just_changed: bool=False, placed: int=None):
    """Usable free bytes on one remote, from the cache unless `just_changed` forces a live
    `rclone about`.

    `placed` is the caller's live count of bytes it has put on this remote, for the inventory
    cross-check in `usable_free`. The upload phase passes its own running total, which is
    fresher than the persisted inventory; omitted, the inventory file is consulted. Either way
    the `about` figure alone is never trusted on its own -- see `usable_free`.
    """
    if placed is None:
        placed = inventory_bytes_by_remote().get(remote, 0)
    all_free_spaces = {}
    if config.FREE_SPACE_PATH.exists():
        try:
            with open(config.FREE_SPACE_PATH, 'r') as f:
                all_free_spaces = json.load(f)
                if remote in all_free_spaces.keys() and not just_changed:
                    return usable_free(all_free_spaces[remote], placed)
        except json.JSONDecodeError:
            all_free_spaces = {}

    output, err = repeat_command([RCLONE_PATH, "about", f"{remote}:/", "--json"])
    if err or not output: return 0
    output_json = json.loads(output)

    all_free_spaces[remote] = output_json
    # Atomic + absolute. free_space.json is read live by predownload, mega_accounts and
    # check_space, so a truncate-then-write hands them a half-file; and the path came from
    # the CWD, which only resolved because the launcher happens to `cd` into the repo first.
    write_json_atomic(config.FREE_SPACE_PATH, all_free_spaces)

    return usable_free(output_json, placed)

def free_space_ledger(remotes, remote_index=None):
    """Return {remote: usable free bytes} for `remotes`, from ONE read of free_space.json.

    The per-remote accessor parses that file on every call and falls through to a live
    `rclone about` -- a round trip that may rotate the exit node and reset every TCP
    connection on the machine, including in-flight uploads. The upload phase must never do
    that on its hot path, so it seeds this ledger once, debits it locally as it claims
    remotes, and lets the periodic rescan reconcile against reality.

    The seed is cross-checked against the inventory (`usable_free`), because free_space.json
    alone is only as fresh as the last `about` sweep and a phase reseeds from it every time
    the daemon loops -- which is far more often than the sweep runs.

    A remote absent from the cache file is reported as 0 free rather than fetched. That is
    the safe direction: it is skipped for this phase and picked up once the next rescan's
    `about` sweep records it.
    """
    try:
        with open(config.FREE_SPACE_PATH, 'r') as f:
            cached = json.load(f)
    except (OSError, json.JSONDecodeError):
        cached = {}
    placed = inventory_bytes_by_remote(remote_index)
    # Excluded remotes are seeded at 0 so the claim loop can never pick them, while they
    # stay in the INDEX scan -- they already hold media, and dropping them from the
    # inventory would make that media look absent and get re-uploaded elsewhere.
    excluded = config.upload_excluded_remotes()
    ledger = {}
    for remote in remotes:
        if remote in excluded:
            ledger[remote] = 0
            continue
        ledger[remote] = usable_free(cached.get(remote), placed.get(remote, 0))
    return ledger

def find_local_files():
    all_files = set()
    junk = 0
    # Resolved per call, not at import: a drive attached after the daemon started must be
    # scanned for upload on the very next cycle.
    for root_str in scan_dirs():
        root = Path(root_str)

        if not root.is_dir():
            logging.warning(f"Configured media directory does not exist: {root}")
            continue

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for filename in filenames:
                # Finder writes a .DS_Store into every folder it opens, including ones on
                # the library drives. They are never uploaded (the dotfile skip below sees
                # to that) but they accumulate on disk and travel with a drive. This walk is
                # already happening, so pruning here costs no extra stat or traversal.
                if filename == ".DS_Store":
                    try:
                        (Path(dirpath) / filename).unlink()
                        junk += 1
                    except OSError:
                        pass
                    continue
                if not filename.startswith('.'):
                    extension = os.path.splitext(filename)[1].lower()
                    if extension in (VIDEO_EXTENSIONS | COMICS_EXTENSIONS):
                        all_files.add((Path(dirpath) / filename).resolve())
    if junk:
        logging.info(f"pruned {junk} .DS_Store file(s) from the library roots")
    return all_files

def refresh_free_space(remotes):
    """Re-measure free space on every remote IN PARALLEL, writing free_space.json once.

    Each `about` costs ~1.77 s of pure latency, so a serial sweep of the pool leaves the
    uploader idle for minutes.

    Writing once at the end is required, not tidiness: the per-remote accessor rewrites the
    whole file on every call, so doing that concurrently would have threads serialising
    different snapshots over each other and eventually truncate it.
    """
    try:
        with open(config.FREE_SPACE_PATH, 'r') as f:
            all_free = json.load(f)
    except (OSError, json.JSONDecodeError):
        all_free = {}

    def _one(remote):
        output, err = repeat_command([RCLONE_PATH, "about", f"{remote}:/", "--json"],
                                     rotate=False)
        if err or not output:
            return remote, None
        try:
            return remote, json.loads(output)
        except json.JSONDecodeError:
            return remote, None

    ok = 0
    with ThreadPoolExecutor(max_workers=config.SCAN_WORKERS) as ex:
        for remote, data in ex.map(_one, remotes):
            if data is not None:
                all_free[remote] = data
                ok += 1

    write_json_atomic(config.FREE_SPACE_PATH, all_free)
    return ok


def build_remote_index(remotes):
    """Rebuild the path -> [remote, mtime, size] inventory by listing every remote IN
    PARALLEL. Each listing costs ~1.56 s of latency, so a serial sweep of the pool is
    minutes of dead time.

    The listing of each remote is reconciled INDEPENDENTLY against the previous index. A
    remote that fails to list (a transient MEGA outage, a dead session, a timeout) keeps its
    previous entries rather than silently dropping them -- the single-residence invariant
    means a path can only ever live on one remote, so a remote's entries are only ever
    replaced by that remote's OWN successful listing, never by another remote's (or a
    failure's) silence. This is what stops a one-remote outage from truncating
    remote_inventory.json and making files already on MEGA disappear from the mount and the
    searcher's "do we own this?" index.

    The caller must still pass the full find_mega_remotes() list: a remote absent from that
    list cannot be listed, so its entries would otherwise linger forever.
    """
    try:
        with open(config.REMOTE_INVENTORY_PATH, "r") as f:
            old_remote_index = json.load(f)
    except Exception as e:
        logging.error(f"Failed to parse file list from local file 'remote_inventory.json': {e}.")
        old_remote_index = {}

    # Group the previous index by remote so each remote's entry set can be swapped in whole.
    old_by_remote = {}
    for path_str, entry in old_remote_index.items():
        if isinstance(entry, (list, tuple)) and entry:
            old_by_remote.setdefault(entry[0], {})[path_str] = entry

    def _one(remote):
        output, _ = repeat_command([RCLONE_PATH, "lsjson", "--recursive", f"{remote}:"],
                                   rotate=False)
        if output is None:
            logging.error(f"Failed to parse file list from remote '{remote}'. Skipping...")
            return remote, None
        try:
            return remote, json.loads(output)
        except json.JSONDecodeError:
            logging.error(f"Failed to parse file list from remote '{remote}'. Skipping...")
            return remote, None

    new_remote_index = {}
    with ThreadPoolExecutor(max_workers=config.SCAN_WORKERS) as ex:
        # Results are merged here, in the main thread, so new_remote_index needs no lock.
        # Ordering across remotes is irrelevant: the single-residence invariant means a
        # given path lives on exactly one remote, so two remotes cannot contend for a key.
        for remote, remote_files in ex.map(_one, remotes):
            if remote_files is None:
                # Listing failed: preserve this remote's previous entries so a transient
                # outage never erases its files from the inventory (and thus the mount).
                new_remote_index.update(old_by_remote.get(remote, {}))
                continue
            for item in remote_files:
                path_str = item.get("Path")
                mod_time = item.get("ModTime")
                size = item.get("Size")
                if not path_str: continue
                if item.get('IsDir'): continue

                if any(part.startswith('.') for part in Path(path_str).parts):
                    continue

                new_remote_index[path_str] = [remote, mod_time, size]

    # Record the index locally if a change occurred
    if old_remote_index != new_remote_index:
        # The SECOND writer of this file (media_sync._persist_inventory is the other). Both
        # must be atomic: mediafs decides whether to reload from (mtime_ns, size), so a
        # truncating write here stamps a fresh mtime over incomplete content and a poller can
        # latch the torn read as the live library view.
        write_json_atomic(config.REMOTE_INVENTORY_PATH, new_remote_index)

    return new_remote_index