#!/usr/bin/env python3
"""playlist_autobuild.py -- session-independent playlist (re)build daemon.

Curating a show needs a headless AI research run, which fails while the account
has no balance. This script, run periodically by launchd
(`com.mikeyferguson.playlistautobuild`), waits for the API to answer again and
then builds/re-builds the queued playlists **unattended** -- so the work finishes
on its own once the account is topped up.

Each cycle:
  1. If every task is done -> no-op, exit.
  2. Probe whether the API will answer (a tiny one-turn ping). Still refusing ->
     log and exit; retry next cycle.
  3. API available -> do ONE pending task (bounds spend, lets a burst of work
     spread out): a headless run researches the show and writes a candidate
     manifest to a TEMP path (the model proposes); the deterministic builder
     validates it; only if it
     resolves to real files is it promoted over the live manifest and pushed to
     Jellyfin (harness disposes). A bad run therefore can NEVER clobber an
     existing good playlist -- the temp just gets discarded and the task retries.

Tasks are gated purely by the done-marker (state/playlist_autobuild_done.json),
so this re-curates existing playlists too when the marker is cleared (that is how
a philosophy change -- e.g. "be more aggressive" -- is rolled out to every show).
Nothing here ever touches a library file.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config              # noqa: E402
import playlist            # noqa: E402
import playlist_curation   # noqa: E402

DONE_FILE = config.STATE_DIR / "playlist_autobuild_done.json"

# The cut philosophy lives in playlist_curation.CUT_PHILOSOPHY (shared with the
# per-episode judge in playlist_watch.py). It is description-driven -- cut DEAD
# TIME read from each episode's `.nfo` `<plot>`, not "un-fun canon" -- and paired
# with a per-show keep-rate band + real KEEP/REMOVE examples injected per task.
_RULES = playlist_curation.CUT_PHILOSOPHY + (
    "\nManifest schema: {\"name\": \"...\", \"shows\": [<folders>], "
    "\"items\": [{\"path\": ...}, ...]}. Write it to the EXACT temp path given "
    "below. Include good films from Movies/ where they fit.\n"
)

CURATION_TASKS = [
    {"slug": "hidden-leaf-watchable", "name": "The Hidden Leaf - Watchable",
     "shows": ["Shows/Naruto (2002)", "Shows/Naruto Shippuden (2007)",
               "Shows/Boruto - Naruto Next Generations (2017)"],
     "brief": "One combined 'Hidden Leaf' saga in watch order: Naruto (2002) -> Naruto "
              "Shippuden (2007) -> Boruto - Naruto Next Generations (2017).\n"
              "NARUTO (2002) = 5 sequential seasons (S01=abs1-57, S02=58-100, S03=101-141, "
              "S04=142-183, S05=184-220). Cut the ~41% filler; keep the cast intros (S01 eps 1-24) whole.\n"
              "SHIPPUDEN (2007) = 22 ARC-BASED seasons; ls every Season folder to read the real "
              "SxxExx. Cut the heavy filler and the drawn-out war stalls -- read each episode's .nfo "
              "plot and drop the recap/reaction/stall episodes while keeping the ones that turn a "
              "fight or land a beat; keep the finale (S21).\n"
              "BORUTO (2017) = one continuous ABSOLUTE Season 01 (293 eps, files S01E01..S01E293) plus "
              "Season 00 (2 specials). ls the folder for real numbers -- do NOT trust a guide's arc "
              "numbering. Boruto is ~40%+ filler/slice-of-life: cut RUTHLESSLY. Keep the Academy/team "
              "intros, the Chunin Exams + Momoshiki (movie) arc, the Mujina Bandits/Ao arc, the "
              "Kara/Kawaki introduction, and the Isshiki/Kurama climax; drop the mission-of-the-week "
              "padding and the stall/reaction episodes (judge by each episode's .nfo plot).\n"
              "FILMS (all in Movies/): the two CANON, must-keep films are 'The Last - Naruto the Movie "
              "(2014)' (after the Shippuden finale, before Boruto -- it bridges the eras) and "
              "'Road to Ninja - Naruto the Movie (2012)' (Kishimoto-written, slot late-Shippuden). The "
              "other Naruto/Shippuden films (Ninja Clash in the Land of Snow, Legend of the Stone of "
              "Gelel, Guardians of the Crescent Moon Kingdom, Naruto Shippuden the Movie, Bonds, The "
              "Will of Fire, The Lost Tower, Blood Prison) are non-canon -- include one only if it is "
              "genuinely fun by the ruthless rule, else drop. There is NO standalone Boruto film on "
              "disk: 'Boruto: Naruto the Movie' is re-adapted into the Boruto TV arc (the Momoshiki "
              "arc), so use those episodes, not a film token."},
    {"slug": "fairy-tail-watchable", "name": "Fairy Tail - Watchable",
     "shows": ["Shows/Fairy Tail (2009)", "Shows/Fairy Tail - 100 Years Quest (2024)"],
     "brief": "Combined Fairy Tail (2009) -> Fairy Tail - 100 Years Quest (2024). Fairy Tail "
              "has heavy filler and padded arcs -- cut hard. Keep the guild intro and the major "
              "arcs' setup+payoff. Films in Movies/: 'Fairy Tail - Phoenix Priestess (2012)' and "
              "'Fairy Tail - Dragon Cry (2017)' -- slot if worth it."},
    {"slug": "steven-universe-watchable", "name": "Steven Universe - Watchable",
     "shows": ["Shows/Steven Universe (2013)", "Shows/Steven Universe Future (2019)"],
     "brief": "Steven Universe lore cut. Keep the essential serialized lore + the great "
              "standalones + intros; drop weak slice-of-life. Mind broadcast-vs-streaming order. "
              "Slot 'Steven Universe: The Movie' (Movies/) between the series and Future if present."},
    {"slug": "sailor-moon-watchable", "name": "Sailor Moon - Watchable",
     "shows": ["Shows/Sailor Moon (1992)"],
     "brief": "Original Sailor Moon (1992), five arcs (Classic/R/S/SuperS/Stars). Cut the "
              "monster-of-the-week filler hard; keep arc openers/closers, every Guardian intro, "
              "and the major transformations/reveals. Add the R/S/SuperS films from Movies/ if present."},
    {"slug": "one-piece-watchable", "name": "One Piece - Watchable",
     "shows": ["Shows/One Piece (1999)"],
     "brief": "One Piece (1999). Cut the DEAD TIME: every filler island AND the padded canon "
              "(Davy Back drag, Hody drag, Caesar drag, the Dressrosa colosseum/birdcage crawl, "
              "Whole Cake stalls, Wano raid padding) -- read each episode's .nfo plot and drop the "
              "stall/crowd-reaction/recap episodes while KEEPING the ones where a fight actually "
              "turns or resolves. Keep the East Blue intros and the big beats (Arlong Park, "
              "Alabasta finish, Enies Lobby climax, Marineford, Gear 5). Slot the strong films "
              "(Strong World, Film Z, Gold, Stampede, Red, Baron Omatsuri, 3D2Y). NOTE: this "
              "playlist is also auto-extended by playlist_watch.py; overwriting the manifest is fine."},
    {"slug": "dragon-ball-watchable", "name": "Dragon Ball - Watchable",
     "shows": ["Shows/Dragon Ball (1986)", "Shows/Dragon Ball Z (1989)", "Shows/Dragon Ball GT (1996)",
               "Shows/Dragon Ball Daima (2024)", "Shows/Dragon Ball Super (2015)"],
     "brief": "Combined franchise in watch order DB -> DBZ -> GT -> Daima -> Super. DBZ especially "
              "has huge in-episode padding (the Namek 'five minutes' countdown, Frieza/Buu stalling) "
              "-- read each episode's .nfo plot and drop the stall/charge-up/reaction episodes while "
              "KEEPING the ones where the fight turns or resolves. Redundancy rule: Garlic Jr TV arc "
              "-> Dead Zone film; Super Battle of Gods / Resurrection F arcs -> the films (in Movies/ "
              "under DBZ naming). Slot the good DBZ/Super films from Movies/."},
    {"slug": "bleach-watchable", "name": "Bleach - Watchable",
     "shows": ["Shows/Bleach (2004)"],
     "brief": "Bleach (2004), one absolute season E001-E406 (E367-406 = TYBW, all canon, keep). "
              "Cut all the filler arcs (Bount, Karakuraizer, the New Captain Shusuke Amagai arc, "
              "Zanpakuto Rebellion) and the beach/swimsuit downtime; read each episode's .nfo plot "
              "and thin the long Soul Society / Hueco Mundo fights by dropping the stall/reaction "
              "episodes while KEEPING the ones where a fight turns or a bankai is revealed. Keep the "
              "Agent-arc intros (1-20)."},
    {"slug": "adventure-time-watchable", "name": "Adventure Time - Watchable",
     "shows": ["Shows/Adventure Time (2010)", "Shows/Adventure Time - Distant Lands (2020)",
               "Shows/Adventure Time - Fionna and Cake (2023)"],
     "brief": "Adventure Time lore/arc cut (11-min segment numbering, 1:1 with files). Keep the "
              "Lich/Simon-Marceline/Finn-origin/GOLB spine + the beloved standalones + intros; drop "
              "throwaway one-offs. End with Distant Lands then Fionna and Cake."},
    {"slug": "regular-show-watchable", "name": "Regular Show - Watchable",
     "shows": ["Shows/Regular Show (2010)"],
     "brief": "Regular Show (2010), 8 seasons of self-contained gag comedy with little plot spine. "
              "FAST-PACED cut: keep the inventive/memorable/fan-favorite episodes and the "
              "recurring-cast/arc threads (Mordecai/Margaret/CJ, Muscle Man/Starla, the "
              "Rigby-Eileen thread, the space/future finale run); drop the forgettable, repetitive, "
              "low-stakes one-offs (read each episode's .nfo plot). Slot 'Regular Show: The Movie' "
              "from Movies/ if present."},
    {"slug": "gintama-watchable", "name": "Gintama - Watchable",
     "shows": ["Shows/Gintama (2006)"],
     "brief": "Gintama (2006), one continuous ABSOLUTE season ~E1-E367 (pilot-EXCLUDED: disk "
              "S01E01-E02='Gintama 001-002', E202='S02E01'; map by title, do NOT add a +2 offset). "
              "FAST-PACED arc-forward cut (user prefers fast pacing): keep ALL the serious arcs "
              "(Benizakura, Yoshiwara in Flames, Mitsuba, Shinsengumi Crisis, Shogun Assassination, "
              "Farewell Shinsengumi, Rakuyo, Silver Soul) + the cast intros (~E1,2,3,5,6,7,8,11,13,15) "
              "+ ONLY the elite/legendary gag episodes; drop the many slow, leisurely one-offs (not "
              "just the flat ones). Films/specials in Movies/ and Season 00 (Semi-Final -> The Very "
              "Final ending)."},
]


def _log(msg: str) -> None:
    print(f"[playlist_autobuild] {msg}", flush=True)


def _load_done() -> set[str]:
    if DONE_FILE.exists():
        try:
            return set(json.loads(DONE_FILE.read_text()))
        except Exception:                                             # noqa: BLE001
            return set()
    return set()


def _mark_done(slug: str) -> None:
    done = _load_done()
    done.add(slug)
    DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DONE_FILE.write_text(json.dumps(sorted(done), indent=2) + "\n")


def _ai_env() -> dict:
    """The headless-run environment (PATH for the agent's own Bash); see config.ai_env."""
    return config.ai_env()


def budget_available() -> bool:
    """Tiny ping. Clean exit => the API will answer; non-zero => it will not, and the
    cycle skips rather than spending a full curation run to learn the same thing."""
    cmd = [*config.AI_BIN, "-p", "--max-turns", "1", "--tools", "",
           "--output-format", "text"]
    if config.AI_MODEL:
        cmd += ["--model", config.AI_MODEL]
    try:
        proc = subprocess.run(cmd, input="Reply with exactly: OK",
                              capture_output=True, text=True, timeout=90,
                              env=_ai_env(), cwd=str(config.PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        return False
    if config.identify_unavailable((proc.stdout + proc.stderr).lower()):
        return False
    return proc.returncode == 0


def _run_curation(task: dict) -> bool:
    """The headless run writes a candidate manifest to a TEMP path; only if it
    resolves to real files is it promoted over the live manifest and built. A bad
    run leaves the existing playlist untouched. Returns True on success."""
    slug = task["slug"]
    tmp_path = config.TMP_DIR / f"autobuild_{slug}.json"
    real_path = playlist.manifest_path_for(slug)
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    if tmp_path.exists():
        tmp_path.unlink()

    band = playlist_curation.band_for(task["shows"])
    examples = playlist_curation.examples_block(task["shows"])
    prompt = (
        f"Build a curated 'watchable' playlist manifest for: {task['name']}.\n\n"
        f"{task['brief']}\n{_RULES}\n"
        f"KEEP BAND for this show: aim for roughly {band} of episodes kept. This is a "
        f"self-check, not a hard quota -- if your cut lands far outside it, reconsider.\n"
        f"{examples}\n"
        f"The library root is {config.MEDIA_ROOT}. Inspect the show folder(s): "
        f"{', '.join(task['shows'])}. Write the finished manifest JSON to this EXACT "
        f"path and nothing else: {tmp_path}\n"
        f'Use "shows": {json.dumps(task["shows"])} and "name": "{task["name"]}".'
    )
    cmd = [
        *config.AI_BIN, "-p",
        "--output-format", "json",
        "--tools", "Read,Glob,Grep,ListDir,WebSearch,WebFetch,Write",
        "--max-turns", "80", "--timeout", "1740",
    ]
    if config.AI_MODEL:
        cmd += ["--model", config.AI_MODEL]

    _log(f"curating {slug} (headless AI run)...")
    try:
        subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=1800, env=_ai_env(), cwd=str(config.PROJECT_ROOT))
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
    if problems or not paths:
        _log(f"{slug}: candidate did not resolve ({len(problems)} problems, "
             f"{len(paths)} files); keeping existing playlist, retry next cycle")
        return False

    # Promote the validated candidate over the live manifest, then build.
    real_path.parent.mkdir(parents=True, exist_ok=True)
    real_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    _log(f"{slug}: {len(paths)} files resolved; building in Jellyfin")
    built_ok = False
    try:
        jf = playlist.Jellyfin()
        built_ok = bool(playlist.build_playlist(real_path, jf, dry_run=False))
    except Exception as exc:                                          # noqa: BLE001
        _log(f"{slug}: Jellyfin build failed ({exc}); manifest saved, will retry next cycle")
    # Return whether the playlist ACTUALLY landed in Jellyfin. The manifest is saved
    # either way (the expensive curation isn't lost), but the caller must NOT mark the
    # task done on a failed push -- otherwise a transient Jellyfin outage permanently
    # leaves the playlist un-built (it only reappears if the nightly rebuild happens to
    # run). A False return makes the curator retry -- cheaply, by re-pushing the manifest.
    return built_ok


def main() -> int:
    done = _load_done()
    pending = [t for t in CURATION_TASKS if t["slug"] not in done]
    if not pending:
        _log("all playlists built; nothing to do")
        return 0

    # Playlist curation is a NON-ingestion AI run, so it fires only inside the
    # off-peak window -- it must not compete with the identify step for the API budget
    # during the day.
    if not config.ai_budget_healthy():
        _log("off-peak window not active; deferring playlist curation AI runs")
        return 0

    _log(f"{len(pending)} playlist(s) pending: {', '.join(t['slug'] for t in pending)}")
    if not budget_available():
        _log("AI API still unavailable (no balance or no credential); retry next cycle")
        return 0

    task = pending[0]                       # one per cycle -> bounds spend
    if _run_curation(task):
        _mark_done(task["slug"])
        _log(f"{task['slug']} done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
