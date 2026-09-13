"""Central configuration for the YouTube ingest daemon.

Named `ytconfig`, not `config`, ON PURPOSE. Torrent-Ingest's engine modules -- which
this repo imports and runs -- all do a plain `import config` and expect THEIR config.
If this file were called `config.py` it would win that import (it is in the entry
script's own directory, which leads `sys.path`), and `library`/`identify` would come up
holding a module with none of the attributes they need. Every module in this repo
therefore imports `ytconfig` for its own settings and `config` for Torrent-Ingest's.

This repo is the YOUTUBE SOURCE for the same library pipeline Torrent-Ingest runs.
It is deliberately not a standalone downloader any more: it discovers what you have
saved on YouTube, fetches it in space-bounded waves onto the Downloads volume, and
then hands the finished files to Torrent-Ingest's own machinery -- the headless
AI identify step, `library.validate_plan` / `apply_plan` / `verify_applied` --
so a YouTube video lands in the library through exactly the same door a torrent
does, with the same naming, the same write-once safety, the same locked `.nfo`, and
the same Media-Syncer upload afterwards.

The one thing that does NOT go to the library is an audio track (an OST rip, a
character theme, a song): those go to a flat cloud-synced music folder, which is where
they already live -- iCloud `Soundtracks/` or Google Drive `Music/`, chosen by which
playlist the track came from (`AUDIO_PLAYLIST_DIRS`).

Every tunable lives here so the engine modules stay declarative.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# --- The Torrent-Ingest pipeline we borrow ------------------------------------
#
# Placement, validation, staging, atomic publish, the locked-`.nfo` writers, the
# library digest, the Jellyfin rescan and the playlist auto-extend all already exist
# in the sibling repo, and they are the parts that MUST behave identically for a
# YouTube video and a torrent -- so they are imported, never reimplemented. A second
# copy of "how a show folder is named" is exactly how two ingest paths drift into
# filing the same show two different ways.

TORRENT_INGEST_DIR = Path(
    os.environ.get("TORRENT_INGEST_DIR", "").strip()
    or (Path.home() / "Developer" / "Torrent-Ingest")
)
# Appended, not prepended: Torrent-Ingest's modules must resolve, but THIS repo's
# modules still take precedence for any name both repos happen to define.
if str(TORRENT_INGEST_DIR) not in sys.path:
    sys.path.append(str(TORRENT_INGEST_DIR))

try:
    import config as TI           # Torrent-Ingest's config (see the module docstring)
except ImportError as exc:        # pragma: no cover - a broken install, surfaced loudly
    raise SystemExit(
        f"Cannot import Torrent-Ingest's config from {TORRENT_INGEST_DIR}. "
        f"Set TORRENT_INGEST_DIR to the repo path. ({exc})"
    ) from exc
if not hasattr(TI, "MEDIA_ROOT"):
    raise SystemExit(
        "`import config` resolved to the wrong module (no MEDIA_ROOT). Something on "
        "sys.path shadows Torrent-Ingest's config.py -- check for a stray config.py "
        f"or __pycache__ in {PROJECT_ROOT}."
    )

# --- Roots -------------------------------------------------------------------

# The library root. Taken from Torrent-Ingest so the two ingests can never disagree
# about where the library is (the old standalone version wrote to an external drive
# that is no longer the library root at all).
MEDIA_ROOT = TI.MEDIA_ROOT
SHOWS_ROOT = TI.SHOWS_ROOT
MOVIES_ROOT = TI.MOVIES_ROOT

# Where short audio tracks go. Flat folders of `<Track Title>.mp3`, already populated
# by hand -- NOT Jellyfin libraries, so nothing filed here goes through the placement
# plan, gets an `.nfo`, or is uploaded to the MEGA pool. Each is inside a cloud-synced
# tree that provides its own durability (iCloud Drive; Google Drive), which is why the
# MEGA pool is not also asked to hold them.
SOUNDTRACKS_DIR = (Path.home() / "Library" / "Mobile Documents"
                   / "com~apple~CloudDocs" / "Soundtracks")

# The second audio destination: Tally's Google Drive `Music` folder, fed by her
# "Download" playlist. Hand-curated, with subfolders of its own (`Church/`, `Folk
# music/`, ...), and reached through the File Provider mount rather than a plain
# directory -- see AUDIO_PLAYLIST_DIRS for why that second part matters.
MUSIC_DIR = (Path.home() / "Library" / "CloudStorage"
             / "GoogleDrive-tallyferguson@gmail.com" / "My Drive" / "Music")

# Dot-prefixed scratch dir where yt-dlp downloads. On the DOWNLOADS volume, never in
# the library root -- same rule (and the same reason) as Torrent-Ingest's
# INCOMING_DIR: heavy write I/O inside the library root starves the directory reads
# mediafs serves to Jellyfin and wedges the mount. It is dot-prefixed so Media-Syncer's
# scan and the reaper's scan both prune it, and an in-flight download is therefore
# invisible to the uploader (never half-uploaded) and to the reaper (its cleanup
# delete is never mistaken for a library file vanishing).
INCOMING_DIR = TI.DOWNLOADS_DIR / ".youtube-ingest"

# --- State (all gitignored) --------------------------------------------------

STATE_DIR = PROJECT_ROOT / "state"
LEDGER_FILE = STATE_DIR / "seen.json"          # video_id -> what we did with it
SHOWS_FILE = STATE_DIR / "shows.json"          # playlist_id -> the show it became
PLAYLISTS_FILE = STATE_DIR / "playlists.json"  # last discovery snapshot (for --status)
TMP_DIR = STATE_DIR / "tmp"                    # identify plans/prompts
LOCK_FILE = STATE_DIR / "youtube_sync.lock"    # single-instance guard
LOG_FILE = PROJECT_ROOT / "youtube_sync.log"

# --- No authentication ------------------------------------------------------
#
# This daemon never signs in. Every tracked playlist is PUBLIC, so it enumerates and
# downloads signed-out. Cookies bought only two things -- reading which playlists were
# saved, and Liked videos -- and cost a session Google revoked twice under the request
# volume (40 minutes, then 2 hours), each needing a manual browser export. Moving the
# account-only list into a public "Soundtracks" playlist removed the last reason to hold a
# credential at all, so there is no cookie file, no expiry, and no chore.
#
# Adding a playlist is now an explicit one-off: `youtube_sync.py --add-playlist <url>`.

# --- Egress ------------------------------------------------------------------

# Force IPv4 on every yt-dlp call. This is the second half of the Google split-tunnel in
# Media-Syncer's `scripts/split_tunnel.sh`: that daemon pins Google's published
# IPv4 prefixes to the physical gateway so YouTube is reached from the stable home IP, and
# only v4 is pinnable in practice (pinning v6 needs a physical-interface v6 router whose
# discovery Tailscale owns, and a half-working v6 route is worse than none). Forcing v4
# here means this daemon can never reach Google over IPv6 and leak back out through the
# exit node, silently undoing it.
FORCE_IPV4 = os.environ.get("YOUTUBE_FORCE_IPV4", "1").strip() not in ("0", "false", "no")

# Refuse to run while Google traffic would leave through the VPN tunnel.
#
# This mattered more when a live session was at stake; with no credentials to lose it is
# now about download reliability rather than revocation -- a rotating Mullvad datacenter
# exit gets rate-limited and 403'd far more than a residential IP, and it wastes a wave.
# It also closes a reboot race: the split-tunnel is a system daemon that runs at boot and
# may still be waiting for DHCP when this agent starts at login.
REQUIRE_DIRECT_EGRESS = os.environ.get(
    "YOUTUBE_REQUIRE_DIRECT_EGRESS", "1").strip() not in ("0", "false", "no")

# --- Discovery ---------------------------------------------------------------

# Playlist ids that are never ingested, whatever discovery turns up:
#   WL = Watch Later     -- a queue of things you have not decided to keep
#   HL = History         -- everything you ever pressed play on
# Both are explicitly excluded by request, and both would otherwise flood the library.
SKIP_PLAYLIST_IDS = {"WL", "HL"}
# Belt-and-braces on the same exclusion by NAME, in case a feed ever hands back one
# of these under a different id (matched case-insensitively against the full title).
SKIP_PLAYLIST_TITLES = {"watch later", "history", "watch history", "liked music"}

# There is deliberately NO local URL list. The account is the single source of truth: if
# you want it ingested, save it on YouTube. The retired `links.txt` was a second source
# that could ingest a playlist invisible on the account, and its contents rotted unseen --
# four of its nine URLs were already HTTP 404 by the time it was removed, re-checked every
# cycle for as long as the file existed.

# A playlist with more entries than this is enumerated but reported, so a 5,000-video
# saved playlist is an obvious event in the log rather than a silent month of syncing.
LARGE_PLAYLIST_WARN = 300

# --- Audio routing -----------------------------------------------------------
#
# The audio playlists, and the folder each one files into. A playlist listed here files
# EVERY item in it as an audio track; every playlist not listed here goes through the
# library pipeline, no matter how short or how music-like its title. The retired
# length/title/AI classifier used to split a short "track" out of *any* playlist; that
# was filing soundtrack-looking shorts from every playlist into Soundtracks/, which is
# not what the folder is for.
#
# A MAP rather than a set because the destination is per-playlist: "Soundtracks" is the
# OST/character-theme folder in iCloud, and Tally's "Download" playlist files into her
# shared Google Drive `Music` folder instead. They are different folders for different
# people, so one flat "audio goes here" constant cannot express it.
#
# Keyed by playlist ID (not title) so a rename can't silently change where a playlist
# routes -- and dedupe is per-destination, so the same track can legitimately exist in
# both folders without either one suppressing the other.
#
# Every destination must be an EXISTING cloud-synced folder. `soundtracks.file_track`
# refuses to create one whose parent is missing, because a Drive or iCloud tree that has
# not mounted yet looks exactly like an empty path -- and creating it would file every
# track into a plain local directory that nothing ever syncs, silently, forever.
AUDIO_PLAYLIST_DIRS = {
    "PLJtTPjwghzms": SOUNDTRACKS_DIR,     # "Soundtracks" -> iCloud Soundtracks/
    "PLSLJ9WOPSCSU": MUSIC_DIR,           # "Download"    -> Google Drive Music/
}

AUDIO_FORMAT = "mp3"                   # matches what is already in both folders

# --- Download behaviour ------------------------------------------------------

YT_DLP = "yt-dlp"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
EXTRA_PATH = TI.EXTRA_PATH

# MKV holds VP9/AV1 + Opus + subtitle tracks cleanly, and `.mkv` is one of the
# extensions Media-Syncer actually replicates to the MEGA pool -- a `.webm` would land
# in the library and then never be backed up, so the remux is not cosmetic.
CONTAINER = "mkv"

# Cap the height. Storage discipline: 4K YouTube re-encodes cost several times a 1080p
# copy for content that is mostly not worth it, and every byte here is a byte the pool
# has to hold and the predictive cache has to move.
MAX_HEIGHT = 1080
FORMAT = (f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]/bv*+ba/b")
SUB_LANGS = "en.*"

# Which YouTube "player clients" yt-dlp asks for formats from.
#
# This is a FIX, not a tuning knob. YouTube's default `web` client now issues media URLs
# that require a PO Token (proof-of-origin); without one, metadata extraction succeeds and
# then the media fetch dies with `HTTP Error 403: Forbidden`. It is not throttling and it
# is not transient -- a retry reproduces it exactly, which is what makes it so misleading:
# the failure looks like a network blip and gets recorded as one and retried forever.
#
# Measured here on the four tracks that failed this way: the default client failed all four
# on every retry. The rescue is the FALLBACK client below, not a change to the primary set.
#
# `default` is deliberately EXCLUDED rather than listed last. yt-dlp merges the format
# lists from every client given, and the selector then picks the best match across all of
# them -- so including `default` lets a PO-token-gated format win selection and 403 again.
# Verified: a client list that includes a PO-token-gated client 403s the whole batch, not
# just its share of the formats.
# PRIMARY: empty = yt-dlp's default client set, which has by far the best format coverage.
# Do NOT set this to `tv` globally. That was tried and it is a bad trade: `tv` returns only
# storyboards for a lot of long-form content, so a global switch fixed 5 videos and broke
# 119 with "Requested format is not available". Coverage first, then fall back.
PLAYER_CLIENTS = os.environ.get("YOUTUBE_PLAYER_CLIENTS", "").strip()

# FALLBACK: retried per item, only for what the primary pass failed to produce, and tried
# as a CHAIN -- each client in order, for whatever is still missing. A chain rather than a
# single client is the whole point: it is what keeps a future YouTube change from becoming
# a manual "edit the plist" chore. If `web_embedded` ever gets PO-gated like `web`/
# `web_safari` did, the next client is already queued behind it and the daemon self-heals.
#
# `web_embedded` leads because it is the embedded-player client: no PO-token policy, and it
# exposes the FULL format set (same audio ladder and 144p-1080p video ladder as `web`), so
# it rescues the 403 at full quality. `web_music` is the fallback's fallback: it carries no
# PO-token policy either, but only the progressive `18` format, so it downloads at lower
# audio quality -- still far better than nothing.
#
# `tv` was once this fallback, but YouTube now answers its player request with "The page
# needs to be reloaded", and `tv_embedded` still lists the full format set yet 403s on the
# actual download (same gated URLs under a different client name) -- both measured dead on
# 2026-08-19, so neither is in the chain. `ios`/`mweb`/`android` list storyboards-only or
# `18`-only, so they add nothing `web_music` does not already cover.
#
# Measured 2026-08-19: the default client 403'd audio track `mIHAyQ9wa6o` on every retry;
# `web_embedded` downloaded it immediately (opus 123k -> mp3).
#
# Env override is comma-separated; leave unset for the chain above.
PLAYER_CLIENTS_FALLBACK = tuple(
    c.strip() for c in os.environ.get("YOUTUBE_PLAYER_CLIENTS_FALLBACK", "").split(",")
    if c.strip()
) or ("web_embedded", "web_music")

# Per-video hard ceiling. A multi-hour 1080p upload can be tens of GB; above this the
# video is recorded as skipped (with the reason) rather than silently eating the wave
# budget. Raise it if you genuinely want long-form archives.
MAX_VIDEO_BYTES = 12 * 1024**3

# --- Wave budget (storage discipline) ----------------------------------------
#
# The same shape as Torrent-Ingest's chunked download: never let in-flight downloads
# grow unbounded. Fetch a WAVE whose estimated sizes sum under the budget, ingest it
# (apply -> verify -> free the local scratch copy), then fetch the next wave. So an
# 800-video backlog flows through a small amount of transient disk instead of
# demanding room for all of it at once.
#
# The fraction is smaller than the torrent one: YouTube items are individually small,
# so a modest wave is already dozens of videos, and this daemon runs alongside the
# torrent ingest and must not crowd it out of the same disk.
WAVE_FRACTION = 1.0 / 20.0
WAVE_MIN_BYTES = 4 * 1024**3
# Free space on the shared volume never goes below this; a wave that would breach it is
# trimmed, and a cycle that cannot fit even one video simply waits.
#
# This is Media-Syncer's SSD floor, NOT Torrent-Ingest's 20 GiB OS headroom, and the
# difference is load-bearing. The Downloads volume and the library are the SAME physical
# disk here, and Media-Syncer holds 80 GiB free on it deliberately: 20 GiB of OS headroom
# plus ~60 GiB of torrent staging room, because nothing else on the box ever frees space
# for a queued torrent (its README: a 23 GB pack sat queued indefinitely when the cache
# held the disk at ~31 GB free). Using the 20 GiB floor here would let the YouTube ingest
# drive the disk down into exactly that hole -- starving the predictive cache and blocking
# torrents -- while every one of its own checks reported healthy.
#
# Measured: 4.5 hours of ingesting long-form playlists took the disk from 143 GiB free to
# 95 GiB. This is not a hypothetical margin.
#
# It sits FLOOR_DIP_BYTES *below* Media-Syncer's floor rather than level with it, and that
# gap is the whole reason this ingest moves at all. Matching 80 GiB exactly looks safe and is
# actually a deadlock: Media-Syncer's tiering drives the disk down toward its floor by design,
# so `free - MIN_FREE_BYTES` settles at approximately zero and stays there. Measured
# 2026-08-07 -- 80.4 GiB free against an 80 GiB floor left 485 MB of headroom, too little for
# a single 900 MB video, and every wave deferred for 19 hours while the backlog grew.
#
# A dip is safe where a lower floor is not, because it is bounded and transient: one wave at a
# time, hardlinked into the library and the scratch copy freed. It leaves 50 GiB of staging,
# still room for a ~43 GiB torrent after the 1.15 safety factor.
FLOOR_DIP_BYTES = 10 * 1024**3
MIN_FREE_BYTES = 80 * 1024**3 - FLOOR_DIP_BYTES
# When yt-dlp reports no filesize for an entry (common on a flat playlist listing),
# assume this much for budgeting. Under-estimating is what overruns a disk, and 900 MB
# -- the previous value, whose comment also called itself "deliberately generous" --
# was not. Measured over the 12-video wave of 2026-08-07 12:30: mean 1744 MB, median
# 920 MB, max 7376 MB. The median flattered it; the mean is what a wave sums to. That
# wave estimated 10.5 GB, actually consumed 20.4 GiB, and took the disk 8 GiB THROUGH
# the floor. 2 GiB is above the measured mean with room to spare.
ASSUMED_VIDEO_BYTES = 2 * 1024**3

# Estimates are estimates, so the floor is defended twice: waves are planned against
# `estimate * this`, and `fetch.download_videos` re-reads real free space between videos
# (see its module docstring). Same value and same reasoning as Torrent-Ingest's
# SPACE_SAFETY_FACTOR -- an estimator that is right on average still overshoots half the
# time, and only the live re-check bounds the damage when it does.
SPACE_SAFETY_FACTOR = 1.15

def wave_bytes() -> int:
    """Per-wave download budget: WAVE_FRACTION of the usable Downloads-volume capacity
    (total minus a 1/10 breathing reserve). Computed live so it tracks the real disk."""
    try:
        total = shutil.disk_usage(TI.DOWNLOADS_DIR).total
    except OSError:
        total = 512 * 1024**3
    usable = total - total // 10
    return max(WAVE_MIN_BYTES, int(usable * WAVE_FRACTION))

# Ceiling on how many videos one cycle will place, however much disk is free. Keeps a
# single cycle's identify spend and Jellyfin churn bounded; the rest rides to the next.
MAX_VIDEOS_PER_CYCLE = 120

# --- Identify (headless AI run) ----------------------------------------------
#
# Re-exported from Torrent-Ingest rather than redefined, like everything else in this
# block: one definition of which runtime the fleet talks to, and one definition of what
# a failure from it means. AI_BIN is a LIST (interpreter + runner path) -- call sites
# splat it, `[*ytconfig.AI_BIN, "-p", ...]`.
AI_BIN = TI.AI_BIN
AI_MODEL = TI.AI_MODEL
IDENTIFY_PROMPT_FILE = PROJECT_ROOT / "prompts" / "youtube_identify.md"
# Budget per video, clamped -- same shape as Torrent-Ingest's scaled identify timeout.
IDENTIFY_TIMEOUT_BASE_SEC = 600
IDENTIFY_TIMEOUT_PER_VIDEO_SEC = 25
IDENTIFY_TIMEOUT_MAX_SEC = 3600
IDENTIFY_MAX_ATTEMPTS = 3
IDENTIFY_RETRY_BACKOFF_SEC = 20
IDENTIFY_TRANSIENT_SIGNATURES = TI.IDENTIFY_TRANSIENT_SIGNATURES
# The third failure class, re-exported from Torrent-Ingest for the same reason as the line
# above: one definition of "what a usage limit looks like" for the whole fleet. A batch that
# hit the wall was never routed, so it must NOT be recorded as failed -- mark_failed_batch
# spends a retry-cap slot per attempt, and past the cap those videos are abandoned for good.
IDENTIFY_UNAVAILABLE_SIGNATURES = TI.IDENTIFY_UNAVAILABLE_SIGNATURES
# The classifier itself, not just its signature list: the two ingests must call a
# failure the same way, and a locally-rewritten predicate is how they drift apart.
identify_unavailable = TI.identify_unavailable
# The headless-run environment (PATH). launchd supplies none of the Homebrew tool
# directories, and the agent's `Probe` tool needs ffprobe on PATH.
ai_env = TI.ai_env

# How many videos are routed in one identify run. A batch is one PLAYLIST's new
# videos, further split to this size so a 200-video playlist doesn't become one
# enormous run that times out and retries forever.
IDENTIFY_BATCH_SIZE = 25

# The season every playlist-derived show files into. A YouTube playlist has no real
# season structure; splitting one into seasons invents information we don't have.
SEASON = 1

# --- Loop timing -------------------------------------------------------------

# How often the ingest re-enumerates the playlists for new additions. Kept far shorter
# than the old hourly cadence so a newly-added video/track is picked up promptly (the
# torrent ingest re-checks its watch folder on the order of tens of seconds; a flat
# playlist enumeration is the cheap equivalent here). Each full sweep costs one
# `yt-dlp --flat-playlist` call per registered playlist, so this stays modest enough to
# avoid hammering YouTube.
POLL_INTERVAL_SEC = int(os.environ.get("YOUTUBE_POLL_INTERVAL_SEC", "300"))
IDLE_INTERVAL_SEC = int(os.environ.get("YOUTUBE_IDLE_INTERVAL_SEC", "300"))

# --- Convenience -------------------------------------------------------------

log_stamp = TI.log_stamp        # one log format across both repos
