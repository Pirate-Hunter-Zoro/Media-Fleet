"""Move files off any remote whose usage exceeds REMOTE_CAP_BYTES, until it is back under.

MEGA refuses writes to an account past its quota, so an over-cap remote is dead capacity: it
can still serve reads, but nothing lands on it again and the account sits in violation.

Each move is GAPLESS, the same rule the One Pace relocation follows: the new copy is written
to a remote with room FIRST and the old copy deleted only once the destination has been
verified to hold the file at the right size. At no instant is the path absent from the fleet,
so a failure part-way through leaves a duplicate (which the next `rclone dedupe` or a rerun
resolves) rather than a hole. The source's rubbish bin is emptied after the delete, because a
MEGA deletion keeps consuming quota until it is.

The local copy is preferred as the source when it is present at the right size -- that halves
the bytes crossing the tunnel, since a remote-to-remote copy streams down and back up through
this machine.

    python3 -m scripts.rebalance_overfull                # dry run: print the plan, touch nothing
    python3 -m scripts.rebalance_overfull --execute      # do it
    python3 -m scripts.rebalance_overfull --execute --remote automega30

Takes the uploader lock, so it cannot run alongside the daemon's upload phase -- two processes
spending the same pool free space is the hazard that produces over-cap accounts in the first
place. The daemon skips its upload phase for a cycle rather than competing; it does not need to
be stopped.
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from . import config
from .operations import (
    find_mega_remotes,
    inventory_bytes_by_remote,
    usable_free,
)
from .uploader_lock import acquire_or_die
from .utils import (
    get_expected_local_path,
    repeat_command,
    run_command,
    write_json_atomic,
)

G = 1024 ** 3


def _live_usage(remotes: list[str]) -> dict[str, int]:
    """{remote: used bytes} straight from `rclone about`. The cached figure is not good enough
    here: this script's whole job is repairing a state the cache was too stale to prevent.

    Parallel over SCAN_WORKERS, for the reason `refresh_free_space` is: each `about` is ~1.8 s
    of pure latency, so a serial sweep of a several-hundred-account pool is a quarter of an
    hour before the plan is even printed. `rotate=False` -- a rotation resets every TCP
    connection on the machine, which would kill the other workers mid-probe.
    """
    def _one(remote):
        out, err = repeat_command([config.RCLONE_PATH, "about", f"{remote}:/", "--json"],
                                  rotate=False)
        if err or not out:
            return remote, None
        try:
            return remote, json.loads(out).get("used", 0)
        except json.JSONDecodeError:
            return remote, None

    usage = {}
    with ThreadPoolExecutor(max_workers=config.SCAN_WORKERS) as ex:
        for remote, used in ex.map(_one, remotes):
            if used is None:
                logging.warning(f"could not read usage for {remote}; skipping it")
            else:
                usage[remote] = used
    return usage


def build_plan(inventory: dict, usage: dict[str, int], only: str | None = None) -> list[dict]:
    """Choose what to move. Largest file first, so the fewest transfers close the excess.

    Target is REMOTE_CAP_BYTES - REMOTE_FILL_MARGIN_BYTES rather than the cap itself: landing
    exactly on the cap leaves a remote that the allocator still considers full (the margin) and
    that MEGA would push back over on any rubbish-bin lag.
    """
    target = config.REMOTE_CAP_BYTES - config.REMOTE_FILL_MARGIN_BYTES
    by_remote: dict[str, list[tuple[int, str]]] = {}
    for path, entry in inventory.items():
        try:
            by_remote.setdefault(entry[0], []).append((entry[2] or 0, path))
        except (IndexError, TypeError):
            continue

    plan = []
    for remote, used in sorted(usage.items(), key=lambda kv: -kv[1]):
        if used <= config.REMOTE_CAP_BYTES:
            continue
        if only and remote != only:
            continue
        excess = used - target
        freed = 0
        for size, path in sorted(by_remote.get(remote, []), reverse=True):
            if freed >= excess:
                break
            plan.append({"path": path, "size": size, "src": remote})
            freed += size
        if freed < excess:
            logging.warning(f"{remote} is {excess/G:.2f} GB over target but its inventory only "
                            f"accounts for {freed/G:.2f} GB -- it holds bytes that are not in "
                            f"the path->remote map (orphaned duplicate nodes). Run "
                            f"`rclone dedupe --dedupe-mode largest {remote}:` then "
                            f"`rclone cleanup {remote}:`.")
    return plan


def _pick_destination(size: int, cached: dict, placed: dict[str, int],
                      remotes: list[str], excluded: set, avoid: set) -> str | None:
    """Emptiest remote with room for `size` plus the fill margin.

    Emptiest, not first-that-fits: this script exists because remotes got packed past their
    cap, and spreading the evacuated files over the roomiest accounts keeps a rerun from
    immediately having to move them again.
    """
    need = size + config.REMOTE_FILL_MARGIN_BYTES
    best, best_free = None, 0
    for remote in remotes:
        if remote in excluded or remote in avoid:
            continue
        free = usable_free(cached.get(remote), placed.get(remote, 0))
        if free >= need and free > best_free:
            best, best_free = remote, free
    return best


def _verify(remote: str, path: str, size: int) -> bool:
    """Confirm the destination holds the file at the right size before anything is deleted.

    Conservative in one direction only: any doubt (a failed listing, a size mismatch) answers
    no, because a wrong yes deletes the only other copy.
    """
    out, err = repeat_command([config.RCLONE_PATH, "lsjson", f"{remote}:{path}"], rotate=False)
    if err or not out:
        return False
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return False
    return any(r.get("Size") == size for r in rows if isinstance(r, dict))


def move_one(item: dict, dest: str, inventory: dict, inv_lock) -> bool:
    """Copy one file to `dest`, verify it, then delete the source copy and empty its bin."""
    path, size, src = item["path"], item["size"], item["src"]

    # Prefer a local source: a remote-to-remote copy streams down and back up through this
    # machine, so using the SSD copy halves the bytes over the tunnel. Only when the local file
    # is present AND the right size -- an evicted or partial copy is not a source.
    source = f"{src}:{path}"
    local = get_expected_local_path(path)
    if local is not None:
        try:
            if local.is_file() and local.stat().st_size == size:
                source = str(local)
        except OSError:
            pass

    logging.info(f"moving {path} ({size/G:.2f} GB) {src} -> {dest} "
                 f"(source: {'local' if source != f'{src}:{path}' else 'remote'})")
    _result, err = run_command(
        [config.RCLONE_PATH, "copyto", source, f"{dest}:{path}",
         "--low-level-retries", "20", "--retries", "1"],
        timeout=config.TIMEOUT(size))
    if not _verify(dest, path, size):
        logging.error(f"copy of {path} to {dest} did not verify ({err.strip()[:120]}); "
                      f"leaving the copy on {src} untouched")
        return False

    # Verified on the destination. Only now is it safe to remove the source copy.
    del_result, del_err = repeat_command(
        [config.RCLONE_PATH, "deletefile", f"{src}:{path}"], rotate=False)
    if del_result is None or "error" in del_err.lower() or "failed to" in del_err.lower():
        logging.error(f"copied {path} to {dest} but could not delete it from {src}: {del_err}. "
                      f"The path is now on TWO remotes -- rerun once {src} accepts deletes.")
        # The inventory must point at the copy that is definitely there. It is also the one
        # a rerun will keep, so recording `dest` makes the duplicate resolvable.
        with inv_lock:
            inventory[path] = [dest, inventory.get(path, [None, None, size])[1], size]
        return False

    # A MEGA delete only parks the file in the rubbish bin, which still counts against quota.
    # Without this the account stays over cap and nothing looks wrong.
    clean_result, clean_err = repeat_command([config.RCLONE_PATH, "cleanup", f"{src}:"],
                                             rotate=False)
    if clean_result is None or clean_err:
        logging.error(f"cleanup of {src} failed ({clean_err.strip()[:120]}); its quota will not "
                      f"drop until the rubbish bin is emptied")

    with inv_lock:
        inventory[path] = [dest, inventory.get(path, [None, None, size])[1], size]
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Move files off over-cap MEGA remotes.")
    ap.add_argument("--execute", action="store_true", help="actually move (default: dry run)")
    ap.add_argument("--remote", help="repair only this remote")
    args = ap.parse_args()

    remotes = find_mega_remotes()
    excluded = config.upload_excluded_remotes()
    try:
        inventory = json.loads(config.REMOTE_INVENTORY_PATH.read_text())
    except (OSError, ValueError) as e:
        return f"cannot read {config.REMOTE_INVENTORY_PATH}: {e}"

    logging.info(f"reading live usage for {len(remotes)} remotes...")
    usage = _live_usage(remotes)
    plan = build_plan(inventory, usage, args.remote)
    if not plan:
        print(f"Nothing over the {config.REMOTE_CAP_BYTES/G:.0f} GB cap. Pool is healthy.")
        return 0

    srcs = sorted({i["src"] for i in plan})
    print(f"{len(plan)} file(s) / {sum(i['size'] for i in plan)/G:.2f} GB to move off "
          f"{len(srcs)} over-cap remote(s):")
    for item in plan:
        print(f"  {item['size']/G:6.2f} GB  {item['src']:<20} {item['path']}")
    if not args.execute:
        print("\nDry run. Re-run with --execute to move them.")
        return 0

    # Exclusive: moving files SPENDS pool free space, so it must not run alongside the
    # daemon's allocator. Held for the whole run; flock releases it if we are killed.
    lock = acquire_or_die("rebalance_overfull")

    try:
        cached = json.loads(config.FREE_SPACE_PATH.read_text())
    except (OSError, ValueError):
        cached = {}
    placed = inventory_bytes_by_remote(inventory)
    # Never send a file to a remote this run is evacuating.
    avoid = set(srcs)

    # Destinations are assigned UP FRONT, all distinct, so the moves can run concurrently.
    # rclone's mega backend pushes one file over a single connection capped around 2.4 MB/s,
    # so a serial run of a whole repair is hours; N moves against N DIFFERENT accounts scale
    # nearly linearly, which is the same reason the upload phase pins one worker per account.
    # Distinct destinations are what makes it safe: no two workers touch the same account's
    # quota, and each file's source remote is distinct too (one file per over-cap remote).
    assignments = []
    for item in plan:
        dest = _pick_destination(item["size"], cached, placed, remotes, excluded, avoid)
        if dest is None:
            logging.error(f"no remote has room for {item['path']} ({item['size']/G:.2f} GB); "
                          f"provision accounts first (python3 -m scripts.mega_accounts --ensure)")
            continue
        assignments.append((item, dest))
        # Charge it now so the next assignment cannot pick the same account for a file that
        # would no longer fit alongside this one, and bar it as a destination for this run.
        placed[dest] = placed.get(dest, 0) + item["size"]
        avoid.add(dest)

    inv_lock = threading.Lock()
    results = []

    def _run(pair):
        item, dest = pair
        ok = move_one(item, dest, inventory, inv_lock)
        # Persist after every move, not at the end: mediafs and predownload read this file
        # live, and a run interrupted half way must leave the inventory pointing at copies
        # that exist rather than at a remote the file has already been deleted from.
        with inv_lock:
            write_json_atomic(config.REMOTE_INVENTORY_PATH, dict(inventory))
        return ok

    workers = max(1, min(config.UPLOAD_WORKERS, len(assignments)))
    logging.info(f"moving {len(assignments)} file(s) with {workers} worker(s), "
                 f"one per destination account.")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_run, assignments))

    moved = sum(1 for r in results if r)
    failed = len(plan) - moved
    logging.info(f"rebalance done: {moved} moved, {failed} failed.")
    print("Re-run `python3 -m scripts.check_space` after the next free-space sweep to confirm.")
    lock.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
