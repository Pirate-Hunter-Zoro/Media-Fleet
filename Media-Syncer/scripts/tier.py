"""Tier engine: inventory-backed hydration + cold-file eviction.

The MEGA pool holds a COMPLETE copy of every media file (single-residence
invariant => `remote_inventory.json` is a full path -> [remote, mtime, size] map).
That makes the local library a CACHE we can thin: evict the coldest media whose
bytes are *proven* on a remote, and hydrate them back on demand straight from the
inventory using the same chunked, VPN-rotating download the daemon already uses.

This module is the mechanism; `mediafs.py` is the on-access trigger that calls
`hydrate()` when Jellyfin/YacReader reads a cold file.

SAFE BY DEFAULT:
  * A file is *evictable* only when `remote_inventory.json` proves its bytes live
    on a remote -- so eviction never removes the last copy of anything.
  * Library eviction is a dry-run PLAN unless `execute=True` is passed explicitly.
  * The hydration cache (TIER_CACHE_DIR) is pure cache: trimming it is always safe
    because every file in it also exists on a remote.

CLI:
    python3 -m scripts.tier --status
    python3 -m scripts.tier --hydrate "Movies/Foo (2020).en.srt"   # fetch one file into cache
    python3 -m scripts.tier --cache-gc                              # LRU-trim the cache now
    python3 -m scripts.tier --evict-plan                           # dry-run: what SSD eviction WOULD do
    python3 -m scripts.tier --evict-plan --execute                 # actually evict (inventory-guarded)
"""
from __future__ import annotations

import argparse
import errno
import json
import logging
import math
import os
import subprocess
import threading
import time
from pathlib import Path

from . import config
from .transfer import chunked_download
from .utils import heal_stale_session, is_stale_session_error
from .vpn import rotate_exit_node


# --- inventory ---------------------------------------------------------------

def load_inventory(path: Path | None = None) -> dict:
    """Load remote_inventory.json -> {relative_path: [remote, mtime_iso, size]}."""
    path = path or config.REMOTE_INVENTORY_PATH
    if not Path(path).exists():
        return {}
    with open(path, "r") as fh:
        return json.load(fh)


def _is_media(relative_path: str) -> bool:
    return os.path.splitext(relative_path)[1].lower() in config.MEDIAFS_PAYLOAD_EXTENSIONS


# --- cache -------------------------------------------------------------------

# Sidecar next to a `.streaming` partial recording which segments are actually present, so a
# fill that is retried after a failure RESUMES instead of re-downloading from zero. The
# partial itself is sparse and pre-truncated to full size, so its length proves nothing about
# its contents -- without this map, bytes already paid for over MEGA are indistinguishable
# from holes and get fetched again.
STREAM_MAP_SUFFIX = ".segmap"


def cache_path_for(relative_path: str) -> Path:
    return config.TIER_CACHE_DIR / relative_path


def is_cached(relative_path: str, size: int | None = None) -> bool:
    p = cache_path_for(relative_path)
    if not p.exists():
        return False
    return size is None or p.stat().st_size == size


def _is_scratch(name: str) -> bool:
    """Whether `name` is download scratch (an in-progress partial or its segment map)
    rather than a finished cache file.

    Scratch is INVISIBLE to cache accounting and eviction alike. It is not a cached file --
    it is the working state of a download in flight, and a trim that counts it or, worse,
    deletes it is deleting the transfer rather than reclaiming a spare copy."""
    return name.endswith((".streaming", ".hydrating", STREAM_MAP_SUFFIX)) or ".hydrating-" in name


def cache_total_bytes() -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(config.TIER_CACHE_DIR):
        for name in filenames:
            if _is_scratch(name):
                continue
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _cache_files_by_atime():
    """Finished cache files, coldest (oldest atime) first. Scratch is excluded: an LRU
    trim that unlinks a `.streaming` partial pulls the file out from under the workers
    still writing it, and their `done` map then points at bytes that no longer exist."""
    out = []
    for dirpath, _d, filenames in os.walk(config.TIER_CACHE_DIR):
        for name in filenames:
            if _is_scratch(name):
                continue
            fp = os.path.join(dirpath, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            out.append((st.st_atime, st.st_size, Path(fp)))
    out.sort(key=lambda t: t[0])
    return out


def enforce_cache_limit(exclude: Path | None = None) -> int:
    """LRU-trim the cache to TIER_CACHE_MAX_BYTES. Always safe (pure cache).
    Returns bytes freed."""
    total = cache_total_bytes()
    if total <= config.TIER_CACHE_MAX_BYTES:
        return 0
    freed = 0
    for _atime, size, fp in _cache_files_by_atime():
        if total - freed <= config.TIER_CACHE_MAX_BYTES:
            break
        if exclude is not None and fp == exclude:
            continue
        try:
            fp.unlink()
            freed += size
        except OSError:
            continue
    logging.info(f"cache LRU trim freed {freed / 1024**2:.0f} MB")
    return freed


# --- hydration ---------------------------------------------------------------

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# Global cap on concurrent downloads (shared VPN/MEGA budget). Only the actual
# chunked_download is held under this -- never cache/metadata reads.
_hydration_sem = threading.Semaphore(config.TIER_MAX_CONCURRENT_HYDRATIONS)


def _lock_for(relative_path: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(relative_path)
        if lk is None:
            lk = _locks[relative_path] = threading.Lock()
        return lk


def hydrate(relative_path: str, inventory: dict | None = None) -> Path | None:
    """Ensure `relative_path` is present in the cache, fetching it from its remote
    (chunked, VPN-rotating) if not. Returns the cache Path, or None if the file is
    not in the inventory (nothing to hydrate from) or the download failed.

    Concurrency-safe: a per-path lock means two simultaneous reads of the same cold
    file trigger exactly one download. The download lands on a temp path and is
    atomically renamed in, so a reader never sees a partial file.
    """
    inv = inventory if inventory is not None else load_inventory()
    entry = inv.get(relative_path)
    if entry is None:
        return None
    remote, _mtime, size = entry
    dest = cache_path_for(relative_path)
    if dest.exists() and dest.stat().st_size == size:
        return dest

    with _lock_for(relative_path):
        # re-check under lock (another thread may have hydrated it)
        if dest.exists() and dest.stat().st_size == size:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + f".hydrating-{os.getpid()}")
        logging.info(f"hydrating '{relative_path}' from {remote} ({size / 1024**2:.0f} MB)...")
        with _hydration_sem:   # bound concurrent downloads (shared VPN/bandwidth)
            ok = chunked_download(remote, relative_path, tmp, size)
        if not ok:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            logging.error(f"hydrate FAILED for '{relative_path}'")
            return None
        os.replace(tmp, dest)
        logging.info(f"hydrated '{relative_path}' -> cache")
        enforce_cache_limit(exclude=dest)
        return dest


# --- "a client is actively streaming" signal (cross-process) -----------------
# Driven by actual READS, not open handles: an app (YacReader/Jellyfin) can hold a media
# file open while completely idle, so "handle open" would falsely read as streaming and
# starve the pre-download daemon forever. Instead, mediafs stamps a flag file on each read
# it serves (throttled); the flag is honored only while FRESH (STREAM_ACTIVE_TTL_SEC), so
# it means "bytes were served to a client just now" == live playback. The sync + pre-
# download daemons (separate processes) read it and yield the MEGA budget to that playback.
# When reads stop, the flag goes stale on its own -- no explicit clear, no heartbeat thread,
# and a mediafs crash can never wedge a daemon paused.

_last_flag_touch = 0.0
_flag_lock = threading.Lock()


def _mark_streaming() -> None:
    """Stamp the streaming-active flag (throttled). Called from read_stream on every served
    read, so the flag stays fresh exactly while a client is actively reading."""
    global _last_flag_touch
    now = time.time()
    with _flag_lock:
        if now - _last_flag_touch < config.STREAM_HEARTBEAT_SEC:
            return
        _last_flag_touch = now
    try:
        config.STREAM_ACTIVE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        config.STREAM_ACTIVE_FLAG.write_text(str(os.getpid()))
    except OSError:
        pass


def streaming_active() -> bool:
    """True iff a client served a read very recently (live playback). Read by the sync and
    pre-download daemons to yield MEGA bandwidth to playback. Honors only a FRESH flag."""
    try:
        st = config.STREAM_ACTIVE_FLAG.stat()
    except OSError:
        return False
    return (time.time() - st.st_mtime) < config.STREAM_ACTIVE_TTL_SEC


# --- streaming hydration (progressive playback) ------------------------------

class _Stream:
    """One in-progress progressive download of a cold file, shared by all readers of
    that path. STREAM_WORKERS parallel workers fill a sparse `.streaming` file one
    segment at a time (front-to-back, so a video's early bytes land first); `done` is a
    per-segment 0/1 map of what is ready to read. A read whose segments are all done is
    served from the partial file; anything not-yet-fetched (a seek, a comic's trailing
    index) is served by an on-demand ranged fetch while the workers keep filling."""
    def __init__(self, rel: str, remote: str, size: int, cache_dest: Path):
        self.rel = rel
        self.remote = remote
        self.size = size
        self.cache_dest = cache_dest
        self.partial = cache_dest.with_name(cache_dest.name + ".streaming")
        self.segmap = cache_dest.with_name(cache_dest.name + STREAM_MAP_SUFFIX)
        self.seg = config.STREAM_SEGMENT_BYTES
        self.nseg = max(1, math.ceil(size / self.seg)) if size > 0 else 0
        self.done = bytearray(self.nseg)   # 1 == segment fully in `partial`
        # Adopt whatever a previous attempt left behind. A retry after a transient failure
        # is otherwise a full re-download: the 44 MB already pulled over MEGA would be
        # fetched a second time purely because nothing recorded that it was there.
        self.resumed = _load_segmap(self)
        self.bad = set()                   # segments a fetch has failed on this attempt; skipped
                                           # by the sweep and retried in a later pass rather than
                                           # killing the fill for the whole file
        self.passes = 0                    # retry sweeps spent on `bad` so far
        self.consecutive_fails = 0         # failures back to back; a RUN (not one) ends the fill
        self.filled_since_save = 0         # segments fetched since the map was last checkpointed
        self.next_seg = 0                  # next segment index to hand a worker
        # Fetch the head AND the tail first: players read the container index on open
        # (an MKV SeekHead/Cues or an MP4 moov atom, usually at the END of the file) to
        # initialize playback, so if the tail isn't ready the player stalls before the
        # first frame ("didn't even play"). Priming both ends means the open-time probe
        # is served immediately; the middle then fills sequentially.
        self.priority_segs = [0] if self.nseg <= 1 else [0, self.nseg - 1]
        self.last_read_end = 0             # end offset of the previous read (seek detection)
        self.readers_waiting = 0           # interactive ranged fetches in flight (fill yields to them)
        self.inflight = set()              # segment indices being fetched right now, by ANY thread
                                           # (fill worker or a reader coalescing a cold miss) -- so
                                           # the two never fetch the same 4 MB block twice
        self.complete = False
        self.failed = False
        self.refs = 0
        self.cond = threading.Condition()

    def _have(self, s0: int, s1: int) -> bool:
        return all(self.done[i] for i in range(s0, s1 + 1))


# Checkpoint interval, in segments (4 MB each), for the resume map. Frequent enough that a
# crash costs little re-download, rare enough that the write never competes with the fetch.
_SEGMAP_SAVE_EVERY = 8


def _load_segmap(s: _Stream) -> int:
    """Restore `s.done` from the sidecar left by a previous attempt. Returns how many
    segments were adopted (0 if there is nothing usable).

    Guarded on BOTH the partial's presence and its size: a map whose partial was reaped,
    replaced, or written for a different revision of the file describes bytes that are not
    there, and trusting it would serve holes as content -- which reaches the player as
    corrupt video, the one failure worse than a slow download."""
    try:
        if not s.partial.exists() or s.partial.stat().st_size != s.size:
            return 0
        raw = json.loads(s.segmap.read_text())
        if raw.get("size") != s.size or raw.get("seg") != s.seg:
            return 0
        done = bytes.fromhex(raw["done"])
        if len(done) != s.nseg:
            return 0
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    s.done[:] = done
    return sum(done)


def _save_segmap(s: _Stream) -> None:
    """Persist `s.done` beside the partial. Best-effort: losing the map costs a re-download,
    so it must never be able to break the fill that produced it."""
    try:
        with s.cond:
            payload = {"size": s.size, "seg": s.seg, "done": bytes(s.done).hex()}
        tmp = s.segmap.with_name(s.segmap.name + ".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, s.segmap)          # atomic: never leave a half-written map behind
    except (OSError, ValueError):
        pass


def _drop_segmap(s: _Stream) -> None:
    try:
        s.segmap.unlink(missing_ok=True)
    except OSError:
        pass


_streams: dict[str, _Stream] = {}
_streams_guard = threading.Lock()
# rel -> monotonic time its fill last gave up, so a rebuild is allowed but not hammered.
_stream_failed_at: dict[str, float] = {}


def get_stream(rel: str, inventory: dict | None = None, count_handle: bool = True) -> _Stream | None:
    """Get (or start) the progressive download for `rel`. Returns None if `rel` is not
    in the inventory. Reference-counted so many concurrent opens share one download.

    `count_handle` marks this as an INTERACTIVE open (a real client read), which sets the
    cross-process streaming flag so the sync daemon yields. Background PREFETCH passes
    count_handle=False: it must NOT pause the daemon (a prefetch can run for many minutes)
    and it is best-effort, not user-visible playback."""
    inv = inventory if inventory is not None else load_inventory()
    entry = inv.get(rel)
    if entry is None:
        return None
    remote, _mtime, size = entry
    dest = cache_path_for(rel)
    with _streams_guard:
        s = _streams.get(rel)
        # A stream whose fill gave up must not be handed out again. Its workers are gone, so
        # it can never make progress, yet every reader that gets it skips the segment wait AND
        # the 4 MB coalescing (both are gated on `failed`) and falls all the way back to one
        # rclone login per 128 KB read. That is the "plays for 90 seconds, then stalls forever,
        # and a restart doesn't help" shape: the object outlives the playback session, so only
        # restarting mediafs ever cleared it. Rebuild instead -- after a cooldown, so a remote
        # that really is unreachable is retried periodically rather than continuously.
        if s is not None and s.failed:
            if time.monotonic() - _stream_failed_at.get(rel, 0.0) < config.STREAM_RETRY_COOLDOWN_SEC:
                s.refs += 1
                return s                   # still cooling down: serve on-demand reads meanwhile
            logging.info(f"retrying stalled fill for '{rel}' (resuming from what is on disk)")
            _streams.pop(rel, None)
            s = None
        if s is None:
            s = _Stream(rel, remote, size, dest)
            _streams[rel] = s
            threading.Thread(target=_stream_worker, args=(s,), daemon=True).start()
        s.refs += 1
    # count_handle is retained for call-site clarity (interactive vs prefetch) but the
    # streaming-active flag is now driven by actual reads (_mark_streaming in read_stream),
    # so opening alone -- interactive or prefetch -- never marks streaming.
    return s


def release_stream(s: _Stream, count_handle: bool = True) -> None:
    with _streams_guard:
        s.refs -= 1
    # The download runs to completion regardless (so the file caches for next time);
    # the worker removes it from the registry when done.


def _forget(s: _Stream) -> None:
    with _streams_guard:
        if _streams.get(s.rel) is s:
            del _streams[s.rel]


# --- predictive prefetch (on-disc feel for binge-watching / sequential reading) ------
# When a client opens episode/volume N, fetch the next few in the same folder into the
# cache so they are already fully local (instant, seek-anywhere) by the time playback
# reaches them. Bounded look-ahead (config.PREFETCH_AHEAD) chained through mediafs.open
# only -- a prefetch itself never triggers further prefetch -- so it can't run away.

_SUBTITLE_EXTS = {".srt", ".ass"}


def _next_media(rel: str, inv: dict, n: int) -> list[str]:
    """The next `n` real media siblings after `rel` in the same folder, filename-sorted
    (so SxxEyy episodes and vNN volumes come in order). Subtitles excluded."""
    folder = os.path.dirname(rel)
    exts = config.MEDIAFS_PAYLOAD_EXTENSIONS - _SUBTITLE_EXTS
    sibs = sorted(k for k in inv
                  if os.path.dirname(k) == folder
                  and os.path.splitext(k)[1].lower() in exts)
    try:
        i = sibs.index(rel)
    except ValueError:
        return []
    return sibs[i + 1: i + 1 + n]


def _present_on_a_drive(rel: str, size: int) -> bool:
    """True if `rel` (matching size) already lives on an attached external drive -- served
    locally from there, so it must not also be pulled into the SSD cache (no duplicates)."""
    for d in config.discover_library_drives():
        try:
            p = d / rel
            if p.is_file() and p.stat().st_size == size:
                return True
        except OSError:
            continue
    return False


def ensure_cached(rel: str, inventory: dict | None = None) -> bool:
    """Fully download `rel` into the cache (via the fast 2-worker streaming fill -- no
    per-chunk rotation), returning True once it is present. Background/best-effort:
    count_handle=False so it never sets the streaming-active flag (never pauses the sync
    daemon) and yields the account budget to any real interactive playback. Used by the
    on-open prefetch and by the predictive pre-download daemon."""
    inv = inventory if inventory is not None else load_inventory()
    entry = inv.get(rel)
    if entry is None:
        return False
    _remote, _mtime, size = entry
    if is_cached(rel, size):
        return True
    if _present_on_a_drive(rel, size):
        return True                       # already local on a drive -> don't duplicate to SSD
    s = get_stream(rel, inventory=inv, count_handle=False)
    if s is None:
        return False
    try:
        with s.cond:
            while not s.complete and not s.failed:
                s.cond.wait(timeout=10)
            complete, done = s.complete, sum(s.done)
        if not complete:
            # Say so. A silent False here is indistinguishable from "nothing to do", and the
            # pre-download daemon's caller treats it that way -- it re-queues the same file
            # every cycle and reports `downloaded=0` forever, which reads as an idle library
            # rather than a file that has been failing for hours.
            logging.warning(f"pre-cache of '{rel}' did not complete "
                            f"({done}/{s.nseg} segments); will retry on a later cycle")
        return complete
    finally:
        release_stream(s, count_handle=False)


def _prefetch_one(rel: str, inv: dict) -> None:
    ensure_cached(rel, inventory=inv)


def trigger_prefetch(rel: str, inv: dict) -> None:
    """Kick off background prefetch of the next `PREFETCH_AHEAD` media after `rel`.
    Call this from the INTERACTIVE open path only (mediafs.open), never from prefetch
    itself, so look-ahead stays bounded and cannot chain through a whole season."""
    if not getattr(config, "PREFETCH_AHEAD", 0):
        return
    for nxt in _next_media(rel, inv, config.PREFETCH_AHEAD):
        threading.Thread(target=_prefetch_one, args=(nxt, inv), daemon=True).start()


def _stream_worker(s: _Stream) -> None:
    """Coordinator: fan out STREAM_WORKERS segment-fetchers, wait for them, promote the
    finished file into the cache. Holds ONE _hydration_sem slot for the whole file so
    the number of files streaming at once is bounded (not the connections per file)."""
    # Already fully cached from a prior play? Serve straight from cache.
    if s.cache_dest.exists() and s.cache_dest.stat().st_size == s.size:
        with s.cond:
            for i in range(s.nseg):
                s.done[i] = 1
            s.complete = True
            s.cond.notify_all()
        _forget(s)
        return
    try:
        s.partial.parent.mkdir(parents=True, exist_ok=True)
        if not s.resumed:
            # Recreating the partial from scratch. The map MUST die first: a sparse partial is
            # full-size the instant it is created, so a surviving map would pass every
            # adoption check on the next attempt and hand out holes as if they were content.
            # Dropping it before the truncate keeps that true even if we crash mid-way.
            _drop_segmap(s)
        # "r+b" when resuming: "wb" truncates to zero, which would discard the very bytes
        # `s.resumed` just told us are there. Only a partial we cannot adopt is recreated.
        mode = "r+b" if s.resumed else "wb"
        with open(s.partial, mode) as f:      # pre-allocate sparse to full size
            f.truncate(s.size)
    except OSError as e:
        _fail(s, f"prealloc: {e}")
        return

    resumed_note = f", resuming {s.resumed}/{s.nseg} segments" if s.resumed else ""
    logging.info(f"streaming '{s.rel}' from {s.remote} "
                 f"({s.size / 1024**2:.0f} MB, {config.STREAM_WORKERS} workers{resumed_note})...")
    with _hydration_sem:   # bound concurrent FILES (shared VPN/bandwidth)
        nworkers = max(1, min(config.STREAM_WORKERS, s.nseg))
        workers = [threading.Thread(target=_seg_worker, args=(s,), daemon=True)
                   for _ in range(nworkers)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

    with s.cond:
        failed = s.failed
        missing = sum(1 for i in range(s.nseg) if not s.done[i])
    if failed:
        # Keep the partial AND its map: the next attempt resumes from here instead of paying
        # for these bytes twice. Drop the registry entry so that next attempt can happen at
        # all -- a failed stream left in the registry is handed to every future reader and can
        # never make progress again.
        _save_segmap(s)
        _stream_failed_at[s.rel] = time.monotonic()
        logging.error(f"fill for '{s.rel}' gave up with {missing}/{s.nseg} segments missing; "
                      f"reads served on demand, retry in {config.STREAM_RETRY_COOLDOWN_SEC}s")
        _forget(s)
        return

    try:
        os.replace(s.partial, s.cache_dest)   # promote to a normal cache file
    except OSError:
        pass
    _drop_segmap(s)                           # the map describes a partial that no longer exists
    with s.cond:
        s.complete = True
        s.cond.notify_all()
    logging.info(f"streamed '{s.rel}' -> cache")
    _stream_failed_at.pop(s.rel, None)
    enforce_cache_limit(exclude=s.cache_dest)
    _forget(s)


def _seg_worker(s: _Stream) -> None:
    """Claim the next undone segment (sequential, for playback locality) and fetch it.
    No proactive rotation between segments -- a fresh connection to one account sustains
    the per-IP budget; rotation happens only inside _fetch_into on an actual failure."""
    while True:
        with s.cond:
            # Interactive reads (a seek / comic index ranged fetch) get priority for the
            # account's throttled budget: don't start a NEW background segment while one is
            # in flight, so the on-demand read isn't fighting two bulk connections.
            #
            # BOUNDED, though -- an unbounded wait here is a priority inversion. A sequential
            # cold reader keeps readers_waiting above zero essentially continuously, which
            # parked both workers forever: the efficient 4 MB bulk fill never ran, so every
            # read stayed a small on-demand fetch, which kept readers_waiting up. The fill
            # starved exactly when it was needed most. Past the ceiling we proceed anyway.
            yield_deadline = time.time() + config.STREAM_FILL_YIELD_MAX_SEC
            while s.readers_waiting > 0 and not s.failed:
                remaining = yield_deadline - time.time()
                if remaining <= 0:
                    break
                s.cond.wait(timeout=remaining)
            if s.failed:
                return
            # Priority segments first (head + tail, for the open-time container-index probe),
            # then the sequential middle. done[] dedups if the sequential sweep reaches one, and
            # inflight skips whatever a coalescing reader is already fetching.
            i = None
            while s.priority_segs:
                cand = s.priority_segs.pop(0)
                if not s.done[cand] and cand not in s.inflight:
                    i = cand
                    break
            if i is None:
                i = s.next_seg
                while i < s.nseg and (s.done[i] or i in s.inflight or i in s.bad):
                    i += 1
                if i >= s.nseg:
                    s.next_seg = s.nseg
                    # End of sweep. Anything parked in `bad` failed on a transient -- an exit
                    # node that rotated mid-fetch, a throttled account -- so sweep back over
                    # those before declaring the file unreachable. By now conditions have
                    # usually changed; giving up here is what stranded a whole episode on one
                    # unlucky 4 MB block.
                    if s.bad and s.passes < config.STREAM_FILL_RETRY_PASSES:
                        s.passes += 1
                        s.bad.clear()
                        s.consecutive_fails = 0
                        s.next_seg = 0
                        continue
                    return
                s.next_seg = i + 1
            s.inflight.add(i)
        off = i * s.seg
        cnt = min(s.seg, s.size - off)
        try:
            ok = _fetch_into(s.remote, s.rel, s.partial, off, cnt)
        finally:
            with s.cond:                       # release the claim even if the fetch raised
                s.inflight.discard(i)
                s.cond.notify_all()
        with s.cond:
            if ok:
                s.done[i] = 1
                s.consecutive_fails = 0
                s.filled_since_save += 1
                s.cond.notify_all()
            else:
                # One segment is not the file. Park it and keep going -- the rest of the
                # episode is still perfectly reachable, and a reader that lands on this block
                # ranged-fetches it with its own retries in the meantime. Only a RUN of
                # failures means the remote itself is gone, and that is what ends the fill.
                s.bad.add(i)
                s.consecutive_fails += 1
                give_up = s.consecutive_fails >= config.STREAM_FILL_MAX_CONSECUTIVE_FAILS
                if give_up and not s.failed:
                    s.failed = True
                    # Start the cooldown HERE, with the flag, not in the coordinator's cleanup.
                    # Between the two, a waiter woken by `failed` can reach get_stream while
                    # this stream is still registered; with no timestamp yet it would look
                    # long-expired, get rebuilt, and the new stream would truncate the partial
                    # out from under the coordinator that is about to checkpoint its map --
                    # leaving a map that describes bytes no longer on disk.
                    _stream_failed_at[s.rel] = time.monotonic()
                    logging.error(f"stream fill for '{s.rel}' stopped after "
                                  f"{s.consecutive_fails} consecutive segment failures "
                                  f"(latest segment {i}, offset {off})")
                s.cond.notify_all()
                if give_up:
                    return
            save = s.filled_since_save >= _SEGMAP_SAVE_EVERY
            if save:
                s.filled_since_save = 0
        if not ok:
            # A just-rotated exit node needs a moment before it will serve. Retrying straight
            # into it spends the consecutive-failure budget on one bad node and turns a
            # recoverable blip into a give-up.
            time.sleep(config.STREAM_FILL_BACKOFF_SEC)
        elif save:
            _save_segmap(s)                    # checkpoint, so a crash resumes near where we are


def _fail(s: _Stream, why: str) -> None:
    """Abandon a stream that never got off the ground (the partial could not be created).
    The partial and its map go too -- there is nothing on disk worth resuming from -- and
    the registry entry goes so the next open is free to try again."""
    logging.error(f"stream failed for '{s.rel}': {why}")
    with s.cond:
        s.failed = True
        s.cond.notify_all()
    try:
        s.partial.unlink(missing_ok=True)
    except OSError:
        pass
    _drop_segmap(s)
    _stream_failed_at[s.rel] = time.monotonic()
    _forget(s)


def _fetch_into(remote: str, rel: str, partial: Path, offset: int, count: int) -> bool:
    """Download [offset, offset+count) into `partial` at that offset (seek-write, so a
    retry overwrites rather than duplicates). Rotates the VPN only on FAILURE."""
    for attempt in range(config.MAX_DOWNLOAD_TRIES):
        err = ""
        try:
            with open(partial, "r+b") as f:
                f.seek(offset)
                proc = subprocess.run(
                    [config.RCLONE_PATH, "cat", f"--offset={offset}", f"--count={count}",
                     f"{remote}:{rel}"],
                    stdout=f, stderr=subprocess.PIPE, timeout=config.STREAM_TIMEOUT(count),
                )
            err = proc.stderr.decode("utf-8", "ignore").strip()
            if proc.returncode == 0 and err == "":
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        if attempt < config.MAX_DOWNLOAD_TRIES - 1:
            # A dead session survives a rotation, and a rotation costs 10-40 s of dead air
            # while someone is waiting on this file. Heal first when the signature says so.
            if is_stale_session_error(err) and heal_stale_session(remote):
                continue
            rotate_exit_node()   # reactive only: the current node/connection went bad
    return False


def _ranged_fetch(remote: str, rel: str, offset: int, size: int) -> bytes:
    """Fetch just [offset, offset+size) directly (for reads on not-yet-filled segments:
    seeks, an MP4 moov atom at the end, a comic's trailing central directory). Retries
    with reactive VPN rotation so an interactive read doesn't die on one bad node."""
    last_err = ""
    for attempt in range(config.MAX_DOWNLOAD_TRIES):
        try:
            proc = subprocess.run(
                [config.RCLONE_PATH, "cat", f"--offset={offset}", f"--count={size}",
                 f"{remote}:{rel}"],
                capture_output=True, timeout=config.STREAM_TIMEOUT(size),
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout[:size]
            last_err = proc.stderr.decode("utf-8", "ignore").strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            last_err = str(e)
        if attempt < config.MAX_DOWNLOAD_TRIES - 1:
            # This read is interactive -- a player is blocked on it -- so a rotation the
            # failure cannot be fixed by is the worst thing to spend the retry on.
            if is_stale_session_error(last_err) and heal_stale_session(remote):
                continue
            rotate_exit_node()
    # errno.EIO, not a bare message. This OSError is raised on a FUSE read thread, and
    # fusepy's operation wrapper turns an exception into a return code with `if e.errno > 0`
    # -- which on an errno-less OSError raises TypeError, and fusepy answers a TypeError by
    # tearing the WHOLE MOUNT DOWN. So on 2026-09-01 one timed-out 64 KB range read of one
    # Futurama episode unmounted the entire library, and Jellyfin -- whose library path is
    # that mount -- began pruning every collection it could no longer see. A failed read of
    # one file must cost that read, and nothing else.
    raise OSError(errno.EIO,
                  f"ranged fetch failed for {rel} @ {offset} (+{size}): {last_err}")


def read_stream(s: _Stream, offset: int, size: int) -> bytes:
    """Serve a read: from cache if the whole file is complete, from the partial file if
    the covering segments are already downloaded, else -- after a short wait for the
    workers to reach them -- via an on-demand ranged fetch."""
    _mark_streaming()      # a client is reading right now -> daemons yield MEGA to it
    if offset >= s.size:
        return b""
    size = min(size, s.size - offset)
    end = offset + size
    s0 = offset // s.seg
    s1 = (end - 1) // s.seg

    with s.cond:
        # Seek-follow: if this read continues roughly where the last one ended (sustained
        # sequential playback) but sits ahead of the background fill, RELOCATE the fill to
        # start here -- otherwise a forward seek leaves the fill stuck behind the playhead
        # and every read past it stays a slow ranged fetch (the "spinning wheel" on scrub).
        # Requiring sequential continuation avoids relocating on a one-off tail read (an
        # MP4 moov atom / a comic's central directory), which would send the fill to the
        # end and starve playback from the start.
        seq = 0 <= (offset - s.last_read_end) <= s.seg
        s.last_read_end = end
        if seq and not s.complete and not s._have(s0, s1):
            s.next_seg = s0
            s.cond.notify_all()   # wake the fill workers to resume from the new position

        # Wait for the covering segments ONLY when the fill is close behind them; a read
        # far ahead of the frontier (a fresh seek's first read, an MP4 moov tail, a comic
        # index) skips the wait and ranged-fetches at once.
        imminent = s0 <= s.next_seg + config.STREAM_LOOKAHEAD_SEGS
        if imminent and not s.complete and not s._have(s0, s1) and not s.failed:
            deadline = time.time() + config.STREAM_READ_WAIT_SEC
            while not s.complete and not s._have(s0, s1) and not s.failed:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                s.cond.wait(timeout=remaining)
        complete = s.complete
        have = complete or s._have(s0, s1)

    if have:
        # Tolerate the promote race: the coordinator may rename partial -> cache_dest
        # between our decision and the open, so try both (cache first if complete).
        for src in ([s.cache_dest, s.partial] if complete else [s.partial, s.cache_dest]):
            try:
                fd = os.open(src, os.O_RDONLY)
            except FileNotFoundError:
                continue
            try:
                return os.pread(fd, size, offset)
            finally:
                os.close(fd)
        # `done` said these segments were on disk and BOTH files are gone -- the partial was
        # reaped (a stale-partial sweep, an out-of-band delete) while this stream still held
        # its map. The map is now a liar: left alone it keeps sending every read down this
        # dead path, so drop it and let the fetch below repopulate honestly.
        with s.cond:
            if not s.complete:
                s.done = bytearray(s.nseg)
                s.next_seg = 0
                s.cond.notify_all()
        logging.warning(f"partial for '{s.rel}' vanished under an active stream; refetching")
    # Segments not ready (seek / comic index / a stalled background fill), or both files
    # briefly absent during promote. Flag ourselves so the background fill yields the account's
    # budget to this interactive read (and clears in a finally, even if the fetch raises).
    with s.cond:
        s.readers_waiting += 1
        s.cond.notify_all()
    try:
        # COALESCE: fetch the whole segment-aligned block covering this read, persist it into
        # the partial, and mark it done -- rather than fetching just these ~128 KB and throwing
        # them away. A sequential cold reader (Jellyfin's ffprobe, a comic page-turn sweep) then
        # gets the next ~32 reads straight from disk instead of paying a fresh MEGA login each
        # time. See config.STREAM_COALESCE_SEGS for the measurement that forced this.
        # Coalesce even when the background fill has given up. Gating this on `not s.failed`
        # was exactly backwards: a stalled fill is when the reader is the ONLY thing still
        # fetching bytes, so that is when spending a MEGA login on 4 MB instead of 128 KB
        # matters most. With the gate, a stalled file served ~8 logins per megabyte and never
        # got faster -- playback that dies a minute or two in and then never recovers.
        if s.nseg > 0:
            data = _coalesced_read(s, offset, size, s0, s1)
            if data is not None:
                return data
        # Coalescing declined (the span is contested, or the block is genuinely unreachable)
        # -> exact-range fetch, so a stall still degrades to slow reads rather than an EIO.
        return _ranged_fetch(s.remote, s.rel, offset, size)
    finally:
        with s.cond:
            s.readers_waiting -= 1
            s.cond.notify_all()


def _coalesced_read(s: _Stream, offset: int, size: int, s0: int, s1: int) -> bytes | None:
    """Fetch the segment-aligned block covering [offset, offset+size) into `s.partial`, mark
    those segments done, and serve the read from disk. Returns None if it could not be done
    (so the caller falls back to an exact-range fetch).

    Bounded to STREAM_COALESCE_SEGS beyond the requested span, so a huge read can't turn into
    an unbounded download. Claims segments via `s.inflight` so a fill worker and a reader --
    or two readers -- never fetch the same block concurrently; a segment someone else already
    has in flight is waited for briefly rather than duplicated.
    """
    lo, hi = s0, min(s.nseg - 1, s1 + max(0, config.STREAM_COALESCE_SEGS - 1))
    claim: list[int] = []
    with s.cond:
        # Wait out anything already in flight over our span -- the other fetcher's bytes are as
        # good as ours, and duplicating a 4 MB MEGA fetch is exactly the waste we're removing.
        deadline = time.time() + config.STREAM_READ_WAIT_SEC
        while any(i in s.inflight for i in range(lo, hi + 1)):
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            s.cond.wait(timeout=remaining)
        if not (s.complete or s._have(s0, s1)):
            claim = [i for i in range(lo, hi + 1) if not s.done[i] and i not in s.inflight]
            if not claim:
                return None                       # still contested -> exact-range fetch instead
            s.inflight.update(claim)
    if claim:                                     # empty == another thread already filled it
        off = claim[0] * s.seg
        cnt = min((claim[-1] + 1) * s.seg, s.size) - off
        try:
            ok = _fetch_into(s.remote, s.rel, s.partial, off, cnt)
        finally:
            with s.cond:
                s.inflight.difference_update(claim)
                s.cond.notify_all()
        with s.cond:
            if ok:
                for i in claim:
                    s.done[i] = 1
                s.cond.notify_all()
            elif not s._have(s0, s1):
                return None                       # block unreachable -> exact-range fetch
        if ok:
            # Once the fill has given up, the reader is the only thing still pulling bytes, so
            # its blocks are the only ones the map would ever record. Checkpoint here too or a
            # resume throws away everything playback paid for.
            _save_segmap(s)
    for src in [s.partial, s.cache_dest]:
        try:
            fd = os.open(src, os.O_RDONLY)
        except FileNotFoundError:
            continue
        try:
            return os.pread(fd, size, offset)
        finally:
            os.close(fd)
    return None


# --- library eviction (SSD thinning) -----------------------------------------

def _library_media_by_coldness(root: Path):
    """All tracked media files under `root`, coldest (oldest atime) first, each
    with its library-relative path, size, and atime."""
    out = []
    for dirpath, _d, filenames in os.walk(root):
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in config.MEDIAFS_PAYLOAD_EXTENSIONS:
                continue
            fp = Path(dirpath) / name
            try:
                st = fp.stat()
            except OSError:
                continue
            rel = str(fp.relative_to(root))
            # Only evict from prefixes served through the mount (Comics excluded
            # until YacReader is repointed -- see TIER_EVICT_PREFIXES).
            if not rel.startswith(config.TIER_EVICT_PREFIXES):
                continue
            out.append((st.st_atime, st.st_size, rel, fp))
    out.sort(key=lambda t: t[0])
    return out


def evict_plan(root: Path | None = None, floor_bytes: int | None = None,
               inventory: dict | None = None, execute: bool = False,
               exclude: set | None = None, protect_sec: int = 0) -> dict:
    """Plan (and optionally perform) eviction of the coldest library media whose
    bytes are proven on a remote, down to a free-space floor on `root`'s volume.

    A file is included ONLY if it appears in `remote_inventory.json` with a matching
    size -- so its bytes are guaranteed on a remote and the local delete never
    removes the last copy. `execute=False` (default) moves nothing; it returns the
    plan for inspection.

    `exclude` is a set of library-relative paths never to evict; `predownload` passes
    its desired set, because deleting a file the predictor is about to fetch turns one
    eviction into a delete-then-re-download over MEGA at 2-3 MB/s. `protect_sec` skips
    anything read that recently, which is what keeps the episode you are watching right
    now -- and its already-fetched successors -- off the list.
    """
    root = root or config.SSD_LIBRARY_ROOT
    floor_bytes = floor_bytes if floor_bytes is not None else config.TIER_CACHE_MAX_BYTES
    inv = inventory if inventory is not None else load_inventory()
    exclude = exclude or set()

    import shutil
    free = shutil.disk_usage(root).free
    need = max(0, floor_bytes - free)
    plan = {"root": str(root), "free_before": free, "floor": floor_bytes,
            "need_to_free": need, "evict": [], "would_free": 0, "skipped_no_inventory": 0,
            "skipped_excluded": 0, "skipped_recent": 0}
    if need <= 0:
        return plan

    now = time.time()
    freed = 0
    for atime, size, rel, fp in _library_media_by_coldness(root):
        if freed >= need:
            break
        if rel in exclude:
            plan["skipped_excluded"] += 1
            continue   # predicted next -> evicting it just forces a re-download
        if protect_sec and (now - atime) <= protect_sec:
            plan["skipped_recent"] += 1
            continue   # being read right now
        entry = inv.get(rel)
        if entry is None or entry[2] != size:
            plan["skipped_no_inventory"] += 1
            continue   # not proven on a remote -> NEVER evict
        plan["evict"].append({"path": rel, "size": size, "remote": entry[0]})
        freed += size
        if execute:
            try:
                fp.unlink()
                logging.info(f"evicted '{rel}' ({size / 1024**2:.0f} MB) -- safe on {entry[0]}")
            except OSError as e:
                logging.error(f"evict failed for '{rel}': {e}")
    plan["would_free"] = freed
    return plan


# --- CLI ---------------------------------------------------------------------

def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Media-Syncer tier engine.")
    ap.add_argument("--status", action="store_true", help="cache + inventory summary")
    ap.add_argument("--hydrate", metavar="RELPATH", help="fetch one inventory path into the cache")
    ap.add_argument("--cache-gc", action="store_true", help="LRU-trim the cache to its limit now")
    ap.add_argument("--evict-plan", action="store_true", help="show/perform SSD eviction plan")
    ap.add_argument("--floor-gb", type=float, default=None, help="free-space floor for eviction (GB)")
    ap.add_argument("--execute", action="store_true", help="with --evict-plan: actually delete (inventory-guarded)")
    args = ap.parse_args()

    inv = load_inventory()

    if args.status:
        media = sum(1 for k in inv if _is_media(k))
        print(f"inventory:   {len(inv)} keys ({media} media) at {config.REMOTE_INVENTORY_PATH}")
        print(f"cache dir:   {config.TIER_CACHE_DIR}")
        print(f"cache used:  {_human(cache_total_bytes())} / {_human(config.TIER_CACHE_MAX_BYTES)} limit")
        return 0

    if args.hydrate:
        p = hydrate(args.hydrate, inventory=inv)
        if p is None:
            print(f"FAILED to hydrate '{args.hydrate}' (not in inventory, or download failed)")
            return 1
        print(f"hydrated -> {p} ({_human(p.stat().st_size)})")
        return 0

    if args.cache_gc:
        freed = enforce_cache_limit()
        print(f"freed {_human(freed)}")
        return 0

    if args.evict_plan:
        floor = int(args.floor_gb * 1024**3) if args.floor_gb is not None else None
        plan = evict_plan(floor_bytes=floor, inventory=inv, execute=args.execute)
        verb = "EVICTED" if args.execute else "WOULD EVICT"
        print(f"root {plan['root']}: free {_human(plan['free_before'])}, "
              f"floor {_human(plan['floor'])}, need to free {_human(plan['need_to_free'])}")
        print(f"{verb} {len(plan['evict'])} files = {_human(plan['would_free'])} "
              f"({plan['skipped_no_inventory']} cold files skipped: not proven on a remote)")
        for e in plan["evict"][:20]:
            print(f"  {_human(e['size']):>10}  {e['remote']:<24}  {e['path']}")
        if len(plan["evict"]) > 20:
            print(f"  ... and {len(plan['evict']) - 20} more")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
