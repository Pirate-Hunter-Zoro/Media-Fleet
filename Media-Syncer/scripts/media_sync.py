# media_sync.py

"""
The Conductor.
"""

import logging
import math
import os
import json
import shutil
import threading
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import config
from . import check_space
from . import tier
from . import mega_accounts
from .utils import (
    setup_logging,
    run_command,
    repeat_command,
    get_root_for_path,
    get_expected_local_path,
    git_pull,
    clear_quarantine,
    clear_session_heals,
    heal_stale_session,
    is_stale_session_error,
    wait_while_streaming,
    write_json_atomic,
)
from .operations import(
    find_mega_remotes,
    get_remote_free_space,
    free_space_ledger,
    inventory_bytes_by_remote,
    refresh_free_space,
    find_local_files,
    build_remote_index,
)
from .transfer import chunked_download
from .uploader_lock import hold as _hold_upload_lock
from .vpn import rotate_exit_node

REMOTES = []

# The set of extensions that mark a file as real, tracked media -- identical to the
# union find_local_files() scans for. Anything else (Jellyfin's .nfo/.jpg/.png, stray
# dotfiles) is not content. Used to decide whether a directory is pure metadata cruft.
MEDIA_EXTENSIONS = config.VIDEO_EXTENSIONS | config.COMICS_EXTENSIONS

def _subtree_has_media(directory):
    """True if any file anywhere under `directory` is a tracked media file.

    A directory that holds only Jellyfin metadata (posters, .nfo) and no media counts
    as media-less, so a failed download can prune it wholesale rather than stranding
    the metadata next to a video that never arrived.
    """
    for _, _, filenames in os.walk(directory):
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() in MEDIA_EXTENSIONS:
                return True
    return False

def relocate_newer_one_pace(relative_path, local_path, old_remote, file_size):
    """Move a newer One Pace re-cut that no longer fits its current remote onto a
    remote that has room, then delete the stale copy from the old remote.

    The upload to the new remote happens BEFORE the old copy is deleted, so the
    episode is never absent from the fleet -- there is no missing-file window for
    the two-way One Pace sync to lose a race over (this is strictly safer than the
    in-place overwrite, which transiently parks the old copy in the rubbish bin).

    Returns the name of the remote the new version landed on, or None if no remote
    had room or the upload failed -- in which case the old copy is left intact and
    the next cycle retries.
    """
    for candidate in REMOTES:
        if candidate == old_remote:
            continue
        if get_remote_free_space(candidate) <= file_size:
            continue
        wait_while_streaming()   # yield to live playback before a One Pace relocation upload
        logging.info(f"Newer {relative_path} no longer fits on {old_remote}; relocating to {candidate}...")
        result, err = run_command([config.RCLONE_PATH, "copyto", str(local_path), f"{candidate}:{relative_path}", "--low-level-retries", "20", "--retries", "1"],
                                  timeout=config.TIMEOUT(file_size))
        if (("error" in err.lower() or "failed to" in err.lower()) and "file exists" not in err.lower()) or (result is None):
            # A dead session on the destination is not a reason to skip an otherwise fine
            # remote -- heal it and give this candidate one more turn before moving on. The
            # old copy is untouched either way, so a second failure costs only the retry.
            if is_stale_session_error(err) and heal_stale_session(candidate):
                result, err = run_command([config.RCLONE_PATH, "copyto", str(local_path), f"{candidate}:{relative_path}", "--low-level-retries", "20", "--retries", "1"],
                                          timeout=config.TIMEOUT(file_size))
            if (("error" in err.lower() or "failed to" in err.lower()) and "file exists" not in err.lower()) or (result is None):
                logging.error(f"Relocation upload of {relative_path} to {candidate} failed: {err}")
                continue
        # New copy is safely on the candidate. NOW remove the stale copy from the old remote.
        # deletefile/cleanup are quiet on success, so they go through repeat_command for the
        # full retry-rotate-quarantine-session-purge treatment. A None result means the op did
        # not run (quarantined or exhausted) -- treat that as "didn't happen, don't cleanup".
        del_result, del_err = repeat_command([config.RCLONE_PATH, "deletefile", f"{old_remote}:{relative_path}"])
        if (del_result is None) or ("error" in del_err.lower() or "failed to" in del_err.lower()):
            logging.error(f"Failed to delete stale {relative_path} from {old_remote} after relocation: {del_err}")
        else:
            # Empty the old remote's rubbish bin to actually reclaim the space.
            clean_result, clean_err = repeat_command([config.RCLONE_PATH, "cleanup", f"{old_remote}:"])
            if (clean_result is None) or (len(clean_err) > 0):
                logging.error(f"Remote cleanup of {old_remote} after relocation failed: {clean_err}")
        # Refresh both remotes' free space regardless of the delete outcome.
        get_remote_free_space(candidate, just_changed=True)
        get_remote_free_space(old_remote, just_changed=True)
        logging.info(f"Relocation of {relative_path} to {candidate} successful...")
        return candidate
    logging.warning(f"Could not relocate newer {relative_path}: no remote with room. Old copy left intact.")
    return None


def drain_replacements_queue(remote_index, sync_state):
    """Re-upload files Torrent-Ingest deliberately REPLACED in place (an anime quality
    upgrade) so the MEGA pool tracks the new version and the stale copy is purged.

    A replaced local file is otherwise invisible to the write-once sync: its mtime drift
    is absorbed as noise, so the pool would keep the OLD version forever. Torrent-Ingest
    appends each replacement to `config.REPLACEMENTS_QUEUE`; this drains it with the same
    gapless overwrite the One Pace churn class uses (copyto over the same remote + empty
    its rubbish bin, or relocate when the new file no longer fits). A handled entry is
    dropped so it never retriggers; a failed one is kept for the next cycle.

    Returns True if `remote_index` was mutated. The caller must OR this into its
    inventory-dirty flag: the updates below are made to the in-memory dict only, and
    `sync_cycle` skips its end-of-cycle flush unless something marked the index dirty.
    While acquisition is paused nothing else uploads, so without this the drain's
    updates were discarded at process exit and the tier engine kept seeing the stale
    pre-upload size -- refusing to evict, and re-deriving the same files as upgrades.
    """
    q = config.REPLACEMENTS_QUEUE
    if not q.exists():
        return False
    try:
        raw = q.read_text(encoding="utf-8")
    except OSError:
        return False
    paths = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and e.get("path"):
            paths.append(e["path"])
    if not paths:
        return False

    remaining = []
    changed = False
    for relative_path in paths:
        if relative_path not in remote_index:
            continue                        # not on any remote (or already gone) -> drop
        expected_local_path = get_expected_local_path(relative_path)
        if expected_local_path is None or not expected_local_path.exists():
            continue                        # local copy gone -> nothing to push
        remote, mod_time, _size = remote_index[relative_path]
        file_size = expected_local_path.stat().st_size

        handled = False
        if get_remote_free_space(remote) >= file_size:
            wait_while_streaming()          # yield to live playback before an overwrite
            logging.info(f"Re-uploading replaced {relative_path} over {remote}...")
            result, err = run_command(
                [config.RCLONE_PATH, "copyto", str(expected_local_path),
                 f"{remote}:{relative_path}", "--low-level-retries", "20", "--retries", "1"],
                timeout=config.TIMEOUT(file_size))
            if (("error" in err.lower() or "failed to" in err.lower())
                    and "file exists" not in err.lower()) or (result is None):
                if is_stale_session_error(err) and heal_stale_session(remote):
                    result, err = run_command(
                        [config.RCLONE_PATH, "copyto", str(expected_local_path),
                         f"{remote}:{relative_path}", "--low-level-retries", "20",
                         "--retries", "1"], timeout=config.TIMEOUT(file_size))
            if (("error" in err.lower() or "failed to" in err.lower())
                    and "file exists" not in err.lower()) or (result is None):
                logging.error(f"Overwrite-upload of replaced {relative_path} to {remote} "
                              f"failed: {err}")
            else:
                # The replaced copy lingers in MEGA's rubbish bin; empty it to reclaim space.
                clean_result, clean_err = repeat_command([config.RCLONE_PATH, "cleanup", f"{remote}:"])
                if (clean_result is None) or (len(clean_err) > 0):
                    logging.error(f"Remote cleanup of {remote} after replace failed: {clean_err}")
                live_mtime = os.path.getmtime(expected_local_path)
                sync_state[relative_path] = [live_mtime, live_mtime]
                remote_index[relative_path] = [remote,
                    datetime.fromtimestamp(live_mtime, tz=timezone.utc).isoformat(), file_size]
                get_remote_free_space(remote, just_changed=True)
                handled = True
                changed = True
                logging.info(f"Overwrite-upload of replaced {relative_path} successful.")

        if not handled:
            new_remote = relocate_newer_one_pace(relative_path, expected_local_path, remote, file_size)
            if new_remote is not None:
                live_mtime = os.path.getmtime(expected_local_path)
                sync_state[relative_path] = [live_mtime, live_mtime]
                remote_index[relative_path] = [new_remote,
                    datetime.fromtimestamp(live_mtime, tz=timezone.utc).isoformat(), file_size]
                handled = True
                changed = True

        if not handled:
            remaining.append(relative_path)

    # Rewrite the queue with only the still-pending paths (usually none).
    try:
        if remaining:
            tmp = q.with_suffix(q.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for p in remaining:
                    fh.write(json.dumps({"path": p}) + "\n")
            os.replace(tmp, q)
        else:
            q.unlink()
    except OSError:
        logging.warning("Could not rewrite the replacements queue; entries may be re-tried.")

    return changed


_remote_index_cache = None
_remote_index_at = 0.0
_free_space_at = 0.0
_last_inv_write = 0.0

# Paths already reported unplaceable (a single file bigger than any one MEGA account can
# hold). They can never fit, so the warning is logged once per process lifetime instead of
# once per cycle -- the busy-idle loop otherwise re-warns the same file every few seconds,
# flooding the log and making the daemon look broken when it is simply idle.
_unplaceable_warned: set[str] = set()


def _quarantine_unplaceable(local_path, relative_path):
    """Move a file that can never be placed out of the media root to UNPLACEABLE_DIR.

    Preserves the library-relative path so the owner sees exactly which file it was
    (re-encode it smaller and drop it back, or delete it). Same-volume os.replace makes
    the move instant; a cross-volume fallback degrades to a real copy so the SSD root is
    still freed. Returns the destination path, or None when the move could not be done
    (the file then stays put and is only warned -- never lost).
    """
    dest = config.UNPLACEABLE_DIR / relative_path
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(local_path, dest)
    except OSError:
        try:
            shutil.move(str(local_path), str(dest))
        except OSError as exc:
            logging.error(f"Could not quarantine unplaceable file '{relative_path}': {exc}")
            return None
    # Keep a running report so the owner (and fleet_health) can see what is parked here.
    try:
        report = config.UNPLACEABLE_DIR / "unplaceable.jsonl"
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("a", encoding="utf-8") as fh:
            try:
                size = dest.stat().st_size
            except OSError:
                size = None
            fh.write(json.dumps({
                "path": relative_path,
                "size_bytes": size,
                "moved_to": str(dest),
                "ts": time.time(),
            }) + "\n")
    except OSError:
        pass
    return dest

# Guards every piece of shared state the upload workers touch: the free-space ledger, the
# claimed-remote set, the pending work list, remote_index, sync_state, and the throttled
# inventory write. One lock rather than several because the critical sections are all
# microseconds of dict work -- the seconds-to-minutes of actual transfer happen outside it.
_upload_lock = threading.Lock()


def _persist_inventory(remote_index, force=False):
    """Write remote_inventory.json, throttled to once per 30s (or force). Called DURING the
    upload loop -- not just at cycle end -- so the pre-download daemon (which reads this
    file to know what is already on the pool) sees each batch of uploads promptly, even
    when a single upload cycle runs for a long time."""
    global _last_inv_write
    now = time.time()
    if not force and (now - _last_inv_write) < 30:
        return
    # ATOMIC, and both halves matter. A plain open(...,'w') TRUNCATES before writing, so this
    # ~12 MB file spends a real interval empty or half-written every 30 s during an upload
    # phase -- and `mediafs` and `predownload` both read it live. A reader landing inside that
    # window gets a JSONDecodeError on a file that is perfectly valid a moment later.
    #
    # It also makes the mtime a LIE, which matters now that mediafs reloads on mtime change:
    # truncation stamps a new mtime while the content is still the old (or no) inventory, so a
    # poller can latch a torn read as the current view. os.replace flips name -> new inode in
    # one step, so the mtime changes exactly when the complete content becomes visible.
    if write_json_atomic(config.REMOTE_INVENTORY_PATH, remote_index):
        _last_inv_write = now


def _is_on_external_drive(local_path):
    """True if this file lives on a discovered external drive rather than the SSD root."""
    try:
        local_path.relative_to(config.SSD_LIBRARY_ROOT)
        return False                     # under the SSD library root: the serving cache
    except ValueError:
        pass
    return get_root_for_path(local_path, config.local_media_roots()) is not None


def _drop_uploaded_local(local_path, local_files, relative_path=None, remote_index=None):
    """Remove a local media copy that the pool provably holds.

    Two independent policies, because the two tiers are being managed in opposite
    directions:

    * **SSD library root** -- `GRADUATED_UPLOAD_THEN_REMOVE`, normally False. The SSD is the
      serving cache; predownload.py owns its space via inventory-guarded eviction.
    * **External drives** -- `DELETE_DRIVE_COPY_AFTER_UPLOAD`, True. A drive is being
      retired, so its content goes as soon as the pool holds it, and the drive emptying is
      the signal that it can be unplugged.

    A drive copy is deleted only on an EXACT size match against the inventory
    (`DRIVE_DELETE_REQUIRE_EXACT_SIZE`); see that setting for the two populations this
    protects. Sidecars are never in `local_files`, so they are untouched either way.
    """
    on_drive = _is_on_external_drive(local_path)
    if on_drive:
        if not getattr(config, "DELETE_DRIVE_COPY_AFTER_UPLOAD", False):
            return
        if getattr(config, "DRIVE_DELETE_REQUIRE_EXACT_SIZE", True):
            entry = (remote_index or {}).get(relative_path)
            if not entry:
                return                          # not proven on the pool -- keep it
            try:
                local_size = local_path.stat().st_size
            except OSError:
                return
            if local_size != entry[2]:
                _note_drive_residue(relative_path, local_size, entry[2])
                return
    elif not getattr(config, "GRADUATED_UPLOAD_THEN_REMOVE", False):
        return

    # Before deleting ANY local copy, force the inventory to disk. The deletion is
    # "proven safe" only by that file's entry in remote_inventory.json, and the in-memory
    # dict is not durable -- a crash between the (throttled) _persist_inventory call in the
    # upload path and this unlink would leave the local copy gone while its inventory entry
    # was never written, producing exactly the "marked uploaded but absent from the pool
    # and the mount" bug. Force-flushing here makes the proof durable first, deletion second.
    if remote_index is not None:
        _persist_inventory(remote_index, force=True)

    try:
        local_path.unlink()
        with _upload_lock:
            local_files.discard(local_path)
        if on_drive:
            _prune_empty_parents(local_path)
        if relative_path:
            where = "drive" if on_drive else "local"
            logging.info(f"Removed {where} copy of '{relative_path}' (held on the pool).")
    except OSError as e:
        if relative_path:
            logging.error(f"Could not remove local copy of '{relative_path}': {e}")


def _report_drive_state():
    """Log what remains on each external drive and why, once per cycle.

    'The drive is empty' is the signal that it can be unplugged for good, so anything that
    cannot be deleted has to be visible rather than silently skipped. Residue is reported
    with its cause: files the pool holds at a different size (see
    DRIVE_DELETE_REQUIRE_EXACT_SIZE) and files not yet uploaded at all.
    """
    if not getattr(config, "DELETE_DRIVE_COPY_AFTER_UPLOAD", False):
        return
    drives = config.discover_library_drives()
    if not drives:
        return
    G = 1024 ** 3
    for root in drives:
        files = bytes_ = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for name in filenames:
                if name.startswith('.'):
                    continue
                if os.path.splitext(name)[1].lower() not in MEDIA_EXTENSIONS:
                    continue
                files += 1
                try:
                    bytes_ += (Path(dirpath) / name).stat().st_size
                except OSError:
                    pass
        if files == 0:
            logging.info(f"drive {root} holds NO media -- it can be unplugged.")
            continue
        with _upload_lock:
            larger, smaller = _drive_residue["larger"], _drive_residue["smaller"]
        note = ""
        if larger or smaller:
            note = (f"; {larger} larger / {smaller} smaller than the pool copy this cycle "
                    f"(kept -- size mismatch, see DRIVE_DELETE_REQUIRE_EXACT_SIZE)")
        logging.info(f"drive {root}: {files} media file(s), {bytes_ / G:.0f} GB remaining{note}")
    with _upload_lock:
        _drive_residue.update({"larger": 0, "smaller": 0, "bytes": 0})


_drive_residue = {"larger": 0, "smaller": 0, "bytes": 0}


def _note_drive_residue(relative_path, local_size, pool_size):
    """Record a drive file the pool holds at a DIFFERENT size, so it is never silently
    skipped. These are what stop a drive from reaching empty."""
    with _upload_lock:
        key = "larger" if local_size > pool_size else "smaller"
        _drive_residue[key] += 1
        _drive_residue["bytes"] += local_size
    logging.debug(f"drive residue ({'larger' if local_size > pool_size else 'smaller'}): "
                  f"{relative_path} local={local_size} pool={pool_size}")


def _prune_empty_parents(path):
    """Remove directories emptied by a drive deletion, stopping at the drive's library root
    so the root itself and any non-empty parent survive."""
    root = get_root_for_path(path, config.local_media_roots())
    if root is None:
        return
    parent = path.parent
    while parent != root and root in parent.parents:
        try:
            next(parent.iterdir())
            return                       # still holds something
        except StopIteration:
            pass
        except OSError:
            return
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _remote_has(remote, relative_path, expected_size):
    """True if `remote` holds `relative_path` at exactly `expected_size`.

    Used to tell a genuine upload failure from a local one. rclone is killed by
    run_command's timeout, and a killed process says nothing about what MEGA received --
    the bytes may all be there. Re-uploading in that case does not overwrite: MEGA keys on
    node ids, so the same path can hold several nodes, which wastes quota and breaks the
    single-residence invariant.

    Deliberately conservative: any doubt (a failed listing, a size mismatch) answers False,
    so the caller retries. A needless retry costs bandwidth; a wrong "it is there" would
    lose the file.
    """
    out, _err = repeat_command([config.RCLONE_PATH, "lsjson", f"{remote}:{relative_path}"],
                               rotate=False)
    if not out:
        return False
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return False
    return any((not r.get("IsDir")) and r.get("Size") == expected_size for r in rows)


def _upload_phase(local_files, remote_index, sync_state):
    """Upload every local file not yet on a remote, config.UPLOAD_WORKERS at a time, each
    worker pinned to a DIFFERENT MEGA account. Returns True if the index changed.

    MEGA throttles per account and rclone's mega backend pushes a file over one connection,
    so a single transfer is capped far below the link. N concurrent transfers to N distinct
    accounts scale nearly linearly up to the exit-node ceiling (§ config.UPLOAD_WORKERS).

    Three things are load-bearing about how the concurrency is arranged:

    * **One remote, one in-flight upload.** _claim() debits the in-memory free-space ledger
      under the lock at claim time, so two workers can never both be admitted against the
      same account's bytes; a failed upload refunds it.
    * **No network calls on the hot path.** The old loop called get_remote_free_space()
      per candidate remote and again after every success -- an `rclone about` round trip
      each time, issued through repeat_command, which is allowed to ROTATE the exit node on
      failure. A rotation resets every TCP connection on the machine, so with N transfers
      in flight it kills N-1 innocent uploads to re-check one account's quota. The ledger is seeded once per phase and reconciled by the periodic rescan.
    * **Largest file first.** A big file left to the tail of a cycle is the one that
      straddles the next rescan; starting it first lets smaller files fill the other slots
      around it.
    """
    pending = []
    for local_path in list(local_files):
        containing_root = get_root_for_path(local_path, config.local_media_roots())
        if not containing_root:
            continue
        relative_path = str(local_path.relative_to(containing_root))
        if relative_path in remote_index:
            # Already on a remote -- nothing to upload. A DRIVE copy is dropped here (the
            # drive is being drained, and most of its content was uploaded on earlier
            # cycles, so this is the path that actually empties it); an SSD copy is dropped
            # only under GRADUATED_UPLOAD_THEN_REMOVE.
            _drop_uploaded_local(local_path, local_files, relative_path, remote_index)
            continue
        try:
            file_size = local_path.stat().st_size
        except OSError:
            continue
        # A single file larger than one account's usable cap can never be placed: each
        # remote is capped at REMOTE_CAP_BYTES (minus the fill margin) and a file cannot
        # span accounts. Auto-provisioning is no help either -- a fresh account is the same
        # 20 GiB -- so skip it with a specific warning instead of (a) letting it drive the
        # provisioner to mint accounts that can't hold it and (b) retrying it every cycle
        # under the generic "no suitable remote" message.
        if file_size + config.REMOTE_FILL_MARGIN_BYTES > config.REMOTE_CAP_BYTES:
            # A single file bigger than any one MEGA account's usable cap can never be
            # placed (a file cannot span accounts). Merely warning and re-skipping every
            # cycle left it squatting on the SSD forever: it is "on no remote", so
            # predownload cannot evict it, and it locks a chunk of the serving cache out
            # of the disk-budget admission. MOVE it out of the media root to a quarantine
            # dir (same volume -> instant rename), preserving its relative path so the
            # owner sees exactly what it was, and report it.
            dest = _quarantine_unplaceable(local_path, relative_path)
            if dest is not None:
                with _upload_lock:
                    local_files.discard(local_path)
                logging.warning(
                    f"Unplaceable '{relative_path}': {file_size / 2**30:.2f} GiB exceeds a single "
                    f"MEGA account's usable cap "
                    f"({(config.REMOTE_CAP_BYTES - config.REMOTE_FILL_MARGIN_BYTES) / 2**30:.1f} GiB). "
                    f"One file cannot span accounts, so no remote can ever hold it. "
                    f"Moved out of the media root to '{dest}' -- re-encode (split/shrink) "
                    f"or delete it there.")
            continue
        # (path, relative path, size, fast-failure attempts, full-budget timeouts). The last two
        # are counted separately on purpose -- see config.MAX_UPLOAD_TIMEOUTS_PER_FILE.
        pending.append((local_path, relative_path, file_size, 0, 0))

    if not pending:
        return False

    pending.sort(key=lambda item: item[2])   # popped from the end -> largest first
    ledger = free_space_ledger(REMOTES, remote_index)
    # Bytes we believe are on each remote, seeded from the inventory and incremented on every
    # success. The ledger counts DOWN from a snapshot and so inherits that snapshot's age; this
    # counts UP from the inventory, which gains a row the moment a file lands, so it stays
    # correct across phase restarts. It is what keeps the live resync in _claim honest.
    placed = inventory_bytes_by_remote(remote_index)
    busy = set()
    index_dirty = False
    pending_bytes = sum(item[2] for item in pending)
    _resynced = {}          # remote -> last time its ledger figure was re-read live
    _excluded = config.upload_excluded_remotes()
    # remote -> (unbench_at, ledger balance to restore). A remote whose session just died is
    # parked here instead of being left claimable. Without it the allocator re-offers a dead
    # account instantly and forever: it fails in under a second, _release refunds the whole
    # reservation, so its balance never drops and it is immediately the best candidate again.
    # That is how one dead account absorbed 5,949 of ~7,750 upload attempts in a single day
    # while every healthy account was still uploading normally.
    _benched = {}
    uploaded_bytes = 0
    # Upload timeouts since the last SUCCESSFUL upload, across all workers. A slow exit node is
    # invisible per-file -- each worker just sees its own transfer killed at the timeout, which
    # looks exactly like one throttled account -- and only becomes legible in aggregate. See
    # _note_upload_timeout.
    consecutive_timeouts = 0
    phase_start = time.time()
    stop_provisioner = threading.Event()

    def _provisioner():
        """Keep pool capacity ahead of the uploader for as long as the phase runs.

        Capacity has to be checked DURING the phase, not once per cycle: the pool drains at
        ~68 GB/h and a large backlog keeps one cycle running for days, so a per-cycle check
        would fire long after the pool ran dry. Every upload past that point fails with "no
        suitable remote found", which is only a per-file warning.

        Runs on its own thread so a provisioning run (account registration + IMAP
        confirmation + a git push, i.e. minutes) never blocks a transfer. Reads PLACEABLE
        free space from the live ledger (each remote's raw free minus its fill margin), so
        the check itself costs no network I/O and cannot be fooled by a pool that is full on
        paper but fragmented into slivers too small to place anything.
        """
        while not stop_provisioner.wait(config.POOL_PROVISION_CHECK_SEC):
            try:
                with _upload_lock:
                    free_now = mega_accounts.placeable_free_sum(ledger)
                    left = sum(item[2] for item in pending)
                    done = uploaded_bytes
                elapsed = max(1.0, time.time() - phase_start)
                made = mega_accounts.ensure_capacity(
                    free=free_now, pending=left, drain_bps=done / elapsed)
                if made:
                    # New accounts must become usable NOW, not next cycle: refresh the
                    # remote list and seed the ledger with the new rows only (the existing
                    # entries are the live debited balances -- reseeding them wholesale
                    # would erase every in-flight reservation).
                    fresh = find_mega_remotes()
                    # Seed off a SNAPSHOT of the index. free_space_ledger iterates it, and the
                    # workers insert into it on every success -- iterating it unlocked from this
                    # thread raises "dictionary changed size during iteration", which this
                    # method's own except clause would swallow into a log line while quietly
                    # leaving every new account seeded at zero and therefore unusable.
                    with _upload_lock:
                        snapshot = dict(remote_index)
                    seed = free_space_ledger(fresh, snapshot)
                    with _upload_lock:
                        REMOTES[:] = fresh
                        for r in fresh:
                            ledger.setdefault(r, seed.get(r, 0))
                    logging.info(f"provisioned {made} account(s); pool now {len(fresh)} remotes")
            except Exception as e:                                  # noqa: BLE001
                logging.error(f"provisioning check error: {e}")

    def _bench(remote, seconds):
        """Take `remote` out of the allocator for `seconds` (math.inf = rest of the phase).

        Caller must hold _upload_lock. The remote's ledger balance is preserved rather than
        zeroed, so a remote that comes back after a successful heal returns with the capacity
        it actually has -- a zeroed balance would be indistinguishable from a full account and
        would strand its free space until the next cycle's rescan.

        Benching an already-benched remote is a no-op, and that is not merely an optimisation:
        the second call would save a balance this function has already set to 0, so the real
        figure would be lost and the remote would return from its bench looking full. A remote
        that needs a longer bench gets one the normal way -- its current bench expires, the next
        failure finds the heal budget spent, and it is benched again with math.inf.
        """
        if remote in _benched:
            return
        _benched[remote] = (time.time() + seconds, ledger.get(remote, 0))
        ledger[remote] = 0
        if seconds == math.inf:
            logging.warning(f"Benching {remote} for the rest of this upload phase.")
        else:
            logging.info(f"Benching {remote} for {seconds:.0f}s while its session re-authenticates.")

    def _release_expired_benches():
        """Return benched remotes to the allocator once their bench expires.

        Caller must hold _upload_lock.
        """
        if not _benched:
            return
        now = time.time()
        for remote in [r for r, (until, _) in _benched.items() if now >= until]:
            _, saved = _benched.pop(remote)
            ledger[remote] = saved
            logging.info(f"{remote} returning to the upload pool after its session heal.")

    def _claim(file_size):
        """Reserve an idle remote with room, debiting the ledger. None if none is free.

        Three guards beyond "does it fit", all of which exist because the ledger is arithmetic
        on a snapshot and can only drift ABOVE the truth (§ REMOTE_FILL_MARGIN_BYTES):

        * **Never fill to the last byte.** A remote must retain REMOTE_FILL_MARGIN_BYTES
          after the claim, so rubbish-bin lag, a timed-out-but-landed upload, or a capacity
          that is not exactly REMOTE_CAP_BYTES cannot tip it over quota.
        * **Verify live when a claim could hide a breach.** The ledger's figure is re-read
          from the source of truth first if EITHER test trips, both measured against
          LEDGER_RESYNC_WHEN_BELOW and both rate-limited per remote by LEDGER_RESYNC_SEC:

          - the claim would leave the ledger below it (the remote is nearly full), or
          - the claim itself is bigger than it (the file is large enough that ordinary drift
            is sufficient to breach the cap).

          The second test is the one this account needed, and the first test measures the
          balance AFTER the claim rather than before it, because drift is only dangerous in
          proportion to what is about to be spent. A remote reported at 10.2 GiB free clears
          any pre-claim threshold comfortably; a 7.98 GiB claim against it then lands on the
          15.6 GiB the snapshot had not caught up with, and the account is 3.5 GiB past a
          20 GiB cap having never once looked nearly full. Post-claim it still reads 2.26 GiB
          -- above the 2 GiB threshold, so the balance test alone misses it by 260 MB. Size
          alone is the signal that catches it every time.
        * **Never exceed REMOTE_CAP_BYTES by our own accounting.** `placed` counts up from
          the inventory rather than down from a snapshot, so it is immune to snapshot age;
          it bounds both the seed and every live resync. This is the guard that holds when
          the other two are inside their tolerances.
        """
        margin = getattr(config, "REMOTE_FILL_MARGIN_BYTES", 0)
        need = file_size + margin
        resync_below = getattr(config, "LEDGER_RESYNC_WHEN_BELOW", 0)
        while True:
            with _upload_lock:
                _release_expired_benches()
                candidate = None
                for remote in REMOTES:
                    if remote in busy or remote in _benched or ledger.get(remote, 0) < need:
                        continue
                    candidate = remote
                    break
                if candidate is None:
                    return None
                # Hold it while we decide, so no other worker can also pick it.
                busy.add(candidate)
                risky = ((ledger.get(candidate, 0) - need) < resync_below
                         or need > resync_below)
                stale = (risky and (time.time() - _resynced.get(candidate, 0.0))
                         > getattr(config, "LEDGER_RESYNC_SEC", 900))
                if not stale:
                    ledger[candidate] -= file_size
                    return candidate
                placed_now = placed.get(candidate, 0)

            # Nearly full, or a claim big enough that drift alone could breach the cap: confirm
            # against the remote itself before writing to it. Done OUTSIDE the lock -- it is a
            # network round trip -- with the remote still held in `busy`, so nothing else claims
            # it meanwhile.
            try:
                real = get_remote_free_space(candidate, just_changed=True, placed=placed_now)
            except Exception:                                   # noqa: BLE001
                real = 0
            with _upload_lock:
                _resynced[candidate] = time.time()
                # An excluded remote stays at 0 however much room it reports.
                ledger[candidate] = 0 if candidate in _excluded else real
                if real >= need:
                    ledger[candidate] -= file_size
                    return candidate
                busy.discard(candidate)          # genuinely full; try the next remote

    def _release(remote, file_size, ok):
        with _upload_lock:
            busy.discard(remote)
            if not ok:
                ledger[remote] += file_size      # refund a reservation that never landed

    def _note_upload_timeout(relative_path):
        """Count an upload killed by its transfer budget, and rotate the exit node once enough
        of them have stacked up without a success in between.

        A timeout is the only failure shape that indicts the EXIT NODE rather than the account,
        and it is also the one nothing else handles: `quota` zeroes the ledger entry, a stale
        session heals and benches the remote, but a transfer that simply never finished gets
        re-queued against a different account -- which changes nothing when the node underneath
        all of them is the problem. That is how a bad node costs a whole cycle: 16 workers each
        burning the full budget, every file exhausting MAX_DOWNLOAD_TRIES against a link that
        cannot deliver, 0 uploads in 1 h 43 m and 69 files abandoned.

        Why a counter and not an immediate rotation: a switch resets every TCP connection on the
        machine, so rotating on one timeout destroys the other 15 in-flight transfers to fix one
        file. That trade is only worth making once the evidence says the node is bad for
        everyone, and consecutive-timeouts-without-a-success is exactly that evidence -- on a
        healthy node a success lands between the isolated timeouts and clears the count.

        Counting only rotations that actually happened is load-bearing. `rotate_exit_node()`
        declines when another process rotated within ROTATE_MIN_INTERVAL_SEC, and resetting the
        count on a declined call would throw away the evidence and leave the daemon on the bad
        node until it re-earned the threshold from scratch.

        Returns:
            bool: True if the exit node was actually changed, which tells the caller this file's
                next attempt runs over a different link and so should not be charged an attempt.
        """
        nonlocal consecutive_timeouts
        with _upload_lock:
            consecutive_timeouts += 1
            count = consecutive_timeouts
            if count < config.UPLOAD_TIMEOUT_ROTATE_THRESHOLD:
                return False
        logging.warning(f"{count} upload timeouts with no success in between (latest: "
                        f"'{relative_path}'). The exit node is the common factor; rotating.")
        if not rotate_exit_node():
            return False
        with _upload_lock:
            consecutive_timeouts = 0
        return True

    def _worker():
        nonlocal index_dirty
        while True:
            with _upload_lock:
                if not pending:
                    return
                local_path, relative_path, file_size, attempts, timeouts = pending.pop()

            remote = None
            for _ in range(config.UPLOAD_CLAIM_MAX_WAITS):
                remote = _claim(file_size)
                if remote is not None:
                    break
                time.sleep(config.UPLOAD_CLAIM_WAIT_SEC)
            if remote is None:
                logging.warning(f"Could not upload '{relative_path}'. No suitable remote found.")
                continue

            logging.info(f"Uploading '{relative_path}' to {remote}...")
            result, err = run_command(
                [config.RCLONE_PATH, "copyto", str(local_path), f"{remote}:{relative_path}",
                 *config.UPLOAD_RCLONE_FLAGS],
                timeout=config.TIMEOUT(file_size))

            failed = ((("error" in err.lower() or "failed to" in err.lower())
                       and "file exists" not in err.lower())
                      or (result is None))

            # A local failure does NOT prove the upload failed. `run_command` kills rclone on
            # timeout, but MEGA may already hold every byte -- and MEGA keys on node ids, not
            # paths, so simply re-uploading does not replace the earlier attempt, it adds a
            # SECOND node at the same path. That is how a path ends up with three copies,
            # how the single-residence invariant breaks, and how an account is pushed past
            # its quota by content it already had.
            #
            # So before treating a failure as real, ask the remote. A confirmed file of the
            # right size means the transfer succeeded and only the local process died.
            if failed:
                if _remote_has(remote, relative_path, file_size):
                    logging.info(f"'{relative_path}' is present on {remote} at the right size "
                                 f"despite a local failure ({err.strip()[:60]}); counting it "
                                 f"as uploaded.")
                    failed = False
            _release(remote, file_size, ok=not failed)

            if failed:
                logging.error(f"Upload of '{relative_path}' to {remote} failed: {err}")
                if "quota" in err.lower():
                    # The account had less room than the ledger believed. Zero it for the
                    # rest of this phase rather than re-querying it -- the rescan will
                    # restore a real number, and an `about` here would rotate the exit node
                    # out from under every other worker's transfer.
                    with _upload_lock:
                        ledger[remote] = 0
                elif err == "timeout":
                    # run_command's sentinel for "I killed rclone at config.TIMEOUT(size)".
                    # Matched exactly rather than by substring: rclone's own stderr says
                    # "timeout" for per-operation HTTP timeouts that a plain retry clears, and
                    # treating those as node-level evidence would rotate on ordinary blips.
                    _note_upload_timeout(relative_path)
                    # A timeout never charges the fast-failure attempt budget, whether or not it
                    # was the one that tripped a rotation. It carries no verdict on the file --
                    # it only says the transfer did not fit in its wall clock, which is a
                    # statement about the link. It gets its own budget instead.
                    if timeouts + 1 < config.MAX_UPLOAD_TIMEOUTS_PER_FILE:
                        with _upload_lock:
                            # insert(0), NOT append: workers pop from the END, so appending
                            # hands this same file straight back to the next free worker. On a
                            # bad node that spends its entire timeout budget in consecutive
                            # tries against the very link that is failing it -- the budget is
                            # gone before any rotation has had time to help, which defeats the
                            # point of not charging an attempt. Measured on a node that recovered
                            # after 15 attempts: 2 of 6 files were still abandoned with append,
                            # and none with insert(0). Going to the back of the queue lets the
                            # rest of the backlog absorb the failures while rotation works.
                            pending.insert(0, (local_path, relative_path, file_size,
                                               attempts, timeouts + 1))
                    else:
                        logging.warning(f"Giving up on '{relative_path}' this cycle after "
                                        f"{timeouts + 1} timeouts; either the file cannot be "
                                        f"transferred in its budget or every exit node is bad.")
                    continue
                elif is_stale_session_error(err):
                    # The account is fine; its cached session is dead. Heal it and bench it
                    # while the next login lands. The file keeps its attempt budget -- it was
                    # never given a real chance, and charging it for the account's fault is
                    # how a healthy file ends up abandoned for the cycle.
                    healable = heal_stale_session(remote)
                    with _upload_lock:
                        _bench(remote, config.UPLOAD_BENCH_SEC if healable else math.inf)
                        pending.append((local_path, relative_path, file_size, attempts, timeouts))
                    continue
                # Re-queue so a different account gets a turn, but bounded: an unbounded
                # re-queue lets one poison file (a bad read off a dying drive, a path MEGA
                # refuses) hold every worker in a permanent retry spin. Whatever is left is
                # retried next cycle anyway.
                if attempts + 1 < config.MAX_DOWNLOAD_TRIES:
                    with _upload_lock:
                        pending.append((local_path, relative_path, file_size,
                                        attempts + 1, timeouts))
                else:
                    logging.warning(f"Giving up on '{relative_path}' this cycle "
                                    f"after {attempts + 1} attempts.")
                continue

            logging.info(f"Upload of '{relative_path}' successful.")
            mtime = os.path.getmtime(local_path)
            nonlocal uploaded_bytes, consecutive_timeouts
            with _upload_lock:
                uploaded_bytes += file_size
                # Bytes now on that remote by our own count. This is the figure the resync
                # clamps against, and unlike the ledger it does not inherit the age of any
                # snapshot -- so it stays right however many phases run between two sweeps.
                placed[remote] = placed.get(remote, 0) + file_size
                # A completed transfer proves the node is carrying payload, so whatever timeouts
                # preceded it were about individual files or accounts. Clearing the count here is
                # what keeps the threshold a measure of SYSTEMIC failure rather than a slow tally
                # of every unlucky transfer the phase ever had.
                consecutive_timeouts = 0
                # Keep the cached index + inventory current so the next file's skip-check
                # and the predownload daemon (which reads remote_inventory.json) see this
                # upload at once, without waiting for the next full rescan.
                remote_index[relative_path] = [remote,
                    datetime.now(timezone.utc).isoformat(), file_size]
                # Real-mtime baseline so the update phase stays inert (ordinary drift is
                # absorbed; only One Pace churns).
                sync_state[relative_path] = [mtime, mtime]
                index_dirty = True
                _persist_inventory(remote_index)   # throttled -> predownload sees it promptly
            _drop_uploaded_local(local_path, local_files, relative_path, remote_index)

    workers = max(1, min(config.UPLOAD_WORKERS, len(pending), len(REMOTES)))
    logging.info(f"Upload phase: {len(pending)} file(s) / {pending_bytes / 1024**3:.0f} GB "
                 f"pending, {workers} worker(s).")

    # Provision for the whole backlog up front, before a byte moves: the deficit is knowable
    # now (pending vs pool free), and waiting for free space to fall through a floor mid-run
    # is how a large backlog stalls with nothing but per-file warnings to show for it.
    try:
        mega_accounts.ensure_capacity(free=mega_accounts.placeable_free_sum(ledger),
                                      pending=pending_bytes)
    except Exception as e:                                          # noqa: BLE001
        logging.error(f"initial provisioning check error: {e}")

    prov = threading.Thread(target=_provisioner, daemon=True, name="provisioner")
    prov.start()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for future in [pool.submit(_worker) for _ in range(workers)]:
                try:
                    future.result()
                except Exception as e:                              # noqa: BLE001
                    # A worker must never take the cycle down with it -- the remaining files
                    # are retried next cycle regardless.
                    logging.error(f"Upload worker error: {e}", exc_info=True)
    finally:
        stop_provisioner.set()
        prov.join(timeout=5)
    return index_dirty


def sync_cycle():
    global _remote_index_cache, _remote_index_at, _free_space_at
    local_files = find_local_files()

    # The full remote scan (lsjson + about on every remote) dominates cycle time, so do it
    # only on the REMOTE_RESCAN_SEC cadence and reuse the cached index between rebuilds --
    # uploads below keep the cache + inventory current incrementally. This is what lets the
    # migration backlog upload back-to-back instead of once per multi-minute scan.
    now = time.time()
    rescan = (_remote_index_cache is None
              or (now - _remote_index_at) >= config.REMOTE_RESCAN_SEC)
    if rescan:
        _remote_index_cache = build_remote_index(REMOTES)
        _remote_index_at = now
    remote_index = _remote_index_cache

    if rescan or (now - _free_space_at) >= config.REMOTE_RESCAN_SEC:
        # Parallel, and writes free_space.json exactly once. A per-remote loop costs ~1.77 s
        # per account of pure latency with the uploader idle, and rewrites the whole JSON
        # file on each call.
        t0 = time.time()
        ok = refresh_free_space(REMOTES)
        logging.info(f"free-space sweep: {ok}/{len(REMOTES)} remotes in {time.time() - t0:.0f}s")
        _free_space_at = now
    index_dirty = False

    # Load the sync state which will be useful for the update phase
    sync_state_path = config.SYNC_STATE_PATH
    if not sync_state_path.exists():
        sync_state = {}
    else:
        with open(sync_state_path, 'r') as f:
            sync_state = json.load(f)
    for relative_path, (remote, mod_time, size) in remote_index.items():
        if relative_path in sync_state.keys():
            # No need for a baseline
            continue
            
        expected_local_path = get_expected_local_path(relative_path)
        if (expected_local_path is not None) and expected_local_path.exists():
            # Record current local mod time and remote mod time as baseline
            local_mod_time = os.path.getmtime(expected_local_path)
            remote_mod_time = datetime.fromisoformat(mod_time.replace("Z", "+00:00")).timestamp()
            if abs(local_mod_time - remote_mod_time) >= config.UPDATE_THRESHOLD:
                if local_mod_time > remote_mod_time:
                    # Local newer than remote - set to 0 so a later live local mtime triggers
                    # a re-upload (only ever acted on for the One Pace churn class).
                    sync_state[relative_path] = [0, remote_mod_time]
                else:
                    # Remote newer than local - set to 0 so a re-download is triggered
                    # (only ever acted on for the One Pace churn class).
                    sync_state[relative_path] = [local_mod_time, 0]
            else: # Files in sync
                sync_state[relative_path] = [local_mod_time, remote_mod_time]

    # Torrent-Ingest's anime quality upgrades replace local files in place; re-upload
    # those over their stale MEGA copies BEFORE the drift-absorbing update phase runs,
    # so the pool tracks the new version and the old copy is purged from the rubbish bin.
    index_dirty = drain_replacements_queue(remote_index, sync_state) or index_dirty

    # --- UPDATE PHASE ---
    # One Pace is the lone churn class: newer re-cuts must replace older versions.
    # Replacement is done IN PLACE (copyto over the same path, then empty the remote's
    # rubbish bin) rather than delete-then-reupload, so there is never a window in which
    # the remote file is absent for another host to refill with a stale copy.
    for relative_path, (remote, mod_time, size) in remote_index.items():
        # If the local or remote mod times differ, this file may need to be reuploaded or redownloaded
        if relative_path not in sync_state.keys():
            # Brand new - either hasn't been uploaded or hasn't been downloaded and a different phase will handle this
            continue

        is_one_pace = relative_path.startswith(config.ONE_PACE_PREFIX)

        # Get mod times
        (local_mod_time, remote_mod_time) = sync_state[relative_path]

        expected_local_path = get_expected_local_path(relative_path)
        if expected_local_path is None:
            # File we don't care about
            continue

        # Does the file exist? If so, check if it's an old or new version
        if expected_local_path.exists():
            live_local_mod_time = os.path.getmtime(expected_local_path)
            if mod_time is None:
                # Should never happen - just being pedantic
                continue
            live_remote_mod_time = datetime.fromisoformat(mod_time.replace("Z", "+00:00")).timestamp()
            if abs(local_mod_time - live_local_mod_time) >= config.UPDATE_THRESHOLD and live_local_mod_time > local_mod_time:
                # The local file is newer than our baseline.
                if not is_one_pace:
                    # Not a churn file - absorb the drift, never re-upload.
                    sync_state[relative_path] = [live_local_mod_time, remote_mod_time]
                else:
                    # One Pace: a newer local re-cut must replace the older remote copy.
                    # Preferred path is an in-place overwrite on the SAME remote (gapless and
                    # idempotent). But a larger re-cut may no longer fit there: an in-place
                    # copyto transiently holds BOTH the old copy (parked in the rubbish bin)
                    # and the new one before cleanup reclaims the old, so the remote needs free
                    # space >= the FULL new size, not merely the size delta. When it does not
                    # fit we relocate -- upload to a DIFFERENT remote that has room FIRST, then
                    # delete the old copy. Upload-before-delete keeps it gapless (no stale-copy
                    # race). Both hosts do this whenever their local copy is newer.
                    file_size = expected_local_path.stat().st_size
                    if get_remote_free_space(remote) >= file_size:
                        wait_while_streaming()   # yield to live playback before a One Pace overwrite
                        logging.info(f"Uploading newer local version of {relative_path} over {remote}...")
                        result, err = run_command([config.RCLONE_PATH, "copyto", str(expected_local_path), f"{remote}:{relative_path}", "--low-level-retries", "20", "--retries", "1"],
                                                timeout=config.TIMEOUT(file_size))
                        if ((("error" in err.lower() or "failed to" in err.lower()) and "file exists" not in err.lower()) or (result is None)) \
                                and is_stale_session_error(err) and heal_stale_session(remote):
                            # The only remote that can hold this re-cut gaplessly is this one, so
                            # a dead session here means waiting a whole cycle for the replacement.
                            # Heal and retry once instead.
                            result, err = run_command([config.RCLONE_PATH, "copyto", str(expected_local_path), f"{remote}:{relative_path}", "--low-level-retries", "20", "--retries", "1"],
                                                    timeout=config.TIMEOUT(file_size))
                        if (("error" in err.lower() or "failed to" in err.lower()) and "file exists" not in err.lower()) or (result is None):
                            logging.error(f"Overwrite-upload of {relative_path} to {remote} failed: {err}")
                        else:
                            # The replaced copy lingers in MEGA's rubbish bin; empty it to reclaim the
                            # space. cleanup is quiet on success, so it goes through repeat_command for
                            # retry-rotate-quarantine; a None result means it did not run.
                            clean_result, clean_err = repeat_command([config.RCLONE_PATH, "cleanup", f"{remote}:"])
                            if (clean_result is None) or (len(clean_err) > 0):
                                logging.error(f"Remote cleanup of {remote} after overwrite failed: {clean_err}")
                            # copyto stamps the remote mtime to match the source, so both baselines track the new local mtime.
                            sync_state[relative_path] = [live_local_mod_time, live_local_mod_time]
                            get_remote_free_space(remote, just_changed=True)
                            logging.info("Overwrite-upload successful...")
                    else:
                        # New version no longer fits on its current remote -- relocate it to
                        # one with room, then drop the stale copy (gapless, see helper).
                        new_remote = relocate_newer_one_pace(relative_path, expected_local_path, remote, file_size)
                        if new_remote is not None:
                            # copyto stamped the new remote's mtime to the source mtime.
                            sync_state[relative_path] = [live_local_mod_time, live_local_mod_time]
                            # Keep the in-memory index consistent for the rest of THIS cycle:
                            # the path now lives on new_remote, not the old one.
                            remote_index[relative_path] = [new_remote, datetime.fromtimestamp(live_local_mod_time, tz=timezone.utc).isoformat(), file_size]
            elif abs(live_remote_mod_time - remote_mod_time) >= config.UPDATE_THRESHOLD and live_remote_mod_time > remote_mod_time:
                # The remote file is newer than our baseline.
                if not is_one_pace:
                    # Not a churn file -- absorb the drift, never overwrite locally.
                    sync_state[relative_path] = [local_mod_time, live_remote_mod_time]
                else:
                    # One Pace: overwrite the stale local copy with the fresh remote version.
                    wait_while_streaming()   # yield to live playback before a One Pace re-download
                    logging.info(f"Overwriting {expected_local_path} with {remote}:{relative_path}...")
                    if chunked_download(remote, relative_path, expected_local_path, size):
                        # Download successful
                        sync_state[relative_path] = [os.path.getmtime(expected_local_path), live_remote_mod_time]
                        logging.info("Overwrite successful...")

    # --- DOWNLOAD PHASE ---
    # Skipped entirely when the library is served virtually via mediafs: evicted
    # files are missing from the SSD library root ON PURPOSE and are hydrated on demand by the
    # tier engine, so bulk-downloading every "missing" file here would just re-fill
    # exactly what eviction freed. In that mode the Mini is upload-only (One Pace
    # still churns via the update phase, which is unaffected).
    for relative_path, (remote, remote_mod_time, size) in remote_index.items():
        if config.SERVE_VIA_MEDIAFS:
            break
        expected_local_path = get_expected_local_path(relative_path)
        if expected_local_path is None:
            continue

        if not expected_local_path.exists():
            logging.info(f"File '{relative_path}' missing locally. Downloading from {remote}...")
            # Ensure local directory structure is preserved
            expected_local_path.parent.mkdir(parents=True, exist_ok=True)
            if chunked_download(remote, relative_path, expected_local_path, size):
                sync_state[relative_path] = [os.path.getmtime(expected_local_path), datetime.fromisoformat(remote_mod_time.replace("Z", "+00:00")).timestamp()]
                local_files.add(expected_local_path)
                logging.info("Download successful...")
            else:
                # Download failed; chunked_download already removed the file stub. Now
                # prune the directories we created so a failed fetch leaves no trace.
                # "Empty" is the wrong test: Jellyfin litters .nfo/.jpg/.png metadata
                # into show/season folders, so a media-less directory is rarely empty.
                # The right test is "holds no tracked media file" -- such a directory is
                # pure metadata cruft and is removed wholesale (rmtree takes the metadata
                # with it). Walk upward from the file's parent, deleting each media-less
                # directory, and stop at the first one that still holds real media (a
                # sibling episode) or at the media root (never delete the root or above).
                media_root = config.local_root_for(relative_path)
                pruned = expected_local_path.parent
                while pruned != media_root and media_root in pruned.parents:
                    if _subtree_has_media(pruned):
                        break
                    parent = pruned.parent
                    shutil.rmtree(pruned, ignore_errors=True)
                    pruned = parent

    # --- UPLOAD PHASE --
    # Places NEW local files (not yet on any remote) on a remote with space. Every new
    # file of any class is uploaded; write-once means no existing remote copy is ever
    # deleted, so there is no gap for a stale copy to slip into.
    # Exclusive: the free-space ledger is only correct while ONE process spends pool space
    # (§ scripts/uploader_lock.py). If something else is uploading, skip the phase entirely
    # rather than queue behind it -- by then this cycle's snapshot would be stale anyway.
    with _hold_upload_lock("media_sync") as got_lock:
        if got_lock:
            index_dirty = _upload_phase(local_files, remote_index, sync_state) or index_dirty
        else:
            logging.warning("another uploader holds the upload lock; skipping the upload "
                            "phase this cycle.")

    # Final flush of the incrementally-updated inventory (a full rescan rewrites it
    # authoritatively next cycle).
    if index_dirty:
        _persist_inventory(remote_index, force=True)

    _report_drive_state()

    # Record sync state. Atomic because this is the drift BASELINE, not a cache: it is what
    # keeps the update phase inert, and it is one of the two files backup_state.py mirrors to
    # the pool. A truncating write that is interrupted -- launchd SIGTERM on a restart, a
    # panic, a full disk -- leaves it empty or half-parsed, and a lost baseline means every
    # file looks like it has drifted on the next cycle. os.replace keeps the last good copy
    # until a complete new one exists.
    write_json_atomic(sync_state_path, sync_state)

def main():
    setup_logging()
    
    global REMOTES
        
    _, err_test = run_command([config.RCLONE_PATH, "version"])
    if "command not found" in err_test or "No such file" in err_test:
        logging.error(f"CRITICAL: rclone not found at '{config.RCLONE_PATH}'. Halting.")
        return
    
    while True:
        # The whole iteration is guarded: this daemon has NO launchd KeepAlive (so the
        # reaper can pause it with a kill), which means it must NEVER exit on its own --
        # an unhandled error anywhere (a git-pull network blip, a bad remote scan) would
        # otherwise leave it dead until the next reboot. So nothing here is allowed to
        # escape the loop.
        try:
            clear_quarantine()
            clear_session_heals()

            # NOTE: the sync cycle no longer pauses for live playback. Playback of a
            # physically-present file is a mediafs passthrough read that never touches
            # MEGA, so uploads/scans don't affect it; and for a cold (pool-only) show,
            # the whole point of the pre-downloader is to fetch the FUTURE episodes
            # while you watch the first -- pausing would defeat that. VPN churn is
            # already bounded by the rotation throttle + the free-model chain split-tunnel.

            git_pull()  # In case of any new remotes present for next cycle

            REMOTES = find_mega_remotes()
            if not REMOTES:
                logging.error("No Mega remotes found. The script cannot continue.")
                time.sleep(5)
                continue

            try:
                sync_cycle()
            except Exception as e:
                logging.critical(f"Unhandled exception in sync_cycle: {e}", exc_info=True)

            # Phone-viewable free-space report (silent; reads free_space.json).
            check_space.write_report()

            # Keep the pool from filling: auto-provision new MEGA accounts when free space
            # is low. Cheap no-op when there's room; inert (monitor-only) without the email
            # app-password. So capacity manages itself with no operator involvement.
            try:
                mega_accounts.ensure_capacity()
            except Exception as e:                              # noqa: BLE001
                logging.error(f"account provisioning check error: {e}")

            # Reclaim SSD space: evict the coldest already-uploaded media to hold the
            # free-space floor. Inventory-guarded (never evicts un-uploaded files) and a
            # cheap no-op when already above the floor, so it's safe every cycle.
            if config.TIER_AUTO_EVICT:
                try:
                    plan = tier.evict_plan(floor_bytes=config.TIER_EVICT_FLOOR_BYTES, execute=True)
                    if plan.get("evict"):
                        logging.info(f"auto-evicted {len(plan['evict'])} files "
                                     f"({plan['would_free'] / 1024**3:.0f} GB) to hold the free-space floor")
                except Exception as e:
                    logging.error(f"auto-eviction error: {e}")

            # Idle pacing: with no uploads to occupy the cycle, the loop would otherwise
            # spin (git_pull + remote-list + local scan every few seconds). Sleep a fixed
            # interval between cycles; an upload backlog keeps each cycle busy for as long
            # as the transfers take, so this only throttles the idle case.
            time.sleep(config.SYNC_IDLE_SLEEP_SEC)
        except Exception as e:
            logging.critical(f"Top-level loop error (continuing): {e}", exc_info=True)
            time.sleep(5)
        
if __name__ == "__main__":
    main()
