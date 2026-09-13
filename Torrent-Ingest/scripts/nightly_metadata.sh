#!/bin/bash
# Nightly metadata safety net. Audits the whole show library for blank episodes
# (the "bare Episode N, no description" bug) and repairs any that have been on
# disk long enough that Jellyfin has demonstrably failed to scrape them. Run by
# launchd (com.mikeyferguson.torrentmetadata.plist), or by hand any time.
#
# The 48h age guard is the safeguard against fighting the scraper: an episode is
# only owned+filled once it has stayed blank for two nightly Jellyfin scans, so a
# newly-ingested episode Jellyfin simply hasn't gotten to yet is left alone.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

CONDA_PY="/opt/homebrew/Caskroom/miniconda/base/envs/torrent_ingest_env/bin/python3"
PYTHON="${CONDA_PY:-$(command -v python3)}"
[ -x "$CONDA_PY" ] || PYTHON="$(command -v python3)"

STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
echo "[$STAMP] nightly metadata audit + repair starting"

# 1) Audit -> worklist (also serves as a dated record of what was blank).
"$PYTHON" scripts/audit_metadata.py --json state/metadata_worklist.json || true

# 2) Repair anything blank for >= 48h (give the scraper its chance first). This one
#    call fixes BOTH classes the audit flags:
#      * blank EPISODES -> a locked episode .nfo written directly, and
#      * blank/mis-identified MOVIES -> the AI resolves the correct TMDB /movie/ id,
#        the harness pins it in a seed .nfo and asks Jellyfin to full-refresh, so the
#        rich .nfo + poster + backdrop land on disk (needs JELLYFIN_URL/API_KEY, set
#        in this agent's plist; skipped non-fatally if absent).
"$PYTHON" scripts/repair_metadata.py --worklist state/metadata_worklist.json --min-age-hours 48

# 3) Nudge Jellyfin to pick up the new locked .nfo (safe scan respects lockdata).
if [ -n "${JELLYFIN_URL:-}" ] && [ -n "${JELLYFIN_API_KEY:-}" ]; then
    curl -fsS -X POST "$JELLYFIN_URL/Library/Refresh" \
        -H "X-Emby-Token: $JELLYFIN_API_KEY" >/dev/null 2>&1 \
        && echo "[$(date '+%Y-%m-%d %H:%M:%S')] triggered Jellyfin library refresh" \
        || echo "[$(date '+%Y-%m-%d %H:%M:%S')] Jellyfin refresh nudge failed (non-fatal)"
fi

# 3b) Rebuild curated playlists from their manifests (state/playlists/*.json).
#     Idempotent: an unchanged playlist is left untouched; a missing or altered
#     one is (re)created from the manifest -- so this self-heals a playlist lost
#     to Jellyfin's scan-deletion bug or a wiped data dir. The manifest is the
#     source of truth (backed up with all of state/ in step 5); Jellyfin's own
#     playlist store is just a rebuildable projection. Non-fatal.
if [ -n "${JELLYFIN_URL:-}" ] && [ -n "${JELLYFIN_API_KEY:-}" ]; then
    "$PYTHON" playlist.py \
        && echo "[$(date '+%Y-%m-%d %H:%M:%S')] playlists rebuilt from manifests" \
        || echo "[$(date '+%Y-%m-%d %H:%M:%S')] playlist rebuild failed (non-fatal, will retry)"
fi

# 4) Fill any show missing an on-disk poster (the migrated-show "no cover" bug).
#    Runs after the Jellyfin refresh so a just-scraped show gets its own art
#    first; only shows still bare are filled from Jellyfin's providers. Non-fatal.
"$PYTHON" scripts/save_posters.py \
    && echo "[$(date '+%Y-%m-%d %H:%M:%S')] poster backstop done" \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] poster backstop failed (non-fatal, will retry)"

# 5) Back up the metadata Media-Syncer never touches: the Jellyfin sidecars
#    (.nfo + artwork, including any poster step 4 just wrote) and this repo's
#    state/ audit trail. Runs last so it captures the freshly-owned .nfo -- the
#    irreplaceable-by-scraping ones. Non-fatal: logged and retried next run.
"$PYTHON" scripts/backup_metadata.py \
    && echo "[$(date '+%Y-%m-%d %H:%M:%S')] metadata backup complete" \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] metadata backup failed (non-fatal, will retry)"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] nightly metadata run done"
