#!/usr/bin/env python3
"""onepace_thumbs.py — generate episode thumbnails for shows with no image provider.

One Pace has no TMDB/TVDB entry, so its episode stills normally come from the One Pace
Jellyfin plugin, which keys off a per-episode id that can't be recovered after a DB
reset. When those stills are missing (a fresh ingest, or the id lost), this generates a
representative frame from each episode video, saves it as the `-thumb.jpg` sidecar and
pushes it to Jellyfin — capped per run so a cold-library backfill drains gradually and
new media is picked up the same way, cycle after cycle.

Runs under launchd (`com.mikeyferguson.onepacethumbs`); idempotent (skips any episode
that already has a `-thumb.jpg`), so it is safe to re-run and catches new episodes as
they land. Stdlib + ffmpeg/ffprobe only.

    python3 scripts/onepace_thumbs.py --once --dry-run
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                                  # noqa: E402
from media_doctor import Jellyfin              # noqa: E402


# --- tunables ----------------------------------------------------------------

TARGET_SHOWS = [s.strip() for s in os.environ.get("ONEPACE_THUMBS_SHOWS", "One Pace").split(",")
                if s.strip()]
MAX_PER_RUN = int(os.environ.get("ONEPACE_THUMBS_MAX_PER_RUN", "20"))
CYCLE_SEC = int(os.environ.get("ONEPACE_THUMBS_CYCLE_SEC", "1800"))    # 30 min
# Frame taken this far into the episode (past the OP for most shows), clamped to a sane band.
FRAME_AT = 0.15
FRAME_MIN_SEC = 60.0
FRAME_MAX_SEC = 600.0


def _log(msg: str) -> None:
    print(f"[onepace_thumbs] {msg}", flush=True)


def _duration_sec(video: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=120)
        return float(out.stdout.strip() or 0)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return 0.0


def _screenshot(video: Path, offset: float, out: Path) -> bool:
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{offset:.1f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "3", "-y", str(out)],
            capture_output=True, timeout=300)
        return r.returncode == 0 and out.exists() and out.stat().st_size > 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_once(dry_run: bool) -> int:
    made = 0
    try:
        jf = Jellyfin()
    except Exception as exc:                                          # noqa: BLE001
        _log(f"Jellyfin unavailable ({exc}); nothing done this pass")
        return 0

    # {resolved video path -> episode id}, for the push step.
    series_by_path = jf.series_index()
    for show in TARGET_SHOWS:
        show_dir = None
        sid = None
        for path, item_id in series_by_path.items():
            folder = Path(path).name
            if folder == show or folder.startswith(show):
                show_dir = Path(path)
                sid = item_id
                break
        if not sid:
            _log(f"{show}: no Jellyfin series item; skipping")
            continue
        ep_by_path = {}
        for e in jf.episodes(sid):
            p = e.get("Path")
            if p:
                try:
                    ep_by_path[str(Path(p).resolve())] = e["Id"]
                except Exception:                                     # noqa: BLE001
                    pass
        videos = [p for p in show_dir.rglob("*")
                  if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS
                  and not p.name.startswith("._")]
        for v in videos:
            if made >= MAX_PER_RUN:
                _log(f"{show}: hit per-run cap ({MAX_PER_RUN}); the rest drains next cycle")
                return made
            thumb = v.with_name(v.stem + "-thumb.jpg")
            if thumb.exists():
                continue                          # already has a still
            dur = _duration_sec(v)
            offset = max(FRAME_MIN_SEC, min(dur * FRAME_AT, FRAME_MAX_SEC)) if dur else FRAME_MIN_SEC
            if dry_run:
                _log(f"  WOULD screenshot {v.name} @ {offset:.0f}s")
                made += 1
                continue
            tmp = v.with_name(v.stem + "-thumb.tmp.jpg")
            if not _screenshot(v, offset, tmp):
                _log(f"  screenshot failed for {v.name}; leaving it for a later pass")
                continue
            data = tmp.read_bytes()
            tmp.replace(thumb)                     # write the -thumb.jpg sidecar
            eid = ep_by_path.get(str(v.resolve()))
            if eid:
                try:
                    jf.push_primary(eid, data)
                except Exception as exc:          # noqa: BLE001
                    _log(f"  push failed for {v.name}: {exc}")
            _log(f"  generated {v.name} -> {thumb.name}")
            made += 1
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate thumbnails for shows with no image provider.")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--dry-run", action="store_true", help="report only; generate nothing")
    args = ap.parse_args()

    _log(f"onepace_thumbs up (shows={TARGET_SHOWS}, cycle={CYCLE_SEC}s)")
    while True:
        try:
            n = run_once(args.dry_run)
            _log(f"pass done: {n} thumbnail(s) {'would be' if args.dry_run else ''} generated")
        except Exception as exc:                                      # noqa: BLE001
            _log(f"cycle error (non-fatal): {exc}")
        if args.once:
            return 0
        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
