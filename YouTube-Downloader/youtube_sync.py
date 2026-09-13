#!/usr/bin/env python3
"""YouTube ingest -- everything you have saved on YouTube, folded into the library.

This is a SOURCE for Torrent-Ingest's pipeline, not a separate downloader. A YouTube
video reaches the library through the identical door a torrent does:

    registered playlists (all public -- no account, no cookies)
        -> enumerate videos, drop the ones already handled (ledger, keyed by video id)
        -> split off the audio playlists    -> iCloud Soundtracks/, Drive Music/
        -> download a SPACE-BOUNDED WAVE of the rest onto the Downloads volume
        -> a headless AI run routes each video and WRITES its metadata
        -> library.validate_plan  (Torrent-Ingest)
        -> library.apply_plan     (dot-staging dir, atomic publish, locked .nfo)
        -> library.verify_applied
        -> free the wave, record the ledger, rescan Jellyfin, extend playlists
        -> next wave

What that buys, all of it inherited rather than reimplemented: the same show/movie
naming, write-once safety (a pre-existing file is never overwritten), atomic publish
(Media-Syncer never sees a partial file), locked `.nfo` for content no scraper can
identify, the MEGA upload and eviction that follow, and the curated-playlist
auto-extend.

    python3 youtube_sync.py                 # one full cycle (what launchd runs)
    python3 youtube_sync.py --daemon        # loop forever
    python3 youtube_sync.py --status        # what is discovered / done / parked
    python3 youtube_sync.py --dry-run       # discover + plan waves, download nothing
    python3 youtube_sync.py --retry-failed  # release parked failures and exit
    python3 youtube_sync.py --playlist ID   # only this playlist, this run
    python3 youtube_sync.py --add-playlist <url>     # register a public playlist
    python3 youtube_sync.py --forget-playlist <id>   # stop ingesting one
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import ytconfig                     # our settings (see its docstring re: the name)
import classify
import discover
import fetch
import identify_yt
import ledger as ledger_mod
import preflight
import soundtracks

import library                      # Torrent-Ingest: plan validation / apply / verify


# --- logging -----------------------------------------------------------------

def log(msg: str) -> None:
    line = f"[{ytconfig.log_stamp()}] {msg}"
    print(line, flush=True)
    try:
        with open(ytconfig.LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --- locking -----------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_lock() -> bool:
    ytconfig.STATE_DIR.mkdir(parents=True, exist_ok=True)
    if ytconfig.LOCK_FILE.exists():
        try:
            pid = int(ytconfig.LOCK_FILE.read_text().strip())
        except (ValueError, OSError):
            pid = None
        if pid and _pid_alive(pid):
            log(f"Another run (pid {pid}) holds the lock. Exiting.")
            return False
        log("Stale lock found; reclaiming.")
    ytconfig.LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        ytconfig.LOCK_FILE.unlink()
    except OSError:
        pass


# --- Jellyfin ----------------------------------------------------------------

def jellyfin_rescan() -> None:
    """Ask Jellyfin to pick up what we just placed. Same call Torrent-Ingest makes;
    inlined rather than imported because Torrent-Ingest's `ingest` module pulls in the
    qBittorrent client, which is irrelevant here and not always installed."""
    url = getattr(library.config, "JELLYFIN_URL", "")
    if not url:
        return
    try:
        import requests
        requests.post(f"{url.rstrip('/')}/Library/Refresh",
                      params={"api_key": getattr(library.config, "JELLYFIN_API_KEY", "")},
                      timeout=10)
        log("  triggered Jellyfin library rescan")
    except Exception as exc:                                       # noqa: BLE001
        log(f"  Jellyfin rescan skipped: {exc}")


def extend_playlists(dst_rels: list) -> None:
    """Let the curated-playlist watcher consider newly placed episodes, exactly as it
    does after a torrent ingest -- so a YouTube-sourced episode of an auto-extended show
    is judged and appended like any other."""
    shows = [r for r in dst_rels if r.startswith("Shows/")]
    if not shows:
        return
    try:
        import playlist_watch
        playlist_watch.consider_new_episodes(shows, log_fn=lambda m: log(f"  {m}"))
    except Exception as exc:                                       # noqa: BLE001
        log(f"  playlist auto-extend skipped: {exc}")


# --- artwork -----------------------------------------------------------------

def place_artwork(applied_map: dict, downloaded: dict, series: dict | None) -> None:
    """Put each video's thumbnail next to its placed file, and seed show-level art.

    Not part of the placement plan on purpose: the plan carries only true media (the
    files Media-Syncer replicates to the pool), and artwork is a locally-regenerable
    sidecar -- Torrent-Ingest's own metadata backup explicitly drops `-thumb.jpg` for
    that reason. A YouTube video's thumbnail IS its only poster, though, so it is worth
    writing; it is just not worth uploading.
    """
    for vid, rel in applied_map.items():
        thumb = downloaded.get(vid, {}).get("thumb")
        if not thumb or not Path(thumb).exists():
            continue
        dst = ytconfig.MEDIA_ROOT / rel
        try:
            if rel.startswith("Shows/"):
                target = dst.with_name(f"{dst.stem}-thumb.jpg")
            else:                                   # a film's poster sits beside it
                target = dst.with_name(f"{dst.stem}-poster.jpg")
            if not target.exists():
                shutil.copy2(thumb, target)
        except OSError:
            continue

    if not series:
        return
    show_dir = ytconfig.MEDIA_ROOT / series["show_rel"]
    first = next((downloaded[v]["thumb"] for v, r in applied_map.items()
                  if r.startswith(series["show_rel"] + "/")
                  and downloaded.get(v, {}).get("thumb")), None)
    if not first:
        return
    for art in ("poster.jpg", "folder.jpg", "backdrop.jpg",
                f"season{ytconfig.SEASON:02d}-poster.jpg"):
        dest = show_dir / art
        try:
            if not dest.exists():
                shutil.copy2(first, dest)
        except OSError:
            pass


def seed_series_nfo(series: dict) -> None:
    """Write the show's `tvshow.nfo` if it has none.

    Deliberately NOT the unlocked, id-pinned seed Torrent-Ingest writes: that one exists
    to stop Jellyfin's scraper merging a sequel onto its parent, and it works by pinning
    a provider id. A playlist-derived show has no provider id and no provider entry, so
    leaving the series unlocked means Jellyfin title-searches it and scrapes some real TV
    series' plot and poster onto it. Locking our own description is the only way it shows
    up as what it actually is. Episodes are locked by `apply_plan` for the same reason.
    """
    show_dir = ytconfig.MEDIA_ROOT / series["show_rel"]
    nfo = show_dir / "tvshow.nfo"
    if nfo.exists():
        return
    from xml.sax.saxutils import escape
    plot = series.get("plot") or ""
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<tvshow>",
        f"  <title>{escape(str(series['title']))}</title>",
        f"  <showtitle>{escape(str(series['title']))}</showtitle>",
    ]
    if series.get("year"):
        parts.append(f"  <year>{int(series['year'])}</year>")
    if plot:
        parts.append(f"  <plot>{escape(plot)}</plot>")
        parts.append(f"  <outline>{escape(plot)}</outline>")
    if series.get("studio"):
        parts.append(f"  <studio>{escape(str(series['studio']))}</studio>")
    for g in series.get("genres") or ():
        parts.append(f"  <genre>{escape(str(g))}</genre>")
    parts.append("  <genre>YouTube</genre>")
    parts.append("  <lockdata>true</lockdata>")
    parts.append("</tvshow>")
    library._atomic_write(nfo, "\n".join(parts) + "\n")


# --- one batch ---------------------------------------------------------------

def process_batch(playlist: dict, batch: list, downloaded: dict,
                  led: ledger_mod.Ledger) -> list:
    """Route, place and verify one batch. Returns the library-relative paths placed.

    A failure anywhere here fails only THIS batch: its videos are recorded as failed
    (retried on later cycles up to a cap) and the run moves on, so one unroutable video
    never stalls the whole library.
    """
    pinned = led.show_for(playlist["id"])
    next_ep = led.next_episode(playlist["id"])
    ids = [e["id"] for e in batch]

    try:
        routing, rationale = identify_yt.run_identify(
            playlist, batch, downloaded, pinned, next_ep, log_fn=log)
        plan, per_video, series = identify_yt.build_plan(
            routing, batch, downloaded, pinned, next_ep, log_fn=log)
    except identify_yt.IdentifyUnavailable:
        # Usage window exhausted: the batch was never routed. Propagate WITHOUT touching the
        # ledger -- mark_failed_batch() spends one of a capped number of retries, and past
        # that cap the videos are abandoned. The caller stops the cycle; the next one
        # re-routes this batch untouched.
        raise
    except Exception as exc:                                       # noqa: BLE001
        log(f"  ! routing failed: {exc}")
        led.mark_failed_batch(ids, f"identify: {exc}")
        return []

    if rationale:
        log(f"  rationale: {rationale.strip().splitlines()[0][:200]}")

    # Skips are a real outcome: record them so they are never fetched again.
    for vid, rec in per_video.items():
        if rec.get("route") == "skip":
            log(f"  - skip {vid}: {rec['reason'][:120]}")
            led.record(vid, "skipped", reason=rec["reason"])

    if not plan:
        return []

    try:
        # Validation is the single gate in front of apply, exactly as for a torrent. The
        # content root is the wave's scratch dir, so a plan can only ever reference files
        # this wave actually downloaded.
        plan = library.validate_plan(plan, str(downloaded[ids[0]]["path"].parent))
        # Seed the series .nfo BEFORE apply, not after. `apply_plan` writes its own
        # UNLOCKED, id-pinned tvshow.nfo seed, and neither writer clobbers an existing
        # file -- so whichever runs first wins. For a playlist-derived show ours must
        # win (there is no provider id to pin, and an unlocked series gets title-searched
        # onto some real TV show). Getting in first also makes apply's seed a no-op
        # rather than a conflict: it finds the file present and only fills in provider
        # ids, of which this plan deliberately has none.
        if series:
            seed_series_nfo(series)
        applied = library.apply_plan(plan, f"youtube-{playlist['id']}-{batch[0]['id']}")
        ok, msg = library.verify_applied(applied)
        if not ok:
            raise RuntimeError(f"verification failed: {msg}")
    except Exception as exc:                                       # noqa: BLE001
        log(f"  ! placement failed: {exc}")
        led.mark_failed_batch([v for v, r in per_video.items() if r.get("route") != "skip"],
                              f"placement: {exc}")
        return []

    placed_rels, applied_map = [], {}
    for vid, rec in per_video.items():
        if rec.get("route") == "skip":
            continue
        rel = rec["dst_rel"]
        placed_rels.append(rel)
        applied_map[vid] = rel
        led.record(vid, "placed", dst_rel=rel, route=rec["route"],
                   playlist_id=playlist["id"], title=next(
                       (e["title"] for e in batch if e["id"] == vid), ""))
        log(f"  . {rel}")

    if series:
        led.pin_show(playlist["id"], playlist.get("title", ""),
                     series["show_rel"], series["year"], series["title"])
        led.advance_episode(playlist["id"], series["last_episode"])
    place_artwork(applied_map, downloaded, series)
    led.save()
    return placed_rels


# --- one playlist ------------------------------------------------------------

def process_playlist(playlist: dict, led: ledger_mod.Ledger, dry_run: bool) -> tuple[list, int]:
    """Ingest one playlist's new items. Returns (placed_video_rels, track_count).

    Audio tracks are counted separately from library videos: they file into a music folder
    (iCloud Soundtracks/, Drive Music/) rather than the library, and the cycle's summary
    must reflect both so a run that only placed tracks does not read as "nothing new".
    """
    entries = discover.playlist_entries(playlist, log_fn=log)
    if not entries:
        return [], 0

    pending = [e for e in entries if not led.is_done(e["id"])]
    if not pending:
        return [], 0
    log(f"[{playlist['title']}] {len(pending)} new of {len(entries)} video(s)")

    # Short audio tracks leave the pipeline here -- they are not library media.
    videos, tracks, duplicates = classify.split(pending, log_fn=log)

    # A track already in its destination folder under any name is recorded and never
    # fetched. Doing this BEFORE the download saves the bandwidth, and recording it means
    # it is never reconsidered -- the alternative is re-judging the same songs every cycle.
    for d in duplicates:
        where = Path(d["audio_dir"]).name
        log(f"  = already have {d['title'][:60]!r} as {d['duplicate_of']!r}")
        led.record(d["id"], "skipped", reason=f"already in {where} as {d['duplicate_of']}",
                   title=d["title"])
    if duplicates:
        led.save()

    ntracks = 0
    if tracks and not dry_run:
        track_dir = ytconfig.INCOMING_DIR / "tracks"
        got = fetch.download_tracks(tracks, track_dir, log_fn=log)
        for t in tracks:
            src = got.get(t["id"])
            if src is None:
                led.record(t["id"], "failed", reason="audio extraction produced no file")
                continue
            audio_dir = Path(t["audio_dir"])
            try:
                dst = soundtracks.file_track(src, t["track_title"], audio_dir)
            except OSError as exc:
                log(f"  ! could not file track {t['track_title']!r}: {exc}")
                led.record(t["id"], "failed", reason=f"audio: {exc}")
                continue
            log(f"  ~ {audio_dir.name}/{dst.name}")
            led.record(t["id"], "soundtrack", dst=str(dst), title=t["title"])
            ntracks += 1
        fetch.cleanup(track_dir)
        led.save()
    elif tracks:
        log(f"  DRY-RUN: {len(tracks)} track(s) would go to "
            f"{Path(tracks[0]['audio_dir']).name}/ (e.g. {tracks[0]['track_title']!r})")

    placed = []
    remaining = list(videos)
    while remaining:
        wave, oversized = fetch.plan_wave(remaining, log_fn=log)
        for e in oversized:
            log(f"  - skip {e['id']}: larger than the {ytconfig.MAX_VIDEO_BYTES // 1024**3} GB "
                f"per-video ceiling")
            led.record(e["id"], "skipped", reason="exceeds MAX_VIDEO_BYTES",
                       title=e["title"])
        if oversized:
            led.save()
        remaining = [e for e in remaining if e not in wave and e not in oversized]
        if not wave:
            if remaining:
                log(f"  {len(remaining)} video(s) deferred to a later cycle "
                    f"(disk budget)")
            break
        if dry_run:
            log(f"  DRY-RUN: would download and place {len(wave)} video(s)")
            break

        wave_dir = ytconfig.INCOMING_DIR / f"{playlist['id']}-{wave[0]['id']}"
        try:
            downloaded = fetch.download_videos(wave, wave_dir, log_fn=log)
            missing = [e for e in wave if e["id"] not in downloaded]
            for e in missing:
                log(f"  ! no usable download for {e['id']} ({e['title'][:60]!r})")
                led.record(e["id"], "failed", reason="download produced no usable file",
                           title=e["title"])
            got = [e for e in wave if e["id"] in downloaded]
            if not got:
                led.save()
                continue

            # Split the wave into identify-sized batches so one enormous run can't
            # time out repeatedly and starve everything behind it.
            n = ytconfig.IDENTIFY_BATCH_SIZE
            for i in range(0, len(got), n):
                batch = got[i:i + n]
                placed += process_batch(playlist, batch, downloaded, led)
        finally:
            # Always free the wave. Anything still here is either already hardlinked
            # into the library or a failure that re-downloads next cycle.
            fetch.cleanup(wave_dir)
            led.save()
    return placed, ntracks


# --- cycle -------------------------------------------------------------------

def cycle(dry_run: bool = False, only: str = "") -> int:
    if not ytconfig.MEDIA_ROOT.is_dir():
        log(f"Library root not present: {ytconfig.MEDIA_ROOT}. Skipping this cycle.")
        return 1

    # Assert the cross-repo couplings before touching the network. Each of these fails
    # quietly and gets blamed on the wrong layer if unchecked (§ preflight.py), and a
    # broken plan API means every plan this cycle would be rejected anyway -- so failing
    # here costs nothing and saves a ledger full of misleading failures.
    if not preflight.assert_ok(log_fn=log):
        return 1
    # Do not put a live YouTube session on the VPN exit. On a reboot the split-tunnel
    # daemon (boot) can still be waiting for DHCP while this agent (login) already fires,
    # and authenticating from a rotating Mullvad datacenter IP does not get captcha'd --
    # it gets the session REVOKED, costing a manual re-export. Deferring costs one hourly
    # cycle and self-heals, so it is the obvious side to err on.
    if ytconfig.REQUIRE_DIRECT_EGRESS:
        egress = _split_tunnel_state()
        if egress.startswith("NOT ACTIVE"):
            log("Google traffic is still routed through the VPN tunnel; deferring this "
                "cycle rather than risking the YouTube session.")
            log(f"  {egress}")
            log("  (this resolves itself once the split-tunnel daemon re-asserts; set "
                "YOUTUBE_REQUIRE_DIRECT_EGRESS=0 to override)")
            return 0
        log(f"egress: {egress}")


    led = ledger_mod.Ledger()
    fetch.prune_incoming()

    log("=== registered playlists ===")
    playlists = discover.discover_playlists(log_fn=log)

    if only:
        playlists = [p for p in playlists if p["id"] == only]
        if not playlists:
            log(f"No registered playlist with id {only!r}.")
            return 1
    if not playlists:
        log("No playlists registered. Add one with --add-playlist <url>.")
        return 0

    log(f"{len(playlists)} playlist(s): "
        + ", ".join(f"{p['title']}" for p in playlists[:8])
        + (f" ... +{len(playlists) - 8} more" if len(playlists) > 8 else ""))

    placed = []
    ntracks = 0
    for p in playlists:
        try:
            pv, nt = process_playlist(p, led, dry_run)
            placed += pv
            ntracks += nt
        except identify_yt.IdentifyUnavailable as exc:
            # Stop the whole cycle, not just this playlist: every remaining playlist would
            # hit the same closed door, and each one that tries first downloads its wave and
            # then discards it in the `finally` cleanup -- pure bandwidth burn. The ledger is
            # untouched, so everything re-routes on the next cycle.
            log(f"  ! identify API unavailable; ending this cycle early ({exc})")
            break
        except Exception as exc:                                   # noqa: BLE001
            log(f"  ! error on playlist {p['title']!r}: {exc!r}")
    led.save()

    if placed:
        log(f"=== placed {len(placed)} file(s)"
            + (f", {ntracks} audio track(s)" if ntracks else "") + " ===")
        jellyfin_rescan()
        extend_playlists(placed)
    elif ntracks:
        log(f"=== placed {ntracks} audio track(s) ===")
    else:
        log("=== nothing new to place ===")
    return 0


# --- status ------------------------------------------------------------------

# A Google frontend address inside a prefix the split-tunnel pins (142.250.0.0/15 is in
# Google's published goog.json). Asking the kernel which interface it would leave by is
# the only honest test of whether the split-tunnel is actually in force -- the daemon
# being installed proves nothing if Tailscale has since rewritten the table.
_GOOGLE_PROBE_IP = "142.250.72.14"


def _split_tunnel_state() -> str:
    """Whether YouTube traffic currently leaves via the physical interface or the VPN."""
    try:
        out = subprocess.run(["/sbin/route", "-n", "get", _GOOGLE_PROBE_IP],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    iface = ""
    for line in out.splitlines():
        if "interface:" in line:
            iface = line.split(":", 1)[1].strip()
            break
    if not iface:
        return "unknown"
    if iface.startswith("utun"):
        return (f"NOT ACTIVE -- Google exits via {iface} (the VPN tunnel). An "
                f"authenticated YouTube session on a rotating Mullvad exit gets REVOKED; "
                f"install the split-tunnel daemon (Media-Syncer, "
                f"scripts/split_tunnel.sh)")
    return f"active -- Google exits via {iface} (physical, stable home IP)"


def print_status() -> None:
    led = ledger_mod.Ledger()
    counts = led.counts()
    reg = discover.load_registry()
    print(f"library root:  {ytconfig.MEDIA_ROOT}")
    for i, (pid, d) in enumerate(ytconfig.AUDIO_PLAYLIST_DIRS.items()):
        # Reachability, not just the path: a cloud folder that has not mounted is the one
        # failure mode of this route, and it is invisible unless something says so.
        state = "ok" if d.is_dir() else ("MISSING -- not mounted?" if not d.parent.is_dir()
                                         else "not created yet")
        label = "audio folders:" if i == 0 else " " * 14
        title = str(reg.get(pid, {}).get("title") or pid)
        print(f"{label} {title!r} -> {d}  [{state}]")
    print(f"egress:        yt-dlp forced to IPv4: {ytconfig.FORCE_IPV4}   "
          f"(Google split-tunnel: {_split_tunnel_state()})")
    print(f"wave budget:   {ytconfig.wave_bytes() / 1024**3:.1f} GB   "
          f"free on Downloads: {fetch.free_bytes(ytconfig.TI.DOWNLOADS_DIR) / 1024**3:.1f} GB")
    print(f"\nvideos seen: {sum(counts.values())}  "
          + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    if led.shows:
        print(f"\nplaylist -> show ({len(led.shows)}):")
        for pid, rec in sorted(led.shows.items(), key=lambda t: t[1]["show_rel"]):
            print(f"  {rec['show_rel']:<58} next S{ytconfig.SEASON:02d}E"
                  f"{led.next_episode(pid):02d}   <- {rec.get('playlist_title', pid)!r}")

    parked = led.parked()
    if parked:
        print(f"\nparked after {ledger_mod.FAIL_MAX_ATTEMPTS} failed attempts "
              f"({len(parked)}) -- release with --retry-failed:")
        for vid, rec in parked[:20]:
            print(f"  {vid}  {str(rec.get('reason', ''))[:110]}")

    if reg:
        print(f"\nregistered playlists: {len(reg)}  (all public; no account needed)")
        for pid, p in sorted(reg.items(), key=lambda t: str(t[1].get("title", "")).lower()):
            print(f"  {pid:<36} {p.get('title')}")
    else:
        print("\nno playlists registered -- add one with --add-playlist <url>")


# --- main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest your saved YouTube playlists into "
                                             "the Jellyfin library.")
    ap.add_argument("--daemon", action="store_true", help="loop forever instead of one cycle")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="discover and plan waves; download and place nothing")
    ap.add_argument("--retry-failed", action="store_true",
                    help="release parked failures so the next cycle retries them")
    ap.add_argument("--playlist", default="", metavar="ID",
                    help="restrict this run to one discovered playlist id")
    ap.add_argument("--preflight", action="store_true",
                    help="check the cross-repo dependencies and exit")
    ap.add_argument("--add-playlist", default="", metavar="URL",
                    help="register a PUBLIC playlist to ingest (verified, then remembered)")
    ap.add_argument("--forget-playlist", default="", metavar="ID",
                    help="stop ingesting a registered playlist")
    args = ap.parse_args()

    if args.preflight:
        return preflight.main()
    if args.add_playlist:
        return 0 if discover.register_playlist(args.add_playlist) else 1
    if args.forget_playlist:
        return 0 if discover.forget_playlist(args.forget_playlist) else 1
    if args.status:
        print_status()
        return 0
    if args.retry_failed:
        led = ledger_mod.Ledger()
        n = led.retry_failed()
        led.save()
        print(f"released {n} parked failure(s); they retry on the next cycle")
        return 0

    if not acquire_lock():
        return 0
    try:
        if not args.daemon:
            return cycle(args.dry_run, args.playlist)
        log(f"youtube_sync daemon up (cycle {ytconfig.POLL_INTERVAL_SEC}s)")
        while True:
            try:
                cycle(args.dry_run, args.playlist)
            except Exception as exc:                               # noqa: BLE001
                log(f"cycle error (continuing): {exc!r}")
            time.sleep(ytconfig.POLL_INTERVAL_SEC)
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
