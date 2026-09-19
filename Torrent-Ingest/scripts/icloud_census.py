#!/usr/bin/env python3
"""Census the fleet's iCloud control directory, so the NEXT vanishing has a before.

WHY THIS EXISTS AT ALL. Four times in six weeks, content disappeared from
`~/Library/Mobile Documents/com~apple~CloudDocs/Torrents` in an orderly way around 05:46.
Every investigation was retrospective and every one failed for the same unmeasured reason:
the unified log retains about NINE HOURS on this machine, and the fleet is chatty enough to
roll it before anyone looks. `log show` can never explain a §4.13 event after the fact, no
matter how fast the investigation starts. The machine did not crash (continuous uptime
across two of the events, zero panic reports, the ingest daemon logged straight through);
what died was qBittorrent, and the surviving hypothesis is an iCloud sync event from another
device -- both days' casualties were iCloud Drive content.

So the only thing that can ever answer it is a record written BEFORE the next one. That is
all this does: append one JSON line per run to a log the fleet never rotates, holding the
per-subdirectory file counts, byte totals and newest mtimes, plus every top-level file. It
is deliberately small -- 830 bytes a run measured, so 39 KB/day and about 14 MB/year at the
30-minute interval it ships with -- because a log that gets rotated, pruned or compacted is
exactly the failure it exists to avoid. Nothing prunes it. If it ever needs to shrink,
sample less often; never truncate the history, which is the only part with any value.

WHAT IT WATCHES AND WHY IT MATTERS. This directory is not incidental -- it is the fleet's
control plane. `find.txt` is Title-Scout's inbox; `queued/`, `ingesting/`, `finished/` and
`failed/` carry work in flight, and the owner hand-drops every `.torrent` here. An iCloud
sync event that empties it does not just lose files, it silently erases the owner's
instructions to the fleet.

ONE FOLDER IS EXEMPT FROM THE DROP VERDICT, AND ONLY THE VERDICT. `DirectIngest/` is the
bridge's drop mirror (`direct_ingest_bridge.py`), and it is drained BY DESIGN: every drop
that appears is MOVED to the local watch folder, so its file count falls constantly. A
decrease there is the fleet working, not an iCloud vanishing -- flagging it would put a
"drop" in the one log that is supposed to be nothing but real ones. It is still CENSUSED
(the snapshot records its counts, so a vanish before a bridge pass is still visible in the
log by comparing snapshots), just never promoted to a `"drop"` list entry.

NOTE (2026-09-10): `new.txt`, `compilations.txt`, `acquisition_mode.txt` and
`would_download.txt` were REMOVED DELIBERATELY when torrent/comic discovery was deleted from
the fleet -- they were the searcher's inbox, mode switch and drop ledger, and nothing reads
them now. The census recorded that as a drop, correctly; it is the one drop in this log with
a known cause. `new.txt` used to be the only admission path and no longer exists as one.

The census also DETECTS: when a subdirectory's file count falls, or a top-level file that
existed last time is gone, the line is stamped `"drop": [...]` and the tool exits 1 and
prints to stderr. That is what makes it evidence rather than telemetry -- the event names
itself in the log, at the time it happens, instead of waiting for someone to diff two
snapshots by hand months later.

    python3 scripts/icloud_census.py            # take a census, append, report a drop
    python3 scripts/icloud_census.py --report    # summarise the log, list every drop seen
    python3 scripts/icloud_census.py --selftest  # both directions, on fixtures

Exit 0 = censused, nothing lost. Exit 1 = SOMETHING VANISHED since the last census.
Read-only with respect to the watched directory: it never writes, moves or deletes there.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Torrents"))
# Deliberately NOT under state/: nothing in the fleet rotates, prunes or resets this file,
# and no tool but this one writes it. That is the whole point (§4.13).
LOG = Path(os.path.expanduser("~/Developer/Media-Orchestrator/Torrent-Ingest/state/icloud_census.jsonl"))


def census(root: Path) -> dict:
    """One snapshot: per-subdir counts/bytes/newest-mtime, plus top-level files."""
    snap = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "root": str(root),
            "dirs": {}, "files": {}, "ok": root.is_dir()}
    if not snap["ok"]:
        return snap
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        try:
            if entry.is_dir():
                n = 0
                total = 0
                newest = 0.0
                for dirpath, _dirnames, filenames in os.walk(entry):
                    for fn in filenames:
                        try:
                            st = os.stat(os.path.join(dirpath, fn))
                        except OSError:
                            continue
                        n += 1
                        total += st.st_size
                        newest = max(newest, st.st_mtime)
                snap["dirs"][entry.name] = {"n": n, "bytes": total,
                                            "newest": round(newest, 1)}
            elif entry.is_file():
                st = entry.stat()
                snap["files"][entry.name] = {"bytes": st.st_size,
                                             "mtime": round(st.st_mtime, 1)}
        except OSError:
            continue
    return snap


# Folders whose falling count is the fleet working, not a loss (see the module docstring).
_TRANSIENT_DIRS = {"DirectIngest"}


def diff(prev: dict, cur: dict) -> list[str]:
    """What is GONE or SMALLER since the previous census. Growth is never a drop, and a
    transient drop folder's decrease (the bridge draining it) is not one either."""
    if not prev or not prev.get("ok") or not cur.get("ok"):
        return []
    out = []
    for name, was in (prev.get("dirs") or {}).items():
        if name in _TRANSIENT_DIRS:
            continue
        now = (cur.get("dirs") or {}).get(name)
        if now is None:
            out.append(f"dir {name!r} GONE (had {was['n']} files)")
        elif now["n"] < was["n"]:
            out.append(f"dir {name!r} {was['n']} -> {now['n']} files")
    for name, was in (prev.get("files") or {}).items():
        now = (cur.get("files") or {}).get(name)
        if now is None:
            out.append(f"file {name!r} GONE ({was['bytes']} bytes)")
        elif now["bytes"] < was["bytes"]:
            out.append(f"file {name!r} shrank {was['bytes']} -> {now['bytes']} bytes")
    return out


def last_line(log: Path) -> dict:
    """The most recent census, or {} -- read from the tail, not the whole file."""
    try:
        with log.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for raw in reversed(lines):
            try:
                return json.loads(raw)
            except ValueError:
                continue
    except OSError:
        pass
    return {}


def append(log: Path, snap: dict) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap, ensure_ascii=False) + "\n")


def report(log: Path) -> int:
    try:
        lines = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
    except (OSError, ValueError):
        print(f"no census log at {log}")
        return 0
    print(f"{len(lines)} census line(s) in {log}")
    if not lines:
        return 0
    print(f"  first: {lines[0]['ts']}")
    print(f"  last:  {lines[-1]['ts']}")
    drops = [ln for ln in lines if ln.get("drop")]
    unreadable = [ln for ln in lines if not ln.get("ok")]
    if unreadable:
        print(f"  {len(unreadable)} census(es) found the directory MISSING, "
              f"latest {unreadable[-1]['ts']}")
    print(f"\n{len(drops)} census(es) recorded a DROP:")
    for ln in drops:
        print(f"  {ln['ts']}")
        for d in ln["drop"]:
            print(f"      {d}")
    if not drops:
        print("  (none — nothing has vanished since watching began)")
    cur = lines[-1]
    if cur.get("ok"):
        print("\nlatest census:")
        for name, d in sorted(cur["dirs"].items()):
            print(f"  {name + '/':<14} {d['n']:>5} files  {d['bytes']:>10} bytes")
        for name, f in sorted(cur["files"].items()):
            print(f"  {name:<14} {'':>5}         {f['bytes']:>10} bytes")
    return 0


def selftest() -> int:
    """Both directions (§4.5): a real loss is reported AND growth is not."""
    failures = []

    def check(label, got, want):
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}: {got!r}")
        if not ok:
            failures.append(label)

    tmp = Path(tempfile.mkdtemp())
    root = tmp / "Torrents"
    (root / "queued").mkdir(parents=True)
    (root / "finished").mkdir()
    (root / "queued" / "a.torrent").write_bytes(b"aaaa")
    (root / "queued" / "b.torrent").write_bytes(b"bbbb")
    (root / "new.txt").write_text("One Piece\n")

    first = census(root)
    check("counts the subdirectory", first["dirs"]["queued"]["n"], 2)
    check("counts the top-level file", "new.txt" in first["files"], True)
    check("a first census has no previous, so no drop", diff({}, first), [])

    # grow: must NOT be reported
    (root / "queued" / "c.torrent").write_bytes(b"cccc")
    (root / "find.txt").write_text("x\n")
    grown = census(root)
    check("growth is not a drop", diff(first, grown), [])

    # lose a file from a subdir, and a whole top-level file
    (root / "queued" / "a.torrent").unlink()
    (root / "new.txt").unlink()
    shrunk = census(root)
    d = diff(grown, shrunk)
    check("a lost queued file is reported", any("queued" in x and "3 -> 2" in x for x in d), True)
    check("a lost new.txt is reported", any("new.txt" in x and "GONE" in x for x in d), True)

    # the bridge's drop folder is drained on purpose: still censused, never a "drop"
    (root / "DirectIngest").mkdir()
    (root / "DirectIngest" / "movie.mkv").write_bytes(b"vvvv")
    with_bridge = census(root)
    check("the bridge drop folder is censused",
          with_bridge["dirs"]["DirectIngest"]["n"], 1)
    (root / "DirectIngest" / "movie.mkv").unlink()
    drained = census(root)
    check("its drained count is recorded", drained["dirs"]["DirectIngest"]["n"], 0)
    check("a drained bridge drop is NOT reported as a loss", diff(with_bridge, drained), [])

    # lose the whole directory
    for p in sorted(root.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    root.rmdir()
    gone = census(root)
    check("a missing root is recorded, not crashed on", gone["ok"], False)
    check("and is not mistaken for a clean census", bool(gone["dirs"]), False)

    # a drop against a missing root must not be spammed as a thousand fake losses
    check("no phantom drops when the root cannot be read", diff(shrunk, gone), [])

    for p in sorted(tmp.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    tmp.rmdir()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="summarise the log")
    ap.add_argument("--selftest", action="store_true", help="fixture check, both ways")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    ap.add_argument("--log", default=str(LOG), help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    log = Path(args.log)
    if args.report:
        return report(log)

    prev = last_line(log)
    snap = census(Path(args.root))
    lost = diff(prev, snap)
    if lost:
        snap["drop"] = lost
    append(log, snap)
    if not snap["ok"]:
        print(f"iCloud census: {args.root} IS NOT READABLE", file=sys.stderr)
        return 1
    if lost:
        print(f"iCloud census: {len(lost)} LOSS(ES) since {prev.get('ts')}", file=sys.stderr)
        for x in lost:
            print(f"    {x}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
