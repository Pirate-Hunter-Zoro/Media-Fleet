#!/usr/bin/env python3
"""A library SCAN must not be restarted as if it were a hang.

HANDOFF §6 carried this as an accepted limit: `library_supervisor` cannot tell a hung
Jellyfin from a scanning one (90s threshold; a real FUSE scan is slower). It had restarted
Jellyfin 17 times. The two were genuinely indistinguishable through `/Items/Counts` alone,
because that endpoint runs a DB query that a scan starves past its own timeout -- so a
scanning Jellyfin and a hung one both return None, forever, identically.

They ARE separable through `/ScheduledTasks`, which Jellyfin serves from memory and keeps
answering under scan load. This freezes the three behaviours that follow from that:

  1. API quiet and NO scan running          -> restart (the original hang case, preserved)
  2. API quiet and a scan PROGRESSING       -> defer, and keep deferring while it moves
  3. API quiet and a scan STUCK at one pct  -> restart once the grace expires

Case 3 is the one that keeps this honest: a wedged scan reports `Running` forever, so
"a scan is running" can never be an unbounded excuse not to restart.
"""
from __future__ import annotations

import sys
import time
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


class FakeGuardian:
    """Counts restarts instead of performing them."""

    def __init__(self) -> None:
        self.restarts = 0

    def stop_jellyfin(self) -> bool:
        return True

    def start_jellyfin(self) -> None:
        self.restarts += 1

    def jellyfin_running(self) -> bool:
        return True


def drive(scan_sequence, seconds_per_poll=45, polls=200):
    """Run the supervisor's unresponsive branch against a scripted scan state.

    `scan_sequence` is called with the poll index and returns what
    `jellyfin_scan_progress()` should report. Returns (restarts, polls_run).
    """
    guard = FakeGuardian()
    saved_guard = ls.db_guardian
    saved_count = ls.jellyfin_episode_count
    saved_scan = ls.jellyfin_scan_progress
    saved_log = ls.log
    saved_time = ls.time.time

    clock = [1_000_000.0]
    ls.db_guardian = guard
    ls.jellyfin_episode_count = lambda: None          # the API is quiet throughout
    ls.log = lambda *a, **k: None
    ls.time.time = lambda: clock[0]

    state = {"ready": 0, "gutted": 0, "unresponsive_since": None,
             "scan_since": None, "scan_pct": None}
    try:
        for i in range(polls):
            ls.jellyfin_scan_progress = lambda _i=i: scan_sequence(_i)
            _unresponsive_branch(state, clock)
            clock[0] += seconds_per_poll
    finally:
        ls.db_guardian = saved_guard
        ls.jellyfin_episode_count = saved_count
        ls.jellyfin_scan_progress = saved_scan
        ls.log = saved_log
        ls.time.time = saved_time
    return guard.restarts


def _unresponsive_branch(state, clock):
    """The exact logic under test, lifted from library_supervisor.main()'s else-branch.

    Kept as a transcription rather than calling main(), because main() owns the lock file,
    the mount probe and the YacReader half -- none of which this is about. The assertion
    that this stays in step with the real thing is `test_branch_matches_source` below.
    """
    count = ls.jellyfin_episode_count()
    if count is None:
        if state["unresponsive_since"] is None:
            state["unresponsive_since"] = ls.time.time()
        elif ls.time.time() - state["unresponsive_since"] >= config.SUPERVISOR_UNRESPONSIVE_SEC:
            pct = ls.jellyfin_scan_progress()
            if pct is None:
                ls.db_guardian.stop_jellyfin()
                ls.db_guardian.start_jellyfin()
                state["unresponsive_since"] = None
                state["scan_since"] = None
                state["scan_pct"] = None
            else:
                now = ls.time.time()
                if state.get("scan_since") is None or pct > (state.get("scan_pct") or -1.0):
                    state["scan_since"] = now
                    state["scan_pct"] = pct
                    state["unresponsive_since"] = now
                elif now - state["scan_since"] >= config.SUPERVISOR_SCAN_GRACE_SEC:
                    ls.db_guardian.stop_jellyfin()
                    ls.db_guardian.start_jellyfin()
                    state["unresponsive_since"] = None
                    state["scan_since"] = None
                    state["scan_pct"] = None
    else:
        state["unresponsive_since"] = None
        state["scan_since"] = None
        state["scan_pct"] = None


print("=== supervisor: a scan is not a hang ===")

# 1. No scan running: the original hang behaviour must be untouched.
restarts = drive(lambda i: None, seconds_per_poll=45, polls=10)
check("API quiet, no scan -> Jellyfin IS restarted (hang case preserved)", restarts >= 1)

# 2. A scan that keeps progressing must never be restarted, however long it runs.
#    200 polls x 45s = 2.5 hours, well past both the 90s threshold and the 1h grace.
restarts = drive(lambda i: min(99.0, i * 0.5), seconds_per_poll=45, polls=200)
check("API quiet, scan PROGRESSING for 2.5h -> never restarted", restarts == 0)

# 3. A scan wedged at one percentage is restarted once the grace expires.
restarts = drive(lambda i: 42.0, seconds_per_poll=45, polls=200)
check("API quiet, scan STUCK at 42% -> restarted after the grace", restarts >= 1)

# 4. The stuck scan must NOT be restarted before the grace has elapsed.
polls_inside_grace = int(config.SUPERVISOR_SCAN_GRACE_SEC // 45) - 2
restarts = drive(lambda i: 42.0, seconds_per_poll=45, polls=max(3, polls_inside_grace))
check("stuck scan inside the grace -> not yet restarted", restarts == 0)

# 5. A scan that finishes (None again) with the API still quiet falls back to the hang path.
restarts = drive(lambda i: 10.0 if i < 5 else None, seconds_per_poll=45, polls=20)
check("scan ends while API still quiet -> restarted as a hang", restarts >= 1)

# 6. The probe must treat its OWN failure as "no scan", never as "scanning" -- otherwise an
#    unreachable Jellyfin would defer its own restart forever.
saved = ls.urllib.request.urlopen
try:
    def boom(*a, **k):
        raise OSError("connection refused")
    ls.urllib.request.urlopen = boom
    saved_url, saved_key = config.JELLYFIN_URL, config.JELLYFIN_API_KEY
    config.JELLYFIN_URL, config.JELLYFIN_API_KEY = "http://x", "k"
    got = ls.jellyfin_scan_progress()
    config.JELLYFIN_URL, config.JELLYFIN_API_KEY = saved_url, saved_key
    check("scan probe failing -> None ('no scan'), so a hang still restarts", got is None)
finally:
    ls.urllib.request.urlopen = saved

# 7. The transcription above must still match the shipped source, or this test is theatre.
src = (Path(__file__).resolve().parent.parent / "library_supervisor.py").read_text()
for needle in ("pct = jellyfin_scan_progress()",
               'state["scan_since"] = now',
               'elif now - state["scan_since"] >= config.SUPERVISOR_SCAN_GRACE_SEC:',
               'state["unresponsive_since"] = now'):
    check(f"source still contains {needle!r}", needle in src)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("supervisor scan-grace: all checks passed")
