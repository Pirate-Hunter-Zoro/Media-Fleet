"""MEGA rubbish-bin emptier.

Media-Syncer replaces files in the MEGA pool when a better copy lands (and the MEGA
desktop app replaces files it backs up), which moves the old versions into each account's
rubbish bin. Nothing empties those bins, so they accumulate to tens of GB. This daemon
runs `rclone cleanup <remote>:` for every `type = mega` remote in the active rclone.conf
on a slow schedule, reclaiming the space.

`base_mega1` (the owner's MEGA backup account) is picked up automatically as soon as
it exists in the conf — no code change needed.

Runs as its own KeepAlive user-agent:

    python3 mega_trash_daemon.py            # daemon loop
    python3 mega_trash_daemon.py --once     # one sweep then exit
"""
from __future__ import annotations

import argparse
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
import mega


def log(msg: str) -> None:
    line = f"[{config.log_stamp()}] {msg}"
    print(line, flush=True)
    try:
        config.rotate_log_if_large(config.MEGA_TRASH_LOG_FILE)
        with config.MEGA_TRASH_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def sweep() -> None:
    remotes = mega.mega_remotes()
    if not remotes:
        log("no MEGA remotes in rclone.conf; nothing to empty")
        return
    for remote in remotes:
        ok = mega.cleanup(remote)
        log(f"{remote}: rubbish bin {'emptied' if ok else 'cleanup failed'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Empty every MEGA remote's rubbish bin.")
    ap.add_argument("--once", action="store_true", help="one sweep then exit")
    args = ap.parse_args()
    log("mega_trash_daemon starting")
    while True:
        try:
            sweep()
        except Exception as exc:  # noqa: BLE001 -- a failed sweep must not kill the daemon
            log(f"sweep error (continuing): {exc}")
        if args.once:
            return 0
        log(f"sleeping {config.MEGA_TRASH_INTERVAL_SEC}s")
        time.sleep(config.MEGA_TRASH_INTERVAL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
