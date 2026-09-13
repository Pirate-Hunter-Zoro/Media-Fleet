#!/usr/bin/env python3
"""playlist_curator.py -- decide, per show, whether it EARNS a curated playlist.

The old model hard-coded WHICH shows get a "watchable" playlist: a fixed
`CURATION_TASKS` list in playlist_autobuild.py. Add a filler-heavy show to the
library and it silently never got a playlist until someone edited that list.

This daemon removes the hard-coding. It walks EVERY show in the library and, for
each one not yet decided, asks a headless AI run the single judgment question the
whole curation philosophy already turns on:

    Does this show carry enough DEAD TIME (filler arcs, padded canon, long
    stall/recap stretches) that a lean "watchable" cut is worth having --
    or is it already tight enough to just watch straight through?

Toriko, One Piece, Bleach, the Dragon Balls -> yes. Hunter x Hunter, Death Note,
Monster, Cowboy Bebop, Frieren -> no. The verdict is the SAME description-driven
lens (playlist_curation.CUT_PHILOSOPHY) used to keep/cut individual episodes; it
is just applied one level up, to the whole show.

Flow, budget-gated exactly like playlist_autobuild (one AI research run at a
time, waits out the API spend limit):

  1. SEED once from the legacy hand-tuned `CURATION_TASKS` so the existing
     combined-franchise playlists (Naruto+Shippuden+Boruto, the five Dragon Balls,
     ...) are preserved verbatim and their member shows count as already-decided.
  2. DECIDE: one AI run judges ALL still-undecided shows at once (a light
     per-show verdict + franchise grouping), persisting each decision. Cheap and
     re-runnable; clear the state file to re-judge the whole library after a
     philosophy change.
  3. BUILD: for each YES decision without a playlist yet, build one per cycle via
     playlist_autobuild's validated builder (the model proposes a manifest to a temp
     path; only a manifest that resolves to real files is promoted and pushed to
     Jellyfin -- a bad run can never clobber a good playlist).
  4. Keep the auto-extend set (playlist_watch) in sync: every ONGOING show that
     got a playlist is written to state/playlist_auto_shows.json so new weekly
     episodes are judged and appended automatically.

FILMS TOO. The same daemon asks a SECOND, different question of `Movies/` (§ the
movie section below): not "what should be cut?" -- a film has no filler episodes --
but "do these films form a saga whose WATCH ORDER isn't what the folder gives you?"
Movies/ is a flat, alphabetically-listed folder, so Endgame sorts above Infinity
War, Star Wars scatters across three naming eras, and a prequel has nowhere sensible
to sit. Where that's genuinely a problem the daemon builds an ORDERED playlist
(additive -- nothing is cut); where it isn't, it builds nothing. Because the ledger
is keyed by what is on disk, a newly-acquired film is automatically undecided, so the
next cycle slots it into the collection it belongs to at the right watch position.

Nothing here ever touches a library media file.

    python3 scripts/playlist_curator.py --once          # one decide/build pass
    python3 scripts/playlist_curator.py --decide-only    # judge, don't build
    python3 scripts/playlist_curator.py --status         # print the decision ledger
    python3 scripts/playlist_curator.py --rejudge        # wipe decisions, start over
    python3 scripts/playlist_curator.py --movies-only    # judge/build film collections only
    python3 scripts/playlist_curator.py --shows-only     # judge/build shows only
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))    # for playlist_autobuild

import config              # noqa: E402
import playlist            # noqa: E402
import playlist_curation   # noqa: E402
import playlist_autobuild as autobuild   # noqa: E402  (reuse its builder + budget probe)

DECISIONS_FILE = config.STATE_DIR / "playlist_decisions.json"
AUTO_SHOWS_FILE = config.STATE_DIR / "playlist_auto_shows.json"
DONE_FILE = autobuild.DONE_FILE                    # shared build-done marker


def _log(msg: str) -> None:
    print(f"[playlist_curator] {msg}", flush=True)


# --- decision ledger ---------------------------------------------------------
#
# decisions[show_rel] = {
#   "needs": bool,               # does this show warrant a curated playlist?
#   "reason": str,
#   "role": "flagship"|"member"|"standalone"|"none",
#   "slug": str,                 # the task/playlist this show belongs to (if needs)
#   "name": str,                 # playlist display name (flagship only)
#   "shows": [rel, ...],         # all shows in this task (flagship only)
#   "band": str,                 # keep-rate band, e.g. "45-55%"
#   "brief": str,                # curation brief for the builder
#   "ongoing": bool,             # airing weekly -> auto-extend
#   "decided_ts": float,
# }

def _load(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:                                             # noqa: BLE001
            return default
    return default


def _save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _all_show_rels() -> list[str]:
    root = config.SHOWS_ROOT
    if not root.is_dir():
        return []
    return sorted(f"Shows/{p.name}" for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


def seed_from_legacy(decisions: dict) -> int:
    """Import the hand-tuned CURATION_TASKS as decisions so existing combined
    playlists are preserved and their shows count as already-decided."""
    added = 0
    done = autobuild._load_done()
    for task in autobuild.CURATION_TASKS:
        shows = [s.rstrip("/") for s in task["shows"]]
        flagship = shows[0]
        if flagship in decisions:
            continue
        band = playlist_curation.band_for(task["shows"])
        decisions[flagship] = {
            "needs": True, "reason": "legacy hand-tuned curation task",
            "role": "flagship", "slug": task["slug"], "name": task["name"],
            "shows": shows, "band": band, "brief": task["brief"],
            "ongoing": task["slug"] in ("one-piece-watchable",),
            "built": task["slug"] in done, "decided_ts": 0,
        }
        for member in shows[1:]:
            decisions.setdefault(member, {
                "needs": True, "reason": "member of a combined franchise playlist",
                "role": "member", "slug": task["slug"], "decided_ts": 0,
            })
        added += 1
    return added


# --- DECIDE: one headless AI run judges every undecided show ----------------

_DECIDE_PROMPT = """\
You curate a self-hosted anime/TV library. For EACH show below, decide ONE thing:

  Does this show carry enough DEAD TIME -- filler arcs, heavily padded canon, long
  stall / recap / reaction stretches -- that a lean "watchable" cut (only the
  episodes where something HAPPENS, in order) is genuinely worth having?

  YES for the notoriously padded long-runners: One Piece, Naruto, Bleach, the
  Dragon Balls, Fairy Tail, Gintama, Sailor Moon, Toriko, Boruto, Black Clover,
  Hunter x Hunter (1999), Inuyasha, Nisekoi, Reborn, Sword Art Online arcs, etc.
  NO for the tight, filler-free shows you'd just watch straight through: Hunter x
  Hunter (2011), Death Note, Monster, Cowboy Bebop, Fullmetal Alchemist Brotherhood,
  Steins;Gate, Frieren, Vinland Saga, Mob Psycho, Chainsaw Man, most 12-26 episode
  seasonal shows, and most Western cartoons with self-contained ~11-min episodes
  UNLESS they are long and uneven (Adventure Time / Steven Universe / Regular Show
  are YES; a tight 26-ep show is NO).

Judge from what you KNOW about each show's filler/pacing reputation. When unsure,
lean NO -- a needless playlist is worse than none.

Also GROUP franchises: if several listed shows are one continuous story that should
be ONE combined playlist in watch order (e.g. a base series + its sequels/films),
put them in one entry with the earliest as the flagship. Otherwise each YES show is
its own standalone entry.

SHOWS TO JUDGE (library-relative folders):
{show_list}

Write ONLY a JSON array to this EXACT path and nothing else: {out}
Each element:
{{
  "shows": ["Shows/<flagship>", "Shows/<member>", ...],   // one or more, watch order
  "needs": true/false,
  "reason": "<one concise line: why it does or doesn't need a cut>",
  "name": "<Flagship Title> - Watchable",                 // only if needs
  "slug": "<kebab-case-flagship>-watchable",              // only if needs
  "band": "45-55%",            // rough keep-rate; padded shows lower, lore-forward higher
  "ongoing": true/false,       // still airing new episodes weekly?
  "brief": "<2-4 sentences telling a curator what to keep vs cut for THIS show: the
            filler arcs / padded canon to drop, the intros/beats to keep, and any
            films in Movies/ to slot in>"
}}
Every show in the input must appear in exactly one entry.
"""


def decide(decisions: dict, dry_run: bool) -> int:
    """Judge all still-undecided shows in one AI run. Returns count decided."""
    undecided = [rel for rel in _all_show_rels() if rel not in decisions]
    if not undecided:
        _log("every show already decided; nothing to judge")
        return 0
    _log(f"{len(undecided)} undecided show(s) to judge")
    if dry_run:
        _log("DRY-RUN: would ask the AI to judge them")
        return 0

    out_path = config.TMP_DIR / "playlist_decide.json"
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    prompt = _DECIDE_PROMPT.format(show_list="\n".join(f"  {r}" for r in undecided),
                                   out=str(out_path))
    cmd = [*config.AI_BIN, "-p", "--output-format", "json",
           "--tools", "Read,Glob,Grep,ListDir,WebSearch,WebFetch,Write",
           "--max-turns", "40", "--timeout", "1140"]
    if config.AI_MODEL:
        cmd += ["--model", config.AI_MODEL]
    try:
        subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=1200, env=autobuild._ai_env(),
                       cwd=str(config.PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        _log("decide run timed out; retry next cycle")
        return 0
    if not out_path.exists():
        _log("decide run produced no verdicts; retry next cycle")
        return 0
    try:
        verdicts = json.loads(out_path.read_text())
    except json.JSONDecodeError as e:
        _log(f"decide verdicts were not valid JSON ({e}); retry next cycle")
        return 0

    decided = 0
    known = set(undecided)
    for v in verdicts if isinstance(verdicts, list) else []:
        shows = [s.rstrip("/") for s in (v.get("shows") or []) if s.rstrip("/") in known]
        if not shows:
            continue
        needs = bool(v.get("needs"))
        flagship = shows[0]
        if not needs:
            for rel in shows:
                decisions[rel] = {"needs": False, "reason": str(v.get("reason", "")).strip(),
                                  "role": "none", "decided_ts": time.time()}
                decided += 1
            continue
        decisions[flagship] = {
            "needs": True, "reason": str(v.get("reason", "")).strip(),
            "role": "flagship" if len(shows) > 1 else "standalone",
            "slug": v.get("slug") or (flagship.split("/", 1)[-1].lower()
                                      .replace(" ", "-").replace("(", "").replace(")", "") + "-watchable"),
            "name": v.get("name") or f"{Path(flagship).name.split(' (')[0]} - Watchable",
            "shows": shows, "band": v.get("band") or "50-60%",
            "brief": str(v.get("brief", "")).strip(),
            "ongoing": bool(v.get("ongoing")), "built": False, "decided_ts": time.time(),
        }
        decided += 1
        for member in shows[1:]:
            decisions[member] = {"needs": True, "role": "member",
                                 "slug": decisions[flagship]["slug"],
                                 "reason": "member of a combined franchise playlist",
                                 "decided_ts": time.time()}
            decided += 1
    _log(f"recorded {decided} decision(s) "
         f"({sum(1 for d in decisions.values() if d.get('role') in ('flagship','standalone') and d.get('needs'))} playlists warranted)")
    return decided


# --- MOVIES: collections in Movies/ that earn an ordered playlist ------------
#
# The show side of this daemon asks "does this show carry enough DEAD TIME that a
# lean cut is worth having?" -- a SUBTRACTIVE question. Movies are the opposite
# problem, so they get their own question rather than being forced through the
# filler lens (a film has no filler episodes to cut):
#
#     Do these films form a saga/franchise/thematic set whose WATCH ORDER is not
#     obvious from the folder, so that an ordered playlist is genuinely useful?
#
# Movies/ is a FLAT folder of `Title (Year).mkv`, so Jellyfin shows it alphabetically:
# "Avengers: Endgame" sorts above "Avengers: Infinity War", the Star Wars films
# scatter across three naming eras, and a prequel/interquel (Rogue One, Fantastic
# Beasts, the Fate/Zero films) has no place in an A-Z list at all. An ordered
# playlist is the fix, and it is ADDITIVE -- nothing is cut, the films are simply
# put in the order you'd actually watch them.
#
# A collection is keyed in the same decision ledger as the shows, under
# `Movies/<flagship stem>`, with `kind: "movies"`. Because a film is only judged
# once and the ledger is keyed by what is on disk, a NEWLY-ACQUIRED film is
# automatically undecided -- so the next cycle judges it and, if it belongs to a
# collection that already exists, slots it into that manifest at its watch
# position (§ _assign_to_existing). That is the movie analogue of the shows'
# auto-extend, with no separate prowl needed.

def _all_movie_stems() -> list[str]:
    """Every film the library holds, as `Movies/<filename stem>` keys.

    Enumerated from the mediafs MOUNT (`playlist.MOVIES_DIR`), not the SSD lower:
    a film's payload is evicted from the lower once it is in the MEGA pool, so the
    lower shows a handful of films out of hundreds while the mount shows them all.
    Same reason `playlist._movie_index` reads the mount -- and it has to be the SAME
    set, or the curator would propose films the builder then can't resolve."""
    root = playlist.MOVIES_DIR
    if not root.is_dir():
        return []
    return sorted(f"Movies/{p.stem}" for p in root.iterdir()
                  if p.suffix.lower() in config.VIDEO_EXTENSIONS
                  and not p.name.startswith("."))


def _movie_collections(decisions: dict) -> list[tuple[str, dict]]:
    """The (key, decision) pairs that are movie-collection flagships."""
    return [(k, d) for k, d in decisions.items()
            if d.get("kind") == "movies" and d.get("needs")
            and d.get("role") in ("flagship", "standalone")]


_DECIDE_MOVIES_PROMPT = """\
You curate a self-hosted film library. `Movies/` is a FLAT folder, so the player
lists these films ALPHABETICALLY. Your job is to find the sets of films that
deserve an ORDERED playlist, and to leave everything else alone.

For each film below, decide which (if any) COLLECTION it belongs to. A collection
earns a playlist when BOTH are true:

  1. Two or more of the films on disk are one connected body of work -- a saga,
     franchise, trilogy, or a film series tied to a show already in the library.
  2. The right viewing order is NOT what alphabetical order gives you. Release
     order differs from chronological order, or a prequel/interquel/side-story
     sits in the middle, or the titles simply don't sort into sequence.

YES examples: the MCU (a long saga in a deliberate order), Star Wars (three naming
eras plus Rogue One/Solo slotting mid-saga), The Lord of the Rings + The Hobbit
(chronological order inverts release order), a numbered-but-badly-titled trilogy,
an anime film series that must interleave with its TV show, a director's connected
trilogy.

NO examples: a single standalone film (ALWAYS no -- one film is not a playlist);
two films that merely share a studio, genre, or actor; a set already in obvious
order where the alphabetical list IS the watch order; a "franchise" of one film
plus an unrelated remake. When unsure, answer NO -- a needless playlist is worse
than none.

Do NOT cut anything. This is an ORDERING task, not a filler-trimming one: every
film in a collection belongs in its playlist unless it is genuinely not part of
the work (an unrelated remake, a making-of).

{existing_block}
FILMS ON DISK (library-relative, `Movies/<filename stem>`):
{movie_list}

Write ONLY a JSON array to this EXACT path and nothing else: {out}
Each element is EITHER a new collection, a NO verdict, or an assignment to an
existing collection:

{{
  "movies": ["Movies/<flagship stem>", "Movies/<next stem>", ...],  // WATCH ORDER
  "needs": true/false,
  "reason": "<one concise line: why this set does or doesn't need an ordered list>",
  "name": "<Collection Name> - In Order",         // only if needs
  "slug": "<kebab-case-collection>-in-order",     // only if needs
  "ongoing": true/false,        // is this franchise still getting new films?
  "existing_slug": "",          // set ONLY when these films join a collection that
                                // already exists (listed above); then give
                                // "after_movie" per film below instead of a new slug
  "after_movie": "Movies/<stem this film should follow>",   // with existing_slug
  "brief": "<2-4 sentences telling a curator the ORDER to use for THIS set and why
            (chronological vs release), where any prequel/side-story slots in, and
            any film on disk to leave OUT>"
}}

Rules:
  * Every film in the input must appear in exactly ONE element.
  * A film with no collection gets its own element with "needs": false.
  * Order the "movies" array in the order it should be WATCHED, not alphabetically.
  * Use ONLY the exact `Movies/<stem>` strings from the list above -- never invent,
    reword, or re-punctuate one. A stem that isn't on disk is dropped.
"""


def _existing_collections_block(decisions: dict) -> str:
    """Tell the decide run which collections already exist, so a newly-acquired film
    is slotted into one instead of spawning a near-duplicate playlist."""
    cols = _movie_collections(decisions)
    if not cols:
        return ""
    lines = ["EXISTING COLLECTIONS -- if a film below belongs to one of these, return it",
             "with that collection's `existing_slug` and an `after_movie`, NOT a new slug:"]
    for _k, d in sorted(cols, key=lambda t: t[1].get("slug", "")):
        members = ", ".join(m.split("/", 1)[-1] for m in (d.get("movies") or [])[:12])
        more = "" if len(d.get("movies") or []) <= 12 else f" (+{len(d['movies'])-12} more)"
        lines.append(f"  {d.get('slug')}  \"{d.get('name')}\"  [{members}{more}]")
    return "\n".join(lines) + "\n\n"


def _assign_to_existing(v: dict, movies: list[str], decisions: dict) -> int:
    """Slot newly-acquired films into a collection that already has a playlist, at the
    watch position the verdict names. Returns the number of films recorded."""
    slug = str(v.get("existing_slug") or "").strip()
    target = next((d for _k, d in _movie_collections(decisions) if d.get("slug") == slug), None)
    if target is None:
        return 0
    name = target.get("name", slug)
    after = str(v.get("after_movie") or "").split("/", 1)[-1]
    added = 0
    for rel in movies:
        stem = rel.split("/", 1)[-1]
        if playlist.insert_movie_item(slug, name, stem, after, str(v.get("reason", "")).strip()):
            added += 1
        after = stem              # chain, so a multi-film batch keeps its own order
        target.setdefault("movies", []).append(rel)
        decisions[rel] = {"needs": True, "role": "member", "kind": "movies",
                          "slug": slug, "reason": "joined an existing film collection",
                          "decided_ts": time.time()}
    if added:
        _log(f"{slug}: slotted {added} newly-acquired film(s) into the existing collection")
        try:
            playlist.build_playlist(playlist.manifest_path_for(slug), playlist.Jellyfin(),
                                    dry_run=False)
        except Exception as e:                                        # noqa: BLE001
            _log(f"{slug}: Jellyfin re-push failed ({e}); manifest saved, retry next cycle")
    return len(movies)


def decide_movies(decisions: dict, dry_run: bool) -> int:
    """Judge all still-undecided films in one AI run. Returns count decided."""
    undecided = [rel for rel in _all_movie_stems() if rel not in decisions]
    if not undecided:
        _log("every film already decided; nothing to judge")
        return 0
    _log(f"{len(undecided)} undecided film(s) to judge")
    if dry_run:
        _log("DRY-RUN: would ask the AI to judge them")
        return 0

    out_path = config.TMP_DIR / "playlist_decide_movies.json"
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    prompt = _DECIDE_MOVIES_PROMPT.format(
        movie_list="\n".join(f"  {r}" for r in undecided),
        existing_block=_existing_collections_block(decisions),
        out=str(out_path))
    cmd = [*config.AI_BIN, "-p", "--output-format", "json",
           "--tools", "Read,Glob,Grep,ListDir,WebSearch,WebFetch,Write",
           "--max-turns", "40", "--timeout", "1740"]
    if config.AI_MODEL:
        cmd += ["--model", config.AI_MODEL]
    try:
        subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=1800, env=autobuild._ai_env(),
                       cwd=str(config.PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        _log("movie decide run timed out; retry next cycle")
        return 0
    if not out_path.exists():
        _log("movie decide run produced no verdicts; retry next cycle")
        return 0
    try:
        verdicts = json.loads(out_path.read_text())
    except json.JSONDecodeError as e:
        _log(f"movie decide verdicts were not valid JSON ({e}); retry next cycle")
        return 0

    decided = 0
    known = set(undecided)
    for v in verdicts if isinstance(verdicts, list) else []:
        # Only films that are really on disk AND still undecided; a hallucinated or
        # re-punctuated stem is dropped rather than written into the ledger.
        movies = [m.rstrip("/") for m in (v.get("movies") or []) if m.rstrip("/") in known]
        if not movies:
            continue
        if not bool(v.get("needs")):
            for rel in movies:
                decisions[rel] = {"needs": False, "kind": "movies", "role": "none",
                                  "reason": str(v.get("reason", "")).strip(),
                                  "decided_ts": time.time()}
                decided += 1
            continue
        if str(v.get("existing_slug") or "").strip():
            decided += _assign_to_existing(v, movies, decisions)
            continue
        # A lone film is never a playlist, whatever the verdict claims.
        if len(movies) < 2:
            for rel in movies:
                decisions[rel] = {"needs": False, "kind": "movies", "role": "none",
                                  "reason": "single film — a collection needs two or more",
                                  "decided_ts": time.time()}
                decided += 1
            continue
        flagship = movies[0]
        base = Path(flagship).name.split(" (")[0]
        slug = v.get("slug") or (base.lower().replace(" ", "-")
                                 .replace("(", "").replace(")", "") + "-in-order")
        decisions[flagship] = {
            "needs": True, "kind": "movies", "role": "flagship",
            "reason": str(v.get("reason", "")).strip(),
            "slug": slug, "name": v.get("name") or f"{base} - In Order",
            "movies": movies, "brief": str(v.get("brief", "")).strip(),
            "ongoing": bool(v.get("ongoing")), "built": False, "decided_ts": time.time(),
        }
        decided += 1
        for member in movies[1:]:
            decisions[member] = {"needs": True, "role": "member", "kind": "movies",
                                 "slug": slug,
                                 "reason": "member of a film collection playlist",
                                 "decided_ts": time.time()}
            decided += 1
    _log(f"recorded {decided} film decision(s) "
         f"({len(_movie_collections(decisions))} film collection(s) warranted)")
    return decided


_MOVIE_RULES = (
    "This is an ORDERING task, not a trimming one — do NOT cut films to hit a keep "
    "rate. Include every film of the collection that is on disk, in watch order, "
    "unless it genuinely is not part of the work.\n"
    'Manifest schema: {"name": "...", "items": [{"movie": "<exact filename stem, '
    'no extension>"}, ...]}. Items are ORDERED — that order IS the playlist. Use a '
    '{"movie": ...} token per film; a stem must match a real file in Movies/ exactly '
    "(case-insensitively), so list the folder and copy each stem verbatim. If a TV "
    "show in the library must be interleaved with the films, you may also use "
    '{"path": "Shows/<show>/Season NN/<file>.mkv"} tokens for the specific episodes '
    "that belong in the run.\n"
)


def _run_movie_curation(task: dict) -> bool:
    """Build one film-collection playlist. Same model-proposes/harness-disposes
    discipline as autobuild._run_curation: the run writes a candidate manifest to a
    TEMP path, and it is promoted over the live manifest ONLY if it resolves to real
    files — so a bad run can never clobber a good playlist."""
    slug = task["slug"]
    tmp_path = config.TMP_DIR / f"autobuild_{slug}.json"
    real_path = playlist.manifest_path_for(slug)
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    if tmp_path.exists():
        tmp_path.unlink()

    films = "\n".join(f"  {m}" for m in task.get("movies") or [])
    prompt = (
        f"Build an ORDERED playlist manifest for the film collection: {task['name']}.\n\n"
        f"{task['brief']}\n{_MOVIE_RULES}\n"
        f"The library root is {config.MEDIA_ROOT}; films live FLAT in "
        f"{config.MOVIES_ROOT}. The films identified as this collection are:\n{films}\n"
        f"Verify each one against the real folder listing (and check whether any "
        f"OTHER film on disk belongs to this collection and was missed). Decide the "
        f"order deliberately — say in the manifest's \"description\" whether it is "
        f"chronological or release order.\n"
        f"Write the finished manifest JSON to this EXACT path and nothing else: "
        f"{tmp_path}\n"
        f'Use "name": "{task["name"]}".'
    )
    cmd = [*config.AI_BIN, "-p", "--output-format", "json",
           "--tools", "Read,Glob,Grep,ListDir,WebSearch,WebFetch,Write",
           "--max-turns", "60", "--timeout", "1740"]
    if config.AI_MODEL:
        cmd += ["--model", config.AI_MODEL]

    _log(f"curating film collection {slug} (headless AI run)...")
    try:
        subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=1800, env=autobuild._ai_env(),
                       cwd=str(config.PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        _log(f"{slug}: curation timed out; retry next cycle")
        return False
    if not tmp_path.exists():
        _log(f"{slug}: no manifest produced; retry next cycle")
        return False
    try:
        manifest = json.loads(tmp_path.read_text())
    except json.JSONDecodeError as exc:
        _log(f"{slug}: candidate manifest invalid JSON ({exc}); retry next cycle")
        return False

    paths, problems = playlist.resolve_items(manifest)
    if problems or len(paths) < 2:
        _log(f"{slug}: candidate did not resolve ({len(problems)} problems, "
             f"{len(paths)} files); keeping existing playlist, retry next cycle")
        return False

    real_path.parent.mkdir(parents=True, exist_ok=True)
    real_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    _log(f"{slug}: {len(paths)} films resolved; building in Jellyfin")
    try:
        return bool(playlist.build_playlist(real_path, playlist.Jellyfin(), dry_run=False))
    except Exception as exc:                                          # noqa: BLE001
        _log(f"{slug}: Jellyfin build failed ({exc}); manifest saved, retry next cycle")
        return False


# --- BUILD: one warranted-but-unbuilt playlist per cycle ---------------------

def _pending_builds(decisions: dict) -> list[dict]:
    done = autobuild._load_done()
    out = []
    for rel, d in decisions.items():
        if d.get("needs") and d.get("role") in ("flagship", "standalone") \
                and d.get("slug") not in done and not d.get("built"):
            task = {"slug": d["slug"], "name": d["name"], "brief": d.get("brief", ""),
                    "kind": d.get("kind", "shows")}
            if task["kind"] == "movies":
                task["movies"] = d.get("movies") or [rel]
                task["shows"] = []
            else:
                task["shows"] = d["shows"]
            out.append(task)
    return out


def _finalize_built(task: dict, decisions: dict) -> None:
    """Mark a slug built and seed its per-decision `_considered` ledger with every
    episode currently on disk, so the per-cycle prowl only judges episodes that land
    LATER -- it must never re-judge the ones this build already decided to cut.
    A film collection has no episodes to prowl (a newly-acquired film is picked up by
    the movie decide pass instead, § _assign_to_existing), so its ledger is empty."""
    autobuild._mark_done(task["slug"])
    considered = _episodes_across(task.get("shows") or [])
    for rel, d in decisions.items():
        if d.get("slug") == task["slug"] and d.get("role") in ("flagship", "standalone"):
            d["built"] = True
            d["_considered"] = considered


def _build_task(task: dict, decisions: dict) -> bool:
    """Build one warranted playlist (the model proposes -> validated builder pushes)."""
    real_path = playlist.manifest_path_for(task["slug"])
    # CHEAP RETRY: a valid manifest already exists (curated on a previous cycle; only
    # the Jellyfin push failed, e.g. Jellyfin was down) -> just re-push it. Don't burn
    # a fresh AI curation run to rebuild a manifest we already have.
    if real_path.exists():
        try:
            paths, problems = playlist.resolve_items(json.loads(real_path.read_text()))
            if paths and not problems and playlist.build_playlist(real_path, playlist.Jellyfin(), dry_run=False):
                _finalize_built(task, decisions)
                _log(f"{task['slug']} re-pushed from existing manifest")
                return True
        except Exception:                                             # noqa: BLE001
            pass    # Jellyfin down / manifest unresolvable -> fall through to a full curation
    # A film collection is an ORDERING job, not a filler cut, so it gets its own
    # curation runner and default brief -- never the shows' cut philosophy.
    if task.get("kind") == "movies":
        if not task.get("brief"):
            task["brief"] = (f"Put the films of {task['name']} into the order they should "
                             f"be watched. Decide chronological vs release order on the "
                             f"merits and slot any prequel/side-story where it belongs.")
        if not _run_movie_curation(task):
            return False
        _finalize_built(task, decisions)
        _log(f"{task['slug']} built")
        return True
    if not task.get("brief"):
        task["brief"] = (f"Curate a lean 'watchable' cut of {task['name']}. Drop the filler "
                         f"arcs and padded/stall/recap stretches; keep the cast/premise intros "
                         f"and the episodes where a fight turns or a real beat lands.")
    if not autobuild._run_curation(task):
        return False
    _finalize_built(task, decisions)
    _log(f"{task['slug']} built")
    return True


def build_all(decisions: dict, dry_run: bool, save_cb=None) -> int:
    """Build EVERY warranted-but-unbuilt playlist this cycle -- don't wait around.
    Deciding which shows warrant a playlist is a once-per-cycle scan; actually
    building the ones we already know we want should just go to town. Re-checks the
    API budget before each build so it stops gracefully (and resumes next cycle) if
    the spend limit is hit mid-run. Returns the number built."""
    pending = _pending_builds(decisions)
    if not pending:
        _log("no playlists pending build")
        return 0
    _log(f"{len(pending)} playlist(s) pending build: {', '.join(t['slug'] for t in pending)}")
    if dry_run:
        return 0
    built = 0
    for i, task in enumerate(pending):
        # Budget can lapse partway through a long build run; re-probe between builds
        # (skip the probe for the first, we already checked it in run_once).
        if i and not autobuild.budget_available():
            _log(f"API hit its spend limit after {built} build(s); {len(pending)-i} left for next cycle")
            break
        if _build_task(task, decisions):
            built += 1
            if save_cb:
                save_cb()          # persist after each build so progress survives a crash
    _log(f"built {built}/{len(pending)} pending playlist(s) this cycle")
    return built


def _episodes_across(show_rels: list) -> list:
    """Every main-series episode rel across one or more shows, sorted."""
    import playlist_watch
    out = []
    for s in show_rels or []:
        out += playlist_watch._all_episodes(s.rstrip("/"))
    return sorted(set(out))


def extend_ongoing(decisions: dict, dry_run: bool) -> bool:
    """On the prowl: for every ONGOING show that already has a playlist, judge only
    episodes that appeared AFTER the last consideration (tracked per-decision in
    `_considered`) and append the keepers. Idempotent and cheap on a settled library
    -- nothing new means nothing judged; it NEVER re-judges an already-cut episode.
    A freshly-built playlist has `_considered` seeded to its whole disk set, so the
    first prowl over a legacy playlist (no ledger yet) just bootstraps that set and
    judges nothing. Returns True if any decision changed."""
    if dry_run:
        return False
    done = autobuild._load_done()
    try:
        import playlist_watch      # noqa: E402  (local import: optional, heavy)
    except Exception as e:          # noqa: BLE001
        _log(f"playlist_watch unavailable ({e}); skipping proactive extend")
        return False

    changed = False
    for rel, d in decisions.items():
        if not (d.get("needs") and d.get("ongoing")
                and d.get("role") in ("flagship", "standalone") and d.get("slug") in done):
            continue
        if d.get("kind") == "movies":
            continue        # films have no episodes to prowl (§ _assign_to_existing)
        slug = d["slug"]
        shows = d.get("shows") or [rel]
        all_eps = _episodes_across(shows)
        considered = set(d.get("_considered") or [])
        if not considered:                      # legacy playlist, no ledger yet
            d["_considered"] = sorted(all_eps)   # bootstrap; judge nothing this pass
            changed = True
            continue
        manifest = playlist.load_or_init_manifest(slug, shows[0], d.get("name", slug))
        new = [r for r in all_eps if r not in considered
               and not playlist.manifest_has_path(manifest, r)]
        if not new:
            continue
        _log(f"{slug}: {len(new)} new episode(s) to judge on the prowl")
        added = 0
        for r in new:
            show_rel = "/".join(r.split("/")[:2])          # "Shows/<name>"
            show_name = Path(show_rel).name
            verdict = playlist_watch.judge_episode(config.MEDIA_ROOT / r, show_name, show_rel, _log)
            if verdict is None:
                continue                                    # transient; retry next cycle (stays unconsidered)
            considered.add(r)
            if verdict.get("keep"):
                if playlist.append_path_item(slug, show_rel, d.get("name", slug), r, verdict.get("reason", "")):
                    added += 1
        d["_considered"] = sorted(considered)
        changed = True
        if added:
            _log(f"extended {slug} with {added} new keeper(s)")
            playlist_watch._rebuild([slug])
    return changed


# --- keep the auto-extend set (playlist_watch) in sync -----------------------

def sync_auto_shows(decisions: dict) -> None:
    """Every ONGOING show that has (or is getting) a playlist is written here so
    playlist_watch appends new weekly episodes automatically -- no hard-coded list."""
    auto = {}
    for rel, d in decisions.items():
        if d.get("kind") == "movies":
            continue        # playlist_watch only understands Shows/ episode paths
        if d.get("needs") and d.get("ongoing") and d.get("role") in ("flagship", "standalone"):
            auto[rel] = d["slug"]
        elif d.get("role") == "member" and d.get("ongoing"):
            auto[rel] = d["slug"]
    _save(AUTO_SHOWS_FILE, auto)


# --- driver ------------------------------------------------------------------

def run_once(dry_run=False, decide_only=False, kinds=("shows", "movies"),
             force=False) -> None:
    # Playlist judgment/build is NON-ingestion AI work, so it runs only inside the
    # off-peak window -- never competing with identify for the API budget during the day.
    # `--dry-run` is read-only (no AI) and always allowed; `--force` is the manual escape
    # hatch that overrides the window.
    if not force and not dry_run and not config.ai_budget_healthy():
        _log("off-peak window not active; deferring playlist AI judgment/build")
        return
    decisions = _load(DECISIONS_FILE, {})
    if seed_from_legacy(decisions):
        _save(DECISIONS_FILE, decisions)

    if not autobuild.budget_available():
        _log("AI API unavailable (no balance or no credential); retry next cycle")
        sync_auto_shows(decisions)
        return

    if "shows" in kinds and decide(decisions, dry_run):
        _save(DECISIONS_FILE, decisions)
    # Films get their own judgment (ordering, not cutting). Re-probe the budget: the
    # show pass may have just spent what was left.
    if "movies" in kinds and (dry_run or autobuild.budget_available()):
        if decide_movies(decisions, dry_run):
            _save(DECISIONS_FILE, decisions)

    if not decide_only:
        # Build EVERY warranted-but-unbuilt playlist now (don't wait around), then
        # prowl the ongoing ones for newly-landed episodes to append.
        build_all(decisions, dry_run, save_cb=lambda: _save(DECISIONS_FILE, decisions))
        if extend_ongoing(decisions, dry_run):
            _save(DECISIONS_FILE, decisions)

    sync_auto_shows(decisions)


def print_status() -> None:
    decisions = _load(DECISIONS_FILE, {})
    seed_from_legacy(decisions)
    done = autobuild._load_done()

    def _report(label, yes, undecided, unit):
        no = [r for r, d in decisions.items()
              if not d.get("needs") and (d.get("kind", "shows") == label)]
        print(f"{label}: {len(yes)} playlist(s) warranted | {len(no)} no-playlist")
        for r, d in sorted(yes):
            built = "built" if (d.get("slug") in done or d.get("built")) else "PENDING"
            members = d.get("movies") if label == "movies" else d.get("shows")
            combo = f" (+{len(members)-1} more)" if len(members or []) > 1 else ""
            print(f"  [{built:7}] {d['slug']}{combo}  <- {r}  -- {d.get('reason','')}")
        if undecided:
            print(f"  {len(undecided)} {unit}(s) still undecided "
                  f"(will be judged next budgeted cycle)")

    show_yes = [(r, d) for r, d in decisions.items()
                if d.get("needs") and d.get("role") in ("flagship", "standalone")
                and d.get("kind", "shows") != "movies"]
    print(f"decisions: {len(decisions)} entries in the ledger")
    _report("shows", show_yes,
            [r for r in _all_show_rels() if r not in decisions], "show")
    _report("movies", _movie_collections(decisions),
            [r for r in _all_movie_stems() if r not in decisions], "film")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Decide which shows earn a curated cut and which film collections "
                    "earn an ordered playlist, then build them.")
    ap.add_argument("--once", action="store_true", help="one decide+build pass, then exit")
    ap.add_argument("--decide-only", action="store_true", help="judge; build nothing")
    ap.add_argument("--dry-run", action="store_true", help="report only; no AI runs, no writes")
    ap.add_argument("--status", action="store_true", help="print the decision ledger and exit")
    ap.add_argument("--rejudge", action="store_true", help="wipe all decisions and re-judge from scratch")
    ap.add_argument("--shows-only", action="store_true", help="judge shows only, skip films")
    ap.add_argument("--movies-only", action="store_true", help="judge film collections only, skip shows")
    ap.add_argument("--force", action="store_true",
                    help="run the AI judgment/build even outside the off-peak window")
    args = ap.parse_args()

    if args.status:
        print_status()
        return 0
    if args.rejudge and DECISIONS_FILE.exists():
        DECISIONS_FILE.unlink()
        _log("decision ledger wiped; every show and film will be re-judged")

    kinds = ("shows", "movies")
    if args.shows_only:
        kinds = ("shows",)
    elif args.movies_only:
        kinds = ("movies",)

    if args.once or args.decide_only or args.dry_run or args.shows_only or args.movies_only:
        run_once(args.dry_run, args.decide_only, kinds, force=args.force)
        return 0

    # daemon (fallback; launchd runs this script with --once on a StartInterval)
    interval = int(os.environ.get("PLAYLIST_CURATOR_CYCLE_SEC", str(3 * 3600)))
    _log(f"playlist_curator daemon up (cycle {interval}s)")
    while True:
        try:
            run_once()
        except Exception as e:                                        # noqa: BLE001
            _log(f"cycle error (non-fatal): {e}")
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
