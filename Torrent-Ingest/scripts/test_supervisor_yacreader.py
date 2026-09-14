#!/usr/bin/env python3
"""YacReader's freshness contract in the supervisor, both directions.

Three failure modes, all measured on 2026-09-14:

  * the app is down -> it must be started WITH the scan flags patched, and the
    refresh marker consumed (the startup update covers it);
  * the app is up but its flags drifted -> bounce it, or nothing ever scans again;
  * the app is up but has NO library open (a crash restore: no window means
    `LibrariesUpdateCoordinator::init()` never runs) -> activate it, because it looks
    healthy while scanning nothing. That is the state ElfQuest sat invisible in.

And the anti-thrash policy: a app that dies and comes straight back is crashing, so the
supervisor backs off and alerts instead of restarting forever.

Drives the real `_yacreader_tick` with fake app control and a fake clock.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import library_supervisor as ls                                      # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


class Fake:
    def __init__(self) -> None:
        self.running = False
        self.starts = 0
        self.stops = 0
        self.activations = 0
        self.index_open = False
        self.settings_ok = True
        self.alerts: list[str] = []


CLOCK = [1_000_000.0]

saved = {n: getattr(ls, n) for n in ("yacreader_running", "stop_yacreader", "start_yacreader",
                                     "log", "alert")}
saved_db = {n: getattr(ls.yacreader_db, n) for n in ("ensure_scan_settings",
                                                     "scan_settings_ok", "activate_app",
                                                     "index_open")}
saved_time = ls.time.time
saved_marker = ls.config.YACREADER_REFRESH_MARKER
TMP = Path(tempfile.mkdtemp(prefix="yac-supervisor-"))
ls.config.YACREADER_REFRESH_MARKER = TMP / "yacreader_refresh_request"


def wire(fake: Fake) -> None:
    ls.yacreader_running = lambda: fake.running
    ls.stop_yacreader = lambda: (setattr(fake, "stops", fake.stops + 1),
                                 setattr(fake, "running", False), True)[-1]
    ls.start_yacreader = lambda: (setattr(fake, "starts", fake.starts + 1),
                                  setattr(fake, "running", True))
    ls.log = lambda *a, **k: None
    ls.alert = lambda msg: fake.alerts.append(msg)
    ls.time.time = lambda: CLOCK[0]
    ls.yacreader_db.ensure_scan_settings = lambda: False
    ls.yacreader_db.scan_settings_ok = lambda: fake.settings_ok
    ls.yacreader_db.activate_app = lambda: (setattr(fake, "activations",
                                                    fake.activations + 1), True)[-1]
    ls.yacreader_db.index_open = lambda: fake.index_open


def state() -> dict:
    return {"yac_started_at": None, "yac_stopped_by_us": False, "yac_crashes": 0,
            "yac_backoff_until": None, "yac_backoff_alerted": False,
            "yac_last_refresh": 0.0, "yac_index_checked_at": 0,
            "yac_activate_attempts": 0, "yac_activate_alerted": False}


try:
    print("=== YacReader supervisor tick ===")

    # 1. Down -> start, and the marker is consumed because the startup scan covers it.
    fake = Fake()
    wire(fake)
    ls.config.YACREADER_REFRESH_MARKER.write_text("pending\n", encoding="utf-8")
    st = state()
    ls._yacreader_tick(st)
    check("a down app is started", fake.starts == 1 and fake.running)
    check("the refresh marker is consumed at start",
          not ls.config.YACREADER_REFRESH_MARKER.exists())

    # 2. Drifted flags while up -> bounce (stop + start) so the patch takes.
    fake = Fake()
    fake.running = True
    fake.settings_ok = False
    wire(fake)
    st = state()
    ls._yacreader_tick(st)
    check("drifted flags bounce the app", fake.stops == 1 and fake.starts == 1)
    check("...and it is running again", fake.running)

    # 3. Up, flags fine, no library open -> activate (throttled), never restart.
    fake = Fake()
    fake.running = True
    fake.index_open = False
    wire(fake)
    st = state()
    ls._yacreader_tick(st)
    check("a windowless app is activated", fake.activations == 1 and fake.stops == 0)
    CLOCK[0] += 10
    ls._yacreader_tick(st)
    check("activation is throttled inside the check interval", fake.activations == 1)
    fake.index_open = True
    CLOCK[0] += config.SUPERVISOR_YAC_INDEX_CHECK_SEC
    ls._yacreader_tick(st)
    check("an open index resets the attempts",
          st["yac_activate_attempts"] == 0 and st["yac_activate_alerted"] is False)

    # 4. Persistent windowlessness alerts once and keeps trying, not restarting.
    fake = Fake()
    fake.running = True
    fake.index_open = False
    wire(fake)
    st = state()
    for _ in range(6):
        CLOCK[0] += config.SUPERVISOR_YAC_INDEX_CHECK_SEC
        ls._yacreader_tick(st)
    check("activation persists", fake.activations == 6)
    check("it never restarts for a missing window", fake.starts == 0)
    check("the owner is alerted once", len(fake.alerts) == 1)

    # 5. Crash loop -> backoff, no thrash.
    fake = Fake()
    wire(fake)
    st = state()
    ls._yacreader_tick(st)                    # starts, yac_started_at = now
    check("first start", fake.starts == 1)
    for _ in range(config.SUPERVISOR_YAC_CRASH_LIMIT):
        fake.running = False                  # it died immediately
        ls._yacreader_tick(st)
    check("three rapid crashes alert", any("crashed" in a for a in fake.alerts))
    check("and back off instead of restarting", st["yac_backoff_until"] is not None)
    before = fake.starts
    fake.running = False
    ls._yacreader_tick(st)
    check("no start while backed off", fake.starts == before)
    CLOCK[0] += config.SUPERVISOR_YAC_BACKOFF_SEC
    ls._yacreader_tick(st)
    check("after the backoff it tries again", fake.starts == before + 1)

    src = (Path(__file__).resolve().parent.parent / "library_supervisor.py").read_text()
    for needle in ("yacreader_db.index_open()", "yacreader_db.activate_app()",
                   "SUPERVISOR_YAC_CRASH_LIMIT", "ensure_scan_settings()"):
        check(f"source still contains {needle!r}", needle in src)
finally:
    for n, v in saved.items():
        setattr(ls, n, v)
    for n, v in saved_db.items():
        setattr(ls.yacreader_db, n, v)
    ls.time.time = saved_time
    ls.config.YACREADER_REFRESH_MARKER = saved_marker

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("YacReader supervisor: all checks passed")
