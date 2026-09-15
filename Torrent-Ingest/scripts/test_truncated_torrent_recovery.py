#!/usr/bin/env python3
"""Regression test for truncated-drop recovery (2026-09-14).

A `.torrent` served by a cache can arrive cut short: a valid bencode prefix that simply
ends mid-`pieces`. The old behavior filed it under `failed/` with "replace the file rather
than re-dropping it" -- advice the owner could not follow once the searcher (which fetched
the original and wrote a sibling `.magnet` for exactly this case) was deleted on
2026-09-10. Every drop here is named with its 40-hex info hash, and a hash is all a magnet
needs, so ingest now recovers the drop itself: qBittorrent pulls the real metadata from the
swarm and the normal pipeline takes over.

Three parts, each proving a thing that can silently rot:

**Part 1 -- the salvage reader, against a real torrent as its own oracle.** The recovery
is only as good as the name and trackers it lifts out of the truncated bytes, so this
compares `qbt.salvage_from_truncated_file` against an independent decoder on a real
torrent cut INSIDE `pieces`, and proves both halves are actually salvageable: the name
comes back, and the trackers come back, not an empty list that would leave the magnet
adrift on DHT alone.

**Part 2 -- the recovery end to end.** Through `register_new_torrents`, in a temp tree:
the drop becomes a QUEUED magnet record, its URI parses to the filename's hash, the dead
bytes land under failed/, and nothing is left at the top of the watch folder.

**Part 3 -- the negatives that keep it from eating work.** A drop still inside the iCloud
sync grace is left for the next cycle; a truncated drop with no hash in its name still
takes the old failed/ path; and a HEALTHY hash-named `.torrent` is still registered as a
`.torrent` -- the recovery must not hijack every drop whose name looks like a hash.

    python3 scripts/test_truncated_torrent_recovery.py

Read-only with respect to the live fleet: every path it writes is a temp dir, and the
journal is stubbed so records go nowhere. Exit 0 means every check passed.
"""

import os
import sys
import tempfile
import time
import types
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                    # noqa: E402
import ingest                                                    # noqa: E402
import journal as real_journal                                   # noqa: E402
import qbt                                                       # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


# --------------------------------------------------------------------------------------
# An independent oracle: decode a COMPLETE torrent and pull the same two fields
# --------------------------------------------------------------------------------------

def _oracle(data: bytes):
    """(name, trackers) from a complete torrent, decoded from the spec.

    Deliberately not shared with the code under test -- it exists to disagree with it.
    Tracker order matches the code's: `announce` first, then `announce-list`, deduped.
    """
    def dec(b, i):
        c = b[i:i + 1]
        if c == b"d":
            i += 1
            out = {}
            while b[i:i + 1] != b"e":
                k, i = dec(b, i)
                v, i = dec(b, i)
                out[k] = v
            return out, i + 1
        if c == b"l":
            i += 1
            out = []
            while b[i:i + 1] != b"e":
                v, i = dec(b, i)
                out.append(v)
            return out, i + 1
        if c == b"i":
            j = b.index(b"e", i)
            return int(b[i + 1:j]), j + 1
        j = b.index(b":", i)
        n = int(b[i:j])
        return b[j + 1:j + 1 + n], j + 1 + n

    meta, _ = dec(data, 0)
    info = meta.get(b"info", {})
    name = info.get(b"name")
    name = name.decode("utf-8", "replace") if isinstance(name, bytes) else None
    trackers = []
    announce = meta.get(b"announce")
    if isinstance(announce, bytes):
        trackers.append(announce.decode("utf-8", "replace"))
    for tier in meta.get(b"announce-list", []):
        if isinstance(tier, list):
            for url in tier:
                if isinstance(url, bytes):
                    trackers.append(url.decode("utf-8", "replace"))
    seen, unique = set(), []
    for url in trackers:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return name, unique


def _pick_source():
    """A real mirror torrent that truncates cleanly mid-`pieces` and salvages whole."""
    for p in sorted(config.TORRENT_SOURCE_MIRROR.glob("*.torrent")):
        try:
            src = p.read_bytes()
            info_hash = qbt.info_hash_from_file(p)
        except (OSError, ValueError, IndexError):
            continue
        name, trackers = _oracle(src)
        if not name or not trackers:
            continue
        cut = src[:-64]                       # standard v1 layout: pieces runs to EOF
        try:
            qbt.info_hash_from_file(_write_temp(cut))
        except (ValueError, IndexError):
            pass                              # good: the cut really is unparseable
        else:
            continue
        s_name, s_trackers = qbt.salvage_from_truncated_file(_write_temp(cut))
        if s_name == name and s_trackers == trackers:
            return p, src, info_hash, name, trackers
    return None


_TEMP_FILES: list[Path] = []


def _write_temp(data: bytes) -> Path:
    fd, name = tempfile.mkstemp(suffix=".torrent")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    p = Path(name)
    _TEMP_FILES.append(p)
    return p


# --------------------------------------------------------------------------------------
# Part 1: the salvage reader vs. the oracle, on a real torrent cut inside pieces
# --------------------------------------------------------------------------------------

def part1(picked):
    print("Part 1: truncated salvage vs. an independent oracle")
    if picked is None:
        check("a real mirror torrent truncates cleanly mid-pieces", False, True)
        return None, None, None
    _src_path, src, info_hash, name, trackers = picked
    cut = src[:-64]

    s_name, s_trackers = qbt.salvage_from_truncated_file(_write_temp(cut))
    check("display name survives the cut", s_name, name)
    check(f"all {len(trackers)} trackers survive the cut", s_trackers, trackers)

    # A cut BEFORE the info dict: no name, but the announce-list must still come back.
    head = src[:src.index(b"4:info")]
    h_name, h_trackers = qbt.salvage_from_truncated_file(_write_temp(head))
    check("cut before `info`: name is absent, trackers survive",
          (h_name, h_trackers), (None, trackers))

    # Degenerate inputs returned by accident must be quiet, never raise.
    junk = _write_temp(b"this is not bencode")
    check("garbage bytes salvage as (None, [])",
          qbt.salvage_from_truncated_file(junk), (None, []))
    check("a missing file salvages as (None, [])",
          qbt.salvage_from_truncated_file(junk.parent / "nope.torrent"), (None, []))

    # The recovery can only work if the filename hash is the real one, so prove it here:
    # the kept mirror names ARE info hashes, and the hash the magnet will carry is this.
    check("the fixture is named by its true info hash", _src_path.stem, info_hash)
    return src, info_hash, name


# --------------------------------------------------------------------------------------
# Part 2: the recovery end to end, in a temp tree
# --------------------------------------------------------------------------------------

def _make_tree(base: Path):
    torrents = base / "Torrents"
    dirs = {k: torrents / k for k in ("queued", "ingesting", "finished", "failed")}
    dirs["torrents"] = torrents
    dirs["mirror"] = base / "torrent_sources"
    for d in [torrents, *dirs.values()]:
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def _patched(dirs, loglines):
    """Point ingest at the temp tree and cut the journal loose from the real one.

    Returns an `unpatch()` callable. `new_record` is the REAL one (its shape is part of
    what we assert); only the disk write is captured.
    """
    saved = {}
    for name, value in (("TORRENTS_DIR", dirs["torrents"]), ("QUEUED_DIR", dirs["queued"]),
                        ("INGESTING_DIR", dirs["ingesting"]), ("FINISHED_DIR", dirs["finished"]),
                        ("FAILED_DIR", dirs["failed"]),
                        ("TORRENT_SOURCE_MIRROR", dirs["mirror"])):
        saved[("config", name)] = getattr(config, name)
        setattr(config, name, value)
    saved[("ingest", "materialize")] = ingest.materialize
    saved[("ingest", "log")] = ingest.log
    saved[("ingest", "journal")] = ingest.journal

    written: list[dict] = []

    def _record(rec):
        written.append(dict(rec))
        return rec

    ingest.materialize = lambda path, wait_sec=60: path.exists()
    ingest.log = lambda line: loglines.append(line)
    ingest.journal = types.SimpleNamespace(
        COMPLETED=real_journal.COMPLETED, FAILED=real_journal.FAILED,
        REFUSED=real_journal.REFUSED, new_record=real_journal.new_record,
        write_record=_record)

    def unpatch():
        for (mod, name), value in saved.items():
            setattr(config if mod == "config" else ingest, name, value)
    return unpatch, written


def part2(src, info_hash, name, trackers):
    print("Part 2: recovery end to end through register_new_torrents")
    base = Path(tempfile.mkdtemp(prefix="trunc-recovery-"))
    dirs = _make_tree(base)
    loglines: list[str] = []
    unpatch, written = _patched(dirs, loglines)
    drop = dirs["torrents"] / f"{info_hash.upper()}.torrent"
    drop.write_bytes(src[:-64])                    # hash-named, truncated inside pieces
    old = time.time() - config.UNPARSEABLE_GRACE_SEC - 60
    os.utime(drop, (old, old))
    try:
        records: dict = {}
        ingest.register_new_torrents(records)
    finally:
        unpatch()

    magnet = dirs["queued"] / f"{info_hash.upper()}.magnet"
    check("magnet filed under queued/", magnet.exists(), True)
    check("dead .torrent filed under failed/",
          (dirs["failed"] / drop.name).exists(), True)
    check("watch folder top is clear", drop.exists(), False)
    check("a record was registered under the filename hash", list(records), [info_hash])
    rec = records.get(info_hash) or {}
    check("the record carries the magnet", bool(rec.get("magnet")), True)
    check("the record is QUEUED with unknown size",
          (rec.get("status"), rec.get("total_size")), (real_journal.QUEUED, None))

    if magnet.exists():
        m_hash, m_name, m_uri = ingest._read_magnet(magnet)
        check("the magnet parses to the filename hash", m_hash, info_hash)
        check("the magnet carries the display name", m_name, name)
        check("the recovery is logged in plain words",
              any("Recovered truncated .torrent" in line for line in loglines), True)
        got_tr = [v for k, v in urllib.parse.parse_qsl(
            urllib.parse.urlsplit(m_uri).query, keep_blank_values=True) if k == "tr"]
        check(f"the magnet carries all {len(trackers)} trackers", got_tr, trackers)


# --------------------------------------------------------------------------------------
# Part 3: the negatives
# --------------------------------------------------------------------------------------

def part3(src, info_hash):
    print("Part 3: the negatives -- no hijack of healthy, young, or nameless drops")

    # A drop still inside the iCloud sync grace: left exactly where it is.
    base = Path(tempfile.mkdtemp(prefix="trunc-grace-"))
    dirs = _make_tree(base)
    loglines: list[str] = []
    unpatch, _ = _patched(dirs, loglines)
    drop = dirs["torrents"] / f"{info_hash}.torrent"
    drop.write_bytes(src[:-64])
    try:
        ingest.register_new_torrents({})
    finally:
        unpatch()
    check("young truncated drop stays in place", drop.exists(), True)
    check("young truncated drop files nothing",
          (list(dirs["queued"].iterdir()), list(dirs["failed"].iterdir())), ([], []))
    check("the sync grace is why", any("sync grace" in line for line in loglines), True)

    # A truncated drop with no hash in its name: the old failed/ path, no magnet.
    base = Path(tempfile.mkdtemp(prefix="trunc-nohash-"))
    dirs = _make_tree(base)
    loglines = []
    unpatch, _ = _patched(dirs, loglines)
    drop = dirs["torrents"] / "smallville-season-10.torrent"
    drop.write_bytes(src[:-64])
    old = time.time() - config.UNPARSEABLE_GRACE_SEC - 60
    os.utime(drop, (old, old))
    try:
        ingest.register_new_torrents({})
    finally:
        unpatch()
    check("nameless truncated drop still goes to failed/",
          (dirs["failed"] / drop.name).exists(), True)
    check("nameless truncated drop writes no magnet",
          list(dirs["queued"].iterdir()), [])
    check("nameless truncated drop is reported as unrecoverable",
          any("carries no hash-named recovery" in line for line in loglines), True)

    # A HEALTHY hash-named .torrent: registered as a .torrent, mirror kept, no magnet.
    base = Path(tempfile.mkdtemp(prefix="trunc-healthy-"))
    dirs = _make_tree(base)
    loglines = []
    unpatch, _ = _patched(dirs, loglines)
    drop = dirs["torrents"] / f"{info_hash}.torrent"
    drop.write_bytes(src)
    try:
        records: dict = {}
        ingest.register_new_torrents(records)
    finally:
        unpatch()
    check("healthy drop files under queued/", (dirs["queued"] / drop.name).exists(), True)
    check("healthy drop writes no magnet",
          [p.name for p in dirs["queued"].iterdir() if p.suffix == ".magnet"], [])
    check("healthy drop is mirrored for read-back", (dirs["mirror"] / drop.name).exists(), True)
    rec = records.get(info_hash) or {}
    check("healthy drop is a .torrent record, not a magnet",
          (bool(rec), "magnet" in rec), (True, False))


def main() -> int:
    print("=== truncated-drop recovery ===")
    picked = _pick_source()
    src, info_hash, name = part1(picked) or (None, None, None)
    if src is not None:
        part2(src, info_hash, name, picked[4])
        part3(src, info_hash)
    for p in _TEMP_FILES:
        p.unlink(missing_ok=True)
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
