"""Discovery: which playlists to ingest, and every video in them.

NOTHING HERE AUTHENTICATES. That is the whole design, and it is what makes this
maintenance-free.

Every tracked playlist is PUBLIC, so it enumerates signed-out, and public videos download
signed-out. Account credentials bought exactly two things -- reading which playlists had
been saved, and Liked videos -- and cost a session Google revoked twice under the request
volume (at 40 minutes, then 2 hours), each time needing a manual browser export. Moving the
one account-only list into a PUBLIC "Soundtracks" playlist removed the last thing cookies
were for, so they are gone entirely: no cookie file, no expiry, no alert, no chore.

The trade, stated plainly: a playlist newly saved on YouTube is no longer discovered by
itself, because that discovery is the one thing that needed the account. Register it once:

    python3 youtube_sync.py --add-playlist <url>

and it is remembered forever.

So `state/playlists.json` is the source of truth for WHICH playlists to ingest, and YouTube
remains the source of truth for what is IN them -- add a video to any registered playlist
and the next cycle picks it up, with no credentials involved.

Everything here is a `yt-dlp --flat-playlist` call: metadata only, no media, no per-video
extraction, so enumerating every playlist is cheap.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import ledger
import ytconfig


def build_env() -> dict:
    env = os.environ.copy()
    existing = env.get("PATH", "")
    env["PATH"] = ":".join(ytconfig.EXTRA_PATH + ([existing] if existing else []))
    return env


def net_args() -> list:
    """Network flags common to every yt-dlp call.

    `--force-ipv4` closes the other half of the split-tunnel (Media-Syncer's
    `scripts/split_tunnel.sh`). That daemon pins Google's published IPv4 prefixes
    to the physical gateway so YouTube is reached from the stable home IP rather than a
    rotating Mullvad datacenter exit. Only IPv4 is pinned, because pinning v6 needs a
    physical-interface v6 router whose discovery Tailscale owns, and a half-working v6 route
    is worse than none. So the v6 path is closed HERE: forcing IPv4 means this daemon can
    never reach Google over IPv6 and slip back out through the exit node, silently undoing
    the arrangement. Cheap, local, and needs no root.
    """
    return (["--force-ipv4"] if ytconfig.FORCE_IPV4 else []) + extractor_args()


def extractor_args(extra: str = "", clients: str | None = None) -> list:
    """The youtube extractor-args for every call, composed into ONE flag.

    Composed rather than appended as several `--extractor-args` because they all target the
    same extractor, and passing that flag twice for one extractor is not a reliable merge --
    the second can simply replace the first, silently dropping whichever setting was in the
    other. So every youtube setting is joined with `;` into a single argument.

    `player_client` is the PO-token 403 fix (§ ytconfig.PLAYER_CLIENTS); `extra` carries
    call-specific settings such as `player_skip=webpage` for flat listing.
    """
    parts = []
    pc = ytconfig.PLAYER_CLIENTS if clients is None else clients
    if pc:
        parts.append(f"player_client={pc}")
    if extra:
        parts.append(extra)
    return ["--extractor-args", "youtube:" + ";".join(parts)] if parts else []


def _run(args: list, timeout: int = 600) -> tuple[int, str, str]:
    proc = subprocess.run([ytconfig.YT_DLP, *args], env=build_env(),
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _flat_json(url: str, timeout: int = 600) -> dict:
    """One flat `-J` dump for a playlist URL. Signed-out, always."""
    rc, out, err = _run([
        *(["--force-ipv4"] if ytconfig.FORCE_IPV4 else []),
        *extractor_args("player_skip=webpage"),
        "--flat-playlist", "--ignore-config", "--no-warnings",
        "-J", url,
    ], timeout=timeout)
    if rc != 0 or not out.strip():
        raise RuntimeError((err.strip() or f"yt-dlp exited {rc}")[:400])
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp emitted unparseable JSON for {url}: {exc}") from exc


# --- the registry: which playlists to ingest ---------------------------------

_LIST_ID_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")


def playlist_id_from_url(url: str) -> str:
    m = _LIST_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _excluded(pid: str, title: str) -> str:
    """Reason this playlist is refused, or "" to keep it. Watch Later and History are
    excluded by id AND by name -- both would flood the library, and neither is something
    that was deliberately kept."""
    if pid in ytconfig.SKIP_PLAYLIST_IDS:
        return f"excluded id {pid}"
    if title.strip().lower() in ytconfig.SKIP_PLAYLIST_TITLES:
        return f"excluded title {title!r}"
    if not pid:
        return "no playlist id"
    return ""


def load_registry() -> dict:
    reg = ledger._load(ytconfig.PLAYLISTS_FILE, {})
    return reg if isinstance(reg, dict) else {}


def save_registry(reg: dict) -> None:
    ledger._save(ytconfig.PLAYLISTS_FILE, reg)


def register_playlist(url: str, log_fn=print) -> bool:
    """Add a playlist to the registry, verifying it is reachable signed-out FIRST.

    The verification is the point. Registering something that needs an account, or a dead
    link, would otherwise fail silently every cycle forever. A playlist that cannot be read
    without credentials cannot be ingested by this daemon at all, so it is refused here
    with an explanation rather than accepted and quietly broken.
    """
    pid = playlist_id_from_url(url) or url.strip()
    if not pid:
        log_fn(f"  ! not a playlist URL: {url!r}")
        return False
    full = url if "://" in url else f"https://www.youtube.com/playlist?list={pid}"
    try:
        data = _flat_json(full, timeout=180)
    except Exception as exc:                                       # noqa: BLE001
        log_fn(f"  ! cannot read {pid} without an account ({str(exc)[:120]}). This daemon "
               f"never authenticates, so the playlist must be PUBLIC or unlisted.")
        return False
    title = str(data.get("title") or pid)
    why = _excluded(pid, title)
    if why:
        log_fn(f"  ! refusing {title!r}: {why}")
        return False
    n = len([e for e in (data.get("entries") or []) if e])

    reg = load_registry()
    existed = pid in reg
    reg[pid] = {
        "id": pid, "title": title,
        "url": f"https://www.youtube.com/playlist?list={pid}",
        "source": reg.get(pid, {}).get("source", "registered"),
        "missing": 0,
        "first_seen": reg.get(pid, {}).get("first_seen") or ytconfig.log_stamp(),
        "last_seen": ytconfig.log_stamp(),
    }
    save_registry(reg)
    log_fn(f"  {'updated' if existed else 'registered'}: {title!r} ({n} videos, id {pid})")
    return True


def forget_playlist(pid: str, log_fn=print) -> bool:
    reg = load_registry()
    rec = reg.pop(pid, None)
    if rec is None:
        log_fn(f"  ! no registered playlist with id {pid!r}")
        return False
    save_registry(reg)
    log_fn(f"  forgot {str(rec.get('title'))!r} ({pid})")
    return True


def discover_playlists(log_fn=print) -> list:
    """Every playlist to ingest, from the registry. No network call, no credentials.

    Reachability is proven at REGISTER time rather than re-litigated every cycle, so a
    transient network blip cannot drop a playlist -- and `playlist_entries` already treats
    an unreadable playlist as a skip rather than a failure.
    """
    reg = load_registry()
    out = []
    for pid, p in reg.items():
        if _excluded(pid, str(p.get("title") or "")):
            continue
        out.append({"id": pid, "title": p.get("title") or pid,
                    "url": p.get("url") or f"https://www.youtube.com/playlist?list={pid}",
                    "source": p.get("source", "registered")})
    if not out:
        log_fn("  ! no playlists registered -- add one with --add-playlist <url>")
    return sorted(out, key=lambda p: p["title"].lower())


# --- entries within a playlist ------------------------------------------------

def playlist_entries(playlist: dict, log_fn=print) -> list:
    """Every video in one playlist, in playlist order, as
    [{id, title, duration, url, filesize, position}].

    Unavailable entries (deleted, private, region-blocked) are dropped here rather than
    failing the playlist: a playlist accumulates dead videos over years, and one of them
    must never stop the live ones from syncing.
    """
    try:
        data = _flat_json(playlist["url"])
    except Exception as exc:                                       # noqa: BLE001
        log_fn(f"  ! could not read playlist {playlist['title']!r}: {exc}")
        return []

    out = []
    for i, entry in enumerate(data.get("entries") or [], start=1):
        if not entry:
            continue
        vid = str(entry.get("id") or "").strip()
        if not vid:
            continue
        title = str(entry.get("title") or "").strip()
        # yt-dlp reports these placeholder titles for entries it cannot access.
        if title.lower() in ("[deleted video]", "[private video]", "[unavailable video]", ""):
            continue
        duration = entry.get("duration")
        out.append({
            "id": vid,
            "title": title,
            "duration": int(duration) if isinstance(duration, (int, float)) else None,
            "url": entry.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "filesize": entry.get("filesize") or entry.get("filesize_approx"),
            "position": i,
            "playlist_id": playlist["id"],
            "playlist_title": data.get("title") or playlist["title"],
            "uploader": entry.get("uploader") or entry.get("channel") or "",
        })

    # Keep the registry's title in step with the live one, so a renamed playlist does not
    # keep showing its old name in --status forever.
    live_title = data.get("title")
    if live_title and live_title != playlist.get("title"):
        playlist["title"] = live_title
        reg = load_registry()
        if playlist["id"] in reg:
            reg[playlist["id"]]["title"] = live_title
            save_registry(reg)

    if len(out) > ytconfig.LARGE_PLAYLIST_WARN:
        log_fn(f"  * {playlist['title']!r} is large ({len(out)} videos); it will sync "
               f"across several cycles in waves")
    return out
