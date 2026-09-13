"""Google Drive app supervisor.

Light novels land in a Google Drive folder (`config.NOVELS_ROOT`), so the Google
Drive macOS app must be running and its File Provider mount healthy or every novel
placement stalls. This daemon is a sibling of `library_supervisor`, but with a far
narrower job: it only keeps the Google Drive app up, and reports when the Novels
mount never comes back.

Each cycle:
  * If the Google Drive app is not running -> start it (`open -a`).
  * Confirm the Novels root is live (the CloudStorage folder exists and is a real,
    usable directory). A not-running app leaves it absent, so this also proves the
    app actually came up rather than merely spawning.

Unlike library_supervisor, it never STOPS anything and never gates the media
daemons: novels are an auxiliary shelf, and a dead Drive must never block
Shows/Movies/Comics. Storage pressure is a human problem, reported by
`fleet_health` (which flags the Drive volume when free space runs low), not fixed
here.

Runs as its own KeepAlive user-agent:

    python3 gdrive_supervisor.py            # daemon loop
    python3 gdrive_supervisor.py --status   # print state and exit
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# This repo's own directory goes FIRST on sys.path. Torrent-Ingest and Torrent-Searcher
# both ship modules named `config.py`, `library.py` and `ingest.py`, and both repos are on
# `sys.path` in some processes -- so a bare `import config` resolves to whichever repo the
# launcher happened to put first. That is how `directingest` died at import on 2026-08-27,
# reading Torrent-Ingest's `config` through Torrent-Searcher's `ingest` (§5 item 4a). The
# pin makes the resolution a property of the FILE rather than of how it was launched.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import config


def log(msg: str) -> None:
    line = f"[{config.log_stamp()}] {msg}"
    print(line, flush=True)
    try:
        config.rotate_log_if_large(config.GDRIVE_LOG_FILE)
        with config.GDRIVE_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def alert(msg: str) -> None:
    log("ALERT: " + msg)
    try:
        config.GDRIVE_ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with config.GDRIVE_ALERT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except OSError:
        pass
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        f"display notification {json.dumps(msg[:200])} with title "
                        f"{json.dumps('Google Drive Supervisor')}"],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def acquire_lock():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = config.GDRIVE_LOCK_FILE.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Another Google Drive supervisor holds the lock; exiting.")
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


# --- app control -------------------------------------------------------------

def gdrive_running() -> bool:
    return subprocess.run(["/usr/bin/pgrep", "-f", config.GDRIVE_PROC_PATTERN],
                          capture_output=True).returncode == 0


def start_gdrive() -> None:
    subprocess.run(["/usr/bin/open", "-a", config.GDRIVE_APP_NAME], capture_output=True)


def novels_ready() -> bool:
    """The Novels root exists and is a usable directory. The CloudStorage mount only
    appears once the Google Drive File Provider is up, so this doubles as the liveness
    proof that starting the app actually worked."""
    try:
        return config.NOVELS_ROOT.is_dir() and os.access(config.NOVELS_ROOT, os.W_OK)
    except OSError:
        return False


def tick(state: dict) -> None:
    if not gdrive_running():
        log("Google Drive not running; starting it")
        start_gdrive()
        state["ready"] = 0
        return

    if novels_ready():
        state["ready"] += 1
        if state["ready"] == config.GDRIVE_READY_DEBOUNCE:
            log("Google Drive running and Novels mount healthy")
        state["alerted"] = False
        return

    # App is running but the mount never came up. Don't spam: alert once per outage.
    state["ready"] = 0
    if not state.get("alerted"):
        state["alerted"] = True
        alert(f"Google Drive app is running but {config.NOVELS_ROOT} is not mounted/"
              f"accessible; light-novel placements will stall until it returns.")


def print_status() -> None:
    print(f"Google Drive: {'up' if gdrive_running() else 'down'} "
          f"(bundle {config.GDRIVE_BUNDLE_ID})")
    print(f"Novels root: {config.NOVELS_ROOT} ready={novels_ready()}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Google Drive app supervisor.")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    ap.add_argument("--once", action="store_true", help="one cycle then exit")
    args = ap.parse_args()

    if args.status:
        print_status()
        return 0

    acquire_lock()
    log("gdrive_supervisor started")
    state = {"ready": 0, "alerted": False}
    if args.once:
        tick(state)
        return 0
    while True:
        try:
            tick(state)
        except Exception as e:   # noqa: BLE001 -- daemon must never die on a cycle
            log(f"unexpected error: {e}")
        time.sleep(config.GDRIVE_POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
