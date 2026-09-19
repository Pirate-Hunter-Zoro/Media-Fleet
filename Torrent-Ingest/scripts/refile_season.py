#!/usr/bin/env python3
"""Re-file a show's episodes from a WRONG season folder to the right one, MEGA-side.

WHY THIS IS A TOOL AND NOT A ONE-OFF
    This is the fourth time the fleet has filed a pack under the wrong season number and
    needed the files moved back:
      §4.49  Dawn of the Croods S03 pack   -> filed as Season 02
      §4.54  That '90s Show S03 pack       -> filed as Season 02
      §4.91  Dawn of the Croods S04 pack   -> filed as Season 05
    Each repair was done by hand, from scratch, under time pressure, against a live
    write-once library. The guard added in `library._reject_season_gap` should stop new
    instances, but the repair itself deserves to be a reviewed, resumable, verified thing
    rather than a fresh shell loop each time.

HOW IT DECIDES WHAT TO MOVE -- and what it refuses to do
    SEASON MODE (default): the move set comes from the JOURNAL RECORD'S OWN PLAN, never
    from a glob over the season folder. §4.49 is explicit about why: the season folder also
    holds older files from a previous era of the same show, filed under a different naming
    scheme, and a glob sweeps those up too. The plan names exactly the files this ingest
    placed. Before moving anything it checks the plan's SOURCE filenames: they must state
    the season being moved TO. If the sources are silent, it refuses -- moving media on an
    assumption is the failure that produced three of the four incidents above.

    MAPPING MODE (`--mapping file.json`): a per-file move set for the repairs the season
    mode cannot express -- an episode number that is wrong within its own season, or a
    cross-season remap where one release serial folds to many broadcast slots (Doctor Who
    1963, 2026-09-15). The JSON is a list of `{"old": "<library-relative>", "new": "..."}`
    pairs and IS the reviewed evidence: it is checked for boundaries, collisions and
    already-existing destinations before anything moves, but the numbering argument lives
    in the file. This mode also updates the record and `library.db` in step with the bytes
    (`--record HASH`), and can re-arm torrent indices whose bytes are gone (`--rearm`).

    Sidecars (`.nfo`, `-thumb.jpg`) are DELETED rather than moved: they carry the metadata
    the wrong number fetched, so carrying them across would preserve the error under a
    corrected name. Jellyfin regenerates them. Mapping mode deletes only the sidecars of
    the files it moves -- a season-wide sweep would take correctly-filed neighbours too.

USAGE
    python3 scripts/refile_season.py --show "Dawn of the Croods (2015)" --from 5 --to 4
    python3 scripts/refile_season.py --show "..." --from 5 --to 4 --apply
    python3 scripts/refile_season.py --mapping state/doctorwho_map.json \
        --record e099421feeda2f49a2e5bdfef3a85b120e234dc2 --rearm 5,6,7 --apply
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                          # noqa: E402
import journal                                                         # noqa: E402
import library                                                         # noqa: E402
import rclone_conf                                                     # noqa: E402

SYNCER = Path.home() / "Developer/Media-Orchestrator/Media-Syncer"
INVENTORY = SYNCER / "remote_inventory.json"
SYNC_STATE = SYNCER / "sync_state.json"
UPLOAD_LOG = Path.home() / "Library/Logs/MediaSync.err"
ALREADY_GONE = ("directory not found", "doesn't exist", "not found", "no such file")

SIDECAR_SUFFIXES = (".nfo", "-thumb.jpg", "-thumb.png", ".jpg")
_SRC_SEASON_RE = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})E\d{1,3}", re.IGNORECASE)


def _rclone(args, timeout=180):
    proc = subprocess.run([config.RCLONE_BIN, "--config", str(config.RCLONE_CONFIG), *args],
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stderr or "") + (proc.stdout or "")


def _strip_session(remote) -> None:
    """Drop a remote's cached MEGA session so the next call logs in fresh.

    Delegates to `rclone_conf.strip_session`, which holds the CROSS-PROCESS lock this used
    to lack. The old copy here took a `threading.Lock`, which serialized this tool's own
    worker pool and nothing else -- while mediasync's session purge and the account
    provisioner rewrite the very same file from other processes. That race was observed
    live (`didn't find section in config file ("automega113")`, 2026-08-31 22:18) and it is
    the worst kind available: losing a section loses an ACCOUNT, and with it the only copy
    of whatever single-residence files live on it.
    """
    rclone_conf.strip_session(config.RCLONE_CONFIG, remote)


def _moveto(remote, src, dst):
    for attempt in (1, 2):
        rc, out = _rclone(["moveto", f"{remote}:{src}", f"{remote}:{dst}", "--timeout", "120s"])
        low = out.lower()
        if rc == 0:
            return True, "moved"
        if any(s in low for s in ALREADY_GONE):
            return True, "absent"
        if ("invalid arguments" in low or "couldn't login" in low) and attempt == 1:
            _strip_session(remote)
            _rclone(["about", f"{remote}:"], timeout=90)
            continue
        return False, (out.strip().splitlines() or [f"rc={rc}"])[-1][:150]
    return False, "unreachable"


def _upload_targets(prefix):
    out = defaultdict(set)
    rx = re.compile(r"Uploading '(.+)' to ([A-Za-z0-9_]+)")
    try:
        with UPLOAD_LOG.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if prefix not in line:
                    continue
                m = rx.search(line)
                if m:
                    out[m.group(1)].add(m.group(2))
    except OSError:
        pass
    return out


def build_moves(show, frm, to):
    """[(old_rel, new_rel, [remotes])] from the journal plans that filed into `frm`."""
    old_dir = f"Shows/{show}/Season {frm:02d}/"
    moves = []
    evidence = []
    for rec in journal.load_records().values():
        for f in ((rec.get("plan") or {}).get("files") or []):
            dst = f.get("dst_rel") or ""
            if not dst.startswith(old_dir):
                continue
            src_name = Path(f.get("src") or "").name
            m = _SRC_SEASON_RE.search(src_name)
            evidence.append((src_name, int(m.group(1)) if m else None))
            new = dst.replace(f"/Season {frm:02d}/", f"/Season {to:02d}/")
            new = re.sub(rf"S{frm:02d}E(\d+)", rf"S{to:02d}E\1", new)
            moves.append((dst, new))

    said = {s for _n, s in evidence if s is not None}
    if not said:
        raise SystemExit(
            f"REFUSED: none of the {len(evidence)} source filenames state a season, so "
            f"nothing here proves these episodes belong to Season {to:02d}. Establish the "
            f"season from the episode TITLES against TVMaze before moving media.")
    if said != {to}:
        raise SystemExit(
            f"REFUSED: the source filenames name season(s) {sorted(said)}, not {to}. "
            f"Moving them to Season {to:02d} would be an assumption, not a correction.")
    print(f"evidence: all {len(evidence)} source filenames state S{to:02d} "
          f"(e.g. {evidence[0][0][:70]})")

    inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
    uploads = _upload_targets(f"Shows/{show}/")
    out = []
    for old, new in sorted(set(moves)):
        remotes = set()
        res = inv.get(old)
        if isinstance(res, list) and res:
            remotes.add(res[0])
        remotes |= uploads.get(old, set())
        out.append((old, new, sorted(remotes)))
    return out


def build_moves_from_mapping(mapping_path):
    """[(old_rel, new_rel, [remotes])] from a reviewed JSON mapping.

    The mapping is either a bare list `[{"old": ..., "new": ...}, ...]` or an object
    `{"moves": [...], "rearm": [indices]}` (what the generator writes). It IS the reviewed
    evidence, so the checks here are structural, not editorial: boundaries, a 1:1
    destination set, and no destination that already exists (the library is write-once; a
    collision is a misreviewed map, not something to overwrite).
    """
    try:
        data = json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"REFUSED: cannot read mapping {mapping_path}: {exc}")
    rearm = []
    if isinstance(data, dict):
        rearm = [int(x) for x in (data.get("rearm") or []) if str(x).strip()]
        data = data.get("moves")
    if not isinstance(data, list) or not data:
        raise SystemExit("REFUSED: mapping must be a non-empty list of {old,new}")
    pairs, seen_new = [], set()
    for i, row in enumerate(data):
        old, new = (row or {}).get("old"), (row or {}).get("new")
        if not old or not new:
            raise SystemExit(f"REFUSED: mapping[{i}] needs non-empty old and new")
        for rel in (old, new):
            p = Path(rel)
            if p.is_absolute() or p.parts[:1] != ("Shows",):
                raise SystemExit(f"REFUSED: mapping[{i}] is not a Shows/-relative path: {rel}")
        if old == new:
            raise SystemExit(f"REFUSED: mapping[{i}] maps a file onto itself: {old}")
        if new in seen_new:
            raise SystemExit(f"REFUSED: two files map onto one destination: {new}")
        seen_new.add(new)
        pairs.append((old, new))
    for _old, new in pairs:
        # A destination that already exists is only acceptable when the file occupying it
        # is ITSELF being moved away in this same mapping. The Doctor Who repair is exactly
        # that shape: S01E07 -> S01E31 while S01E31 -> S02E11. The apply orders the moves so
        # a slot is vacated before it is filled; anything else is a misreviewed map.
        if new in {o for o, _n in pairs}:
            continue
        if (config.MEDIA_ROOT / new).exists() or (config.MEDIAFS_MOUNT / new).exists():
            raise SystemExit(f"REFUSED: destination already exists in the library: {new}")

    inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
    uploads = {}
    for prefix in {"/".join(old.split("/")[:3]) + "/" for old, _n in pairs}:
        uploads.update(_upload_targets(prefix))
    out = []
    for old, new in pairs:
        remotes = set()
        res = inv.get(old)
        if isinstance(res, list) and res:
            remotes.add(res[0])
        remotes |= uploads.get(old, set())
        out.append((old, new, sorted(remotes)))
    return out, rearm


def _rel_from_abs(p):
    for root in (config.MEDIA_ROOT.resolve(), config.MEDIAFS_MOUNT.resolve(),
                 config.MEDIA_ROOT, config.MEDIAFS_MOUNT):
        try:
            return str(Path(p).relative_to(root))
        except (ValueError, OSError):
            continue
    return None


def _rearm_indices(record, indices):
    """Clear indices from every proof list so the next wave RE-FETCHES them.

    `_carry_chunk_progress` carries an index only when it sits in `chunk_done`, and
    `chunk_dropped` is what keeps a declined file from being selected again. The bytes of
    an index whose plan dropped it are gone -- left in either list it is "proven" and the
    missing episode is never re-acquired. This is the one place those lists are edited
    outside `_advance_chunked`, and it is exactly why the repair parks the daemon first.
    """
    done = set(record.get("chunk_done") or [])
    dropped = set(record.get("chunk_dropped") or [])
    failed_idx = set(record.get("chunk_failed_idx") or [])
    attempts = dict(record.get("chunk_attempts") or {})
    cleared = []
    for i in indices:
        if str(i) in (record.get("chunk_filed") or {}):
            continue                      # bytes are filed; re-arming would re-download
        for coll in (done, dropped, failed_idx):
            coll.discard(i)
        attempts.pop(str(i), None)
        cleared.append(i)
    record["chunk_done"] = sorted(done)
    record["chunk_dropped"] = sorted(dropped)
    record["chunk_failed_idx"] = sorted(failed_idx)
    record["chunk_attempts"] = attempts
    return cleared


def update_record(info_hash, moves, rearm=(), wave_started_at=None):
    """Rewrite the journal record's filed paths and re-arm indices, in step with the bytes.

    `chunk_filed` maps torrent file index -> library-relative path, and `applied` holds
    absolute destinations; both are rewritten through the same old->new map so
    `_carry_chunk_progress`, `media_doctor`'s journal confirmation and the next wave all
    read the corrected layout. Returns counts for the log.
    """
    records = journal.load_records()
    rec = records.get(info_hash)
    if rec is None:
        raise SystemExit(f"REFUSED: no journal record for {info_hash}")
    mapping = {old: new for old, new, _r in moves}
    renamed = 0
    cf = dict(rec.get("chunk_filed") or {})
    for key, rel in list(cf.items()):
        if rel in mapping:
            cf[key] = mapping[rel]
            renamed += 1
    rec["chunk_filed"] = cf
    moved_applied = 0
    for entry in rec.get("applied") or []:
        rel = _rel_from_abs(entry.get("dst") or "")
        if rel and rel in mapping:
            entry["dst"] = str(config.MEDIA_ROOT / mapping[rel])
            moved_applied += 1
    cleared = _rearm_indices(rec, rearm)
    if wave_started_at is not None:
        rec["wave_started_at"] = wave_started_at
    journal.write_record(rec)
    return {"renamed": renamed, "applied": moved_applied, "rearmed": cleared}


def update_db(moves, show_title):
    """Mirror the moves into `library.db`: supersede the old rows, record the new ones.

    Uses the two primitives the fleet already trusts -- `dbhook.record_purge` (already
    verified in `test_purge_db_sync.py`) for the paths that are gone, and
    `dbhook.record_plan` for the corrected layout. A manual refile is otherwise invisible
    to the DB and the moved episodes read as "not owned" forever.
    """
    import dbhook                                                      # noqa: PLC0415
    old_rels = [old for old, _new, _r in moves]
    purged = dbhook.record_purge(old_rels)
    plan = {"title": show_title, "media_type": "show", "files": []}
    for old, new, _r in moves:
        m = re.search(r"[Ss](\d{1,3})[Ee](\d{1,4})", Path(new).name)
        f = {"dst_rel": new, "src": old}
        if m:
            f["season"], f["episode"] = int(m.group(1)), int(m.group(2))
        plan["files"].append(f)
    dbhook.record_plan(plan)
    return purged, len(plan["files"])


def _order_moves(moves):
    """Move pairs in an order whose destination is not a not-yet-vacated source.

    Mapping mode can contain chains (`S01E07 -> S01E31` while `S01E31 -> S02E11`). A
    `moveto` onto a live destination would clobber it, so a move is only safe once nothing
    that is still going to move sits on its destination. The loop repeats until every move
    is ordered; a genuine cycle falls back to parking that source at a unique temp name,
    which a later pass then moves to its real destination -- at the cost of an extra remote
    hop, and only when the map is a true cycle.
    """
    remaining = list(moves)
    ordered = []
    while remaining:
        sources = {old for old, _new, _r in remaining}
        free = [m for m in remaining if m[1] not in sources]
        if not free:
            old, new, remotes = remaining[0]
            tmp = f"{old}.refile-tmp-{len(ordered)}"
            ordered.append((old, tmp, remotes))
            remaining[0] = (tmp, new, remotes)
            continue
        for m in free:
            ordered.append(m)
        remaining = [m for m in remaining if m not in free]
    return ordered


def _rewrite_state(moves, tag):
    mapping = {o: n for o, n, _r in moves}
    for path in (INVENTORY, SYNC_STATE):
        data = json.loads(path.read_text(encoding="utf-8"))
        bak = path.with_suffix(path.suffix + f".bak-{tag}")
        if not bak.exists():
            bak.write_text(json.dumps(data), encoding="utf-8")
        n = 0
        for o, nw in mapping.items():
            if o in data:
                data[nw] = data.pop(o); n += 1
        tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
        print(f"  {path.name}: rewrote {n} key(s)")


def _parse_indices(spec):
    """`1,3,5-9` -> [1, 3, 5, 6, 7, 8, 9]. Ranges because a repair list like DW's
    `1-31, 41, 55-59, 92` is unreadable and error-prone expanded by hand."""
    out = []
    for tok in str(spec or "").replace(" ", "").split(","):
        if not tok:
            continue
        head, sep, tail = tok.partition("-")
        try:
            if sep:
                start, end = int(head), int(tail)
                if end < start or end - start > 100000:
                    raise ValueError(tok)
                out.extend(range(start, end + 1))
            else:
                out.append(int(tok))
        except ValueError:
            raise SystemExit(f"REFUSED: bad index list item {tok!r} (want N or N-M)")
    return out


def rearm_only(info_hash, indices):
    """Clear re-fetchable indices from every proof list WITHOUT moving a byte.

    The repair path for bytes that are GONE (`refile_season` mapping mode moves files;
    this is its complement, for when there is nothing left to move). `_rearm_indices`
    already holds the rule -- an index in `chunk_filed` is content the library holds,
    so re-arming it would re-download something already owned, and it is refused.
    This wrapper adds the rest of the re-drop contract: reset the wave clock so the
    next admission does not inherit a stale stall deadline, and put the re-arm in
    `decisions.log` where a human can see it. One journal write.

    Used by the DW (2005) repair (HANDOFF 10.2): 38 indices were freed unfiled by a
    wave's collision cleanup, and the record's `chunk_dropped` still listed them as a
    deliberate verdict, so a plain re-drop of the same `.torrent` was a no-op that
    immediately reported COMPLETED. Clearing them makes exactly those indices fetch
    again.
    """
    import time as _time                                                # noqa: PLC0415
    records = journal.load_records()
    rec = records.get(info_hash)
    if rec is None:
        raise SystemExit(f"REFUSED: no journal record for {info_hash}")
    cleared = _rearm_indices(rec, indices)
    if cleared:
        rec["wave_started_at"] = _time.time()
        journal.write_record(rec)
        journal.log_decision(
            info_hash, rec.get("name") or info_hash[:12],
            f"re-arm-only: {len(cleared)} index(es) made re-fetchable on the next "
            f"re-drop: {cleared}")
    return {"cleared": cleared}


def _apply_mapping(moves, rearm, args):
    """Sequential, ordered apply for mapping mode: remotes first, then locals.

    Sequential on purpose (the season mode parallelises across remotes): every move here
    can share a remote with another, and the chain ordering above must be respected on the
    remote side too.
    """
    ordered = _order_moves(moves)
    failures = 0
    for old, new, remotes in ordered:
        for r in remotes:
            ok, detail = _moveto(r, old, new)
            if not ok:
                failures += 1
                print(f"  ✗ {r}:{old} -> {detail}")
        lp = config.MEDIA_ROOT / old
        if lp.is_file():
            (config.MEDIA_ROOT / new).parent.mkdir(parents=True, exist_ok=True)
            lp.rename(config.MEDIA_ROOT / new)
    print(f"\nmove failures: {failures}")

    # Sidecars carry the WRONG metadata; per-file delete so correctly-filed neighbours in
    # the same season folder keep theirs.
    removed = 0
    for old, _new, _r in moves:
        stem = Path(old).with_suffix("")
        for suffix in SIDECAR_SUFFIXES:
            p = config.MEDIA_ROOT / Path(str(stem) + suffix)
            if p.is_file():
                p.unlink()
                removed += 1
    print(f"stale sidecars deleted: {removed} (Jellyfin regenerates them)")

    if failures:
        print("\nNOT rewriting state keys while moves are outstanding. Re-run --apply.")
        return 1
    _rewrite_state(moves, "remap")
    if args.record:
        import time as _time                                            # noqa: PLC0415
        counts = update_record(args.record, moves, rearm,
                               wave_started_at=_time.time())
        print(f"  journal {args.record[:12]}: {counts['renamed']} chunk_filed path(s), "
              f"{counts['applied']} applied entr(ies), re-armed "
              f"{len(counts['rearmed'])} index(es) {counts['rearmed']}")
    if args.show_title or args.show:
        purged, recorded = update_db(moves, args.show_title or args.show)
        print(f"  library.db: {purged.get('superseded', 0)} row(s) superseded, "
              f"{recorded} corrected file(s) recorded")
    else:
        print("  (no --show-title/--show given: library.db NOT updated)")
    print("\nre-file complete.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", help='e.g. "Dawn of the Croods (2015)"')
    ap.add_argument("--from", dest="frm", type=int)
    ap.add_argument("--to", type=int)
    ap.add_argument("--mapping", help="JSON list of {old,new} Shows/-relative pairs")
    ap.add_argument("--record", help="info hash whose chunk_filed/applied to rewrite")
    ap.add_argument("--rearm", default="",
                    help="comma-separated torrent indices to make re-fetchable")
    ap.add_argument("--rearm-only", action="store_true",
                    help="with --record/--rearm: clear the indices so a re-drop refetches "
                         "them, without moving anything (no --mapping needed)")
    ap.add_argument("--show-title", help="series title for library.db (mapping mode)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.rearm_only:
        if not (args.record and args.rearm):
            ap.error("--rearm-only needs --record HASH and --rearm i,j or i-J")
        indices = _parse_indices(args.rearm)
        if not args.apply:
            rec = journal.load_records().get(args.record) or {}
            filed = {int(k) for k in (rec.get("chunk_filed") or {}) if str(k).isdigit()}
            would = [i for i in indices if i not in filed]
            print(f"re-arm-only: {len(indices)} requested, {len(would)} would be cleared "
                  f"({len(indices) - len(would)} already filed, refused); pass --apply to "
                  f"write")
            return 0
        counts = rearm_only(args.record, indices)
        print(f"re-arm-only: cleared {len(counts['cleared'])} index(es): "
              f"{counts['cleared']}")
        return 0

    mapping_mode = bool(args.mapping)
    if mapping_mode:
        moves, file_rearm = build_moves_from_mapping(args.mapping)
        rearm = _parse_indices(args.rearm) or file_rearm
        print(f"mapping: {len(moves)} reviewed move(s)"
              + (f", {len(rearm)} index(es) to re-arm" if rearm else ""))
    else:
        if not (args.show and args.frm and args.to):
            ap.error("give --show/--from/--to, or --mapping")
        moves = build_moves(args.show, args.frm, args.to)
        rearm = []
        print(f"\nfiles to re-file: {len(moves)}")

    for old, new, remotes in moves[:8]:
        print(f"  {old}  ->  {new}   {remotes}")
    if len(moves) > 8:
        print(f"  ... and {len(moves) - 8} more")
    if not args.apply:
        print("\n(dry run -- pass --apply)")
        return 0

    if mapping_mode:
        return _apply_mapping(moves, rearm, args)

    # GROUPED BY (remote, destination directory), and SERIAL within a group. See the
    # original season-mode comment: two parallel moves into a not-yet-existing MEGA
    # directory race and one copy reads as MISSING forever.
    failures = []
    groups = defaultdict(list)
    for old, new, remotes in moves:
        for r in remotes:
            groups[(r, str(Path(new).parent))].append((old, new))

    def one_group(item):
        (remote, _dest_dir), pairs = item
        bad = []
        for old, new in pairs:
            ok, detail = _moveto(remote, old, new)
            if not ok:
                bad.append(f"{remote}:{old} -> {detail}")
        return bad

    for _old, new, _r in moves:
        (config.MEDIA_ROOT / new).parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=5) as ex:
        for bad in ex.map(one_group, list(groups.items())):
            failures.extend(bad)

    # The local copy is renamed once per file, after the remote moves.
    for old, new, _remotes in moves:
        lp = config.MEDIA_ROOT / old
        if lp.is_file():
            (config.MEDIA_ROOT / new).parent.mkdir(parents=True, exist_ok=True)
            lp.rename(config.MEDIA_ROOT / new)
    print(f"\nmove failures: {len(failures)}")
    for f in failures[:20]:
        print(f"  ✗ {f}")

    # Sidecars carry the WRONG season's fetched metadata; delete rather than carry over.
    removed = 0
    old_local = config.MEDIA_ROOT / f"Shows/{args.show}/Season {args.frm:02d}"
    if old_local.is_dir():
        for p in list(old_local.iterdir()):
            if p.is_file() and p.name.endswith(SIDECAR_SUFFIXES):
                p.unlink()
                removed += 1
        try:
            old_local.rmdir()
        except OSError:
            pass
    print(f"stale sidecars deleted: {removed} (Jellyfin regenerates them)")

    if failures:
        print("\nNOT rewriting state keys while moves are outstanding. Re-run --apply.")
        return 1
    _rewrite_state(moves, f"s{args.frm:02d}to{args.to:02d}")

    print("\nre-file complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
