"""Reconcile `completed` journal records against the remote inventory / local mount.

The ingest journal marks a torrent `completed` the moment its files are verified in the
local SSD library root. That is a cache, not durability: the durable copy lives on MEGA,
in Media-Syncer's `remote_inventory.json`, and the MOUNT (`MEDIAFS_MOUNT`) is the only
complete local view -- `~/Media` is the SSD tier and the tier engine evicts freely
(HANDOFF §2.1).

This module audits every `completed` record and re-queues only a completion whose applied
files are provably gone from every witness:

  * the remote inventory, by key;
  * the SSD (`MEDIA_ROOT`) and the mount (`MEDIAFS_MOUNT`), by key -- the old check
    stopped at the SSD, so a freshly-filed or evicted file read as "gone" until the next
    inventory refresh;
  * the remote inventory under a MOVED path. A library-internal move keeps the file's
    name and byte size, and the inventory carries both, so a shelf/franchise migration
    that re-keyed a file is not a deletion. The 2026-09-20 One Piece migration moved 191
    files; to the path-only check every moved chapter completion read as "gone" and was
    re-queued, re-downloaded and re-filed on a loop, and the five covered ones were then
    purged again by the chapter reconciler -- a storm that competed with the Smurfs
    re-fetch for the same constrained provider budget;
  * library.db, which is the ledger of a DELIBERATE supersede. Content the fleet purged
    on purpose (a volume covers its chapters, a better copy replaced it) is closed
    (`reconcile_closed`) rather than re-acquired, because re-acquiring it only runs the
    purge again.

The audit is cheap (one cached read of remote_inventory.json) and safe: it never deletes
anything, and it only re-queues a torrent whose files are gone by every computed witness.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import config
import journal


# Cache of the inventory, with the (mtime_ns, size) it was read from, so a steady-state
# sweep does not re-parse a multi-megabyte JSON file every cycle. `keys` answers the path
# lookup; `index` answers "does this content exist under a moved path" by (basename,
# bytes) -- the two facts a completed record needs.
_inv_cache: dict = {"stamp": None, "keys": None, "index": None}

# Metadata backups are COPIES, not library content: a match there must never stand in for
# a file missing from the library itself.
_BACKUP_PREFIX = "metadata-backup/"


def _remote_views() -> tuple[set[str] | None, dict]:
    """(inventory key set, {(basename, int size): [rel, ...]}) or (None, {})."""
    p = config.MEDIA_SYNCER_INVENTORY
    try:
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        _inv_cache.update({"stamp": None, "keys": None, "index": None})
        return None, {}
    if _inv_cache["keys"] is not None and _inv_cache["stamp"] == stamp:
        return _inv_cache["keys"], _inv_cache["index"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _inv_cache.update({"stamp": None, "keys": None, "index": None})
        return None, {}
    keys: set[str] = set()
    index: dict = {}
    if isinstance(data, dict):
        for rel, meta in data.items():
            rel = str(rel)
            if rel.startswith(_BACKUP_PREFIX):
                continue
            keys.add(rel)
            size = meta[2] if isinstance(meta, (list, tuple)) and len(meta) >= 3 else None
            try:
                size = int(size)
            except (TypeError, ValueError):
                continue
            index.setdefault((Path(rel).name, size), []).append(rel)
    _inv_cache.update({"stamp": stamp, "keys": keys, "index": index})
    return keys, index


def _remote_keys() -> set[str] | None:
    """The set of library-relative paths Media-Syncer has on MEGA, or None if unreadable.

    Kept as its own name: `ingest._episode_keys_in_library` uses it to ask which episodes
    the library holds before treating an empty plan as "already present".
    """
    return _remote_views()[0]


def _is_present_local(rel: str) -> bool:
    """The file exists in the local tiers -- SSD or (crucially) the MOUNT.

    The SSD alone is a cache: the tier engine evicts a file to the pool once it is
    uploaded, and `MEDIA_ROOT/<rel>` then answers False while the mount still serves it
    (HANDOFF §2.1). The module's docstring always claimed "the local mount"; the code
    checked only the SSD, which is how a freshly-filed file read as gone.
    """
    return ((config.MEDIA_ROOT / rel).exists()
            or (config.MEDIAFS_MOUNT / rel).exists())


def _is_present(rel: str, size, keys: set, index: dict) -> bool:
    """This applied entry still exists somewhere the fleet can see it."""
    if rel in keys or _is_present_local(rel):
        return True
    if size is None:
        return False
    return bool(index.get((Path(rel).name, size)))


def resolve_moved(rel: str, size, keys: set, index: dict) -> str | None:
    """The one inventory key this moved file now lives at, or None when unsure.

    Exact identity only: same basename AND same byte count. An exact key hit, a local
    file, a missing size, or more than one candidate all answer None -- and None means
    "never rewrite", so an ambiguity is reported, not guessed.
    """
    if rel in keys or _is_present_local(rel) or size is None:
        return None
    cands = [c for c in index.get((Path(rel).name, size), []) if c != rel]
    return cands[0] if len(cands) == 1 else None


def _applied_entries(record) -> list[tuple[str, int | None]]:
    """(library-relative path, size) for every applied file under the library root."""
    out: list[tuple[str, int | None]] = []
    for f in (record.get("applied") or []):
        dst = f.get("dst")
        if not dst:
            continue
        try:
            rel = Path(dst).relative_to(config.MEDIA_ROOT.resolve())
        except ValueError:
            continue
        size = f.get("size")
        try:
            size = int(size) if size is not None else None
        except (TypeError, ValueError):
            size = None
        out.append((rel.as_posix(), size))
    return out


def _superseded_evidence(rels) -> bool:
    """library.db's verdict that every path names deliberately-superseded content.

    False whenever the DB cannot say -- no row, an owned row, a collection it must not
    guess, any error -- so an uncertain record keeps the historical re-queue path.
    """
    try:
        import dbhook                                                   # noqa: PLC0415
        return dbhook.purged_evidence(rels)
    except Exception:                                                   # noqa: BLE001
        return False


def audit(record, keys: set, index: dict, evidence=None) -> str:
    """`present` | `superseded` | `missing` | `skip` for one completed record."""
    entries = _applied_entries(record)
    if not entries:
        return "skip"
    if any(_is_present(rel, size, keys, index) for rel, size in entries):
        return "present"
    if (evidence or _superseded_evidence)([rel for rel, _s in entries]):
        return "superseded"
    return "missing"


def reconcile(records: dict, log_fn=None) -> int:
    """Re-queue completed torrents whose files are gone from MEGA and from disk.

    Returns the number of records re-queued. A record whose content library.db proves was
    deliberately superseded is CLOSED instead (`reconcile_closed`), never re-acquired.
    Never raises: a reconciliation problem must not take the ingest daemon down.
    """
    log = log_fn or (lambda msg: None)
    keys, index = _remote_views()
    if keys is None:
        # Can't prove anything is missing without the inventory. Do NOT re-queue on a
        # missing/unreadable inventory -- that would re-download everything we own.
        log("reconcile: remote inventory unreadable; skipping audit")
        return 0

    re_queued = 0
    for h, rec in list(records.items()):
        if rec.get("status") != journal.COMPLETED:
            continue
        if rec.get("reconcile_dead") or rec.get("reconcile_closed"):
            continue
        verdict = audit(rec, keys, index)
        if verdict == "superseded":
            rec["reconcile_closed"] = "superseded"
            rec["error"] = "reconcile: content deliberately superseded; not re-acquiring"
            journal.write_record(rec)
            log(f"reconcile: {rec.get('name') or h[:12]} was deliberately superseded; "
                f"closing the completion")
            continue
        if verdict != "missing":
            continue
        if _re_queue(h, rec, log):
            re_queued += 1
    return re_queued


def _re_queue(info_hash: str, record: dict, log_fn) -> bool:
    """Move the source `.torrent` from finished/ back to the watch folder top level and
    reset the journal record so the daemon re-ingests it. Idempotent and non-destructive:
    the only thing changed is the .torrent's location and the record's status. Returns True
    when the record was re-queued; False when there was nothing to re-queue from (the
    source `.torrent` is itself gone), in which case the record is marked `reconcile_dead`
    so the audit stops re-visiting a completion it can never act on."""
    tp = Path(record.get("torrent_path") or "")
    target = config.TORRENTS_DIR / (tp.name or f"{info_hash[:12]}.torrent")
    try:
        if tp.exists():
            config.TORRENTS_DIR.mkdir(parents=True, exist_ok=True)
            if tp.resolve() != target.resolve():
                os.replace(tp, target)
        elif not target.exists():
            # The source .torrent is itself gone; nothing to re-queue from. Mark the record
            # so this stale completion is not re-audited (and re-logged) every cycle.
            record["reconcile_dead"] = True
            record["error"] = ("reconcile: files gone and no source .torrent to re-queue "
                               f"({tp})")
            journal.write_record(record)
            log_fn(f"reconcile: {record.get('name') or info_hash[:12]} files gone and no "
                   f"source .torrent to re-queue ({tp}); marked unrecoverable")
            return False
    except OSError as exc:
        log_fn(f"reconcile: could not re-queue {info_hash[:12]}: {exc}")
        return False

    record["status"] = journal.QUEUED
    record["torrent_path"] = str(target)
    record["error"] = "re-queued: files absent from remote inventory and local mount"
    journal.write_record(record)
    log_fn(f"reconcile: re-queued {record.get('name') or info_hash[:12]} "
           f"({len(record.get('applied') or [])} file(s) gone from MEGA and disk)")
    return True
