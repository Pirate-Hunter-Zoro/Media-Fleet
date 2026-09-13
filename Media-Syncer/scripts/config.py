"""
The Scroll of Edicts.

This file contains all user-configurable settings and global constants
for the Media Syncer application. All paths, file type definitions,
and operational parameters are defined here.

Single-host: this daemon runs on the Mac Mini only. The MacBook Air's old
stage-and-upload role is obsolete -- Torrent-Ingest now lands new media directly
on the SSD library root on the Mini, and this daemon replicates it to the MEGA
pool. There is no host-mode toggle anymore; every path assumes the Mini and its
SSD library root (~/Media). (Other machines may still `git pull` this repo to receive
rclone.conf changes, but they do not run the sync daemon.)
"""

import os
import shutil
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# --- USER CONFIGURATION ---
# The local library root on the Mac internal SSD -- where the daemons read/write files
# (Torrent-Ingest and the GetComics ingest land here; Media-Syncer uploads from here).
# It sits BENEATH the mediafs mount: mediafs mounts at MEDIAFS_MOUNT (~/MediaLibrary,
# what Jellyfin/YacReader read) with this dir as its `lower`. New media lands here, is
# uploaded to the MEGA pool, and is KEPT locally (GRADUATED_UPLOAD_THEN_REMOVE = False) --
# the pool copy is durability, not the serving path; a hot file serves straight off the
# SSD. The predictive pre-download daemon (predownload.py) manages SSD space, evicting COLD
# files whose bytes are proven on a remote, and anything evicted is hydrated back on demand
# via mediafs. Metadata sidecars (.nfo/artwork) live here permanently so
# Jellyfin/YacReader keep serving them through the mount. External library DRIVES are an
# optional second tier of local cache (see discover_library_drives); this is the internal
# SSD root and always present.
SSD_LIBRARY_ROOT = Path("/Users/mikeyferguson/Media")

# Where the uploader moves a file that can NEVER be placed: a single file whose size
# exceeds one MEGA account's usable cap (REMOTE_CAP_BYTES - fill margin). One file cannot
# span accounts, so no remote can ever hold it -- and leaving it on the SSD library root
# makes it permanently un-evictable (it is "on no remote", so predownload refuses to evict
# it) while it squats on the serving cache and blocks the disk-budget admission forever.
# The fix is to MOVE it out of the media root (preserving its library-relative path) so it
# stops counting against the library cache, and report it for the owner to re-encode or
# delete. Same volume as SSD_LIBRARY_ROOT, so the move is an instant rename, not a copy.
UNPLACEABLE_DIR = Path("/Users/mikeyferguson/unplaceable_media")

# KEEP local copies after upload on the SSD library root. The pool copy is durability, not
# the serving path, so a hot file serves straight off the SSD and predownload.py manages
# that space by evicting cold, inventory-proven media. (Set True only for an SSD-only
# deployment where local space is too scarce to retain anything.)
GRADUATED_UPLOAD_THEN_REMOVE = False

# EXTERNAL DRIVES are the opposite: a drive is being retired, so its content is deleted once
# the pool provably holds it. The drive empties as the backlog uploads, and "the drive is
# empty" becomes the signal that it can be unplugged for good.
#
# This does NOT apply to SSD_LIBRARY_ROOT -- only to discover_library_drives() roots. The SSD
# is the serving cache and keeps its copy; the drive is the thing being drained.
DELETE_DRIVE_COPY_AFTER_UPLOAD = True

# The guard on that deletion, and it is strict on purpose: the drive copy is removed ONLY
# when its size matches the inventory's recorded size EXACTLY.
#
# An exact match is the only cheap proof that the pool holds *this* file rather than some
# other version of the same path. Two real populations make a looser test dangerous:
#
#   * LARGER on the drive -- a better local encode whose pool copy is a different, worse
#     file. Deleting it destroys the good version, and write-once means the pool will not
#     be corrected by re-upload. These are reported, never deleted.
#   * SMALLER on the drive -- a truncated file, typically a download killed mid-flight, sitting
#     at a real library path where mediafs serves it as valid media. Also reported rather
#     than deleted, because "smaller" alone does not prove which copy is wrong.
#
# Anything that is not an exact match is left on the drive and logged, so the residue that
# blocks the drive from emptying is always visible rather than silently skipped.
DRIVE_DELETE_REQUIRE_EXACT_SIZE = True

# The lone churn class: One Pace ships newer re-cut versions that must replace
# older ones. Any remote path under this prefix is allowed to overwrite-on-newer
# (both directions -- a newer local re-cut replaces the remote copy, a newer
# remote version overwrites the stale local copy). One Pace lives among the
# ordinary Shows in the SSD library root -- only its OVERWRITE behavior is special,
# not its residence. This is the churn test ONLY. Mirrors Torrent-Ingest's
# identical ONE_PACE_PREFIX; keep the two strings in step.
ONE_PACE_PREFIX = "Shows/One Pace (2013)/"

def local_root_for(relative_path) -> Path:
    """The local media root a given remote-relative path hangs off of.

    Everything -- ordinary Shows/Movies/Comics and the One Pace churn class --
    lives beneath SSD_LIBRARY_ROOT (the SSD library root). This is the single source of
    truth for local routing: both the remote->local mapper (get_expected_local_path)
    and the local->remote strip (the upload phase) resolve through here, so the
    two directions can never disagree about where a file lives.
    """
    return SSD_LIBRARY_ROOT

# --- External library drives (disposable local caches that also back up to MEGA) ------
# Any number of external drives can be used as large local media stores. Each holds the
# canonical library under a `Media/` subfolder (same {Shows,Movies,Comics} hierarchy as the
# mount); a legacy `MediaStore/` subfolder and a bare drive root are also accepted for
# back-compat. A drive is used if it carries a `.media-library` marker OR has a Shows/
# folder. Files live on the drive AND are uploaded to the pool, so a drive is disposable:
# lose it and the mount serves everything from the pool instead. When a drive has room it is
# filled with predicted content; a fraction is always kept free.
DRIVE_BUFFER_FRACTION = 0.10        # keep this fraction of each drive free (1/10)
MEDIA_TOP_DIRS = ("Shows", "Movies", "Comics")
DRIVE_LIBRARY_SUBDIR = "Media"      # canonical external-drive library folder name


def discover_library_drives():
    """Return the library root of every attached external drive: a `Media/` subfolder
    (canonical), a legacy `MediaStore/` subfolder, or the drive root -- whichever holds the
    library. Defensive: never raises, skips anything that errors (a wedged or vanishing
    drive) so the caller degrades to pool-only serving."""
    out = []
    vroot = Path("/Volumes")
    try:
        vols = list(vroot.iterdir()) if vroot.exists() else []
    except OSError:
        return out
    for v in vols:
        try:
            if not v.is_dir() or v.resolve() == Path("/"):
                continue           # skip the boot volume symlink
            for root in (v / "Media", v / "MediaStore", v):
                try:
                    if (root / ".media-library").exists() or (root / "Shows").is_dir():
                        out.append(root)
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return out


# --- Drive sets are resolved LIVE, never frozen at import ---------------------
#
# These were module-level lists built by calling discover_library_drives() once at import.
# That silently broke hot-plug for the long-lived daemons: media_sync runs for days, so a
# drive attached after startup was never in SCAN_DIRS, its content was never scanned for
# upload, and in media_sync's upload phase get_root_for_path() returned None for every file
# on it -- which is a bare `continue`, so the files were skipped with no error and no log.
# A whole drive could sit un-backed-up indefinitely while the daemon reported healthy.
# (predownload's fill_drives()/_drive_resident_rels() already called discover_library_drives()
# live each cycle, so the two halves of the system disagreed about which drives existed.)
#
# Now every caller re-resolves. discover_library_drives() stats /Volumes, so results are
# memoised for a few seconds -- long enough that an os.walk driver doesn't re-scan per file,
# short enough that plugging a drive in takes effect within one daemon cycle.
_DRIVE_CACHE_TTL_SEC = 5
_drive_cache: tuple[float, list] = (0.0, [])


def _drives_cached() -> list:
    global _drive_cache
    now = time.monotonic()
    ts, drives = _drive_cache
    if now - ts >= _DRIVE_CACHE_TTL_SEC:
        drives = discover_library_drives()
        _drive_cache = (now, drives)
    return drives


def local_media_roots() -> list:
    """Roots used to recover a remote-relative path from a local file (the upload phase
    strips whichever of these contains the file) -- the SSD library root PLUS every
    currently-attached external drive."""
    return [SSD_LIBRARY_ROOT] + _drives_cached()


def scan_dirs() -> list:
    """Where to look for local files to upload: the SSD library root plus every
    currently-attached external drive (so drive-resident content that isn't on the pool
    yet gets backed up, including on a drive plugged in after the daemon started)."""
    return [SSD_LIBRARY_ROOT] + _drives_cached()

# Provide the FULL path to your rclone executable here.
RCLONE_PATH = "/opt/homebrew/bin/rclone"

# --- Automatic MEGA account provisioning (scripts/mega_accounts.py) -----------
# When the pool's usable free space drops below POOL_LOW_FREE_BYTES, new free-tier accounts
# are created and appended to rclone.conf automatically. Each free account is 20 GB (== the
# REMOTE_CAP_BYTES ceiling; never rely on promo space above that). New accounts are named
# `<ACCOUNT_ALIAS_PREFIX><n>` with the email alias base+<name>@domain (all +aliases land in
# the one base inbox). SAFE: creation is INERT until the base inbox's IMAP app-password is
# present at MEGA_APP_PASSWORD_FILE -- until then the system only MONITORS and logs.
MEGATOOLS_BIN = "/opt/homebrew/bin/megatools"
ACCOUNT_EMAIL_BASE = "mtf6056@gmail.com"      # +aliases (base+automega1@gmail.com, ...)
ACCOUNT_ALIAS_PREFIX = "automega"             # remote name + email-alias tag for new accounts
MEGA_APP_PASSWORD_FILE = Path.home() / ".config" / "media-syncer" / "email_app_password"
POOL_LOW_FREE_BYTES = 200 * 1024**3           # create accounts when pool free < this
ACCOUNTS_PER_LOW_BATCH = 5                     # accounts to create per low-capacity trigger
ACCOUNT_CREATE_MIN_INTERVAL_SEC = 300         # rate-limit between creations (be gentle to MEGA)

def IMAP_HOST_FOR(email: str) -> str:
    dom = email.rsplit("@", 1)[-1].lower()
    if "gmail" in dom:
        return "imap.gmail.com"
    if "icloud" in dom or "me.com" in dom:
        return "imap.mail.me.com"
    return f"imap.{dom}"

# Tailscale executable
TAILSCALE_PATH = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

# --- VPN exit-node country BLOCKLIST ------------------------------------------
# The exit node is machine-global, so routing traffic through a country where an API the
# machine depends on is UNAVAILABLE breaks it for everything, surfacing as API errors.
#
# This conservative list keeps the free-model chain calls away from jurisdictions where API access is
# commonly unavailable while preserving nearly all IP diversity for MEGA throttle avoidance.
#
# Exit nodes are Mullvad, whose hostname begins with the 2-letter country code
# (`us-atl-wg-001` -> us), which is how we filter.
BLOCKED_EXIT_COUNTRIES = {
    "cn",  # China
    "hk",  # Hong Kong
    "mo",  # Macau
    "ru",  # Russia
    "by",  # Belarus
    "ir",  # Iran
    "kp",  # North Korea
    "cu",  # Cuba
    "sy",  # Syria
}

# Cross-process rotation throttle. Changing the exit node is machine-global and resets EVERY
# live TCP connection, including the fleet's the free-model chain calls,
# so rotating on every failed MEGA op (hundreds of times during a scan) is what makes API
# calls die mid-request. Cap actual exit-node switches to at most once per this interval,
# coordinated across all rotating processes (media_sync + mediafs) via a shared timestamp
# file. IP diversity for MEGA throttle avoidance is preserved (still cycles every few min,
# backed by the 390-account per-account quarantine); the connection just stops churning.
ROTATE_MIN_INTERVAL_SEC = 180
ROTATE_STAMP_FILE = Path.home() / "Library" / "Application Support" / "media-syncer" / "last_exit_rotate"

# --- VPN exit-node SPEED allowlist (scripts/benchmark_exit_nodes.py) ----------
# The exit node is the fleet's aggregate upload ceiling, and that ceiling spans more than two
# orders of magnitude across Mullvad's fleet: 26.7 MB/s measured on `us-den-wg-101` against
# 0.060 MB/s on `za-jnb-wg-001`. A slow node does not merely slow the daemon down, it stops it
# COMPLETELY, and the mechanism is worth stating because it is not obvious: every transfer gets
# a wall-clock budget from TIMEOUT(), so once per-stream throughput falls below what that budget
# implies, NOTHING completes. Nothing completing means nothing is recorded as uploaded, so the
# same files are retried next cycle at the same doomed rate. Measured on `za-jnb-wg-001`: 0.86
# MB/s aggregate across all 16 workers, 0 uploads in 1 h 43 m, 56 timeouts an hour, and 69 files
# abandoned -- while the same phase on a healthy node had been clearing 150-280 files an hour.
#
# So rotation is restricted to nodes MEASURED fast, and the measurement lives in a gitignored
# file rather than a constant here, because the fast set is a property of where this machine
# sits on the network -- it does not survive being copied to another host, and it goes stale as
# Mullvad's fleet changes. Re-measure with `python3 -m scripts.benchmark_exit_nodes` (daemon
# stopped) whenever throughput looks wrong; the sweep takes about 25 minutes.
#
# The threshold is a floor on AGGREGATE upload through the node, and 8 MB/s is where it belongs:
# split across UPLOAD_WORKERS it leaves each stream 0.5 MB/s, which is double the 0.25 MB/s
# TIMEOUT_ASSUMED_RATE_BPS the transfer budget is sized from. That gap is the point -- a node
# admitted at exactly the assumed rate would put every transfer on the edge of its own timeout.
FAST_EXIT_MIN_UPLOAD_BPS = 8 * 1024 * 1024      # 8 MB/s aggregate; 2x assumed rate per worker
EXIT_NODE_SPEEDS_FILE = SCRIPT_DIR.parent / "exit_node_speeds.json"    # full ranked sweep
FAST_EXIT_NODES_FILE = SCRIPT_DIR.parent / "fast_exit_nodes.json"      # the live allowlist

# How stale an allowlist may be before it is ignored. Mullvad adds and retires nodes, and a
# months-old list slowly narrows to a handful of IPs that no longer exist -- which reads as
# "rotation is broken" rather than "the measurement expired". Past this age vpn.get_exit_nodes()
# logs and falls back to the country-filtered list, which is slower but never wrong.
FAST_EXIT_MAX_AGE_SEC = 30 * 24 * 3600          # 30 days

# --- Rotate the exit node when uploads are systemically timing out ------------
# A single upload timeout means nothing -- one large file over one throttled MEGA account will
# do that on a perfectly good node, and rotating on it would be actively harmful, since a switch
# resets every TCP connection on the machine and would kill the other 15 in-flight transfers to
# fix one.
#
# N consecutive timeouts with no success in between is a different signal entirely: it means the
# node, not the file, is the problem. At that point the other transfers are dying anyway, so a
# rotation costs nothing and is the only thing that helps. The counter resets on any successful
# upload, so a healthy phase never reaches the threshold no matter how many isolated timeouts it
# accumulates over hours.
#
# 3 is low deliberately. With 16 workers a dead node produces 16 timeouts within a couple of
# minutes of the first, so the threshold is reached almost as soon as the evidence exists, and
# the cost of being wrong is one rotation.
UPLOAD_TIMEOUT_ROTATE_THRESHOLD = 3

# A timeout gets its own per-file budget, separate from MAX_DOWNLOAD_TRIES, because the two
# bound different hazards and collapsing them makes the wrong file get abandoned.
#
# MAX_DOWNLOAD_TRIES exists to stop a POISON file -- a bad read off a dying drive, a path MEGA
# refuses -- from holding a worker in a spin. Those fail in seconds, so a small budget is right.
# A timeout is the opposite shape: it consumed the file's ENTIRE transfer budget without a
# verdict, and what it indicts is the link, not the file. Charging it against the poison-file
# budget means three slow minutes on a bad node abandon a perfectly good file for a cycle that
# runs for days -- which is exactly what happened: 69 files abandoned in one morning.
#
# So a timeout is re-queued without spending an attempt, and bounded here instead. 6 is chosen
# so that a file has to fail across several rotations before it is believed to be the problem:
# on a healthy node an isolated timeout is rare, so six of them against one file means either
# the file is genuinely untransferable or every node is bad, and both deserve to stop.
MAX_UPLOAD_TIMEOUTS_PER_FILE = 6

# --- Tailscale watchdog -------------------------------------------------------
# Tailscale is a single point of failure for the WHOLE fleet: qBittorrent is bound to the
# 100.x CGNAT address (so Torrent-Ingest refuses to download at all when it is gone) and
# every MEGA transfer rides the exit node. The Mac app's network extension can die or lose
# its address with nothing to bring it back -- launchd does not supervise it. So a small
# watchdog polls the condition that actually matters and relaunches the app when it fails.
TS_WATCHDOG_POLL_SEC = 30
TS_WATCHDOG_FAIL_STREAK = 3        # consecutive bad polls before acting (~90s of real outage)
TS_WATCHDOG_RECOVER_GRACE_SEC = 90 # how long to wait for the address to come back after a fix
TS_WATCHDOG_LOG = SCRIPT_DIR.parent / "tailscale_watchdog.log"
# The watchdog's HEARTBEAT: rewritten every poll, healthy or not.
#
# The watchdog repairs but never reported, and those are different jobs. Its log only speaks
# when something is wrong, so "is the VPN okay right now?" could only be answered by the
# ABSENCE of complaints -- which is indistinguishable from the watchdog itself being dead,
# and reads identically whether the tunnel has been healthy for a week or has not been
# looked at since Tuesday. On 2026-09-01 the exit node carried nothing for five hours,
# taking MEGA, mediafs, every torrent and (through the mount) Jellyfin down with it, and
# nothing anywhere said so in words.
#
# So every poll writes state, and the freshness of THIS FILE is what proves the guardian is
# alive. A stale heartbeat is itself the alarm -- the one failure a self-report can never
# make on its own behalf. `fleet_health.py` reads it; `python3 -m scripts.tailscale_watchdog
# --status` prints it.
TS_WATCHDOG_STATUS = SCRIPT_DIR.parent / "tailscale_status.json"
# A heartbeat older than this means the watchdog is not running, whatever it last said.
# Generous against TS_WATCHDOG_POLL_SEC (30 s) so a slow poll is never mistaken for a death.
TS_WATCHDOG_STALE_SEC = 300

# Reachability probe: is traffic actually FLOWING through the selected exit node?
#
# A bound CGNAT address and a selected exit node are both necessary and together still not
# sufficient. A Mullvad node can go dead while remaining selected and "active" -- the
# symptom is tx climbing against rx frozen at zero -- and every local check passes while the
# box has no internet at all: no DNS, no route, torrents stalled, MEGA stalled. Nothing else
# on the machine notices, because everything downstream just sees timeouts.
#
# So the watchdog probes, and the probe targets are load-bearing. They must be:
#   * REACHED THROUGH THE EXIT NODE. split_tunnel.sh pins whatever
#     api.free-model.com resolves to and every Google netblock to the physical gateway -- so
#     probing 8.8.8.8 or the the free-model chain API
#     would succeed via the direct route and report a dead node healthy. Cloudflare and
#     Quad9 are on none of those lists.
#   * ADDRESSED BY IP. DNS is one of the things that dies with the exit node, so a hostname
#     probe cannot distinguish "no exit node" from "no resolver".
# Two providers, so one operator's outage is not read as a dead tunnel.
TS_WATCHDOG_PROBE_IPS = ("1.1.1.1", "9.9.9.9")
TS_WATCHDOG_PROBE_PORT = 443
TS_WATCHDOG_PROBE_TIMEOUT_SEC = 5
# Consecutive failed probes before rotating away. Higher than the address streak on purpose:
# a rotation resets every TCP connection on the box, and mid-rotation the path is legitimately
# dead for a few seconds, so this must not fire on the rotator's own work.
TS_WATCHDOG_PROBE_FAIL_STREAK = 4
# How many DISTINCT exit nodes must be probed dead, with no success in between, before the
# watchdog says so as a WIDESPREAD OUTAGE rather than repeating "this node is dead". Purely
# a reporting threshold: it never stops the sweep, because sweeping the node list is the
# only repair for a dead exit node and on 2026-09-01 it did eventually find a live one.
# Three, because two consecutive dead nodes is still plausibly bad luck and a third is not.
TS_WATCHDOG_DEAD_NODE_LIMIT = 3

# Git executable path
GIT_PATH = "/opt/homebrew/bin/git"

# Wall-clock bound on the top-of-cycle config-propagation `git pull`. It is best-effort --
# a failed pull just means this cycle runs on the local rclone.conf and the next cycle
# tries again -- so it must never be able to stall a cycle. Two minutes is far more than a
# ~100 KB fetch needs and short enough that a hung transport is a blip, not an outage.
GIT_PULL_TIMEOUT_SEC = 120

# --- Log rotation ------------------------------------------------------------
# The repo's own logs are working logs -- read to see what the daemon is doing now -- so
# they are bounded and the oldest slice is allowed to fall off the end. 5 MB x 3 backups
# caps each at 20 MB; media_sync.log sat at 2.3 MB, so a normal window still fits in the
# live file and rotation is a ceiling, not a routine event.
#
# THIS DOES NOT APPLY TO ~/Library/Logs/MediaSync.err, AND MUST NOT.
# That file is launchd's stderr capture, and it is the daemon's ARCHIVE: it retains upload
# history back to install, while this log may start mid-window. The remote-purge runbook
# (see README, "For the remote purge, discover remotes from the upload logs") harvests a
# title's full `Uploading '<path>' to <remote>...` history out of it, and that history is
# what catches remote copies the inventory alone misses -- an inventory-only purge once
# left 49 remotes dirty. Rotating or truncating MediaSync.err would silently delete the
# evidence that procedure depends on, so it is deliberately left unbounded. If it ever has
# to be capped, ARCHIVE it (gzip the old slice and keep it); never drop the oldest.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# --- CORE CONFIGURATION ---
RCLONE_CONF_PATH = SCRIPT_DIR.parent / "rclone.conf"
# The live config every rclone process on the box reads, and the cross-process lock that
# serializes rewrites of it.
#
# THE LOCK PATH IS A CROSS-REPO CONTRACT. Four writers touch this file: mediasync's session
# purge, the account provisioner's append, Torrent-Ingest's comic-migration tools, and
# rclone itself. The first three are ours and must agree on ONE lock path or they do not
# exclude each other at all -- the §4.106 lesson, where two implementations of one lock used
# the same path with different primitives and both sides believed they held it. It lives
# beside the live config rather than inside either repo, because no repo owns it.
# Torrent-Ingest hard-codes the same literal in scripts/migrate_comic_franchises.py.
RCLONE_CONF_LIVE_PATH = Path.home() / ".config/rclone/rclone.conf"
RCLONE_CONF_LOCK_PATH = Path.home() / ".config/rclone/.rclone-conf.lock"
# How long a writer waits for that lock before giving up. A rewrite is a handful of
# milliseconds, so anything near this bound means a holder died badly; refusing to write is
# the safe answer, since a skipped session purge only costs a retry.
RCLONE_CONF_LOCK_TIMEOUT_SEC = 30
LOG_FILE = SCRIPT_DIR.parent / "media_sync.log"
SYNC_STATE_PATH = SCRIPT_DIR.parent / "sync_state.json"
FREE_SPACE_PATH = SCRIPT_DIR.parent / "free_space.json"   # per-remote {total,used,free}

# --- Fleet free-space report (phone-viewable) --------------------------------
# After each sync cycle, a compact free-space summary is written here so you can
# glance at it on your phone (iCloud-synced) and know when to add more MEGA
# accounts. It just READS free_space.json (which the cycle already refreshes), so
# it makes no extra rclone calls and does no logging -- keeping media_sync.log clean.
TORRENTS_ICLOUD_DIR = Path(
    "/Users/mikeyferguson/Library/Mobile Documents/com~apple~CloudDocs/Torrents"
)
FREE_SPACE_REPORT_PATH = TORRENTS_ICLOUD_DIR / "mega_free_space.txt"
FREE_SPACE_LOW_WARN_BYTES = 40 * 1024**3    # total free below this -> "add accounts" warning

# Per-remote usable-capacity cap. A free MEGA account is 20 GB; some accounts
# temporarily report MORE (e.g. a 1-year +5 GB bonus -> 25 GB total), but that
# space evaporates when the bonus expires, so we must NOT rely on it. Cap every
# remote's usable capacity at this value: effective free = max(0, min(total, CAP)
# - used), so used + free is always <= CAP. Applied everywhere free space is
# computed (upload decisions and the phone report).
REMOTE_CAP_BYTES = 20 * 1024**3

# Wall-clock budget for a single rclone transfer, sized from the file.
#
# The budget is NOT a throughput target -- it is the "something is wrong, stop waiting"
# line, so it must sit BELOW the slowest transfer that still legitimately finishes.
# The old numbers (0.5 MB/s, 300 s floor) did not: measuring 396 completed uploads out of
# media_sync.log on 2026-08-06 gave median 2.06 MB/s, p10 0.66 MB/s, min 0.07 MB/s. A
# 0.5 MB/s budget therefore cut into the normal slow tail rather than bounding it, which
# is where 189 logged timeouts came from -- and every one of those discards a transfer
# that was still moving, then re-uploads the whole file from zero next cycle.
#
# Two distinct failure shapes were in that count, so both get fixed:
#   * The floor. `Bleach v26.cbz` (135 MB) needed 270 s by formula, got the 300 s floor,
#     and died at exactly 300 s -- a small file killed by the floor, not by the rate.
#   * The rate. The 1 GB streaming chunk sized to 2048 s and timed out there 34 times,
#     the single largest cluster in the log.
#
# ASSUMED_RATE is set to 0.25 MB/s: below p10 with room to spare, so an ordinary slow
# transfer is never cut off, while a truly dead one still ends in bounded time. The
# ceiling exists because the library holds 12 GB movies, and a pure rate budget would
# hand one a 17-hour timeout -- with `--low-level-retries 20` on the rclone calls, a
# stalling transfer can burn a very long time without ever moving a byte, so the slot has
# to come back eventually. 6 h covers a 12 GB file at 0.57 MB/s, still under measured p10.
TIMEOUT_ASSUMED_RATE_BPS = int(0.25 * 1024 * 1024)   # 0.25 MB/s -- below p10 (0.66)
TIMEOUT_FLOOR_SEC = 900                              # 15 min; was 300 and cut off real work
TIMEOUT_CEILING_SEC = 6 * 60 * 60                    # 6 h; bounds a stalled 12 GB transfer


def TIMEOUT(file_size_bytes: int) -> int:
    """Calculates timeout based on file size (in bytes)."""
    calculated_timeout = int(file_size_bytes / TIMEOUT_ASSUMED_RATE_BPS)
    return max(TIMEOUT_FLOOR_SEC, min(calculated_timeout, TIMEOUT_CEILING_SEC))

COMICS_EXTENSIONS = set({".cbz", ".cbr"})
# `.m4v` is a real container the library holds (Justice League Unlimited S01, The Dark
# Knight Rises, ...). It MUST be here or those files are invisible to the whole fleet:
# never uploaded, never evicted (not "proven on a remote"), never reaped when deleted,
# and their .nfo/artwork sidecars never purged. Keep this list in step with
# Torrent-Ingest's `REAP_TRACKED_EXTENSIONS`/`REAP_VIDEO_EXTENSIONS` and the searcher's
# `VIDEO_EXTENSIONS` -- they all describe the same "one canonical media set".
VIDEO_EXTENSIONS = set({".srt", ".mkv", ".avi", ".mp4", ".ass", ".m4v"})

DOWNLOAD_CHUNK_SIZE = 1024*1024*1024 # 1 gig chunk limit
UPDATE_THRESHOLD = 60
MAX_DOWNLOAD_TRIES = 3

# How often to do the FULL remote rescan (lsjson every remote to rebuild the inventory,
# plus an `about` on every remote for free space). This scan dominates cycle time -- doing
# it every cycle throttles uploads to a trickle. Instead rebuild it on this cadence and,
# between rebuilds, reuse the cached index while uploads keep the cache + inventory current
# incrementally (each uploaded file is added to the in-memory index, persisted to
# remote_inventory.json, and its target remote's free space refreshed live). So uploads run
# back-to-back and only pay the big scan occasionally -- which is what lets the migration
# backlog drain quickly.
#
# The sweep exists to reconcile drift -- a manual MEGA-side deletion, or an upload whose
# incremental bookkeeping was lost to a crash -- and to refresh free space against reality
# rather than the in-phase ledger. On a single-host fleet nothing else writes to the pool,
# and the uploader already keeps the inventory current as it goes, so the sweep is a
# safety net rather than the source of truth. At 6 h it costs a few percent of the duty
# cycle. Lower it only if a second host ever starts writing to the pool.
REMOTE_RESCAN_SEC = 6 * 3600   # 6 h

# How long the sync loop sleeps between cycles when there is nothing to do. The loop has no
# uploads to pace it when the local backlog is empty, so without this it spins -- each pass
# still runs git_pull + a remote-list + a local scan, so the idle loop used to burn CPU and
# (before the unplaceable warning was throttled) flood the log every few seconds.
SYNC_IDLE_SLEEP_SEC = 60

# --- Upload throughput -------------------------------------------------------
# How many uploads run at once, each pinned to a DIFFERENT MEGA account.
#
# MEGA throttles per account and rclone's mega backend uploads a file over a single
# connection, so one transfer is capped far below the link. N connections to N distinct
# accounts scale nearly linearly until the exit node saturates. Measured at the en0
# interface (wire traffic, not rclone's own accounting):
#
#     workers   aggregate @ en0   per stream
#        1          2.4 MB/s       2.4 MB/s
#        6          9.3 MB/s       ~1.6 MB/s
#       14         23.4 MB/s       ~1.4 MB/s
#       18         24.4 MB/s       ~1.35 MB/s The limit on this number is therefore NOT bandwidth,
# it is what else needs that exit node: `mediafs` hydration (cold-title playback) and
# qBittorrent ride it too, and winning upload MB/s by stuttering a show you are watching is
# a bad trade. 12 keeps roughly half the measured headroom in reserve.
#
# The aggregate ceiling is the Tailscale/Mullvad exit node, not MEGA and not the ethernet --
# an 8-connection CDN download through the same tunnel hits the same wall with MEGA
# uninvolved, while transfers outside the tunnel reach ~50 MB/s. That ceiling MOVES with the
# selected node: ~24 MB/s measured on a Stockholm exit, ~42 MB/s on a Denver one. Never treat
# a single throughput reading as the system's ceiling without noting which node it came from.
#
# 16 saturates a slow node and still leaves per-stream headroom on a fast one. Drop toward 6
# if playback of a cold title starts hitching: the exit node is shared with mediafs hydration
# and qBittorrent.
#
# The "distinct accounts" part is load-bearing. Two workers on ONE account would race its
# free-space number and both be admitted against the same bytes; _claim() debits the
# in-memory ledger under a lock at claim time and refunds on failure, so a remote is
# reserved for exactly one in-flight upload.
UPLOAD_WORKERS = 16

# Flags added to every upload copyto.
#   --buffer-size 64M  read-ahead per transfer; the default 16M under-feeds a long-haul
#                      link with an exit node's worth of latency in front of it.
#   --use-mmap         allocate those buffers outside the Go heap, which keeps allocator
#                      churn down across many concurrent transfers.
#   --stats 0          no periodic stats. Never use --progress here: under launchd stdout
#                      is a pipe rather than a terminal, so rclone's progress redraw emits
#                      megabytes of ANSI noise into the same stream run_command
#                      substring-matches for "error" to judge whether the upload failed.
#   --transfers/--checkers 1  one file per rclone process by construction (copyto); the
#                      parallelism lives in the worker pool, where the ledger can see it.
UPLOAD_RCLONE_FLAGS = [
    "--low-level-retries", "10",
    "--retries", "1",
    "--buffer-size", "64M",
    "--use-mmap",
    "--stats", "0",
    "--transfers", "1",
    "--checkers", "1",
]

# Bound on how long a worker waits for a free remote when every other worker holds one.
# With ~200 remotes carrying space and 6 workers this is unreachable in practice; it exists
# so a pathologically full pool degrades into a slow cycle rather than a spin.
UPLOAD_CLAIM_WAIT_SEC = 5
UPLOAD_CLAIM_MAX_WAITS = 12

# Headroom the ledger always leaves unused on every remote.
#
# The ledger is arithmetic on a snapshot: seeded from free_space.json and debited locally as
# files are claimed. Several things make the real figure drift ABOVE the ledger's estimate,
# and every one of them ends the same way -- an account pushed past its quota, where MEGA
# then refuses further writes:
#
#   * MEGA's rubbish bin counts against quota until `cleanup` runs, so a delete or an
#     overwrite keeps consuming space the ledger has already handed back.
#   * An upload that times out locally may still have completed server-side.
#   * A remote's capacity is not always exactly REMOTE_CAP_BYTES.
#
# Filling to the last byte leaves no room for any of that. A remote is treated as full once
# its ledger free space drops below this.
REMOTE_FILL_MARGIN_BYTES = 512 * 1024**2      # 512 MB

# How often the upload phase re-reads a remote's TRUE free space instead of trusting the
# ledger. Only remotes the ledger considers nearly full are re-checked, so this costs one
# `about` per nearly-full remote per interval rather than a sweep.
#
# Without this the ledger can only ever drift in the dangerous direction, because it is
# never corrected within a phase -- and an upload phase now runs for days.
LEDGER_RESYNC_SEC = 900                        # 15 min
LEDGER_RESYNC_WHEN_BELOW = 2 * 1024**3         # re-check a remote once it looks under 2 GB

# Stale-session self-heal (see utils.heal_stale_session and README, *Stale-session recovery*).
#
# A remote whose cached MEGA session has died fails INSTANTLY. That speed is the hazard: on a
# path that retries, one dead account can absorb an unbounded share of the work while healthy
# accounts are still moving bytes at their own pace. The cure -- strip its session_id/master_key
# so the next call re-authenticates -- is cheap, so the whole design is about not applying it
# too eagerly.
#
# The cooldown exists because every worker that touches a dead remote sees the same signature at
# the same moment; without it, all UPLOAD_WORKERS purge the same section in a burst, and the
# resulting simultaneous logins are themselves rejected as EARGS -- a login storm that looks
# exactly like the failure it is trying to fix.
SESSION_HEAL_COOLDOWN_SEC = 120

# Heals allowed per remote per cycle. A session purge that does not fix the remote means the
# fault is not the session (a suspended or quota-dead account), and repeating it forever is how
# a spin starts. Past this budget the remote is benched for the rest of the phase and picked up
# again by the next cycle's fresh scan.
SESSION_HEAL_MAX_PER_CYCLE = 3

# Idle period after which a remote's heal budget resets on its own. mediasync resets budgets at
# each cycle boundary; mediafs and predownload are resident and never reach one, so without this
# they would retire a remote permanently on three unlucky heals spread across weeks.
SESSION_HEAL_WINDOW_SEC = 3600

# How long a remote stays out of the upload allocator after a heal. It must exceed the login
# round trip so the retry lands on a session that actually exists, and it doubles as the
# anti-spin guard: a benched remote cannot be re-claimed, so a file cannot ping-pong against it.
UPLOAD_BENCH_SEC = 180

# Exclusive lock held for the duration of an upload phase.
#
# The ledger is only correct while ONE process is spending pool free space. Two uploaders
# each keep a private ledger, both seeded from the same snapshot, and both spend the same
# bytes -- which is how accounts get pushed past quota even though neither is wrong on its
# own terms. The fill margin and the live resync bound that drift but cannot remove it: two
# processes each seeing 5 GB free can each write 4 GB before either drops under the resync
# threshold.
#
# So the invariant is enforced rather than documented. media_sync holds this lock while
# uploading; any maintenance script that writes to the pool must call
# `uploader_lock.acquire_or_die()` and will refuse to start while the daemon has it.
UPLOAD_LOCK_PATH = SCRIPT_DIR.parent / "upload.lock"

# Remotes the media uploader must never ALLOCATE to, though they are still indexed.
#
# The metadata-backup remote is written by three other daemons on their own schedules --
# Media-Syncer's `backup_state`, and Torrent-Ingest's `db_guardian` and `backup_metadata`.
# None of them can take the upload lock without either starving behind a days-long media
# upload phase or blocking it, so serialising them is the wrong answer. Instead the media
# uploader simply never allocates there, and the contention cannot arise.
#
# CRITICAL: excluded remotes are still SCANNED by build_remote_index. They already hold
# media, and dropping them from the inventory would make that media look absent from the
# pool -- so it would be re-uploaded elsewhere, creating exactly the duplicate paths this
# whole mechanism exists to prevent. Exclusion applies to allocation only.
def upload_excluded_remotes() -> set:
    """Remote names the uploader will not place new files on."""
    excluded = set()
    try:
        import sys
        ti = Path.home() / "Developer" / "Media-Fleet" / "Torrent-Ingest"
        if str(ti) not in sys.path:
            sys.path.append(str(ti))
        import config as ti_config                     # Torrent-Ingest's config
        name = getattr(ti_config, "METADATA_BACKUP_REMOTE", None)
        if name:
            excluded.add(name)
    except Exception:                                  # noqa: BLE001
        # Fall back to the known name rather than silently allocating onto it.
        excluded.add("vm_mega1")
    return excluded

# --- Pool capacity provisioning ----------------------------------------------
# How often the upload phase re-checks pool capacity and provisions accounts.
#
# It must run DURING the upload phase, not once per cycle: the pool drains at ~68 GB/h and
# a cycle that uploads a large backlog runs for days, so a per-cycle check would fire long
# after the pool ran dry. Every upload past that point fails with "no suitable remote
# found", which is only a per-file warning, so the backlog stops draining with nothing in
# any log tying it to capacity.
#
# The check is free: the upload phase already keeps a live free-space ledger
# (§ _upload_phase), so "how much room is left" is a sum over a dict, not a network call.
# Only an actual provisioning run costs anything, and that is gated behind the floor below.
POOL_PROVISION_CHECK_SEC = 300          # 5 min

# Free-space floor that triggers provisioning, expressed as HOURS OF DRAIN rather than a
# fixed byte count. A fixed floor is really a time budget in disguise, and it silently
# decays whenever throughput changes; deriving it from the live drain rate keeps the lead
# time constant no matter how fast the uploader gets.
POOL_PROVISION_LEAD_HOURS = 6.0
POOL_LOW_FREE_BYTES = 200 * 1024**3     # absolute floor; the lead-hours figure can exceed it

# A provisioning run creates this many accounts at most, so a runaway condition cannot mint
# hundreds. Each account is ~20 GB usable (REMOTE_CAP_BYTES), so 25 buys ~500 GB.
ACCOUNTS_PER_LOW_BATCH = 5              # batch size for the manual `--ensure` entry point
ACCOUNTS_MAX_PER_RUN = 25

# PROACTIVE provisioning: when new local content appears, provision for it BEFORE the
# uploader needs the room rather than waiting for free space to fall through a floor.
# find_local_files() already knows the pending (not-yet-uploaded) byte count each cycle, so
# the deficit is simply pending - free, and it is knowable the moment content lands. This
# is what keeps a large drop from discovering a shortfall hours into the upload.
POOL_PROVISION_PROACTIVE = True

# --- Remote scan concurrency -------------------------------------------------
# The full rescan is `rclone lsjson --recursive` plus `rclone about` on every account:
# roughly 1.56 s and 1.77 s each, so across ~390 accounts it is ~22 minutes of round-trips
# if run serially, with no bytes moving. The calls are independent and almost entirely
# latency, so they parallelize well. Kept far below the account count deliberately -- the
# goal is to overlap latency, not to open hundreds of sockets, and the sweep must not
# starve the uploads running alongside it.
# At 8 the full sweep completes in roughly 80 seconds. Higher concurrency shaves little off
# that while measurably raising MEGA's stale-session panic rate, and every panic triggers a
# session purge that rewrites the shared rclone.conf.
SCAN_WORKERS = 8
# Extra margin on top of the measured deficit, because a file only fits an account with room
# for the WHOLE file -- packing is never perfect.
POOL_PROVISION_DEFICIT_MARGIN = 1.10

# --- Tier engine + mediafs (virtual library) ---------------------------------
# The MEGA pool holds a COMPLETE copy of every media file (single-residence
# invariant => remote_inventory.json is a full path->[remote, mtime, size] map).
# That makes the local library a CACHE we can thin: evict the coldest media whose
# bytes are proven on a remote, hydrate them back on demand straight from the
# inventory using the same chunked, VPN-rotating download the daemon already uses.
# tier.py is the mechanism; mediafs.py is the on-access trigger. SAFE BY DEFAULT:
# eviction is a dry-run PLAN unless explicitly executed, and a file is evictable
# only when the inventory proves its bytes live on a remote.

REMOTE_INVENTORY_PATH = SCRIPT_DIR.parent / "remote_inventory.json"

# Local cache for hydrated media payloads (the fast Mac SSD). Hot/recently-read
# bytes live here; cold ones are evicted. Kept OFF the repo and OFF the SSD library root.
TIER_CACHE_DIR = Path.home() / "Library" / "Application Support" / "media-syncer" / "cache"

# LRU ceiling for the hydrated-payload cache. When the cache exceeds this, the
# coldest files (by access time) are evicted until it is back under. This IS the
# "on-disc feel" budget: everything in it plays/reads at local-SSD speed with
# instant seeking. Sized generously against the Mac SSD (which becomes the sole
# store now that the library is served from the MEGA pool); LRU-by-atime keeps the
# recently/likely-watched set resident and drops cold files automatically.
TIER_CACHE_MAX_BYTES = 300 * 1024**3   # 300 GB -- a SAFETY BACKSTOP only. The predictive
                                       # pre-download daemon is the real cache manager and
                                       # holds the cache to storage_plan()['prefetch_budget']
                                       # (~3/4 of disk) with watch-aware eviction; this LRU
                                       # ceiling only fires if that daemon is down, so it sits
                                       # above the budget and rarely triggers.

# Predictive prefetch: on opening episode/volume N, pull the next PREFETCH_AHEAD media
# in the same folder into the cache so a binge (or reading a manga volume-by-volume)
# is served from local disk before you arrive. Best-effort background (does NOT pause
# the daemon); triggered only from the interactive mediafs.open, so it never chains
# through a whole season. Kept at 1 so a single open never spawns a pile of large
# concurrent downloads that would compete with the active stream. 0 disables.
PREFETCH_AHEAD = 1

# Max number of files streaming/hydrating (downloading from MEGA) at once. Bounded
# so parallel downloads don't thrash the shared VPN/bandwidth -- but NOT serialized
# to 1, or an active playback stream would have to wait behind a background prefetch
# before its first chunk arrives. A few lets concurrent plays (and a play alongside
# a prefetch) each make progress. Reads of already-cached files/metadata and the
# on-demand ranged fetches for seeks are never bounded by this.
TIER_MAX_CONCURRENT_HYDRATIONS = 3

# --- Streaming hydration (progressive playback) ------------------------------
# A cold file is STREAMED, not downloaded-whole-then-served: N parallel workers fill
# a sparse `.streaming` file segment-by-segment (front-to-back for playback locality),
# reads are served from any completed segment, and a read landing on a not-yet-fetched
# segment (a seek, an MP4 moov atom at the end, or a comic's central directory) gets an
# on-demand ranged fetch. So the first frame appears after one small segment, not the
# whole file.
#
# Throughput reality (measured against the MEGA pool over the Mullvad/Tailscale exit):
# one connection to one account sustains ~1.9 MB/s; TWO parallel connections to the
# same account reach ~3.3 MB/s; FOUR is *slower* (~2.6) as MEGA's per-IP throttle bites.
# So STREAM_WORKERS=2 is the sweet spot -- more connections lose throughput. And we do
# NOT rotate the exit node between segments on success: a rotation drops the connection
# and re-auths (10-40 s of dead air), which is what made cold playback time out. Rotation
# is REACTIVE only -- inside _fetch_into/_ranged_fetch on an actual failure.
STREAM_SEGMENT_BYTES = 4 * 1024**2        # per-segment fetch size (finer map -> faster fast-start)
STREAM_WORKERS = 2                        # parallel fetchers per file (2 == throughput sweet spot)
STREAM_READ_WAIT_SEC = 4                  # brief wait for the covering segments, else a ranged fetch
                                          # (during steady playback the fill runs well ahead of the
                                          # playhead, so reads hit ready segments and never wait)
STREAM_LOOKAHEAD_SEGS = 4                 # only wait for the sequential fill if the read is within this
                                          # many segments of the fill frontier; a read further ahead (a
                                          # seek, an MP4 moov tail, a comic's index) ranged-fetches at
                                          # once instead of waiting for the fill to crawl to it

# Per-fetch timeout for STREAMING (segment fills + interactive ranged fetches). The bulk
# TIMEOUT() above has a 900 s FLOOR -- correct for a 1 GB daemon chunk, fatal for a 1 MB
# interactive read, where a single stuck connection would block the player for minutes.
# Here a stuck fetch must fail FAST so we rotate to a healthier node quickly; a good node
# returns on completion long before the ceiling, so a low ceiling never truncates a
# working transfer of these small sizes.
# The rate here is the line between "slow node" and "dead node", and it must sit BELOW the
# slowest account the pool actually has, not below the average. Set too high, a slow-but-
# working account fails every fetch deterministically -- a 4 MB segment it serves in 17 s is
# killed at 15 s, every attempt, so the file is unfetchable from it and gets written off as
# unreachable. Accounts in this pool vary by nearly an order of magnitude (measured
# 2026-08-12: showsmega41 sustained ~245 KB/s while showsmega37 ran ~1.5 MB/s), so the floor
# is pegged low enough that only a genuinely stuck connection trips it.
STREAM_MIN_TIMEOUT_SEC = 20               # floor: even a 1 MB read gets at least this long
STREAM_MIN_RATE_BPS = 0.1 * 1024**2       # below ~100 KB/s a node is stuck, not merely slow
def STREAM_TIMEOUT(nbytes: int) -> int:
    return max(STREAM_MIN_TIMEOUT_SEC, int(nbytes / STREAM_MIN_RATE_BPS))

# --- Cold-miss coalescing: never spend a whole MEGA login on one 128 KB read ----------
# A FUSE read is ~128 KB. The original on-demand path fetched EXACTLY the requested range
# with its own `rclone cat` -- a fresh process spawn plus a full MEGA login to deliver 128 KB,
# where the auth dominates the transfer. Fine for what it was designed for (one seek, one MP4
# moov tail, one comic index). Catastrophic for a SEQUENTIAL cold reader, which issues those
# reads back to back: ~8 rclone logins per megabyte, and it never gets faster because each
# fetch is discarded rather than persisted.
#
# So a cold miss now fetches the whole SEGMENT-ALIGNED block covering the read, writes it into
# the partial file, and marks those segments done -- exactly what the background fill would
# have produced. The following ~32 sequential reads inside that block are then plain
# `os.pread`s with zero network. That is the difference between ~8 logins per MB and ~0.25.
# (Measured 2026-08-04: Jellyfin's `ffprobe -probesize 1G` grinding a cold pool file this way
# ran for 6h49m and froze the whole library scan.)
STREAM_COALESCE_SEGS = 1                  # segments to fetch per cold miss (1 == 4 MB block)

# Ceiling on how long a background fill worker defers to in-flight interactive reads. Without
# it the yield is a PRIORITY INVERSION: `readers_waiting > 0` parks both fill workers, and a
# sequential cold reader keeps that counter above zero essentially forever, so the efficient
# 4 MB bulk fill never runs and every read stays a tiny on-demand fetch -- the fill starves
# precisely when it is needed most. Past this, the worker proceeds anyway.
STREAM_FILL_YIELD_MAX_SEC = 20

# --- Fill resilience: one bad 4 MB block is not a dead file ---------------------------
# A segment fetch fails for reasons that have nothing to do with the file: an exit-node
# rotation lands mid-transfer, a MEGA account throttles for a minute, a connection resets.
# Those are transient and per-BLOCK. Treating one as terminal for the whole file is the
# difference between a two-second hiccup and an episode that never plays again.
#
# So a failed segment is recorded and SKIPPED, not fatal: the fill moves on, and every
# skipped segment is retried in a second pass once the sweep reaches the end. The fill
# gives up on the file only when this many segments fail BACK TO BACK, which is the
# signature of a genuinely unreachable remote rather than a bad moment on the wire.
STREAM_FILL_MAX_CONSECUTIVE_FAILS = 4
STREAM_FILL_RETRY_PASSES = 2              # extra sweeps over skipped segments before giving up
STREAM_FILL_BACKOFF_SEC = 3               # pause after a failed segment (a just-rotated node needs
                                          # a moment before it will serve; retrying instantly
                                          # burns the consecutive-failure budget on one bad node)

# How long a FAILED stream stays failed before a new open is allowed to rebuild it and try
# again. Without a rebuild the failure is permanent for the life of the process: the _Stream
# is cached per path, `failed` is never cleared, and every later read skips both the wait and
# the 4 MB coalescing to issue one rclone login per 128 KB -- a file that stalls forever and
# recovers only on a mediafs restart. The cooldown is what keeps the rebuild from becoming a
# spin against a remote that really is gone.
STREAM_RETRY_COOLDOWN_SEC = 30

# --- Metadata probers may not hydrate cold pool media ---------------------------------
# Same rule as trickplay and chapter-image extraction being off: on a virtual library, nothing
# should read whole media files just to describe them. Jellyfin's `ffprobe` is the one reader
# that cannot be switched off in its settings, and on a cold pool file it is pure cost -- it
# pulls megabytes over MEGA, hydrates content nobody asked for, and pins Jellyfin's bounded
# library-scan worker pool while it does it (a scan frozen at progress=0 for hours).
#
# So a read from one of these processes against a POOL-ONLY file fails fast with EIO. Jellyfin
# treats it as a cancelled probe and moves on; the item just lacks stream info until a later
# refresh, which is strictly better than freezing every scan behind it.
#
# `ffprobe` ONLY -- deliberately NOT `ffmpeg`. ffmpeg is Jellyfin's TRANSCODER: denying it
# would break real playback of a cold file. ffprobe is metadata analysis and never serves
# frames to a client. Files that are local (passthrough or fully cached) are unaffected either
# way -- they never reach the streaming path at all, so a freshly-ingested title on the SSD
# still probes normally at disk speed.
MEDIAFS_DENY_PROBER_READS = True
MEDIAFS_PROBER_NAMES = ("ffprobe",)

# Cross-process "a client is actively streaming" signal. mediafs (which serves reads)
# writes/heartbeats this flag while any media handle is open; the sync daemon (a SEPARATE
# process) reads it and PAUSES its cycle while set -- so its ~390-remote scan (which
# rotates the global exit node dozens of times) and its uploads never fight a live stream
# for the per-IP bandwidth budget or yank the exit node out from under playback. The flag
# carries a heartbeat mtime: it is honored only while fresh (STREAM_ACTIVE_TTL_SEC), so a
# mediafs crash can never wedge the daemon paused forever.
STREAM_ACTIVE_FLAG = Path.home() / "Library" / "Application Support" / "media-syncer" / "stream_active.flag"
STREAM_ACTIVE_TTL_SEC = 90                # a flag older than this is stale -> streaming considered idle
STREAM_HEARTBEAT_SEC = 5                  # how often mediafs re-touches the flag while handles are open
STREAM_YIELD_POLL_SEC = 3                 # how often the daemon re-checks the flag while yielding mid-op
# Safety cap: the daemon yields to LIVE PLAYBACK, but the flag is stamped by ANY
# cold-pool read through mediafs -- including Jellyfin's own background analysis
# (media-segment / trickplay / chapter-image scans, or a metadata refresh's ffprobe).
# Such a scan can read cold files for HOURS, holding the flag perpetually fresh and
# starving the sync/upload loop the whole time (seen 2026-08-03: an overnight
# "Media Segment Scan" stalled all sync for ~11h). So a single continuous yield is
# capped: past this, the daemon proceeds anyway (one scan op won't ruin a real
# stream, and most playback is served from cache=passthrough, which never marks the
# flag). Bounds worst-case starvation instead of trusting every reader to be a human.
STREAM_MAX_YIELD_SEC = 1200               # 20 min: never yield longer than this in one stretch

# media_sync's own post-cycle eviction (evict_plan) stays OFF, and this is still the right
# setting: two evictors racing for the same bytes is how the two-floor deadlock in *Failure
# mode: the pre-download cache starves the torrent downloader* got built. predownload.py
# remains the SINGLE space manager for the SSD. What changed is that it now manages the
# whole disk rather than only the cache -- see SSD_LIBRARY_AUTO_EVICT below. Leave this OFF.
TIER_AUTO_EVICT = False
TIER_EVICT_FLOOR_BYTES = 0
TIER_EVICT_PREFIXES = ("Shows/", "Movies/", "Comics/")

# --- Library-root eviction (the SSD's only source of reclaimable space) -------
#
# Media is KEPT locally after upload (GRADUATED_UPLOAD_THEN_REMOVE = False), so uploading
# frees nothing and the library root grows monotonically as new media is placed. The tier
# CACHE is not enough to evict on its own: once it is empty there is nothing left to give,
# and the SSD sits under its floor logging "eviction cannot reach it" indefinitely while a
# ~2 GiB cache faces a ~20 GB shortfall
# against an 18 GB shortfall. Torrent admission was squeezed and the YouTube ingest stopped
# entirely. Nothing automatic was ever going to fix it -- evict_plan existed but only ever
# ran by hand.
#
# So predownload now evicts cold, already-uploaded LIBRARY media too, under the same
# inventory guard (a file is deleted only if remote_inventory.json holds it at a matching
# size, so the local delete never removes the last copy). This is the difference between a
# disk that self-heals and a recurring manual chore.
SSD_LIBRARY_AUTO_EVICT = True
# Hysteresis, and it matters: eviction TRIGGERS at SSD_MIN_FREE_BYTES but runs down to this
# higher target, so it does real work occasionally instead of shaving a few hundred MB every
# 20-minute cycle and walking the disk along its own floor forever.
#
# Expressed as a MULTIPLE of the floor rather than its own byte count: the two numbers are
# only meaningful relative to each other (the gap between them IS the hysteresis band), so a
# floor that scales with the disk while the target stayed pinned at 100 GiB would silently
# widen or invert the band on a different machine. 1.25 is the measured-good 100/80 ratio.
# The resolved byte value is defined with SSD_MIN_FREE_BYTES below, once the floor is known.
SSD_LIBRARY_EVICT_TARGET_RATIO = 1.25

# --- Predictive pre-download (the "smart downloader") ------------------------
# On-demand streaming from MEGA is too slow for a good comic/video experience, so we
# PRE-DOWNLOAD what you are likely to want next into the local cache and read it from
# disk. Driven by what you actually watch/read: opening episode/volume N pulls a rolling
# window ahead; watched items are evicted; the whole thing is bounded by a storage budget
# derived from the machine's free space so it scales to any host.
#
# Budget (scales to any machine): of the space the cache system can use (current free +
# what the cache already holds), keep 1/10 as breathing room; of the remaining 9/10, size
# a "chunk" (torrent download batch) at 1/6, and dedicate the other 5/6
# (== 3/4 of the whole) to predicted pre-downloads.
#
# The ratio math above is NOT sufficient on its own, because `breathing` is a FRACTION of a
# reference that itself shrinks as the disk fills -- so the "floor" it implies slides down with
# the disk and can settle below what Torrent-Ingest needs to admit a download. That is a real
# deadlock, not a theoretical one: Torrent-Ingest requires
# `MIN_FREE_BYTES (20 GiB) + size * SPACE_SAFETY_FACTOR (1.15)` free on this same SSD before it
# will start a torrent, it leaves an oversized one QUEUED, and nothing else on the box ever
# frees space for it. With the cache holding the disk at ~31 GB free, a 23 GB season pack
# (The Eminence in Shadow, 2026-08-03) sat queued indefinitely. So the cache also obeys a
# floor that does not slide: 20 GiB to match Torrent-Ingest's own OS headroom, plus torrent
# staging room. The cache gets everything above it.
#
# That floor is a FRACTION OF TOTAL DISK CAPACITY, not a hardcoded byte count, so it scales
# to whatever machine this runs on. The distinction from the failed ratio above is the
# reference, and it is the whole point: `breathing` above is a fraction of FREE space, which
# shrinks as the disk fills, so the floor it implies slides down with the disk and deadlocks.
# Total capacity is a property of the hardware and never moves, so a fraction of it is as
# stable as a constant while still sizing itself to the host.
#
# 0.175 reproduces the measured-good 80 GiB on this 460 GiB SSD. The clamps keep the result
# sane at both extremes: below ~143 GiB of total disk the fraction would fall under
# Torrent-Ingest's 20 GiB MIN_FREE_BYTES and admission would deadlock at any size, so LOWER
# holds it at 25 GiB; on a multi-TB SSD the fraction would reserve hundreds of GB that buy
# nothing once the largest admissible torrent already fits, so UPPER caps it at 200 GiB.
SSD_MIN_FREE_FRACTION = 0.175
SSD_MIN_FREE_LOWER_BYTES = 25 * 1024**3    # never below Torrent-Ingest's own OS headroom
SSD_MIN_FREE_UPPER_BYTES = 200 * 1024**3   # past this, more reserved space buys nothing


def _ssd_total_bytes() -> int:
    """Total capacity of the filesystem holding the SSD library/cache. Falls back through
    the same path chain storage_plan() uses, so budget and floor measure the same disk."""
    for p in (TIER_CACHE_DIR, SSD_LIBRARY_ROOT, Path.home()):
        try:
            if p.exists():
                return shutil.disk_usage(p).total
        except OSError:
            continue
    return 0


def _ssd_min_free_bytes() -> int:
    total = _ssd_total_bytes()
    if not total:                                   # unreadable disk -> keep the old constant
        return 80 * 1024**3
    scaled = int(total * SSD_MIN_FREE_FRACTION)
    return max(SSD_MIN_FREE_LOWER_BYTES, min(scaled, SSD_MIN_FREE_UPPER_BYTES))


# Resolved once at import: total capacity is fixed for the life of the process, so there is
# nothing to re-measure, and every existing `config.SSD_MIN_FREE_BYTES` reader keeps working.
SSD_MIN_FREE_BYTES = _ssd_min_free_bytes()

# The eviction target rides the floor (see SSD_LIBRARY_EVICT_TARGET_RATIO above).
SSD_LIBRARY_EVICT_TARGET_BYTES = int(SSD_MIN_FREE_BYTES * SSD_LIBRARY_EVICT_TARGET_RATIO)


def _cache_bytes_on_disk() -> int:
    """ALLOCATED bytes the cache dir actually occupies -- `st_blocks`, not `st_size`.

    Must not use apparent size: the progressive-streaming partials (`*.streaming`) are SPARSE
    files created at their full final length, so `st_size` overcounts them enormously (measured
    2026-08-04: 175.5 GB apparent vs 27.0 GB on disk across 94 partials). Feeding that inflated
    figure into `reference` invented ~148 GB of space that does not exist, while
    `predownload._cached_media()` -- which skips partials entirely -- measured occupancy a
    THIRD way. Budget and enforcement must agree, so both now count real blocks.
    """
    total = 0
    for dp, _dn, fns in os.walk(TIER_CACHE_DIR) if TIER_CACHE_DIR.exists() else []:
        for n in fns:
            try:
                total += os.lstat(os.path.join(dp, n)).st_blocks * 512
            except OSError:
                pass
    return total


def storage_plan() -> dict:
    import shutil
    cache_used = _cache_bytes_on_disk()
    free = shutil.disk_usage(TIER_CACHE_DIR if TIER_CACHE_DIR.exists() else Path.home()).free
    ref = free + cache_used                 # stable regardless of how full the cache is
    breathing = max(ref // 10, SSD_MIN_FREE_BYTES)   # ratio OR the absolute floor, whichever binds
    after = max(0, ref - breathing)
    return {"reference": ref, "breathing": breathing,
            "chunk": after // 6, "prefetch_budget": after * 5 // 6,
            "cache_used": cache_used, "free": free}

# How far ahead to keep cached, per content class (rolling window; truncated by budget).
PREDOWNLOAD_EPISODES_AHEAD = 50    # next N unwatched episodes of an active show
PREDOWNLOAD_VOLUMES_AHEAD = 20     # next N volumes of an active comic series
PREDOWNLOAD_MOVIES_AHEAD = 10      # next N AI-predicted related movies
PREDOWNLOAD_RECENT_ITEMS = 12      # how many recently-touched items drive prediction
PREDOWNLOAD_POLL_SEC = 60          # reconcile cadence
# Per-phase time caps, so a reconcile() cycle ALWAYS turns over.
#
# reconcile() does its self-healing at the TOP of the pass (reap stale `.streaming` partials,
# evict the SSD cache back to budget, re-check the free-space floor) and its transfers at the
# bottom. So any transfer phase that runs unbounded suspends all of that healing for as long as it
# lasts -- and both phases naturally run for a very long time: the SSD prefetch can have tens of GB
# of desired content queued at MEGA's ~2-3 MB/s, and fill_drives() walks the ENTIRE pool inventory,
# which against a mostly-empty 7 TB drive is DAYS inside one call. (Measured 2026-08-04: the last
# completed cycle was 20 h old, 94 orphaned partials had accumulated against a 30-min reap
# threshold, and the SSD sat pinned near-full -- which is what starved Torrent-Ingest's queue.)
#
# Two SEPARATE caps rather than one shared cycle deadline, deliberately: a single deadline shared
# in priority order would let a large prefetch backlog consume the whole budget every cycle and
# starve drive-fill indefinitely. Bounding each phase independently guarantees both make progress.
# Yielding is free -- both phases skip content that is already present, so progress is monotonic
# and the next cycle resumes where this one stopped.
PREDOWNLOAD_PREFETCH_MAX_SEC = 900     # 15 min: SSD hot-set prefetch
PREDOWNLOAD_DRIVE_FILL_MAX_SEC = 900   # 15 min: bulk drive fill

# How many prefetch downloads run at once, each pinned to a DIFFERENT MEGA account.
#
# The pre-downloader fetched strictly one file at a time, which measured 3.28 MB/s -- and
# that number was widely (and wrongly) believed to be a hard per-IP ceiling, on the strength
# of the streaming finding that "four workers is SLOWER than two". That measurement is real
# but it is about four workers on ONE FILE from ONE ACCOUNT, where the per-account throttle
# is what pushes back. Across DISTINCT accounts the picture is completely different, exactly
# as it was for uploads:
#
#     1 stream                       3.28 MB/s
#     8 streams, 8 distinct accounts  13.46 MB/s   (measured at en0, 2026-08-08)
#
# So the throttle that matters here is per-ACCOUNT, and the fix is the same claim-a-distinct
# -remote pattern the upload phase uses -- no multi-tunnel/multi-exit-IP machinery required.
#
# Held below the upload worker count on purpose. Prefetch is speculative work whose entire
# job is to make PLAYBACK instant, so it must not be the reason a cold title stutters:
# `mediafs` prioritizes interactive reads over its own fill workers, but it cannot deprioritize
# a separate predownload process competing for the same link. 6 roughly doubles prefetch
# throughput while leaving clear headroom for a live stream.
PREDOWNLOAD_WORKERS = 6

# --- Drives are UPLOAD-ONLY (the graduation) ---------------------------------
# When False, fill_drives() does not run: nothing is ever DOWNLOADED onto an external drive.
# Drives keep every other role they have -- they are still discovered live, still merged
# into the mediafs view so their files serve locally, still scanned by find_local_files()
# so their content is uploaded to the pool, and drive_ingest (Torrent-Ingest) still
# organizes and renames loose media found on them. The single thing that stops is using a
# drive's free space as bulk predictive cache.
#
# WHY. Drive-fill was correct when the drive was the library. It is counterproductive when
# the drive is being retired, for three compounding reasons:
#
#   1. It competes with the uploader for the one resource that matters. Both directions
#      ride the same exit node; drive-fill pulls pool content DOWN at ~4.3 GiB/h while the
#      upload backlog is trying to push 2.7 TB UP. The README argues below that the two do
#      not contend in a way pausing one relieves -- that argument holds for a drive being
#      KEPT and does not hold for a drive being emptied.
#   2. It writes new files onto the exact disk we are trying to make disposable, and
#      every byte it writes is a byte that must later be verified as pool-resident again.
#   3. It is the phase that has repeatedly starved reconcile()'s self-healing (see the
#      failure mode below), and the cheapest version of that bug is not to run it.
#
# Setting this True restores the old behaviour verbatim; the code path is untouched.
PREDOWNLOAD_FILL_DRIVES = False
# Protect a file from eviction if it was read this recently (guards the file you are
# actively watching/reading, which the pre-downloader must never evict mid-play).
PREDOWNLOAD_PROTECT_SEC = 1800     # 30 min

# Universal access signal: mediafs appends {rel, ts} here on every interactive open, so
# the pre-downloader knows what you just started (works for video AND comics, unlike the
# Jellyfin-only watch state). One JSON object per line.
PREDOWNLOAD_ACCESS_LOG = SCRIPT_DIR.parent / "access_log.jsonl"

# Jellyfin (video watch-state: which episodes are played, and recency). Comics are not in
# Jellyfin; those are driven by the access log above. Env-overridable.
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").strip() or "http://127.0.0.1:8096"
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "").strip() or "__JELLYFIN_API_KEY__"

# --- Off-machine backup of the load-bearing state files ----------------------
# Since the cutover, remote_inventory.json is RUNTIME-load-bearing: mediafs presents
# the whole library from it and eviction/hydration depend on it, so losing it costs
# a multi-hour fleet rescan to rebuild. Back it up (+ sync_state.json) to the shared
# metadata-backup MEGA remote periodically, versioned. Deliberately NOT via git:
# these are high-churn machine state, not config (rclone.conf is the only thing that
# belongs in git). free_space.json is pure cache -- not backed up.
METADATA_BACKUP_REMOTE = os.environ.get("MEDIA_SYNCER_BACKUP_REMOTE", "").strip() or "vm_mega1"
METADATA_BACKUP_BASE = "metadata-backup"
STATE_BACKUP_SUBPATH = "media-syncer-state"
STATE_BACKUP_FILES = [REMOTE_INVENTORY_PATH, SYNC_STATE_PATH]
# How many timestamped version dirs to retain under .../_versions/ before pruning.
# This backup runs hourly, so 72 == three days of rollback. WITHOUT a bound the
# version tree grows forever and quietly fills the metadata-backup account -- the
# exact failure that over-filled vm_mega1 and started returning "over quota". Each
# prune is followed by an rclone `cleanup`, because MEGA parks deletes in the
# rubbish bin (use_trash) which still counts against quota until it is emptied.
STATE_BACKUP_KEEP_VERSIONS = 72

# mediafs: the virtual view of the library. It mounts at MEDIAFS_MOUNT -- a LOCAL
# path on the Mac SSD, so the library presentation lives on the Mac (cold files that
# have been evicted from the SSD library root just stream from MEGA).
# Jellyfin and YacReader were migrated onto this path (jellyfin.db had every media
# path rewritten and every path-derived item GUID recomputed + cascaded so all
# watch-state survived; YacReader's library root was repointed). `lower` is the real
# media dir (SSD_LIBRARY_ROOT = the SSD library root ~/Media): its files (metadata
# sidecars + hot/pinned media) pass through untouched; anything only in the inventory
# is presented full-size and hydrated on first read into TIER_CACHE_DIR.
MEDIAFS_MOUNT = Path("/Users/mikeyferguson/MediaLibrary")
MEDIAFS_LOWER = SSD_LIBRARY_ROOT                       # the real media dir (the SSD library root ~/Media)

# Media payload extensions mediafs virtualizes (everything else in `lower` --
# .nfo/.jpg/.png metadata -- passes through as real files, never virtualized).
MEDIAFS_PAYLOAD_EXTENSIONS = VIDEO_EXTENSIONS | COMICS_EXTENSIONS

# Only these top-level prefixes are presented by mediafs. The inventory can carry
# other trees (e.g. `metadata-backup/`) that must NOT appear in the library view
# Jellyfin/YacReader see.
MEDIAFS_PREFIXES = ("Shows/", "Movies/", "Comics/")

# When a media file is deleted THROUGH the mount (you remove a title in
# Jellyfin/Infuse with file management on), mediafs appends its library-relative
# path here; the Torrent-Ingest reaper drains this queue to purge the file from
# the MEGA pool + metadata backup + state. This is the DELETE signal in the
# virtual model: a file merely vanishing from the SSD library root is now an EVICTION (do NOT
# purge), so the reaper can no longer key off a local-vanish -- only an explicit
# unlink through the mount is a real delete. One entry per line: {"path": ...}.
MEDIAFS_DELETIONS_QUEUE = SCRIPT_DIR.parent / "mediafs_deletions.jsonl"

# The REPLACE signal, opposite direction to the deletions queue: when Torrent-Ingest
# deliberately REPLACES a file already on disk (an anime quality upgrade -- higher
# definition or dual audio), it appends the library-relative path here; this daemon
# drains it to overwrite the stale MEGA copy in place and empty that remote's rubbish
# bin, exactly like a One Pace re-cut. Without this, the write-once sync absorbs the
# mtime drift and the pool keeps the OLD version forever. One entry per line:
# {"path": ...}. Mirror Torrent-Ingest's config.MEDIA_SYNCER_REPLACEMENTS_QUEUE.
REPLACEMENTS_QUEUE = SCRIPT_DIR.parent / "replacements.jsonl"

# How often mediafs may stat remote_inventory.json to notice a new version. It reads the file
# once at mount and would otherwise treat that snapshot as immutable for the life of the
# process -- which loses titles, because media_sync keeps adding to the pool and predownload
# keeps evicting the local copies of what is already there. A title uploaded and then evicted
# inside one mediafs lifetime is on the pool, absent from disk, and unknown to the mount: it
# disappears from Jellyfin until something restarts. See MediaFS._maybe_reload_inventory.
#
# 5 s is a throttle on the STAT, not a reload interval -- a reload only happens when the
# (mtime_ns, size) stamp actually changes, which is at most once per _persist_inventory write
# (itself throttled to 30 s). So a directory walk of thousands of entries costs one stat every
# 5 s, and a quiet mount costs nothing at all.
MEDIAFS_INVENTORY_POLL_SEC = 5.0

# When True, the library is served on demand through mediafs and the tier engine
# hydrates evicted files on read. In this mode Media-Syncer must be UPLOAD-ONLY:
# its download phase is SKIPPED, because "download every file missing locally"
# would re-fetch the very files the tier engine evicted to free the SSD -- the two
# are directly opposed. New local media is still uploaded; One Pace still churns.
# (The reaper in Torrent-Ingest is likewise incompatible until reworked: a file
# vanishing from the SSD library root now means "evicted," not "deleted," so it must not drive
# a remote purge. Keep it disabled while this is True.)
SERVE_VIA_MEDIAFS = True
