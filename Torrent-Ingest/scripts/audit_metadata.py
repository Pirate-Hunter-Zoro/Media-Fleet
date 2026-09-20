#!/usr/bin/env python3
"""Audit the Jellyfin library for missing/blank/mis-identified metadata.

The deterministic safety net for two disk-detectable failures:

  * SHOWS -- "episode is in the right show but renders as a bare 'Episode N' with
    no description." Walks every show and checks each episode's sidecar .nfo the
    way Jellyfin reads it (library.episode_is_blank): missing sidecar or empty
    <plot>. With --json, writes a worklist that repair_metadata.py consumes.
  * MOVIES -- a film that was never identified (missing/blank <plot>, e.g. an
    oddly-titled special) OR two distinct films sharing one <tmdbid> (the
    collection-sibling mis-match, e.g. a trilogy's Part II filed under Part I).
    Movies/ must be inside the walk; leaving it out lets both slip the net.
  * SPECIAL/MOVIE COLLISION -- a Season-0 special whose .nfo shares a tmdb/imdb id
    with a film we hold in Movies/, i.e. Jellyfin scraped that separate film's
    entry ONTO the special (the Kim Possible case: the "So the Drama" film landed
    on an "A Sitch in Time" special). Wrong-but-populated, so the plot-centric
    blank test can't see it. Detection-only (manual review), like the One Pace
    title mismatches — new ingests can't produce it now (apply locks all specials).

Read-only and re-runnable. Run it after ingests or on a schedule; any non-zero
count is something the scraper/identify step got wrong.

Usage:
    python3 scripts/audit_metadata.py                 # shows + movies with issues
    python3 scripts/audit_metadata.py --all           # include healthy items too
    python3 scripts/audit_metadata.py --show Gintama  # one show; skips movie scan
    python3 scripts/audit_metadata.py --no-movies     # show episodes only
    python3 scripts/audit_metadata.py --json state/metadata_worklist.json
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import library     # noqa: E402

ONE_PACE_FOLDER = Path(config.ONE_PACE_PREFIX).name          # "One Pace (2013)"

# A multi-episode file names a range: "S01E01-E02", "S01E01-02", "S01E01E02".
# It occupies MORE THAN ONE absolute slot, so the running absolute index must
# advance by the whole span — otherwise every episode after a merged file runs
# one (or more) behind its true absolute number, and the repair tool, which
# trusts `abs` over the on-disk SxxExx, would look up and write the WRONG
# episode's title/plot (a populated-but-wrong .nfo the plot-centric test can
# never re-detect). Gintama ships eps 1-2 as one "S01E01-E02" file, which is
# exactly this case.
_EP_RANGE_RE = re.compile(r"[Ss]\d+[Ee](\d+)(?:\s*-\s*[Ee]?|[Ee])(\d+)")


def _episode_span(filename):
    """Number of episodes a file represents: 1 normally, or (end - start + 1)
    for a SxxExx-Eyy range. Defaults to 1 when no clean range is found or the
    range is malformed (end < start)."""
    m = _EP_RANGE_RE.search(filename)
    if not m:
        return 1
    start, end = int(m.group(1)), int(m.group(2))
    return end - start + 1 if end >= start else 1


def container_title(video_path):
    """The episode title One Pace embeds in the mkv container `title` tag, with any
    leading '<Arc> NN - ' prefix stripped, e.g. 'Wano 60 - Conqueror's Haki' ->
    'Conqueror's Haki'. This is the authoritative One Pace title source and the key
    the audit uses to detect a .nfo whose title belongs to the WRONG arc — a
    populated-but-wrong sidecar the plot-centric blank test cannot see. Returns ''
    if ffprobe is unavailable or the file has no title tag."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=title",
             "-of", "default=nokey=1:noprint_wrappers=1", str(video_path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""
    return re.sub(r"^[A-Za-z0-9'&.\- ]+?\s+\d+\s*-\s*", "", out).strip()


def _norm_title(s):
    """Collapse to lowercase alphanumerics for a forgiving title comparison
    (punctuation/spacing differences between a filename and a .nfo are not a bug)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _titles_conflict(nfo_title, container_title):
    """True only when a .nfo title and the mkv's embedded title genuinely CONFLICT
    — not merely differ. One-word articles, plural 's', romanization variants, and
    fuller-vs-shorter forms are NOT conflicts (they are the common, harmless case);
    reporting them would bury the real wrong-arc hits. Requires both present, a
    normalized difference, neither a substring of the other, and low overlap of
    significant (3+ letter) words."""
    if not nfo_title or not container_title:
        return False
    na, nb = _norm_title(nfo_title), _norm_title(container_title)
    if not na or not nb or na == nb or na in nb or nb in na:
        return False
    words = lambda s: {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) >= 3}
    wa, wb = words(nfo_title), words(container_title)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) < 0.34


def _show_title_year(show_dir):
    """('Naruto', 2002) from a 'Naruto (2002)' folder name."""
    m = re.search(r"\((\d{4})\)\s*$", show_dir.name)
    year = int(m.group(1)) if m else None
    title = re.sub(r"\s*\(\d{4}\)\s*$", "", show_dir.name).strip()
    return title, year


def _tvshow_ids(show_dir):
    """Pull tmdb/tvdb ids from the show's tvshow.nfo if present (lookup hints)."""
    text = library._read_text(show_dir / "tvshow.nfo") or ""
    return {
        "tmdb_id": library._xml_tag(text, "tmdbid") or None,
        "tvdb_id": library._xml_tag(text, "tvdbid") or None,
    }


def audit_show(show_dir, movie_ids=None):
    """Return a per-show record. `blanks` carries the on-disk season/episode AND a
    computed absolute index (1..N over the whole show in air order) — the latter is
    the reliable key for anime per-episode metadata lookups when the on-disk
    season split is custom.

    `movie_ids` (a {provider-id -> film title} map from _movie_id_index) enables the
    Season-0 special/movie collision check: a special whose .nfo shares a tmdb/imdb
    id with a film we hold in Movies/ means Jellyfin scraped that film's entry ONTO
    the special — wrong-but-populated, so invisible to the plot-centric blank test."""
    title, year = _show_title_year(show_dir)
    ids = _tvshow_ids(show_dir)
    movie_ids = movie_ids or {}

    # Order every episode across all seasons, then assign a stable absolute index
    # counting ONLY main-series episodes (season >= 1). Season 00 specials are
    # excluded from the count — interleaving them would shift every episode's
    # absolute number and break the "abs == real episode number" lookup key that
    # anime metadata is best resolved by. Specials keep abs=None and a flag.
    episodes = []
    for video in library.iter_episode_videos(show_dir):
        m = library._EP_RE.search(video.name)
        season = int(m.group(1)) if m else None
        episode = int(m.group(2)) if m else None
        episodes.append((season if season is not None else 9999,
                         episode if episode is not None else 9999, video, season, episode))
    episodes.sort(key=lambda e: (e[0], e[1]))

    is_op = show_dir.name == ONE_PACE_FOLDER
    abs_idx = 0
    blanks = []
    mismatches = []
    collisions = []
    for _s, _e, video, season, episode in episodes:
        is_special = (season == 0)
        # A merged "SxxExx-Eyy" file spans several episodes; advance the absolute
        # index by the whole span so files after it keep their true absolute
        # number. The file's own abs is the FIRST episode in its range.
        span = _episode_span(video.name) if not is_special else 1
        file_abs = None if is_special else abs_idx + 1
        if not is_special:
            abs_idx += span
        base = {
            "video": str(video),
            "season": season,
            "episode": episode,
            "abs": file_abs,
            "special": is_special,
            "filename": video.name,
        }
        if library.episode_is_blank(video, title):
            blanks.append({**base, "reason": "blank"})
            continue
        # A Season-0 special that shares a TMDB/IMDb id with a film we hold in
        # Movies/ means Jellyfin scraped that separate movie's entry ONTO the
        # special (the Kim Possible case: the "So the Drama" film's imdb id landed
        # on an "A Sitch in Time" special). Wrong-but-POPULATED, so the plot-centric
        # blank test can never see it — exactly the movie-dup-id failure, one axis
        # over. DETECTION-ONLY and high-signal (an exact id match, not a fuzzy
        # title): surfaced for review, never auto-repaired, because the right fix
        # (own the special / move it out) is a judgment call. Fixed going forward by
        # validate_plan + _write_owned_nfo, which lock every ingested special.
        if is_special and movie_ids:
            nfo_text = library._read_text(library.episode_nfo_path(video))
            for tag in ("tmdbid", "imdbid"):
                sid = library._xml_tag(nfo_text, tag) if nfo_text else ""
                if sid and sid in movie_ids:
                    collisions.append({
                        **base,
                        "nfo_title": library._xml_tag(nfo_text, "title") if nfo_text else "",
                        "shared_id": f"{tag[:-2]}:{sid}",
                        "movie": movie_ids[sid],
                    })
                    break
        if is_op:
            # One Pace only: a populated .nfo whose <title> CONFLICTS with the mkv's
            # own embedded title can be wrong-but-present (a Wano episode carrying an
            # Impel Down title/plot) — invisible to the plot-centric blank test. This
            # is DETECTION-ONLY: title variants ("Six Powers" vs "The Six Powers")
            # are common and harmless, so the check is conservative (real word
            # conflict, not a variant) and the hits are surfaced for MANUAL review,
            # never auto-repaired (auto-rewriting variants would churn forever).
            ct = container_title(video)
            nfo_text = library._read_text(library.episode_nfo_path(video))
            nfo_title = library._xml_tag(nfo_text, "title") if nfo_text else ""
            if _titles_conflict(nfo_title, ct):
                mismatches.append({**base, "nfo_title": nfo_title, "container_title": ct})

    return {
        "show": show_dir.name,
        "show_title": title,
        "year": year,
        "dir": str(show_dir),
        "tmdb_id": ids["tmdb_id"],
        "tvdb_id": ids["tvdb_id"],
        "episodes": len(episodes),
        "blank": len(blanks),
        "blanks": blanks,
        "title_mismatches": mismatches,   # One Pace only; detection-only (manual review)
        "special_movie_collisions": collisions,  # detection-only (manual review)
    }


def _movie_id_index():
    """Map every provider id (tmdb + imdb) found in a Movies/ .nfo -> that film's
    title. The key for the special/movie collision check in audit_show: if a
    Season-0 special's .nfo carries an id in here, Jellyfin scraped that film's
    entry onto the special. Cheap (a handful of small .nfo reads)."""
    index = {}
    for video in _movie_videos():
        text = library._read_text(video.with_suffix(".nfo"))
        if not text:
            continue
        for tag in ("tmdbid", "imdbid"):
            sid = library._xml_tag(text, tag)
            if sid:
                index[sid] = video.stem
    return index


def _movie_videos():
    """Every movie video file directly under Movies/. This library keeps movies
    flat as `Title (YYYY).ext` (see build_library_digest), so a shallow scan is
    both sufficient and cheap on the spinning disk."""
    if not config.MOVIES_ROOT.exists():
        return []
    return sorted(p for p in config.MOVIES_ROOT.iterdir()
                  if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS)


def audit_movies():
    """Audit Movies/ for the two disk-detectable failures the show audit never
    covered (Movies/ was entirely outside its walk):

      * BLANK -- the movie's .nfo is missing or carries no <plot>: Jellyfin never
        identified the film (an oddly-titled special/TV-movie that name matching
        missed, e.g. One Piece "3D2Y"). Plot-centric, the same test as
        episode_is_blank, so a numbered-but-described film is not a false positive.
      * DUP-TMDB -- two DISTINCT movie files carry the SAME <tmdbid>, i.e. one was
        identified as the other: the collection-sibling collision (e.g. Madoka
        Part II filed under Part I's TMDB entry). A wrong-but-populated match has a
        plot, so the blank test cannot see it -- this is the check that can.

    Read-only. Returns (records, dup_ids); each record flags `blank`/`dup_tmdb`."""
    records, by_tmdb = [], {}
    for video in _movie_videos():
        text = library._read_text(video.with_suffix(".nfo"))
        tmdb = library._xml_tag(text, "tmdbid") if text else ""
        records.append({
            "video": str(video),
            "title": video.stem,
            "tmdb_id": tmdb or None,
            "blank": (text is None) or not library._xml_tag(text, "plot"),
            "dup_tmdb": False,
        })
        if tmdb:
            by_tmdb.setdefault(tmdb, []).append(video.stem)
    dup_ids = {k for k, v in by_tmdb.items() if len(v) > 1}
    for r in records:
        r["dup_tmdb"] = bool(r["tmdb_id"] and r["tmdb_id"] in dup_ids)
    return records, dup_ids


def shows_root():
    """The shelf the owner actually has: the mediafs mount when it is available.

    HANDOFF 10.4 reason 1: this walked `config.SHOWS_ROOT` (`~/Media`), where Toriko's
    147 videos are EVICTED to the pool, so the audit found 0 episodes, the worklist was
    empty, and the self-heal was blind to 8 junk titles and 69 blank plots. The mount
    (or its sidecars, which are local) is the complete view.
    """
    mount = config.MEDIAFS_MOUNT / "Shows"
    return mount if mount.is_dir() else config.SHOWS_ROOT


def audit_series_id_collisions():
    """Two DISTINCT show folders carrying the SAME series provider id in their
    tvshow.nfo are the sequel/spin-off MERGE signature: Jellyfin scraped one
    series' id onto another, or a sequel was pinned to its parent (Fairy Tail: 100
    Years Quest onto Fairy Tail). Read-only; returns a list of
    {provider, id, folders} groups. Detection-only — a merge is a placement/seed bug
    to fix by hand (re-pin the sequel's own id, refresh), never auto-rewritten. The
    seed now fills a missing id on ingest (library._seed_tvshow_nfo), so a fresh
    drop should not create this; a hit is a legacy folder or a manual mis-scrape."""
    root = shows_root()
    if not root.exists():
        return []
    by = {"tmdb": {}, "tvdb": {}}
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        text = library._read_text(d / "tvshow.nfo")
        if not text:
            continue
        for prov, tag in (("tmdb", "tmdbid"), ("tvdb", "tvdbid")):
            v = library._xml_tag(text, tag)
            if v:
                by[prov].setdefault(v, []).append(d.name)
    groups = []
    for prov, m in by.items():
        for pid, folders in sorted(m.items()):
            if len(folders) > 1:
                groups.append({"provider": prov, "id": pid, "folders": sorted(folders)})
    return groups


def main():
    ap = argparse.ArgumentParser(description="Audit show + movie library for blank/mis-identified metadata.")
    ap.add_argument("--show", action="append", default=[],
                    help="Limit to show folder(s) whose name contains this substring (repeatable).")
    ap.add_argument("--all", action="store_true",
                    help="List every show/movie, not just those flagged.")
    ap.add_argument("--no-movies", action="store_true",
                    help="Skip the Movies/ audit (show episodes only).")
    ap.add_argument("--json", metavar="PATH",
                    help="Write a repair worklist (flagged shows + movies) to this path.")
    args = ap.parse_args()

    root = shows_root()
    if not root.exists():
        print(f"Shows root not mounted: {config.SHOWS_ROOT}", file=sys.stderr)
        return 2

    show_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    if args.show:
        needles = [n.lower() for n in args.show]
        show_dirs = [d for d in show_dirs if any(n in d.name.lower() for n in needles)]

    # The movie-id index is cheap and enables the special/movie collision check
    # even on a --show run (a KP-style collision is worth catching in that mode
    # too), so build it unconditionally.
    movie_index = _movie_id_index()
    records = [audit_show(d, movie_index) for d in show_dirs]
    with_blanks = [r for r in records if r["blank"]]
    total_blank = sum(r["blank"] for r in records)
    total_eps = sum(r["episodes"] for r in records)

    for r in (records if args.all else with_blanks):
        flag = "OK " if r["blank"] == 0 else "!! "
        print(f"{flag}{r['show']:48} {r['blank']:>4} blank / {r['episodes']:>4} episodes")

    print("-" * 72)
    print(f"{len(with_blanks)} show(s) with blanks; {total_blank} blank episodes of "
          f"{total_eps} across {len(records)} show(s) scanned.")

    # One Pace title mismatches: DETECTION-ONLY (like flagged movies). These are
    # surfaced for a human to eyeball — not auto-repaired, because most are harmless
    # title variants. To fix a confirmed-wrong one, delete its .nfo and run
    # repair_metadata.py (its One Pace arc prompt refills it correctly).
    mismatches = [(r["show"], m) for r in records for m in r.get("title_mismatches", [])]
    if mismatches:
        print(f"\nOne Pace title mismatches to REVIEW ({len(mismatches)}; not auto-repaired):")
        for show, m in mismatches:
            print(f"  ?? S{m['season']:02d}E{m['episode']:02d}  .nfo={m['nfo_title']!r}  "
                  f"mkv={m['container_title']!r}")

    # Season-0 special / Movies-film id collisions: DETECTION-ONLY, like the One
    # Pace mismatches above. A special sharing a film's tmdb/imdb id is a special
    # that Jellyfin mislabelled as a movie we hold separately (the Kim Possible
    # bug). New ingests can't produce this (specials are now locked at apply), so
    # a hit here is a legacy un-owned special: own it or move it out by hand.
    collisions = [(r["show"], c) for r in records
                  for c in r.get("special_movie_collisions", [])]
    if collisions:
        print(f"\nSeason-0 specials mislabelled as a Movies/ film to REVIEW "
              f"({len(collisions)}; not auto-repaired):")
        for show, c in collisions:
            print(f"  ?? {show}  S{c['season']:02d}E{c['episode']:02d}  "
                  f".nfo={c['nfo_title']!r} shares {c['shared_id']} with film {c['movie']!r}")

    # Movies/: scanned by default; skipped when narrowing to specific --show names
    # (a show-focused run) or when --no-movies is passed.
    scan_movies = not args.no_movies and not args.show
    movie_records, _dup_ids = audit_movies() if scan_movies else ([], set())
    flagged_movies = [m for m in movie_records if m["blank"] or m["dup_tmdb"]]
    if scan_movies:
        for m in (movie_records if args.all else flagged_movies):
            tags = []
            if m["blank"]:
                tags.append("no-plot/unidentified")
            if m["dup_tmdb"]:
                tags.append(f"shares-tmdb:{m['tmdb_id']}")
            flag = "OK " if not tags else "!! "
            print(f"{flag}{m['title'][:60]:60} {', '.join(tags) or 'ok'}")
        n_blank = sum(m["blank"] for m in movie_records)
        n_dup = sum(m["dup_tmdb"] for m in movie_records)
        print(f"{len(flagged_movies)} movie(s) flagged of {len(movie_records)} scanned "
              f"({n_blank} unidentified, {n_dup} sharing a TMDB id).")

    # Sequel/spin-off MERGE signature: two show folders sharing one series id. Cheap
    # (one small tvshow.nfo per show), so run it on every audit regardless of --show.
    # Detection-only, like the mismatches/collisions above.
    id_collisions = audit_series_id_collisions()
    if id_collisions:
        print(f"\nShow folders SHARING a series id — sequel/spin-off MERGE signature "
              f"({len(id_collisions)}; not auto-repaired, re-pin the sequel by hand):")
        for g in id_collisions:
            print(f"  ?? {g['provider']} {g['id']}: {g['folders']}")

    if args.json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "media_root": str(config.MEDIA_ROOT),
            "shows": with_blanks,
            "movies": flagged_movies,
            "one_pace_title_mismatches": [dict(show=s, **m) for s, m in mismatches],
            "special_movie_collisions": [dict(show=s, **c) for s, c in collisions],
            "series_id_collisions": id_collisions,
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Worklist written to {args.json} "
              f"({total_blank} episodes, {len(flagged_movies)} movies).")

    return 1 if (total_blank or flagged_movies or id_collisions) else 0


if __name__ == "__main__":
    sys.exit(main())
