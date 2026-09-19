#!/usr/bin/env python3
"""Orphaned source sweep: a `.torrent` in queued//ingesting/ gets a way out (10.6).

THE CASE, verified 2026-09-19. `find_drop_files()` scans only the watch root's TOP
level, so a source filed into `queued/` is never looked at again by anything. Two
mechanisms leave one there with no record pointing at it:

  * an iCloud duplicate -- `X.torrent` and `X 2.torrent` for one drop. Registration
    filed the extra into `queued/` "so the top level stays clean", and then nothing
    ever moved it again. That is how two One Piece files (`c1177`, `c1178`) sat in
    `queued/` for a month;
  * a TERMINAL record whose source is still in a state folder -- a purge or a
    re-registration that left the `.torrent` behind.

`ingest.sweep_orphan_sources` walks both state folders and files each resolved hash
where it belongs: terminal -> finished/ (or failed/ for failed/refused), a live
record's duplicate -> finished/, a live record whose recorded copy is GONE -> adopt
this copy (it is the only survivor), and an unrecognized hash -> back to the watch
root for registration. It must never delete and never touch a source it cannot parse.

    python3 scripts/test_orphan_sources.py

Writes only inside a temp dir; the journal write is stubbed. Exit 0 = all passed.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                          # noqa: E402
import ingest                                                          # noqa: E402
import journal                                                         # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def _benc(obj):
    if isinstance(obj, bool):
        raise TypeError(obj)
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, str):
        obj = obj.encode()
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, list):
        return b"l" + b"".join(_benc(v) for v in obj) + b"e"
    if isinstance(obj, dict):
        return b"d" + b"".join(_benc(k) + _benc(v) for k, v in sorted(obj.items())) + b"e"
    raise TypeError(obj)


def make_torrent(path: Path, name=None):
    """A minimal valid single-file torrent. `name` defaults to the file's own stem, so
    distinct fixtures get distinct info hashes (the hash is over the info dict, and a
    shared name would silently make every fixture the same torrent)."""
    if name is None:
        name = path.stem.encode()
    elif isinstance(name, str):
        name = name.encode()
    info = {b"name": name, b"piece length": 16384, b"pieces": b"\x00" * 20,
            b"length": 100}
    path.write_bytes(_benc({b"info": info}))
    import qbt
    return qbt.info_hash_from_file(path)


tmp = Path(tempfile.mkdtemp(prefix="orphan-sources-test-")).resolve()
queued = tmp / "queued"
ingesting = tmp / "ingesting"
finished = tmp / "finished"
failed = tmp / "failed"
root = tmp / "root"
for d in (queued, ingesting, finished, failed, root):
    d.mkdir(parents=True)

saved = {name: getattr(config, name) for name in
         ("TORRENTS_DIR", "QUEUED_DIR", "INGESTING_DIR", "FINISHED_DIR", "FAILED_DIR")}
saved_write = journal.write_record
try:
    config.TORRENTS_DIR = root
    config.QUEUED_DIR = queued
    config.INGESTING_DIR = ingesting
    config.FINISHED_DIR = finished
    config.FAILED_DIR = failed
    journal.write_record = lambda rec: rec            # never touch the live journal

    # ---- a terminal record's source is filed away ---------------------------------
    h1_path = queued / "A.torrent"
    h1 = make_torrent(h1_path)
    records = {h1: {"info_hash": h1, "status": journal.COMPLETED,
                    "torrent_path": str(h1_path)}}
    moved = ingest.sweep_orphan_sources(records)
    check("a completed record's source leaves queued/", h1_path.exists(), False)
    check("...and lands in finished/", (finished / "A.torrent").exists(), True)
    check("the sweep reports one move", moved, 1)

    h2_path = ingesting / "B.torrent"
    h2 = make_torrent(h2_path)
    records = {h2: {"info_hash": h2, "status": journal.FAILED,
                    "torrent_path": str(h2_path)}}
    ingest.sweep_orphan_sources(records)
    check("a failed record's source lands in failed/", (failed / "B.torrent").exists(), True)

    # ---- a live record's duplicate goes to finished/, the original stays -------------
    tracked = queued / "C.torrent"
    h3 = make_torrent(tracked)
    dup = queued / "C 2.torrent"
    make_torrent(dup, name=tracked.stem)              # same info dict -> same hash
    records = {h3: {"info_hash": h3, "status": journal.DOWNLOADING,
                    "torrent_path": str(tracked)}}
    ingest.sweep_orphan_sources(records)
    check("the live tracked source stays put", tracked.exists(), True)
    check("its duplicate is filed under finished/", (finished / "C 2.torrent").exists(), True)

    # ---- a live record whose recorded copy is GONE adopts the queued one -------------
    h4_path = queued / "D.torrent"
    h4 = make_torrent(h4_path)
    rec4 = {"info_hash": h4, "status": journal.DOWNLOADING,
            "torrent_path": str(tmp / "gone" / "D.torrent")}
    records = {h4: rec4}
    ingest.sweep_orphan_sources(records)
    check("the survivor is adopted as the record's source",
          rec4["torrent_path"], str(h4_path))
    check("...and stays where it is", h4_path.exists(), True)

    # ---- an unknown hash goes back to the watch root ---------------------------------
    h5_path = queued / "E.torrent"
    make_torrent(h5_path)
    ingest.sweep_orphan_sources({})
    check("an untracked source returns to the watch root", (root / "E.torrent").exists(), True)

    # ---- control: a live record whose tracked source IS the queued file is untouched --
    h6_path = queued / "F.torrent"
    h6 = make_torrent(h6_path)
    records = {h6: {"info_hash": h6, "status": journal.QUEUED,
                    "torrent_path": str(h6_path)}}
    ingest.sweep_orphan_sources(records)
    check("a tracked live source is never moved", h6_path.exists(), True)

    # ---- control: an unparseable source is left alone --------------------------------
    bad = queued / "G.torrent"
    bad.write_bytes(b"this is not bencode")
    ingest.sweep_orphan_sources({})
    check("an unparseable source is left alone", bad.exists(), True)

    # ---- registration's duplicate helper routes by record status ---------------------
    h7_path = tmp / "H.torrent"
    h7 = make_torrent(h7_path)
    ingest._file_duplicate_source(h7_path, {h7: {"status": journal.REFUSED}}, h7)
    check("a refused record's duplicate goes to failed/", (failed / "H.torrent").exists(), True)
finally:
    journal.write_record = saved_write
    for name, value in saved.items():
        setattr(config, name, value)

print()
if failures:
    print(f"FAIL: {len(failures)} check(s) failed")
    raise SystemExit(1)
print("PASS")
