#!/usr/bin/env python3
"""Backfill real per-episode metadata for episodes that render blank in Jellyfin.

Some long shows are filed with season/episode coordinates the metadata provider
can't resolve (a custom season split, or absolute numbering past the provider's
coverage / across a multi-entry show). Left un-owned, those episodes show as a
bare "Episode N" with no description, forever. This tool repairs them by OWNING
them: it asks a headless AI run to look up the real episode title and plot
for each blank episode, then writes a locked episode .nfo (byte-identical to what
the owned-ingest path produces) so Jellyfin serves the correct metadata.

The split mirrors the pipeline's core invariant — the model only proposes (returns
title+plot per exact video path; it never chooses placement, moves, or deletes),
and this deterministic harness validates every entry and writes the sidecar.
Nothing with an empty title or plot is ever written, so a blank can never be
locked in place. It is idempotent: an episode that is no longer blank is skipped,
so a re-run only touches what remains.

Usage:
    # Repair everything the audit finds (backs up touched .nfo first):
    python3 scripts/repair_metadata.py --all
    # A single show, or several:
    python3 scripts/repair_metadata.py --show "Naruto Shippuden" --show Gintama
    # Consume a pre-built worklist instead of re-auditing:
    python3 scripts/repair_metadata.py --worklist state/metadata_worklist.json
    # See what would happen without invoking the AI or writing anything:
    python3 scripts/repair_metadata.py --all --dry-run
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # project root: config, library
sys.path.insert(0, str(Path(__file__).resolve().parent))          # scripts/: sibling audit tool

import config      # noqa: E402
import library     # noqa: E402
import epguide     # noqa: E402
import audit_metadata as audit   # noqa: E402  (sibling script)

BATCH_SIZE = 40                       # episodes per AI run (contiguous by abs)
AI_TIMEOUT_SEC = 1200                 # a batch may need many web lookups
ALLOWED_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch,Write"   # no Bash: pure lookup

ONE_PACE_FOLDER = Path(config.ONE_PACE_PREFIX).name          # "One Pace (2013)"


def _is_one_pace(show):
    """One Pace is a fan RECUT of One Piece: no provider carries its arc-based
    episode list, so it is always owned, and — crucially — its episodes must NOT be
    resolved by a global 'absolute One Piece anime episode N' (the recut is not 1:1
    with the anime; that mapping pulls wrong-arc plots). It gets its own prompt."""
    return Path(show["dir"]).name == ONE_PACE_FOLDER


def _named_seasons(show_dir):
    """{season_number: arc_name} from One Pace's tvshow.nfo <namedseason> list, so
    the repair prompt can name which ARC each season is (Wano, Egghead, …)."""
    text = library._read_text(Path(show_dir) / "tvshow.nfo") or ""
    # <namedseason> text is like "35. Wano"; strip the leading "NN. " ordinal so the
    # prompt names the arc cleanly ("Wano", not "35. Wano").
    return {int(n): re.sub(r"^\s*\d+\.\s*", "", name).strip()
            for n, name in re.findall(r'<namedseason number="(\d+)">([^<]*)</namedseason>', text)}


# The mkv-container-title extractor lives in audit_metadata (the authoritative One
# Pace title source, shared by the audit's mismatch detector) — reuse it here.
_container_title = audit.container_title


def _prompt(show, batch, plan_path):
    """Build the repair prompt for one contiguous batch of a show's blanks."""
    lines = []
    for e in batch:
        coord = (f"absolute episode {e['abs']}" if e["abs"] is not None
                 else "a SPECIAL (Season 00)")
        lines.append(
            f'- video: {e["video"]}\n'
            f'    filename: {e["filename"]}  (on-disk S{e["season"]:02d}E{e["episode"]:02d}, '
            f'this is {coord} of the series)'
        )
    listing = "\n".join(lines)
    hints = []
    if show.get("tmdb_id"):
        hints.append(f'series TMDB id {show["tmdb_id"]}')
    if show.get("tvdb_id"):
        hints.append(f'series TheTVDB id {show["tvdb_id"]}')
    hint_line = ("Series ids for lookup: " + ", ".join(hints) + ".") if hints else ""

    return f"""You are a metadata librarian. For a set of already-placed episode video
files of ONE show, look up each episode's REAL title and plot and return them as
strict JSON. You do NOT move, rename, or delete anything, and you do NOT decide
placement — the season/episode are already fixed by each file's name. Your only
job is to resolve accurate `episode_title` and `plot` text for each listed video.

Show: {show['show_title']} ({show.get('year') or 'year unknown'})
Library folder: {show['dir']}
{hint_line}

CRITICAL — resolve by the ABSOLUTE episode number given for each file, not by the
on-disk SxxExx. This show is filed with a numbering scheme the scraper could not
resolve (that is why these are blank), so the on-disk season/episode is NOT a
reliable key. Use the stated "absolute episode N of the series" to find the
correct source episode. For anime, the absolute episode number maps to a specific
titled episode (e.g. "Naruto Shippuden episode 372"); look that up on TMDB (by the
series id above, mapping absolute -> the provider's season/episode), TheTVDB
(absolute order), AniList, or Wikipedia episode lists, and cross-check the title.
For a SPECIAL, identify it from the filename and the show's special list.

Episodes to resolve:
{listing}

Look up authoritative sources with WebSearch/WebFetch. You may Read a sibling
.nfo already in the folder to match tone, but do not copy a wrong episode's text.
Do your best to fill EVERY episode; if after genuine effort you cannot find real
text for one, omit it entirely rather than inventing a plot — a missing entry is
handled safely, a fabricated plot is not.

Write your result as strict JSON to EXACTLY this path (overwrite if present):
{plan_path}

Schema:
{{
  "episodes": [
    {{"video": "<the exact video path from the list above>",
      "episode_title": "<real episode title, non-empty>",
      "plot": "<real 1-3 sentence episode synopsis, non-empty>"}}
  ]
}}

Match each object to a listed file by its exact `video` path. After writing the
file, reply with one sentence naming the source you used and how many you filled.
"""


def _one_pace_prompt(show, batch, plan_path, arcs):
    """Repair prompt for One Pace episodes. Unlike the generic prompt, it resolves
    by ARC (the season) + the episode's own container title, NEVER by a global
    absolute One Piece anime episode number — because One Pace is a recut whose
    episodes do not line up 1:1 with the anime, so an absolute mapping pulls a
    wrong-arc synopsis (the failure that once filed a Wano episode with an Impel
    Down plot). The harness has already extracted each file's embedded title."""
    lines = []
    for e in batch:
        arc = arcs.get(e["season"], f"season {e['season']}")
        ct = e.get("container_title")
        title_hint = (f'    embedded container title: "{ct}"  (authoritative — use this title)'
                      if ct else '    (no embedded title found — look the title up in the One Pace guide)')
        lines.append(
            f'- video: {e["video"]}\n'
            f'    filename: {e["filename"]}  (on-disk S{e["season"]:02d}E{e["episode"]:02d}, '
            f'arc "{arc}", i.e. {arc} episode {e["episode"]})\n' + title_hint)
    listing = "\n".join(lines)

    return f"""You are a metadata librarian repairing episodes of ONE PACE, a fan RECUT
of the One Piece anime that re-edits the story arc by arc to match the manga's
pacing. One Pace is NOT carried per-episode by any metadata provider, which is why
these episodes need hand-written metadata. You do NOT move, rename, or delete
anything and you do NOT choose placement — each file's season/episode is fixed by
its name. Your only job is an accurate `episode_title` and `plot` per listed video.

Show: One Pace (a recut of One Piece)
Library folder: {show['dir']}

CRITICAL RULES:
- Each season IS a One Piece story arc (given per file below). Resolve each episode
  WITHIN ITS ARC by its arc-relative episode number — e.g. "Wano episode 60".
- **NEVER map a One Pace episode to a global "absolute One Piece anime episode N"
  and copy that anime episode's synopsis.** The recut is not 1:1 with the anime;
  that mapping produces a wrong-arc plot. If you catch yourself computing an
  absolute anime episode number, stop — that is the exact bug this repair fixes.
- **Title:** when an "embedded container title" is given for a file, that is the
  authoritative One Pace title — use it verbatim. Otherwise look the title up in
  the One Pace episode guide (onepace.net, the community One Pace guide, or the One
  Pace edits spreadsheet) by arc + episode number.
- **Plot:** find the MANGA CHAPTER range this One Pace episode adapts (the One Pace
  guide lists chapters per episode) and write a short synopsis of THOSE chapters'
  events. Ending the plot with a `Manga Chapter(s): X-Y` line matches the existing
  One Pace .nfo and is encouraged. Cross-check against the episode title so the
  synopsis clearly belongs to the same arc.

Episodes to resolve:
{listing}

Use WebSearch/WebFetch on the One Pace guide and the One Piece manga chapter lists.
Do your best to fill EVERY episode; if after genuine effort you cannot find real
text for one, omit it entirely rather than inventing a plot.

Write your result as strict JSON to EXACTLY this path (overwrite if present):
{plan_path}

Schema:
{{
  "episodes": [
    {{"video": "<the exact video path from the list above>",
      "episode_title": "<real episode title, non-empty>",
      "plot": "<real 1-3 sentence synopsis of the adapted chapters, non-empty>"}}
  ]
}}

Match each object to a listed file by its exact `video` path. After writing the
file, reply with one sentence naming the source you used and how many you filled.
"""


def _run_ai(prompt, plan_path):
    if plan_path.exists():
        plan_path.unlink()
    cmd = [
        *config.AI_BIN, "-p",
        "--output-format", "json",
        "--tools", ALLOWED_TOOLS,
        "--max-turns", "80",
        "--timeout", str(AI_TIMEOUT_SEC - 60),
    ]
    if config.AI_MODEL:
        cmd += ["--model", config.AI_MODEL]
    env = config.ai_env()
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=AI_TIMEOUT_SEC, env=env,
                              cwd=str(config.PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        return None, f"timed out after {AI_TIMEOUT_SEC}s"
    if proc.returncode != 0:
        return None, f"ai run exited {proc.returncode}: {proc.stderr[:300]}"
    if not plan_path.exists():
        return None, "no plan file written"
    try:
        return json.loads(plan_path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return None, f"plan JSON invalid: {exc}"


def _backup_nfo(video_path, backup_root):
    """Copy an existing episode .nfo into the backup tree before we overwrite it."""
    nfo = library.episode_nfo_path(video_path)
    if not nfo.exists():
        return
    try:
        rel = nfo.relative_to(config.MEDIA_ROOT)
    except ValueError:
        rel = Path(nfo.name)
    dst = backup_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(nfo, dst)



def _guide_index(show_title):
    """(season, episode) -> (title, plot) from TVMaze, or {} when it cannot be had.

    NAME-ONLY ROWS ARE ACCEPTED (HANDOFF 10.4). Requiring both name and summary is why
    Toriko -- 146 episode names and ZERO summaries on TVMaze -- produced an empty index
    and repaired nothing. The title is real metadata and is written; a missing summary
    is handled separately by the synopsis fallbacks, which must never overwrite the
    guide's title with a model's guess.
    """
    try:
        eps = epguide.episodes(show_title)
    except Exception:                      # noqa: BLE001 -- a guide miss must never block
        return {}
    idx = {}
    for e in eps or []:
        name = (e.get("name") or "").strip()
        plot = (e.get("summary") or "").strip()
        if name and isinstance(e.get("season"), int) and isinstance(e.get("number"), int):
            idx[(e["season"], e["number"])] = (name, plot)
    return idx


def _abs_guide_index(guide):
    """`abs_index -> (title, plot)` from a guide whose season split is not the shelf's.

    The audit computes each episode's absolute index precisely because anime shelves
    number one continuous run while the guides split seasons (Toriko: files E001-E147
    under `Season 01`, TVMaze/TMDB 49+49+49 across three). Matching on-disk (1, 56)
    against the guide's (2, 7) is what made the deterministic filler a no-op.
    """
    by_season = {}
    for e in guide or ():
        s, n = e.get("season"), e.get("number")
        if isinstance(s, int) and isinstance(n, int) and s > 0:
            by_season.setdefault(s, []).append((n, e))
    out = {}
    offset = 0
    for s in sorted(by_season):
        rows = sorted(by_season[s])
        for i, (_n, e) in enumerate(rows):
            out[offset + i + 1] = ((e.get("name") or "").strip(),
                                   (e.get("summary") or "").strip())
        offset += len(rows)
    return out


_JUNK_TITLE = re.compile(r"\[[^\]]+\]|x26[45]|\b\d{3,4}p\b|\b(?:webrip|web-dl|bluray|"
                         r"aac|hevc)\b", re.IGNORECASE)


def _is_junk_title(title):
    return not title or bool(_JUNK_TITLE.search(title))


def _fill_synopses(show, todo, backup_root):
    """Fill blank `<plot>`s from TMDB episode overviews, keeping the on-disk title.

    The second source in the 10.4 priority order (provider guide -> TMDB/TVDB -> free
    AI). Only rows still blank after the guide are attempted, and a model summary can
    never overwrite a provider one because this path writes the title already on disk.
    Returns `(fixed, residue)`.
    """
    tmdb_id = show.get("tmdb_id")
    if not tmdb_id:
        return 0, todo
    try:
        import tmdbguide
    except Exception:                      # noqa: BLE001
        return 0, todo
    # Fetch every season once and build the absolute-index map, so a shelf numbered as
    # one continuous run (Toriko) can use the provider's split seasons.
    overviews = {}
    offsets = {}
    try:
        shape = tmdbguide.season_shape(tmdb_id) or {}
        off = 0
        for s in sorted(k for k in shape if k and int(k) > 0):
            for (a, b), text in (tmdbguide.episode_overviews(tmdb_id, int(s)) or {}).items():
                overviews[(a, b)] = text
            count = int((shape[s] or {}).get("count") or 0)
            for i in range(1, count + 1):
                offsets[off + i] = (int(s), i)
            off += count
    except Exception:                      # noqa: BLE001
        return 0, todo
    fixed, residue = 0, []
    for e in todo:
        plot = overviews.get((e.get("season"), e.get("episode")))
        if not plot and e.get("abs") in offsets:
            plot = overviews.get(offsets[e["abs"]])
        if not plot:
            residue.append(e)
            continue
        video = Path(e["video"])
        title = e.get("title") or ""
        if not title:
            nfo = library.episode_nfo_path(video)
            try:
                title = library._xml_tag(nfo.read_text("utf-8", "ignore"), "title")
            except OSError:
                title = ""
        try:
            _backup_nfo(video, backup_root)
            library.write_locked_episode_nfo(video, show.get("show", ""), {
                "season": e["season"], "episode": e["episode"],
                "episode_title": title or f"Episode {e['episode']}", "plot": plot})
        except Exception:                  # noqa: BLE001
            residue.append(e)
            continue
        if library.episode_is_blank(video, show.get("show", "")):
            residue.append(e)
        else:
            fixed += 1
    return fixed, residue


def _fill_from_guide(show_title, todo, backup_root):
    """Resolve what TVMaze already knows; return (fixed, residue_for_the_AI).

    §5b item 5: none of the ~45 repairs made on 2026-09-03 needed a language model. TVMaze
    is key-less, cached and already a dependency of `library.py`, so the deterministic
    answer should be tried FIRST and the model handed only what is left.

    §4.146 is why each fill is VERIFIED rather than counted: a past session shipped a tool
    that reported 250 repairs it had not made. An episode is counted only when the sidecar
    it wrote actually stops reading blank; anything else falls through to the AI, which is
    the safe direction -- a double attempt costs a batch, a false "fixed" costs the fault.
    """
    idx = _guide_index(show_title)
    if not idx:
        return 0, todo
    try:
        abs_idx = _abs_guide_index(epguide.episodes(show_title))
    except Exception:                      # noqa: BLE001
        abs_idx = {}
    fixed, residue = 0, []
    for e in todo:
        hit = idx.get((e["season"], e["episode"]))
        if not hit and e.get("abs") in abs_idx:
            # The shelf's season split is not the guide's (one absolute run vs 3
            # seasons). `abs` is the key the audit computed for exactly this.
            hit = abs_idx[e["abs"]]
        if not hit:
            residue.append(e)
            continue
        name, plot = hit
        video = Path(e["video"])
        entry = {"season": e["season"], "episode": e["episode"], "episode_title": name}
        if plot:
            entry["plot"] = plot
        try:
            _backup_nfo(video, backup_root)
            library.write_locked_episode_nfo(video, show_title, entry)
        except Exception:                  # noqa: BLE001
            residue.append(e)
            continue
        # Verify the TITLE landed (not `episode_is_blank`, which is plot-centric):
        # a name-only row is a real partial fill and stays in the residue so the
        # synopsis sources still visit it, without losing the guide's title.
        try:
            text = library._read_text(library.episode_nfo_path(video)) or ""
        except Exception:                  # noqa: BLE001
            text = ""
        if library._xml_tag(text, "title") != name:
            residue.append(e)              # the write did not take -- let a later pass retry
            continue
        if plot and library.episode_is_blank(video, show_title):
            residue.append(e)              # plot write did not take
            continue
        fixed += 1
        if not plot:
            residue.append(dict(e, title=name))
    return fixed, residue


def repair_show(show, backup_root, batch_size, dry_run, min_age_sec, allow_ai=True):
    """Resolve+write metadata for one show's blanks. Returns (fixed, attempted, skipped)."""
    title = show["show_title"]
    is_op = _is_one_pace(show)
    # Re-verify blank NOW (idempotent) and apply the age guard so we never own an
    # episode Jellyfin simply hasn't scraped yet. Keep only main+special episodes
    # that carry a usable season/episode to write a sidecar for. Repair is
    # blank-driven: a One Pace episode with a wrong-but-present title (surfaced by
    # the audit's detection-only mismatch report) is NOT auto-rewritten here — to
    # fix one, delete its .nfo (making it blank) and re-run, and this One-Pace arc
    # prompt fills it correctly.
    now = time.time()
    todo = []
    skipped = 0
    for e in show["blanks"]:
        video = Path(e["video"])
        if e["season"] is None or e["episode"] is None or not video.exists():
            skipped += 1
            continue
        if not library.episode_is_blank(video, title):
            continue                       # already filled by a prior run/scrape
        try:
            if min_age_sec and (now - video.stat().st_ctime) < min_age_sec:
                skipped += 1               # too fresh; give the scraper its chance
                continue
        except OSError:
            pass
        todo.append(e)

    if not todo:
        print(f"  {show['show']}: nothing to repair (all filled or skipped).")
        return 0, 0, skipped

    arcs = _named_seasons(show["dir"]) if is_op else {}
    if is_op:
        # Authoritative title lives in the mkv container; extract it here (repair
        # runs the AI without Bash) and hand it to the prompt.
        for e in todo:
            e["container_title"] = _container_title(e["video"])
        todo.sort(key=lambda e: (e["season"], e["episode"]))
    else:
        todo.sort(key=lambda e: (e["abs"] is None, e["abs"] or 0, e["season"], e["episode"]))
    print(f"  {show['show']}: {len(todo)} to repair"
          + (" [One Pace: arc-based]" if is_op else "")
          + (f" ({skipped} skipped)" if skipped else "")
          + (" [dry-run]" if dry_run else ""))
    if dry_run:
        return 0, len(todo), skipped

    # Deterministic first (§5b item 5): TVMaze is key-less, cached, and already a
    # dependency of library.py. One Pace is excluded -- its episodes are arc cuts that no
    # episode guide describes, which is exactly why it has its own prompt.
    guide_fixed = 0
    if not is_op:
        guide_fixed, todo = _fill_from_guide(title, todo, backup_root)
        if guide_fixed:
            print(f"      guide: filled {guide_fixed} deterministically "
                  f"({len(todo)} left for the synopsis source/AI)")
        # Source 2 (10.4): TMDB episode overviews, for the rows TVMaze named but did
        # not summarize (Toriko: 146 names, 0 summaries).
        if todo:
            syn_fixed, todo = _fill_synopses(show, todo, backup_root)
            if syn_fixed:
                guide_fixed += syn_fixed
                print(f"      TMDB overviews: filled {syn_fixed} synopsis(es) "
                      f"({len(todo)} left for the AI)")
    if not todo:
        return guide_fixed, guide_fixed, skipped
    if not allow_ai:
        # Deterministic sources only (manual mode): the rest stays blank and is named,
        # so a later pass (or the nightly AI filler) can take it without the tool
        # spending provider budget competing with identify.
        print(f"      deterministic only: {len(todo)} episode(s) left queued for a "
              f"synopsis source/AI pass")
        return guide_fixed, len(todo) + guide_fixed, skipped

    plan_path = config.TMP_DIR / f"repair_{abs(hash(show['show'])) & 0xffffffff:x}.json"
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    by_path = {e["video"]: e for e in todo}
    fixed = guide_fixed

    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        lo, hi = batch[0], batch[-1]
        if is_op:
            span = f"S{lo['season']:02d}E{lo['episode']:02d}..S{hi['season']:02d}E{hi['episode']:02d}"
        else:
            span = f"abs {lo['abs']}..{hi['abs']}" if lo["abs"] else "specials"
        print(f"    batch {i//batch_size + 1}: {len(batch)} eps ({span}) ...", flush=True)
        prompt = (_one_pace_prompt(show, batch, plan_path, arcs) if is_op
                  else _prompt(show, batch, plan_path))
        result, err = _run_ai(prompt, plan_path)
        if err:
            print(f"      FAILED: {err}")
            continue
        wrote = 0
        for obj in (result.get("episodes") or []):
            e = by_path.get(str(obj.get("video", "")).strip())
            ep_title = str(obj.get("episode_title") or "").strip()
            plot = str(obj.get("plot") or "").strip()
            if not e or not ep_title or not plot:
                continue                   # unmatched or blank -> never written
            # A MODEL SUMMARY NEVER OVERWRITES A PROVIDER ONE (10.4 source priority):
            # when the guide already gave a real title, keep it -- the run is here for
            # the synopsis, not to re-guess what TVMaze answered exactly.
            try:
                nfo = library.episode_nfo_path(Path(e["video"]))
                existing = library._xml_tag(
                    (library._read_text(nfo) or "") if nfo.exists() else "", "title")
            except Exception:              # noqa: BLE001
                existing = ""
            if existing and not _is_junk_title(existing) and existing != ep_title:
                ep_title = existing
            entry = {"season": e["season"], "episode": e["episode"],
                     "episode_title": ep_title, "plot": plot}
            library.write_locked_episode_nfo(Path(e["video"]), title, entry)
            wrote += 1
        fixed += wrote
        print(f"      wrote {wrote}/{len(batch)} locked .nfo")

    return fixed, len(todo) + guide_fixed, skipped


# --------------------------- movie repair ----------------------------------
#
# Movies are the audit's other blank class (a film Jellyfin never identified, e.g.
# an oddly-titled TV-movie special, or one filed under a collection sibling's TMDB
# id). Unlike an owned episode — which this tool writes a locked .nfo for directly —
# a movie is best repaired by PINNING the right film so Jellyfin scrapes it: the run
# proposes only the correct TMDB /movie/ id, the harness writes an unlocked seed
# .nfo with that id (library._movie_nfo_xml) and asks Jellyfin to full-refresh the
# item, so Jellyfin writes the rich .nfo + poster + backdrop to disk exactly as it
# does for every other movie. The run still cannot move/delete/misfile — it only
# supplies an id, and every id is guarded (year must match the filename, no two
# films may share an id) before anything is pinned.

MOVIE_BATCH_SIZE = 30


def _jf_configured():
    return bool(config.JELLYFIN_URL and config.JELLYFIN_API_KEY)


def _jf_api(path, params=None, method="GET"):
    url = config.JELLYFIN_URL.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("X-Emby-Token", config.JELLYFIN_API_KEY)
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
    return json.loads(body) if (body and method == "GET") else None


def _jf_movie_index():
    """Map absolute movie file path -> Jellyfin ItemId (the Movies library)."""
    data = _jf_api("/Items", {"Recursive": "true", "IncludeItemTypes": "Movie",
                              "Fields": "Path", "Limit": "100000"}) or {}
    return {it["Path"]: it["Id"] for it in data.get("Items", []) if it.get("Path")}


def _jf_refresh_item(item_id):
    """Force Jellyfin to re-scrape one item from its (now pinned) provider id and
    write the metadata + images back to disk."""
    _jf_api(f"/Items/{item_id}/Refresh", {
        "metadataRefreshMode": "FullRefresh", "imageRefreshMode": "FullRefresh",
        "replaceAllMetadata": "true", "replaceAllImages": "true"}, method="POST")


def _movie_is_blank(video):
    """A movie is blank when its .nfo is missing or carries no <plot> — the same
    plot-centric test the audit uses, so this stays idempotent with it."""
    text = library._read_text(video.with_suffix(".nfo"))
    return (text is None) or not library._xml_tag(text, "plot")


def _movie_prompt(batch, plan_path):
    listing = "\n".join(f'- video: {m["video"]}\n    filename: {m["filename"]}'
                        for m in batch)
    return f"""You are a metadata librarian. For each already-placed MOVIE video file
below, identify the correct film on The Movie Database (TMDB) and return its TMDB
MOVIE id. You do NOT move, rename, or delete anything — the file is already placed;
your ONLY job is to resolve the correct TMDB /movie/ id (and the IMDb id if you find
it) so the library can pin it and Jellyfin can scrape the rest.

Each filename is formatted `Title (Year)`. Many are anime films or TV-movie
specials whose on-disk title does not exactly match TMDB's title (e.g. a One Piece
"Episode of ..." TV special, or a differently-worded franchise film) — which is
exactly why automatic name matching failed and they are blank.

RULES:
- Return the id of the entry that exists as a standalone TMDB **/movie/** page
  (https://www.themoviedb.org/movie/<id>). Confirm it by matching BOTH the title
  and the release year (allow +/- 1 year for region differences).
- Give each DISTINCT film its own DISTINCT id. NEVER return the same TMDB id for two
  different files — that is the collection-sibling collision that labels one film as
  another.
- If after genuine effort you cannot find a confident /movie/ match for a file, OMIT
  it entirely rather than guessing — a missing entry is handled safely; a wrong id
  writes a wrong plot.

Movies to identify:
{listing}

Use WebSearch/WebFetch on themoviedb.org to confirm each id. Write strict JSON to
EXACTLY this path (overwrite if present):
{plan_path}

Schema:
{{
  "movies": [
    {{"video": "<exact video path from the list>",
      "tmdb_id": <integer TMDB movie id>,
      "imdb_id": "<tt... or empty>",
      "title": "<the film's canonical title>",
      "year": <release year integer>}}
  ]
}}

Match each object to a listed file by its exact `video` path. After writing the
file, reply with one sentence stating how many you identified.
"""


def repair_movies(movies, backup_root, dry_run, min_age_sec):
    """Identify + pin the correct TMDB id for flagged movies, then let Jellyfin
    scrape rich metadata + artwork to disk. Returns (fixed, attempted, skipped)."""
    if not _jf_configured():
        print("  movies: JELLYFIN_URL/API_KEY not set — skipping movie repair "
              "(it needs Jellyfin to scrape TMDB). Non-fatal.")
        return 0, 0, 0

    now = time.time()
    todo, skipped = [], 0
    for m in movies:
        video = Path(m["video"])
        if not video.exists():
            skipped += 1
            continue
        # Repair blanks (unidentified) and dup_tmdb collisions (wrong id -> wrong
        # film). A blank that has filled since the audit ran is skipped (idempotent).
        if not (m.get("blank") or m.get("dup_tmdb")):
            continue
        if m.get("blank") and not m.get("dup_tmdb") and not _movie_is_blank(video):
            continue
        try:
            if min_age_sec and (now - video.stat().st_ctime) < min_age_sec:
                skipped += 1                # too fresh; give the scraper its chance
                continue
        except OSError:
            pass
        m["filename"] = video.name
        m["_fileyear"] = library._parse_movie_title_year(video.stem)[1]
        todo.append(m)

    if not todo:
        print("  movies: nothing to repair (all filled or skipped).")
        return 0, 0, skipped

    print(f"  movies: {len(todo)} to identify"
          + (f" ({skipped} skipped)" if skipped else "")
          + (" [dry-run]" if dry_run else ""))
    if dry_run:
        for m in todo:
            print(f"      would identify+pin: {m['filename']}")
        return 0, len(todo), skipped

    # Back up any existing (wrong/sparse) movie .nfo before overwriting it.
    for m in todo:
        nfo = Path(m["video"]).with_suffix(".nfo")
        if nfo.exists():
            try:
                rel = nfo.relative_to(config.MEDIA_ROOT)
            except ValueError:
                rel = Path(nfo.name)
            dst = backup_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(nfo, dst)

    plan_path = config.TMP_DIR / "repair_movies.json"
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    by_path = {m["video"]: m for m in todo}

    # 1) Ask the AI for the correct TMDB ids (batched), guarding every answer.
    resolved = {}            # video -> (tmdb_id, imdb_id, title, year)
    seen_ids = {}            # tmdb_id -> video, the cross-file collision guard
    for i in range(0, len(todo), MOVIE_BATCH_SIZE):
        batch = todo[i:i + MOVIE_BATCH_SIZE]
        print(f"    identify batch {i // MOVIE_BATCH_SIZE + 1}: {len(batch)} movie(s) ...", flush=True)
        result, err = _run_ai(_movie_prompt(batch, plan_path), plan_path)
        if err:
            print(f"      FAILED: {err}")
            continue
        for obj in (result.get("movies") or []):
            m = by_path.get(str(obj.get("video", "")).strip())
            try:
                tmdb = int(obj.get("tmdb_id"))
            except (TypeError, ValueError):
                tmdb = 0
            if not m or tmdb <= 0:
                continue
            year, fy = obj.get("year"), m.get("_fileyear")
            if fy and isinstance(year, int) and abs(year - fy) > 1:
                print(f"      SKIP {m['filename']}: TMDB year {year} != filename {fy} (likely wrong id)")
                continue
            if tmdb in seen_ids and seen_ids[tmdb] != m["video"]:
                print(f"      SKIP {m['filename']}: TMDB id {tmdb} already claimed by another file (collision)")
                continue
            seen_ids[tmdb] = m["video"]
            imdb = str(obj.get("imdb_id") or "").strip() or None
            title = (str(obj.get("title") or "").strip()
                     or library._parse_movie_title_year(Path(m["video"]).stem)[0])
            resolved[m["video"]] = (tmdb, imdb, title, year or fy)

    if not resolved:
        print("      no TMDB ids resolved; nothing pinned.")
        return 0, len(todo), skipped

    # 2) Pin each id in a seed .nfo and tell Jellyfin to full-refresh that item.
    idx = _jf_movie_index()
    for video, (tmdb, imdb, title, year) in resolved.items():
        Path(video).with_suffix(".nfo").write_text(
            library._movie_nfo_xml(title, year, tmdb, imdb), encoding="utf-8")
        item_id = idx.get(video)
        if not item_id:
            print(f"      pinned nfo; item not in Jellyfin DB yet (next scan picks it up): {Path(video).name}")
            continue
        try:
            _jf_refresh_item(item_id)
        except Exception as exc:            # noqa: BLE001 — non-fatal per movie
            print(f"      Jellyfin refresh failed for {Path(video).name}: {exc}")

    # 3) Verify: give Jellyfin a moment, then count movies that gained a <plot>.
    fixed, pending = 0, set(resolved)
    deadline = time.time() + 90
    while pending and time.time() < deadline:
        time.sleep(10)
        for video in list(pending):
            if not _movie_is_blank(Path(video)):
                fixed += 1
                pending.discard(video)
    for video in resolved:
        print(f"      {'OK' if video not in pending else 'pinned (scrape pending)'}: {Path(video).name}")
    return fixed, len(todo), skipped


def _load_movies(args):
    """Flagged movies to repair. A --show run is show-focused, so it skips movies
    (matching audit_metadata, which does not scan Movies/ under --show)."""
    if getattr(args, "no_movies", False) or args.show:
        return []
    if args.worklist:
        data = json.loads(Path(args.worklist).read_text("utf-8"))
        return data.get("movies", [])
    records, _dup = audit.audit_movies()      # --all
    return [m for m in records if m.get("blank") or m.get("dup_tmdb")]


def _load_worklist(args):
    if args.worklist:
        data = json.loads(Path(args.worklist).read_text("utf-8"))
        shows = data["shows"]
    else:
        show_dirs = sorted(p for p in audit.shows_root().iterdir() if p.is_dir())
        if args.show:
            needles = [n.lower() for n in args.show]
            show_dirs = [d for d in show_dirs if any(n in d.name.lower() for n in needles)]
        shows = [r for r in (audit.audit_show(d) for d in show_dirs) if r["blank"]]
    if args.show and args.worklist:
        needles = [n.lower() for n in args.show]
        shows = [s for s in shows if any(n in s["show"].lower() for n in needles)]
    return shows


def main():
    ap = argparse.ArgumentParser(description="Backfill locked metadata for blank episodes.")
    ap.add_argument("--show", action="append", default=[],
                    help="Limit to show folder(s) whose name contains this substring (repeatable).")
    ap.add_argument("--all", action="store_true", help="Repair every show with blanks.")
    ap.add_argument("--worklist", help="Use a worklist JSON from audit_metadata.py instead of re-auditing.")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--min-age-hours", type=float, default=0.0,
                    help="Skip episodes placed less than this many hours ago (give the "
                         "scraper its chance). Use 0 for a manual backfill of known-stale "
                         "blanks; use e.g. 48 for the scheduled safety net.")
    ap.add_argument("--no-movies", action="store_true",
                    help="Repair episodes only; skip the Movies/ pass.")
    ap.add_argument("--dry-run", action="store_true", help="Report only; do not call the AI or write.")
    ap.add_argument("--no-ai", action="store_true",
                    help="Deterministic sources only (TVMaze + TMDB overviews); leave the "
                         "rest queued instead of spending provider budget on the AI.")
    args = ap.parse_args()

    if not (args.all or args.show or args.worklist):
        ap.error("choose --all, --show NAME, or --worklist PATH")
    if not audit.shows_root().exists():
        print(f"Shows root not mounted: {config.SHOWS_ROOT}", file=sys.stderr)
        return 2

    shows = _load_worklist(args)
    movies = _load_movies(args)
    if not shows and not movies:
        print("No shows or movies with blank metadata. Nothing to do.")
        return 0

    total_blank = sum(s["blank"] for s in shows)
    print(f"Repairing {len(shows)} show(s), {total_blank} blank episode(s)"
          + (f"; {len(movies)} flagged movie(s)." if movies else "."))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_root = config.STATE_DIR / f"nfo-backup-{ts}"
    if not args.dry_run:
        backup_root.mkdir(parents=True, exist_ok=True)
        for s in shows:
            for e in s["blanks"]:
                _backup_nfo(Path(e["video"]), backup_root)
        print(f"Backed up existing .nfo to {backup_root}")

    tot_fixed = tot_attempted = tot_skipped = 0
    for s in shows:
        f, a, sk = repair_show(s, backup_root, args.batch_size, args.dry_run,
                               int(args.min_age_hours * 3600),
                               allow_ai=not args.no_ai)
        tot_fixed += f
        tot_attempted += a
        tot_skipped += sk

    # Movies: identify + pin + Jellyfin-refresh (its own backup of touched .nfo).
    if movies and args.no_ai:
        print("  movies: skipped under --no-ai (movie repair is an AI identify).")
        movies = []
    if movies:
        mf, ma, msk = repair_movies(movies, backup_root, args.dry_run,
                                    int(args.min_age_hours * 3600))
        print(f"  movies: pinned/filled {mf}/{ma} ({msk} skipped).")

    print("-" * 72)
    print(f"Done. Filled {tot_fixed}/{tot_attempted} episode(s) attempted "
          f"({tot_skipped} skipped). Re-run to retry any that failed lookup.")
    print("Trigger a Jellyfin library scan (the safe mode respects <lockdata>) to "
          "surface the new metadata. Do NOT use 'Replace all metadata'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
