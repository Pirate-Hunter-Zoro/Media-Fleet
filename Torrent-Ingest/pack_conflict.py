#!/usr/bin/env python3
"""Resolve a blocked pack against a duplicate pack whose library footprint is displaced.

THE CALL THIS AUTOMATES (2026-09-26). Two releases of one show were in flight. The
single-episode pack mis-filed itself by a uniform +15 shift (release E01-E32 filed at
S01E16-E47); the complete-series pack then parked because its own files collided with
those misplaced copies. `_park_chunked_unfiled` parks by design -- a collision must never
delete -- so the resolution needed a human to notice that the displaced pack was fully
redundant with the complete one, purge it, and let the complete pack finish. This module
makes that call computationally, under the owner's standing rules.

WHAT IT REQUIRES BEFORE IT TOUCHES ANYTHING (all computed, no title constants):

  * COVERAGE. The blocked pack's own filenames must NAME every episode the blocker's
    copies hold (title coverage, exact and unique against the provider Jellyfin scrapes;
    a combined file's two titles both count). Superseding the copies therefore cannot
    lose content -- the survivor holds it.
  * DISPLACEMENT. Every copy in the blocker's footprint must be either an explained
    self-keyed agreement copy (its destination IS its recorded source key, confirmed by
    its own title) or part of ONE non-zero same-season episode shift. At least 60% of the
    displaced copies' own titles must confirm their source keys, so the shift is anchored
    by broadcast numbers and a title-swapped pair can ride the group but never anchor it.
    A deliberate renumber moves a file to another SEASON (an absolute run, a merged cour),
    and a release-order pack shifts its files by varying amounts (its own catalogue
    order), so neither is ever mistaken for the mistake; at least half the footprint must
    be displaced.
  * ONE CANDIDATE. Exactly one record may qualify; two candidates is ambiguous and fails
    open to the existing park.

The supersede goes through the one sanctioned path (`library.supersede_paths` queues the
local unlink and the MEGA purge; `dbhook.record_purge` supersedes the library.db rows),
the blocker is retired REFUSED -- a deliberate refusal, not a failure -- and its payload
is KEPT (`delete_files=False`, recoverable by a re-drop). Anything weaker leaves the park
standing, exactly as before.
"""

from __future__ import annotations

import re
from pathlib import Path

import config
import dbhook
import identify
import journal
import library
import qbt

_SLOT_RE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,4})")


def _source_path(record):
    """The record's source `.torrent`, wherever its life left it, or None."""
    tp = Path(record.get("torrent_path") or "")
    if tp.is_file():
        return tp
    for base in (config.FAILED_DIR, config.FINISHED_DIR,
                 config.STATE_DIR / "torrent_sources"):
        for cand in (base / tp.name, base / f"{record.get('info_hash', '')}.torrent"):
            try:
                if cand.is_file():
                    return cand
            except OSError:
                continue
    return None


def release_names(record):
    """The release's own file list, or None when it cannot be enumerated."""
    p = _source_path(record)
    if p is None:
        return None
    try:
        files = qbt.file_list_from_file(p)
    except Exception:                                                     # noqa: BLE001
        return None
    if not files:
        return None
    return [str(name) for name, _size in files]


def _slot_in_name(name):
    m = _SLOT_RE.search(Path(str(name)).name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _slot_in_rel(rel):
    parts = Path(str(rel)).parts
    if len(parts) < 4 or parts[0] != "Shows":
        return None
    m = _SLOT_RE.search(parts[-1])
    return (int(m.group(1)), int(m.group(2))) if m else None


def _show_folder(rel):
    parts = Path(str(rel)).parts
    return parts[1] if len(parts) >= 2 and parts[0] == "Shows" else None


def filed_copies(record, names):
    """`[{rel, src}]` -- the library paths this record filed, with the release name each
    came from when it can be joined. Chunked records index into the release list; a
    whole-torrent record carries its plan's src. A copy that cannot be joined is dropped
    (it contributes no evidence either way)."""
    out = []
    cf = record.get("chunk_filed") or {}
    if cf and names:
        for key, rel in sorted(cf.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
            try:
                i = int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(names):
                out.append({"rel": str(rel), "src": names[i]})
        return out
    plan = record.get("plan") or {}
    for f in plan.get("files") or []:
        if not isinstance(f, dict):
            continue
        rel, src = str(f.get("dst_rel") or ""), str(f.get("src") or "")
        if rel and src:
            out.append({"rel": rel, "src": Path(src).name})
    return out


def displaced_footprint(record, names, guide):
    """The record's copies when the footprint is PROVABLY a displaced duplicate, else None.

    Returns `(copies, content_slots, folder)`: `copies` are the library paths to purge,
    `content_slots` the guide slots their own titles name. The proof, per copy:

      * a copy at its own source key (`dst == key`) is an AGREEMENT copy and is left
        alone -- but only when its own title confirms that key, so a release-order file
        (whose key is not the broadcast slot) never slips through as "fine";
      * any other copy must stay in its key's SEASON (a cross-season move is a deliberate
        library renumber, not this mistake), and its own title must confirm its source
        key (so the destination is the error, not a release-order convention);
      * every displaced copy must share ONE non-zero episode shift, and they must be at
        least half the footprint. A well-formed pack has none; a release-order pack's
        shifts vary; a partially-shifted pack is broken and its displaced half is exactly
        what the surviving release covers.

    Anything unreadable or unexplained returns None, so the park stands.
    """
    raw = filed_copies(record, names or [])
    if not raw:
        return None
    parsed = []
    for c in raw:
        key = _slot_in_name(c["src"])
        dst = _slot_in_rel(c["rel"])
        if key is None or dst is None:
            return None                     # a footprint that cannot be read fails open
        slots = identify.match_guide_titles(
            identify.release_file_titles(c["src"]), guide)
        if not slots:
            return None                     # a copy with no readable title fails open
        parsed.append((c, key, dst, slots))
    # One show folder only: a blocker spanning several shows is not one uniform mistake.
    folder = _show_folder(parsed[0][0]["rel"])
    if not folder or any(_show_folder(c["rel"]) != folder for c, _k, _d, _s in parsed):
        return None
    displaced, shifts, content_slots, confirmed = [], set(), set(), 0
    for c, key, dst, slots in parsed:
        if dst == key:
            if key not in slots:
                return None                 # an unexplained copy at its own key
            continue
        if dst[0] != key[0]:
            return None                     # a cross-season move is a library layout
        displaced.append(c)
        shifts.add(dst[1] - key[1])
        content_slots |= slots
        if key in slots:
            confirmed += 1                  # the source key IS the broadcast slot here
    if len(displaced) < 2 or len(shifts) != 1 or 0 in shifts:
        return None                         # not one uniform non-zero shift
    if len(displaced) < len(parsed) * 0.5:
        return None                         # a mostly-correct pack is not a duplicate
    # What anchors the proof is a CONFIRMED source key, not mere displacement: a release
    # can swap two of its own numbers (an E01/E02 title swap) while the shift stays one
    # constant, and those two copies may ride the group but never anchor it. The same 60%
    # floor the title-map witnesses use keeps one coincidental match from authorizing a
    # purge.
    if confirmed < max(2, int(len(displaced) * 0.6)):
        return None
    return displaced, content_slots, folder


def _target_folders(record, names):
    """The show folders the blocked record itself says it belongs to."""
    folders = set()
    for rel in (record.get("chunk_filed") or {}).values():
        f = _show_folder(rel)
        if f:
            folders.add(f)
    for entry in record.get("applied") or []:
        rel = journal._library_relative(entry.get("dst") or "")
        if rel:
            f = _show_folder(rel)
            if f:
                folders.add(f)
    if not folders and names:
        title = identify._release_title_guess(record.get("content_path") or "", names)
        found = library.find_show_folder(title) if title else None
        if found is not None:
            folders.add(Path(found).name)
    return sorted(folders)


def plan_resolution(record, records=None):
    """Find whether a parked record can be unblocked by superseding a duplicate pack.

    Returns `{folder, blocker, copies, content_slots, target_slots, reason}` when a
    single fully-displaced, fully-covered blocker exists, else `{reason}` describing why
    not. Read-only: the caller decides whether to `apply`.
    """
    records = records if records is not None else journal.load_records()
    names = release_names(record)
    if not names:
        return {"reason": "the blocked record's release list cannot be read"}
    folders = _target_folders(record, names)
    if not folders:
        return {"reason": "the blocked record names no show folder"}
    for folder in folders:
        tmdb_id = identify.folder_tmdb_id(folder)
        target_slots, provider = identify.release_covered_slots(
            names, show_hint=folder, tmdb_id=tmdb_id)
        if not target_slots:
            continue
        guide, _p = identify._guide_for(folder, tmdb_id)
        if not guide:
            return {"reason": f"no provider guide for {folder!r}"}
        candidates = []
        for h, other in records.items():
            if h == record.get("info_hash") or other.get("info_hash") == record.get("info_hash"):
                continue
            found = displaced_footprint(other, release_names(other), guide)
            if found is None:
                continue
            copies, content_slots, blob_folder = found
            if blob_folder != folder:
                continue
            if not content_slots or not content_slots <= target_slots:
                continue
            candidates.append((h, other, copies, content_slots))
        if len(candidates) > 1:
            return {"reason": f"{len(candidates)} displaced duplicate packs in "
                              f"{folder!r} match; ambiguous, leaving the park"}
        if candidates:
            h, other, copies, content_slots = candidates[0]
            return {"folder": folder, "provider": provider, "blocker_hash": h,
                    "blocker": other, "copies": copies,
                    "content_slots": sorted(content_slots),
                    "target_slots": sorted(target_slots)}
    return {"reason": "no displaced duplicate pack is fully covered by this release"}


def apply_resolution(plan, client=None):
    """Supersede the blocker footprint and retire it REFUSED. Returns a summary."""
    blocker = plan["blocker"]
    rels = [c["rel"] for c in plan["copies"]]
    library.supersede_paths(rels)
    purged = dbhook.record_purge(rels)
    h = plan["blocker_hash"]
    if client is not None:
        try:
            qbt.remove(client, h, delete_files=False)
        except Exception as exc:                                          # noqa: BLE001
            print(f"[pack_conflict] could not remove {h} from qBittorrent: {exc}")
    reason = (f"superseded: its {len(rels)} library file(s) were filed at one uniform "
              f"episode shift from the release numbering, and a separate in-flight "
              f"release names every episode it holds; its footprint was superseded in "
              f"favour of that release (payload kept, re-droppable)")
    import ingest                                                         # noqa: PLC0415
    ingest._fail(blocker, reason, refused=True)
    journal.log_decision(h, blocker.get("name") or "",
                         f"pack_conflict: {reason}; purged {len(rels)} path(s), "
                         f"library.db superseded {purged.get('superseded', 0)} row(s)")
    return {"purged": rels, "db": purged, "reason": reason}


def resolve_parked(record, client, records=None):
    """The daemon hook: resolve a park in place, or tell the caller to park as before.

    Returns True only when a blocker was superseded; the caller must then NOT park --
    the wave retries next cycle against a library the blocker no longer occupies.
    """
    plan = plan_resolution(record, records)
    if "blocker" not in plan:
        return False
    summary = apply_resolution(plan, client)
    print(f"[pack_conflict] {record.get('name')}: superseded duplicate pack "
          f"{plan['blocker'].get('name')} ({len(summary['purged'])} file(s)); the "
          f"blocked wave will retry", flush=True)
    return True
