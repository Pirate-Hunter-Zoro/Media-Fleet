"""MEGA desktop app supervisor.

The MEGA desktop app is what actually backs up the Developer directory (including these
repos and the library DB) to the owner's MEGA backup account. If it dies, the backup silently stops.
This daemon keeps the app up and reports when it stays down, mirroring
`gdrive_supervisor` but for MEGA.

Each cycle:
  * If the MEGA app is not running -> start it (`open -a MEGA`).
  * It never STOPS anything and never gates the media daemons — a dead backup app must
    never block ingestion.

Runs as its own KeepAlive user-agent:

    python3 mega_supervisor.py            # daemon loop
    python3 mega_supervisor.py --status   # print state and exit
"""
from __future__ import annotations

import argparse
import subprocess
import time

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
        config.rotate_log_if_large(config.MEGA_LOG_FILE)
        with config.MEGA_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _mega_running() -> bool:
    try:
        out = subprocess.run(
            ["/bin/ps", "axo", "command"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return True  # can't tell -> assume fine, don't thrash
    return config.MEGA_PROC_PATTERN in out.stdout


def _launch() -> bool:
    try:
        r = subprocess.run(["/usr/bin/open", "-a", config.MEGA_APP_NAME],
                           check=False, capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def cycle() -> str:
    if _mega_running():
        return "running"
    log("MEGA app not running; starting it")
    if _launch():
        return "started"
    log("ALERT: could not start MEGA app")
    return "down"


def main() -> int:
    ap = argparse.ArgumentParser(description="Keep the MEGA desktop app alive.")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    args = ap.parse_args()
    if args.status:
        print("running" if _mega_running() else "down")
        return 0
    log("mega_supervisor starting")
    down_streak = 0
    while True:
        try:
            state = cycle()
        except Exception as exc:  # noqa: BLE001
            log(f"cycle error (continuing): {exc}")
            state = "down"
        # A supervisor that cannot bring the app up must back off instead of thrashing
        # `open` every poll -- a stuck launch once filled the log with "MEGA app not
        # running; starting it" every 10s while the app was in fact fine (the process
        # pattern simply didn't match the real app name). Healthy/started clears the
        # streak; each consecutive down poll doubles the wait up to a 10-minute cap.
        if state == "down":
            down_streak = min(down_streak + 1, 6)
        else:
            down_streak = 0
        time.sleep(config.MEGA_POLL_SEC * (2 ** down_streak))


if __name__ == "__main__":
    raise SystemExit(main())
