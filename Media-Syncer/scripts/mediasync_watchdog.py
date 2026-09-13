"""Watchdog for the media_sync daemon.

`com.mikeyferguson.mediasync` deliberately carries **no** `KeepAlive`: the reaper stops it
with a kill while it purges deletions, and a KeepAlive would fight that by relaunching it
mid-purge. The reaper restarts it afterwards itself (`reap.ms_resume`, which retries and
verifies), so the normal pause/resume cycle needs no help.

What that leaves uncovered is the daemon dying for any *other* reason -- an unhandled crash,
an OOM kill, a bad interpreter upgrade. Its own loop swallows every exception, so this is
rare, but nothing brings it back and the failure is silent: uploads simply stop.

This watchdog closes that gap without re-creating the problem KeepAlive would cause. It
relaunches media_sync only when BOTH are true:

  * the process is absent, and
  * the reaper's pause marker (`REAP_PAUSED_MARKER`) is NOT present.

So a deliberate pause is respected and an accidental death is repaired. It also waits for
the marker to be genuinely stale before acting on a missing process, because there is a
brief window during a pause where the reaper has killed the daemon but not yet written the
marker.

    python3 -m scripts.mediasync_watchdog            # run the watchdog loop
    python3 -m scripts.mediasync_watchdog --once     # one check, print the verdict, exit
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config


POLL_SEC = 60
# How long the process must be absent before a relaunch. Covers the pause window in which
# the reaper has killed media_sync but has not yet written its marker, and the ordinary
# restart gap when something else kickstarts the job.
ABSENT_GRACE_SEC = 120
LABEL = "com.mikeyferguson.mediasync"
PROC_PATTERN = "scripts.media_sync"
LOG_FILE = config.SCRIPT_DIR.parent / "mediasync_watchdog.log"

# The reaper writes this while it holds media_sync down on purpose. Read from
# Torrent-Ingest's config when importable so the two cannot drift; the literal path is the
# fallback for a machine where that repo is absent.
_FALLBACK_MARKER = Path.home() / "Developer" / "Torrent-Ingest" / "state" / "reap_ms_paused"


def paused_marker() -> Path:
    try:
        import sys
        ti = Path.home() / "Developer" / "Torrent-Ingest"
        if str(ti) not in sys.path:
            sys.path.append(str(ti))
        import config as ti_config           # noqa: WPS433  (Torrent-Ingest's config)
        return Path(ti_config.REAP_PAUSED_MARKER)
    except Exception:                        # noqa: BLE001
        return _FALLBACK_MARKER


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[RotatingFileHandler(LOG_FILE, maxBytes=config.LOG_MAX_BYTES,
                                      backupCount=config.LOG_BACKUP_COUNT),
                  logging.StreamHandler()])


def is_running() -> bool:
    r = subprocess.run(["/usr/bin/pgrep", "-f", PROC_PATTERN],
                       capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def is_paused() -> bool:
    try:
        return paused_marker().exists()
    except OSError:
        return False                          # unreadable marker -> assume not paused


def relaunch() -> bool:
    """Kickstart the job and confirm it actually came up."""
    uid = subprocess.run(["/usr/bin/id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["/bin/launchctl", "kickstart", f"gui/{uid}/{LABEL}"],
                   capture_output=True, text=True)
    for _ in range(10):
        time.sleep(1)
        if is_running():
            return True
    return False


def check_once(log: bool = True) -> str:
    """One evaluation. Returns 'running' | 'paused' | 'relaunched' | 'relaunch-failed'."""
    if is_running():
        return "running"
    if is_paused():
        if log:
            logging.info("media_sync is down and the reaper's pause marker is present; "
                         "leaving it alone.")
        return "paused"
    if log:
        logging.warning("media_sync is NOT running and is NOT paused by the reaper; "
                        "relaunching.")
    if relaunch():
        if log:
            logging.info("media_sync relaunched.")
        return "relaunched"
    if log:
        logging.error("media_sync relaunch did NOT bring the process up; will retry.")
    return "relaunch-failed"


def main() -> int:
    ap = argparse.ArgumentParser(description="Relaunch media_sync if it dies unpaused.")
    ap.add_argument("--once", action="store_true", help="one check, print the verdict, exit")
    args = ap.parse_args()
    setup_logging()
    if args.once:
        print(check_once(log=False))
        return 0

    logging.info(f"mediasync watchdog started (poll {POLL_SEC}s, "
                 f"grace {ABSENT_GRACE_SEC}s, marker {paused_marker()})")
    absent_since = None
    while True:
        try:
            if is_running():
                absent_since = None
            else:
                now = time.time()
                if absent_since is None:
                    absent_since = now
                elif (now - absent_since) >= ABSENT_GRACE_SEC:
                    check_once()
                    absent_since = None
        except Exception as e:                # noqa: BLE001
            logging.error(f"watchdog error (continuing): {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
