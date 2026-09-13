"""playlist_watch.py -- auto-maintain curated playlists as new episodes ingest.

Some curated playlists can never be "finished": One Piece airs weekly forever,
and this pipeline grabs each new episode. The "watchable" cut must therefore
extend itself. After a successful ingest, `ingest._advance_cleanup` calls
`consider_new_episodes(applied_dst_rels)`; for every show in
`config.PLAYLIST_AUTO_SHOWS`, each newly-placed episode is judged by a headless
AI run -- "is this episode worth watching, or is it the draggy stall/recap/
filler One Piece is infamous for?" -- and the keepers are appended to the show's
manifest and the playlist is rebuilt.

This is the same split as the identify step (README, "Safety invariants"):
**The model proposes, the harness disposes.** The judge only returns a keep/skip
verdict per exact episode path; it never moves, deletes, or reorders anything.
The verdict is persisted as a manifest entry under `state/playlists/` -- backed
up to MEGA with the rest of `state/` -- so the curation survives a Jellyfin wipe;
the Jellyfin playlist is just the rebuildable projection.

Bulk guard: if one ingest places more than `config.PLAYLIST_AUTO_MAX_INLINE` new
episodes of an auto-show (a complete-series pack, not a weekly drop), inline
judging is skipped -- judging hundreds synchronously would stall the daemon --
and a notice tells you to backfill deliberately:

    python3 playlist_watch.py --show "One Piece (1999)"     # judge every not-yet-considered episode
    python3 playlist_watch.py --show "One Piece (1999)" --dry-run   # judge + report, write nothing
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config              # noqa: E402
import playlist            # noqa: E402
import playlist_curation   # noqa: E402


def _log(msg: str, log_fn=None) -> None:
    line = f"[playlist_watch] {msg}"
    (log_fn or print)(line)


# The set of shows whose curated playlist auto-extends as new episodes ingest is
# no longer hard-coded to `config.PLAYLIST_AUTO_SHOWS` alone. playlist_curator.py
# decides per show whether a show warrants a playlist and, for the ONGOING ones,
# writes {show_rel: slug} to state/playlist_auto_shows.json. We merge the two so a
# newly-curated weekly show auto-extends without a code edit (config still wins as
# a hand-pinned override).
_AUTO_SHOWS_FILE = config.STATE_DIR / "playlist_auto_shows.json"


def _auto_shows() -> dict:
    merged = dict(getattr(config, "PLAYLIST_AUTO_SHOWS", {}) or {})
    try:
        if _AUTO_SHOWS_FILE.exists():
            merged.update(json.loads(_AUTO_SHOWS_FILE.read_text()))
    except Exception:                                                     # noqa: BLE001
        pass
    return merged


# --- the judge (headless AI run: propose only) -------------------------------
#
# Shares the whole-show curator's philosophy (playlist_curation.CUT_PHILOSOPHY):
# cut DEAD TIME read from the episode's own `.nfo` `<plot>`, keep everything where
# something happens. The per-show keep band + real KEEP/REMOVE examples are
# injected so a single-episode verdict is calibrated the same way a from-scratch
# curation is.

_JUDGE_PROMPT = """\
You are extending a "watchable" playlist for {show} -- a lean cut that keeps only
episodes where something HAPPENS and drops DEAD TIME.

{philosophy}
KEEP BAND for this show: about {band} of episodes are kept overall. Use it to
calibrate how selective to be; it is not a hard quota.
{examples}
Decide whether THIS ONE episode earns a place in that cut:

  {path}

Steps:
1. READ this episode's description first: open the sidecar `.nfo` next to the
   video (same path, `.nfo` extension) and read its `<plot>` and `<title>`. That
   on-disk synopsis is your primary evidence.
2. If the `.nfo` is missing or has no plot, read the S..E.. tag from the filename
   (for One Piece that is the absolute episode number) and look up what the
   episode covers (Fandom wiki, episode guides, filler lists); cross-check.
3. Apply the KEEP / CUT test above. When uncertain, lean KEEP for a real beat or
   introduction and CUT for stall/recap/filler.

Write ONLY this JSON object to the file {out} (no other output):
  {{"keep": true, "reason": "<one concise line: what it covers + why kept/cut>"}}
"""


def judge_episode(video_abs: Path, show_name: str, show_rel: str = "",
                  log_fn=None) -> dict | None:
    """Run the headless judge for one episode. Returns {"keep": bool,
    "reason": str} or None on failure (treated as 'skip for now', retry later)."""
    out_path = config.TMP_DIR / f"plwatch_{abs(hash(str(video_abs))) & 0xffffffff:08x}.json"
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    shows = [show_rel] if show_rel else []
    prompt = _JUDGE_PROMPT.format(
        show=show_name, path=str(video_abs), out=str(out_path),
        philosophy=playlist_curation.CUT_PHILOSOPHY,
        band=playlist_curation.band_for(shows),
        examples=playlist_curation.examples_block(shows),
    )
    cmd = [
        *config.AI_BIN, "-p",
        "--output-format", "json",
        "--tools", "Read,Glob,Grep,ListDir,WebSearch,WebFetch,Write",
        "--max-turns", str(config.PLAYLIST_JUDGE_MAX_TURNS),
        "--timeout", str(max(60, config.PLAYLIST_JUDGE_TIMEOUT_SEC - 30)),
    ]
    # The judge is a best-effort one-shot; it uses the first enabled FREE provider (§6.5).
    # A failure here just returns None ("skip for now, retry later"), so a single attempt
    # is enough — no fallback chain needed.
    free = config.enabled_ai_attempts()
    if free:
        cmd += ["--provider", free[0]["provider"], "--model", free[0]["model"]]
    elif config.AI_MODEL:
        cmd += ["--model", config.AI_MODEL]
    else:
        _log("no free AI provider configured; skipping judge", log_fn)
        return None

    env = config.ai_env()
    try:
        subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=config.PLAYLIST_JUDGE_TIMEOUT_SEC, env=env,
                       cwd=str(config.PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        _log(f"judge timed out on {video_abs.name}; will retry next time", log_fn)
        return None

    if not out_path.exists():
        _log(f"judge produced no verdict for {video_abs.name}; will retry next time", log_fn)
        return None
    try:
        verdict = json.loads(out_path.read_text())
        out_path.unlink()
    except json.JSONDecodeError:
        _log(f"judge verdict for {video_abs.name} was not valid JSON; skipping this run", log_fn)
        return None
    if not isinstance(verdict, dict) or "keep" not in verdict:
        _log(f"judge verdict for {video_abs.name} malformed; skipping this run", log_fn)
        return None
    return {"keep": bool(verdict.get("keep")), "reason": str(verdict.get("reason", "")).strip()}


# --- driving the auto-shows --------------------------------------------------

def _is_video(rel: str) -> bool:
    return Path(rel).suffix.lower() in config.VIDEO_EXTENSIONS


def _episodes_under(show_rel: str, rels: list[str]) -> list[str]:
    """The video files in ``rels`` that live under ``show_rel`` (a Season dir),
    excluding Season 00 specials (the playlist is the main watch order)."""
    prefix = show_rel.rstrip("/") + "/"
    out = []
    for r in rels:
        if not r.startswith(prefix) or not _is_video(r):
            continue
        if "/Season 00/" in r:      # specials aren't auto-added to the main cut
            continue
        out.append(r)
    return sorted(out)


def _consider_show(show_rel: str, slug: str, candidate_rels: list[str],
                   dry_run: bool, log_fn=None) -> int:
    """Judge each candidate episode of one show; append keepers to its manifest.
    Returns the number of episodes newly added. Rebuild is done by the caller."""
    show_name = Path(show_rel).name
    name = f"{Path(show_rel).name.split(' (')[0]} - Watchable"
    manifest = playlist.load_or_init_manifest(slug, show_rel, name)

    # Only consider episodes not already recorded (idempotent, resumable).
    pending = [r for r in candidate_rels if not playlist.manifest_has_path(manifest, r)]
    if not pending:
        return 0
    _log(f"{show_name}: {len(pending)} new episode(s) to judge", log_fn)

    added = 0
    for rel in pending:
        video_abs = config.MEDIA_ROOT / rel
        if not video_abs.exists():
            continue
        verdict = judge_episode(video_abs, show_name, show_rel, log_fn)
        if verdict is None:
            continue    # transient; leave unrecorded so a later run retries it
        tag = "KEEP" if verdict["keep"] else "skip"
        _log(f"  [{tag}] {Path(rel).name} -- {verdict['reason']}", log_fn)
        if verdict["keep"] and not dry_run:
            if playlist.append_path_item(slug, show_rel, name, rel, verdict["reason"]):
                added += 1
    return added


def consider_new_episodes(applied_dst_rels: list[str], log_fn=None) -> None:
    """Ingest hook. For each auto-show touched by this ingest, judge the newly
    placed episodes and (if any kept) rebuild that show's playlist. Best-effort:
    every failure is logged and swallowed -- this never breaks an ingest.

    Judging is a NON-ingestion AI run, so it only fires inside the off-peak
    window; outside it the hook returns and the nightly `playlist_curator` prowl
    (`extend_ongoing`) picks up the new episodes instead."""
    try:
        if not config.ai_budget_healthy():
            _log("off-peak window not active; deferring episode judging to the nightly prowl",
                 log_fn)
            return
        touched: list[tuple[str, str, list[str]]] = []
        for show_rel, slug in _auto_shows().items():
            eps = _episodes_under(show_rel, applied_dst_rels)
            if eps:
                touched.append((show_rel, slug, eps))
        if not touched:
            return

        rebuild_slugs: list[str] = []
        for show_rel, slug, eps in touched:
            if len(eps) > config.PLAYLIST_AUTO_MAX_INLINE:
                _log(f"{Path(show_rel).name}: {len(eps)} episodes in one ingest "
                     f"(> {config.PLAYLIST_AUTO_MAX_INLINE}); skipping inline judge. "
                     f"Backfill with: python3 playlist_watch.py --show \"{Path(show_rel).name}\"",
                     log_fn)
                continue
            if _consider_show(show_rel, slug, eps, dry_run=False, log_fn=log_fn):
                rebuild_slugs.append(slug)

        if rebuild_slugs:
            _rebuild(rebuild_slugs, log_fn)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"auto-update skipped (non-fatal): {exc}", log_fn)


def _rebuild(slugs: list[str], log_fn=None) -> None:
    """Rebuild the named playlists in Jellyfin from their manifests."""
    try:
        jf = playlist.Jellyfin()
    except Exception as exc:                                              # noqa: BLE001
        _log(f"Jellyfin unavailable ({exc}); manifest updated, playlist will "
             f"rebuild on the nightly run", log_fn)
        return
    for slug in slugs:
        try:
            playlist.build_playlist(playlist.manifest_path_for(slug), jf, dry_run=False)
        except Exception as exc:                                          # noqa: BLE001
            _log(f"rebuild of {slug} failed (non-fatal): {exc}", log_fn)


# --- CLI (manual backfill of an auto-show) -----------------------------------

def _all_episodes(show_rel: str) -> list[str]:
    """Every main-series episode file currently on disk under a show, as
    library-relative paths, in season/episode order."""
    show_dir = config.MEDIA_ROOT / show_rel
    rels: list[str] = []
    if not show_dir.is_dir():
        return rels
    for season in sorted(p for p in show_dir.iterdir() if p.is_dir()):
        if season.name == "Season 00":
            continue
        for f in sorted(season.iterdir()):
            if f.suffix.lower() in config.VIDEO_EXTENSIONS:
                rels.append(str(f.relative_to(config.MEDIA_ROOT)))
    return rels


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill an auto-maintained playlist "
                                             "by judging every not-yet-considered episode.")
    ap.add_argument("--show", required=True,
                    help='show folder name, e.g. "One Piece (1999)"')
    ap.add_argument("--dry-run", action="store_true",
                    help="judge and report; write no manifest changes, build nothing")
    args = ap.parse_args()

    show_rel = f"Shows/{args.show}"
    slug = _auto_shows().get(show_rel)
    if not slug:
        _log(f"{args.show} is not an auto-extend show (config.PLAYLIST_AUTO_SHOWS or state/playlist_auto_shows.json); the curator adds ongoing ones automatically")
        return 1

    eps = _all_episodes(show_rel)
    _log(f"{args.show}: {len(eps)} episode(s) on disk to consider")
    added = _consider_show(show_rel, slug, eps, dry_run=args.dry_run)
    _log(f"added {added} episode(s) to the manifest")
    if added and not args.dry_run:
        _rebuild([slug])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
