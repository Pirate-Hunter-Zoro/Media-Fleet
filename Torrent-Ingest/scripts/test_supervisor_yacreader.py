#!/usr/bin/env python3
"""YacReader's freshness contract in the supervisor, both directions.

Three failure modes, all measured on 2026-09-14:

  * the app is down -> it must be started WITH the scan flags patched, and the
    refresh marker consumed (the startup update covers it);
  * the app is up but its flags drifted -> bounce it, or nothing ever scans again;
  * the app is up but has NO library open (a crash restore: no window means
    `LibrariesUpdateCoordinator::init()` never runs) -> activate it, because it looks
    healthy while scanning nothing. That is the state ElfQuest sat invisible in. The
    activation is BOUNDED (two tries, then one alert): it steals focus, and a reader
    with nothing to scan is not repaired by being brought to the front every minute.

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
        self.hides = 0
        self.hide_ok = True
        self.index_open = False
        self.updating = False          # a library update is in flight
        self.settings_ok = True
        self.alerts: list[str] = []


CLOCK = [1_000_000.0]

saved = {n: getattr(ls, n) for n in ("yacreader_running", "stop_yacreader", "start_yacreader",
                                     "log", "alert")}
saved_db = {n: getattr(ls.yacreader_db, n) for n in ("ensure_scan_settings",
                                                     "scan_settings_ok", "activate_app",
                                                     "hide_app", "index_open",
                                                     "update_in_progress")}
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
    ls.yacreader_db.hide_app = lambda attempts=3, wait_sec=1.0: (
        setattr(fake, "hides", fake.hides + 1), fake.hide_ok)[-1]
    ls.yacreader_db.index_open = lambda: fake.index_open
    ls.yacreader_db.update_in_progress = lambda: fake.updating


def state() -> dict:
    return {"yac_started_at": None, "yac_stopped_by_us": False, "yac_crashes": 0,
            "yac_backoff_until": None, "yac_backoff_alerted": False,
            "yac_last_refresh": 0.0, "yac_index_checked_at": 0,
            "yac_activate_attempts": 0, "yac_activate_alerted": False,
            "yac_hide_pending": False, "yac_hide_arm_at": 0.0}


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
    check("a just-started reader is NOT hidden before its window exists", fake.hides == 0)
    check("...but hiding is armed", st["yac_hide_pending"] is True)

    # 1b. Hiding waits for the library update -- the proof the window exists. Hiding
    #     before then suppresses the window entirely (measured 2026-09-19: 0 windows
    #     while hidden, 1 after un-hiding).
    fake.updating = True
    CLOCK[0] += 5
    ls._yacreader_tick(st)
    check("the reader is hidden once its update is underway", fake.hides == 1)
    check("...and hiding is not re-armed", st["yac_hide_pending"] is False)
    fake.updating = False
    CLOCK[0] += 5
    ls._yacreader_tick(st)
    check("an idle reader is not hidden again", fake.hides == 1)

    # 1d. An app that never starts an update -- parked on the library CHOOSER, measured
    #     2026-09-19 -- is still hidden, once the settle window passes. Before that it is
    #     left alone so the window created at launch is never raced.
    fake = Fake()
    wire(fake)
    st = state()
    ls._yacreader_tick(st)                    # down -> start, arms the hide
    CLOCK[0] += 5
    ls._yacreader_tick(st)
    check("before the settle window a chooser app is not hidden", fake.hides == 0)
    CLOCK[0] += config.SUPERVISOR_YAC_HIDE_SETTLE_SEC
    ls._yacreader_tick(st)
    check("after the settle window it is hidden anyway", fake.hides == 1)
    check("...and hiding is not re-armed", st["yac_hide_pending"] is False)

    # 1c. A refused hide -- System Events/Accessibility is not granted to the fleet -- must
    #     not break the start or the tick. It is reported, not swallowed, and the app runs.
    fake = Fake()
    fake.hide_ok = False
    wire(fake)
    logs: list[str] = []
    ls.log = lambda msg, *a, **k: logs.append(str(msg))
    st = state()
    ls._yacreader_tick(st)
    fake.updating = True
    CLOCK[0] += 5
    ls._yacreader_tick(st)
    check("a refused hide still starts the app", fake.starts == 1 and fake.running)
    check("...and it is reported, not swallowed",
          any("could not be hidden" in m for m in logs))
    ls.log = lambda *a, **k: None

    # 2. Drifted flags while up -> bounce (stop + start) so the patch takes.
    fake = Fake()
    fake.running = True
    fake.settings_ok = False
    wire(fake)
    st = state()
    ls._yacreader_tick(st)
    check("drifted flags bounce the app", fake.stops == 1 and fake.starts == 1)
    check("...and it is running again", fake.running)
    check("...and hiding is armed for the new process", st["yac_hide_pending"] is True)
    fake.updating = True
    CLOCK[0] += 5
    ls._yacreader_tick(st)
    check("...and it is hidden once its update runs", fake.hides == 1)

    # 2b. A refresh marker while the app is UP no longer restarts it. The 30-minute
    #     periodic update indexes new comics; a restart lands the app on its library
    #     chooser where nothing scans until a human clicks Comics (owner decision
    #     2026-09-19).
    fake = Fake()
    fake.running = True
    wire(fake)
    ls.config.YACREADER_REFRESH_MARKER.write_text("pending\n", encoding="utf-8")
    st = state()
    st["yac_started_at"] = CLOCK[0] - config.SUPERVISOR_YAC_CRASH_WINDOW_SEC - 1
    ls._yacreader_tick(st)
    check("filed comics do NOT restart the reader",
          fake.stops == 0 and fake.starts == 0 and fake.running)
    check("the marker is consumed anyway",
          not ls.config.YACREADER_REFRESH_MARKER.exists())

    # 3. Up, flags fine, no library open -> activate (throttled), never restart.
    fake = Fake()
    fake.running = True
    fake.index_open = False
    wire(fake)
    st = state()
    ls._yacreader_tick(st)
    check("a windowless app is activated", fake.activations == 1 and fake.stops == 0)
    check("activation does NOT hide it immediately", fake.hides == 0)
    check("...but arms the hide for when its update runs", st["yac_hide_pending"] is True)
    CLOCK[0] += 10
    ls._yacreader_tick(st)
    check("activation is throttled inside the check interval", fake.activations == 1)
    fake.updating = True
    CLOCK[0] += config.SUPERVISOR_YAC_INDEX_CHECK_SEC
    ls._yacreader_tick(st)
    check("an update in flight resets the attempts",
          st["yac_activate_attempts"] == 0 and st["yac_activate_alerted"] is False)

    # 3b. An app with an update IN FLIGHT must never be activated: it closes its index
    #     between operations and I/O-bound scanning can sit at ~1.5% CPU, so the
    #     transaction's journal (`update_in_progress`) is the signal -- not CPU. Activating
    #     it can collide a model reload with the transaction and wedge the scan (the
    #     2026-09-14 ElfQuest wedge): it must be left alone.
    fake = Fake()
    fake.running = True
    fake.index_open = False
    fake.updating = True
    wire(fake)
    st = state()
    for _ in range(3):
        CLOCK[0] += config.SUPERVISOR_YAC_INDEX_CHECK_SEC
        ls._yacreader_tick(st)
    check("an app mid-update is never activated", fake.activations == 0)
    fake.updating = False
    CLOCK[0] += config.SUPERVISOR_YAC_INDEX_CHECK_SEC
    ls._yacreader_tick(st)
    check("an idle app still gets activated", fake.activations == 1)

    # 4. Persistent chooser-parking alerts once and stops activating: activation steals
    #    focus, and a reader parked on its library chooser is not repaired by being
    #    brought to the front every minute (the 2026-09-15 stale-index false positive
    #    had this firing for hours). It is NOT restarted: a restart does not make the
    #    chooser open the library, it only interrupts whatever the owner was doing.
    fake = Fake()
    fake.running = True
    fake.index_open = False
    wire(fake)
    st = state()
    for _ in range(6):
        CLOCK[0] += config.SUPERVISOR_YAC_INDEX_CHECK_SEC
        ls._yacreader_tick(st)
    check("activation is bounded, not repeated",
          fake.activations == config.SUPERVISOR_YAC_ACTIVATE_MAX_ATTEMPTS)
    check("a chooser-parked reader is never restarted",
          fake.starts == 0 and fake.stops == 0)
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
    for needle in ("yacreader_db.update_in_progress()", "yacreader_db.activate_app()",
                   "yacreader_db.hide_app()", "_hide_yacreader",
                   "SUPERVISOR_YAC_CRASH_LIMIT", "ensure_scan_settings()",
                   '"/usr/bin/open", "-g"'):
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
