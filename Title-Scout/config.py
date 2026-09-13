"""Central configuration for Title-Scout.

Title-Scout is a one-shot, on-demand finder: you drop a single title into `find.txt` in
the iCloud Torrents folder, and the daemon searches a broad set of sources (torrent
trackers *and* direct-download archives), asks the free-model chain to confirm the candidate is the
RIGHT book/movie/album rather than a same-name different thing, then starts the download
-- a torrent added straight into qBittorrent, or a `.pdf`/`.epub`/... fetched directly.

It is deliberately separate from the fleet's media pipeline: it never reads
Media-Syncer's inventory, never writes `.torrent` files for Torrent-Ingest, and never
places anything into ~/Media. Its only outputs are (a) downloads landing in ~/Downloads
and (b) a record under `state/`.

Stdlib-only, same rationale as the rest of the fleet: the sibling repos run under
different conda envs, so a module-level `import requests` would move an ImportError to
daemon start. Everything talks to the network through `urllib`.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


# --- Log timestamps: LOCAL time, matching the fleet ---------------------------
def log_stamp() -> str:
    """`YYYY-MM-DD HH:MM:SS` in LOCAL time -- the fleet's human log format."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_stamp_iso() -> str:
    """Local time as full ISO-8601 WITH its UTC offset, for audit trails."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --- Roots -------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_DIR = PROJECT_ROOT / "state"
LOG_FILE = PROJECT_ROOT / "title_scout.log"

# The human inbox. Same iCloud Torrents folder the fleet uses, but this is the only
# document Title-Scout reads -- and it reads only its own `find.txt`.
TORRENTS_DIR = Path(
    "/Users/mikeyferguson/Library/Mobile Documents/com~apple~CloudDocs/Torrents"
)
FIND_TXT_FILE = TORRENTS_DIR / "find.txt"

# Where found content actually lands. A dedicated folder inside the iCloud Torrents
# folder (which the user already syncs), so the result is on all their devices. Kept
# clearly distinct from the fleet's ingest subfolders (queued/ingesting/finished/failed)
# -- the fleet only ever touches those four plus the top level, so it leaves "Scouted"
# alone. This is deliberately NOT ~/Media: Title-Scout starts the download and stops.
DOWNLOADS_DIR = TORRENTS_DIR / "Scouted"

# Torrent payloads never download straight into iCloud (partial-file writes fight the
# sync). They stage on the local fast disk first, then are moved into DOWNLOADS_DIR once
# qBittorrent reports them complete. Dot-prefixed for the same reason as the fleet's
# `.torrent-ingest` dir: it is an in-flight scratch area, not a destination.
TORRENT_STAGING_DIR = Path.home() / "Downloads" / ".title-scout"


# --- Sources -----------------------------------------------------------------

NYAA_BASE = "https://nyaa.si"
NYAA_RSS = NYAA_BASE + "/?page=rss&q={q}&c={c}&f=0&o=desc&s=seeders"

X1337_BASE = "https://1337x.to"
X1337_SEARCH = X1337_BASE + "/search/{q}/{page}/"

# apibay.org is a read-only JSON API over The Pirate Bay's database.
APIBAY_BASE = "https://apibay.org"
APIBAY_SEARCH = APIBAY_BASE + "/q.php?q={q}&cat={c}"

# eztv: a TV-focused indexer with a plain JSON API (no Cloudflare) — the cheap win for
# western TV that nyaa/1337x cover poorly. torrentdownloads is a long-lived HTML indexer
# that serves `.torrent` via `/download/...torrent`. Both were added so Title-Scout
# reaches the same western-TV complete packs the Torrent-Searcher does (§5 item 0).
EZTV_BASE = "https://eztv.re"
EZTV_SEARCH = EZTV_BASE + "/api/get-torrents?q={q}&limit={n}"

TORRENTDOWNLOADS_BASE = "https://www.torrentdownloads.me"
TORRENTDOWNLOADS_SEARCH = TORRENTDOWNLOADS_BASE + "/search/?search={q}"

# Internet Archive: a huge, Cloudflare-free direct-download source for public-domain
# books, scans, audio and film. Advancedsearch finds items; /metadata/<id> lists the
# files (and their direct /download/ URLs).
ARCHIVE_BASE = "https://archive.org"
ARCHIVE_ADV_SEARCH = ARCHIVE_BASE + "/advancedsearch.php"
ARCHIVE_METADATA = ARCHIVE_BASE + "/metadata/{ident}"
ARCHIVE_DOWNLOAD = ARCHIVE_BASE + "/download/{ident}/{name}"

# Library Genesis: the standard source for textbooks, papers and ebooks. Search
# `/index.php?req=...` returns HTML rows carrying a 32-hex `md5` per file; download is a
# two-hop `ads.php?md5=...` -> `get.php?md5=...&key=...` chain (the key is session-bound,
# so the adapter keeps a cookie jar). Mirrors are tried in order and the first reachable
# one wins -- the main `.is`/`.rs`/`.st` mirrors are frequently down, so the fork mirrors
# lead the list.
LIBGEN_MIRRORS = (
    "https://libgen.la",
    "https://libgen.li",
    "https://libgen.bz",
    "https://libgen.gl",
    "https://libgen.vg",
    "https://libgen.lc",
    "https://libgen.is",
    "https://libgen.rs",
    "https://libgen.st",
)

# Book-like kinds also search Library Genesis (direct PDF/EPUB, not torrents).
LIBGEN_KINDS = {"book", "manga", "comic", "audiobook", "other"}

# LibGen mirror requests are given a short timeout so a down mirror fails over fast
# rather than stalling the whole sweep.
LIBGEN_TIMEOUT_SEC = 12

# --- Anna's Archive (headless-browser "broader hunt" fallback) ---------------

# Anna's Archive is only searched when the primary sources came up empty: a "broader
# hunt" rather than an always-on source. Its /search endpoint sits behind a DDoS-Guard
# JavaScript challenge that a plain HTTP client cannot pass, so it needs a real browser
# -- we drive the system Chrome via `annas.js` over the Chrome DevTools Protocol
# (Node's built-in WebSocket, no npm deps). The `.org`/`.se`/`.li`/`.gs` domains are
# suspended or parked as of 2026; these are the live mirrors, tried in order.
ANNAS_ENABLED = os.environ.get("ANNAS_ENABLED", "1") == "1"
ANNAS_DOMAINS = (
    "annas-archive.pk",
    "annas-archive.gd",
    "annas-archive.gl",
)
ANNAS_JS = PROJECT_ROOT / "annas.js"
NODE_BIN = os.environ.get("NODE_BIN", "node")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# nyaa category per kind (all category ids documented in nyaa's RSS). "0_0" = all.
NYAA_CATEGORY_BY_KIND = {
    "anime": "1_0", "tv": "1_0", "movie": "1_0",
    "book": "3_0", "manga": "3_0", "comic": "3_0",      # Literature
    "audiobook": "2_0",                                  # Audio
    "other": "0_0",
}

# apibay (The Pirate Bay) category ids per kind. 601 = E-books, 602 = Comics,
# 102 = Audio books, 201/207 = Movies + HD-Movies, 205/208 = TV + HD-TV.
TPB_CATS_BY_KIND = {
    "anime": (205, 208), "tv": (205, 208),
    "movie": (201, 207),
    "book": (601,), "manga": (602,), "comic": (602,),
    "audiobook": (102,),
    "other": (601, 602),
}

# Internet Archive mediatype per kind, for the advancedsearch query.
ARCHIVE_MEDIATYPE_BY_KIND = {
    "book": "texts", "manga": "texts", "comic": "texts",
    "movie": "movies", "tv": "movies", "anime": "movies",
    "audiobook": "audio", "other": None,
}


# --- Cadence -----------------------------------------------------------------

# How often the daemon checks `find.txt` for a new request. Cheap: it only touches the
# network once a non-empty inbox is found, so this can be short without hammering the
# trackers.
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "60"))

# Per-source request throttle (seconds between requests to the same host).
REQUEST_DELAY_SEC = 1.5

# Min seeders a torrent result needs before we'll consider it. A zero-seeder torrent
# never completes; we skip it in favour of a seeded copy or a direct download.
MIN_SEEDERS = 1

# Maximum results to pull per (query, source).
MAX_RESULTS_PER_QUERY = 8

# How many archive.org items to inspect for a direct-download file.
ARCHIVE_MAX_ITEMS = 8

# Network timeouts (seconds).
HTTP_TIMEOUT = 25


# --- qBittorrent Web API -----------------------------------------------------

# Same client the fleet uses; WebUI is on port 8090 with localhost auth bypassed, so no
# credentials are needed for calls originating on 127.0.0.1.
QBT_HOST = "127.0.0.1"
QBT_PORT = 8090
QBT_BASE = f"http://{QBT_HOST}:{QBT_PORT}/api/v2"
QBT_CATEGORY = "title-scout"

# macOS bundle id, used to (re)launch the GUI app if the WebUI is unreachable.
QBT_BUNDLE_ID = "org.qbittorrent.qBittorrent"


# --- Free AI providers (Title-Scout's judgment chain) ------------------------
#
# Same free-provider registry the ingest and searcher use. Title-Scout makes two plain
# completions (interpret the request, verify the match); both now run on FREE models --
# OpenRouter's `:free` tier, spread across several UPSTREAM providers so a rate limit on
# one shared pool does not starve the fleet. A provider is enabled by its key being
# present (env var, else a key file under API_KEYS_DIR). the free-model chain, Gemini and every other
# paid API are deliberately OUT.
API_KEYS_DIR = Path.home() / ".config" / "api-keys"

AI_PROVIDERS = (
    {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1/chat/completions",
     "key_env": "OPENROUTER_API_KEY", "key_file": "openrouter_key",
     "models": (
         "nvidia/nemotron-3-super-120b-a12b:free",
         "minimax/minimax-m3:free",
         "minimax/minimax-m2.7:free",
         "dots-studio/dots-3-note-preview:free",
         "cohere/north-mini-code:free",
     )},
    # Independent free tiers so a daily cap on one provider never starves Title-Scout.
    {"name": "groq", "base_url": "https://api.groq.com/openai/v1/chat/completions",
     "key_env": "GROQ_API_KEY", "key_file": "groq_key",
     "models": (
         "openai/gpt-oss-120b",
         "openai/gpt-oss-20b",
     )},
    {"name": "cloudflare",
     "base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions",
     "key_env": "CLOUDFLARE_API_TOKEN", "key_file": "cloudflare_token",
     "account_env": "CLOUDFLARE_ACCOUNT_ID", "account_file": "cloudflare_account",
     "models": (
         "@cf/openai/gpt-oss-20b",
         "@cf/google/gemma-4-26b-a4b-it",
     )},
)


def _provider_key(provider):
    key = os.environ.get(provider["key_env"], "").strip()
    if key:
        return key
    try:
        key = (API_KEYS_DIR / provider["key_file"]).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return key


def _provider_account_id(provider):
    acct = os.environ.get(provider.get("account_env", ""), "").strip()
    if acct:
        return acct
    fname = provider.get("account_file", "")
    if not fname:
        return ""
    try:
        return (API_KEYS_DIR / fname).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _provider_base_url(provider):
    url = provider["base_url"]
    if "{account_id}" in url:
        acct = _provider_account_id(provider)
        if not acct:
            return ""
        url = url.replace("{account_id}", acct)
    return url


def enabled_ai_attempts():
    """The ordered [{provider, base_url, key, model}] with a key present, free-first."""
    out = []
    for p in AI_PROVIDERS:
        key = _provider_key(p)
        base_url = _provider_base_url(p)
        if not key or not base_url:
            continue
        for model in p["models"]:
            out.append({"provider": p["name"], "base_url": base_url, "key": key,
                        "model": model})
    return out


# Kept for back-compat only (an explicit manual override, unused by the chain above).
API_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_FILE = API_KEYS_DIR / "openrouter_key"
MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
MAX_TOKENS = 2000


# --- State file names --------------------------------------------------------

SEEN_FILE = STATE_DIR / "seen.json"        # key -> first-seen (dedupe, and "don't retry")
FOUND_FILE = STATE_DIR / "found.json"      # what we found + whether/how it downloaded
FIND_TXT_BACKUP = STATE_DIR / ".last_find_txt"  # safety copy before clearing the inbox
