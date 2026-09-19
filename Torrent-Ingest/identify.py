"""The identify step: hand a finished download to a headless AI run and get
back a validated placement plan.

This is the piece that does the judgment guessit/TMDB can't: absolute-vs-seasoned
numbering, movie-vs-special, interleaved specials, and matching a show that
already exists on disk. The run inspects the files and the existing library, then
writes a JSON plan to a known path. The engine (not the model) later applies and
verifies that plan and does the irreversible delete — the model only proposes.

That split is what makes the model behind `ai_runner` an implementation detail:
`library.validate_plan` re-derives every destination, confines it to the media root,
and rejects a plan that would lock a blank title, whatever wrote it.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import config
import library

# Load the searcher's stdlib-only `librarydb` by file path, WITHOUT inserting the
# searcher's directory onto sys.path. The old `sys.path.insert(0, searcher)` shadowed
# this repo's own `ingest`/`config` modules for every later import in the process, so
# `direct_ingest.py`'s `import ingest` resolved to the SEARCHER's `ingest.py` and crashed
# the daemon at start (`module 'config' has no attribute 'DISCOVERY_MAX_TOKENS'`).
import importlib.util as _ilu

_librarydb_spec = _ilu.spec_from_file_location(
    "librarydb", str(config.PROJECT_ROOT / "librarybrain" / "librarydb.py"))
librarydb = _ilu.module_from_spec(_librarydb_spec)
sys.modules["librarydb"] = librarydb
_librarydb_spec.loader.exec_module(librarydb)


class IdentifyUnavailable(RuntimeError):
    """The identify step could not RUN: the API has no credential, or no balance.

    Deliberately its own type: every caller wraps run_identify in a broad `except Exception`
    that treats a failure as the content's fault and quarantines it (a comic into
    GetComics/.failed/, a torrent into failed/ via _fail(), abandoning a finished
    download; a chunked file's only copy freed UNFILED). Nothing about the content is at
    fault here, and the identical input succeeds once the CLI can run -- so callers catch
    this FIRST and defer, leaving the work exactly where it was for the next pass.
    """


def _is_unavailable(detail: str) -> bool:
    """Whether the run could not HAPPEN -- no credential or no balance -- not a bad plan.

    Shared with the YouTube ingest's identify, which must classify identically.
    """
    return config.identify_unavailable(detail)


def _is_transient(detail: str) -> bool:
    """Whether an identify failure looks like a transient network/server blip mid
    stream (retryable) rather than a deterministic bad plan (not retryable)."""
    low = (detail or "").lower()
    return any(sig in low for sig in config.IDENTIFY_TRANSIENT_SIGNATURES)


def _stored_plan_is_incoherent(stored_plan):
    """Whether a stored plan's own season/episode map is internally contradictory.

    The same chained-run test `library._reject_absolute_run_split` applies to a finished
    plan: if season N+1's first episode is season N's last plus one, over and over, the
    mapping is one absolute run cut into season folders.
    """
    files = (stored_plan or {}).get("files") or []
    seasons = {}
    for f in files:
        if not isinstance(f, dict) or f.get("type") != "episode":
            continue
        try:
            sn, ep = int(f.get("season")), int(f.get("number"))
        except (TypeError, ValueError):
            continue
        if sn > 0:
            seasons.setdefault(sn, set()).add(ep)
    order = sorted(seasons)
    if len(order) < 3:
        return False
    chained = sum(1 for a, b in zip(order, order[1:])
                  if seasons[a] and seasons[b]
                  and min(seasons[b]) == max(seasons[a]) + 1)
    return chained >= 2


def _settled_block(stored_plan):
    """Render a stored searcher plan (§ issues.txt 6.4) as guidance for the run.

    WHAT THIS USED TO DO, AND WHY IT WAS DANGEROUS. It rendered the stored mapping under
    the instruction "reuse this mapping by filename; do NOT re-derive the season/episode
    numbering". That was sound while a live searcher produced these maps and the fleet
    wanted one numbering decision made once. It is not sound now:

      * The searcher was REMOVED on 2026-09-10. Nothing produces these maps any more, so
        every stored plan is a historical artifact nothing has re-checked.
      * They can be wrong, and one demonstrably is. `[MTBB] Monogatari Series (BD 1080p)`
        has a stored plan plotting its five "Second Season" arcs into seasons 4,5,7,8,9
        while KEEPING the filenames' absolute numbers (season 5 starting at episode 6,
        season 7 at 10, season 8 at 14, season 9 at 18) and stranding episode 22 alone in
        season 10. That is exactly the layout the harness now rejects -- and exactly what
        was filed, because the prompt told the model not to re-derive it.

    So a stored plan is now offered as EVIDENCE, not as an instruction, and one whose own
    numbering fails the coherence test is not offered at all. Getting this backwards meant
    the most authoritative-sounding line in a 90,000-character prompt was the wrong one.
    """
    if not stored_plan or not isinstance(stored_plan, dict):
        return ""
    files = stored_plan.get("files") or []
    if not files:
        return ""
    if _stored_plan_is_incoherent(stored_plan):
        return (
            "NOTE: a stored file->item mapping exists for this torrent from the retired "
            "searcher, but its own season/episode numbering is internally inconsistent (it "
            "keeps one absolute run going across several seasons), so it has been withheld "
            "rather than shown to you. Derive the numbering yourself from the release "
            "structure and the existing library.\n\n")
    lines = []
    for f in files:
        if not isinstance(f, dict):
            continue
        src = f.get("src") or "?"
        t = f.get("type") or "other"
        if t == "episode":
            item = f"S{f.get('season')}E{f.get('number')}"
        elif t in ("volume", "chapter"):
            item = f"{t} {f.get('number')}"
        elif t == "movie":
            item = "movie"
        elif t == "delete":
            item = "delete (extra: NCOP/NCED/OP/ED/menu/sample)"
        else:
            item = "other"
        lines.append(f"  {src}  ->  {item}")
    return (
        "A stored file->item mapping exists for this torrent, made by the searcher that "
        "was retired on 2026-09-10. Treat it as EVIDENCE, not as an instruction: nothing "
        "has re-checked it, and at least one stored mapping was wrong in exactly the way "
        "the harness now rejects. Prefer it where it agrees with the release structure and "
        "the existing library, and override it where it does not:\n"
        + "\n".join(lines)
        + "\nFiles marked `delete` are extras with no library home -- leave them out of the "
          "plan.\n\n"
    )


def load_stored_plan(info_hash):
    """Load the searcher's per-infohash file→item map from the shared library DB, or None."""
    if not info_hash:
        return None
    try:
        conn = librarydb.connect()
    except Exception:                                    # noqa: BLE001
        return None
    try:
        return librarydb.load_torrent_plan(conn, info_hash)
    except Exception:                                    # noqa: BLE001
        return None
    finally:
        try:
            conn.close()
        except Exception:                                # noqa: BLE001
            pass


def fast_path_plan(info_hash, content_path, stored_plan):
    """Deterministic placement plan (§ diagnosis 6.4), or None to fall back to the AI.

    The searcher's stored file→item map, when complete and unambiguous, is enough to
    derive the destination for the common case (show/volume/chapter, not owned, not a
    movie) without any model call. Returns a plan dict the caller still validates; None
    when the fast-path cannot safely apply.
    """
    import fastpath
    try:
        return fastpath.build_plan(info_hash, content_path, stored_plan)
    except Exception:                                    # noqa: BLE001
        # A fast-path miss is free (the AI still runs); a fast-path crash is not worth a
        # failed torrent. Never let it escape.
        return None


def settled_ok(stored_plan):
    """Whether a stored plan lets the AI run TIGHT: few turns, no web tools, a scoped
    digest (§ diagnosis 6.3.3).

    Only comics/novels qualify. Their volume/chapter numbering is deterministic (the
    searcher's `parse.manga_kind`/`lightnovel_kind`, not an AI title-match), so the stored
    map genuinely settles placement and the run needs no web. Shows keep the FULL
    web-enabled run whenever the fast-path cannot fire, because their numbering judgment
    (absolute vs seasoned, wrong-series/spin-off detection) is exactly what the web
    lookups exist to resolve — re-running Test B against the journal showed the item_map
    is sometimes wrong about show numbering even when the library has titles, so a
    tight, web-less run would reproduce those mis-placements.
    """
    if not stored_plan or not isinstance(stored_plan, dict):
        return False
    kind = (stored_plan.get("kind") or "").lower()
    return kind in ("manga", "comic", "lightnovel")


# How many files a rejected plan may carry before the feedback block summarises it instead
# of quoting it. A 103-file plan is ~20,000 characters of JSON, and echoing it back to the
# NEXT provider spends a fifth of a daily budget restating what the model already wrote --
# on the one call that most needs room to think. The rejection is almost always about the
# SHAPE of the numbering, and the shape is what the summary keeps.
_FEEDBACK_FULL_PLAN_MAX_FILES = 25


def _plan_for_feedback(plan):
    """The rejected plan, quoted in full when small and summarised when large.

    The summary keeps exactly what a rejection is about: which show, which seasons, and
    the episode range filed into each. That is enough for the next model to see what the
    previous one did wrong -- "S04=1-5, S05=6-9, S07=10-13" makes a split absolute run
    obvious at a glance, where 103 JSON objects hide it.
    """
    files = (plan or {}).get("files") or []
    if len(files) <= _FEEDBACK_FULL_PLAN_MAX_FILES:
        return json.dumps(plan, indent=2, ensure_ascii=False, default=str)

    per = {}
    for f in files:
        rel = str(f.get("dst_rel") or "")
        parts = rel.split("/")
        if len(parts) < 2:
            continue
        m = re.search(r"S(\d{1,3})E(\d{1,4})", parts[-1])
        if not m:
            per.setdefault((parts[0], parts[1]), {}).setdefault(None, []).append(0)
            continue
        per.setdefault((parts[0], parts[1]), {}).setdefault(int(m.group(1)), []).append(
            int(m.group(2)))

    out = [f"  (summarised -- {len(files)} files, too many to quote; this is the SHAPE "
           f"of what was rejected)"]
    for (top, folder), seasons in sorted(per.items()):
        out.append(f"  {top}/{folder}")
        for season in sorted(k for k in seasons if k is not None):
            eps = sorted(seasons[season])
            gaps = [e for e in range(eps[0], eps[-1] + 1) if e not in set(eps)]
            line = (f"    Season {season:02d}: {len(eps)} file(s), "
                    f"episodes {eps[0]:02d}-{eps[-1]:02d}")
            if gaps:
                line += f"  MISSING {','.join(str(g) for g in gaps[:12])}"
            out.append(line)
        if None in seasons:
            out.append(f"    (+{len(seasons[None])} file(s) with no SxxExx in the name)")
    meta = {k: v for k, v in (plan or {}).items() if k != "files"}
    if meta:
        out.append("  plan metadata: "
                   + json.dumps(meta, ensure_ascii=False, default=str)[:600])
    return "\n".join(out)


# Rejections that OUTLIVE one walk of the provider chain.
#
# WHY THIS EXISTS. A rejected plan plus the harness's reason is the single most valuable
# thing a failed attempt produces -- `_failure_context_block` turns the chain into a
# progressive repair rather than a blind retry, and it explicitly tells the next model to
# "keep everything that was already right". But the list holding them was a LOCAL in
# `run_identify`, so it died the moment the chain ran out of providers. The next cycle,
# fifteen minutes later, began knowing nothing.
#
# That is how the same mistake gets made three times. Monogatari was filed with mixed arcs
# on three separate runs, and no run was ever told what the previous one got wrong.
#
# The loss is bigger than it first looks, because `validate_plan` rejects WHOLESALE: there
# are 41 `raise PlanError` sites and the first one to fire discards the entire plan. A plan
# that placed 31 of 32 files correctly and got one wrong is thrown away in full. Persisting
# the rejection does not re-apply any of it -- applying a plan the harness distrusts is
# exactly how a bad placement becomes the premise for the next one -- but it does let the
# next attempt start from "here is what was right, here is the one thing that was not".
#
# BOUNDED THREE WAYS, because a stale rejection is worse than none:
#   * a TTL, because a rejection is a fact about a plan judged by a SPECIFIC set of guards,
#     and guards change (two were added on 2026-09-12). An old reason may describe
#     something that is no longer wrong, or miss what now is.
#   * a cap on how many are kept, because each one costs prompt characters against
#     providers with hard ceilings.
#   * deduplicated by reason, because the same error from three providers is one lesson.
REJECTIONS_FILE = config.STATE_DIR / "identify_rejections.json"
REJECTION_TTL_SEC = int(os.environ.get("IDENTIFY_REJECTION_TTL_SEC", str(3 * 24 * 3600)))
REJECTIONS_KEPT = int(os.environ.get("IDENTIFY_REJECTIONS_KEPT", "2"))


def _load_rejections(wave_id, now=None):
    """Rejections recorded for this wave on earlier cycles, still inside their TTL."""
    now = time.time() if now is None else now
    try:
        raw = json.loads(REJECTIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for rec in (raw.get(wave_id) or []):
        if not isinstance(rec, dict) or not rec.get("error"):
            continue
        if now - float(rec.get("at", 0)) < REJECTION_TTL_SEC:
            out.append(rec)
    return out[-REJECTIONS_KEPT:]


def _save_rejections(wave_id, rejections, now=None):
    """Persist this walk's rejections so the NEXT cycle inherits them. Best-effort."""
    now = time.time() if now is None else now
    try:
        raw = json.loads(REJECTIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    seen, keep = set(), []
    for r in list(raw.get(wave_id) or []) + list(rejections or []):
        err = str(r.get("error") or "")[:600]
        if not err or err in seen:
            continue                      # one lesson per distinct reason
        seen.add(err)
        keep.append({"provider": r.get("provider"), "model": r.get("model"),
                     "error": err, "plan": r.get("plan"),
                     "at": float(r.get("at") or now)})
    raw[wave_id] = keep[-REJECTIONS_KEPT:]
    try:
        REJECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = REJECTIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw), encoding="utf-8")
        tmp.replace(REJECTIONS_FILE)
    except OSError:
        pass


def _clear_rejections(wave_id):
    """Forget this wave's rejections -- it finally produced an accepted plan."""
    try:
        raw = json.loads(REJECTIONS_FILE.read_text(encoding="utf-8"))
        if wave_id in raw:
            del raw[wave_id]
            tmp = REJECTIONS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw), encoding="utf-8")
            tmp.replace(REJECTIONS_FILE)
    except (OSError, ValueError):
        pass


def _failure_context_block(rejections):
    """The "a previous model tried and here's exactly how it failed — fix it" block that
    turns the free-provider chain into a progressive repair, not a blind re-try (§6.5)."""
    if not rejections:
        return ""
    parts = ["A previous model already produced a placement plan for this SAME torrent, "
             "and the harness REJECTED it. Fix the specific problem(s) below — do not "
             "repeat them. Re-derive only what you must; keep everything that was already "
             "right."]
    for i, r in enumerate(rejections, 1):
        parts.append(f"\n--- Previous attempt {i} ({r['provider']}/{r['model']}) ---")
        parts.append(f"REJECTION REASON: {r['error']}")
        if r.get("plan"):
            parts.append("That attempt's (rejected) plan:")
            parts.append(_plan_for_feedback(r["plan"]))
    return "\n" + "\n".join(parts) + "\n"



# Base-prompt sections that only ever govern ONE media kind. `_relevant_sections` already
# scopes the library DIGEST to the kinds a download actually holds; these are the parts of
# the engineered prompt that can be scoped by exactly the same signal. A comics-only
# download cannot be One Pace, and a video-only download has no .epub to route to Google
# Drive, so those rules are pure tokens on the way to a provider with a daily budget.
#
# Measured: identify.md is 46,155 chars, of which One Pace is 3,705 (8%) and the light-novel
# routing is 2,038 (4%). It is a modest saving and it is the only SAFE one -- the file has
# zero verbatim repetition, so every other reduction means deleting a rule.
#
# The mapping is section-heading -> the kinds that need it. A section not listed here is
# always sent.
_KIND_SCOPED_SECTIONS = {
    "## Light novels / e-books": {"novels"},
    "## One Pace": {"shows"},
}


# Sections that are about ONE NAMED SHOW and are dead weight for every other drop. Scoped
# by the release title rather than by media kind, because "this is a show" does not narrow
# them at all -- the One Pace section is 4,921 characters and matters only when the drop IS
# One Pace, so every other show pack has been carrying it.
#
# The bar for putting a section here is deliberately high: it must be USELESS unless the
# title matches, not merely unlikely to apply. A rule that could bear on an unrelated drop
# stays in the always-sent core.
_TITLE_SCOPED_SECTIONS = {
    "## One Pace": ("one pace", "onepace"),
}


def _scope_base_prompt(base: str, sections, title_hint: str = "") -> str:
    """Drop base-prompt sections this download cannot use.

    `sections is None` means `_relevant_sections` could not tell what the download holds,
    and the answer to "I cannot verify this" is never to drop rules (§ diagnosis 4.4) --
    the full prompt goes. Same for an unrecognised heading: silence, not omission.

    `title_hint` additionally drops sections about one named show when the release is
    plainly not that show. An EMPTY hint drops nothing, for the same reason: not knowing
    the title is not evidence about it.
    """
    low = (title_hint or "").lower()
    title_drop = set()
    if low:
        for head, needles in _TITLE_SCOPED_SECTIONS.items():
            if not any(n in low for n in needles):
                title_drop.add(head)
    if not sections and not title_drop:
        return base
    sections = sections or ()
    keep, dropping = [], None
    for line in base.splitlines(keepends=True):
        if line.startswith("## "):
            dropping = None
            for head in title_drop:
                if line.startswith(head):
                    dropping = True
                    break
            if dropping is None and sections:
                for head, needed in _KIND_SCOPED_SECTIONS.items():
                    if line.startswith(head):
                        dropping = not (needed & set(sections))
                        break
        elif line.startswith("# "):
            dropping = None
        if not dropping:
            keep.append(line)
    return "".join(keep)


# The narrowest digest scoping a real identify call can use. Scoping is the only shrink
# available (the base prompt has no repetition, so deleting from it deletes rules), which
# makes this the true floor: no provider is ever offered a smaller prompt than this.
FLOOR_SECTIONS = ("comics", "novels")


def prompt_chars(sections=None):
    """Size of a real identify prompt with an empty file listing, at this scoping.

    A property of the prompt file and the library digest, not of any one torrent, so it is
    measured from an empty listing -- a real prompt is always larger.
    """
    return len(_runtime_prompt("/x", "", Path("/x"), sections=sections))


def confirm_prompt_chars():
    """Size of a CONFIRM-mode prompt with an empty file listing — the short path's floor.

    Measured the same way as `prompt_chars`: from an empty listing and no release, so a
    real prompt is always larger. It is the number that answers "could a provider this
    small file ANYTHING?", which for groq is the difference between a day's worth of runs
    and none at all.
    """
    import arcmap
    empty = arcmap.Proposal([], {}, [], False, {})
    # A title that matches no show on disk, so the scoped digest is at its smallest. An
    # empty title would fall through to the WHOLE-library digest and measure the wrong
    # thing entirely -- 47,152 chars instead of 12,000, which reads as "groq still cannot
    # serve this" about a prompt groq serves fine.
    return len(_confirm_prompt("/x", "", Path("/x"), empty,
                               "\x00no such show\x00", release_files=None))


def stamp_identify_floor():
    """Measure the SMALLEST prompt identify can build and record it for other processes.

    A provider that cannot take this can take nothing, which is what makes it the right
    number to compare a stated ceiling against -- and why it must be the NARROWED size
    (58,513 chars measured 2026-09-07), not the typical full-digest one (89,220). Stamping
    the larger number would read as "incapable" for any provider whose ceiling falls
    between the two, reserving budget against a provider that could in fact have filed.

    Written to `config.IDENTIFY_FLOOR_FILE` because the processes that need it cannot get
    it any other way: `config` cannot import this module (circular) and the brain modules
    must not import it at all (both repos ship a colliding `config`).
    """
    chars = prompt_chars(FLOOR_SECTIONS)
    config.save_identify_floor(chars)
    return chars


# --- release structure: what the harness can work out WITHOUT asking a model ------
#
# Monogatari, 2026-09-10. The free chain filed `[MTBB] Monogatari Series (BD 1080p)` with
# one 26-episode arc spread over six season folders as absolute episodes 1-23, leaving
# Season 09 holding 18,19,20,21,23 and Season 10 holding only 22. Every file had a correct
# title and plot; the PLAN was incoherent.
#
# The cause is a genuinely hard, genuinely detectable conflict: the release splits one
# broadcast season into named ARC folders while the filenames inside number the episodes
# absolutely ACROSS those folders. The model has to notice that from 103 paths, in the
# middle of a 90,000-character prompt, and it did not.
#
# It does not have to. Which folders share a filename label, and whether their numbers form
# one continuous run, is arithmetic -- so the harness does it and states the finding as a
# fact. That is the division of labour this pipeline is built on (§4.4): compute what is
# computable, and spend the model on the judgement that is left, which here is "one season
# or a season per arc?" -- a real call with two defensible answers.

_REL_EP = re.compile(
    r"^(?:\[[^\]]*\]\s*)?"          # optional [group] tag
    r"(?P<label>.+?)"                 # the series label the release uses
    r"\s*-\s*"
    r"(?P<num>\d{1,4})"               # the episode number
    r"(?:v\d+)?"                      # a version suffix (01v2)
    r"\s*(?:\[[^\]]*\]\s*)*$"        # trailing [crc] tags
)


def _parse_release_name(stem):
    """(label, number) a release filename advertises, or None."""
    m = _REL_EP.match(stem.strip())
    if not m:
        return None
    label = m.group("label").strip(" -_.")
    if not label:
        return None
    return label, int(m.group("num"))


# --- serial-numbered releases: the numbering is arithmetic ----------------------
#
# Doctor Who (1963) classic pack, 2026-09-15. The release names its files
# `S01E05 (005) - The Keys of Marinus (1) - …` where `S01E05` is the SERIAL number
# (season 1, story 5) and the part is in the `(1)`. Every part of a story therefore
# advertises the SAME season+episode, and a model that trusts the filename files all
# six parts at `S01E05`. The repair moved the parts to the accumulated broadcast
# slots, and the re-fetch waves then re-filed them by serial number AGAIN -- twice,
# because nothing in the harness could tell that the release's numbering is serial.
#
# It can. `SxxEyy (NNN)`, a folder that says `Parts 1-N`, and the order of the serial
# numbers make the broadcast number pure arithmetic: episode = sum(parts of earlier
# stories in the season) + part. The map below is that arithmetic, computed from the
# release's own file list; `_serial_numbering_block` states it to the model and
# `library.validate_plan` rejects a plan that contradicts it. This is the arcmap rule
# (§5 of the handoff): if a step is arithmetic, do the arithmetic.

_SERIAL_FILE = re.compile(
    r"S(?P<season>\d{1,2})E(?P<serial>\d{1,3})\s*\((?P<num>\d{1,4})\)"
    r"(?P<rest>.*?)(?P<ext>\.[A-Za-z0-9]{2,5})$")
_SERIAL_FOLDER = re.compile(
    r"S(?P<season>\d{1,2})E(?P<serial>\d{1,3})\s*\((?P<num>\d{1,4})\)\s*-\s*"
    r"(?P<story>.+?)\s*-\s*Parts (?P<start>\d{1,3})-(?P<end>\d{1,3})")


def serialize_parse(dirname, basename):
    """(folder_key, part, start, end, story) for one release file, or None.

    `folder_key` is `(release_season, folder_serial)` -- the FOLDER is the story, and its
    `Parts N-M` range says which episode numbers it holds. The file's own `SxxEyy` is
    deliberately not trusted for the episode: a story split across folders or a season
    packed into one folder (the 14-part Trial of a Time Lord) carries file serials that do
    not match the folder's, and the part number plus the folder's range is enough.
    Bonus files, Intros/Outros and summary clips return None: they are not episodes and
    no arithmetic maps them.
    """
    if " Bonus - " in basename:
        return None
    if re.search(r"\bintro\b|\boutro\b", basename, re.IGNORECASE):
        # A clip labeled with a part number but named Intro/Outro (`(3) - Intro for E3`)
        # is still not the episode; claiming it would put an extra in an episode's slot.
        return None
    fm = _SERIAL_FOLDER.search(dirname)
    if not fm:
        return None
    bm = _SERIAL_FILE.search(basename)
    if not bm:
        return None
    if fm.group("season") != bm.group("season"):
        return None                       # folder and file are different seasons: no
    pm = re.search(r"\((\d{1,2})\)(?=[\s.)]|$)", bm.group("rest"))
    if not pm:
        return None                       # Intro/Outro/summary shapes are not parts
    part = int(pm.group(1))
    start, end = int(fm.group("start")), int(fm.group("end"))
    if not (start <= part <= end):
        return None
    return ((int(fm.group("season")), int(fm.group("serial"))),
            part, start, end, fm.group("story").strip())


def serial_release_map(release_files, content_root=None):
    """`{(parent_folder, basename): {"season", "episode", "story", "part"}}` or {}.

    `release_files` are release-relative paths (torrent names or a content walk). Empty
    for any release that is not serial-numbered.

    The arithmetic: within each release season, folders are ordered by their serial
    number and their part RANGES accumulate; a file's episode is its folder's offset plus
    `part - start + 1`. A story split across folders (`Parts 5-8`) or a season packed
    into one folder (`Parts 1-14`) both land correctly because the range carries where it
    starts.
    """
    parsed = {}
    for rel in release_files or ():
        rel_s = str(rel).replace("\\", "/")
        parts_ = rel_s.split("/")
        if len(parts_) < 2:
            continue
        dirname, basename = parts_[-2], parts_[-1]
        got = serialize_parse(dirname, basename)
        if got:
            parsed[(dirname, basename)] = got
    if not parsed:
        return {}

    # Folder spans per release season, then the running offset per folder.
    folder_span = {}
    for _key, (folder_key, _part, start, end, _story) in parsed.items():
        folder_span[folder_key] = (start, end)
    offsets = {}
    for season in {s for s, _ in folder_span}:
        off = 0
        for serial in sorted(ser for se, ser in folder_span if se == season):
            offsets[(season, serial)] = off
            start, end = folder_span[(season, serial)]
            off += (end - start + 1)

    out = {}
    for key, (folder_key, part, start, _end, story) in parsed.items():
        out[key] = {"season": folder_key[0],
                    "episode": offsets[folder_key] + (part - start) + 1,
                    "story": story, "part": part}
    return out


def serial_release_map_for_content(content_path):
    """The serial map from a content-root walk, or {} (direct/whole-torrent drops)."""
    from pathlib import Path as _P
    root = _P(content_path)
    if not root.is_dir():
        return {}
    rels = []
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in config.MEDIA_EXTENSIONS:
            try:
                rels.append(str(f.relative_to(root)))
            except ValueError:
                continue
    return serial_release_map(rels, content_root=root)


def serial_numbering_block(content_root, release_files=None, wave_names=None):
    """The computed serial->broadcast numbering, stated to the model as fact.

    Only the wave's files are printed when `wave_names` is given (a chunked wave is <=
    ~32 files; the whole release can be 1,000+ and its other stories' numbers are noise
    on this call). Returns "" when the release is not serial-numbered.
    """
    if release_files:
        rels = [str(r).replace("\\", "/") for r in release_files]
        smap = serial_release_map(rels)
    else:
        rels, smap = [], serial_release_map_for_content(content_root)
    if not smap:
        return ""
    wanted = set()
    if wave_names:
        wanted = {str(n).replace("\\", "/").split("/")[-1] for n in wave_names}
    rows = []
    for (dirname, basename), exp in sorted(smap.items(),
                                           key=lambda kv: (kv[1]["season"], kv[1]["episode"])):
        if wanted and basename not in wanted:
            continue
        rows.append(f"  {basename[:60]:62s} =>  Season {exp['season']:02d}, "
                    f"Episode {exp['episode']:02d}   ({exp['story']}, part {exp['part']})")
    if not rows:
        return ""
    return ("======================================================================\n"
            "SERIAL-NUMBERED RELEASE -- COMPUTED BROADCAST NUMBERING\n"
            "======================================================================\n"
            "This release's `SxxEyy` is its SERIAL number, not the episode: every part of\n"
            "a story repeats the same `SxxEyy`, and the part is in the `(N)` after the\n"
            "story name. The harness accumulated the parts per season, so the numbers\n"
            "below are what each file IS. File each file at the computed slot; do NOT\n"
            "copy `SxxEyy` onto the destination.\n\n"
            + "\n".join(rows) + "\n")


def _release_structure_block(content_path, release_files=None):
    """A factual summary of how the release is laid out, plus any split/numbering conflict.

    `release_files` is the WHOLE release's relative paths when the caller knows them --
    which for a chunked pack matters enormously. A chunked torrent is identified one WAVE
    at a time and only the wave's files exist on disk, so walking the directory shows the
    model a fifth of the release and asks it to make numbering decisions about the rest.
    Monogatari's first wave is 32 of 103 files and contains three of the five folders whose
    numbering conflicts, so the conflict is INVISIBLE from disk and perfectly visible from
    the torrent's file list. Passing it is the difference between the model guessing at the
    shape of a pack and being told it.

    Returns "" when the release has no top-level folder structure worth describing -- a
    single-folder or single-file drop tells the model nothing it cannot already see.
    """
    from pathlib import Path as _P

    folders = {}
    if release_files:
        # qBittorrent names a chunked torrent's files from the TORRENT ROOT, while the
        # directory walk below yields paths relative to the content root. Both arrive here
        # as "the release's relative paths", and reading `parts[0]` as the arc folder made
        # every file in a chunked pack share one folder -- so this block returned "" for
        # exactly the chunked multi-arc packs it was written for. See
        # `arcmap.strip_release_root`.
        import arcmap
        for rel_s in sorted(arcmap.strip_release_root(release_files)):
            rel = _P(rel_s)
            if rel.suffix.lower() not in config.MEDIA_EXTENSIONS:
                continue
            top = rel.parts[0] if len(rel.parts) > 1 else "(root)"
            folders.setdefault(top, []).append(rel.stem)
    else:
        root = _P(content_path)
        if not root.is_dir():
            return ""
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in config.MEDIA_EXTENSIONS:
                continue
            try:
                rel = f.relative_to(root)
            except ValueError:
                continue
            top = rel.parts[0] if len(rel.parts) > 1 else "(root)"
            folders.setdefault(top, []).append(f.stem)

    if len(folders) < 2:
        return ""

    rows, by_label = [], {}
    for top in sorted(folders):
        parsed = [_parse_release_name(st) for st in folders[top]]
        parsed = [x for x in parsed if x]
        n = len(folders[top])
        if not parsed:
            rows.append((top, n, "", None, None))
            continue
        labels = {lab for lab, _ in parsed}
        nums = sorted(num for _, num in parsed)
        label = sorted(labels)[0] if len(labels) == 1 else f"{len(labels)} different labels"
        rows.append((top, n, label, nums[0], nums[-1]))
        if len(labels) == 1:
            by_label.setdefault(label, []).append((top, nums[0], nums[-1]))

    scope = ("the ENTIRE release (including files not yet on disk -- this is a chunked "
             "pack being filed a wave at a time, so place only what the file listing above "
             "shows, but number it consistently with the whole release described here)"
             if release_files else "the files currently on disk")
    lines = ["======================================================================",
             "RELEASE STRUCTURE (computed by the harness from the filenames -- facts,",
             "not a guess; use them, do not re-derive them)",
             "======================================================================",
             f"This describes {scope}.",
             f"{len(folders)} top-level folder(s). Episode numbers below are what the "
             f"FILENAMES say, not what you should necessarily file them as:", ""]
    for top, n, label, lo, hi in rows:
        span = f"{lo:02d}-{hi:02d}" if lo is not None else "(no episode numbers parsed)"
        lines.append(f"  {top[:42]:44s} {n:3d} file(s)  {span:>16s}  "
                     f"{('label: ' + label) if label else ''}")

    # The conflict: several folders share ONE filename label and their numbers run on.
    conflicts = []
    for label, spans in by_label.items():
        if len(spans) < 2:
            continue
        spans = sorted(spans, key=lambda t: t[1])
        chained = all(spans[i + 1][1] == spans[i][2] + 1 for i in range(len(spans) - 1))
        if chained:
            conflicts.append((label, spans))

    for label, spans in conflicts:
        lo, hi = spans[0][1], spans[-1][2]
        lines += [
            "",
            f"  [!] SPLIT/NUMBERING CONFLICT -- you must resolve this before you file.",
            f"      {len(spans)} folders all carry the filename label {label!r}, and their",
            f"      episode numbers form ONE CONTINUOUS RUN {lo:02d}-{hi:02d} across them:",
        ]
        for top, a, b in spans:
            lines.append(f"          {top[:44]:46s} {a:02d}-{b:02d}")
        lines += [
            f"      The FOLDERS say {len(spans)} separate arcs. The FILENAMES say one run of",
            f"      {hi - lo + 1}. Both cannot be true of the seasons you file.",
            "      Pick ONE and convert fully (see 'The multi-arc franchise trap'):",
            f"        (a) ONE season holding all {hi - lo + 1}, keeping the numbers {lo:02d}-{hi:02d}; or",
            "        (b) a season PER FOLDER, each RENUMBERED from 01 -- so the second",
            "            folder above becomes E01.. in its own season, NOT its filename",
            "            numbers.",
            "      Keeping the filename numbers while splitting per folder is the one",
            "      answer the harness will REJECT outright.",
        ]

    return "\n".join(lines) + "\n"


def _provider_season_block(content_path, release_files=None):
    """What the PROVIDER says this show's seasons are -- from the fleet's own guide cache.

    The fleet already holds this. `epguide.season_shape(title)` returns {season: episode
    count} and `epguide.episodes(title)` the per-episode names, cached on disk and used by
    the metadata repair. Until now identify never saw it, so every run re-derived the
    season layout by web search -- twenty-odd turns of it on Monogatari -- and still got a
    boundary wrong.

    That error is the reason this exists. Bakemonogatari -> S01 and Nisemonogatari -> S02
    were right; the four `04 - Nekomonogatari (Black)` files went to Season 03, where the
    provider actually keeps Tsubasa Tiger. The guide says S03 has 23 episodes beginning
    "Tsubasa Tiger - Part 1" -- which is exactly the 23 files this release labels
    "Monogatari Series Second Season - 01..23". Both the mistake and its answer were one
    lookup away.

    Best-effort and silent on failure: an unknown title, a provider miss or a network
    problem returns "" and the run proceeds exactly as before.
    """
    title = _release_title_guess(content_path, release_files)
    if not title:
        return ""
    try:
        import epguide
        shape = epguide.season_shape(title)
        eps = epguide.episodes(title) or []
    except Exception:                                                # noqa: BLE001
        return ""
    if not shape:
        return ""

    first = {}
    for e in eps:
        s_, n_ = e.get("season"), e.get("number")
        if s_ is None or n_ is None:
            continue
        if s_ not in first or n_ < first[s_][0]:
            first[s_] = (n_, e.get("name") or "")

    lines = [
        "======================================================================",
        f"WHAT THE PROVIDER HAS FOR {title!r} (the fleet's own episode guide --",
        "authoritative for season boundaries; do not re-derive this by web search)",
        "======================================================================",
        "Season sizes and each season's FIRST episode. Use this to decide which season an",
        "arc belongs to: match the arc to the season whose first episode is that arc's",
        "opening, and check the episode count is consistent with how many files the arc has.",
        "",
    ]
    for s_ in sorted(shape):
        n_, name = first.get(s_, (1, ""))
        lines.append(f"  Season {s_:02d}: {shape[s_]:3d} episode(s)   "
                     f"first = E{n_:02d} {name!r}")
    lines += [
        "",
        "  If an arc in this release does not correspond to any season above, it is a",
        "  special, a film, or an entry the provider carries separately -- say which in",
        "  your rationale rather than forcing it into the nearest season number.",
    ]
    return "\n".join(lines) + "\n"


def _arc_season_block(content_path, release_files=None):
    """The harness's own arc->season mapping, stated as the arithmetic it is.

    This is the step `_provider_season_block` stopped one short of. That block hands the
    run the season SIZES and asks it to marry them to the release's arcs -- and the
    marrying is the part that failed three runs in a row on Monogatari, because the
    provider names a season by its opening arc-internal title ("Tsubasa Tiger") while the
    release names its folders by the `-monogatari` arc names. There is no string between
    them to match on. There is arithmetic: see `arcmap`.

    Silent whenever the answer is not unique, which is the whole discipline -- a guess
    dressed as a computed fact would be worse than the model's own guess, because the
    prompt says to trust it.
    """
    title, proposal = _arc_proposal(content_path, release_files)
    if proposal is None:
        return ""
    try:
        import arcmap
        return arcmap.block(proposal, title)
    except Exception:                                                # noqa: BLE001
        return ""


def _ownership_block(content_path, release_files=None):
    """Which files Jellyfin's own provider cannot render, so the run owns exactly those.

    Needs the show's TMDB id, and the only place the harness reliably has one is the
    `tvshow.nfo` of a show already in the library. For a brand-new show it stays silent and
    the run behaves as it always did -- which is correct: there is nothing on disk to
    disagree with yet.
    """
    title, proposal = _arc_proposal(content_path, release_files)
    if proposal is None or not proposal.settled:
        return ""
    try:
        import arcmap
        return arcmap.ownership_block(proposal, library.existing_show_tmdb_id(title))
    except Exception:                                                # noqa: BLE001
        return ""


def _specials_metadata_block(content_path, release_files=None, wave_paths=None):
    """Titles and plots for the arcs the provider carries as specials. "" when it cannot.

    Wired into BOTH prompts. The Season-0 title+plot requirement is the same on either
    path, and so is the failure it causes when the run has to go and find them.
    """
    title, proposal = _arc_proposal(content_path, release_files)
    if proposal is None:
        return ""
    try:
        import arcmap
        import epguide
        import tmdbguide
        runs = tmdbguide.specials_runs(library.existing_show_tmdb_id(title))
        return arcmap.metadata_block(proposal, epguide.specials(title),
                                     wave_paths=wave_paths, tmdb_runs=runs)
    except Exception:                                                # noqa: BLE001
        return ""


def _listing_names(file_listing):
    """The basenames a rendered file listing mentions, for scoping a block to one wave."""
    from pathlib import Path as _P
    out = set()
    for line in (file_listing or "").splitlines():
        t = line.strip().split("  ")[0].strip()
        if t and not t.endswith("/"):
            out.add(_P(t).name)
    return out or None


def _arc_proposal(content_path, release_files=None):
    """`(series title, arcmap.Proposal or None)` for this release. Never raises."""
    title = _release_title_guess(content_path, release_files)
    if not title or not release_files:
        return title, None
    try:
        import arcmap
        import epguide
        shape = epguide.season_shape(title)
        if not shape:
            return title, None
        return title, arcmap.propose(release_files, shape)
    except Exception:                                                # noqa: BLE001
        return title, None


def _release_title_guess(content_path, release_files=None):
    """The show title a release name is advertising, stripped of group and quality tags."""
    from pathlib import Path as _P
    name = _P(content_path).name if content_path else ""
    if not name and release_files:
        name = str(release_files[0]).split("/")[0]
    if not name:
        return ""
    name = re.sub(r"\[[^\]]*\]", " ", name)          # [MTBB], [1080p]
    name = re.sub(r"\([^)]*\)", " ", name)            # (BD 1080p)
    # "Series" is NOT stripped: it is part of real titles ("Monogatari Series",
    # "Fate Series"), and the guide lookup is more forgiving of a slightly long title
    # than of one missing a word.
    name = re.sub(r"\b(?:BD|BDRip|BluRay|WEB|WEBRip|1080p|720p|2160p|4K|UHD|HEVC|x264|"
                  r"x265|AAC|FLAC|Dual[- ]?Audio|Complete|Batch)\b", " ", name,
                  flags=re.IGNORECASE)
    name = re.sub(r"[._]+", " ", name)
    return " ".join(name.split()).strip(" -") or ""


def _runtime_prompt(content_path, file_listing, plan_path, stored_plan=None,
                    series_hint=None, kind=None, failure_context=None, sections=None,
                    release_files=None):
    """The engineered base prompt plus this torrent's concrete context.

    With `series_hint`/`kind` (the settled case) the library digest is scoped to that one
    series instead of the whole library (§ diagnosis 6.3.2). With `failure_context`
    (a prior provider's rejected plan) the next provider is told exactly what to fix.
    `sections` narrows the whole-library digest to the kinds of media this download
    actually holds — the cheap shrink a provider gets offered before it is written off as
    unable to serve the prompt at all."""
    base = _scope_base_prompt(
        config.IDENTIFY_PROMPT_FILE.read_text(encoding="utf-8"), sections,
        title_hint=(Path(content_path).name if content_path else ""))
    # The release name is the relevance hint for the shows digest: full season detail
    # for the folders that could be this show, names only for the other ~300. It fails
    # open -- a hint matching nothing yields the full digest, as before.
    title_hint = Path(content_path).name if content_path else None
    digest = library.build_library_digest(series_hint, kind, sections=sections,
                                          title_hint=title_hint)
    settled = _settled_block(stored_plan)
    failure = _failure_context_block(failure_context or [])
    structure = _release_structure_block(content_path, release_files)
    serial = serial_numbering_block(content_path, release_files,
                                    wave_names=_listing_names(file_listing))
    provider = _provider_season_block(content_path, release_files)
    arcs = _arc_season_block(content_path, release_files)
    specials = _specials_metadata_block(content_path, release_files,
                                        wave_paths=_listing_names(file_listing))
    ownership = _ownership_block(content_path, release_files)
    return f"""{base}

======================================================================
RUNTIME CONTEXT FOR THIS TORRENT
======================================================================

Downloaded content is at:
{content_path}

Files in the download (relative to that path):
{file_listing}

{settled}
{structure}
{serial}
{provider}
{arcs}
{specials}
{ownership}
{digest}
{failure}
======================================================================
YOUR OUTPUT
======================================================================

Inspect the download and the existing library above (use `ListDir`, `Probe`,
`Read` on existing .nfo, and web lookups as needed). Then write your placement
plan as strict JSON to EXACTLY this path (overwrite if present):

Work efficiently — this run is time-boxed:
  * Ignore any top-level entry the listing marks "(no media — ignore)"; those are
    decoy/promo folders of unrelated shows, not part of this title.
  * When a file's season/episode is already unambiguous in its name (e.g.
    `S03E13`, `- 1x05`), trust it — do NOT `Probe` it. Reserve `Probe` for the
    genuinely ambiguous files (a movie-vs-special call, an unlabeled runtime
    check), not routine confirmation of clearly-named episodes.
  * For a MOVIE, put the film's TMDB id in the plan (top-level for a single movie,
    or per-file `tmdb_id` in a mixed/multi-movie plan) so Jellyfin pins the exact
    film and cannot mis-match it to a sibling in the same collection.
  * NEVER return an empty `files` list. If the show/movie already exists in the
    library and every downloaded file looks already-present, you MUST still list
    those files with their correct destinations — the harness detects the existing
    copy and skips it safely (already-present is a SUCCESS, not "nothing to do").
    An empty `files` list is only correct when the torrent contains no library
    media at all (pure junk/samples), which is rare; prefer listing over omitting.

{plan_path}

The JSON MUST match the schema described above. After writing the file, reply
with a short plain-English rationale for the calls you made (which show, why
that season numbering, which files you judged specials/movies and why, and
whether you flagged the show as owned). Do not move, rename, or delete any
files yourself — only inspect and write the plan.
"""


# The tools a CONFIRM-mode run may use, and what they cost. Narrower than the deriving
# run's eight, for two reasons that point the same way:
#
#   * the job is smaller. The mapping, the Season-00 numbers and the specials' titles are
#     all handed over, so what is left is checking filenames (`ListDir`), looking up a film
#     id (`WebSearch`/`WebFetch`) and the `Write`. `Read`, `Glob`, `Grep` and `Probe` have
#     nothing to do here, and every tool offered is a way for a short-budget run to spend
#     a turn not writing.
#   * the SCHEMAS travel with every request and count against the provider's per-request
#     allowance, and they are not in the prompt text. Eight schemas are 3,205 characters;
#     these four are 1,460. On a provider with ~21,900 to spend that is real money.
CONFIRM_TOOLS = "Write,ListDir,WebSearch,WebFetch"

# What every request carries besides the prompt: the tool schemas plus the runtime's own
# system prompt. Measured 2026-09-12 (1,460 + 1,348, rounded up for JSON framing). The
# ceiling a provider states is on the whole REQUEST, so a prompt built right up to it is
# refused -- which is a 413 the caller reads as "this provider cannot serve us", when in
# fact it could have served a prompt 3,000 characters shorter.
CONFIRM_REQUEST_OVERHEAD = 3_000


def _confirm_prompt(content_path, file_listing, plan_path, proposal, title,
                    release_files=None, failure_context=None, max_chars=0):
    """The SHORT prompt: confirm a mapping the harness already computed.

    WHY THIS EXISTS, and it is a capacity fix as much as a correctness one. The full
    identify prompt has a floor of 63,177 characters, almost all of it `identify.md`,
    and `identify.md` has no repetition in it -- shrinking it means deleting rules, which
    makes a free model's "confident but subtly wrong" failures worse rather than better.
    So the floor was treated as fixed, and groq -- whose measured ceiling is ~26,400
    characters but which is the only provider that is ever up when the other two have hit
    their daily caps -- was written off as permanently unable to file anything.

    That reasoning only holds while every run has to DERIVE the placement. When `arcmap`
    has already settled which arc goes in which season, the run is doing a different and
    much smaller job, and most of `identify.md` is rules about cases the mapping has
    ruled out. This prompt is that job written down honestly -- not a truncated
    `identify.md`, which is the trade §6 warns against.

    Gated hard: built ONLY from a settled proposal, and offered to a provider ONLY when
    the full prompt does not fit it. The plan it produces goes through exactly the same
    `library.validate_plan`, so a confirm-mode plan is never trusted more than any other.
    """
    base = config.CONFIRM_PROMPT_FILE.read_text(encoding="utf-8")
    import arcmap
    # Deliberately WITHOUT the release-structure and provider-season blocks. Both exist to
    # help a run derive the mapping; this run is handed the mapping, and the arc block
    # already states each season's provider episode count and which folders each arc spans.
    # Carrying them anyway costs ~3,500 characters -- about 1,300 tokens -- and on a
    # provider serving 8,000 tokens a MINUTE that is the difference between two turns a
    # minute and one.
    arcs = arcmap.block(proposal, title)
    # Scoped to the files this wave actually holds: the whole show's specials would be
    # ~9,000 characters, and a wave needs only its own.
    try:
        import epguide
        sp = epguide.specials(title)
    except Exception:                                                # noqa: BLE001
        sp = []

    def _meta(max_plot):
        try:
            import tmdbguide
            return arcmap.metadata_block(
                proposal, sp, max_plot=max_plot,
                wave_paths=_listing_names(file_listing),
                tmdb_runs=tmdbguide.specials_runs(
                    library.existing_show_tmdb_id(title)))
        except Exception:                                            # noqa: BLE001
            return ""

    meta = _meta(220)
    try:
        import arcmap as _am
        ownership = _am.ownership_block(proposal, library.existing_show_tmdb_id(title))
    except Exception:                                                # noqa: BLE001
        ownership = ""
    # The digest is scoped to this ONE show. The whole-library digest is 26.6K characters
    # of other people's shows; a run that has been told which show this is and which
    # season each arc belongs to needs exactly one folder's worth of context -- whether
    # it already exists, and what it already holds.
    digest = library.build_library_digest(title, "show")
    failure = _failure_context_block(failure_context or [])

    def _assemble():
        return f"""{base}

======================================================================
RUNTIME CONTEXT FOR THIS TORRENT
======================================================================

Downloaded content is at:
{content_path}

Files in the download (relative to that path) — these are the files you may place:
{file_listing}

{arcs}
{meta}
{ownership}
{digest}
{failure}
======================================================================
YOUR OUTPUT
======================================================================

Write the placement plan as strict JSON to EXACTLY this path (overwrite if present):

{plan_path}

Then reply with a short rationale: the mapping you confirmed, what you decided each
left-over arc is, and anything you disagreed with.
"""

    out = _assemble()
    if not max_chars or len(out) <= max_chars:
        return out
    # Over the provider's ceiling. Shorten the SYNOPSES before anything else: they are the
    # only part of this prompt with slack in it, and a shorter plot is still a real plot,
    # where dropping the block entirely sends the run back to the web search this whole
    # path exists to avoid. Titles are kept to the last.
    for plot_len in (140, 90, 0):
        meta = _meta(plot_len)
        out = _assemble()
        if len(out) <= max_chars:
            return out
    return out


# {provider: the largest prompt, in CHARACTERS, it is known to be able to serve}.
#
# WHAT THIS REPLACED, AND WHY. This used to be `{provider: skip-until-timestamp}`: one
# refusal put the provider in a 30-minute penalty box for EVERY record. The comment
# justifying that said "the prompt is the same shape for every record" -- and that is the
# bug. Prompt size is dominated by the library digest and the file listing, both of which
# vary by an order of magnitude between a 1-file comic and a 522-file season pack, so a
# provider that cannot take the big one is banned from the small ones it could serve fine.
#
# Measured 2026-09-05: Groq's free tier answers `Limit 8000, Requested 30399` and was
# consequently skipped 18 times in one day -- while it was the ONLY provider with budget
# left, and while Made in Abyss, Yu Yu Hakusho and Hunter x Hunter sat unfiled.
#
# A ceiling is exact where the old cooldown was a guess: the refusal states the limit and
# what we asked for, so the provider tells us its own boundary. A prompt under the ceiling
# is tried; one over it is skipped WITHOUT spending a request. Nothing expires, because
# nothing needs to -- a smaller prompt re-tests the provider on its own.
# Seeded from disk so a daemon restart does not re-pay a 413 to relearn what the provider
# already told us (`config.load_prompt_ceilings`, TTL'd so a tier upgrade is still noticed).
_TOO_LARGE_CEILING: dict = config.load_prompt_ceilings()


def _note_too_large(provider_name, detail, prompt_chars):
    """Record what `provider_name` just proved it cannot take, in characters.

    Prefers the provider's own numbers: `Limit 8000, Requested 30399` over a prompt of
    99,763 characters measures 3.28 chars/token for THIS prompt, so the ceiling is
    8000 * 3.28 ~= 26,200 characters. Falling back on a fixed chars-per-token estimate
    only matters for a provider that refuses without saying what it measured.
    """
    limit, requested = config.identify_token_limit(detail)
    if limit and requested:
        ceiling = int(limit * (prompt_chars / requested))
    elif limit:
        ceiling = int(limit * config.IDENTIFY_CHARS_PER_TOKEN)
    else:
        # No numbers at all: all we know is that THIS size was refused.
        ceiling = prompt_chars - 1
    prev = _TOO_LARGE_CEILING.get(provider_name)
    ceiling = min(prev, ceiling) if prev else ceiling
    _TOO_LARGE_CEILING[provider_name] = config.save_prompt_ceiling(provider_name, ceiling)
    return _TOO_LARGE_CEILING[provider_name]


# When EVERY provider reported it could not run, the next record's chain will report the
# same thing seconds later -- the condition is the fleet's free-tier budget, not this
# download. The ingest daemon has no backoff of its own on this path (direct_ingest does:
# it sleeps IDENTIFY_UNAVAILABLE_BACKOFF_SEC), so it re-entered the chain every ~27s and
# spent two failed HTTP calls per queued record each time. Measured 2026-09-05: ~130
# pointless round trips an hour while OpenRouter and Cloudflare were both out of budget
# for the day.
#
# Deliberately NOT armed by a provider being skipped for prompt SIZE: that is a fact about
# one record, and a smaller record must still be allowed to try.
_UNAVAILABLE_UNTIL = 0.0


def _budget_deferred_for():
    """Seconds still left on the fleet-wide "no provider can run" backoff, else 0."""
    return max(0.0, _UNAVAILABLE_UNTIL - time.time())


def _note_retired_model(provider_name, model, detail, note):
    """If `detail` says this model id is GONE, find and record a live stand-in. True if so.

    Kept tiny and exception-proof: model discovery must never be able to fail an ingest.
    """
    try:
        import ai_models
        if not ai_models.is_retired(detail):
            return False
        note(f"  {provider_name}/{model} is RETIRED by the provider; looking for a "
             f"live replacement")
        repl = ai_models.find_replacement(provider_name, model)
        if repl:
            note(f"  {provider_name}: {model} -> {repl} (probed, tool-calling confirmed; "
                 f"recorded in state/ai_model_overrides.json -- promote it into config.py)")
        else:
            note(f"  {provider_name}: no usable replacement found for {model}; "
                 f"this provider is short one model until config.py is updated")
        return True
    except Exception as exc:                                          # noqa: BLE001
        try:
            note(f"  model-retirement check failed harmlessly: {exc}")
        except Exception:                                             # noqa: BLE001
            pass
        return False


def _fits(provider_name, prompt):
    """Whether `prompt` is under everything `provider_name` has proved it cannot take."""
    ceiling = _TOO_LARGE_CEILING.get(provider_name)
    return ceiling is None or len(prompt) <= ceiling


# Which digest sections a download could possibly need, from the extensions it holds. A
# comic pack cannot be filed into Shows/, so the 26.6K characters of show folders are
# prompt weight it can never use -- and prompt weight is exactly what a provider refuses
# on. Anything unrecognised widens back to the full digest: guessing NARROW would hide
# library context from a run that needed it, which is the expensive direction.
def _relevant_sections(content_path):
    from pathlib import Path

    root = Path(content_path)
    paths = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    want, unknown = set(), False
    for p in paths:
        ext = p.suffix.lower()
        if ext in config.VIDEO_EXTENSIONS:
            want |= {"shows", "movies"}
        elif ext in config.COMIC_EXTENSIONS or ext in config.LOOSE_PAGE_EXTENSIONS:
            want |= {"comics", "novels"}      # a .pdf is either; keep both book sections
        elif ext in config.NOVEL_EXTENSIONS:
            want |= {"comics", "novels"}
        elif ext in config.MEDIA_EXTENSIONS:
            unknown = True
    if unknown or not want:
        return None                            # full digest: the historical behaviour
    return tuple(sorted(want))


def run_identify(info_hash, content_path, log_fn=None, stored_plan=None, settled=False,
                 sibling_seasons=None, release_files=None):
    """Invoke the headless AI run. Returns (plan_dict, rationale_text).

    `stored_plan` is the searcher's per-infohash file→item map (§ issues.txt 6.4); when
    provided it is handed to the run as settled numbering so a re-download of the same
    infohash never re-derives the mapping.

    `settled` (the deterministic fast-path's tight twin, § diagnosis 6.3.3) scopes the
    library digest to the series named by the stored plan, caps the run to
    `config.IDENTIFY_SETTLED_MAX_TURNS`, and drops the web tools — the stored map already
    settled the numbering, so the run only fills in destinations.

    `sibling_seasons` feeds the season-gap guard when a plan is validated IN-LOOP (a
    chunked wave's legitimately-absent siblings); the caller still validates again before
    apply, so this only makes the in-loop verdict match the caller's.

    The run walks a CHAIN of FREE providers (§ diagnosis 6.5). Each provider gets the same
    task; when one writes a plan the harness rejects, the rejection + that plan are
    appended to the NEXT provider's prompt as "fix exactly this" context, so the fleet
    escalates through free models instead of paying for one good one. A provider whose API
    cannot run (no key / quota / rate-limit) is skipped for the next. The final plan is
    validated before returning; on any rejection the next provider fixes it.

    Raises IdentifyUnavailable when NO provider could run at all (the caller defers the
    work); RuntimeError when providers ran but every plan was rejected (the caller treats
    that as the content's fault). `log_fn`, if given, receives the chain's progress.
    """
    def _note(msg):
        if log_fn:
            log_fn(msg)

    plan_path = config.TMP_DIR / f"{info_hash}_plan.json"
    verbose_log = config.TMP_DIR / f"{info_hash}_identify.log"
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)

    attempts = config.enabled_ai_attempts()
    if not attempts:
        raise IdentifyUnavailable(
            "no free AI provider configured: add an OpenRouter key under "
            "~/.config/api-keys/openrouter_key or set TORRENT_INGEST_AI_PROVIDERS")

    left = _budget_deferred_for()
    if left:
        # Same exception the callers already handle by DEFERRING and keeping the bytes --
        # this only stops us re-proving a fleet-wide fact once per record per cycle.
        raise IdentifyUnavailable(
            f"no provider produced a plan on the last pass; re-testing in {int(left)}s "
            f"(the pass itself logged which provider said what -- this line is the "
            f"backoff, not a diagnosis)")

    series_hint = kind = None
    if settled and isinstance(stored_plan, dict):
        series_hint = stored_plan.get("series")
        kind = (stored_plan.get("kind") or "").lower()

    file_listing, media_count = _list_files(content_path)
    timeout = _timeout_for(media_count)
    sections = _relevant_sections(content_path)
    # Computed once for the whole chain: every provider sees the same mapping, and the
    # lookup behind it is a cached provider call we should not repeat per attempt.
    arc_title, arc_proposal = _arc_proposal(content_path, release_files)
    # Serial-numbered releases (classic Doctor Who, 2026-09-15): computed once, stated to
    # every provider, and passed to validate_plan so a serial-copied destination is
    # refused rather than filed.
    serial_map = (serial_release_map(release_files) if release_files
                  else serial_release_map_for_content(content_path))

    if settled:
        tools = "Read,Write,Glob,Grep,Probe,ListDir"
        max_turns = str(config.IDENTIFY_SETTLED_MAX_TURNS)
    else:
        tools = "Read,Write,Glob,Grep,Probe,ListDir,WebSearch,WebFetch"
        max_turns = "120"

    env = config.ai_env()

    def _stderr_tail(n: int = 800) -> str:
        try:
            return verbose_log.read_text(encoding="utf-8", errors="replace")[-n:]
        except OSError:
            return ""

    # Seeded from earlier CYCLES, not just earlier providers in this walk: a chain that
    # runs out of providers used to forget everything it had learned. See REJECTIONS_FILE.
    rejections = _load_rejections(info_hash)
    if rejections:
        _note(f"identify: carrying {len(rejections)} rejection(s) from an earlier cycle "
              f"-- the next model is told what the last one got wrong")
    saw_plan = False       # any provider produced a plan (even a rejected one)?
    # Which providers reported they could not RUN (budget/credential) -- the SET, not a
    # bool. As a bool this armed a fleet-wide backoff whose message read "every free
    # provider reported no budget" when exactly one of three had said so, and the other
    # two had failed for entirely different reasons (a retired model slug answering 404,
    # and empty replies). That sentence was then read as a measurement and became this
    # project's standing conclusion that identify was out of daily budget everywhere --
    # §4.9 exactly: a log line about ONE call, promoted to a fact about the fleet. Name
    # the providers instead, so the next reader sees what was actually said and by whom.
    unavailable_providers: set = set()
    last_err = "identify failed"

    capped_providers: set = set()

    for attempt in attempts:
        provider_name = attempt["provider"]
        model = attempt["model"]
        if provider_name in capped_providers:
            continue                     # account out of budget for the day (see below)
        prompt = _runtime_prompt(content_path, file_listing, plan_path, stored_plan,
                                 series_hint=series_hint, kind=kind,
                                 failure_context=rejections,
                                 release_files=release_files)
        # Per ATTEMPT, not per run: confirm mode below narrows these for the one provider
        # that needs it, and leaking that narrowing to the next provider would cap a run
        # that has no reason to be capped.
        turns, attempt_tools = max_turns, tools
        # Offer a provider that has already refused this size the NARROWED digest before
        # writing it off. The narrow prompt drops only sections this download's own file
        # extensions prove it cannot use, so nothing the run needs is withheld.
        if not _fits(provider_name, prompt) and sections:
            compact = _runtime_prompt(content_path, file_listing, plan_path, stored_plan,
                                      series_hint=series_hint, kind=kind,
                                      failure_context=rejections, sections=sections,
                                      release_files=release_files)
            if len(compact) < len(prompt):
                _note(f"  {provider_name}: full prompt is {len(prompt)} chars, over its "
                      f"measured {_TOO_LARGE_CEILING[provider_name]}; retrying with the "
                      f"{'+'.join(sections)} digest only ({len(compact)} chars)")
                prompt = compact
        # Last offer before writing the provider off: when the harness has already
        # settled the arc->season mapping, the run is confirming an answer rather than
        # deriving one, and that job fits in a fraction of the prompt (`_confirm_prompt`).
        # This is what lets groq -- whose ~21,900-character ceiling no full identify prompt
        # can ever meet -- file a multi-arc pack at all.
        if not _fits(provider_name, prompt) and arc_proposal is not None \
                and arc_proposal.settled:
            try:
                short = _confirm_prompt(content_path, file_listing, plan_path,
                                        arc_proposal, arc_title,
                                        release_files=release_files,
                                        failure_context=rejections,
                                        max_chars=max(
                                            1000,
                                            _TOO_LARGE_CEILING[provider_name]
                                            - CONFIRM_REQUEST_OVERHEAD))
            except Exception as exc:                                  # noqa: BLE001
                short = None
                _note(f"  confirm-mode prompt could not be built: {exc}")
            if short and len(short) + CONFIRM_REQUEST_OVERHEAD <= \
                    _TOO_LARGE_CEILING[provider_name]:
                _note(f"  {provider_name}: full prompt is {len(prompt)} chars, over its "
                      f"measured {_TOO_LARGE_CEILING[provider_name]}; the arc->season "
                      f"mapping is settled, so retrying in CONFIRM mode "
                      f"({len(short)} chars, {config.IDENTIFY_CONFIRM_MAX_TURNS} turns)")
                prompt = short
                # A provider small enough to need this prompt has a small daily budget
                # too, and a wandering run spends all of it. See the constant's comment.
                turns = str(config.IDENTIFY_CONFIRM_MAX_TURNS)
                attempt_tools = CONFIRM_TOOLS
        if not _fits(provider_name, prompt):
            # Measured, not guessed: this provider stated a ceiling this prompt is over.
            # Skipping costs nothing and spends no request, and a smaller prompt on a
            # later record re-tests it automatically.
            _note(f"  {provider_name}/{model} skipped: prompt {len(prompt)} chars exceeds "
                  f"its measured ceiling of {_TOO_LARGE_CEILING[provider_name]}")
            continue
        _note(f"identify: trying {provider_name}/{model}"
              + (f" (fixing {len(rejections)} prior rejection(s))" if rejections else ""))

        cmd = [
            *config.AI_BIN,
            "-p",
            "--output-format", "json",
            "--tools", attempt_tools,
            "--max-turns", turns,
            # Tool-by-tool progress on stderr, so a wedged run is diagnosable from the
            # log instead of a black box. The agent's OWN budget is set a minute inside
            # the subprocess timeout so a plan written to disk survives a clean stop.
            "--timeout", str(max(60, timeout - 60)),
            "--verbose",
            "--require-file", str(plan_path),
            "--provider", provider_name,
            "--model", model,
        ]

        # Transient retry within THIS provider only; a validation rejection moves straight
        # to the next provider (the same model would just repeat its mistake).
        for attempt_no in range(1, config.IDENTIFY_MAX_ATTEMPTS + 1):
            if plan_path.exists():
                plan_path.unlink()
            try:
                with open(verbose_log, "w", encoding="utf-8") as stderr_f:
                    proc = subprocess.run(
                        cmd,
                        input=prompt,
                        stdout=subprocess.PIPE,
                        stderr=stderr_f,
                        text=True,
                        timeout=timeout,
                        env=env,
                        cwd=str(config.PROJECT_ROOT),
                    )
            except subprocess.TimeoutExpired:
                # A timeout already burned the full scaled budget; retrying the SAME
                # provider would just burn it again. Move to the next provider.
                last_err = (f"identify timed out after {timeout}s "
                            f"({media_count} media/loose-page units)")
                break

            rationale = _extract_rationale(proc.stdout)
            fail_detail = None

            if proc.returncode != 0:
                fail_detail = rationale or _stderr_tail()
                last_err = f"identify run exited {proc.returncode}: {fail_detail}"
                # Exit 2 IS the unavailable signal (ai_runner); the text match is the
                # fallback for a provider that phrases a quota/spend cap in prose. Either
                # way this provider cannot run right now — not the content's fault, and
                # not worth the transient budget against the same provider, so move on.
                if proc.returncode == 2 or _is_unavailable(fail_detail):
                    unavailable_providers.add(provider_name)
                    _note(f"  {provider_name}/{model} unavailable: {fail_detail[:160]}")
                    if config.identify_account_capped(fail_detail):
                        # The whole account is out of budget for the day, so this
                        # provider's OTHER models cannot serve either -- skip them and
                        # fail over to the next provider immediately.
                        capped_providers.add(provider_name)
                        # Tell the REST of the fleet too. Discovery, the completeness
                        # audit, playlist curation and media_doctor all spend the same
                        # free daily budget, and identify is the only consumer that loses
                        # content by not running -- so they stand down for a while and
                        # leave what is left to the critical path (config.ai_budget_healthy).
                        config.note_ai_account_capped(provider_name)
                    break
                # A provider that says it is BUSY is not retried -- the chain has five
                # others, and each caller-level retry is a whole fresh agent run whose
                # turns are thrown away. See config.IDENTIFY_BUSY_SIGNATURES for the
                # forty-eight-minute measurement that prompted this.
                if config.identify_provider_busy(fail_detail):
                    _note(f"  {provider_name}/{model} is BUSY (provider-side); moving to "
                          f"the next provider rather than retrying it")
                    break
                if _is_transient(fail_detail) and attempt_no < config.IDENTIFY_MAX_ATTEMPTS:
                    backoff = config.IDENTIFY_RETRY_BACKOFF_SEC * attempt_no
                    _note(f"  {provider_name}/{model} transient (attempt "
                          f"{attempt_no}/{config.IDENTIFY_MAX_ATTEMPTS}), retrying in "
                          f"{backoff}s: {fail_detail[:160]}")
                    time.sleep(backoff)
                    continue
                # An exit that is neither "unavailable" nor "transient" used to break
                # SILENTLY. Groq failed this way 98 times without printing one line: the
                # log showed "identify: trying groq/..." and then, 33 seconds later, the
                # next provider. A provider that never succeeds and never says why cannot
                # be fixed or dropped on evidence, so say it.
                _note(f"  {provider_name}/{model} failed (exit {proc.returncode}): "
                      f"{fail_detail[:200]}")
                # A model id the provider has RETIRED is neither "unavailable" (that means
                # no budget) nor "transient" (a retry would fix it), so it used to fall
                # through here and cost one wasted attempt per record, forever, until a
                # human noticed. OpenRouter retired two `:free` slugs on 2026-09-07 and the
                # fleet 404'd on every pass for days. Discover a live replacement, PROBE it
                # with a real tool call, and record it; `config.enabled_ai_attempts` picks
                # it up next cycle. Bounded and fail-soft -- see `ai_models`.
                if _note_retired_model(provider_name, model, fail_detail, _note):
                    break
                if config.identify_request_too_large(fail_detail):
                    # A per-organisation tokens-per-minute ceiling: this provider's other
                    # models share it and will refuse the same prompt, so do not pay
                    # another full timeout to be told so.
                    capped_providers.add(provider_name)
                    ceiling = _note_too_large(provider_name, fail_detail, len(prompt))
                    _note(f"  {provider_name}: refused a {len(prompt)}-char prompt; its "
                          f"measured ceiling is now {ceiling} chars")
                    # A narrower digest may still fit under the ceiling we just learned.
                    # Retry THIS provider with it rather than losing the only one with
                    # budget left over a prompt we can legitimately make smaller.
                    if sections and attempt_no < config.IDENTIFY_MAX_ATTEMPTS:
                        compact = _runtime_prompt(
                            content_path, file_listing, plan_path, stored_plan,
                            series_hint=series_hint, kind=kind,
                            failure_context=rejections, sections=sections,
                            release_files=release_files)
                        if len(compact) < len(prompt) and _fits(provider_name, compact):
                            _note(f"  {provider_name}: retrying with the "
                                  f"{'+'.join(sections)} digest only "
                                  f"({len(compact)} chars)")
                            prompt = compact
                            capped_providers.discard(provider_name)
                            continue
                break

            if not plan_path.exists():
                # rc 0 but no plan — the run stopped (deadline or max-turns) before the
                # Write ran. Transient.
                last_err = (f"identify produced no plan file at {plan_path}; "
                            f"tail: {_stderr_tail()}")
                if attempt_no < config.IDENTIFY_MAX_ATTEMPTS:
                    _note(f"  {provider_name}/{model} wrote no plan (attempt {attempt_no}); "
                          f"retrying")
                    continue
                _note(f"  {provider_name}/{model} wrote no plan after "
                      f"{config.IDENTIFY_MAX_ATTEMPTS} attempt(s); giving up on it")
                break

            try:
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                # A truncated plan file is an artifact of a cut stream. Transient.
                last_err = f"plan JSON invalid: {exc}"
                if attempt_no < config.IDENTIFY_MAX_ATTEMPTS:
                    _note(f"  {provider_name}/{model} plan JSON invalid (attempt "
                          f"{attempt_no}); retrying")
                    continue
                break

            saw_plan = True
            # An empty plan is a legitimate "nothing to place" verdict (repeat/extras), NOT
            # a fixable mistake — the caller decides how to treat it. Return it unchanged.
            if not plan.get("files"):
                globals()["_UNAVAILABLE_UNTIL"] = 0.0
                _clear_rejections(info_hash)
                return plan, rationale
            try:
                library.validate_plan(plan, str(content_path),
                                      sibling_seasons=sibling_seasons,
                                      serial_map=serial_map)
                globals()["_UNAVAILABLE_UNTIL"] = 0.0
                _clear_rejections(info_hash)      # it finally landed; the lesson is spent
                return plan, rationale
            except library.PlanError as exc:
                # A genuine rejection: hand it to the next provider as "fix this".
                _note(f"  {provider_name}/{model} plan rejected: {exc}")
                rejections.append({"provider": provider_name, "model": model,
                                   "plan": plan, "error": str(exc)})
                # Persisted HERE rather than at the end of the walk: this run is a
                # subprocess of the ingest daemon, and a deploy restarts the daemon (see
                # HANDOFF §4b) -- a rejection saved only on a clean exit is a rejection
                # lost exactly when the fleet is being changed.
                _save_rejections(info_hash, [rejections[-1]])
                last_err = f"plan rejected by the harness: {exc}"
                break
            except Exception as exc:                                      # noqa: BLE001
                last_err = f"validate failed: {exc}"
                _note(f"  {provider_name}/{model} validation crashed: {exc}")
                break

    if saw_plan:
        summary = "; ".join(f"{r['provider']}/{r['model']}: {r['error']}"
                            for r in rejections)
        raise RuntimeError(f"all {len(attempts)} provider(s) failed to place the content"
                           + (f": {summary}" if summary else ""))
    if unavailable_providers:
        globals()["_UNAVAILABLE_UNTIL"] = (
            time.time() + config.IDENTIFY_UNAVAILABLE_BACKOFF_SEC)
        tried = sorted({a["provider"] for a in attempts})
        others = [p for p in tried if p not in unavailable_providers]
        who = ", ".join(sorted(unavailable_providers))
        rest = (f"; {', '.join(others)} failed for other reasons (see the lines above) "
                f"-- NOT out of budget" if others else "")
        _note(f"  out of budget: {who}{rest}. No provider produced a plan, so identify "
              f"is deferred for {config.IDENTIFY_UNAVAILABLE_BACKOFF_SEC // 60} min")
    raise IdentifyUnavailable(last_err)



def _timeout_for(media_count):
    """Scale the identify timeout to the amount of media to place (see config)."""
    scaled = (config.IDENTIFY_TIMEOUT_BASE_SEC
              + config.IDENTIFY_TIMEOUT_PER_MEDIA_FILE_SEC * media_count)
    return max(config.IDENTIFY_TIMEOUT_BASE_SEC,
               min(scaled, config.IDENTIFY_TIMEOUT_MAX_SEC))


def content_has_media(content_path):
    """Whether the download holds any file the library ingests AS-IS (an archive or
    a video — `config.MEDIA_EXTENSIONS`). This is the harness's side of the
    empty-plan question: an identify run that returns an empty `files` list is a
    legitimate "nothing to shelve" verdict only over a download with none of these.
    Over a download that DOES contain media, an empty plan is the "already-present"
    shortcut the validator still refuses. Loose page images (jpg/png) are NOT
    counted here: they are packageable into a .cbz, so the run is expected to plan
    them; if it still returns empty (all redundant, or a refusal), the caller's
    skip path treats the drop as a clean no-op.
    """
    root = Path(content_path)
    if root.is_file():
        return root.suffix.lower() in config.MEDIA_EXTENSIONS
    return any(p.is_file() and p.suffix.lower() in config.MEDIA_EXTENSIONS
               for p in root.rglob("*"))


def _list_files(content_path):
    """Return (listing_text, media_count) for the download.

    The listing is engineered to keep the identify run focused on placement rather
    than spelunking. Two things bloat a run and cause timeouts (Billy & Mandy, the
    Phineas & Ferb pack): thousands of clutter files, and big TOP-LEVEL decoy
    folders of unrelated shows that release groups bundle in (e.g. an "OTHER
    Cartoons You'd PROBABLY Like" dir full of promo `.txt`). So we:
      * list media files first (with sizes), non-media clutter after,
      * annotate each top-level entry with its media-file count, explicitly marking
        those with NONE as "(no media — ignore)" so the run skips the decoy,
      * surface folders of LOOSE PAGE IMAGES (bare jpg/png) as packageable work —
        one .cbz per story folder — instead of marking them "no media — ignore",
      * cap the inline listing; past the cap, summarize the tail rather than dump
        it, so a pathological pack can't explode the prompt.
    """
    from pathlib import Path

    root = Path(content_path)
    if root.is_file():
        is_media = root.suffix.lower() in config.MEDIA_EXTENSIONS
        return (f"{root.name}  ({root.stat().st_size} bytes)", 1 if is_media else 0)

    media, loose, clutter = [], [], []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if p.suffix.lower() in config.MEDIA_EXTENSIONS:
            media.append(f"{rel}  ({p.stat().st_size} bytes)")
        elif p.suffix.lower() in config.LOOSE_PAGE_EXTENSIONS:
            loose.append(rel)
        else:
            clutter.append(f"{rel}  (non-media)")
    media_count = len(media)

    # Group loose page images by their containing (story) folder: the run proposes
    # ONE .cbz per folder, so we present folders, not 6000 individual pages.
    loose_by_dir = {}
    for rel in loose:
        loose_by_dir.setdefault(str(rel.parent), []).append(rel)
    loose_count = len(loose_by_dir)

    # Per top-level entry: how much media lives under it. A folder of loose pages
    # is packageable work, not a decoy.
    top_summary = []
    for child in sorted(root.iterdir()):
        if child.is_dir():
            n = sum(1 for p in child.rglob("*")
                    if p.is_file() and p.suffix.lower() in config.MEDIA_EXTENSIONS)
            if n:
                tag = f"{n} media file(s)"
            else:
                npg = sum(1 for p in child.rglob("*")
                          if p.is_file() and p.suffix.lower() in config.LOOSE_PAGE_EXTENSIONS)
                tag = (f"{npg} page image(s) — package as .cbz"
                       if npg else "no media — ignore")
            top_summary.append(f"  {child.name}/  [{tag}]")

    cap = config.IDENTIFY_MAX_LISTING_FILES
    shown = media[:cap]
    parts = []
    if top_summary:
        parts.append("Top-level entries:")
        parts.extend(top_summary)
        parts.append("")
    parts.append(f"MEDIA FILES ({media_count} total"
                 + (f", showing first {cap}" if media_count > cap else "") + "):")
    parts.extend(shown or ["  (none)"])
    if media_count > cap:
        parts.append(f"  ... and {media_count - cap} more media files (same naming pattern).")
    if loose_by_dir:
        parts.append("")
        parts.append(f"LOOSE PAGE-IMAGE FOLDERS ({loose_count} folders, {len(loose)} "
                     f"pages — package each as a Comics .cbz; src = <content path>/<folder>):")
        parts.extend(f"  {d}/  ({len(ps)} pages)" for d, ps in sorted(loose_by_dir.items()))
    if clutter:
        parts.append("")
        parts.append(f"NON-MEDIA CLUTTER ({len(clutter)} files — samples/txt/nfo/art, "
                     f"not for the library):")
        parts.extend(clutter[:40])
        if len(clutter) > 40:
            parts.append(f"  ... and {len(clutter) - 40} more non-media files.")
    # The second value drives the identify timeout: media files and loose-page
    # folders are both units of placement work.
    return ("\n".join(parts), media_count + loose_count)


def _extract_rationale(stdout):
    """Pull the run's closing text out of ai_runner's --output-format json envelope."""
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
