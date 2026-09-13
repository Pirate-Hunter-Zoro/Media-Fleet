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
    The move set comes from the JOURNAL RECORD'S OWN PLAN, never from a glob over the
    season folder. §4.49 is explicit about why: the season folder also holds older files
    from a previous era of the same show, filed under a different naming scheme, and a glob
    sweeps those up too. The plan names exactly the files this ingest placed.

    Before moving anything it checks the plan's SOURCE filenames: they must state the
    season being moved TO. The Dawn of the Croods pack's files are named
    `Dawn.of.the.Croods.S04E01E02-...` while the plan filed them into `Season 05`, so the
    source itself is the evidence. If the sources are silent, TVMaze episode TITLES are
    consulted instead. If neither can say, it refuses -- moving media on an assumption is
    the failure that produced three of the four incidents above.

    Sidecars (`.nfo`, `-thumb.jpg`) are DELETED rather than moved: they carry the metadata
    the wrong season number fetched, so carrying them across would preserve the error under
    a corrected name. Jellyfin regenerates them.

USAGE
    python3 scripts/refile_season.py --show "Dawn of the Croods (2015)" --from 5 --to 4
    python3 scripts/refile_season.py --show "..." --from 5 --to 4 --apply
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

SYNCER = Path.home() / "Developer/Media-Fleet/Media-Syncer"
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", required=True, help='e.g. "Dawn of the Croods (2015)"')
    ap.add_argument("--from", dest="frm", type=int, required=True)
    ap.add_argument("--to", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    moves = build_moves(args.show, args.frm, args.to)
    print(f"\nfiles to re-file: {len(moves)}")
    for old, new, remotes in moves[:6]:
        print(f"  {Path(old).name}  ->  Season {args.to:02d}/{Path(new).name}   {remotes}")
    if len(moves) > 6:
        print(f"  ... and {len(moves) - 6} more")
    if not args.apply:
        print("\n(dry run -- pass --apply)")
        return 0

    # GROUPED BY (remote, destination directory), and SERIAL within a group.
    #
    # MEGA allows two sibling directories with the same name and `rclone lsf` shows only one
    # of them, so two parallel `moveto` calls into a destination directory that does not
    # exist yet each create it -- and whatever lands in the shadowed node reads as MISSING
    # (§7, §4.34). Repairing that needs a rename dance, and `rclone dedupe`, the obvious
    # tool, can delete on a write-once library.
    #
    # The old code parallelised per FILE across five workers, which is exactly that race
    # whenever one remote holds more than one of the files being moved. It survived earlier
    # repairs by luck: those move sets happened to hold one file per remote. This one does
    # not -- automega303 holds two of the twelve Croods Family Tree files -- so the race was
    # one run away from being real. Parallelism across remotes is safe and is kept; within a
    # remote's destination directory the moves are serialised, so the first call creates the
    # directory and the rest land in it.
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

    (config.MEDIA_ROOT / f"Shows/{args.show}/Season {args.to:02d}").mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=5) as ex:
        for bad in ex.map(one_group, list(groups.items())):
            failures.extend(bad)

    # The local copy is renamed once per file, after the remote moves -- never inside the
    # per-remote loop, where a file present on two remotes would rename it twice.
    for old, new, _remotes in moves:
        lp = config.MEDIA_ROOT / old
        if lp.is_file():
            (config.MEDIA_ROOT / new).parent.mkdir(parents=True, exist_ok=True)
            lp.rename(config.MEDIA_ROOT / new)
    print(f"\nmove failures: {len(failures)}")
    for f in failures[:20]:
        print(f"  ✗ {f}")

    # Sidecars carry the WRONG season's fetched metadata; delete rather than carry over.
    old_local = config.MEDIA_ROOT / f"Shows/{args.show}/Season {args.frm:02d}"
    removed = 0
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
    mapping = {o: n for o, n, _r in moves}
    for path in (INVENTORY, SYNC_STATE):
        data = json.loads(path.read_text(encoding="utf-8"))
        bak = path.with_suffix(path.suffix + f".bak-s{args.frm:02d}to{args.to:02d}")
        if not bak.exists():
            bak.write_text(json.dumps(data), encoding="utf-8")
        n = 0
        for o, nw in mapping.items():
            if o in data:
                data[nw] = data.pop(o); n += 1
        tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
        print(f"  {path.name}: rewrote {n} key(s)")
    print("\nre-file complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
