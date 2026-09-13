#!/bin/bash
# Split-tunnel: route specific networks AROUND the Tailscale/Mullvad exit node.
#
# The VPN's only job is MEGA throttle avoidance (and torrent privacy). Anything that is
# NOT MEGA and NOT a torrent gains nothing from exiting through a rotating Mullvad IP,
# and two services are actively BROKEN by it. Both are pinned to the physical gateway
# here, and MEGA is untouched (it is on its own infra, api.mega.co.nz), so avoidance is
# not weakened at all. Torrent DOWNLOADS keep exiting through Mullvad — the P2P media
# transfer is qBittorrent's job and uses the default route (the exit node); only the
# searcher's plain-HTTP indexer SEARCH (and .torrent metadata) is pinned around the VPN,
# because that is plain web browsing, a different risk class from the P2P transfer.
#
# 1. AI PROVIDERS (Groq, Cloudflare Workers AI, OpenRouter, DeepSeek) -- exit-node ROTATION
#    resets an in-flight API connection, and Groq additionally BLOCKS datacenter IPs
#    outright (403 "Access denied. Please check your network settings."), which every Mullvad
#    exit is. These carry every unattended judgment the fleet makes (ingest identify, the
#    searcher cull/discovery, the playlist judge, the media doctor, the YouTube router), so
#    they all bypass the VPN to the home residential IP. They CANNOT be pinned by a stable
#    allocation: every one is a CloudFront/Cloudflare alias with no dedicated prefix, so we
#    pin only what each hostname currently RESOLVES to, refreshed every time this daemon
#    re-asserts, and accept that a resolution which changes between assertions rides the
#    VPN until the next one.
#
#    That gap is tolerable now in a way it was not for the streaming CLI this fleet used
#    to run. An agent turn is a short discrete request, not one thirty-minute stream, and
#    `Torrent-Ingest/ai_client.py` retries a failed request four times with backoff before
#    the caller's own retry budget is touched at all. A rotation now costs a retried turn;
#    it used to cost the whole run.
#
# 2. GOOGLE / YOUTUBE -- the YouTube ingest (~/Developer/YouTube-Downloader) authenticates
#    as the account owner with a session cookie. Mullvad exits are commercial-VPN
#    DATACENTER IPs, which YouTube challenges hard, and for an authenticated request its
#    response is not a solvable captcha -- it REVOKES the session. Rotation makes it
#    worse: one Google session appearing from a different country every rotation is
#    indistinguishable from a stolen cookie, and revoking it is the correct thing for
#    Google to do. The result is cookies that die in days and an ingest that needs a
#    manual re-export constantly.
#
#    Google publishes its netblocks as JSON (GOOG_URL below) -- ~99 IPv4 prefixes,
#    regenerated daily. That is few enough to pin as static routes and authoritative,
#    unlike a `dig` snapshot that goes stale. So YouTube always sees the home IP: one
#    stable residential address, no revocation, and the ingest's cookie jar renews itself
#    on every run instead of expiring.
#
#    TRADEOFF, stated plainly: the ISP sees YouTube traffic as YouTube traffic. That is
#    a different risk class from
#    torrenting, which stays behind the VPN.
#
#    IPv6 IS DELIBERATELY NOT PINNED. Pinning v6 needs the physical interface's v6 router,
#    which Tailscale owns the discovery of, and a half-working v6 route is worse than
#    none: v4 going direct while v6 still goes through the tunnel would silently defeat
#    the whole point. Instead the leak is closed at the other end -- the YouTube ingest
#    forces IPv4 for every yt-dlp call (`--force-ipv4` in its fetch/discover args), so it
#    can never reach Google over v6 through the exit node. Other Google traffic (a browser)
#    may still use v6 over the VPN; that is irrelevant to this.
#
# Requires root (route changes do). Install as a LaunchDaemon so it survives reboots and
# re-asserts periodically (gateway changes / Tailscale re-writes):
#   sudo cp scripts/split_tunnel.sh /usr/local/bin/split_tunnel.sh
#   sudo chmod +x /usr/local/bin/split_tunnel.sh
#   sudo cp com.mikeyferguson.splittunnel.plist /Library/LaunchDaemons/
#   sudo launchctl bootstrap system /Library/LaunchDaemons/com.mikeyferguson.splittunnel.plist
# To undo: sudo launchctl bootout system /Library/LaunchDaemons/com.mikeyferguson.splittunnel.plist
# then reboot (routes are not persistent).
#
#   --dry-run   print what would be pinned and exit; changes nothing, needs no root.
set -uo pipefail
export PATH="/sbin:/usr/sbin:/bin:/usr/bin:/opt/homebrew/bin:/usr/local/bin"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# Self-staleness check. Installing this means COPYING it to /usr/local/bin, so the file
# running is a snapshot and the repo can move on without it. That divergence is invisible
# from the outside -- the routes it pins are still up, so `route -n get` says everything is
# fine -- and it only bites at the next boot, when the OLD script is what runs. Since
# updating needs sudo, it is also the step most often skipped after a change here. So the
# running copy compares itself to the repo and says so.
#
# HOME is NOT set for a root LaunchDaemon -- launchd hands the job a minimal environment.
# Combined with `set -u` that is a FATAL error, so this check, written to catch a silent
# divergence, became a silent kill of its own: every scheduled run died on this line
# before pinning a single route (21 consecutive failures in /tmp/split_tunnel.err on
# 2026-08-06). The routes already in the kernel table kept the split tunnel *looking*
# healthy, because nothing removes them until a reboot or a Tailscale route rewrite --
# at which point the daemon that exists to re-assert them would not have run in hours.
# So: fall back to the console user's home, and treat "no home found" as "skip the
# check", never as a reason to abort the routing work that is the point of the script.
HOME_DIR="${HOME:-}"
if [ -z "$HOME_DIR" ]; then
    CONSOLE_USER="$(stat -f%Su /dev/console 2>/dev/null || true)"
    if [ -n "$CONSOLE_USER" ] && [ "$CONSOLE_USER" != "root" ]; then
        HOME_DIR="$(dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
    fi
fi
REPO_COPY="${HOME_DIR:-/nonexistent}/Developer/Media-Syncer/scripts/split_tunnel.sh"
SELF="${BASH_SOURCE[0]}"
if [ -f "$REPO_COPY" ] && [ "$(cd "$(dirname "$SELF")" && pwd)/$(basename "$SELF")" != "$REPO_COPY" ]; then
    if ! cmp -s "$SELF" "$REPO_COPY"; then
        echo "split-tunnel: WARNING the installed copy ($SELF) DIFFERS from the repo copy."
        echo "split-tunnel:   Routes below are from the OLD script; the repo version runs"
        echo "split-tunnel:   only after: sudo cp '$REPO_COPY' '$SELF'"
    fi
fi

# Google's published netblocks. Cached locally so a failed/offline fetch NEVER drops the
# routes -- a stale cache is vastly better than reverting YouTube onto the exit node,
# which is what silently burns the session cookie.
GOOG_URL="https://www.gstatic.com/ipranges/goog.json"
CACHE_DIR="/usr/local/share/split-tunnel"
GOOG_CACHE="$CACHE_DIR/goog.json"
GOOG_MAX_AGE_SEC=86400          # refresh at most daily; the upstream file regenerates daily

# Physical gateway (independent of the routing table, which Tailscale owns): the DHCP router
# on whichever wired/wireless interface has one.
#
# WAIT for it rather than giving up immediately. This runs at BOOT (RunAtLoad on a system
# daemon), which is before DHCP has necessarily handed out a router -- and the old
# behaviour of exiting straight away meant nothing was pinned until StartInterval fired
# THIRTY MINUTES later. The YouTube ingest's LaunchAgent starts at login, comfortably
# inside that window, so it would authenticate to YouTube through the VPN exit and risk
# having the session revoked -- precisely what these routes exist to prevent. Polling for
# up to a minute covers normal boot; past that we exit and the 30-minute re-assert (plus
# the ingest's own egress gate) is the backstop.
GW=""
for attempt in $(seq 1 30); do
    for IF in en0 en1 en2 en3; do
        G=$(ipconfig getoption "$IF" router 2>/dev/null || true)
        if [ -n "$G" ]; then GW="$G"; break; fi
    done
    [ -n "$GW" ] && break
    sleep 2
done
[ -z "$GW" ] && { echo "split-tunnel: no physical gateway after 60s; leaving routes unchanged."; exit 0; }

# --- 0. Cached resolution ------------------------------------------------------
# Every /32 block below is pinned from what a hostname RESOLVES TO right now, and until
# now a failed `dig` simply yielded nothing -- the block came out empty, the routes were
# not re-asserted, and the traffic they exist to keep OFF the exit node went back onto it
# with only a WARNING line to show for it.
#
# That is precisely backwards during the outage it is most likely to happen in. DNS on this
# box goes to Tailscale's MagicDNS, which forwards upstream THROUGH the exit node -- so the
# moment the exit node stops carrying traffic, every `dig` here fails, and the script's
# response is to stop protecting the very services the exit node has just broken. Observed
# 2026-09-01: the exit node went dead at ~17:00 and took all DNS with it.
#
# So resolutions are cached, exactly as goog.json already is, and a `dig` that returns
# nothing falls back to the last known answer. A stale IP is a far better bet than no route.
RESOLVE_CACHE="$CACHE_DIR/resolved"
resolve_cached() {
    # Separate declarations: under `set -u`, referring to `host` inside the same `local`
    # statement that creates it is an unbound-variable error, not a forward reference.
    local host="$1"
    local cache="$RESOLVE_CACHE/$host"
    local fresh=""
    fresh="$(dig +short +time=3 +tries=1 "$host" 2>/dev/null | grep -E '^[0-9]+\.' || true)"
    if [ -n "$fresh" ]; then
        if [ "$DRY_RUN" -eq 0 ]; then
            mkdir -p "$RESOLVE_CACHE" 2>/dev/null || true
            printf '%s\n' "$fresh" > "$cache.tmp" 2>/dev/null && mv "$cache.tmp" "$cache" 2>/dev/null
        fi
        printf '%s\n' "$fresh"
        return 0
    fi
    [ -r "$cache" ] && cat "$cache"
}

# --- 1. AI providers: live host resolutions ONLY -------------------------------
# No dedicated allocation to pin (CloudFront / Cloudflare); see the header. /32s only,
# deliberately: widening to the enclosing CDN prefix would take most of the web off the VPN.
# Groq is the load-bearing one: its API BLOCKS datacenter IPs outright (403 "Access denied.
# Please check your network settings."), so it MUST bypass the VPN to the home residential
# IP. OpenRouter and Cloudflare Workers AI are pinned alongside so exit-node rotation never
# resets their in-flight calls either (the same reason DeepSeek was). api.deepseek.com is
# kept for the interactive assistant key that lives on this box.
AI=""
for h in api.groq.com api.cloudflare.com openrouter.ai api.deepseek.com; do
    for ip in $(resolve_cached "$h"); do
        AI="$AI ${ip}/32"
    done
done
[ -z "$AI" ] && echo "split-tunnel: WARNING no AI host resolved; the fleet AI calls will exit via the VPN this cycle."

# --- 1b. Anthropic: the ASSISTANT's own API ------------------------------------
# Distinct from the fleet's AI providers above. The assistant that operates this box talks
# to api.anthropic.com, and that traffic was taking the VPN like everything else -- so a
# Mullvad exit-node rotation reset an in-flight request and surfaced as an API error in the
# middle of a working session. Same failure mode the providers above are pinned for.
# Unlike the CloudFront/Cloudflare-fronted providers, Anthropic has its OWN allocation, so
# the PREFIX is pinned directly rather than chasing /32s -- a DNS change cannot then
# silently unpin it. Live resolutions are appended as a belt-and-braces in case the
# allocation ever moves.
# TRADEOFF, and it is the same one already accepted for every other AI provider here: the
# ISP sees traffic to Anthropic from the home IP rather than the VPN exit.
ANTHROPIC="160.79.104.0/23"
for h in api.anthropic.com claude.ai console.anthropic.com statsig.anthropic.com; do
    for ip in $(resolve_cached "$h"); do
        ANTHROPIC="$ANTHROPIC ${ip}/32"
    done
done

# --- 2. Torrent INDEXERS: search + .torrent metadata, pinned around the VPN ---------
# The searcher's job is FINDING .torrent files: it makes plain HTTPS GETs to indexer
# SEARCH pages and downloads the small .torrent METADATA files (KBs, not media). The
# actual media download is qBittorrent's P2P, which uses the DEFAULT route and keeps
# exiting through Mullvad — nothing here changes that. Two things are broken by the
# datacenter exit and fixed by the home IP:
#   * 1337x and eztv sit behind Cloudflare, which 403s / rate-limits / serves a decoy
#     "popular torrents" page to the Mullvad exit IP (the exact block the AI providers hit).
#   * nyaa.si and the itorrents .torrent cache serve TRUNCATED .torrent files through the
#     exit node — the node ROTATES and resets an in-flight download, so the KB-sized
#     .torrent arrives cut short and the searcher records a magnet instead of a real file.
# TRADEOFF: the ISP sees 1337x/nyaa/... search + metadata traffic as traffic to those
# sites — but the P2P download (the part that matters) stays on the VPN.
INDEXERS=""
for h in 1337x.to 1377x.to 1337x.st 1337x.is x1337x.ws x1337x.eu 1337x.bz \
         1337xx.to 1337xxx.to www.1377x.to \
         eztv.re eztvx.to eztv.it eztv.ch \
         www.torrentdownloads.me www.torrentdownloads.pro \
         www.limetorrents.lol limetorrents.info \
         nyaa.si nyaa.land itorrents.net itorrents.org; do
    for ip in $(resolve_cached "$h"); do
        INDEXERS="$INDEXERS ${ip}/32"
    done
done
[ -z "$INDEXERS" ] && echo "split-tunnel: WARNING no indexer host resolved; torrent search will exit via the VPN this cycle."

# --- 2b. METADATA ORACLES: the free episode/series guides ----------------------
# TVMaze (`epguide`) and AniList are how placement checks its work against a FACT rather
# than a model's guess: TVMaze supplies the season CEILING that stops a pack being filed
# into a season the show never aired, and the episode-title list that catches a pack filed
# under the wrong season number; AniList backs the comic franchise table. Both are free and
# key-less, which is why the fleet is allowed to depend on them at all.
#
# They are pinned for the same reason the AI providers are, and for one that is worse. Every
# lookup here FAILS SOFT by design -- a metadata lookup must never block an ingest -- so
# when the exit node stops carrying traffic the guards do not fail, they simply stop
# guarding, and nothing says so. On 2026-09-01 the season ceiling was inert for five hours
# and the only trace was the placement replay quietly resolving fewer shows.
#
# There is no privacy argument for routing them over the VPN: an anonymous GET for a
# cartoon's episode list is not the traffic Mullvad is paid for, and it is the same class
# as the indexer SEARCH traffic already pinned above.
ORACLES=""
for h in api.tvmaze.com graphql.anilist.co; do
    for ip in $(resolve_cached "$h"); do
        ORACLES="$ORACLES ${ip}/32"
    done
done
[ -z "$ORACLES" ] && echo "split-tunnel: WARNING no metadata oracle resolved; the season ceiling and franchise table will exit via the VPN this cycle."

# --- 3. Google: published IPv4 prefixes, cached ---------------------------------
fetch_goog() {
    # Refresh only if the cache is missing or older than GOOG_MAX_AGE_SEC. Written via a
    # temp file and only promoted if it PARSES and yields prefixes, so a truncated
    # download or a captive-portal HTML page can never replace a good cache.
    [ "$DRY_RUN" -eq 1 ] || mkdir -p "$CACHE_DIR" 2>/dev/null || true
    if [ -f "$GOOG_CACHE" ]; then
        local age
        age=$(( $(date +%s) - $(stat -f %m "$GOOG_CACHE" 2>/dev/null || echo 0) ))
        [ "$age" -lt "$GOOG_MAX_AGE_SEC" ] && return 0
    fi
    local tmp="${GOOG_CACHE}.tmp.$$"
    if curl -fsS --max-time 25 "$GOOG_URL" -o "$tmp" 2>/dev/null; then
        if python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if [p for p in d.get('prefixes',[]) if 'ipv4Prefix' in p] else 1)" "$tmp" 2>/dev/null; then
            mv -f "$tmp" "$GOOG_CACHE" 2>/dev/null || rm -f "$tmp"
            return 0
        fi
    fi
    rm -f "$tmp" 2>/dev/null || true
    return 1
}

GOOGLE=""
if [ "$DRY_RUN" -eq 1 ] && [ ! -f "$GOOG_CACHE" ]; then
    # Dry-run on a machine that has never installed the daemon: fetch to a temp path so the
    # preview is real rather than empty, without needing to write the root-owned cache.
    TMPJ="$(mktemp -t goog)"
    curl -fsS --max-time 25 "$GOOG_URL" -o "$TMPJ" 2>/dev/null && GOOG_CACHE="$TMPJ"
else
    fetch_goog || echo "split-tunnel: goog.json refresh failed; using cached prefixes if present."
fi
if [ -f "$GOOG_CACHE" ]; then
    GOOGLE=$(python3 -c "
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print(' '.join(p['ipv4Prefix'] for p in d.get('prefixes',[]) if 'ipv4Prefix' in p))
" "$GOOG_CACHE" 2>/dev/null || true)
fi
[ -z "$GOOGLE" ] && echo "split-tunnel: WARNING no Google prefixes available; YouTube will exit via the VPN this cycle."

# --- apply ---------------------------------------------------------------------
pin() {
    local label="$1"; shift
    local n=0
    for cidr in $@; do
        if [ "$DRY_RUN" -eq 1 ]; then
            n=$((n+1)); continue
        fi
        route -n add -net "$cidr" "$GW" 2>/dev/null \
            || route -n change -net "$cidr" "$GW" 2>/dev/null || true
        n=$((n+1))
    done
    echo "split-tunnel: ${label}: $n route(s) via $GW"
}

if [ "$DRY_RUN" -eq 1 ]; then
    echo "split-tunnel DRY RUN -- nothing changed. Physical gateway: $GW"
    echo "  AI prefixes:  $(echo $AI | wc -w | tr -d ' ')"
    echo "  Anthropic prefixes: $(echo $ANTHROPIC | wc -w | tr -d ' ')"
    echo "  Indexer prefixes:    $(echo $INDEXERS | wc -w | tr -d ' ')"
    echo "  Oracle prefixes:     $(echo $ORACLES | wc -w | tr -d ' ')"
    echo "  Google prefixes:    $(echo $GOOGLE | wc -w | tr -d ' ')"
    echo "  first few Google:   $(echo $GOOGLE | cut -d' ' -f1-6)"
    exit 0
fi

pin "AI providers (Groq/Cloudflare/OpenRouter/DeepSeek bypass the VPN)" $AI
pin "Anthropic (the assistant's own API - stable home IP so exit rotation cannot reset a call)" $ANTHROPIC
pin "Torrent indexers (1337x/eztv/torrentdownloads/limetorrents search bypass the VPN)" $INDEXERS
pin "Metadata oracles (TVMaze/AniList - the season ceiling must not die with the exit node)" $ORACLES
pin "Google/YouTube (stable home IP for the YouTube ingest)" $GOOGLE
