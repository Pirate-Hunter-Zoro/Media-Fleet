"""Thin wrapper over the qBittorrent Web API (via qbittorrent-api).

Responsibilities kept here so the engine never speaks HTTP directly:
  * compute a torrent's v1 info hash from its .torrent file (so we can track it
    in qBittorrent regardless of what torrents_add returns),
  * (re)launch the GUI app if the WebUI is unreachable,
  * add a torrent paused (so we can read its size before committing disk),
  * report progress / completion,
  * locate the downloaded content on disk,
  * remove a torrent and its files.

All torrents we add carry config.QBT_CATEGORY so we only ever act on our own.
"""

import hashlib
import re
import subprocess
import time

import qbittorrentapi

import config


# --- info hash from the .torrent file ---------------------------------------

def _bdecode(data, i):
    """Minimal bencode decoder. Returns (value, next_index).

    Only used to locate the raw bytes of the `info` dict; correctness on the
    surrounding structure is all we need.
    """
    c = data[i:i + 1]
    if c == b"i":                                  # integer: i<num>e
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c.isdigit():                                # byte string: <len>:<bytes>
        colon = data.index(b":", i)
        length = int(data[i:colon])
        start = colon + 1
        return data[start:start + length], start + length
    if c == b"l":                                  # list: l...e
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            val, i = _bdecode(data, i)
            out.append(val)
        return out, i + 1
    if c == b"d":                                  # dict: d(key value)...e
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            key, i = _bdecode(data, i)
            val, i = _bdecode(data, i)
            out[key] = val
        return out, i + 1
    raise ValueError(f"bad bencode at byte {i}")


def info_hash_from_file(torrent_path):
    """SHA1 of the bencoded `info` dict — the v1 info hash qBittorrent keys on."""
    data = torrent_path.read_bytes()
    # Find the top-level dict, then re-scan to capture the raw `info` value bytes.
    if data[:1] != b"d":
        raise ValueError("not a bencoded dict")
    i = 1
    while data[i:i + 1] != b"e":
        key, i = _bdecode(data, i)
        start = i
        _val, i = _bdecode(data, i)
        if key == b"info":
            raw_info = data[start:i]
            return hashlib.sha1(raw_info).hexdigest().lower()
    raise ValueError("no info dict in torrent")


def info_hash_from_magnet(magnet_uri):
    """Parse the v1 info hash out of a magnet URI (`urn:btih:<40-hex>`). A magnet carries no
    `.torrent` metadata, so this is the only stable key until qBittorrent fetches it."""
    m = re.search(r"urn:btih:([0-9a-fA-F]{40})", magnet_uri or "")
    if not m:
        raise ValueError("magnet URI has no v1 btih infohash")
    return m.group(1).lower()


def torrent_name_from_file(torrent_path):
    """Best-effort display name from the .torrent metadata."""
    data = torrent_path.read_bytes()
    meta, _ = _bdecode(data, 0)
    info = meta.get(b"info", {})
    name = info.get(b"name") or meta.get(b"comment") or b""
    try:
        return name.decode("utf-8", "replace")
    except AttributeError:
        return ""


def salvage_from_truncated_file(torrent_path):
    """Best-effort `(name, trackers)` from a `.torrent` whose bencode is truncated.

    A truncated drop still carries its top-level keys intact -- `announce` and
    `announce-list` sit before `info` -- and the info dict's `name` precedes the giant
    `pieces` blob the cut almost always runs through. That is enough to rebuild the drop
    as a magnet: the info hash comes from the filename (the naming convention here), so
    ingest only needs the display name and whatever trackers survive to give qBittorrent
    the best chance of pulling the real metadata from the swarm.

    Never raises: a missing name or empty tracker list is a valid salvage, and a dropped
    error here would take down registration for every following drop.
    """
    try:
        data = torrent_path.read_bytes()
    except OSError:
        return None, []
    if data[:1] != b"d":
        return None, []
    name = None
    trackers = []
    try:
        i = 1
        while i < len(data) and data[i:i + 1] != b"e":
            key, i = _bdecode(data, i)
            if key == b"info":
                i += 1                              # step INTO the info dict
                while i < len(data) and data[i:i + 1] != b"e":
                    ik, i = _bdecode(data, i)
                    if ik == b"pieces":
                        # Do not decode the value: it is the part the cut runs through,
                        # and a length prefix past EOF would poison every following step.
                        break
                    val, i = _bdecode(data, i)
                    if ik == b"name" and isinstance(val, bytes):
                        name = val.decode("utf-8", "replace")
                break
            val, i = _bdecode(data, i)
            if key == b"announce" and isinstance(val, bytes):
                trackers.insert(0, val.decode("utf-8", "replace"))
            elif key == b"announce-list" and isinstance(val, list):
                for tier in val:
                    for url in (tier if isinstance(tier, list) else []):
                        if isinstance(url, bytes):
                            trackers.append(url.decode("utf-8", "replace"))
    except (ValueError, IndexError, TypeError, UnicodeDecodeError):
        pass
    seen = set()
    unique = []
    for url in trackers:
        if url not in seen and url.startswith(("http://", "https://", "udp://",
                                                "ws://", "wss://")):
            seen.add(url)
            unique.append(url)
    return name, unique


def _has_urls(value):
    """True when a bencode value names at least one URL, at any nesting depth
    (`announce` is a string; `announce-list` is a list of tiers of strings)."""
    if isinstance(value, bytes):
        return bool(value)
    if isinstance(value, (list, tuple)):
        return any(_has_urls(v) for v in value)
    return False


def undownloadable_reason(torrent_path):
    """Why this `.torrent` can never find a peer, or None when it has a chance.

    A PRIVATE torrent (info.private == 1) may not use DHT, PeX or LSD -- the spec
    forbids it, and a live probe of this qBittorrent confirms it reports all three as
    "** [DHT] **"/"** [PeX] **"/"** [LSD] **" with "This torrent is private". So its
    trackers (or a web seed) are its ONLY ways to reach data. A private drop carrying
    neither is structurally undownloadable: it sits in qBittorrent until the 24h stall
    clock abandons it, and a re-drop repeats that. Observed on a SpongeBob S16 pack
    whose three discovery rows all read "This torrent is private" and which reported
    `stalled 24h with no progress (no peer activity)` on 2026-09-24.

    Public trackerless drops are NOT refused: DHT finds their peers, and that is the
    normal shape here (`announce` absent, `url-list` empty -- HANDOFF 2026-09-23).

    Never raises: a `.torrent` this cannot parse returns None (fail open; truncated or
    unreadable drops have their own recovery paths).
    """
    try:
        meta, _ = _bdecode(torrent_path.read_bytes(), 0)
        info = meta.get(b"info") if isinstance(meta, dict) else None
        if not isinstance(info, dict):
            return None
        try:
            private = int(info.get(b"private") or 0)
        except (TypeError, ValueError):
            return None
        if private != 1:
            return None
        if (_has_urls(meta.get(b"announce")) or _has_urls(meta.get(b"announce-list"))
                or _has_urls(meta.get(b"url-list"))):
            return None
        return ("private torrent with no tracker and no web seed: DHT, PeX and LSD are "
                "switched off by its private flag, so it can never find a peer; re-create "
                "the .torrent with its announce list -- re-dropping this file cannot help")
    except (OSError, ValueError, IndexError, TypeError):
        return None


def total_size_from_file(torrent_path):
    """Total payload size in bytes from the .torrent metadata (v1 fields).

    Lets the scheduler size a torrent for the disk budget WITHOUT adding it to
    qBittorrent first. Returns None if the size can't be read (e.g. a v2-only
    torrent), in which case the caller falls back to a paused add.
    """
    data = torrent_path.read_bytes()
    meta, _ = _bdecode(data, 0)
    info = meta.get(b"info", {})
    if b"length" in info:                       # single-file torrent
        return int(info[b"length"])
    total = 0
    for f in info.get(b"files", []):            # multi-file torrent
        if _is_padding_file(f):                 # padding is never written to disk
            continue                            # (BEP 47) — don't count it toward size
        total += int(f.get(b"length", 0))
    return total or None


def file_sizes_from_file(torrent_path):
    """Per-file sizes from a `.torrent`'s `info` dict, in torrent order.

    Returns the sizes of every file qBittorrent will list (padding files included, so
    the indices line up with `files()` / `chunk_done`), or None if the metadata can't be
    read. Lets the chunked driver compute "smallest remaining file" from the `.torrent`
    alone -- without adding the torrent to qBittorrent just to ask.
    """
    try:
        data = torrent_path.read_bytes()
        meta, _ = _bdecode(data, 0)
    except (OSError, ValueError, IndexError):
        return None
    info = meta.get(b"info") if isinstance(meta, dict) else None
    if not isinstance(info, dict):
        return None
    if b"length" in info:                       # single-file torrent
        try:
            return [int(info[b"length"])]
        except (ValueError, TypeError):
            return None
    if b"files" not in info:
        return None
    sizes = []
    for f in info.get(b"files", []):
        if not isinstance(f, dict):
            return None
        try:
            sizes.append(int(f.get(b"length", 0)))
        except (ValueError, TypeError):
            return None
    return sizes


def file_list_from_file(torrent_path):
    """`[(relative_path, size)]` for every file a `.torrent` will write, in torrent order.

    The plan-coverage contract's release enumeration for a torrent: it must name every
    file that exists after the download, and nothing that does not. BEP 47 padding
    files are therefore EXCLUDED -- qBittorrent never writes them -- while
    `file_sizes_from_file` keeps them so its indices line up with `files()`. Returns
    None when the metadata cannot be read, which callers treat as "cannot enumerate,
    fail open" rather than "empty release".
    """
    try:
        data = torrent_path.read_bytes()
        meta, _ = _bdecode(data, 0)
    except (OSError, ValueError, IndexError):
        return None
    info = meta.get(b"info") if isinstance(meta, dict) else None
    if not isinstance(info, dict):
        return None
    if b"length" in info:                       # single-file torrent
        name, size = info.get(b"name"), info.get(b"length")
        if not name or size is None:
            return None
        return [(name.decode("utf-8", "replace"), int(size))]
    if b"files" not in info:
        return None
    out = []
    for f in info.get(b"files", []):
        if not isinstance(f, dict) or _is_padding_file(f):
            continue
        parts = [p.decode("utf-8", "replace") for p in (f.get(b"path") or [])
                 if isinstance(p, (bytes, bytearray))]
        if not parts:
            continue
        try:
            size = int(f.get(b"length", 0))
        except (ValueError, TypeError):
            size = None
        out.append(("/".join(parts), size))
    return out or None


def _is_padding_file(f):
    """True for a BEP 47 padding file: a piece-alignment pad qBittorrent counts in
    the .torrent metadata but never writes to disk. Counting it would make the
    on-disk completeness check (ingest._advance_downloading) under-read every
    hybrid torrent and wrongly fail it. Detected by the `attr` flag `p` or the
    conventional `.pad/` path component."""
    attr = f.get(b"attr", b"") or b""
    if b"p" in attr:
        return True
    path = f.get(b"path", []) or []
    return bool(path) and path[0] == b".pad"


# --- client / process management --------------------------------------------

def _launch_app():
    """Bring the qBittorrent GUI app up (WebUI serves only while it runs)."""
    subprocess.run(["/usr/bin/open", "-b", config.QBT_BUNDLE_ID],
                   check=False, capture_output=True)


def connect(launch_if_needed=True, wait_sec=40):
    """Return a logged-in client, launching qBittorrent if the WebUI is down."""
    deadline = time.time() + wait_sec
    launched = False
    last_err = None
    while True:
        client = qbittorrentapi.Client(
            host=config.QBT_HOST,
            port=config.QBT_PORT,
            username=config.QBT_USERNAME or None,
            password=config.QBT_PASSWORD or None,
            VERIFY_WEBUI_CERTIFICATE=False,
            REQUESTS_ARGS={"timeout": 15},
        )
        try:
            client.app_version()          # cheap reachability probe
            _ensure_category(client)
            return client
        except (qbittorrentapi.APIConnectionError, qbittorrentapi.exceptions.Conflict409Error,
                Exception) as exc:                                        # noqa: BLE001
            last_err = exc
            if launch_if_needed and not launched:
                _launch_app()
                launched = True
            if time.time() >= deadline:
                raise RuntimeError(
                    f"qBittorrent WebUI unreachable on "
                    f"{config.QBT_HOST}:{config.QBT_PORT}: {last_err}"
                )
            time.sleep(3)


def _ensure_category(client):
    try:
        cats = client.torrents_categories()
        if config.QBT_CATEGORY not in cats:
            client.torrents_create_category(name=config.QBT_CATEGORY)
    except Exception:                                                     # noqa: BLE001
        pass  # category is a nicety, not load-bearing


# --- torrent operations ------------------------------------------------------

def add(client, torrent_path, save_path, paused=False):
    """Add a torrent (running by default) and return its info hash.

    Sizing is done from the .torrent file up front (total_size_from_file), so we
    normally add straight to running. `paused=True` is the fallback path used only
    when the size couldn't be read from the file and must be read from qBittorrent.
    """
    info_hash = info_hash_from_file(torrent_path)
    client.torrents_add(
        torrent_files=str(torrent_path),
        save_path=str(save_path),
        category=config.QBT_CATEGORY,
        is_paused=paused,             # qbittorrent-api maps this to stop on v5
    )
    # torrents_add returns "Ok."; poll briefly until qBittorrent registers it.
    for _ in range(20):
        if get(client, info_hash) is not None:
            return info_hash
        time.sleep(0.5)
    return info_hash


def add_paused(client, torrent_path, save_path):
    """Back-compat helper: add a torrent paused."""
    return add(client, torrent_path, save_path, paused=True)


def add_magnet(client, magnet_uri, save_path, paused=False):
    """Add a torrent from a magnet URI and return its info hash.

    Same contract as `add()`, but the info hash is parsed from the magnet's `urn:btih`
    (a magnet has no `.torrent` metadata; qBittorrent fetches that from the swarm itself)."""
    info_hash = info_hash_from_magnet(magnet_uri)
    client.torrents_add(
        urls=[magnet_uri],
        save_path=str(save_path),
        category=config.QBT_CATEGORY,
        is_paused=paused,
    )
    # torrents_add returns "Ok."; poll briefly until qBittorrent registers it.
    for _ in range(20):
        if get(client, info_hash) is not None:
            return info_hash
        time.sleep(0.5)
    return info_hash


def get(client, info_hash):
    """Return the torrent info object for a hash, or None if not present."""
    lst = client.torrents_info(torrent_hashes=info_hash)
    return lst[0] if lst else None


def torrent_map(client):
    """Snapshot every torrent in qBittorrent as a single {info_hash: torrent} dict.

    The engine used to resolve a torrent with one `get()` call per hash, which is one
    HTTP request each. With hundreds of chunked torrents that turned `_remaining_budget`
    (itself called once per chunked torrent) into an O(n^2) request storm that made a
    single cycle take hours -- and starved the registration of new drops. One bulk
    `torrents_info()` call per cycle replaces all of those lookups; callers consult the
    map instead of the API.
    """
    return {t.hash: t for t in client.torrents_info()}


def resume(client, info_hash):
    client.torrents_resume(torrent_hashes=info_hash)


def stop(client, info_hash):
    """Halt a torrent's transfers without removing it.

    Used to keep a chunked torrent from seeding. A chunked pack is exempt from the global
    remove-when-finished rule (exempt_from_share_limits) because it must survive between
    waves, and the cost of that exemption is that qBittorrent will happily seed it for the
    days the pack takes to drain. Stopping it whenever no wave is actually in flight keeps
    the torrent registered -- which is all chunking needs -- while uploading nothing.

    qBittorrent 5 renamed pause to stop; qbittorrent-api keeps `torrents_pause` as an
    alias, so prefer the current name and fall back for an older server.
    """
    try:
        client.torrents_stop(torrent_hashes=info_hash)
    except AttributeError:
        client.torrents_pause(torrent_hashes=info_hash)


def is_complete(torrent):
    """True once the payload is fully downloaded."""
    if torrent is None:
        return False
    if torrent.progress is not None and torrent.progress >= 1.0:
        return True
    return torrent.state in {
        "uploading", "stalledUP", "pausedUP", "queuedUP", "forcedUP", "checkingUP",
    }


def content_path(torrent):
    """Absolute path qBittorrent wrote to (a file, or the torrent's root folder)."""
    return torrent.content_path


def remove(client, info_hash, delete_files=True):
    client.torrents_delete(delete_files=delete_files, torrent_hashes=info_hash)


# --- file-level control (for chunked download of oversized torrents) ----------

def files(client, info_hash):
    """List the torrent's files: each has .index, .name, .size, .progress, .priority."""
    try:
        return list(client.torrents_files(torrent_hash=info_hash))
    except Exception:                                                     # noqa: BLE001
        return []


def set_file_priority(client, info_hash, indices, priority):
    """Set download priority for a set of file indices (0 = don't download / skip,
    1 = normal). Used to download a huge torrent in space-bounded waves."""
    idx = [int(i) for i in indices]
    if not idx:
        return
    client.torrents_file_priority(torrent_hash=info_hash, file_ids=idx, priority=priority)


# qBittorrent's action when a torrent hits a share limit. The enum is ordered
# Stop, Remove, RemoveWithContent, EnableSuperSeeding -- so 1 removes the torrent and
# LEAVES THE FILES, which is the only value this pipeline can run under, and 2 would
# delete the payload out from under an ingest that has not filed it yet.
SHARE_LIMIT_ACT_STOP = 0
SHARE_LIMIT_ACT_REMOVE = 1
SHARE_LIMIT_ACT_REMOVE_WITH_CONTENT = 2

# Remove a torrent as soon as it finishes downloading, keeping its files. The whole-torrent
# path reads "gone from qBittorrent" as "finished" and then confirms against the bytes on
# disk, so this is not a preference -- it is the completion signal the engine runs on.
#
# The queueing block is the concurrency ceiling the engine runs under. It is kept GENEROUS
# on purpose: the real admission bound is the ingest's own disk budget, so these values
# only stop qBittorrent from swamping the network with more simultaneous transfers than the
# link can feed. max_active_checking_torrents matters a lot here -- re-adopting a parked
# chunked pack re-checks its files, and checking one-at-a-time serialized hundreds of
# re-adoptions into a multi-hour crawl.
REQUIRED_PREFERENCES = {
    "max_seeding_time_enabled": True,   # retire on seeding time...
    "max_seeding_time": 0,              # ...of zero minutes, i.e. the moment it completes
    "max_ratio_act": SHARE_LIMIT_ACT_REMOVE,
    "queueing_enabled": True,
    "max_active_downloads": 20,
    "max_active_torrents": 100,
    "max_active_uploads": 10,
    "max_active_checking_torrents": 5,
}


def assert_share_limit_policy(client, log_fn=None):
    """Verify -- and repair -- the global share-limit policy the engine depends on.

    Two share-limit failures this catches, both silent and both expensive:

      * The auto-remove rule is off or set to Stop. Finished torrents then seed forever and
        the whole-torrent path never sees the completion it waits for.
      * `max_ratio_act` is RemoveWithContent. qBittorrent would then delete the payload the
        instant the download finishes -- before identify has run, let alone filed anything.
        A stray click in the WebUI is all it takes, and the loss is total and silent.

    It also re-asserts the concurrency ceiling (queueing on, generous active limits) so a
    manual WebUI tweak or a fresh qBittorrent install can't silently collapse the engine
    back to the 5-download default.

    Repairs rather than refuses, because the correct values are known and a daemon that
    declines to run is worse than one that fixes its own environment. Returns the settings
    it had to change, empty when the policy was already correct.
    """
    prefs = client.app_preferences()
    wrong = {k: v for k, v in REQUIRED_PREFERENCES.items() if prefs.get(k) != v}
    if wrong:
        if log_fn:
            was = {k: prefs.get(k) for k in wrong}
            log_fn(f"qBittorrent policy drifted {was} -> {wrong}; correcting. "
                   f"(max_ratio_act must stay {SHARE_LIMIT_ACT_REMOVE} = remove the torrent "
                   f"and KEEP its files; {SHARE_LIMIT_ACT_REMOVE_WITH_CONTENT} would delete "
                   f"the download before it is filed.)")
        client.app_set_preferences(prefs=wrong)
    return wrong


def exempt_from_share_limits(client, info_hash):
    """Pin this torrent's share limits to "no limit" so qBittorrent's global
    auto-remove rule cannot reap it.

    REQUIRED FOR EVERY CHUNKED TORRENT, and load-bearing. qBittorrent is configured
    here to remove a torrent the moment it finishes (`max_seeding_time = 0`,
    `max_seeding_time_enabled = true`, `max_ratio_act = 1`), which is what the
    whole-torrent path relies on to detect completion. But a torrent is "finished"
    to qBittorrent as soon as every *selected* file is complete, and a chunked
    torrent selects one wave at a time — so the pack is reaped at the end of wave 1
    with hundreds of GB still parked at priority 0 and never fetched.

    Per-torrent limits override the global rule: -1 means "no limit" (-2 would mean
    "use the global setting", which is exactly the thing being escaped). The chunked
    driver removes the torrent itself once the last wave is filed.
    """
    try:
        client.torrents_set_share_limits(
            ratio_limit=-1,
            seeding_time_limit=-1,
            inactive_seeding_time_limit=-1,
            torrent_hashes=info_hash,
        )
    except TypeError:
        # Older qBittorrent builds have no inactive-seeding limit; the two that
        # matter are still applied.
        client.torrents_set_share_limits(
            ratio_limit=-1, seeding_time_limit=-1, torrent_hashes=info_hash,
        )
