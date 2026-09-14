#!/usr/bin/env python3
"""Report YacReader's scan-at-startup flags, and put them back if they have drifted.

WHY THIS IS A TOOL AND NOT A ONE-TIME FIX
    YACReader never notices the filesystem on its own. A comic exists to the reader only
    after the APP runs a library update, and the only reliable trigger is the
    `UPDATE_LIBRARIES_AT_STARTUP` flag in its own ini. On 2026-09-14 every ElfQuest file
    was filed, on the mount, in the pool -- and invisible in YacReader, because both
    auto-update flags read `false` (they were `true` in the July and Sep-05 backups).
    The library supervisor now owns this invariant and repairs drift in the same window
    in which it starts the app; this tool is the human half: look, and force.

    `--apply` also drops the supervisor's refresh marker, so a running app is bounced
    and re-scans even when the flags were never wrong. That is the repair for "the index
    is stale", which an app restart fixes and a flag patch alone does not.

    The ini is patched under the index lock, never while the app is up: YacReader
    rewrites the file itself on exit.

    python3 scripts/yacreader_rescan.py             # report; exit 1 on drift
    python3 scripts/yacreader_rescan.py --files     # also list shelf files the index lacks
    python3 scripts/yacreader_rescan.py --apply     # fix the flags + force a rescan
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                        # noqa: E402
import yacreader_db                                                  # noqa: E402
import yacreader_index                                               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="YacReader scan-at-startup report/repair.")
    ap.add_argument("--apply", action="store_true",
                    help="patch the flags under the index lock and request a rescan")
    ap.add_argument("--files", action="store_true",
                    help="also list shelf comic files that are not in the index")
    args = ap.parse_args()

    current = yacreader_db.read_scan_settings()
    drift = not yacreader_db.scan_settings_ok()
    print(f"ini:    {config.YACREADER_INI}")
    for key, want in config.YACREADER_SCAN_SETTINGS.items():
        have = (current.get(key) or "<absent>")
        mark = "ok   " if have.strip().lower() == want else "DRIFT"
        print(f"  [{mark}] {key}={have}  (the app must say {want})")
    print(f"app:    {'running' if yacreader_db.app_running() else 'not running'}")

    missing: list[str] = []
    if args.files and config.YACREADER_DB.exists():
        missing = yacreader_index.unindexed_files(
            config.YACREADER_DB, config.MEDIA_SYNCER_INVENTORY,
            config.MEDIAFS_MOUNT / "Comics")
        print(f"index:  {len(missing)} comic file(s) on the shelf are not in the index")
        for m in missing[:20]:
            print(f"   {m}")
        if len(missing) > 20:
            print(f"   ... and {len(missing) - 20} more")

    if not args.apply:
        if drift or missing:
            print("\nfix:   python3 scripts/yacreader_rescan.py --apply"
                  "   (lock, patch, restart through the supervisor, rescan)")
        return 1 if drift else 0

    with yacreader_db.db_lock("yacreader_rescan"):
        changed = yacreader_db.ensure_scan_settings()
        try:
            config.YACREADER_REFRESH_MARKER.parent.mkdir(parents=True, exist_ok=True)
            config.YACREADER_REFRESH_MARKER.write_text("manual rescan requested\n",
                                                       encoding="utf-8")
        except OSError as exc:
            print(f"could not write the refresh marker: {exc}")
    print(f"\nini {'patched' if changed else 'already correct'}; refresh requested.")
    print("The library supervisor restarts YacReader within a few seconds; its startup "
          "update then indexes every comic on the shelf.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
