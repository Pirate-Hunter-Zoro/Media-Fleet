"""mediafs -- a virtual media filesystem backed by the inventory + tier cache.

This is the on-access half of the virtual-library design. It presents a MERGED,
read-only view of two sources:

  * `lower` -- a real on-disk directory. Everything in it (Jellyfin/YacReader
    metadata sidecars: .nfo/.jpg/.png, plus any pinned or already-cached media)
    passes through untouched. Reads of a real file are just reads of that file.

  * `remote_inventory.json` -- the complete path -> [remote, mtime, size] map of
    every media payload on the MEGA pool. Any media file that is NOT present in
    `lower` is presented here as a FULL-SIZE file (size straight from the
    inventory, so `stat`/scans are instant and never touch a remote). Its bytes
    are fetched only on an actual READ, via `tier.hydrate()` -- the same chunked,
    VPN-rotating download the daemon uses -- landing in the tier cache, after
    which reads are served locally.

The result: Jellyfin (pointed at the mount) sees the entire library at full size,
scans stay local and cheap, and a file's bytes materialize only when you press
play. Cold-file first-read is one chunked download long (hidden by pinning +
prefetch); everything hot/cached is instant.

Writes pass through to `lower` (the real drive): metadata is never virtualized, so
a `.nfo`/artwork write from Jellyfin lands on the real disk. Media payloads are
read-through-hydrate and are never written through the mount (new media arrives via
Torrent-Ingest -> SSD library root -> Media-Syncer).

    python3 -m scripts.mediafs <mountpoint> [--lower DIR] [--foreground]

Run it through `run_mediafs.sh`, which exports FUSE_LIBRARY_PATH pointing fusepy at
fuse-t. Invoked bare, fusepy looks libfuse up by name (`find_library('fuse')`), which
is the macFUSE path that macOS 27 refuses -- and macFUSE is no longer installed here,
so the lookup finds nothing and the import fails.
"""
from __future__ import annotations

import argparse
import errno
import functools
import json
import logging
import os
import stat as statmod
import subprocess
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import fuse as _fusepy
from fuse import FUSE, FuseOSError, Operations, fuse_get_context

from . import config
from . import tier


def _patch_fusepy_critical_handler() -> None:
    """Repair fusepy's own crash handler, which is broken in the released library.

    fusepy 3.0.1 (the newest release; the bug is unfixed upstream) declares
    `FUSE._wrapper` as a @staticmethod, so it has no `self` -- and then its last-resort
    handler does:

        except BaseException as e:
            self.__critical_exception = e     # <-- NameError: name 'self' is not defined
            log.critical(...)
            fuse_exit()
            return -errno.EFAULT

    Every line after the first is therefore dead. The consequences compound, worst last:

      1. The real exception is destroyed. It becomes the NameError's `__context__` and is
         never recorded, so `FUSE.__init__` cannot re-raise it at the end of the mount
         (fuse.py:708) and the actual fault is simply never reported.
      2. `fuse_exit()` never runs, so FUSE is never told to stop.
      3. `return -errno.EFAULT` never runs, so the ctypes callback returns None where the
         C side expects an int. That is what produces `fuse: read too many bytes` and
         `fuse: writing device: Invalid argument`, and it can take the mount down by
         SIGSEGV -- the wedged-mount failure this repo already has a section about.

    In other words the handler that exists to shut the filesystem down cleanly is the
    thing that corrupts the shutdown. Seen in ~/Library/Logs/MediaFS.err on 2026-08-05,
    with exactly that NameError followed by the read-too-many-bytes/Invalid-argument pair.

    The fix rebinds `_wrapper` as a normal method (so `self` exists -- the call site at
    fuse.py:688 is `partial(self._wrapper, ...)`, which binds correctly either way) and
    delegates to the original for all the behaviour that works, intercepting only the path
    the library gets wrong. Recovering `__context__` is what gets the true exception back.

    Patched here rather than in site-packages on purpose: an edit to the conda env is lost
    the next time the environment is rebuilt, and it is invisible to review. This is
    idempotent and must run before FUSE() is constructed, because the operations table is
    wired up in FUSE.__init__.
    """
    current = getattr(_fusepy.FUSE, "_wrapper", None)
    if current is None or getattr(current, "_mediafs_patched", False):
        return
    original = getattr(current, "__func__", current)   # unwrap if it is a staticmethod

    def _wrapper(self, func, *args, **kwargs):
        try:
            return original(func, *args, **kwargs)
        except BaseException as exc:      # noqa: BLE001 -- the library's own handler failed
            # A NameError from the broken handler carries the exception it was trying to
            # record as its __context__; anything else escaping is itself the fault.
            critical = exc
            if isinstance(exc, NameError) and exc.__context__ is not None:
                critical = exc.__context__
            # The attribute is name-mangled inside class FUSE (fuse.py:645/708 read it as
            # self.__critical_exception), so it must be set under its mangled name from out
            # here for __init__ to find and re-raise it.
            self._FUSE__critical_exception = critical
            logging.critical("Critical exception from FUSE operation %s; aborting mount: %r",
                             getattr(func, "__name__", func), critical)
            try:
                _fusepy.fuse_exit()
            except Exception:             # noqa: BLE001 -- never mask the real fault
                logging.exception("fuse_exit() failed while aborting the mount")
            return -errno.EFAULT

    _wrapper._mediafs_patched = True
    _fusepy.FUSE._wrapper = _wrapper


_patch_fusepy_critical_handler()


def _iso_to_ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


# --- prober identification ----------------------------------------------------
# Cached per PID: this is consulted on EVERY cold read, and a read is ~128 KB, so shelling out
# to `ps` each time would cost more than the byte serving. PIDs are recycled by the OS, so the
# cache is bounded and entries expire -- a stale hit would at worst misjudge one short-lived
# process, and the cache is only ever a fast path for a decision that is itself advisory.
_PROC_CACHE: dict[int, tuple[float, bool]] = {}
_PROC_CACHE_TTL_SEC = 30
_PROC_CACHE_MAX = 512
_PROC_CACHE_LOCK = threading.Lock()
_PROBER_DENIED_N = 0
_PROBER_DENIED_LOGGED_AT = 0.0
_PROBER_DENIED_LOGGED_N = 0
_PROBER_LOG_INTERVAL_SEC = 60


def _is_prober_pid(pid: int) -> bool:
    """True if `pid`'s executable basename is one of config.MEDIAFS_PROBER_NAMES."""
    try:
        out = subprocess.run(["/bin/ps", "-o", "comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    if not out:
        return False
    return os.path.basename(out.split()[0]) in config.MEDIAFS_PROBER_NAMES


def _caller_is_prober() -> bool:
    """Whether the process issuing the current FUSE request is a metadata prober.

    Fails OPEN on any error (returns False -> serve the read). A misidentification that denies
    a legitimate reader would look like corrupt media; one that allows a prober merely costs
    bandwidth, which is the far cheaper mistake.
    """
    if not config.MEDIAFS_DENY_PROBER_READS:
        return False
    try:
        pid = fuse_get_context()[2]
    except Exception:                                                   # noqa: BLE001
        return False
    if not pid:
        return False
    now = time.time()
    with _PROC_CACHE_LOCK:
        hit = _PROC_CACHE.get(pid)
        if hit is not None and now - hit[0] < _PROC_CACHE_TTL_SEC:
            return hit[1]
    verdict = _is_prober_pid(pid)
    with _PROC_CACHE_LOCK:
        if len(_PROC_CACHE) >= _PROC_CACHE_MAX:
            _PROC_CACHE.clear()
        _PROC_CACHE[pid] = (now, verdict)
    return verdict


def _log_prober_denied(path: str) -> None:
    """Report denials as a periodic ROLLING COUNT, not one line each.

    A library scan probes every cold file in the pool, so per-path logging emits ~10k lines and
    buries everything else -- against this project's rule that the log stays signal-dense. The
    FIRST denial earns a full line (it is what explains the EIO if you are staring at Jellyfin);
    after that a count every minute carries the same information in a fraction of the space.
    """
    global _PROBER_DENIED_N, _PROBER_DENIED_LOGGED_AT, _PROBER_DENIED_LOGGED_N
    now = time.time()
    with _PROC_CACHE_LOCK:
        _PROBER_DENIED_N += 1
        first = _PROBER_DENIED_N == 1
        if not first and now - _PROBER_DENIED_LOGGED_AT < _PROBER_LOG_INTERVAL_SEC:
            return
        since = _PROBER_DENIED_N - _PROBER_DENIED_LOGGED_N
        total = _PROBER_DENIED_N
        _PROBER_DENIED_LOGGED_AT = now
        _PROBER_DENIED_LOGGED_N = _PROBER_DENIED_N
    if first:
        logging.info(f"refused a prober read of cold pool file '{path}' -- it would hydrate the "
                     f"file for metadata only (see MEDIAFS_DENY_PROBER_READS); further denials "
                     f"are reported as a periodic count")
    else:
        logging.info(f"refused {since} more prober read(s) of cold pool files ({total} total); "
                     f"latest '{path}'")


# Every operation the kernel can call into. `init`/`destroy` are fusepy's own and excluded.
_FUSE_OPS = (
    "getattr", "readdir", "open", "create", "read", "write", "truncate", "flush", "fsync",
    "release", "unlink", "mkdir", "rmdir", "rename", "utimens", "chmod", "chown", "statfs",
    "access", "readlink", "symlink", "link", "mknod",
)


def _mount_safe(func):
    """Turn an unexpected exception into an errno, because fusepy turns it into an UNMOUNT.

    `_patch_fusepy_critical_handler` above repairs how the library ABORTS; this stops the
    abort from being reached. fusepy reduces an exception to a return code with
    `if e.errno > 0`. An OSError carrying no errno makes that comparison raise TypeError,
    and a non-OSError never reaches it at all -- either way the operation lands in the
    critical handler, `fuse_exit()` runs, and the whole filesystem goes away.

    On 2026-09-01 that happened for real. One 64 KB ranged read of one Futurama episode
    timed out against a VPN exit node that had stopped carrying traffic, `_ranged_fetch`
    raised an errno-less OSError, and the ENTIRE library unmounted -- after which Jellyfin,
    whose library path is that mount, saw an empty disk, logged every title as "cannot be
    found", and began pruning the collections pointing at them.

    That raise now carries EIO, but the errno was never the real hazard: ANY exception on
    ANY FUSE thread from ANY future code path has this same blast radius, and a library
    whose availability depends on ~800 flaky network remotes will keep producing them. So
    the boundary is closed here. A failed operation costs exactly that operation.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except FuseOSError:
            raise                                   # a deliberate errno; fusepy handles it
        except OSError as exc:
            if exc.errno:
                raise FuseOSError(exc.errno) from None
            logging.error(f"mediafs: {func.__name__} raised an errno-less OSError; "
                          f"answering EIO: {exc}")
            raise FuseOSError(errno.EIO) from None
        except Exception as exc:                                          # noqa: BLE001
            logging.exception(f"mediafs: {func.__name__} raised {type(exc).__name__}; "
                              f"answering EIO rather than unmounting the library: {exc}")
            raise FuseOSError(errno.EIO) from None
    return wrapper


class MediaFS(Operations):
    def __init__(self, lower: Path, inventory: dict):
        self.lower = Path(lower)
        self.uid = os.getuid()
        self.gid = os.getgid()
        # Paths deleted THROUGH THE MOUNT whose removal has not yet reached the on-disk
        # inventory. Load-bearing for the reload below: a real delete pops the key from the
        # in-memory view and queues it for the reaper, but remote_inventory.json still lists
        # it until the reaper purges the pool copy and the next scan rewrites the file. Without
        # this set, the very next reload would read that stale entry back and RESURRECT a title
        # the user just deleted -- turning a bug fix into a data-visibility bug.
        self._tombstones: set[str] = self._pending_deletions()
        self._del_lock = threading.Lock()   # guards inv/dirs mutation + queue append
        # Inventory reload bookkeeping (see _maybe_reload_inventory).
        self._inv_path = Path(config.REMOTE_INVENTORY_PATH)
        self._inv_stamp = self._inv_stat()
        self._inv_checked_at = time.monotonic()
        self._build_index(inventory)
        # External library drives (read-through secondary lowers): media that lives on an
        # attached drive is served from the drive (fast, local) in preference to streaming
        # the pool copy; anything only in the pool streams as normal. The drive list is
        # re-discovered periodically so a freshly-plugged drive is picked up and a lost one
        # is dropped (its files then serve from the pool). Access is defensive -- a wedged
        # or vanishing drive is skipped, never allowed to hang or crash a read.
        self._drives = []
        self._drives_at = 0.0
        self._drives_lock = threading.Lock()
        self._current_drives()
        # Open-file handle registry: FUSE fh -> ('fd', real_fd) for real files in
        # `lower` (metadata/hot media, read-write) or ('stream', _Stream) for a cold
        # file being progressively streamed. read/write/release dispatch on the kind.
        self._handles: dict[int, tuple] = {}
        self._handle_seq = 0
        self._handle_lock = threading.Lock()
        logging.info(f"mediafs: {len(self.inv)} inventory paths, lower={self.lower}")

    @staticmethod
    def _pending_deletions() -> set[str]:
        """Paths deleted through the mount whose reaper purge has not completed yet.

        Seeds the tombstone set at startup, which closes the one window an in-memory-only set
        leaves open: mediafs restarts (a crash, a `KeepAlive` remount, a deploy) between the
        user deleting a title and the reaper purging it. The on-disk inventory still lists that
        path for the whole of that interval -- the reaper only prunes `remote_inventory.json`
        *after* a successful purge -- so a fresh process reads the entry back and re-presents a
        title the user deleted. Restarts are routine here, so this is not a corner case.

        Both files are read because the reaper claims the queue by ATOMIC RENAME:
        `mediafs_deletions.jsonl` is what mediafs is appending to now, and
        `...jsonl.processing` is a batch already claimed but not yet purged (or left behind by
        a reaper crash, which it re-adopts on its next tick). A path in either is still live.

        Nothing to prune here: once the reaper finishes it deletes the `.processing` file AND
        prunes the inventory, so the entry disappears from disk and no tombstone is needed to
        keep it hidden. The set is naturally bounded by what is genuinely in flight.
        """
        pending: set[str] = set()
        q = Path(config.MEDIAFS_DELETIONS_QUEUE)
        for path in (q, q.with_name(q.name + ".processing")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue                        # absent is the normal case
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    key = json.loads(line).get("path")
                except (ValueError, AttributeError):
                    continue                    # a torn final line from a crash mid-append
                if key:
                    pending.add(key)
        if pending:
            logging.info(f"mediafs: {len(pending)} deletion(s) still awaiting reaper purge; "
                         f"keeping them hidden across this restart")
        return pending

    def _build_index(self, inventory: dict) -> None:
        """(Re)build every lookup structure from a raw inventory dict.

        Called at startup and on every reload. Builds new dicts and swaps them in rather than
        mutating the live ones, so a lock-free reader holds a complete old view or a complete
        new one -- never a half-rebuilt directory tree.
        """
        # Present only the library prefixes; drop inventory trees like
        # metadata-backup/ that must never appear in the Jellyfin/YacReader view.
        # Tombstoned paths are dropped too -- see self._tombstones.
        inv = {k: v for k, v in inventory.items()
               if k.startswith(config.MEDIAFS_PREFIXES) and k not in self._tombstones}
        # Build the inventory directory tree: for each relative path, register every
        # ancestor directory -> child-name edge, so readdir/getattr can answer purely
        # from memory (no remote listing, ever).
        dirs: dict[str, set] = {}
        for key in inv:
            parts = key.split("/")
            for i in range(len(parts)):
                parent = "/".join(parts[:i])       # "" == root
                child = parts[i]
                dirs.setdefault(parent, set()).add(child)
        # Unicode normalization-insensitive indexes. macOS apps (Qt/YacReader, Infuse)
        # request accented paths in NFC, while the inventory/MEGA store them in NFD (or a
        # mix) -- so a raw dict lookup MISSES and the file is invisible: "library folder
        # doesn't exist" / read timeouts on any accented title (Nausicaä, Pokémon, ...).
        # Map each path's canonical (NFC) form to its ORIGINAL inventory key, so a lookup
        # in EITHER normalization resolves, while tier/rclone still fetch the exact stored
        # name. (Verified 0 NFC collisions across the inventory, so this is unambiguous.)
        inv_canon = {self._canon(k): k for k in inv}
        dirs_canon = {self._canon(d): d for d in dirs}
        # Publish. Each assignment is atomic, and resolution structures go first so a reader
        # in the gap can still resolve a path it just saw listed.
        self.inv = inv
        self._inv_canon = inv_canon
        self.dirs = dirs
        self._dirs_canon = dirs_canon

    def _inv_stat(self) -> tuple:
        """(mtime_ns, size) of remote_inventory.json, or (0, 0) if it cannot be stat'd."""
        try:
            st = self._inv_path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return (0, 0)

    def _maybe_reload_inventory(self) -> None:
        """Re-read remote_inventory.json when it changes on disk.

        WHY THIS EXISTS. The inventory was read exactly once, at mount time, and the view was
        treated as immutable for the life of the process. That silently loses files, because
        three independent daemons race across one library:

            1. media_sync uploads a title and adds it to the inventory.
            2. predownload sees it is now pool-resident, evicts the local bytes to reclaim
               space, and logs "safe on <remote>" -- correctly, by its own rules.
            3. mediafs never learned step 1, so it will not present the pool copy.

        The bytes are on the pool and the local copy is gone, so the title vanishes from
        Jellyfin/YacReader until the process happens to restart. Observed on
        `Kim Possible Movie - So the Drama (2005).mp4`: uploaded 20:15, evicted 20:16, absent
        from the mount at 20:19 while sitting safely on automega15 the whole time. Eviction is
        continuous, so any title ingested during one mediafs lifetime is exposed to this.

        Change is detected by (mtime_ns, size) rather than a content hash: re-reading 12 MB of
        JSON to decide whether to re-read it defeats the purpose, and _persist_inventory now
        publishes via os.replace, so the stamp changes exactly once per complete new version.

        Defensive on every axis, because this runs on the FUSE read path:
          * The stat is throttled to once per MEDIAFS_INVENTORY_POLL_SEC, so a directory walk
            of thousands of entries costs one stat, not thousands.
          * A failed or unparseable read keeps the current view and simply retries later. The
            old view is always serviceable; a half-read one is not.
          * Tombstones are re-applied on every rebuild, so a reload cannot resurrect a title
            deleted through the mount whose reaper purge has not landed yet.
        """
        now = time.monotonic()
        if now - self._inv_checked_at < config.MEDIAFS_INVENTORY_POLL_SEC:
            return
        self._inv_checked_at = now
        stamp = self._inv_stat()
        if stamp == self._inv_stamp or stamp == (0, 0):
            return
        try:
            raw = tier.load_inventory()
        except (OSError, ValueError) as e:
            # Torn or missing read -- keep serving the current view and try again next poll.
            logging.warning(f"mediafs: inventory reload failed, keeping current view ({e})")
            return
        if not raw:
            logging.warning("mediafs: inventory reload returned nothing; keeping current view")
            return
        with self._del_lock:            # serialize against a concurrent mount-delete
            before = len(self.inv)
            self._build_index(raw)
            after = len(self.inv)
        self._inv_stamp = stamp
        if after != before:
            logging.info(f"mediafs: inventory reloaded, {before} -> {after} paths "
                         f"({len(self._tombstones)} tombstoned)")

    _access_last: dict = {}
    _access_lock = threading.Lock()

    def _record_access(self, rel: str) -> None:
        """Append an interactive open to the access log (best-effort), throttled per path
        so a player that reopens a file repeatedly doesn't spam it. The pre-download daemon
        tails this to learn what you just started -- the one signal that covers comics too."""
        now = time.time()
        with self._access_lock:
            if now - self._access_last.get(rel, 0.0) < 30:
                return
            self._access_last[rel] = now
        try:
            with config.PREDOWNLOAD_ACCESS_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"rel": rel, "ts": now}) + "\n")
        except OSError:
            pass

    def _register(self, value: tuple) -> int:
        with self._handle_lock:
            self._handle_seq += 1
            h = self._handle_seq
            self._handles[h] = value
            return h

    # --- path helpers --------------------------------------------------------

    def _rel(self, path: str) -> str:
        return path.lstrip("/")

    def _lower(self, rel: str) -> Path:
        return self.lower / rel

    def _current_drives(self) -> list:
        """The attached external library drives, re-discovered at most every 60s (so a
        newly-plugged drive appears and a lost one drops out). Never raises."""
        now = time.time()
        with self._drives_lock:
            if now - self._drives_at >= 60 or not self._drives_at:
                try:
                    self._drives = config.discover_library_drives()
                except Exception:                       # noqa: BLE001
                    self._drives = []
                self._drives_at = now
            return list(self._drives)

    def _fallback(self, rel: str):
        """The on-drive path for `rel` if it exists on any attached drive, else None.
        Consulted for media served from a physical drive rather than the pool. Drive access
        is defensive: a wedged/vanishing drive is skipped, never allowed to hang the read."""
        for d in self._current_drives():
            try:
                p = d / rel
                if p.exists():
                    return p
            except OSError:
                continue
        return None

    @staticmethod
    def _canon(s: str) -> str:
        """Canonical (NFC) form used only for normalization-insensitive MATCHING."""
        return unicodedata.normalize("NFC", s)

    def _resolve_file(self, rel: str):
        """Return the ORIGINAL inventory key for `rel` regardless of its Unicode
        normalization (NFC/NFD), or None if it is not an inventory file."""
        if rel in self.inv:
            return rel
        return self._inv_canon.get(self._canon(rel))

    def _resolve_dir(self, rel: str):
        """Return the ORIGINAL inventory-dir key for `rel` regardless of normalization,
        or None if it is not an inventory directory."""
        if rel in self.dirs:
            return rel
        return self._dirs_canon.get(self._canon(rel))

    def _is_inv_file(self, rel: str) -> bool:
        return self._resolve_file(rel) is not None

    def _is_inv_dir(self, rel: str) -> bool:
        return self._resolve_dir(rel) is not None

    # --- attributes ----------------------------------------------------------

    def getattr(self, path, fh=None):
        # Hooked on getattr and readdir because they are what discovery actually goes through:
        # a Jellyfin scan, a Finder listing, and an Infuse browse all land here, so a title that
        # appeared on the pool since mount time becomes visible on the first look at it. The
        # call is throttled internally, so the common case is one clock read.
        self._maybe_reload_inventory()
        rel = self._rel(path)
        if path == "/" or rel == "":
            return self._dir_attr()

        lower_p = self._lower(rel)
        if lower_p.exists():
            st = lower_p.lstat()
            return {k: getattr(st, k) for k in (
                "st_mode", "st_nlink", "st_size", "st_uid", "st_gid",
                "st_atime", "st_mtime", "st_ctime")}

        key = self._resolve_file(rel)          # normalization-insensitive (NFC/NFD)
        entry = self.inv.get(key) if key else None   # .get(): safe against a concurrent delete pop
        if entry is not None:
            _remote, iso, size = entry
            ts = _iso_to_ts(iso)
            return {
                "st_mode": statmod.S_IFREG | 0o444,
                "st_nlink": 1,
                "st_size": size,
                "st_uid": self.uid, "st_gid": self.gid,
                "st_atime": ts, "st_mtime": ts, "st_ctime": ts,
            }

        if self._is_inv_dir(rel) or lower_p.is_dir():
            return self._dir_attr()

        fb = self._fallback(rel)               # still-migrating content on the old drive
        if fb is not None:
            if fb.is_dir():
                return self._dir_attr()
            st = fb.lstat()
            return {k: getattr(st, k) for k in (
                "st_mode", "st_nlink", "st_size", "st_uid", "st_gid",
                "st_atime", "st_mtime", "st_ctime")}

        raise FuseOSError(errno.ENOENT)

    def _dir_attr(self):
        now = 0.0
        return {
            "st_mode": statmod.S_IFDIR | 0o555,
            "st_nlink": 2,
            "st_size": 0,
            "st_uid": self.uid, "st_gid": self.gid,
            "st_atime": now, "st_mtime": now, "st_ctime": now,
        }

    def readdir(self, path, fh):
        self._maybe_reload_inventory()
        rel = self._rel(path)
        names = set()
        lower_p = self._lower(rel)
        if lower_p.is_dir():
            try:
                names |= set(os.listdir(lower_p))
            except OSError:
                pass
        d = self._resolve_dir(rel)             # normalization-insensitive (NFC/NFD)
        if d is not None:
            names |= self.dirs.get(d, set())
        for d in self._current_drives():       # merge content held on attached drives
            try:
                fbp = d / rel
                if fbp.is_dir():
                    names |= set(os.listdir(fbp))
            except OSError:
                continue
        return [".", ".."] + sorted(names)

    # --- reads (the hydrate trigger) -----------------------------------------

    def open(self, path, flags):
        rel = self._rel(path)
        lower_p = self._lower(rel)
        if lower_p.exists():
            # Real file in `lower` (the SSD library root): honor the requested mode. This is how
            # Jellyfin's NFO/artwork writes land on the real drive -- metadata is
            # never virtual, so it is always read-write passthrough.
            return self._register(("fd", os.open(lower_p, flags)))
        if flags & (os.O_WRONLY | os.O_RDWR):
            # A new file being written (a fresh .nfo/poster) not yet in lower.
            lower_p.parent.mkdir(parents=True, exist_ok=True)
            return self._register(("fd", os.open(lower_p, flags | os.O_CREAT)))
        # Migration window: if the file is still present on the old drive, serve it LOCALLY
        # (read-only passthrough) IN PREFERENCE to streaming the pool copy. It's faster (no
        # download, no MEGA throttle), it doesn't set the streaming flag (so the sync/pre-
        # download daemons keep draining), and -- critically -- it stops a library scan
        # (YacReader/Jellyfin reading every file) from hydrating the entire pool. Once the
        # drive is drained/removed, this path is gone and reads stream from the pool.
        fb = self._fallback(rel)
        if fb is not None and fb.is_file():
            return self._register(("fd", os.open(fb, os.O_RDONLY)))
        key = self._resolve_file(rel)          # normalization-insensitive (NFC/NFD)
        if key is not None:
            # ALREADY FULLY CACHED -> plain read-only passthrough from the cache file, not the
            # streaming path. Two reasons this matters beyond the obvious speed win:
            #   * The bytes are on local disk, so there is nothing to protect against -- and
            #     the prober denial below must NOT fire for a file that is free to read. Without
            #     this branch a hydrated file was still opened as a "stream" and its probe was
            #     refused, which is how a file the pre-downloader had helpfully cached ended up
            #     with no stream info in Jellyfin.
            #   * It skips the whole segment/worker apparatus for a file that needs none.
            entry = self.inv.get(key)
            if entry is not None and tier.is_cached(key, entry[2]):
                return self._register(("fd", os.open(tier.cache_path_for(key), os.O_RDONLY)))
            # Refuse a metadata prober HERE rather than at the first read. Denying only in
            # read() is too late: by then open() has already started a stream worker (which
            # immediately fetches the head AND tail priority segments, ~8 MB), fired
            # trigger_prefetch (pulling the NEXT episodes), and recorded an access that skews
            # the pre-downloader's prediction. Across a full library scan of ~10k cold files
            # that is tens of GB hydrated and a poisoned prediction signal, for metadata
            # nobody asked for. Failing at open() costs nothing and side-effects nothing.
            if _caller_is_prober():
                _log_prober_denied(path)
                raise FuseOSError(errno.EIO)
            # Cold media payload not on the local drive: STREAM it from the pool. open()
            # returns immediately with a stream handle; the bytes fill in the background and
            # read() serves the prefix as it arrives (or ranged-fetches ahead of it).
            # Pass the ORIGINAL key so tier/rclone fetch the exact stored pool name.
            stream = tier.get_stream(key, inventory=self.inv)
            if stream is None:
                raise FuseOSError(errno.EIO)
            # Predictive prefetch: pull the next few episodes/volumes into cache now so
            # they are fully local (instant, seek-anywhere) by the time you get to them.
            tier.trigger_prefetch(key, self.inv)
            # Record the open so the pre-download daemon knows what you just started
            # (universal signal -- covers comics, which Jellyfin does not track).
            self._record_access(key)
            return self._register(("stream", stream))
        raise FuseOSError(errno.ENOENT)

    def create(self, path, mode, fi=None):
        lower_p = self._lower(self._rel(path))
        lower_p.parent.mkdir(parents=True, exist_ok=True)
        return self._register(("fd", os.open(lower_p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)))

    def read(self, path, size, offset, fh):
        entry = self._handles.get(fh)
        if entry is None:
            raise FuseOSError(errno.EBADF)
        kind, obj = entry
        if kind == "fd":
            return os.pread(obj, size, offset)   # atomic positioned read (thread-safe)
        # A pool-only (cold) read. open() already refuses probers (that is where the denial
        # belongs, before any side effects), so this is only a backstop for a handle opened by
        # one process and read by a prober. Only reached for cold files -- anything local
        # (passthrough or fully cached) took the "fd" branch above and is never affected.
        if _caller_is_prober():
            _log_prober_denied(path)
            raise FuseOSError(errno.EIO)
        return tier.read_stream(obj, offset, size)

    def write(self, path, data, offset, fh):
        entry = self._handles.get(fh)
        if entry is None or entry[0] != "fd":
            raise FuseOSError(errno.EROFS)       # media payloads are read-only
        return os.pwrite(entry[1], data, offset)

    def truncate(self, path, length, fh=None):
        lower_p = self._lower(self._rel(path))
        entry = self._handles.get(fh) if fh is not None else None
        if entry is not None and entry[0] == "fd":
            os.ftruncate(entry[1], length)
            return
        fd = os.open(lower_p, os.O_RDWR)
        try:
            os.ftruncate(fd, length)
        finally:
            os.close(fd)

    def flush(self, path, fh):
        entry = self._handles.get(fh)
        if entry is not None and entry[0] == "fd":
            try:
                os.fsync(entry[1])
            except OSError:
                pass
        return 0

    def fsync(self, path, datasync, fh):
        return self.flush(path, fh)

    def release(self, path, fh):
        with self._handle_lock:
            entry = self._handles.pop(fh, None)
        if entry is None:
            return 0
        kind, obj = entry
        if kind == "fd":
            try:
                os.close(obj)
            except OSError:
                pass
        else:
            tier.release_stream(obj)
        return 0

    # --- mutation: passthrough to `lower` (metadata is always real) ----------

    def unlink(self, path):
        rel = self._rel(path)
        lower_p = self._lower(rel)
        # Remove the real file from the primary lower if it's there (hot media) -- or the
        # metadata sidecar (.nfo/artwork), which is always real and just passes through.
        if lower_p.exists():
            os.unlink(lower_p)
        # Also remove the migration-fallback copy (still-migrating content on the old drive),
        # so a through-the-mount delete removes the file EVERYWHERE and the drain can't
        # resurrect it by re-uploading a leftover copy.
        for d in self._current_drives():       # a mount-delete removes the file everywhere
            try:
                fb = d / rel
                if fb.is_file():
                    fb.unlink()
            except OSError:
                continue
        # If it's a tracked media payload (in the inventory), this is a REAL library
        # delete (you removed the title in Jellyfin/Infuse) -- NOT an eviction. Stop
        # presenting it immediately, drop any hydrated cache copy, and queue it for
        # the reaper to purge from the MEGA pool + metadata backup + state. (Eviction,
        # by contrast, removes the local SSD bytes but leaves the inventory entry, so
        # the file stays presented and re-hydratable -- and is never queued here.)
        key = self._resolve_file(rel)          # normalization-insensitive (NFC/NFD)
        if key is not None:
            with self._del_lock:
                # Tombstone FIRST: remote_inventory.json still lists this path until the
                # reaper purges the pool copy and a scan rewrites the file, so without this
                # the next reload would read the stale entry back and undo the delete.
                self._tombstones.add(key)
                self.inv.pop(key, None)                    # stop presenting (in-memory)
                self._inv_canon.pop(self._canon(key), None)
                parent = key.rsplit("/", 1)[0] if "/" in key else ""
                leaf = key.rsplit("/", 1)[-1]
                if parent in self.dirs:
                    self.dirs[parent].discard(leaf)
                try:
                    config.MEDIAFS_DELETIONS_QUEUE.parent.mkdir(parents=True, exist_ok=True)
                    with config.MEDIAFS_DELETIONS_QUEUE.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"path": key}) + "\n")   # original key == MEGA name
                except OSError as e:
                    logging.error(f"mediafs: failed to queue deletion of {key}: {e}")
            cache_p = config.TIER_CACHE_DIR / key
            try:
                if cache_p.exists():
                    cache_p.unlink()
            except OSError:
                pass
            logging.info(f"mediafs: queued real delete of '{key}' for reaper purge")

    def mkdir(self, path, mode):
        os.makedirs(self._lower(self._rel(path)), mode, exist_ok=True)

    def rmdir(self, path):
        lower_p = self._lower(self._rel(path))
        if lower_p.is_dir():
            os.rmdir(lower_p)

    def rename(self, old, new):
        os.rename(self._lower(self._rel(old)), self._lower(self._rel(new)))

    def utimens(self, path, times=None):
        lower_p = self._lower(self._rel(path))
        if lower_p.exists():
            os.utime(lower_p, times)

    def chmod(self, path, mode):
        lower_p = self._lower(self._rel(path))
        if lower_p.exists():
            os.chmod(lower_p, mode)

    def chown(self, path, uid, gid):
        lower_p = self._lower(self._rel(path))
        if lower_p.exists():
            os.chown(lower_p, uid, gid)


# Wrapped after the class body rather than by decorating each method, so the guard cannot be
# forgotten on one operation: every name in _FUSE_OPS that MediaFS implements gets it.
for _op in _FUSE_OPS:
    _fn = getattr(MediaFS, _op, None)
    if callable(_fn):
        setattr(MediaFS, _op, _mount_safe(_fn))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Mount the virtual media filesystem.")
    ap.add_argument("mountpoint", help="where to mount")
    ap.add_argument("--lower", default=str(config.MEDIAFS_LOWER),
                    help="real dir whose files pass through (metadata + pinned/cached media)")
    ap.add_argument("--foreground", action="store_true", help="run in the foreground")
    args = ap.parse_args()

    inv = tier.load_inventory()
    fs = MediaFS(Path(args.lower), inv)
    # Multi-threaded: a cold-file hydrate (which blocks its worker for the whole
    # download) must NOT stall scans, metadata reads, or playback of already-cached
    # files. Reads/writes use os.pread/os.pwrite (no shared seek pointer) so they
    # are safe under concurrency; getattr/readdir touch only immutable init-time
    # state; tier.hydrate is per-path locked and globally throttled. Writable so
    # Jellyfin's NFO/artwork saves pass through to `lower` (the real drive).
    FUSE(fs, args.mountpoint, foreground=args.foreground, nothreads=False,
         allow_other=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
