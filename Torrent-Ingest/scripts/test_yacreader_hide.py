#!/usr/bin/env python3
"""YacReader's window is hidden after every FLEET-initiated start (2026-09-19).

THE REPORT, verbatim: *"it keeps popping up and taking over the whole screen and that's
REALLY god damn annoying"*. The fleet bounces YacReader on its own schedule -- every
comic filing consumes the refresh marker and restarts it, and a crash restore triggers
an `activate` -- and `open -g` stops it STEALING focus but does not stop the window
APPEARING. So each fleet start/activate now ends in `yacreader_db.hide_app()`, and the
supervisor re-hides through a bounded window (async window creation) then stops, so a
reader the owner opens himself is never fought.

TWO ROUTES, and this test pins the order because it is the difference between the fix
working under launchd and only working in a terminal:

  * `NSRunningApplication.hide()` through AppleScriptObjC -- the AppKit hide, NO TCC
    grant required, tried FIRST;
  * System Events `set visible` -- UI scripting, needs Accessibility the fleet may not
    hold, only a FALLBACK.

All subprocesses are faked; nothing here touches the real app or its index.

    python3 scripts/test_yacreader_hide.py

Exit 0 means every check passed.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                          # noqa: E402
import yacreader_db                                                    # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


saved_running = yacreader_db.app_running
saved_run = yacreader_db.subprocess.run
saved_sleep = yacreader_db.time.sleep
try:
    yacreader_db.time.sleep = lambda _s: None          # no waiting in tests
    yacreader_db.app_running = lambda: RUNNING[0]

    RUNNING = [False]
    calls: list[tuple[int, str, str]] = []             # (rc, stdout, script)

    def fake_run(cmd, **kw):
        script = cmd[-1]
        calls.append((rc_to_use[0], stdout_to_use[0], script))
        return SimpleNamespace(returncode=rc_to_use[0],
                               stdout=stdout_to_use[0].encode())

    rc_to_use = [0]
    stdout_to_use = ["hidden"]
    yacreader_db.subprocess.run = fake_run

    print("Part 1 -- an app that is not running is not addressed")
    check("hide_app is a no-op with no process", yacreader_db.hide_app(attempts=1), False)
    check("...and it shells out to nothing", len(calls), 0)

    print("\nPart 2 -- the AppKit route is FIRST, and success is 'hidden'")
    RUNNING[0] = True
    rc_to_use[0], stdout_to_use[0] = 0, "hidden"
    calls.clear()
    check("hide_app succeeds", yacreader_db.hide_app(attempts=1), True)
    check("exactly one osascript ran", len(calls), 1)
    check("it used NSRunningApplication.hide()", "NSRunningApplication" in calls[0][2], True)
    check("it addressed the configured bundle id",
          config.YACREADER_BUNDLE_ID in calls[0][2], True)
    check("it did NOT fall through to System Events", "System Events" in calls[0][2], False)
    check("it checked isHidden() before claiming success", "isHidden" in calls[0][2], True)

    print("\nPart 3 -- a not-yet-registered app is retried, then hidden")
    seen: list[str] = []

    def fake_run_absent_once(cmd, **kw):
        seen.append(cmd[-1])
        out = "absent" if len(seen) == 1 else "hidden"
        return SimpleNamespace(returncode=0, stdout=out.encode())

    yacreader_db.subprocess.run = fake_run_absent_once
    check("hide_app still succeeds after the retry", yacreader_db.hide_app(attempts=2), True)
    check("it tried twice", len(seen), 2)
    yacreader_db.subprocess.run = fake_run

    print("\nPart 4 -- no AppKit bridge -> the System Events fallback runs")
    def fake_run_fallback(cmd, **kw):
        script = cmd[-1]
        calls.append((0, "", script))
        if "NSRunningApplication" in script:
            return SimpleNamespace(returncode=1, stdout=b"")
        return SimpleNamespace(returncode=0, stdout=b"")
    calls.clear()
    yacreader_db.subprocess.run = fake_run_fallback
    check("hide_app succeeds through System Events", yacreader_db.hide_app(attempts=1), True)
    check("AppKit was tried before System Events",
          "NSRunningApplication" in calls[0][2] and "System Events" in calls[1][2], True)

    print("\nPart 5 -- a refused hide fails SOFT, never raises")
    yacreader_db.subprocess.run = lambda cmd, **kw: SimpleNamespace(returncode=1,
                                                                    stdout=b"")
    check("hide_app returns False when both routes refuse",
          yacreader_db.hide_app(attempts=1), False)

    print("\nPart 6 -- the supervisor hides after start and after activation, bounded")
    src = (Path(__file__).resolve().parent.parent / "library_supervisor.py").read_text()
    for needle in ("_hide_yacreader(state, \"start\")",
                   "_hide_yacreader(state, \"activation\")",
                   "yacreader_db.hide_app(attempts=1, wait_sec=0)",
                   "config.SUPERVISOR_YAC_HIDE_SEC",
                   '"yac_hide_until": time.time() + config.SUPERVISOR_YAC_HIDE_SEC'):
        check(f"source contains {needle!r}", needle in src, True)
finally:
    yacreader_db.app_running = saved_running
    yacreader_db.subprocess.run = saved_run
    yacreader_db.time.sleep = saved_sleep

print()
if failures:
    print(f"FAIL: {len(failures)} check(s) failed")
    raise SystemExit(1)
print("PASS")
