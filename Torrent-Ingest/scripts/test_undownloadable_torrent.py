#!/usr/bin/env python3
"""A private trackerless `.torrent` can never find a peer; fail it at registration.

THE CASE (2026-09-24). A SpongeBob S16 pack was hand-dropped as a `.torrent` whose info
dict says `private: 1` and whose top level carries no `announce`, no `announce-list` and
no `url-list`. A private torrent may not use DHT, PeX or LSD -- the spec forbids it, and a
live probe of this qBittorrent reports exactly that: all three rows read "This torrent is
private". With no tracker and no web seed, that drop has ZERO ways to reach a peer. It sat
in qBittorrent until the 24h stall clock abandoned it with "stalled 24h with no progress
(no peer activity)", and a re-drop repeats the whole day. Its iCloud duplicate was NOT why
it failed: the fleet filed the clean copy under `finished/` and tracked the " 2" copy; the
duplicate machinery worked. The drop itself is undownloadable.

**The guard is computed from the file, before qBittorrent is asked to add it.**
`qbt.undownloadable_reason` reads `info.private` and the top-level tracker/web-seed keys
(never the network), and `ingest._refuse_undownloadable` fails the drop through the normal
`_fail` path: the reason goes in the record, the source is filed under `failed/`, and the
watch folder's top level is left clean.

Three parts, each proving a thing that can silently rot:

**Part 1 -- the reader, both ways.** Every private trackerless shape is refused (including
a private torrent whose only URL keys are empty, and a `private` value that arrives as the
string "1"); every private torrent WITH a tracker, every private torrent with a web seed,
and -- load-bearing -- every PUBLIC trackerless drop is accepted. The public trackerless
drop is the fleet's dominant real shape (HANDOFF: DHT-only drops with `announce` absent),
so a guard that refused it would stop the fleet dead while reading "safe".

**Part 2 -- registration end to end, in a temp tree.** The private trackerless drop fails
at registration, its source is filed under `failed/`, nothing lands in `queued/`, and the
journal record carries the reason. Controls: a public trackerless drop and a private drop
WITH a tracker still register as QUEUED. And a terminal re-drop of the same broken file
fails fast again instead of being re-queued for another 24h stall.

**Part 3 -- the real corpus (both ways).** Every `.torrent` on an admission path is judged:
every refusal must be private+trackerless. This is the §4.114 half that proves the guard
cannot eat real work, without asserting that a genuinely broken drop must stay admissible.

    python3 scripts/test_undownloadable_torrent.py

Read-only with respect to the live fleet: every path it writes is a temp dir, and the
journal write is stubbed. Exit 0 means every check passed.
"""

import shutil
import sys
import tempfile
import types
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


def _benc(o):
    """Minimal bencoder -- fixtures only, so the test builds the bytes it judges."""
    if isinstance(o, bool):
        raise TypeError(o)
    if isinstance(o, int):
        return b"i" + str(o).encode() + b"e"
    if isinstance(o, bytes):
        return str(len(o)).encode() + b":" + o
    if isinstance(o, list):
        return b"l" + b"".join(_benc(x) for x in o) + b"e"
    if isinstance(o, dict):
        return b"d" + b"".join(_benc(k) + _benc(v) for k, v in sorted(o.items())) + b"e"
    raise TypeError(type(o))


def _torrent_bytes(name=b"pack", private=None, announce=None, announce_list=None,
                   url_list=None):
    info = {b"name": name, b"piece length": 16384, b"pieces": b"\x00" * 20,
            b"length": 100}
    if private is not None:
        info[b"private"] = private
    meta = {b"info": info}
    if announce is not None:
        meta[b"announce"] = announce
    if announce_list is not None:
        meta[b"announce-list"] = announce_list
    if url_list is not None:
        meta[b"url-list"] = url_list
    return _benc(meta)


_TMP: list[Path] = []


def _write_temp(data: bytes, name="fixture.torrent") -> Path:
    d = Path(tempfile.mkdtemp(prefix="undownloadable-"))
    p = d / name
    p.write_bytes(data)
    _TMP.append(d)
    return p


# --------------------------------------------------------------------------------------
# An independent oracle: what a torrent's bytes SAY, decoded from the spec
# --------------------------------------------------------------------------------------

def _oracle(data: bytes):
    """(private, has_urls) straight off the bytes. Deliberately not shared with the code
    under test -- it exists to disagree with it."""

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
    try:
        private = int(info.get(b"private") or 0)
    except (TypeError, ValueError):
        private = 0
    urls = [meta.get(b"announce"), meta.get(b"announce-list"), meta.get(b"url-list")]

    def has_urls(v):
        if isinstance(v, bytes):
            return bool(v)
        if isinstance(v, list):
            return any(has_urls(x) for x in v)
        return False

    return private, any(has_urls(u) for u in urls)


# --------------------------------------------------------------------------------------
# Part 1: the reader, both ways
# --------------------------------------------------------------------------------------

def part1():
    print("Part 1: undownloadable_reason -- every private trackerless shape refused, "
          "nothing else is")
    refused_torrents = {
        "private, no tracker": _write_temp(_torrent_bytes(private=1)),
        "private, empty announce": _write_temp(_torrent_bytes(private=1, announce=b"")),
        "private, empty announce-list tiers":
            _write_temp(_torrent_bytes(private=1, announce_list=[[], []])),
        "private, empty url-list":
            _write_temp(_torrent_bytes(private=1, url_list=[])),
        "private as the string '1'":
            _write_temp(_torrent_bytes(private=b"1")),
    }
    for label, path in refused_torrents.items():
        reason = qbt.undownloadable_reason(path)
        check(f"refused: {label}", bool(reason), True)
        if reason:
            check(f"  ...says 'private' and 'tracker'", ("private" in reason,
                                                         "tracker" in reason), (True, True))

    accepted_torrents = {
        # The fleet's DOMINANT shape: a public DHT-only drop. Refusing this stops the fleet.
        "public, no tracker": _write_temp(_torrent_bytes()),
        "public (private=0), no tracker": _write_temp(_torrent_bytes(private=0)),
        "private with announce":
            _write_temp(_torrent_bytes(private=1, announce=b"http://tracker/announce")),
        "private with announce-list":
            _write_temp(_torrent_bytes(private=1,
                                       announce_list=[[b"udp://tracker:1337/announce"]])),
        "private with a web seed":
            _write_temp(_torrent_bytes(private=1, url_list=[b"https://seed/file"])),
        "private with a plain url-list string":
            _write_temp(_torrent_bytes(private=1, url_list=b"https://seed/file")),
    }
    for label, path in accepted_torrents.items():
        check(f"accepted: {label}", qbt.undownloadable_reason(path), None)

    # Degenerate inputs must fail open, never raise: truncated or unreadable drops have
    # their own recovery paths and must not be condemned by this reader.
    check("garbage bytes fail open",
          qbt.undownloadable_reason(_write_temp(b"this is not bencode")), None)
    good = _torrent_bytes(private=1)
    check("truncated bytes fail open",
          qbt.undownloadable_reason(_write_temp(good[:len(good) // 2])), None)
    check("a missing file fails open",
          qbt.undownloadable_reason(_TMP[0] / "nope.torrent"), None)


# --------------------------------------------------------------------------------------
# Part 2: registration end to end, in a temp tree
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
    saved = {}
    for name, value in (("TORRENTS_DIR", dirs["torrents"]), ("QUEUED_DIR", dirs["queued"]),
                        ("INGESTING_DIR", dirs["ingesting"]),
                        ("FINISHED_DIR", dirs["finished"]),
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


def _drop(dirs, data: bytes, filename: str) -> Path:
    p = dirs["torrents"] / filename
    p.write_bytes(data)
    return p


def part2():
    print("\nPart 2: registration end to end")

    # --- the broken drop fails fast, visibly, and does not queue ----------------------
    base = Path(tempfile.mkdtemp(prefix="undownloadable-e2e-"))
    dirs = _make_tree(base)
    loglines: list[str] = []
    unpatch, written = _patched(dirs, loglines)
    broken = _torrent_bytes(name=b"broken pack", private=1)
    broken_hash = qbt.info_hash_from_file(_write_temp(broken))
    drop = _drop(dirs, broken, f"{broken_hash.upper()}.torrent")
    try:
        records: dict = {}
        ingest.register_new_torrents(records)
    finally:
        unpatch()

    rec = records.get(broken_hash) or {}
    check("the private trackerless drop is not queued in qBittorrent's lane",
          list(dirs["queued"].iterdir()), [])
    check("its source is filed under failed/ (the state the owner can see)",
          (dirs["failed"] / drop.name).exists(), True)
    check("the watch folder top is clear", drop.exists(), False)
    check("the journal record is FAILED, not queued",
          rec.get("status"), real_journal.FAILED)
    check("the record carries the reason",
          "private torrent" in (rec.get("error") or ""), True)
    check("the failure is logged in plain words",
          any("FAILED " in line and "private torrent" in line for line in loglines), True)
    check("nothing pretends a download started",
          (list(dirs["ingesting"].iterdir()), list(dirs["finished"].iterdir())), ([], []))

    # --- a terminal re-drop of the same broken bytes fails fast again -----------------
    base = Path(tempfile.mkdtemp(prefix="undownloadable-redrop-"))
    dirs = _make_tree(base)
    loglines = []
    unpatch, _ = _patched(dirs, loglines)
    drop = _drop(dirs, broken, f"{broken_hash.upper()}.torrent")
    prior = dict(real_journal.new_record(broken_hash, drop, "broken pack"))
    prior["status"] = real_journal.FAILED
    prior["error"] = "stalled 24h with no progress (no peer activity)"
    records = {broken_hash: prior}
    try:
        ingest.register_new_torrents(records)
    finally:
        unpatch()
    check("the re-drop is FAILED again, not re-queued",
          records[broken_hash].get("status"), real_journal.FAILED)
    check("the re-drop reason is the source's, not the old stall",
          "private torrent" in (records[broken_hash].get("error") or ""), True)
    check("the re-drop source is filed under failed/",
          (dirs["failed"] / drop.name).exists(), True)
    check("no re-queue line was logged",
          any("Re-queuing" in line for line in loglines), False)

    # --- controls: the two healthy shapes still register ------------------------------
    for label, data in (
            ("public trackerless (the normal DHT shape)", _torrent_bytes(name=b"public")),
            ("private WITH a tracker",
             _torrent_bytes(name=b"tracked", private=1,
                            announce=b"http://tracker/announce"))):
        base = Path(tempfile.mkdtemp(prefix="undownloadable-ok-"))
        dirs = _make_tree(base)
        unpatch, _ = _patched(dirs, [])
        h = qbt.info_hash_from_file(_write_temp(data))
        drop = _drop(dirs, data, f"{h.upper()}.torrent")
        try:
            records = {}
            ingest.register_new_torrents(records)
        finally:
            unpatch()
        check(f"registered: {label}",
              (records.get(h, {}).get("status"), (dirs["queued"] / drop.name).exists()),
              (real_journal.QUEUED, True))
        check(f"  ...and not filed under failed/: {label}",
              (dirs["failed"] / drop.name).exists(), False)


# --------------------------------------------------------------------------------------
# Part 3: the real corpus, both ways
# --------------------------------------------------------------------------------------

def part3():
    print("\nPart 3: the real corpus -- every refusal must be private+trackerless")
    real: list[Path] = []
    for d in (config.TORRENTS_DIR, config.QUEUED_DIR, config.INGESTING_DIR,
              config.FINISHED_DIR, config.TORRENT_SOURCE_MIRROR):
        if d.exists():
            real.extend(sorted(d.glob("*.torrent")))
    real = sorted(set(real))
    print(f"  {len(real)} real .torrent file(s) on the admission paths")
    refused, wrong = [], []
    for p in real:
        try:
            reason = qbt.undownloadable_reason(p)
            private, has_urls = _oracle(p.read_bytes())
        except (OSError, ValueError, IndexError):
            continue
        if not reason:
            continue
        refused.append(p)
        if not (private == 1 and not has_urls):
            wrong.append(p)
    for p in wrong:
        print(f"  FAIL refused a torrent the oracle does not call private+trackerless: {p}")
        failures.append(f"guard false-positive on {p.name}")
    if not wrong:
        print(f"  ok   {len(refused)} refused, all private+trackerless by an "
              f"independent decode (a public or tracked torrent is never refused)")


if __name__ == "__main__":
    try:
        part1()
        part2()
        part3()
    finally:
        for d in _TMP:
            shutil.rmtree(d, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("ALL CHECKS PASSED.")
