"""Predictive pre-download daemon -- the "smart downloader".

On-demand streaming from the MEGA pool is too slow for a good comic/video experience,
so instead of streaming what you open, we PRE-DOWNLOAD what you are about to want into
the local cache and read it from disk. The only cost is a possibly-spotty FIRST play of
something never seen before; everything after that is local-disk fast.

Driven by what you actually watch/read:

  * Shows  -- Jellyfin knows exactly which episodes are played and when. For each recently
               active series we cache the next `PREDOWNLOAD_EPISODES_AHEAD` UNWATCHED
               episodes (a rolling window) and let watched ones be evicted.
  * Comics -- Jellyfin does not track these, so we use a universal signal: mediafs appends
               every interactive open to `PREDOWNLOAD_ACCESS_LOG`. Opening a volume caches
               the next `PREDOWNLOAD_VOLUMES_AHEAD` volumes in its folder.
  * Movies -- when you finish one we ask the AI (`scripts/ai.py`, one free-model completion)
               which related titles (sequels, same franchise) that ALSO EXIST in the library
               you're likely to want next, and cache up to `PREDOWNLOAD_MOVIES_AHEAD` of them.
               A true one-off predicts none.

Everything is bounded by a storage budget derived from free disk (`config.storage_plan`),
so it scales to any machine: keep 1/10 free as breathing room, reserve 1/6 of the rest for
a download "chunk", and dedicate the other 5/6 (~3/4 of the disk) to these pre-downloads.
Each cycle it (1) computes the desired set in priority order, (2) evicts cached media that
is watched / no-longer-predicted / cold to stay within budget (never the file you're
reading right now, nor anything still in the desired set), and (3) downloads the desired
files that aren't cached yet -- pausing whenever a real interactive stream is in flight so
a cold miss always beats the background fill.

    python3 -m scripts.predownload            # run the daemon
    python3 -m scripts.predownload --once      # one reconcile pass (for testing)
    python3 -m scripts.predownload --plan      # print the plan + desired set, download nothing
"""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

from . import ai
from . import config
from . import tier


# --- logging -----------------------------------------------------------------

def setup_logging():
    # Bounded like the sync log (see config.LOG_MAX_BYTES); the StreamHandler still feeds
    # launchd's Predownload.err, which is left unbounded on purpose.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s",
                        handlers=[RotatingFileHandler(config.SCRIPT_DIR.parent / "predownload.log",
                                                      maxBytes=config.LOG_MAX_BYTES,
                                                      backupCount=config.LOG_BACKUP_COUNT),
                                  logging.StreamHandler()])


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


# --- classification ----------------------------------------------------------

_SUBTITLE_EXTS = {".srt", ".ass"}


def _ext(rel: str) -> str:
    return os.path.splitext(rel)[1].lower()


def _is_comic(rel: str) -> bool:
    return _ext(rel) in config.COMICS_EXTENSIONS


def _is_movie(rel: str) -> bool:
    return rel.startswith("Movies/") and _ext(rel) in config.VIDEO_EXTENSIONS and _ext(rel) not in _SUBTITLE_EXTS


def _is_episode(rel: str) -> bool:
    return rel.startswith("Shows/") and _ext(rel) in config.VIDEO_EXTENSIONS and _ext(rel) not in _SUBTITLE_EXTS


def _is_media(rel: str) -> bool:
    return _ext(rel) in (config.MEDIAFS_PAYLOAD_EXTENSIONS - _SUBTITLE_EXTS)


# --- Jellyfin (video watch-state) --------------------------------------------

def _jf(path: str, **params) -> dict | list | None:
    params["api_key"] = config.JELLYFIN_API_KEY
    url = f"{config.JELLYFIN_URL}{path}?{urllib.parse.urlencode(params, doseq=True)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.load(r)
    except Exception as e:                                    # noqa: BLE001
        logging.debug(f"jellyfin {path} failed: {e}")
        return None


def _jf_user_id() -> str | None:
    users = _jf("/Users")
    if isinstance(users, list) and users:
        return users[0].get("Id")
    return None


def _path_to_rel(path: str) -> str | None:
    """A Jellyfin item Path (under the mediafs mount) -> inventory-relative key."""
    if not path:
        return None
    base = str(config.MEDIAFS_MOUNT).rstrip("/") + "/"
    return path[len(base):] if path.startswith(base) else None


def _watched_rels(uid: str) -> set:
    """Every played episode/movie as an inventory rel -- so we neither re-download nor
    protect what you've already finished."""
    out = set()
    data = _jf(f"/Users/{uid}/Items", IncludeItemTypes="Episode,Movie", Recursive="true",
               Filters="IsPlayed", Fields="Path", Limit=10000)
    for it in (data or {}).get("Items", []):
        rel = _path_to_rel(it.get("Path", ""))
        if rel:
            out.add(rel)
    return out


def _recent_series(uid: str) -> list:
    """Series ids, most-recently-played first (deduped)."""
    data = _jf(f"/Users/{uid}/Items", IncludeItemTypes="Episode", Recursive="true",
               Filters="IsPlayed", SortBy="DatePlayed", SortOrder="Descending",
               Limit=300, Fields="SeriesId,SeriesName")
    seen, out = set(), []
    for it in (data or {}).get("Items", []):
        sid = it.get("SeriesId")
        if sid and sid not in seen:
            seen.add(sid)
            out.append((sid, it.get("SeriesName")))
    return out


def _series_unwatched_ahead(uid: str, series_id: str, n: int) -> list:
    """The next `n` UNWATCHED episodes of a series, in airing order, as inventory rels."""
    data = _jf(f"/Shows/{series_id}/Episodes", userId=uid, Fields="Path,UserData")
    eps = (data or {}).get("Items", [])
    def key(e):
        return (e.get("ParentIndexNumber") or 0, e.get("IndexNumber") or 0)
    out = []
    for e in sorted(eps, key=key):
        if e.get("UserData", {}).get("Played"):
            continue
        rel = _path_to_rel(e.get("Path", ""))
        if rel:
            out.append(rel)
        if len(out) >= n:
            break
    return out


# --- Jellyfin (curated-playlist playback) ------------------------------------

def _jf_now_playing() -> str | None:
    """The item any client is playing right now, as an inventory rel (or None)."""
    data = _jf("/Sessions")
    if not isinstance(data, list):
        return None
    for s in data:
        npi = s.get("NowPlayingItem") or {}
        rel = _path_to_rel(npi.get("Path", ""))
        if rel:
            return rel
    return None


def _jf_playlists(uid: str) -> list:
    data = _jf(f"/Users/{uid}/Items", IncludeItemTypes="Playlist", Recursive="true")
    return [p["Id"] for p in (data or {}).get("Items", []) if p.get("Id")]


def _jf_playlist_rels(pid: str, uid: str) -> list:
    """A playlist's items as inventory rels, IN PLAYLIST ORDER."""
    data = _jf(f"/Playlists/{pid}/Items", userId=uid, Fields="Path")
    out = []
    for it in (data or {}).get("Items", []):
        rel = _path_to_rel(it.get("Path", ""))
        if rel:
            out.append(rel)
    return out


def _active_playlist_ahead(uid: str, inv: dict, n: int, watched: set) -> list:
    """If an item that belongs to a curated playlist is being played right now (or
    was the most-recently opened thing), return the next `n` items IN PLAYLIST
    ORDER. A "watchable" cut deliberately SKIPS episodes, so the show's natural
    next-episode look-ahead would pre-download the skipped ones; when you're
    watching the playlist, the right next files are the playlist's next entries."""
    anchor = _jf_now_playing()
    if not anchor:
        recents = _recent_accesses(1)
        anchor = recents[0] if recents else None
    if not anchor:
        return []
    for pid in _jf_playlists(uid):
        rels = _jf_playlist_rels(pid, uid)
        if anchor not in rels:
            continue
        i = rels.index(anchor)
        ahead = []
        for r in rels[i + 1:]:
            if r in inv and r not in watched:
                ahead.append(r)
            if len(ahead) >= n:
                break
        return ahead
    return []


# --- access log (universal signal: covers comics too) ------------------------

def _recent_accesses(limit: int) -> list:
    """Most-recently opened media rels (deduped, newest first) from mediafs's access log."""
    p = config.PREDOWNLOAD_ACCESS_LOG
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    seen, out = set(), []
    for line in reversed(lines):            # newest first
        try:
            rel = json.loads(line).get("rel")
        except (ValueError, AttributeError):
            continue
        if rel and rel not in seen and _is_media(rel):
            seen.add(rel)
            out.append(rel)
        if len(out) >= limit:
            break
    return out


# --- folder look-ahead -------------------------------------------------------

def _folder_ahead(rel: str, inv: dict, n: int) -> list:
    """`rel` plus the next `n-1` media siblings in its folder, filename-sorted (episodes /
    volumes come in order). Includes `rel` itself so a re-read stays cached."""
    folder = os.path.dirname(rel)
    exts = config.MEDIAFS_PAYLOAD_EXTENSIONS - _SUBTITLE_EXTS
    sibs = sorted(k for k in inv
                  if os.path.dirname(k) == folder and _ext(k) in exts)
    try:
        i = sibs.index(rel)
    except ValueError:
        return []
    return sibs[i:i + n]


# --- movie prediction via the AI (best-effort, cached) -----------------------

_movie_pred_cache: dict = {}


def _predict_movies(rel: str, inv: dict, n: int) -> list:
    """Ask the AI which library movies you're likely to watch next after `rel`. Cached per
    source movie; returns [] on any failure or for a genuine one-off.

    Every answer is intersected with the real inventory below, so a hallucinated title
    predicts nothing rather than queueing a download for a film that does not exist.
    """
    if rel in _movie_pred_cache:
        return _movie_pred_cache[rel]
    watched_stem = os.path.splitext(os.path.basename(rel))[0]
    catalog = sorted({os.path.splitext(os.path.basename(k))[0]
                      for k in inv if _is_movie(k)})
    prompt = (
        "You predict what a user will watch next so it can be pre-downloaded.\n"
        f"The user just watched the movie: \"{watched_stem}\".\n"
        f"From ONLY this list of movies available in their library, pick up to {n} they are "
        "most likely to watch next -- sequels, prequels, same franchise/series, or very "
        "strongly related. If it is a standalone one-off with nothing related in the list, "
        "return an empty list. Return STRICT JSON: {\"next\": [\"exact title\", ...]} using "
        "titles copied EXACTLY from the list.\n\n"
        "AVAILABLE MOVIES:\n" + "\n".join(catalog)
    )
    try:
        picks = ai.complete_json(prompt, max_tokens=512, timeout=120).get("next", [])
    except Exception as e:                                    # noqa: BLE001
        logging.info(f"movie prediction skipped for {watched_stem}: {e}")
        picks = []
    # map predicted stems back to inventory rels
    stem_to_rel = {}
    for k in inv:
        if _is_movie(k):
            stem_to_rel.setdefault(os.path.splitext(os.path.basename(k))[0], k)
    rels = [stem_to_rel[p] for p in picks if p in stem_to_rel][:n]
    _movie_pred_cache[rel] = rels
    return rels


# --- desired set -------------------------------------------------------------

def build_desired(inv: dict, budget: int) -> list:
    """Ordered list of rels we WANT cached, highest priority first, truncated to `budget`."""
    uid = _jf_user_id()
    watched = _watched_rels(uid) if uid else set()
    ordered, seen = [], set()

    def add(rel):
        if rel in seen or rel not in inv:
            return
        if _is_episode(rel) and rel in watched:
            return                          # already finished -> don't cache it
        seen.add(rel)
        ordered.append(rel)

    # 0) Highest priority: a curated PLAYLIST is being played -> pull its NEXT items
    #    in playlist order. The watchable cut skips episodes, so the show's natural
    #    next-episode look-ahead (section 2) would fetch the skipped ones instead.
    if uid:
        for r in _active_playlist_ahead(uid, inv, config.PREDOWNLOAD_EPISODES_AHEAD, watched):
            add(r)

    # 1) Universal: whatever you most recently opened, look ahead in its folder.
    for rel in _recent_accesses(config.PREDOWNLOAD_RECENT_ITEMS):
        if _is_comic(rel):
            for r in _folder_ahead(rel, inv, config.PREDOWNLOAD_VOLUMES_AHEAD):
                add(r)
        elif _is_movie(rel):
            add(rel)
            for r in _predict_movies(rel, inv, config.PREDOWNLOAD_MOVIES_AHEAD):
                add(r)
        else:  # episode
            for r in _folder_ahead(rel, inv, config.PREDOWNLOAD_EPISODES_AHEAD):
                add(r)

    # 2) Jellyfin active series (covers episodes watched on another device / app, and
    #    crossing season folders which the folder look-ahead can't).
    if uid:
        for sid, _name in _recent_series(uid):
            for r in _series_unwatched_ahead(uid, sid, config.PREDOWNLOAD_EPISODES_AHEAD):
                add(r)

    # Truncate to budget in priority order.
    out, total = [], 0
    for rel in ordered:
        sz = inv[rel][2]
        if total + sz > budget:
            break
        out.append(rel)
        total += sz
    return out, watched


# --- cache accounting --------------------------------------------------------

def _cached_media():
    """(rel, size, atime) for every media file currently in the cache."""
    out = []
    root = config.TIER_CACHE_DIR
    if not root.exists():
        return out
    for dp, _dn, fns in os.walk(root):
        for name in fns:
            if name.endswith((".streaming", ".hydrating")) or ".hydrating-" in name:
                continue
            fp = Path(dp) / name
            rel = str(fp.relative_to(root))
            if not _is_media(rel):
                continue
            try:
                st = fp.stat()
            except OSError:
                continue
            out.append((rel, st.st_size, st.st_atime))
    return out


# --- reconcile ---------------------------------------------------------------

def _clean_stale_partials(execute: bool = True) -> int:
    """Delete orphaned `.streaming`/`.hydrating` partials -- interrupted downloads (e.g.
    from a reboot or a mediafs restart) that are no longer being written. A partial being
    actively filled has a fresh mtime; anything untouched for a while is dead weight and is
    never resumed (the next download truncates a new partial), so it is safe to remove."""
    root = config.TIER_CACHE_DIR
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for dp, _dn, fns in os.walk(root):
        for name in fns:
            if not (name.endswith(".streaming") or ".hydrating" in name):
                continue
            fp = Path(dp) / name
            try:
                if now - fp.stat().st_mtime <= config.PREDOWNLOAD_PROTECT_SEC:
                    continue        # recently written -> an active download, leave it
                if execute:
                    fp.unlink()
                    # The resume map describes THIS partial's contents. Outliving it, it would
                    # describe a future partial's holes instead -- so it goes at the same time.
                    if name.endswith(".streaming"):
                        base = name[: -len(".streaming")]
                        (Path(dp) / (base + tier.STREAM_MAP_SUFFIX)).unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed


def reconcile(execute: bool = True) -> dict:
    stale = _clean_stale_partials(execute)
    if execute and stale:
        logging.info(f"cleaned {stale} stale download partials")
    plan = config.storage_plan()
    budget = plan["prefetch_budget"]
    inv = tier.load_inventory()
    desired, watched = build_desired(inv, budget)
    desired_set = set(desired)
    desired_bytes = sum(inv[r][2] for r in desired)

    cached = _cached_media()
    cached_rels = {r for r, _s, _a in cached}
    # Files already held on an external drive are served locally from there, so they must
    # NOT also be pulled into the SSD cache -- that would be a duplicate download + copy.
    on_drive = _drive_resident_rels()
    now = time.time()

    # What to evict: cached media that is NOT in the desired set and not being read right
    # now (recent atime protects the file you're actively watching/reading). Watched
    # episodes and cold, no-longer-predicted files go first (coldest atime first).
    evictable = sorted(
        [(r, s, a) for (r, s, a) in cached
         if r not in desired_set and (now - a) > config.PREDOWNLOAD_PROTECT_SEC],
        key=lambda t: t[2],
    )
    to_download = [r for r in desired if r not in cached_rels and r not in on_drive]
    dl_bytes = sum(inv[r][2] for r in to_download)

    cache_used = sum(s for _r, s, _a in cached)
    # Free enough so that (kept cache + everything we're about to download) fits the budget.
    need_free = (cache_used + dl_bytes) - budget
    # ...AND enough that the SSD's real free space clears config.SSD_MIN_FREE_BYTES. The budget
    # test above is arithmetic over the cache's own contents; it cannot see space consumed by
    # anything else on the disk (the library root's sidecars + in-flight uploads, sparse
    # streaming partials, the OS). Torrent-Ingest needs real free bytes on THIS disk to admit a
    # download and will otherwise leave a big torrent queued forever, so the floor is checked
    # against the live figure, not the model.
    floor_shortfall = config.SSD_MIN_FREE_BYTES - _ssd_free_bytes()
    need_free = max(need_free, floor_shortfall)
    evicted, freed = [], 0
    for r, s, _a in evictable:
        if freed >= need_free:
            break
        evicted.append(r)
        freed += s
        if execute:
            try:
                (config.TIER_CACHE_DIR / r).unlink()
            except OSError:
                pass
    if execute and evicted:
        _prune_empty_dirs()
        logging.info(f"evicted {len(evicted)} cached files ({_human(freed)}) "
                     f"(watched / no longer predicted)")
    lib_evicted, lib_freed = 0, 0
    if execute and _ssd_free_bytes() < config.SSD_MIN_FREE_BYTES and config.SSD_LIBRARY_AUTO_EVICT:
        # The cache is exhausted (or was never the problem): media is KEPT locally after
        # upload, so the library root is where the reclaimable bytes actually are. Evict the
        # coldest already-uploaded media down to the target, under tier's inventory guard --
        # a file goes only if remote_inventory.json holds it at a matching size, so the last
        # copy can never be the one deleted.
        #
        # `desired` is excluded because deleting what the predictor is about to fetch turns
        # an eviction into a re-download over MEGA at 2-3 MB/s, and PREDOWNLOAD_PROTECT_SEC
        # keeps whatever is being read right now off the list.
        lib = tier.evict_plan(floor_bytes=config.SSD_LIBRARY_EVICT_TARGET_BYTES,
                              inventory=inv, execute=True, exclude=desired_set,
                              protect_sec=config.PREDOWNLOAD_PROTECT_SEC)
        lib_evicted, lib_freed = len(lib["evict"]), lib["would_free"]
        if lib_evicted:
            logging.info(
                f"evicted {lib_evicted} library files ({_human(lib_freed)}) to the "
                f"{_human(config.SSD_LIBRARY_EVICT_TARGET_BYTES)} target -- cold and already "
                f"on a remote; free now {_human(_ssd_free_bytes())}")

    if execute and _ssd_free_bytes() < config.SSD_MIN_FREE_BYTES:
        # Both evictors have run and the floor is still not met: everything left is either
        # predicted, just read, or NOT YET UPLOADED (the inventory guard refuses those, and
        # rightly). Say so -- otherwise Torrent-Ingest's queue silently stops draining and
        # there is nothing in any log tying the two together.
        logging.warning(
            f"SSD below its floor and eviction cannot reach it: free {_human(_ssd_free_bytes())}, "
            f"floor {_human(config.SSD_MIN_FREE_BYTES)}, still short "
            f"{_human(config.SSD_MIN_FREE_BYTES - _ssd_free_bytes())} -- torrent admission may "
            f"stall. Usually this means the uploader is behind: un-uploaded media cannot be "
            f"evicted, so the fix is upload throughput, not eviction.")

    downloaded = 0
    if execute:
        downloaded = _prefetch_parallel(to_download, inv, budget)

    # Gated by config.PREDOWNLOAD_FILL_DRIVES, off by default: external drives are
    # upload-only, so nothing is downloaded onto one. Every other drive role is unaffected --
    # they still serve through mediafs, are scanned for upload, and are organized by
    # drive_ingest.
    drive_filled = (fill_drives(inv, execute)
                    if execute and getattr(config, "PREDOWNLOAD_FILL_DRIVES", True)
                    else 0)

    return {
        "budget": budget, "reference": plan["reference"], "chunk": plan["chunk"],
        "desired": len(desired), "desired_bytes": desired_bytes,
        "to_download": len(to_download), "download_bytes": dl_bytes,
        "evict": len(evicted), "evict_bytes": freed, "downloaded": downloaded,
        "lib_evict": lib_evicted, "lib_evict_bytes": lib_freed,
        "drive_filled": drive_filled, "desired_list": desired,
    }


def _drive_resident_rels() -> set:
    """Every media rel currently present on any attached external drive -- so nothing is
    downloaded to the SSD cache that a drive already holds (no duplicate downloads/copies)."""
    out = set()
    for d in config.discover_library_drives():
        try:
            for dp, dn, fns in os.walk(d):
                dn[:] = [x for x in dn if not x.startswith(".")]
                for n in fns:
                    if os.path.splitext(n)[1].lower() in config.MEDIAFS_PAYLOAD_EXTENSIONS:
                        try:
                            out.add(str((Path(dp) / n).relative_to(d)))
                        except ValueError:
                            pass
        except OSError:
            continue
    return out


def _drive_free_above_buffer(root: Path) -> int:
    """Bytes free on `root`'s volume beyond the reserved buffer (DRIVE_BUFFER_FRACTION of
    the volume's total capacity). Negative clamped to 0."""
    import shutil
    try:
        du = shutil.disk_usage(root)
    except OSError:
        return 0
    buffer = int(du.total * config.DRIVE_BUFFER_FRACTION)
    return max(0, du.free - buffer)


def _is_uploaded(rel: str, size: int, inv_nfc: dict) -> bool:
    """THE delete guard: True only if `rel` is proven on a remote (inventory hit with a
    matching size). A file may NEVER be evicted/deleted for space unless this is True, so a
    not-yet-uploaded original is never lost -- it just waits until it's backed up, after which
    it becomes eligible. Fast: a dict lookup against a precomputed NFC-normalized inventory."""
    e = inv_nfc.get(unicodedata.normalize("NFC", rel))
    return e is not None and e[2] == size


def _evict_drive(root: Path, need: int, inv_nfc: dict, desired_set: set) -> int:
    """Free up to `need` bytes on a drive by deleting its COLDEST media that is (a) proven
    uploaded (_is_uploaded), (b) not in the predicted desired set, and (c) not read recently
    -- so drives cycle like the SSD cache WITHOUT ever risking an un-uploaded original or the
    file you're about to watch. The bytes stay served from the pool. Returns bytes freed."""
    cands = []
    now = time.time()
    for dp, dn, fns in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for n in fns:
            if os.path.splitext(n)[1].lower() not in config.MEDIAFS_PAYLOAD_EXTENSIONS:
                continue
            fp = Path(dp) / n
            try:
                rel = str(fp.relative_to(root))
                st = fp.stat()
            except (OSError, ValueError):
                continue
            cands.append((st.st_atime, st.st_size, rel, fp))
    cands.sort(key=lambda t: t[0])            # coldest (oldest atime) first
    freed = 0
    for atime, size, rel, fp in cands:
        if freed >= need:
            break
        if rel in desired_set:
            continue                          # protect predicted content
        if now - atime < config.PREDOWNLOAD_PROTECT_SEC:
            continue                          # protect what was just read/watched
        if not _is_uploaded(rel, size, inv_nfc):
            continue                          # GUARD: never delete an un-uploaded original
        try:
            fp.unlink()
            freed += size
        except OSError:
            continue
    if freed:
        logging.info(f"drive-evict: freed {_human(freed)} on {root} "
                     f"(uploaded, cold, non-predicted -- still served from pool)")
    return freed


def _prefetch_parallel(to_download: list, inv: dict, budget: int) -> int:
    """Fetch the desired set into the SSD cache, config.PREDOWNLOAD_WORKERS at a time, each
    worker pinned to a DIFFERENT MEGA account. Returns how many files were cached.

    Four properties matter, and the last is deliberately unlike the upload phase:

    * **One account, one in-flight download** -- two streams on a single account is exactly
      the case that throttles, so a remote is claimed before its file is fetched.
    * **Time-bounded.** reconcile() does its self-healing (stale-partial reap, eviction, the
      floor re-check) around this call, so an unbounded prefetch suspends all of it.
      Progress is monotonic (cached files are skipped), so yielding costs nothing.
    * **Budget and floor are re-checked live, under the lock**, because several workers now
      consume the same disk concurrently and the old sequential arithmetic would race.
    * **Strict priority order, not largest-first.** `to_download` is ordered by how likely
      you are to want the file next (active playlist, then folder look-ahead, then
      unwatched-ahead). Reordering it for packing efficiency would discard the one property
      that makes prefetching worth doing at all.
    """
    if not to_download:
        return 0

    workers = max(1, min(config.PREDOWNLOAD_WORKERS, len(to_download)))
    deadline = time.time() + config.PREDOWNLOAD_PREFETCH_MAX_SEC
    lock = threading.Lock()
    queue = list(to_download)          # priority order; workers pop from the FRONT
    busy: set = set()
    state = {"downloaded": 0, "stop": False}

    def _claim():
        """Take the highest-priority item whose remote is idle and which still fits."""
        with lock:
            # An empty queue means DONE, not "wait for a slot". Returning "WAIT" would park
            # every worker in the retry loop until the deadline, holding reconcile() -- and
            # its stale-partial reap and eviction -- for the full window after the real work
            # had finished.
            if state["stop"] or not queue or time.time() >= deadline:
                return None
            for i, rel in enumerate(queue):
                entry = inv.get(rel)
                if not entry:
                    queue.pop(i)
                    return "SKIP"
                remote, _mt, size = entry
                if remote in busy:
                    continue           # another worker holds this account; try the next item
                # Budget: the cache may not grow past its allowance.
                if _live_cache_bytes() + size > budget:
                    state["stop"] = True
                    return None
                # Hard floor on real free space, re-read live. The budget test above is blind
                # to bytes the disk lost to anything but the cache, and overrunning this floor
                # is what starves Torrent-Ingest's admission queue.
                if _ssd_free_bytes() - size < config.SSD_MIN_FREE_BYTES:
                    logging.info(f"holding the {_human(config.SSD_MIN_FREE_BYTES)} SSD floor "
                                 f"(free {_human(_ssd_free_bytes())}); deferring further "
                                 f"pre-downloads")
                    state["stop"] = True
                    return None
                queue.pop(i)
                busy.add(remote)
                return (rel, remote, size)
            return "WAIT"              # every candidate's account is busy right now

    def _worker():
        while True:
            claim = _claim()
            if claim is None:
                return
            if claim == "SKIP":
                continue
            if claim == "WAIT":
                if time.time() >= deadline:
                    return
                time.sleep(1)
                continue
            rel, remote, size = claim
            try:
                logging.info(f"pre-downloading {rel} ({_human(size)}) from {remote}")
                ok = tier.ensure_cached(rel, inventory=inv)
                if ok:
                    with lock:
                        state["downloaded"] += 1
            except Exception as e:                                   # noqa: BLE001
                logging.error(f"prefetch error on {rel}: {e}")
            finally:
                with lock:
                    busy.discard(remote)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in [pool.submit(_worker) for _ in range(workers)]:
            try:
                fut.result()
            except Exception as e:                                   # noqa: BLE001
                logging.error(f"prefetch worker error: {e}")

    if time.time() >= deadline:
        logging.info(f"prefetch: yielding after {config.PREDOWNLOAD_PREFETCH_MAX_SEC}s "
                     f"({state['downloaded']} cached); resuming next cycle")
    return state["downloaded"]


def fill_drives(inv: dict, execute: bool) -> int:
    """Use external drives as bulk predictive-download space AND a cycling cache: while a
    drive has room beyond its buffer, download pool content it doesn't already hold onto the
    drive at its real library path (mediafs serves it locally; media_sync sees it's already
    on a remote -> no re-upload). When a predicted file doesn't fit, evict the coldest
    UPLOADED, non-predicted, non-recent drive files to make room (never an un-uploaded
    original -- the _is_uploaded guard). Prioritises predicted content, then fills leftover
    space with the rest of the pool. Best-effort: defers to live playback, respects the
    per-drive buffer, skips a wedged/vanishing drive. Returns how many files were placed."""
    from .transfer import chunked_download
    drives = config.discover_library_drives()
    if not drives:
        return 0
    # Download (fill drives) and upload run CONCURRENTLY as independent daemons -- predownload
    # here vs media_sync's upload phase -- so a drive's free space is used even while the
    # upload backlog is still draining. Nothing pauses for playback (see wait_while_streaming).
    desired, _watched = build_desired(inv, budget=1 << 62)   # unbounded: just want the order
    desired_set = set(desired)
    inv_nfc = {unicodedata.normalize("NFC", k): v for k, v in inv.items()}
    ordered = list(dict.fromkeys(desired + list(inv.keys())))
    placed = 0
    # TIME-BOUNDED, because this is called at the END of reconcile() and walks the ENTIRE pool
    # inventory. With a mostly-empty 7 TB drive that is days of downloading in one call, and while
    # it runs reconcile() never comes back around -- so stale-partial cleanup and SSD-cache
    # eviction, both of which live in reconcile(), simply stop happening. (Measured 2026-08-04:
    # 94 orphaned `.streaming` partials, the oldest 20 h old, against a 30-min reap threshold.)
    # Cutting the call off hands control back every cycle; progress is monotonic because
    # already-present files are skipped, so the next cycle resumes where this one stopped.
    deadline = time.time() + config.PREDOWNLOAD_DRIVE_FILL_MAX_SEC
    for root in drives:
        room = _drive_free_above_buffer(root)
        for rel in ordered:
            if time.time() >= deadline:
                logging.info(f"drive-fill: yielding after "
                             f"{config.PREDOWNLOAD_DRIVE_FILL_MAX_SEC}s ({placed} placed); "
                             f"resuming next cycle")
                return placed
            entry = inv.get(rel)
            if entry is None or not rel.startswith(config.MEDIAFS_PREFIXES):
                continue
            remote, _mtime, size = entry
            if size <= 0:
                continue
            dest = root / rel
            try:
                if dest.exists() and dest.stat().st_size == size:
                    continue                        # already on this drive
                if any((d / rel).exists() for d in drives if d != root):
                    continue                        # already on another drive -> served anyway
                if tier.is_cached(rel, size):
                    continue                        # already in the SSD cache -> no duplicate
            except OSError:
                continue
            if size > room:
                # Only cycle the drive (evict to make room) for PREDICTED content -- never
                # evict something to shelve mere filler. Filler just uses whatever free space
                # is left.
                if rel in desired_set:
                    room += _evict_drive(root, size - room, inv_nfc, desired_set)
                if size > room:
                    continue
            logging.info(f"drive-fill: {rel} -> {root} ({_human(size)})")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if chunked_download(remote, rel, dest, size):
                    placed += 1
                    room -= size
            except Exception as e:                  # noqa: BLE001
                logging.error(f"drive-fill failed for {rel}: {e}")
                continue
    return placed


def _live_cache_bytes() -> int:
    return sum(s for _r, s, _a in _cached_media())


def _ssd_free_bytes() -> int:
    """Live free bytes on the volume holding the SSD cache. Read fresh at every decision point:
    the cache is not the only writer on this disk (the library root, in-flight uploads and the OS
    all move), so a cached figure drifts exactly when it matters."""
    import shutil
    try:
        root = config.TIER_CACHE_DIR if config.TIER_CACHE_DIR.exists() else Path.home()
        return shutil.disk_usage(root).free
    except OSError:
        return 0


def _prune_empty_dirs():
    root = config.TIER_CACHE_DIR
    for dp, dns, fns in os.walk(root, topdown=False):
        if Path(dp) == root:
            continue
        if not dns and not fns:
            try:
                os.rmdir(dp)
            except OSError:
                pass


# --- entrypoint --------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Predictive pre-download daemon.")
    ap.add_argument("--once", action="store_true", help="one reconcile pass then exit")
    ap.add_argument("--plan", action="store_true", help="print plan + desired set, download nothing")
    args = ap.parse_args()
    setup_logging()

    if args.plan:
        r = reconcile(execute=False)
        print(f"reference {_human(r['reference'])}  budget {_human(r['budget'])}  "
              f"chunk {_human(r['chunk'])}")
        print(f"desired {r['desired']} files ({_human(r['desired_bytes'])})  "
              f"to-download {r['to_download']} ({_human(r['download_bytes'])})  "
              f"would-evict {r['evict']} ({_human(r['evict_bytes'])})")
        for rel in r["desired_list"][:40]:
            print(f"  {rel}")
        if len(r["desired_list"]) > 40:
            print(f"  ... and {len(r['desired_list']) - 40} more")
        return 0

    if args.once:
        r = reconcile(execute=True)
        logging.info(f"reconcile: desired={r['desired']} downloaded={r['downloaded']} "
                     f"evicted={r['evict']} budget={_human(r['budget'])}")
        return 0

    logging.info("predownload daemon started")
    while True:
        try:
            r = reconcile(execute=True)
            logging.info(f"cycle: desired={r['desired']} ({_human(r['desired_bytes'])}) "
                         f"downloaded={r['downloaded']} evicted={r['evict']} "
                         f"lib_evicted={r.get('lib_evict', 0)} "
                         f"({_human(r.get('lib_evict_bytes', 0))}) "
                         f"drive_filled={r.get('drive_filled', 0)} "
                         f"budget={_human(r['budget'])} free={_human(_ssd_free_bytes())}")
        except Exception as e:                               # noqa: BLE001
            logging.error(f"reconcile error: {e}", exc_info=True)
        time.sleep(config.PREDOWNLOAD_POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
