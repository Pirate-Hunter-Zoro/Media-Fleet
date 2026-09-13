"""Reconcile `completed` journal records against the remote inventory / local mount.

The ingest journal marks a torrent `completed` the moment its files are verified in the
local SSD library root. That is a cache, not durability: the durable copy lives on MEGA,
in Media-Syncer's `remote_inventory.json`. A completion whose video files are GONE from
both the local tree and the remote inventory is a stale lie -- the file was evicted
locally without ever landing on MEGA -- and must be re-acquired, not trusted.

This module audits every `completed` record and, for any whose applied files are absent
from BOTH the remote inventory and the local mount, moves its source `.torrent` back to
the watch folder (re-queuing it) and resets the record so the pipeline re-downloads it.
The audit is cheap (one read of remote_inventory.json, cached across calls) and safe: it
never deletes anything, and it only re-queues a torrent whose files are provably gone.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import config
import journal


# Cache of the remote inventory key set, with the (mtime_ns, size) it was read from, so a
# steady-state sweep does not re-parse a multi-megabyte JSON file every cycle.
_inv_cache: dict = {"stamp": None, "keys": None}


def _remote_keys() -> set[str] | None:
    """The set of library-relative paths Media-Syncer has on MEGA, or None if unreadable."""
    p = config.MEDIA_SYNCER_INVENTORY
    try:
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        _inv_cache["keys"] = None
        return None
    if _inv_cache["keys"] is not None and _inv_cache["stamp"] == stamp:
        return _inv_cache["keys"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _inv_cache["keys"] = None
        return None
    keys = set(data.keys()) if isinstance(data, dict) else set()
    _inv_cache["stamp"] = stamp
    _inv_cache["keys"] = keys
    return keys


def _applied_rel(record) -> list[str]:
    """Library-relative paths this record filed, in the remote-inventory key form."""
    out = []
    for f in (record.get("applied") or []):
        dst = f.get("dst")
        if not dst:
            continue
        try:
            rel = Path(dst).relative_to(config.MEDIA_ROOT.resolve())
        except ValueError:
            continue
        out.append(rel.as_posix())
    return out


def _is_present_local(rel: str) -> bool:
    return (config.MEDIA_ROOT / rel).exists()


def reconcile(records: dict, log_fn=None) -> int:
    """Re-queue completed torrents whose files are gone from MEGA and from disk.

    Returns the number of records re-queued. Never raises: a reconciliation problem must
    not take the ingest daemon down. `records` is `journal.load_records()`'s
    last-writer-wins map; a record that is re-queued has its status reset so the next
    cycle picks its `.torrent` back up from the watch folder top level.
    """
    log = log_fn or (lambda msg: None)
    remote = _remote_keys()
    if remote is None:
        # Can't prove anything is missing without the inventory. Do NOT re-queue on a
        # missing/unreadable inventory -- that would re-download everything we own.
        log("reconcile: remote inventory unreadable; skipping audit")
        return 0

    re_queued = 0
    for h, rec in list(records.items()):
        if rec.get("status") != journal.COMPLETED:
            continue
        if rec.get("reconcile_dead"):
            continue
        rels = _applied_rel(rec)
        if not rels:
            continue
        # A completion is trustworthy if ANY applied file is still on MEGA or on disk.
        # (Mixed torrents: some files may legitimately live outside the pool, e.g. Novels
        # on Google Drive; those are absent from remote inventory but present locally.)
        if any(r in remote or _is_present_local(r) for r in rels):
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
