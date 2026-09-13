#!/usr/bin/env python3
"""Torrent-Ingest remote-deletion reaper.

The deliberate INVERSE of the ingest pipeline. Ingest is write-once and never
deletes the library; the reaper is the one component allowed to delete remote
*backups* — and only ever to mirror a deletion you already made on the connected
the SSD library root drive (typically by deleting a show/movie in Infuse, which removes the
video off the SSD).

The daemon's flow is the through-the-mount deletion queue:

    every REAP_SCAN_INTERVAL_SEC: read mediafs's mediafs_deletions.jsonl
        -> if non-empty and quiet for REAP_QUEUE_SETTLE_SEC, claim the batch
        -> pause Media-Syncer
        -> for each deleted path: find every remote copy (fleet probe u inventory u
           Mini upload log), delete it from each, empty rubbish bins, verify it is
           truly gone, refix dead-session survivors
        -> clear a vanished video's Jellyfin sidecars (local + metadata backup)
        -> a title now fully gone locally: purge its whole backup footprint
        -> prune Media-Syncer's sync_state / remote_inventory so nothing resurrects
        -> supersede the purged rows in library.db (shows, films and comics) so a
           re-drop is not refused as already-owned
        -> resume Media-Syncer

The older snapshot-diff flow (`cycle()`, below) is NOT what the daemon loop runs:
a file vanishing from the SSD is an eviction to the pool, not a deletion, so only
an explicit unlink through the mount counts. `--approve`, `--status` and
`--reset-baseline` still read that snapshot state.

The safety contract (never weaken):

  * If the DRIVE itself vanishes (unmount / dead disk), delete NOTHING — the
    backup is exactly what saves you then. Enforced by media_healthy() and the
    fraction/titles circuit breaker: a whole-library disappearance trips the
    breaker; a single deleted title does not.
  * Only files Media-Syncer actually replicates (REAP_TRACKED_EXTENSIONS) are
    tracked — a local-only file's deletion has nothing to purge.
  * A delete is trusted only after an lsjson VERIFY confirms absence; a
    dead-session no-op (exit 0, deleted nothing) is caught and refixed.
  * All destructive work happens while Media-Syncer is paused, so it cannot
    re-download a half-purged file mid-ritual.

Run modes:
    python3 reap.py                 # daemon (the launch agent's entry)
    python3 reap.py --once          # one cycle, then exit
    python3 reap.py --dry-run       # detect + plan, delete NOTHING (any mode)
    python3 reap.py --status        # print snapshot/pending/health, exit
    python3 reap.py --reset-baseline# re-seed the snapshot from disk now, exit
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
import mega

DRY_RUN = False

# Jellyfin sidecar extensions that ride next to a video and must be cleared when
# that video is deleted. Never includes a tracked-media extension.
_SIDECAR_EXTS = {".nfo", ".jpg", ".jpeg", ".png", ".webp"}

_UPLOAD_LINE = re.compile(r"Uploading '(.+)' to (\S+?)\.\.\.")

log = logging.getLogger("reap")


# --- logging + lock ----------------------------------------------------------

def _setup_logging() -> None:
    # The reaper is the one daemon here that logs through `logging`, so it bounds its
    # engine log with RotatingFileHandler rather than config.rotate_log_if_large(): the
    # handler holds the file open, and a rename underneath it would leave the reaper
    # writing to an orphaned inode. This caps ONLY the engine log -- state/reap_purges.log
    # is the audit of what was deleted from the fleet and stays unbounded (see config).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[RotatingFileHandler(config.REAP_LOG_FILE,
                                      maxBytes=config.LOG_MAX_BYTES,
                                      backupCount=config.LOG_BACKUP_COUNT),
                  logging.StreamHandler()],
    )


def _acquire_lock():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = (config.STATE_DIR / "torrent_reap.lock").open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("Another reaper instance holds the lock; exiting.")
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _audit(msg: str) -> None:
    """Append to the human-readable purge audit trail (mirrors decisions.log)."""
    try:
        config.REAP_PURGES_LOG.parent.mkdir(parents=True, exist_ok=True)
        with config.REAP_PURGES_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"[{config.log_stamp_iso()}] {msg}\n")
    except OSError:
        pass


# --- drive health ------------------------------------------------------------

def media_healthy() -> bool:
    """True only if the SSD library root is provably present. This is the primary
    guard against acting on a vanished/unmounted drive: we require the mount
    point AND the Shows/ tree to exist and be non-empty. A dead or unmounted
    drive fails this, so the reaper never mistakes 'drive gone' for 'files
    deleted' and never purges the backup that would restore it."""
    try:
        if not (config.MEDIA_ROOT.exists() and config.SHOWS_ROOT.exists()):
            return False
        # Shows/ is always populated in this library; an empty/absent Shows/ means
        # the mount is broken or a placeholder, not a legitimate state.
        return any(config.SHOWS_ROOT.iterdir())
    except OSError:
        return False


def _on_the_mini() -> bool:
    """This daemon belongs on the Mini (the host with the SSD library root + Media-Syncer's
    `mini` role). Refuse to run anywhere else."""
    hm = config.MEDIA_SYNCER_DIR / ".host_mode"
    try:
        return hm.read_text().strip().lower() == "mini"
    except OSError:
        # No Media-Syncer host_mode readable: fall back to the drive check.
        return config.MEDIA_ROOT.exists()


# --- library scan ------------------------------------------------------------

def scan_tracked_media() -> set[str]:
    """Every tracked-media file currently under MEDIA_ROOT, as remote-relative
    POSIX paths (the same key space as Media-Syncer's inventory). Dotfiles and
    dot-dirs (our .ingest-staging) are skipped, exactly as Media-Syncer's scan
    does."""
    found: set[str] = set()
    root = config.MEDIA_ROOT
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in config.REAP_TRACKED_EXTENSIONS:
                rel = (Path(dirpath) / name).relative_to(root).as_posix()
                found.add(rel)
    return found


# --- snapshot / pending state ------------------------------------------------

def _load_set(path: Path) -> set[str] | None:
    if not path.exists():
        return None
    try:
        return set(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError):
        return None


def _save_set(path: Path, data: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(data), ensure_ascii=False, indent=0))


def _load_pending() -> dict[str, int]:
    if not config.REAP_PENDING_FILE.exists():
        return {}
    try:
        return {str(k): int(v) for k, v in json.loads(config.REAP_PENDING_FILE.read_text()).items()}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _save_pending(pending: dict[str, int]) -> None:
    config.REAP_PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.REAP_PENDING_FILE.write_text(json.dumps(pending, ensure_ascii=False, indent=0))


# --- deliberate-delete approval token ----------------------------------------

def _load_approved() -> set[str]:
    """The set of paths a human explicitly approved for purge despite the breaker
    (via `reap.py --approve`). Empty if no token, malformed, or expired."""
    path = config.REAP_APPROVE_FILE
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("paths", []))
    except (json.JSONDecodeError, OSError, AttributeError):
        return set()


def _save_approved(paths: set[str]) -> None:
    config.REAP_APPROVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.REAP_APPROVE_FILE.write_text(json.dumps(
        {"paths": sorted(paths), "stamped": datetime.now(timezone.utc).isoformat()},
        ensure_ascii=False, indent=0))


def _consume_approved(purged: set[str]) -> None:
    """Drop just-purged paths from the approval token; delete it once exhausted so
    a one-shot approval can never linger to bless a later, unrelated vanish."""
    remaining = _load_approved() - purged
    if remaining:
        _save_approved(remaining)
    else:
        try:
            config.REAP_APPROVE_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def _confirmed_missing() -> tuple[set[str], set[str], dict[str, int], set[str]]:
    """Shared detection used by both the daemon cycle and `--approve`: returns
    (present, snapshot, new_pending, confirmed) computed identically so an approval
    stamps exactly what a cycle would act on. `confirmed` is empty if there is no
    snapshot yet (first run)."""
    present = scan_tracked_media()
    snapshot = _load_set(config.REAP_SNAPSHOT_FILE)
    if snapshot is None:
        return present, set(), {}, set()
    missing_now = snapshot - present
    pending = _load_pending()
    new_pending = {rel: pending.get(rel, 0) + 1 for rel in missing_now}
    confirmed = {rel for rel, n in new_pending.items() if n >= config.REAP_DEBOUNCE_SCANS}
    return present, snapshot, new_pending, confirmed


# --- path classification -----------------------------------------------------

def title_dir_of(relpath: str) -> str | None:
    """The top-level title directory for a path, or None for a loose film that
    sits directly under Movies/ (matched by stem instead). Drives the fleet
    probe's per-title grouping so we do one recursive listing per title per
    remote, not one per file."""
    parts = relpath.split("/")
    cat = parts[0] if parts else ""
    if cat == "Shows" and len(parts) >= 2:
        return f"Shows/{parts[1]}"
    if cat == "Comics" and len(parts) >= 2:
        if parts[1] == "Manga" and len(parts) >= 3:
            return f"Comics/Manga/{parts[2]}"
        return f"Comics/{parts[1]}"
    if cat == "Movies" and len(parts) >= 3:
        return f"Movies/{parts[1]}"
    return None  # loose film: Movies/<file>


# --- Media-Syncer state: discovery + prune -----------------------------------

def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def inventory_remotes(relpaths: set[str]) -> dict[str, set[str]]:
    """{relpath: {remote}} from Media-Syncer's remote_inventory.json (single
    residence per path)."""
    inv = _load_json(config.MEDIA_SYNCER_INVENTORY)
    out: dict[str, set[str]] = {r: set() for r in relpaths}
    for rel in relpaths:
        entry = inv.get(rel)
        if entry and isinstance(entry, list) and entry[0]:
            out[rel].add(entry[0])
    return out


def log_union_remotes(relpaths: set[str]) -> dict[str, set[str]]:
    """{relpath: {every remote it was EVER an upload target for}} harvested from
    Media-Syncer's launchd + app logs. The orphan safety net: a copy left on a
    remote by a failed retry is invisible to the single-residence inventory but
    named in the upload history (Media-Syncer README, § log-discovery)."""
    out: dict[str, set[str]] = {r: set() for r in relpaths}
    want = set(relpaths)
    for logpath in [*config.MEDIA_SYNCER_LAUNCHD_LOGS, config.MEDIA_SYNCER_APP_LOG]:
        try:
            text = logpath.read_text(errors="ignore")
        except OSError:
            continue
        for m in _UPLOAD_LINE.finditer(text):
            path, remote = m.group(1), m.group(2)
            if path in want:
                out[path].add(remote)
    return out


def fleet_probe(relpaths: set[str]) -> tuple[dict[str, set[str]], set[str]]:
    """Ground truth: probe every MEGA remote for which of the vanished files it
    actually holds right now. Returns ({relpath: {remote present}}, {err_remotes}).

    Grouped by title directory so it's one recursive `lsf` per (remote, title),
    not per file — and one top-level `lsf Movies/` per remote for loose films.
    A remote that ERRORS (dead session, throttle) after a heal attempt is
    recorded in err_remotes: callers still attempt a delete there rather than
    scoring it clean (a silenced error must never read as 'absent')."""
    remotes = mega.mega_remotes()
    hits: dict[str, set[str]] = {r: set() for r in relpaths}
    err_remotes: set[str] = set()

    # Build the distinct probe targets.
    title_dirs = {td for td in (title_dir_of(r) for r in relpaths) if td}
    loose_films = {r for r in relpaths if title_dir_of(r) is None and r.startswith("Movies/")}
    by_title: dict[str, set[str]] = {}
    for r in relpaths:
        td = title_dir_of(r)
        if td:
            by_title.setdefault(td, set()).add(r)

    def probe_one(remote: str) -> tuple[str, dict[str, set[str]], bool]:
        local_hits: dict[str, set[str]] = {}
        errored = False
        for td in title_dirs:
            files, status = mega.lsf_recursive_files(remote, td)
            if status == "err":
                errored = True
                continue
            if status == "absent" or files is None:
                continue
            present = {f"{td}/{f}" for f in files}
            for rel in by_title[td] & present:
                local_hits.setdefault(rel, set()).add(remote)
        if loose_films:
            names, status = mega.lsf_top_level(remote, "Movies")
            if status == "err":
                errored = True
            elif names:
                present = {f"Movies/{n}" for n in names}
                for rel in loose_films & present:
                    local_hits.setdefault(rel, set()).add(remote)
        return remote, local_hits, errored

    if not remotes:
        return hits, err_remotes
    workers = max(1, min(config.REAP_PROBE_WORKERS, len(remotes)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for remote, local_hits, errored in pool.map(probe_one, remotes):
            if errored:
                err_remotes.add(remote)
            for rel, rems in local_hits.items():
                hits[rel] |= rems
    return hits, err_remotes


def _inventory_key_purged(key: str, purged: set[str],
                          backup_prefixes: set[str], state_needles: set[str]) -> bool:
    """Whether a remote_inventory.json key should be dropped: an exact purged media
    path, OR a metadata-backup key whose deleted file we just cleared — the live
    `metadata-backup/media/<stem-or-title>` sidecars (prefix match) and the dated
    `metadata-backup/state/nfo-backup-*/Shows/<title>` snapshots (needle match, to
    span the timestamped snapshot dir in the middle of the key)."""
    if key in purged:
        return True
    if any(key.startswith(p) for p in backup_prefixes):
        return True
    if key.startswith(f"{config.METADATA_BACKUP_BASE}/state/") and any(n in key for n in state_needles):
        return True
    return False


def prune_media_syncer_state(purged: set[str], backup_prefixes: set[str] | None = None,
                             state_needles: set[str] | None = None) -> None:
    """Remove purged paths from sync_state.json and remote_inventory.json so
    Media-Syncer's next cycle neither re-downloads (missing-locally) nor treats a
    stale baseline as live. The inventory is rebuilt next scan anyway, but pruning
    keeps it consistent in the interim (Media-Syncer README, step 7).

    `backup_prefixes` / `state_needles` additionally drop the inventory keys for the
    metadata-backup sidecars we deleted from METADATA_BACKUP_REMOTE (the `.nfo`/
    artwork under `metadata-backup/media/...` and the `metadata-backup/state/...`
    nfo snapshots). Those keys are inert to Media-Syncer regardless, but pruning
    them leaves the inventory spotless rather than waiting for the next full scan."""
    backup_prefixes = backup_prefixes or set()
    state_needles = state_needles or set()
    if DRY_RUN:
        log.info("[dry-run] would prune %d media paths (+ backup keys) from Media-Syncer state",
                 len(purged))
        return
    # sync_state only ever keys real media paths, so it takes just `purged`; the
    # inventory also carries the metadata-backup keys, so it takes the fuller test.
    for path, full in ((config.MEDIA_SYNCER_SYNC_STATE, False),
                       (config.MEDIA_SYNCER_INVENTORY, True)):
        data = _load_json(path)
        if not data:
            continue
        if full:
            drop = [k for k in data if _inventory_key_purged(k, purged, backup_prefixes, state_needles)]
        else:
            drop = [k for k in purged if k in data]
        if drop:
            for k in drop:
                del data[k]
            try:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=4))
                log.info("pruned %d keys from %s", len(drop), path.name)
            except OSError as exc:
                log.warning("could not prune %s: %s", path.name, exc)


# --- Media-Syncer daemon control (launchctl) ---------------------------------

def _launchctl(*args: str) -> tuple[str, str, int]:
    try:
        r = subprocess.run([config.LAUNCHCTL_BIN, *args],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc), -1


def _ms_target() -> str:
    return f"gui/{os.getuid()}/{config.MEDIA_SYNCER_LABEL}"


def ms_is_running() -> bool:
    r = subprocess.run(["/usr/bin/pgrep", "-f", config.MEDIA_SYNCER_PROC_PATTERN],
                       capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def ms_pause() -> None:
    """Stop Media-Syncer and confirm it is really down. It has no KeepAlive, so a
    kill keeps it down; the agent stays loaded for a later kickstart. Drops a
    crash-safe marker so a reaper killed mid-purge still gets Media-Syncer resumed
    on its next start (see recover_media_syncer)."""
    if DRY_RUN:
        log.info("[dry-run] would pause Media-Syncer")
        return
    log.info("Pausing Media-Syncer...")
    try:
        config.REAP_PAUSED_MARKER.parent.mkdir(parents=True, exist_ok=True)
        config.REAP_PAUSED_MARKER.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass
    _launchctl("kill", "SIGTERM", _ms_target())
    for _ in range(20):
        if not ms_is_running():
            log.info("Media-Syncer stopped.")
            return
        time.sleep(1)
    # Escalate.
    _launchctl("kill", "SIGKILL", _ms_target())
    subprocess.run(["/usr/bin/pkill", "-9", "-f", config.MEDIA_SYNCER_PROC_PATTERN],
                   capture_output=True)
    time.sleep(2)
    if ms_is_running():
        log.warning("Media-Syncer still appears to be running after kill escalation.")
    else:
        log.info("Media-Syncer stopped (after escalation).")


def ms_resume() -> None:
    """Restart Media-Syncer and VERIFY it came back — kickstart can be flaky, and
    the whole point of the reaper is that the syncer resumes. Retries up to 3
    times, confirming the process is actually up before clearing the paused
    marker. Only a genuine, repeated failure is left for manual intervention."""
    if DRY_RUN:
        log.info("[dry-run] would resume Media-Syncer")
        return
    log.info("Resuming Media-Syncer...")
    for attempt in range(1, 4):
        _out, err, rc = _launchctl("kickstart", _ms_target())
        for _ in range(5):
            if ms_is_running():
                log.info("Media-Syncer resumed.")
                try:
                    config.REAP_PAUSED_MARKER.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            time.sleep(1)
        log.warning("Media-Syncer not up after kickstart attempt %d (rc=%s %s); retrying...",
                    attempt, rc, err)
    log.error("Media-Syncer FAILED to resume after 3 attempts — resume it by hand: "
              "`launchctl kickstart %s` (or reload its agent). The paused marker is "
              "left in place so the next reaper cycle retries the resume.", _ms_target())


def _hold_age_seconds() -> float | None:
    """Seconds since MS was paused for the current purge/drain, read from the pause
    marker's timestamp. None if no marker or it can't be parsed."""
    try:
        started = datetime.fromisoformat(config.REAP_PAUSED_MARKER.read_text().strip())
        return (datetime.now(timezone.utc) - started).total_seconds()
    except (OSError, ValueError):
        return None


def settle_gate(settled: bool) -> None:
    """The single place that RESUMES Media-Syncer. Once the reaper has paused MS to
    purge deletions, MS stays paused across cycles until the library SETTLES (a cycle
    finds nothing more missing/debouncing). Waking it any earlier would let its
    download phase re-fetch a file that was deleted but not yet purged — resurrecting
    it — so a deletion that lands mid-purge is drained first. A hard time cap
    (`REAP_SETTLE_MAX_HOLD_SEC`) resumes MS anyway if an unpurgeable survivor would
    otherwise strand it offline forever."""
    if DRY_RUN or not config.REAP_PAUSED_MARKER.exists():
        return  # MS is not held by us; nothing to resume.
    if settled:
        log.info("Library settled — no more deletions pending; resuming Media-Syncer.")
        ms_resume()
        return
    age = _hold_age_seconds()
    if age is not None and age > config.REAP_SETTLE_MAX_HOLD_SEC:
        log.warning("Media-Syncer held down %.0fs (> %ds cap) while deletions are "
                    "still unsettled (likely an unpurgeable survivor); resuming to "
                    "avoid stranding it.", age, config.REAP_SETTLE_MAX_HOLD_SEC)
        ms_resume()
        return
    log.info("Holding Media-Syncer paused: deletions still settling, so it cannot "
             "re-download a not-yet-purged file.")


def recover_media_syncer() -> None:
    """If a paused marker is lingering at cycle start, a previous reaper run was
    killed mid-purge before it could resume Media-Syncer. Resume it now so the
    syncer is never stranded down across a reaper crash/restart."""
    if DRY_RUN or not config.REAP_PAUSED_MARKER.exists():
        return
    if ms_is_running():
        # It's already back up (KeepAlive-less, so this means it was resumed or
        # relaunched some other way); just clear the stale marker.
        try:
            config.REAP_PAUSED_MARKER.unlink(missing_ok=True)
        except OSError:
            pass
        return
    log.warning("Found a lingering pause marker (prior run interrupted mid-purge); "
                "resuming Media-Syncer.")
    ms_resume()


# --- local + backup metadata cleanup for a vanished video --------------------

def _rmtree(path: Path) -> None:
    if DRY_RUN:
        log.info("[dry-run] would remove local %s", path)
        return
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    except OSError as exc:
        log.warning("could not remove %s: %s", path, exc)


def clear_local_sidecars(relpath: str) -> None:
    """Delete the Jellyfin sidecars Infuse leaves behind next to a video it
    removed: the per-episode `.nfo`, `-thumb.jpg`, poster/artwork for a loose
    film, and the `*.trickplay` scrub-tile dir. Never touches another real media
    file (guarded by extension)."""
    video = config.MEDIA_ROOT / relpath
    parent = video.parent
    stem = video.stem  # e.g. "Title (2020) - S01E05"
    if not parent.exists():
        return
    try:
        entries = list(parent.iterdir())
    except OSError:
        return
    for f in entries:
        try:
            if f.is_dir():
                if f.name.startswith(stem) and f.name.endswith(".trickplay"):
                    _rmtree(f)
                continue
            if f.suffix.lower() in config.REAP_TRACKED_EXTENSIONS:
                continue  # never delete real media
            if f.name.startswith(stem) and f.suffix.lower() in _SIDECAR_EXTS:
                _rmtree(f)
        except OSError:
            continue


def clear_backup_sidecars_for_video(relpath: str, backup_remote: str) -> None:
    """Delete a vanished video's sidecars from the metadata backup on
    METADATA_BACKUP_REMOTE. For a show episode that is just its `.nfo` (thumbs /
    trickplay are excluded from the backup by its filters); for a loose film it
    is every `Movies/<stem>*` sidecar (poster/backdrop/landscape/logo + nfo)."""
    base = config.METADATA_BACKUP_BASE
    if relpath.startswith("Movies/") and title_dir_of(relpath) is None:
        # Loose film: match sidecars under metadata-backup/media/Movies by stem.
        stem = Path(relpath).stem
        if DRY_RUN:
            log.info("[dry-run] would clear backup sidecars for film '%s'", stem)
            return
        names, status = mega.lsf_top_level(backup_remote, f"{base}/media/Movies")
        if status == "ok" and names:
            for n in names:
                if n.startswith(stem) and Path(n).suffix.lower() in _SIDECAR_EXTS | {".trickplay"}:
                    _delete_or_dry(backup_remote, f"{base}/media/Movies/{n}")
        return
    # Show episode (or foldered movie): the per-file .nfo mirror.
    nfo_rel = str(Path(relpath).with_suffix(".nfo"))
    _delete_or_dry(backup_remote, f"{base}/media/{nfo_rel}")


def _delete_or_dry(remote: str, path: str) -> None:
    if DRY_RUN:
        log.info("[dry-run] would delete %s:%s", remote, path)
        return
    ok, detail = mega.deletefile(remote, path)
    if not ok:
        log.warning("backup delete failed %s:%s (%s)", remote, path, detail)


def purge_title_metadata(title_dir: str, backup_remote: str) -> None:
    """A whole show/foldered-title is now gone locally: purge its entire backup
    footprint — the live `metadata-backup/media/<title>` mirror, every dated
    `metadata-backup/state/nfo-backup-*/Shows/<title>` snapshot, AND the LOCAL
    state/nfo-backup-*/Shows/<title> snapshots (else tonight's backup_metadata.py
    resyncs the deleted remote copies straight back — Media-Syncer README
    durability note). Finally prune the now-media-less local title folder."""
    base = config.METADATA_BACKUP_BASE
    title_name = title_dir.split("/", 1)[1] if "/" in title_dir else title_dir

    if DRY_RUN:
        log.info("[dry-run] would purge backup media + state snapshots for '%s'", title_dir)
    else:
        ok, detail = mega.purge_dir(backup_remote, f"{base}/media/{title_dir}")
        log.info("backup media purge %s: %s", title_dir, detail if ok else f"FAILED {detail}")

    # Remote state snapshots that hold this title (Shows only carry state nfos).
    if title_dir.startswith("Shows/") and not DRY_RUN:
        files, status = mega.lsf_recursive_files(backup_remote, f"{base}/state")
        if status == "ok" and files:
            snap_dirs = set()
            needle = f"/Shows/{title_name}/"
            for f in files:
                # f like "nfo-backup-<ts>/Shows/<title>/Season 01/....nfo"
                if needle in f"/{f}":
                    snap = f.split("/", 1)[0]
                    snap_dirs.add(f"{base}/state/{snap}/Shows/{title_name}")
            for sd in sorted(snap_dirs):
                if DRY_RUN:
                    log.info("[dry-run] would purge backup state snapshot %s", sd)
                else:
                    ok, detail = mega.purge_dir(backup_remote, sd)
                    log.info("backup state purge %s: %s", sd, detail if ok else f"FAILED {detail}")

        # Local state snapshots — delete so the nightly backup can't resurrect them.
        for snap in config.STATE_DIR.glob("nfo-backup-*"):
            local_snap = snap / "Shows" / title_name
            if local_snap.exists():
                _rmtree(local_snap)

    # Prune the now-media-less local title folder (takes tvshow.nfo/artwork with it).
    local_title = config.MEDIA_ROOT / title_dir
    if local_title.exists() and not _subtree_has_media(local_title):
        _rmtree(local_title)


def _subtree_has_media(directory: Path) -> bool:
    for _dp, _dn, filenames in os.walk(directory):
        for name in filenames:
            if os.path.splitext(name)[1].lower() in config.REAP_TRACKED_EXTENSIONS:
                return True
    return False


# --- the purge ritual --------------------------------------------------------

def purge_batch(confirmed: set[str]) -> None:
    """Purge every vanished file from the fleet, verify, refix, clean metadata,
    prune state. Assumes the circuit breaker already passed and Media-Syncer is
    paused."""
    backup_remote = config.METADATA_BACKUP_REMOTE

    log.info("Discovering remote copies for %d vanished file(s)...", len(confirmed))
    inv = inventory_remotes(confirmed)
    logs = log_union_remotes(confirmed)
    probe_hits, err_remotes = fleet_probe(confirmed)
    if err_remotes:
        log.warning("%d remote(s) errored during probe (treated as MAYBE-present, "
                    "delete attempted anyway): %s", len(err_remotes),
                    ", ".join(sorted(err_remotes)[:10]) + ("..." if len(err_remotes) > 10 else ""))

    # Per-file remote set = probe u inventory u log u (err remotes, attempted).
    remote_sets: dict[str, set[str]] = {}
    for rel in confirmed:
        remote_sets[rel] = probe_hits[rel] | inv[rel] | logs[rel] | err_remotes

    touched_remotes: set[str] = set()
    attempted: list[tuple[str, str]] = []  # (remote, relpath) for the verify sweep
    purged_ok: set[str] = set()

    # 1) Delete each file from every candidate remote.
    for rel in sorted(confirmed):
        rems = remote_sets[rel]
        if not rems:
            # On zero remotes: local-only file, nothing to purge (README rule).
            log.info("'%s' on no known remote (local-only); nothing to purge.", rel)
            _audit(f"LOCAL-ONLY (no remote): {rel}")
            purged_ok.add(rel)
            continue
        log.info("Purging '%s' from %d remote(s): %s", rel, len(rems), ", ".join(sorted(rems)))
        for remote in sorted(rems):
            if DRY_RUN:
                log.info("[dry-run] would deletefile %s:%s", remote, rel)
                touched_remotes.add(remote)
                attempted.append((remote, rel))
                continue
            ok, detail = mega.deletefile(remote, rel)
            attempted.append((remote, rel))
            touched_remotes.add(remote)
            if not ok:
                log.warning("delete failed %s:%s (%s)", remote, rel, detail)
            else:
                _tidy_parents(remote, rel)
        _audit(f"PURGE {rel} -> {sorted(rems)}")

    # 2) Empty the rubbish bin on every touched remote (sequential, per README).
    if not DRY_RUN:
        for remote in sorted(touched_remotes):
            mega.cleanup(remote)

    # 3) VERIFY: confirm each attempted (remote, path) is truly gone; refix
    #    dead-session survivors. Never trust the delete's exit code.
    if not DRY_RUN:
        survivors = _verify_gone(attempted)
        # A file is safely purged only if NONE of its remotes still show it.
        failed_paths = {rel for _r, rel in survivors}
        for rel in confirmed:
            if rel not in failed_paths:
                purged_ok.add(rel)
        if survivors:
            log.error("%d (remote,path) survivor(s) could not be verified gone: %s",
                      len(survivors), survivors[:10])
            _audit(f"SURVIVORS (left in snapshot for retry): {survivors}")
    else:
        purged_ok |= confirmed

    # 4) Metadata cleanup for vanished videos (local sidecars + backup mirror).
    #    Accumulate the metadata-backup inventory keys we clear so step 6 can prune
    #    them alongside the media paths (they are inert to Media-Syncer, but pruning
    #    keeps remote_inventory.json spotless rather than waiting for a rescan).
    base = config.METADATA_BACKUP_BASE
    videos = {r for r in purged_ok
              if os.path.splitext(r)[1].lower() in config.REAP_VIDEO_EXTENSIONS}
    backup_prefixes: set[str] = set()
    state_needles: set[str] = set()
    for rel in sorted(videos):
        clear_local_sidecars(rel)
        clear_backup_sidecars_for_video(rel, backup_remote)
        # Sidecars share the video's stem: metadata-backup/media/<stem-without-ext>*
        backup_prefixes.add(f"{base}/media/{Path(rel).with_suffix('').as_posix()}")

    # 5) Title-level metadata purge for titles now fully gone locally.
    affected_titles = {td for td in (title_dir_of(r) for r in videos) if td}
    for title_dir in sorted(affected_titles):
        local_title = config.MEDIA_ROOT / title_dir
        if not local_title.exists() or not _subtree_has_media(local_title):
            log.info("Title '%s' fully gone locally; purging its metadata footprint.", title_dir)
            purge_title_metadata(title_dir, backup_remote)
            # Whole-title footprint: the media-backup dir + every state snapshot dir.
            backup_prefixes.add(f"{base}/media/{title_dir}/")
            if title_dir.startswith("Shows/"):
                state_needles.add(f"/{title_dir}/")   # matches .../nfo-backup-*/Shows/<title>/
    if videos:
        if not DRY_RUN:
            mega.cleanup(backup_remote)

    # 6) Prune Media-Syncer state so nothing resurrects (media paths + backup keys).
    prune_media_syncer_state(purged_ok, backup_prefixes, state_needles)

    # 7) Stop library.db claiming what is now verifiably gone. Only `purged_ok`:
    #    a survivor's pool copy still exists and the ledger should keep owning it.
    _sweep_library_db(purged_ok)

    return purged_ok


def _sweep_library_db(purged: set[str]) -> None:
    """Mark the purged titles' rows superseded in library.db. Fail-open.

    Without this a purge leaves its ownership rows behind, and the acceptance gate
    refuses the title's own re-drop as already owned. A DB problem must never fail
    the purge that already succeeded.
    """
    if DRY_RUN or not purged:
        return
    try:
        import dbhook
        res = dbhook.record_purge(purged)
        if res["superseded"]:
            log.info("library.db: superseded %d row(s) for %d purged path(s)",
                     res["superseded"], res["paths"])
    except Exception as exc:  # noqa: BLE001
        log.warning("library.db sweep failed (the purge itself is unaffected): %r", exc)


def _tidy_parents(remote: str, relpath: str) -> None:
    """rmdir now-empty parent dirs on `remote`, climbing up but never removing a
    top-level category dir (Shows/Movies/Comics) or the root. Stops at the first
    non-empty parent (rmdir refuses it)."""
    parts = relpath.split("/")
    # parents from deepest to shallowest, excluding the file and the category dir.
    for depth in range(len(parts) - 1, 1, -1):
        parent = "/".join(parts[:depth])
        if not mega.rmdir(remote, parent):
            break  # non-empty (has sibling media) — stop climbing


def _verify_gone(attempted: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """lsjson-sweep every (remote, path) we deleted; return those still present
    or unverifiable after a re-delete on a stripped session."""
    survivors: list[tuple[str, str]] = []
    for remote, rel in attempted:
        state = mega.exists_on(remote, rel)
        if state is False:
            continue  # confirmed gone
        # Present or unknown: strip session, re-delete, re-verify once.
        mega.strip_session(remote)
        mega.deletefile(remote, rel)
        mega.cleanup(remote)
        if mega.exists_on(remote, rel) is not False:
            survivors.append((remote, rel))
    return survivors


# --- circuit breaker ---------------------------------------------------------

def _trip_breaker(reason: str, confirmed: set[str]) -> None:
    body = (
        f"[{datetime.now(timezone.utc).isoformat()}] CIRCUIT BREAKER TRIPPED\n"
        f"Reason: {reason}\n"
        f"{len(confirmed)} file(s) looked missing but NOTHING was purged — this "
        f"pattern looks like a drive fault, not a deliberate delete.\n"
        f"If you REALLY deleted this much on purpose, approve it with:\n"
        f"    python3 reap.py --approve\n"
        f"That stamps this exact set; the next cycle purges only these paths (while "
        f"the drive stays healthy), then discards the token. Nothing else is touched.\n"
        f"Missing (first 50):\n" + "\n".join(f"  {p}" for p in sorted(confirmed)[:50]) + "\n"
    )
    try:
        config.REAP_ALERT_FILE.write_text(body)
    except OSError:
        pass
    log.error("CIRCUIT BREAKER: %s — refusing to purge %d file(s). See %s",
              reason, len(confirmed), config.REAP_ALERT_FILE.name)


def breaker_ok(confirmed: set[str], present: set[str]) -> bool:
    # The breaker's ONE job: refuse a purge when the loss looks like a DRIVE-SCALE
    # event (a power-outage freak wipe, a half-mounted volume dropping a whole
    # subtree), not second-guess a large DELIBERATE delete. Such an event takes out a
    # huge FRACTION of the library at once, so the fraction guard alone is the signal.
    # A fully-unmounted drive is already caught upstream by media_healthy(); a large
    # but deliberate delete that trips this is released with `reap.py --approve`.
    total = len(present) + len(confirmed)
    if total == 0:
        return False
    frac = len(confirmed) / total
    if frac > config.REAP_MAX_MISSING_FRACTION:
        _trip_breaker(f"{frac:.0%} of the tracked library ({len(confirmed)}/{total}) "
                      f"vanished at once (> {config.REAP_MAX_MISSING_FRACTION:.0%}) — "
                      f"drive-scale loss, not a deliberate delete", confirmed)
        return False
    return True


# --- cycle -------------------------------------------------------------------

def cycle() -> None:
    if not media_healthy():
        log.info("SSD library root not healthy (unmounted or Shows/ empty); not scanning. "
                 "Snapshot untouched so a returning drive is a no-op.")
        return

    if _load_set(config.REAP_SNAPSHOT_FILE) is None:
        _save_set(config.REAP_SNAPSHOT_FILE, scan_tracked_media())
        _save_pending({})
        log.info("Baseline established: no action on first run.")
        return

    present, _snapshot, new_pending, confirmed = _confirmed_missing()

    if not confirmed:
        _save_pending(new_pending)
        # Fold newly-present files in; keep still-debouncing missing files tracked.
        _save_set(config.REAP_SNAPSHOT_FILE, present | set(new_pending))
        if new_pending:
            log.info("%d file(s) missing, debouncing (need %d consecutive scans).",
                     len(new_pending), config.REAP_DEBOUNCE_SCANS)
        # If MS is held from a prior purge, wake it only now that nothing is missing
        # or debouncing — the library has settled, no deletion is mid-flight.
        settle_gate(settled=not new_pending)
        return

    log.info("%d file(s) confirmed vanished after debounce.", len(confirmed))
    if not breaker_ok(confirmed, present):
        # Breaker tripped. The ONE sanctioned override: a human ran `reap.py
        # --approve` and stamped this exact set as a deliberate delete. The drive
        # is already proven healthy above, so an approved subset is safe to purge;
        # anything outside the token stays parked (self-recovers if it returns).
        approved = _load_approved()
        covered = confirmed & approved
        if covered:
            # Purge ONLY the approved∩confirmed subset — never a path outside the
            # token, and robust to the vanished set micro-fluctuating between the
            # `--approve` stamp and this cycle (a rewritten dir flickers files in and
            # out of the scan). Any newly-vanished, unapproved file is parked.
            skipped = confirmed - covered
            log.warning("Circuit breaker OVERRIDDEN by approval token: purging %d "
                        "approved file(s)%s (drive healthy; deliberate delete confirmed).",
                        len(covered),
                        f"; parking {len(skipped)} newly-vanished unapproved file(s)"
                        if skipped else "")
            _audit(f"APPROVED OVERRIDE: purging {len(covered)} approved file(s); "
                   f"{len(skipped)} unapproved parked")
            confirmed = covered
            try:
                config.REAP_ALERT_FILE.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            # No approval (or it doesn't cover this set): do NOT purge, do NOT drop
            # them from tracking — self-recovers if the drive/files come back.
            _save_pending(new_pending)
            _save_set(config.REAP_SNAPSHOT_FILE, present | set(new_pending))
            # A large unexplained-missing set is exactly when MS must NOT restore:
            # keep any existing hold in place (settled=False), never wake it here.
            settle_gate(settled=False)
            return

    # Clear any stale alert now that we're acting normally.
    try:
        config.REAP_ALERT_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    # Pause MS for the purge. It is NOT resumed here: the settle-gate below keeps it
    # paused across cycles until no more deletions are in flight, so a deletion that
    # lands mid-purge can't be re-downloaded before it too is purged.
    if ms_is_running():
        ms_pause()
    purged_ok: set[str] = set()
    try:
        purged_ok = purge_batch(confirmed) or set()
    except Exception as exc:  # noqa: BLE001
        log.critical("purge_batch failed (MS stays held; retried next cycle): %r",
                     exc, exc_info=True)

    # Retire any approval coverage for what we actually purged (one-shot token).
    # Never consume on a dry-run — that would strip the approval before the real run.
    if purged_ok and not DRY_RUN:
        _consume_approved(purged_ok)

    # Advance state: successfully-purged files leave tracking entirely; anything
    # not purged (survivor / breaker) stays tracked + pending for a retry.
    remaining_pending = {rel: n for rel, n in new_pending.items() if rel not in purged_ok}
    _save_pending(remaining_pending)
    _save_set(config.REAP_SNAPSHOT_FILE, present | set(remaining_pending))
    log.info("Cycle done: %d purged, %d still pending.",
             len(purged_ok), len(remaining_pending))

    # Wake MS only once the library has fully settled (nothing missing or debouncing);
    # otherwise keep it paused and drain the rest on the next cycle.
    settle_gate(settled=not remaining_pending)


# --- entry -------------------------------------------------------------------

def _print_status() -> None:
    present = scan_tracked_media() if media_healthy() else set()
    snapshot = _load_set(config.REAP_SNAPSHOT_FILE)
    pending = _load_pending()
    print(f"drive healthy:      {media_healthy()}")
    print(f"on the mini:        {_on_the_mini()}")
    print(f"tracked present:    {len(present)}")
    print(f"snapshot size:      {len(snapshot) if snapshot is not None else 'UNSET (first run)'}")
    print(f"pending (debounce): {len(pending)}")
    print(f"Media-Syncer up:    {ms_is_running()}")
    if snapshot is not None:
        missing = snapshot - present
        print(f"missing vs snapshot: {len(missing)}")
        for p in sorted(missing)[:30]:
            print(f"    - {p}")
    if config.REAP_ALERT_FILE.exists():
        print(f"\n!! ALERT present at {config.REAP_ALERT_FILE}")


def drain_deletions_queue() -> None:
    """Purge every media file explicitly deleted THROUGH the mediafs mount.

    Virtual-library replacement for the old the SSD library root-snapshot diff: a file vanishing
    from the SSD library root now means it was EVICTED (bytes moved to the cloud, still in the
    library) and must NOT be purged. Only an explicit unlink through the mount -- you
    deleting a title in Jellyfin/Infuse with file management on -- is a real delete,
    and mediafs records those here. We drain the queue and run the exact same purge
    machinery (discover remotes, delete, verify, clean metadata backup, prune state).
    No circuit breaker: the signal is an intentional delete, not an ambiguous vanish.
    """
    q = config.MEDIAFS_DELETIONS_QUEUE
    proc = q.with_name(q.name + ".processing")
    # Adopt a crash-leftover batch first; otherwise claim the queue atomically by
    # rename, so mediafs's next append starts a fresh queue and nothing is lost.
    if not proc.exists():
        try:
            if not q.exists() or q.stat().st_size == 0:
                return
            # Settle gate: don't claim the queue until it has stopped growing, so a
            # mass-delete (Jellyfin still unlinking a franchise) batches into ONE
            # purge/probe rather than splitting across drain ticks.
            quiet_for = time.time() - q.stat().st_mtime
            if quiet_for < config.REAP_QUEUE_SETTLE_SEC:
                log.info("Deletions queued but still settling (%.0fs quiet, need %ds); waiting.",
                         quiet_for, config.REAP_QUEUE_SETTLE_SEC)
                return
            os.rename(q, proc)
        except OSError:
            return

    paths: set[str] = set()
    try:
        for line in proc.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line).get("path")
            except (ValueError, AttributeError):
                p = None
            if p:
                paths.add(p)
    except OSError:
        return
    if not paths:
        proc.unlink(missing_ok=True)
        return

    if not media_healthy():
        log.warning("Drive not healthy; deferring purge of %d queued deletion(s).", len(paths))
        return  # leave proc for a later cycle

    log.info("Draining %d queued through-the-mount deletion(s).", len(paths))
    running = ms_is_running()
    if running:
        ms_pause()
    try:
        purge_batch(paths)
    except Exception as exc:  # noqa: BLE001
        log.critical("purge_batch failed; leaving queue for retry: %r", exc, exc_info=True)
        if running and not DRY_RUN:
            ms_resume()
        return
    if running and not DRY_RUN:
        ms_resume()

    if DRY_RUN:
        try:                       # consume nothing in a dry run
            os.rename(proc, q)
        except OSError:
            pass
    else:
        proc.unlink(missing_ok=True)


def main() -> None:
    global DRY_RUN
    ap = argparse.ArgumentParser(description="Torrent-Ingest remote-deletion reaper.")
    ap.add_argument("--once", action="store_true", help="run one cycle then exit")
    ap.add_argument("--dry-run", action="store_true", help="detect + plan, delete NOTHING")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    ap.add_argument("--reset-baseline", action="store_true",
                    help="re-seed the snapshot from disk now, then exit")
    ap.add_argument("--approve", action="store_true",
                    help="approve the CURRENT confirmed-vanished set as a deliberate "
                         "delete: stamp it so the next cycle purges it despite the "
                         "circuit breaker, then exit")
    args = ap.parse_args()

    DRY_RUN = args.dry_run
    _setup_logging()

    if args.status:
        _print_status()
        return

    if args.reset_baseline:
        if not media_healthy():
            print("Refusing to reset baseline: drive not healthy.")
            return
        present = scan_tracked_media()
        _save_set(config.REAP_SNAPSHOT_FILE, present)
        _save_pending({})
        print(f"Baseline reset to {len(present)} files.")
        return

    if args.approve:
        if not media_healthy():
            print("Refusing to approve: drive not healthy (unmounted or Shows/ empty). "
                  "This is exactly the drive-fault case the breaker protects — do NOT "
                  "purge the backup while the drive is in doubt.")
            return
        _present, snapshot, _np, confirmed = _confirmed_missing()
        if snapshot == set() and not confirmed:
            print("No snapshot yet (first run) — nothing to approve.")
            return
        if not confirmed:
            print("Nothing is confirmed-vanished right now — nothing to approve.")
            return
        _save_approved(confirmed)
        titles = sorted({title_dir_of(r) or r for r in confirmed})
        print(f"Approved {len(confirmed)} deliberately-deleted file(s) across "
              f"{len(titles)} title(s) for purge.")
        print("The daemon's next cycle (<=%ds) will purge exactly these and empty "
              "the bins. Run `python3 reap.py --once` to do it now." % config.REAP_SCAN_INTERVAL_SEC)
        return

    if not _on_the_mini():
        log.info("Not the Mini (not the library host / Media-Syncer not in 'mini' role); reaper idle.")
        if args.once:
            return
        # Idle-loop rather than exit, so a launch agent doesn't thrash restart.
        while True:
            time.sleep(config.IDLE_INTERVAL_SEC)

    _acquire_lock()
    log.info("Reaper started%s.", " (DRY RUN)" if DRY_RUN else "")

    # Crash safety, ONCE at startup (not per cycle): if a prior run died and left MS
    # paused, resume it. Between cycles the settle-gate — not this — owns MS's resume,
    # so an intentional mid-drain hold is never prematurely woken.
    recover_media_syncer()

    if args.once:
        drain_deletions_queue()
        return

    while True:
        try:
            drain_deletions_queue()
        except KeyboardInterrupt:
            log.info("Interrupted; exiting.")
            break
        except Exception as exc:  # noqa: BLE001
            log.critical("Cycle error (continuing): %r", exc, exc_info=True)
        time.sleep(config.REAP_SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
