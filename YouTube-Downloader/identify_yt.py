"""The identify step: hand a downloaded wave to a headless AI run, get back routing
decisions plus AI-authored metadata, and turn them into a Torrent-Ingest placement plan.

Division of labour, deliberately drawn where Torrent-Ingest draws it:

  * THE MODEL decides identity and writes prose. Which show/film this video is, whether it
    belongs to the playlist's series or to a show already on disk or stands alone as a
    film, and the episode/film title and plot -- the judgment and the writing.
  * THE ENGINE decides paths and numbers. Show folder naming, filename construction,
    episode numbering, collision refusal. Every deterministic, safety-relevant decision
    stays in code, so a hallucinated path or a reused episode number cannot reach disk.

The numbering split is the load-bearing part. The old version of this repo numbered
episodes by PLAYLIST POSITION, so inserting a video at the top of a playlist renumbered
every episode below it and orphaned the files already on disk. Numbers here are assigned
in append order from what is actually on disk, and never revisited.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import classify
import library                      # Torrent-Ingest: the library digest + plan machinery
import ytconfig


class IdentifyError(RuntimeError):
    """The batch could not be routed. Retried on a later cycle."""


class IdentifyUnavailable(RuntimeError):
    """The routing run could not START -- the API has no credential or no balance.

    NOT an IdentifyError subclass, deliberately: process_batch() answers an IdentifyError by
    calling led.mark_failed_batch(), which spends one of a capped number of retries and, past
    that cap, abandons the videos permanently. A batch that was never routed has nothing
    wrong with it, so it must leave no mark on the ledger at all. Mirrors
    identify.IdentifyUnavailable in Torrent-Ingest.
    """


# --- prompt ------------------------------------------------------------------

def _describe(entry: dict, info: dict, path: Path) -> str:
    """One video's block in the runtime context: everything needed to route it and to
    write its metadata without opening the file."""
    desc = (info.get("description") or "").strip()
    if len(desc) > 1200:
        desc = desc[:1200].rstrip() + " [...]"
    dur = info.get("duration") or entry.get("duration") or 0
    mins = f"{int(dur) // 60}m{int(dur) % 60:02d}s" if dur else "unknown"
    upload = str(info.get("upload_date") or "")
    upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:8]}" if len(upload) == 8 else "unknown"
    return (
        f"  - video_id: {entry['id']}\n"
        f"    title: {info.get('title') or entry['title']!r}\n"
        f"    channel: {info.get('uploader') or info.get('channel') or '?'!r}\n"
        f"    duration: {mins}\n"
        f"    uploaded: {upload}\n"
        f"    file: {path}\n"
        f"    description: |\n"
        + "\n".join(f"      {ln}" for ln in (desc or "(none)").splitlines()[:40])
    )


def _series_context(pinned: dict | None, playlist: dict, next_ep: int) -> str:
    if pinned:
        return (
            f"THIS PLAYLIST ALREADY HAS A SHOW IN THE LIBRARY:\n"
            f"  {pinned['show_rel']}\n"
            f"Videos you route as `series` are appended to it. Use this EXACT folder --\n"
            f"do not rename it, re-year it, or propose a variant, even if the playlist\n"
            f"has since been retitled on YouTube; its existing episodes live there. Put\n"
            f"its title and year in the `series` object to match:\n"
            f"  title: {pinned.get('title') or Path(pinned['show_rel']).name.rsplit(' (', 1)[0]!r}\n"
            f"  year:  {pinned.get('year')}\n"
            f"The engine will number new episodes from S{ytconfig.SEASON:02d}E{next_ep:02d} "
            f"onward.\n"
        )
    return (
        f"THIS PLAYLIST HAS NO SHOW YET. If you route any video as `series`, the\n"
        f"`series` object you write becomes a NEW show folder named `<title> (<year>)`,\n"
        f"and it is pinned permanently -- so pick a name you would still want in a year.\n"
        f"Check the digest first: if this playlist is really a body of content for a show\n"
        f"that ALREADY exists there, prefer `existing_show` over creating a near-duplicate.\n"
    )


def _runtime_prompt(playlist: dict, batch: list, downloaded: dict,
                    pinned: dict | None, next_ep: int, plan_path: Path) -> str:
    base = ytconfig.IDENTIFY_PROMPT_FILE.read_text(encoding="utf-8")
    digest = library.build_library_digest()
    blocks = "\n".join(_describe(e, downloaded[e["id"]]["info"],
                                 downloaded[e["id"]]["path"]) for e in batch)
    return f"""{base}

======================================================================
RUNTIME CONTEXT FOR THIS BATCH
======================================================================

Source playlist:
  id:          {playlist['id']}
  title:       {playlist.get('title')!r}
  discovered:  {playlist.get('source')}

{_series_context(pinned, playlist, next_ep)}
The library root is {ytconfig.MEDIA_ROOT} (browse it through {library.config.MEDIAFS_MOUNT}
if a file's bytes are not local -- every title is visible there whether or not it is).

VIDEOS IN THIS BATCH ({len(batch)}), in the order the engine will number them:
{blocks}

{digest}

======================================================================
YOUR OUTPUT
======================================================================

Write your plan as strict JSON to EXACTLY this path (overwrite if present):

{plan_path}

Every video_id above must appear exactly once in `videos`. Then reply with a short
plain-English rationale. Do not move, rename, or delete any file.
"""


# --- the run -----------------------------------------------------------------

def _ai_env() -> dict:
    """The headless-run environment (PATH for the agent's own Bash); see config.ai_env."""
    return ytconfig.ai_env()


def _is_transient(detail: str) -> bool:
    low = (detail or "").lower()
    return any(sig in low for sig in ytconfig.IDENTIFY_TRANSIENT_SIGNATURES)


def _is_unavailable(detail: str) -> bool:
    """The CLI could not run -- rate-limited or credential-less. The run never happened.

    Shared with Torrent-Ingest's identify so both ingests classify a failure alike.
    """
    return ytconfig.identify_unavailable(detail)


def _timeout_for(n: int) -> int:
    scaled = ytconfig.IDENTIFY_TIMEOUT_BASE_SEC + ytconfig.IDENTIFY_TIMEOUT_PER_VIDEO_SEC * n
    return max(ytconfig.IDENTIFY_TIMEOUT_BASE_SEC,
               min(scaled, ytconfig.IDENTIFY_TIMEOUT_MAX_SEC))


def _extract_rationale(stdout: str) -> str:
    stdout = (stdout or "").strip()
    if not stdout:
        return ""
    try:
        env = json.loads(stdout)
        if isinstance(env, dict):
            return (env.get("result") or "").strip()
    except json.JSONDecodeError:
        pass
    return stdout[:2000]


def run_identify(playlist: dict, batch: list, downloaded: dict,
                 pinned: dict | None, next_ep: int, log_fn=print) -> tuple[dict, str]:
    """Invoke the headless AI run for one batch. Returns (routing_dict, rationale).

    Retries the whole run on a TRANSIENT mid-stream failure (a network/server blip
    cutting the streamed plan), exactly as Torrent-Ingest's identify does. A
    deterministic failure -- a plan that does not parse, a plan that fails validation --
    is not retried here; the batch is failed and picked up on a later cycle.
    """
    batch_key = re.sub(r"[^A-Za-z0-9_-]", "_", playlist["id"])[:40]
    plan_path = ytconfig.TMP_DIR / f"yt_{batch_key}_{batch[0]['id']}_plan.json"
    ytconfig.TMP_DIR.mkdir(parents=True, exist_ok=True)

    prompt = _runtime_prompt(playlist, batch, downloaded, pinned, next_ep, plan_path)
    timeout = _timeout_for(len(batch))

    cmd = [
        *ytconfig.AI_BIN, "-p",
        "--output-format", "json",
        "--tools", "Read,Write,Glob,Grep,Probe,ListDir,WebSearch,WebFetch",
        "--max-turns", "50",
        # The agent's own budget, a minute inside the subprocess kill below, so a plan
        # already written survives the cutoff instead of dying with the process.
        "--timeout", str(max(60, timeout - 60)),
    ]
    if ytconfig.AI_MODEL:
        cmd += ["--model", ytconfig.AI_MODEL]

    last_err = "identify failed"
    attempts = max(1, ytconfig.IDENTIFY_MAX_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        if plan_path.exists():
            plan_path.unlink()
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  timeout=timeout, env=_ai_env(),
                                  cwd=str(ytconfig.PROJECT_ROOT))
        except subprocess.TimeoutExpired:
            raise IdentifyError(f"identify timed out after {timeout}s ({len(batch)} videos)")

        rationale = _extract_rationale(proc.stdout)
        if proc.returncode != 0:
            detail = rationale or (proc.stderr or "")[:500]
            last_err = f"identify run exited {proc.returncode}: {detail}"
            # Exit 2 IS the unavailable signal (ai_runner); the text match is the
            # fallback for a provider that phrases a spend cap in prose instead.
            if proc.returncode == 2 or _is_unavailable(detail):
                # Never routed. Raise the distinct type so the caller leaves the ledger
                # untouched instead of spending a capped retry on a healthy batch.
                raise IdentifyUnavailable(last_err)
            retryable = _is_transient(detail)
        elif not plan_path.exists():
            last_err = f"identify produced no plan at {plan_path}"
            retryable = True
        else:
            try:
                return json.loads(plan_path.read_text(encoding="utf-8")), rationale
            except json.JSONDecodeError as exc:
                last_err = f"plan JSON invalid: {exc}"
                retryable = True

        if attempt < attempts and retryable:
            backoff = ytconfig.IDENTIFY_RETRY_BACKOFF_SEC * attempt
            log_fn(f"  identify transient failure (attempt {attempt}/{attempts}), "
                   f"retrying in {backoff}s: {last_err[:160]}")
            time.sleep(backoff)
            continue
        raise IdentifyError(last_err)

    raise IdentifyError(last_err)


# --- routing -> a Torrent-Ingest plan ----------------------------------------

def _need(v: dict, key: str, vid: str, route: str) -> str:
    val = str(v.get(key) or "").strip()
    if not val:
        raise IdentifyError(f"video {vid} routed as {route} with no {key} "
                            f"(it would be LOCKED blank in Jellyfin)")
    return val


def _episode_filename(show_folder: str, season: int, episode: int, title: str) -> str:
    return (f"{show_folder} - S{season:02d}E{episode:02d} - "
            f"{classify.sanitize(title)}.{ytconfig.CONTAINER}")


def _exists_in_library(dst_rel: str) -> bool:
    """Whether a destination is already taken.

    Checked through the mediafs MOUNT as well as the local root: an episode whose bytes
    have been evicted to the MEGA pool is absent locally but very much still in the
    library, and reusing its number would collide the moment it is materialised (and
    would be silently skipped as 'pre-existing' by the applier in the meantime)."""
    if (ytconfig.MEDIA_ROOT / dst_rel).exists():
        return True
    mount = getattr(library.config, "MEDIAFS_MOUNT", None)
    return bool(mount and (Path(mount) / dst_rel).exists())


def build_plan(routing: dict, batch: list, downloaded: dict,
               pinned: dict | None, next_ep: int, log_fn=print) -> tuple[dict, dict, dict]:
    """Turn the run's routing into a validated-shape Torrent-Ingest plan.

    Returns (plan, per_video, series) where `per_video` maps video_id -> the plan file
    entry (or {"route": "skip", "reason": ...}), and `series` is the show that the
    `series`-routed videos landed in (or None). The plan is NOT yet passed through
    `library.validate_plan` -- the caller does that, so validation stays the single
    gate in front of `apply_plan` exactly as it is for a torrent.

    Everything path- and number-shaped is decided HERE, not by the model:
      * the show folder is the pinned one if this playlist already has one;
      * `series` episode numbers are handed out in batch order from `next_ep`;
      * a destination already present in the library is REFUSED rather than allowed to
        be silently skipped as pre-existing (which would mark a video 'placed' while
        pointing at somebody else's episode).
    """
    by_id = {v.get("video_id"): v for v in (routing.get("videos") or [])
             if isinstance(v, dict)}
    series_meta = routing.get("series") if isinstance(routing.get("series"), dict) else None

    # Resolve the show folder ONCE for the whole batch.
    show_folder = None
    if pinned:
        show_folder = Path(pinned["show_rel"]).name
        series_year = pinned.get("year")
        series_title = show_folder.rsplit(" (", 1)[0]
    else:
        series_title = series_year = None

    files, per_video = [], {}
    ep = next_ep
    used_tops = set()

    for entry in batch:
        vid = entry["id"]
        v = by_id.get(vid)
        if v is None:
            raise IdentifyError(f"routing omitted video {vid}; the batch is incomplete")
        route = str(v.get("route") or "").strip().lower()
        src = downloaded[vid]["path"]

        if route == "skip":
            per_video[vid] = {"route": "skip",
                              "reason": str(v.get("reason") or "no reason given")[:300]}
            continue

        if route == "series":
            if show_folder is None:
                if not series_meta:
                    raise IdentifyError(
                        f"video {vid} routed as series but the plan has no `series` object")
                series_title = _need(series_meta, "title", vid, "series")
                try:
                    series_year = int(series_meta.get("year"))
                except (TypeError, ValueError):
                    raise IdentifyError(f"series {series_title!r} has no usable year")
                show_folder = f"{classify.sanitize(series_title)} ({series_year})"
            title = _need(v, "episode_title", vid, "series")
            plot = _need(v, "plot", vid, "series")
            dst_rel = (f"Shows/{show_folder}/Season {ytconfig.SEASON:02d}/"
                       f"{_episode_filename(show_folder, ytconfig.SEASON, ep, title)}")
            if _exists_in_library(dst_rel):
                raise IdentifyError(
                    f"episode number collision: {dst_rel} already exists. The show's "
                    f"numbering has drifted from the ledger; it re-syncs from disk on "
                    f"the next cycle.")
            files.append({"src": str(src), "dst_rel": dst_rel,
                          "season": ytconfig.SEASON, "episode": ep,
                          "episode_title": title, "plot": plot})
            per_video[vid] = {"route": "series", "dst_rel": dst_rel, "episode": ep}
            used_tops.add("Shows")
            ep += 1
            continue

        if route == "existing_show":
            show_rel = _need(v, "show_rel", vid, "existing_show").strip("/")
            if not show_rel.startswith("Shows/") or len(Path(show_rel).parts) != 2:
                raise IdentifyError(f"video {vid}: show_rel must be `Shows/<folder>`, "
                                    f"got {show_rel!r}")
            if not _exists_in_library(show_rel):
                raise IdentifyError(f"video {vid}: show_rel {show_rel!r} is not in the "
                                    f"library; route it as `series` instead")
            try:
                season, episode = int(v.get("season")), int(v.get("episode"))
            except (TypeError, ValueError):
                raise IdentifyError(f"video {vid} routed as existing_show without a "
                                    f"usable season/episode")
            if season < 1:
                raise IdentifyError(f"video {vid}: season must be 1 or higher "
                                    f"(specials are not routed from YouTube)")
            title = _need(v, "episode_title", vid, "existing_show")
            plot = _need(v, "plot", vid, "existing_show")
            folder = Path(show_rel).name
            dst_rel = (f"{show_rel}/Season {season:02d}/"
                       f"{_episode_filename(folder, season, episode, title)}")
            if _exists_in_library(dst_rel):
                raise IdentifyError(
                    f"video {vid}: S{season:02d}E{episode:02d} of {folder} is already "
                    f"on disk. The library is write-once, so this is refused rather "
                    f"than overwritten -- pick a free number.")
            files.append({"src": str(src), "dst_rel": dst_rel,
                          "season": season, "episode": episode,
                          "episode_title": title, "plot": plot})
            per_video[vid] = {"route": "existing_show", "dst_rel": dst_rel}
            used_tops.add("Shows")
            continue

        if route == "movie":
            mtitle = _need(v, "movie_title", vid, "movie")
            plot = _need(v, "plot", vid, "movie")
            try:
                myear = int(v.get("movie_year"))
            except (TypeError, ValueError):
                raise IdentifyError(f"video {vid} routed as movie without a usable "
                                    f"movie_year")
            stem = f"{classify.sanitize(mtitle)} ({myear})"
            dst_rel = f"Movies/{stem}.{ytconfig.CONTAINER}"
            if _exists_in_library(dst_rel):
                raise IdentifyError(f"video {vid}: {stem} is already in Movies/; the "
                                    f"library is write-once, so this is refused")
            files.append({
                "src": str(src), "dst_rel": dst_rel,
                # `owned` per file: this film is on no provider, so validate_plan takes
                # the locked-metadata path instead of demanding a TMDB id.
                "owned": True,
                "movie_title": mtitle, "plot": plot, "year": myear,
                "studio": str(v.get("studio") or "").strip() or None,
                "genres": [str(g) for g in (v.get("genres") or []) if str(g).strip()],
            })
            per_video[vid] = {"route": "movie", "dst_rel": dst_rel}
            used_tops.add("Movies")
            continue

        raise IdentifyError(f"video {vid}: unknown route {route!r}")

    if not files:
        # Every video was skipped. A legitimate outcome, not a failure -- the caller
        # records the skips and frees the downloads.
        return {}, per_video, None

    media_type = ("mixed" if len(used_tops) > 1
                  else {"Shows": "show", "Movies": "movie"}[next(iter(used_tops))])
    plan = {
        "media_type": media_type,
        # Plan-wide `owned`: nothing here is on a metadata provider, so every episode
        # gets a LOCKED .nfo carrying the metadata written above. This is the same flag
        # One Pace uses, for the same reason.
        "owned": True,
        "title": series_title or "",
        "files": files,
        "reasoning": str(routing.get("reasoning") or "")[:2000],
    }
    if series_year:
        plan["year"] = series_year

    series = None
    if show_folder and any(f["dst_rel"].startswith(f"Shows/{show_folder}/") for f in files):
        series = {
            "show_rel": f"Shows/{show_folder}",
            "title": series_title,
            "year": series_year,
            "plot": str((series_meta or {}).get("plot") or "").strip(),
            "studio": str((series_meta or {}).get("studio") or "").strip(),
            "genres": [str(g) for g in ((series_meta or {}).get("genres") or [])],
            "last_episode": ep - 1,
        }
    return plan, per_video, series
