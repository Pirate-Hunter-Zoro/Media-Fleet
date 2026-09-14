#!/usr/bin/env python3
"""The fleet's own detector for "YacReader is not picking up the shelf".

On 2026-09-14 every ElfQuest file was filed, on the mount, in the pool -- and invisible
in the reader, because both auto-update flags read `false` and the supervisor that should
have enforced them did not yet exist. `check_yacreader` is the outside view that closes
that loop: crash rows, a damaged index, a windowless app, drifted flags, and shelf files
the index does not know about, each reported at the severity that matches who can fix it.

Everything is driven with fakes so every verdict can fire -- a detector that only ever
said ALL CLEAR would pass against the live machine on a good day and prove nothing.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_health as fh                                             # noqa: E402
import remedies                                                       # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


TMP = Path(tempfile.mkdtemp(prefix="yac-health-"))
DB = TMP / "library.ydb"
DB.write_bytes(b"x")

saved = {n: getattr(fh, n) for n in ("_yacreader_integrity",)}
saved_db = {n: getattr(fh.yacreader_db, n) for n in
            ("app_running", "update_in_progress", "scan_settings_ok",
             "index_open", "index_quiet_sec")}
saved_idx = {n: getattr(fh.yacreader_index, n) for n in
             ("load_order_faults", "unindexed_files")}
saved_path = fh.config.YACREADER_DB


class World:
    def __init__(self) -> None:
        self.faults: list[dict] = []
        self.integrity = "ok"
        self.running = True
        self.updating = False
        self.quiet = 7200.0            # seconds since the index last changed
        self.flags_ok = True
        self.index_open = True
        self.missing: list[str] = []


def wire(w: World) -> None:
    fh.config.YACREADER_DB = DB
    fh._yacreader_integrity = lambda: w.integrity
    fh.yacreader_db.app_running = lambda: w.running
    fh.yacreader_db.update_in_progress = lambda: w.updating
    fh.yacreader_db.index_quiet_sec = lambda: w.quiet
    fh.yacreader_db.scan_settings_ok = lambda: w.flags_ok
    fh.yacreader_db.index_open = lambda: w.index_open
    fh.yacreader_index.load_order_faults = lambda _db: list(w.faults)
    fh.yacreader_index.unindexed_files = lambda *_a, **_k: list(w.missing)


def sevs(findings):
    return [s for s, _m in findings]


def joined(findings):
    return " | ".join(m for _s, m in findings)


try:
    print("=== YacReader health check ===")

    w = World()
    wire(w)
    check("a current, idle, healthy reader reports nothing", fh.check_yacreader() == [])

    w = World()
    w.faults = [{"kind": "cycle", "detail": "row id=785 is in a parent cycle", "id": 785}]
    wire(w)
    f = fh.check_yacreader()
    check("a crash row is an ACTION", "ACTION" in sevs(f))
    check("...and names the repair tool", "yacreader_index_repair.py --apply" in joined(f))

    w = World()
    w.integrity = "*** in database main ***\nPage 4 is never used"
    wire(w)
    f = fh.check_yacreader()
    check("a damaged index is an ACTION", "ACTION" in sevs(f))
    check("...and says restore the newest CLEAN backup",
          "PASSES integrity_check" in joined(f))

    w = World()
    w.flags_ok = False
    wire(w)
    f = fh.check_yacreader()
    check("drifted auto-update flags are an ACTION", "ACTION" in sevs(f))
    check("...and name the rescan tool", "yacreader_rescan.py --apply" in joined(f))

    # Windowlessness alone is not a finding: YACReader opens its index per operation,
    # so a healthy idle app looks exactly like a windowless one through this filesystem.
    # The harm is the FILES it is not indexing, which the freshness cases below cover.
    w = World()
    w.index_open = False
    w.updating = False
    wire(w)
    check("a quiet reader with a current index reports nothing",
          fh.check_yacreader() == [])

    w = World()
    w.missing = ["ElfQuest/ElfQuest v01.cbr", "ElfQuest/ElfQuest v02.cbr"]
    w.updating = True
    wire(w)
    f = fh.check_yacreader()
    check("unindexed files during an update are a WARN, not an ACTION",
          "ACTION" not in sevs(f) and "WARN" in sevs(f))

    w = World()
    w.missing = ["ElfQuest/ElfQuest v01.cbr"]
    w.updating = False
    wire(w)
    f = fh.check_yacreader()
    check("unindexed files with a long-quiet IDLE reader are an ACTION", "ACTION" in sevs(f))

    w = World()
    w.missing = ["ElfQuest/ElfQuest v01.cbr"]
    w.updating = False
    w.quiet = 60.0                 # the index just changed: a scan is probably starting
    wire(w)
    f = fh.check_yacreader()
    check("unindexed files moments after an index change are only a WARN",
          "ACTION" not in sevs(f) and "WARN" in sevs(f))

    w = World()
    w.missing = ["ElfQuest/ElfQuest v01.cbr"]
    w.running = False
    wire(w)
    f = fh.check_yacreader()
    check("unindexed files with the reader down are a WARN (its next start scans)",
          "ACTION" not in sevs(f) and "WARN" in sevs(f))

    w = World()
    w.faults = [{"kind": "unreadable", "detail": "database disk image is malformed",
                 "id": None}]
    wire(w)
    f = fh.check_yacreader()
    check("an unreadable index is one ACTION, not a crash-row report",
          len(f) == 1 and f[0][0] == "ACTION" and "cannot be read" in f[0][1])

    print()
    print("=== the remedies answer the check ===")
    hits = remedies.for_check("check_yacreader")
    ids = [r.id for r in hits]
    check("the refresh remedy answers it", "refresh_yacreader" in ids)
    check("the crash-row repair answers it", "repair_yacreader_index" in ids)
    check("a damaged index has an owner remedy too", "yacreader_index_damaged" in ids)
    check("auto remedies sort before the owner one",
          [r.safety for r in hits] == sorted([r.safety for r in hits],
                                             key=lambda s: 0 if s == "auto" else 1))
    for rid in ("refresh_yacreader", "repair_yacreader_index"):
        r = remedies.by_id(rid)
        check(f"{rid} is auto and complete",
              r is not None and r.safety == "auto" and r.apply and r.verify)
    check("the damaged-index remedy is owner-only",
          remedies.by_id("yacreader_index_damaged").safety == "owner")
finally:
    for n, v in saved.items():
        setattr(fh, n, v)
    for n, v in saved_db.items():
        setattr(fh.yacreader_db, n, v)
    for n, v in saved_idx.items():
        setattr(fh.yacreader_index, n, v)
    fh.config.YACREADER_DB = saved_path

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("YacReader health check: all checks passed")
