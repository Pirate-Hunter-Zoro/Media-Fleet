"""Downloading: space-bounded waves of video, and audio-only pulls for tracks.

Two storage rules are enforced here, and they are the reason this module exists at all
rather than being three lines inside the daemon:

  * Downloads land in a dot-prefixed scratch dir on the DOWNLOADS volume, never inside
    the library root. Torrent-Ingest learned this the hard way (the July 2026 One Piece
    outage): heavy write I/O inside the library root starves the directory reads mediafs
    serves to Jellyfin and wedges the mount. The dot prefix keeps an in-flight file
    invisible to Media-Syncer's uploader scan and to the reaper's delete detection.

  * Work is done in WAVES with a live disk budget, and each wave's scratch copy is freed
    as soon as its files are safely in the library. A 900-video backlog therefore flows
    through a few GB of transient disk instead of demanding room for all of it at once.

Because the Downloads volume and the library root are the same physical volume on this
machine, `library.apply_plan` hardlinks the scratch file into its staging dir rather
than copying it -- so publishing a video into the library costs no second copy, and
deleting the scratch afterwards frees nothing because there was never a duplicate.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import discover
import ytconfig


def _env() -> dict:
    return discover.build_env()


def free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def plan_wave(entries: list, log_fn=print) -> tuple[list, list]:
    """Choose the next wave from `entries` (playlist order preserved).

    Returns (wave, oversized): `wave` is what to download now, `oversized` is entries
    that can never fit and should be recorded as skipped rather than retried forever.
    """
    budget = ytconfig.wave_bytes()
    ytconfig.INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    headroom = free_bytes(ytconfig.INCOMING_DIR) - ytconfig.MIN_FREE_BYTES
    budget = min(budget, max(0, headroom))

    wave, oversized, used = [], [], 0
    for e in entries:
        size = e.get("filesize") or ytconfig.ASSUMED_VIDEO_BYTES
        if size > ytconfig.MAX_VIDEO_BYTES:
            oversized.append(e)
            continue
        if len(wave) >= ytconfig.MAX_VIDEOS_PER_CYCLE:
            break
        # Budget against the padded size, not the raw estimate: summing a dozen unpadded
        # guesses is exactly how a wave lands at 2x its planned size.
        if wave and used + size * ytconfig.SPACE_SAFETY_FACTOR > budget:
            break
        # A single video bigger than the whole budget still goes, alone, provided the
        # disk can physically take it -- otherwise nothing would ever move.
        if not wave and size > budget and size > max(0, headroom):
            log_fn(f"  ! not enough free space for {e['title'][:60]!r} "
                   f"({size // 1024**2} MB needed, {max(0, headroom) // 1024**2} MB usable)")
            break
        wave.append(e)
        used += size * ytconfig.SPACE_SAFETY_FACTOR
    if wave:
        log_fn(f"  wave: {len(wave)} video(s), ~{used / 1024**3:.1f} GB budgeted "
               f"(estimate + {int((ytconfig.SPACE_SAFETY_FACTOR - 1) * 100)}%; "
               f"budget {budget / 1024**3:.1f} GB)")
    return wave, oversized


def _base_args(clients: str | None = None) -> list:
    """Download flags. Signed-out, always -- a public video needs no credential."""
    return [
        *(["--force-ipv4"] if ytconfig.FORCE_IPV4 else []),
        *discover.extractor_args(clients=clients),
        "--ignore-config",
        "--no-warnings",
        "--ignore-errors",              # one dead video must not kill the wave
        "--no-playlist",                # each URL is one video, by construction
        "--retries", "5",
        "--fragment-retries", "10",
        "--socket-timeout", "30",
        "--no-progress",
    ]


def download_videos(wave: list, dest: Path, log_fn=print) -> dict:
    """Download a wave of videos into `dest`. Returns {video_id: {path, info}}.

    Each video is named by its ID only. The human-readable library name is assigned
    later, by the placement plan -- so nothing downstream can accidentally depend on a
    YouTube title being a filename.

    Videos are fetched ONE AT A TIME so that real free space can be re-read between them.
    A single yt-dlp call for the whole wave is cheaper and was what this did, but it has
    no seam to check the disk at: the wave's estimate is committed to before the first
    byte lands, so an under-estimate is only discovered once the disk is already through
    the floor. That is not hypothetical -- see ytconfig.ASSUMED_VIDEO_BYTES for the wave
    that overshot by 10.7 GiB. The estimate now bounds what is *attempted*; this loop
    bounds what is actually *spent*, which is the only figure the disk cares about.
    """
    dest.mkdir(parents=True, exist_ok=True)
    log_fn(f"  downloading {len(wave)} video(s), one at a time under the floor guard...")
    got, attempted = _fetch_under_floor(wave, dest, None, log_fn, first_is_exempt=True)

    missing = [e for e in attempted if e["id"] not in got]
    # Second pass, only for what the default client could not deliver. This is where the
    # PO-token 403 is rescued (§ ytconfig.PLAYER_CLIENTS_FALLBACK) without letting a
    # narrow fallback client near the ones that were fine. Each client runs under the same
    # guard, and only for whatever is STILL missing -- so `missing` includes anything
    # --max-filesize just aborted, and retrying THAT in one unguarded batch would spend
    # exactly the bytes the guard refused.
    for client in ytconfig.PLAYER_CLIENTS_FALLBACK:
        if not missing:
            break
        log_fn(f"  retrying {len(missing)} video(s) with player_client={client}")
        retried, _ = _fetch_under_floor(missing, dest, client,
                                        log_fn, first_is_exempt=False)
        got.update(retried)
        missing = [e for e in missing if e["id"] not in got]

    log_fn(f"  downloaded {len(got)} of {len(attempted)} attempted "
           f"({len(wave)} planned); headroom now "
           f"{(free_bytes(dest) - ytconfig.MIN_FREE_BYTES) // 1024**2} MB")
    return got


def _fetch_under_floor(entries: list, dest: Path, clients, log_fn,
                       first_is_exempt: bool) -> tuple[dict, list]:
    """Fetch `entries` one at a time, re-reading real free space between each.

    Returns (got, attempted). Stops as soon as the next video would cross the floor, so
    what is left behind is simply not recorded and comes back next cycle.
    """
    got: dict = {}
    attempted: list = []
    for i, e in enumerate(entries):
        headroom = free_bytes(dest) - ytconfig.MIN_FREE_BYTES
        # The first video of a fresh wave is exempt: plan_wave already admitted it against
        # real headroom, and re-judging it here with a safety factor on top could refuse
        # the only video that fits and stall the queue forever. The fallback pass gets no
        # such exemption -- its videos were already attempted once this cycle.
        if i or not first_is_exempt:
            est = e.get("filesize") or ytconfig.ASSUMED_VIDEO_BYTES
            if headroom < est * ytconfig.SPACE_SAFETY_FACTOR:
                log_fn(f"  holding the {ytconfig.MIN_FREE_BYTES // 1024**3} GiB floor "
                       f"(headroom {headroom // 1024**2} MB); "
                       f"{len(entries) - i} video(s) deferred to a later cycle")
                break
        # The check above spends an ESTIMATE; the cap below spends the real thing. It
        # aborts a video whose true size overran its estimate mid-fetch, rather than
        # discovering it afterwards from the wrong side of the floor. An aborted video is
        # left unrecorded and retried next cycle, once the uploader has freed room.
        #
        # Halved because --max-filesize applies PER FORMAT, not to the merged result
        # (verified against yt-dlp 2026.06.09: a bv*+ba selection aborts the video and
        # audio streams on separate checks), and the parts coexist with the merged output
        # during the remux, so peak usage is about twice the final file.
        cap = max(0, headroom // 2)
        # A zero cap must stop the wave outright rather than fall through: the exempt
        # first video skips the estimate check above, and 0 reads as falsy where the flag
        # is built, which would omit it and leave the download uncapped.
        if cap <= 0:
            log_fn(f"  at or below the {ytconfig.MIN_FREE_BYTES // 1024**3} GiB floor "
                   f"(headroom {headroom // 1024**2} MB); nothing downloaded this cycle")
            break
        attempted.append(e)
        got.update(_download_videos_pass([e], dest, clients, log_fn,
                                         announce=False, max_filesize=cap))
    return got, attempted


def _download_videos_pass(wave: list, dest: Path, clients, log_fn, announce=True,
                          max_filesize=None) -> dict:
    temp = dest / ".tmp"
    temp.mkdir(parents=True, exist_ok=True)
    urls = [e["url"] for e in wave]

    cmd = [
        ytconfig.YT_DLP, *_base_args(clients),
        *(["--max-filesize", str(int(max_filesize))] if max_filesize else []),
        "-f", ytconfig.FORMAT,
        "--merge-output-format", ytconfig.CONTAINER,
        "--remux-video", ytconfig.CONTAINER,
        "--write-info-json",
        "--no-write-playlist-metafiles",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "--embed-subs",
        "--embed-metadata",
        "--embed-chapters",
        "--sub-langs", ytconfig.SUB_LANGS,
        "--paths", f"temp:{temp}",
        "-o", str(dest / "%(id)s.%(ext)s"),
        *urls,
    ]
    if announce:
        log_fn(f"  downloading {len(urls)} video(s)...")
    proc = subprocess.run(cmd, env=_env(), capture_output=True, text=True)
    if proc.returncode != 0:
        # Expected on a partial wave (`--ignore-errors`); what landed is still usable.
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        log_fn("  yt-dlp reported errors: " + " | ".join(t[:160] for t in tail))

    shutil.rmtree(temp, ignore_errors=True)
    return collect(dest, [e["id"] for e in wave])


def collect(dest: Path, video_ids: list) -> dict:
    """Pair each downloaded media file with its `.info.json` sidecar.

    A media file with no sidecar (or vice versa) is an incomplete download and is
    ignored, so a half-fetched video is retried next cycle instead of being filed with
    no metadata.
    """
    out = {}
    for vid in video_ids:
        media = None
        for ext in (ytconfig.CONTAINER, "mp4", "mkv", "webm"):
            cand = dest / f"{vid}.{ext}"
            if cand.exists() and cand.stat().st_size > 0:
                media = cand
                break
        info_path = dest / f"{vid}.info.json"
        if media is None or not info_path.exists():
            continue
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        thumb = dest / f"{vid}.jpg"
        out[vid] = {"path": media, "info": info,
                    "thumb": thumb if thumb.exists() else None,
                    "info_path": info_path,
                    "size": media.stat().st_size}
    return out


def download_tracks(tracks: list, dest: Path, log_fn=print) -> dict:
    """Extract audio for a batch of track entries. Returns {video_id: path}.

    Matches what is already in the music folders: an `.mp3` with the thumbnail embedded as
    cover art, so the file carries its own artwork wherever it is played.
    """
    dest.mkdir(parents=True, exist_ok=True)
    got = _download_tracks_pass(tracks, dest, None, log_fn)
    missing = [t for t in tracks if t["id"] not in got]
    for client in ytconfig.PLAYER_CLIENTS_FALLBACK:
        if not missing:
            break
        log_fn(f"  retrying {len(missing)} track(s) with player_client={client}")
        got.update(_download_tracks_pass(missing, dest, client, log_fn))
        missing = [t for t in missing if t["id"] not in got]
    return got


def _download_tracks_pass(tracks: list, dest: Path, clients, log_fn) -> dict:
    temp = dest / ".tmp"
    temp.mkdir(parents=True, exist_ok=True)
    cmd = [
        ytconfig.YT_DLP, *_base_args(clients),
        "-f", "ba/b",
        "--extract-audio",
        "--audio-format", ytconfig.AUDIO_FORMAT,
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--embed-metadata",
        "--paths", f"temp:{temp}",
        "-o", str(dest / "%(id)s.%(ext)s"),
        *[t["url"] for t in tracks],
    ]
    log_fn(f"  extracting {len(tracks)} audio track(s)...")
    proc = subprocess.run(cmd, env=_env(), capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        log_fn("  yt-dlp reported errors: " + " | ".join(t[:160] for t in tail))
    shutil.rmtree(temp, ignore_errors=True)

    out = {}
    for t in tracks:
        p = dest / f"{t['id']}.{ytconfig.AUDIO_FORMAT}"
        if p.exists() and p.stat().st_size > 0:
            out[t["id"]] = p
    return out


def cleanup(path: Path) -> None:
    """Drop a finished wave's scratch dir. Anything still in it is either already
    hardlinked into the library or a failed partial that will be re-fetched, so
    discarding it is always safe."""
    shutil.rmtree(path, ignore_errors=True)


def prune_incoming() -> None:
    """Remove leftover wave dirs from a previous run that was killed mid-wave. Nothing
    in the scratch dir is authoritative -- the ledger is -- so a leftover is junk."""
    if not ytconfig.INCOMING_DIR.is_dir():
        return
    for child in ytconfig.INCOMING_DIR.iterdir():
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)


def probe_duration(path: Path) -> int:
    """Runtime in seconds via ffprobe, or 0. Used to sanity-check a download against
    the duration the playlist listing advertised."""
    try:
        out = subprocess.run(
            [ytconfig.FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            env=_env(), capture_output=True, text=True, timeout=60,
        ).stdout
        return int(float(json.loads(out).get("format", {}).get("duration", 0)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError,
            subprocess.TimeoutExpired):
        return 0
