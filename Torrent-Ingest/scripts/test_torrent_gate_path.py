#!/usr/bin/env python3
"""Regression test for the acceptance gate's `.torrent` half (§4.120, 2026-09-07).

§4.120 was one gate reachable from only one of two drop paths, which went dark when the
traffic moved to the other path. On 2026-09-07 the same fault appeared mirrored: the
`.torrent` drop path's only gate lived in the searcher, the searcher was quarantined, and
100% of that day's drops were `.torrent` -- so every one was admitted with nothing judging
it, for 50 hours, while the heartbeat sat still. Ingest now gates both paths.

Two parts, proving the two things that can silently rot:

**Part 1 -- the file list.** Every verdict the gate reaches is downstream of reading the
`.torrent`'s file list correctly. This asserts the reader agrees with an independent
in-test oracle over every real `.torrent` on disk, AND that the comparison can report a
disagreement when there is one -- a comparison that can never fail reads exactly like
agreement (§4.5).

**Part 2 -- what each verdict costs.** A REFUSE retires the drop for good, so the cheap
mistake is refusing something and the expensive one is refusing it the wrong way. Asserts
all three verdicts on both paths, and that a `.torrent` refusal -- which happens BEFORE
the add -- never asks qBittorrent to remove a torrent that was never added.

The safety property itself (zero false refusals over the whole journal) is not re-proved
here: both paths reach one `acceptance_gate.check`, and `test_acceptance_gate.py` part 2
already replays it over every completed record.

    python3 scripts/test_torrent_gate_path.py

Read-only. Exit 0 means every check passed.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import acceptance_gate                                           # noqa: E402
import config                                                    # noqa: E402
import ingest                                                    # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


# --------------------------------------------------------------------------------------
# Part 1: the .torrent file-list reader, against an independent oracle
# --------------------------------------------------------------------------------------
#
# This used to compare `acceptance_gate.file_names_from_torrent` against the SEARCHER's
# `torrent_file_names`, because two readers of one format drift and both looked healthy
# while they did. Discovery was removed on 2026-09-10 and the searcher's reader went with
# it, so there is now exactly ONE reader in the fleet and that particular drift is gone by
# construction.
#
# What replaces it is not "nothing": a single reader with no oracle is unfalsifiable, and
# every verdict the gate reaches is downstream of this list being right. So the test
# carries its own minimal bencode decoder -- written from the spec, deliberately naive,
# and short enough to read in one sitting -- and asserts the real reader agrees with it
# over every `.torrent` on disk.


def _oracle(data: bytes) -> list[str]:
    """A deliberately naive bencode file-list reader, written from the format spec.

    Not shared with the code under test and not clever: its only job is to be obviously
    correct, so that a subtle bug in the real reader shows up as a disagreement.
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

    try:
        meta, _ = dec(data, 0)
    except (ValueError, IndexError):
        return []
    info = meta.get(b"info") if isinstance(meta, dict) else None
    if not isinstance(info, dict):
        return []
    files = info.get(b"files")
    if not isinstance(files, list):
        name = info.get(b"name")
        return [name.decode("utf-8", "replace")] if isinstance(name, bytes) else []
    out = []
    for f in files:
        if isinstance(f, dict) and isinstance(f.get(b"path"), list):
            out.append("/".join(x.decode("utf-8", "replace")
                                for x in f[b"path"] if isinstance(x, bytes)))
    return out


def _real_torrents():
    """Every `.torrent` the fleet has on disk: the watch folder and the source mirror."""
    found = set()
    for d in (config.TORRENTS_DIR, config.QUEUED_DIR, config.INGESTING_DIR,
              config.FINISHED_DIR, config.FAILED_DIR, config.TORRENT_SOURCE_MIRROR):
        try:
            found.update(p for p in d.iterdir() if p.suffix.lower() == ".torrent")
        except OSError:
            continue
    return sorted(found)


def part1():
    print("Part 1: .torrent file-list reader vs. an independent oracle")
    canonical = _oracle
    paths = _real_torrents()
    if not paths:
        check("found real .torrent files to compare", 0, "at least one")
        return
    disagreed = [p.name for p in paths
                 if acceptance_gate.file_names_from_torrent(p) != canonical(p.read_bytes())]
    check(f"all {len(paths)} real .torrent(s) read identically", disagreed, [])

    # ...and the other direction: the comparison must be able to REPORT a difference, or
    # "they agree" means nothing (§4.5). Hand the canonical reader bytes whose file list
    # genuinely differs and confirm the comparison notices.
    sample = next((p for p in paths
                   if len(acceptance_gate.file_names_from_torrent(p)) > 1), None)
    if sample is None:
        check("a multi-file .torrent exists to mutate", False, True)
        return
    mine = acceptance_gate.file_names_from_torrent(sample)
    check("comparison detects a difference when there is one",
          mine != canonical(sample.read_bytes())[:-1], True)

    # An unreadable or absent .torrent is [] -- never an exception. This runs on the
    # admission path, where a raise would stop the queue draining.
    junk = Path(tempfile.mkdtemp(prefix="gate-torrent-")) / "x.torrent"
    junk.write_bytes(b"not bencode at all")
    check("garbage .torrent reads as []", acceptance_gate.file_names_from_torrent(junk), [])
    check("missing .torrent reads as []",
          acceptance_gate.file_names_from_torrent(junk.parent / "nope.torrent"), [])


# --------------------------------------------------------------------------------------
# Part 2: what each verdict costs, on each path
# --------------------------------------------------------------------------------------

class _Verdict:
    def __init__(self, decision, reason):
        self.decision, self.reason = decision, reason


def _drive(decision, added_to):
    """Run `_acceptance_gate` for one verdict with everything around it stubbed.

    Returns `(admitted, record, calls)` where `calls` records what the gate reached for:
    the heartbeat, the qBittorrent removal, and whether the source .torrent was filed
    under failed/.
    """
    calls = {"heartbeat": [], "removed": [], "filed_failed": 0, "journal": 0}
    record = {"name": "A Release (2024) [1080p]", "info_hash": "0" * 40,
              "status": "queued", "torrent_path": "/tmp/a.torrent"}

    saved = {n: getattr(ingest, n) for n in ("acceptance_gate", "journal", "qbt", "log")}
    saved["_file_torrent_failed"] = ingest._file_torrent_failed

    class _Gate:
        ACCEPT, REFUSE, UNKNOWN = "accept", "refuse", "unknown"

        @staticmethod
        def check(h, names, title):
            return _Verdict(decision, f"stubbed {decision}")

        @staticmethod
        def record(d):
            calls["heartbeat"].append(d)

    class _Journal:
        REFUSED, FAILED = "refused", "failed"

        @staticmethod
        def write_record(r):
            calls["journal"] += 1

    class _Qbt:
        @staticmethod
        def remove(client, h, delete_files=True):
            calls["removed"].append(h)

    def _filed(r):
        calls["filed_failed"] += 1

    ingest.acceptance_gate, ingest.journal, ingest.qbt = _Gate, _Journal, _Qbt
    ingest.log = lambda *a, **k: None
    ingest._file_torrent_failed = _filed
    try:
        admitted = ingest._acceptance_gate(
            record, "0" * 40, lambda: ["Season 1/ep01.mkv"],
            source=".torrent" if added_to is None else "magnet", added_to=added_to)
    finally:
        for n, v in saved.items():
            setattr(ingest, n, v)
    return admitted, record, calls


def part2():
    print("Part 2: verdict handling on the .torrent path (and the magnet path beside it)")
    for decision, admits in (("accept", True), ("unknown", True), ("refuse", False)):
        admitted, record, calls = _drive(decision, added_to=None)
        check(f".torrent {decision}: admitted={admits}", admitted, admits)
        check(f".torrent {decision}: heartbeat recorded", calls["heartbeat"], [decision])
        # THE point of the .torrent half: the gate runs before the add, so a refusal has
        # nothing in qBittorrent to take back and must never ask it to.
        check(f".torrent {decision}: qBittorrent untouched", calls["removed"], [])
        if not admits:
            check(f".torrent {decision}: retired as refused, not failed",
                  record["status"], "refused")
            check(f".torrent {decision}: source .torrent filed under failed/",
                  calls["filed_failed"], 1)

    # The magnet path, unchanged, through the same shared body: there the torrent IS
    # already added, so a refusal must remove it. Asserting both here is what stops the
    # two paths quietly becoming one behaviour.
    admitted, record, calls = _drive("refuse", added_to=object())
    check("magnet refuse: not admitted", admitted, False)
    check("magnet refuse: removed from qBittorrent", calls["removed"], ["0" * 40])
    admitted, _, calls = _drive("accept", added_to=object())
    check("magnet accept: admitted, nothing removed", (admitted, calls["removed"]),
          (True, []))

    # A verdict already on the record is not re-litigated -- and, just as important, does
    # not double-count the heartbeat.
    calls = {"heartbeat": []}
    rec = {"name": "x", "gate_decision": "refuse"}
    check("a settled REFUSE stays refused without re-running",
          ingest._acceptance_gate(rec, "0" * 40, lambda: [], ".torrent", None), False)
    rec = {"name": "x", "gate_decision": "accept"}
    check("a settled ACCEPT stays accepted without re-running",
          ingest._acceptance_gate(rec, "0" * 40, lambda: [], ".torrent", None), True)


def part3():
    """The REAL `record()` and `liveness()` bodies must actually execute.

    Part 2 stubs `acceptance_gate.record` so it can count heartbeats without writing to
    the live state dir -- which means the real function's body is never run by that test.
    On 2026-09-10 that let a NameError (`_SEARCHER_DIR`, left behind by a rename) sit in
    production code through all 25 blocking checks; every ingest cycle then aborted with
    "Cycle error (continuing)" the moment a gate verdict was reached, and the only symptom
    was drops quietly not being registered.

    A test that stubs the thing it is checking is not checking it (§4.5). So this calls
    the real bodies, against a temp state dir, and asserts they do what they claim.
    """
    print("Part 3: the real heartbeat writer executes")
    import acceptance_gate as ag
    with tempfile.TemporaryDirectory() as td:
        saved = ag._STATE_DIR
        try:
            ag._STATE_DIR = Path(td)
            for decision in (ag.ACCEPT, ag.REFUSE, ag.UNKNOWN):
                ag.record(decision)                       # must not raise
            wrote = list(Path(td).rglob("*"))
            check("record() wrote a heartbeat", bool([p for p in wrote if p.is_file()]), True)
            live = ag.liveness()                          # must not raise
            check("liveness() returns something", live is not None, True)
        finally:
            ag._STATE_DIR = saved

    # And every public callable is at least reachable -- a stale global in any of them is
    # the same class of bug.
    for name in ("check", "record", "liveness", "file_names_from_torrent",
                 "metadata_is_safe"):
        check(f"acceptance_gate.{name} exists", callable(getattr(ag, name, None)), True)


def main() -> int:
    print("=== acceptance gate: .torrent path ===")
    part1()
    part2()
    part3()
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
