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
import journal
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


# --- release-order -> broadcast numbering for title-named packs (HANDOFF 10.9) ---
#
# THE SMURFS. Its replacement pack names every episode `The Smurfs S01E01 (The
# Smurfette).mp4` -- release order, which is NOT broadcast order: *The Smurfette* is
# broadcast S01E31. The deleted dvdrip was filed positionally for exactly this reason
# (`S01E01` = "The Smurfette" on the mount, wrong), and a complete plan written off the
# release's own numbers would repeat the fault at full scale. This is the
# `serial_release_map`/`arcmap` class again: the harness computes the mapping from the
# release's own episode TITLES against the provider's list, states it in the prompt as
# fact, and `validate_plan` refuses a plan that contradicts it. Fail open everywhere.

_TITLE_TAG_RE = re.compile(
    # The closing bracket may be missing: real packs ship `...(I Smurf to All Trees.mp4`
    # (measured on the Smurfs pack), and dropping the whole title over one lost paren
    # leaves the file unmapped and its release number colliding with a computed slot.
    r"[Ss](\d{1,3})[Ee](\d{1,4})\s*[\(\[]([^\)\]]{2,90}?)(?:[\)\]]|\.(?:mp4|mkv|avi|m4v|mov)$)",
    re.I)
# The bracket-less form, `The Smurfs S07E49 - Nobody Smurf.mp4`, is a WEAK witness: the
# whole dash fallback was tried and replayed on 2026-09-20 and swept up multi-episode
# markers (`S03E49-E40 - ...`), scene tags in the title, and `S01E24-25-SP` shapes --
# 54 would-be rejections across three historical plans. What ships now is the narrow
# half that measurement left standing: whitespace-delimited dash only, and the claim it
# may make is exact and unique or nothing at all (`_match_titles(..., exact_only=True)`
# never reaches the ratio pass). Measured on the 70-file Smurfs re-fetch: `Nobody Smurf`
# is TMDB S07E27 while the release number it carries, S07E49, is another episode's slot.
_DASH_TITLE_RE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,4})\s+-\s+(.{2,90})$", re.I)
# The scene form, `Show.Name.S01E03.The.Third.1080p.AMZN.WEB-DL.mkv`: the remainder
# after the episode tag is dot/underscore-joined, and its tail is release tags. Read
# ONLY for the AGREEMENT fact (`release_episode_agreement` below), never as a reorder
# witness. A dot remainder that carries TWO titles (`The.Car.-.The.Curse`, a combined
# file) states no single slot and is skipped.
_DOT_TITLE_RE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,4})[._]([^/\\]{2,90})$", re.I)
_TITLE_MAP_MIN_FRACTION = 0.6
_TITLE_MAP_MIN_ENTRIES = 4


def release_title_entries(release_files):
    """`[(rel, season, episode, title)]` for files named `SxxEyy (Title)`, or [].

    Bracket-only ON PURPOSE. A dash fallback (`SxxEyy - Title.ext`) was tried and
    replayed: it swept up multi-episode markers (`S03E49-E40 - ...`), scene tags in
    the title, and `S01E24-25-SP` shapes, producing 54 would-be rejections across
    three historical plans. The bracket form is the reliable witness; the dash form
    is read only by `release_dash_title_entries` and may claim only exact matches.
    """
    out = []
    for item in release_files or ():
        rel = str(item[0] if isinstance(item, (tuple, list)) else item)
        m = _TITLE_TAG_RE.search(Path(rel).name)
        if not m:
            continue
        try:
            out.append((rel, int(m.group(1)), int(m.group(2)), m.group(3).strip()))
        except ValueError:
            continue
    return out


def release_dash_title_entries(release_files):
    """`[(rel, season, episode, title)]` for bracket-LESS `SxxEyy - Title` names, or [].

    The weak half of the title witness (see `_DASH_TITLE_RE`). A file the bracket parser
    already read is skipped so it is claimed once, by the reliable witness. `_match_titles`
    is called with `exact_only=True` over these, so a title carrying a scene tag, a part
    marker or a release group never becomes a claim through the ratio pass.
    """
    bracketed = {str(e[0]) for e in release_title_entries(release_files)}
    out = []
    for item in release_files or ():
        rel = str(item[0] if isinstance(item, (tuple, list)) else item)
        if rel in bracketed:
            continue
        m = _DASH_TITLE_RE.search(Path(rel).stem)
        if not m:
            continue
        try:
            # Clean the release-tag tail (`[WEBDL-1080p][EAC3 5.1]-playWEB`) before the
            # title is matched. The dash form is the ordinary spelling of real packs
            # (`American Dad! (2005) - S10E06 - Independent Movie [tags]-playWEB.mkv`),
            # and comparing the raw capture against a provider name never matches --
            # which left the identity map empty on the very pack it exists for.
            out.append((rel, int(m.group(1)), int(m.group(2)),
                        library._clean_episode_title(m.group(3).strip())))
        except ValueError:
            continue
    return out


def release_dot_title_entries(release_files):
    """`[(rel, season, episode, title)]` for dot/underscore scene names, or [].

    `The.Amazing.World.of.Gumball.S01E03.The.Third.1080p.AMZN.WEB-DL.mkv` -> the title
    is `The Third` (dots are word separators; the tag tail is left in and simply fails
    the exact match). A file the bracket or dash parsers already read is skipped, and a
    remainder carrying TWO titles (`The.Car.-.The.Curse`) is skipped: a file that holds
    two episodes does not state which single slot it belongs at. Read only by
    `release_episode_agreement` -- never as a reorder witness (see that function for
    the measured reason).
    """
    claimed = {str(e[0]) for e in release_title_entries(release_files)}
    claimed |= {str(e[0]) for e in release_dash_title_entries(release_files)}
    out = []
    for item in release_files or ():
        rel = str(item[0] if isinstance(item, (tuple, list)) else item)
        if rel in claimed:
            continue
        stem = Path(rel).stem
        if _TITLE_TAG_RE.search(stem) or _DASH_TITLE_RE.search(stem):
            continue
        m = _DOT_TITLE_RE.search(stem)
        if not m:
            continue
        rest = m.group(3)
        if re.search(r"\s-\s|\.-\.", rest):
            continue                          # a two-episode file names no single slot
        title = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", rest)
        title = " ".join(title.replace(".", " ").replace("_", " ").split())
        if len(title) < 2:
            continue
        try:
            out.append((rel, int(m.group(1)), int(m.group(2)), title))
        except ValueError:
            continue
    return out


def _title_tokens(text):
    return {w for w in re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split()
            if len(w) > 2}


_PART_RE = re.compile(r"(?:^|\s)(?:pt|part)\.?\s*(\d+)\b|\s*\((\d+)\)\s*$", re.I)


def _title_norm(text):
    """A comparison string with part markers canonicalized.

    `A Smurf on the Wild Side - pt1` and `Smurf On The Wild Side (1)` must be the same
    string, or a two-part story's titles never match the guide's names (measured on the
    Smurfs pack). Digits survive; everything else is lowercased words.
    """
    s = str(text or "").lower()

    def _part(m):
        n = m.group(1) or m.group(2)
        return f" part {n}" if n else " "

    s = _PART_RE.sub(_part, s)
    return " ".join(w for w in re.sub(r"[^a-z0-9]+", " ", s).split())


_RATIO_MATCH_MIN = 0.84
_RATIO_MATCH_MARGIN = 0.06


def _match_titles(entries, guide, exact_only=False):
    """`{(season, episode): (guide_season, guide_episode)}` for uniquely matched titles.

    Three passes, each stricter about ambiguity than the last: exact token set, then a
    character-ratio match for the spelling variants real releases carry ("Smurf Colored
    Glasses" vs the guide's "Smurfed Coloured Glasses", "Smurphony In 'C'" vs
    "Smurphony in 'C'"), and no claim at all on a near-tie. The 2026-09-20 Smurfs run
    is why the ratio pass exists: the token matcher missed *Smurf Colored Glasses*, its
    release number then collided with a matched file's computed slot, and the plan
    collapse destroyed the mapped copy at 32 destinations.

    `exact_only=True` stops after the exact/unique pass. It is how the dash-title
    witness is read: that form is not reliable enough for a fuzzy claim (a scene tag or
    part marker could drag it onto a nearby title), but an exact, unique match is
    positive evidence and is used.
    """
    by_words = []
    for e in guide or ():
        name = str(e.get("name") or "")
        if not name:
            continue
        try:
            by_words.append((_title_tokens(name), _title_norm(name),
                             int(e["season"]), int(e["number"]), name))
        except (KeyError, TypeError, ValueError):
            continue
    # A guide title carried by more than one episode (clip shows, "The Smurfette"
    # remakes) is not an exact identity; those fall through to the ratio pass, where
    # the closest spelling and the query's own part digit decide.
    norm_count = {}
    for _gt, gnorm, _gs, _ge, _gn in by_words:
        norm_count[gnorm] = norm_count.get(gnorm, 0) + 1
    claims = {}
    for _rel, rs, re_, title in entries:
        toks = _title_tokens(title)
        if not toks:
            continue
        hit = None
        for gtoks, gnorm, gs, ge, _gname in by_words:
            if toks == gtoks and norm_count.get(gnorm, 0) == 1:
                hit = (gs, ge)
                break
        if hit is None and not exact_only:
            best = None
            for gtoks, _gnorm, gs, ge, _gname in by_words:
                if not gtoks:
                    continue
                overlap = len(toks & gtoks)
                score = overlap / max(1, min(len(toks), len(gtoks)))
                if score >= 0.75 and overlap >= 2:
                    if best is None or score > best[0]:
                        best = (score, gs, ge)
                    elif score == best[0] and (gs, ge) != best[1:]:
                        best = None       # a tie between different episodes: no claim
                        break
            if best is not None:
                hit = (best[1], best[2])
        if hit is None and not exact_only:
            import difflib as _difflib
            norm = _title_norm(title)
            scored = sorted(((_difflib.SequenceMatcher(None, norm, gnorm).ratio(), gs, ge,
                              gnorm)
                             for _gt, gnorm, gs, ge, _gn in by_words if gnorm),
                            key=lambda x: -x[0])
            if scored and scored[0][0] >= _RATIO_MATCH_MIN:
                top = scored[0]
                # The parts of a multi-part story are the same title modulo their digit
                # ("Wild Side (1)/(2)"); a character ratio cannot separate them, but the
                # query's own digit can, so those candidates do not count as a runner-up.
                # A digitless query against several parts is genuinely undecidable
                # (`Smurfs that Time Forgot` vs (1)/(2)/(3)) and gets NO claim -- the
                # partless case must be resolved by a human or the model, never guessed.
                base = re.sub(r"\d+", "#", top[3])
                same_base = [x for x in scored
                             if re.sub(r"\d+", "#", x[3]) == base]
                if len(same_base) > 1 and not re.search(r"\d", norm):
                    hit = None
                else:
                    second = 0.0
                    for score, _gs, _ge, gnorm in scored[1:]:
                        if re.sub(r"\d+", "#", gnorm) != base:
                            second = score
                            break
                    if top[0] - second >= _RATIO_MATCH_MARGIN:
                        hit = (top[1], top[2])
        if hit is None:
            continue
        key = (rs, re_)
        if key in claims and claims[key] != hit:
            continue
        claims[key] = hit
    # A guide episode claimed by two different release files is not a mapping.
    seen = {}
    for key, target in claims.items():
        seen.setdefault(target, []).append(key)
    for target, keys in seen.items():
        if len(keys) > 1:
            for key in keys:
                claims.pop(key, None)
    return claims


def _match_dot_titles(entries, guide):
    """`{(season, episode): (guide_season, guide_episode)}` for dot-titled release files.

    The remainder after the episode tag is a dot-joined title plus a tag tail
    (`The.Third.1080p.AMZN.WEB-DL`), so the title is the LONGEST guide name that is a
    token prefix of the remainder. Exact prefixes and a unique guide episode only --
    no ratio pass: a dot form is the ordinary spelling of thousands of packs, and the
    2026-09-20 dash lesson is that a fuzzy witness on an ordinary spelling rejects
    real work. A generic guide name ("The End") cannot shadow a longer real one
    ("The End of the World") because the longest prefix wins, and a remainder whose
    leading words match no guide name makes no claim.
    """
    by_words = []
    for e in guide or ():
        toks = str(e.get("name") or "").lower()
        toks = [w for w in re.sub(r"[^a-z0-9]+", " ", toks).split() if w]
        if len(toks) < 2:                # a one-word guide name is too weak to anchor
            continue
        try:
            by_words.append((toks, int(e["season"]), int(e["number"])))
        except (KeyError, TypeError, ValueError):
            continue
    claims = {}
    for _rel, rs, re_, title in entries:
        toks = [w for w in re.sub(r"[^a-z0-9]+", " ", title.lower()).split() if w]
        best = None
        for gtoks, gs, ge in by_words:
            if len(gtoks) <= len(toks) and toks[:len(gtoks)] == gtoks:
                if best is None or len(gtoks) > len(best[0]):
                    best = (gtoks, gs, ge)
        if best is None:
            continue
        if len({(gs, ge) for gtoks, gs, ge in by_words if gtoks == best[0]}) != 1:
            continue                     # two episodes share the matched name
        key = (rs, re_)
        hit = (best[1], best[2])
        if key in claims and claims[key] != hit:
            continue
        claims[key] = hit
    seen = {}
    for key, target in claims.items():
        seen.setdefault(target, []).append(key)
    for target, keys in seen.items():
        if len(keys) > 1:                # one guide slot claimed by two release files
            for key in keys:
                claims.pop(key, None)
    return claims


def _title_claims(strong_entries, weak_entries, guide, identity_weak=False):
    """All computed title claims, with the two witnesses kept separate.

    Brackets are the reliable form: `_match_titles` reads them with every pass and their
    claims win. Dash titles are read exact-only and may only fill what the bracket claims
    left open -- a key or a guide slot already spoken for is not re-decided by the weak
    witness.

    The dash witness is consulted ONLY once the bracket witness has already proven the
    pack release-ordered (some bracket claim differs from its release key). On its own,
    a dash title is not evidence the pack is reordered at all: dash titles are the
    ordinary form of thousands of packs and the replay reproduced the 2026-09-20
    false-positive class in full (50 contradictions over a multi-show pack whose titles
    are all `Show (Year) - SxxEyy - Title.mkv`, where the plan legitimately files into
    other seasons). The Smurfs shape this exists for is 69 bracket rows plus one dash
    file; there the pack is already proven, and the exact, unique dash title supplies the
    missing row (`Nobody Smurf` is TMDB S07E27 while the release calls it S07E49).

    `identity_weak=True` (HANDOFF 15.1) also admits, from an unproven pack, weak claims
    whose target is the release's OWN key -- and only those. Several real packs name
    every episode in the dash form (`American Dad! (2005) - S10E06 - Independent
    Movie.mkv`), and an exact, unique title match at exactly the release key is
    positive evidence the numbering AGREES; it can only ever pin a file to the slot it
    already states. A weak claim that would MOVE a file is still never admitted without
    a bracket-proven reorder, so the 2026-09-20 class cannot return.
    """
    claims = _match_titles(strong_entries, guide)
    if not any(target != key for key, target in claims.items()):
        if identity_weak and weak_entries:
            for key, target in (_match_titles(weak_entries, guide, exact_only=True)
                                or {}).items():
                if key not in claims and tuple(target) == tuple(key):
                    claims[key] = target
        return claims
    weak = _match_titles(weak_entries, guide, exact_only=True) if weak_entries else {}
    taken = set(claims.values())
    for key, target in weak.items():
        if key in claims or target in taken:
            continue
        claims[key] = target
        taken.add(target)
    return claims


def _guide_for(title, tmdb_id=None):
    """The episode list the title map must be computed against, or None.

    TMDB first when the show's id is known: that is the provider Jellyfin scrapes, and
    the two disagree on real season numbering (The Smurfs S07: TVMaze names E41
    *Locomotive Smurfs*, TMDB -- and Jellyfin -- E43). A map computed against the wrong
    provider files every title in that season one place away from the slot the owner
    sees. `epguide` (TVMaze) stays the fallback for shows with no pinned id and for the
    tests that stub it; both sides fail open.
    """
    if tmdb_id:
        try:
            import tmdbguide
            guide = tmdbguide.episode_names(tmdb_id)
        except Exception:                                            # noqa: BLE001
            guide = None
        if guide:
            return guide, "TMDB"
    if not title:
        return None, ""
    try:
        import epguide
        guide = epguide.episodes(title)
    except Exception:                                                # noqa: BLE001
        return None, ""
    return (guide, "TVMaze") if guide else (None, "")


def release_numbering_claims(content_path, release_files, show_hint=None, tmdb_id=None):
    """`(claims, reordered, provider)` for a titled release, or `({}, False, "")`.

    `claims` maps each release key to the guide slot its own episode title names.
    `reordered` says at least one claim lands somewhere OTHER than the release key --
    the Smurfs case, where the pack's numbering is its catalogue order. A pack whose
    every claim lands on its own key is ordinary numbering AGREEMENT (the American Dad
    case, HANDOFF 15.1): the claims are still computed facts, but they must not trigger
    the skeleton or the scary "this is not broadcast order" block.

    Needs at least `_TITLE_MAP_MIN_ENTRIES` titled files and
    `_TITLE_MAP_MIN_FRACTION` of them matching a unique guide episode.
    `tmdb_id` (the pinned id of the existing show folder) makes the map agree with
    Jellyfin's own episode names; without it the TVMaze fallback is used.
    """
    strong = release_title_entries(release_files)
    weak = release_dash_title_entries(release_files)
    entries_n = len(strong) + len(weak)
    if entries_n < _TITLE_MAP_MIN_ENTRIES:
        return {}, False, ""
    title = show_hint or _release_title_guess(content_path, release_files)
    guide, provider = _guide_for(title, tmdb_id)
    if not guide:
        return {}, False, ""
    claims = _title_claims(strong, weak, guide, identity_weak=True)
    if len(claims) < max(_TITLE_MAP_MIN_ENTRIES,
                         int(entries_n * _TITLE_MAP_MIN_FRACTION)):
        return {}, False, ""
    reordered = any(target != key for key, target in claims.items())
    return claims, reordered, provider


def release_title_map(content_path, release_files, show_hint=None, tmdb_id=None):
    """Computed release->broadcast slots for a REORDERED title-named pack, or {}.

    Only returned when the pack actually DIFFERS from the release's own numbering
    somewhere -- an ordinary pack whose numbers already match must not get a scary
    "this is not broadcast order" block and must not trigger the skeleton. See
    `release_numbering_claims` for the full claim set.
    """
    claims, reordered, _provider = release_numbering_claims(
        content_path, release_files, show_hint=show_hint, tmdb_id=tmdb_id)
    return claims if reordered else {}


def release_identity_map(content_path, release_files, show_hint=None, tmdb_id=None):
    """`(claims, provider)` for a pack whose numbering AGREES with the guide, or {}.

    HANDOFF 15.1 (American Dad!): every titled file matched the guide at its OWN
    `SxxEyy`, so the release's numbering IS broadcast numbering. A stale digest made a
    model remap a wave onto an existing season; this map is the computed fact
    `_reject_title_numbering` refuses a remap against.
    """
    claims, reordered, provider = release_numbering_claims(
        content_path, release_files, show_hint=show_hint, tmdb_id=tmdb_id)
    return ({}, "") if reordered else (claims, provider)


def release_episode_agreement(release_files, show_hint=None, tmdb_id=None):
    """`{(s, e): (s, e)}` for dot-titled files whose own title confirms their own key.

    THE AMAZON GUMBALL PACK (2026-09-26). `The.Amazing.World.of.Gumball.S01E03.The.Third.
    1080p.AMZN.WEB-DL.mkv` carries a title that the guide itself puts at S01E03, and the
    release says S01E03. The identify run instead treated the 32-file season as a
    CONTINUATION of the library ("owned show with continuous-absolute numbering") and
    filed E01-E32 at S01E16-E47; `validate_plan` accepted it, 32 episodes landed in the
    wrong slots, and the *other* in-flight Gumball pack then parked on the collisions.
    Nothing was wrong with the plan's own season/episode fields or the destination
    layout, so the layout guards saw nothing; the computed witness is the file's own
    TITLE against the provider Jellyfin scrapes.

    This is deliberately NOT `release_identity_map`: a dot-titled pack whose own key is
    confirmed says nothing about a library that deliberately renumbers (Steven Universe
    files TMDB's S02 opener at S01E50; One Piece runs absolute numbers in S01). Replayed
    over every plan in `state/tmp` with a live guide (190 plans): feeding these claims
    into the season-remap guard rejected correct work; the same-season shift guard
    (`library._reject_same_season_episode_shift`) rejects exactly the Gumball AMZN shape
    and nothing else. So this map is consumed only by that guard, and only own-key
    claims for numbered seasons are returned.

    Same thresholds as the other witnesses, and fails open on no guide, too few titled
    files, or too few exact matches: a single coincidental title must not constrain a
    plan.
    """
    dots = release_dot_title_entries(release_files)
    if len(dots) < _TITLE_MAP_MIN_ENTRIES:
        return {}
    title = show_hint or ""
    guide, _provider = _guide_for(title, tmdb_id)
    if not guide:
        return {}
    claims = _match_dot_titles(dots, guide)
    own = {k: v for k, v in claims.items()
           if tuple(k) == tuple(v) and k[0] >= 1}
    if len(own) < max(_TITLE_MAP_MIN_ENTRIES,
                      int(len(dots) * _TITLE_MAP_MIN_FRACTION)):
        return {}
    return own


def numbering_agreement_block(identity_map, provider=""):
    """State the computed release-numbering AGREEMENT to the model as fact.

    The companion of `title_numbering_block`: same computed claims, but when every one
    lands on its own release key the block is reassurance, not a warning -- the file's
    `SxxEyy` is the broadcast slot and must not be remapped to another season.
    """
    if not identity_map:
        return ""
    rows = [f"  {key[0]:02d}x{key[1]:02d}" for key in sorted(identity_map)[:24]]
    more = (f"  ... and {len(identity_map) - 24} more\n"
            if len(identity_map) > 24 else "")
    return ("======================================================================\n"
            "RELEASE NUMBERING CONFIRMED -- COMPUTED\n"
            "======================================================================\n"
            "Each file below carries its real episode title, and the harness matched it\n"
            f"against {provider or 'the provider'}'s episode list at the SAME `SxxEyy` the release\n"
            "states. For these files the release's own number IS the broadcast slot: file\n"
            "each at its own `SxxEyy`. Do NOT remap one to another season or episode.\n"
            + "".join(r + "\n" for r in rows) + more)


def title_numbering_block(content_path, release_files, wave_names=None,
                          max_rows=80, tmdb_id=None):
    """The computed title->broadcast numbering, stated to the model as fact, plus the map.

    Returns `(block_text, map)`. Empty block when nothing can be computed.
    """
    title = _release_title_guess(content_path, release_files)
    _guide, provider = _guide_for(title, tmdb_id)
    title_map = release_title_map(content_path, release_files, show_hint=title,
                                  tmdb_id=tmdb_id)
    if not title_map:
        return "", {}
    # Both witnesses, so the computed row for a bracket-less file (`S07E49 - Nobody
    # Smurf`) is stated to the model too; the map passed back is the same one the
    # validator enforces.
    entries = release_title_entries(release_files) + release_dash_title_entries(release_files)
    wanted = None
    if wave_names:
        wanted = {str(n).replace("\\", "/").split("/")[-1] for n in wave_names}
    rows = []
    for rel, rs, re_, title in sorted(entries,
                                      key=lambda e: (title_map.get((e[1], e[2]), (e[1], e[2])))):
        target = title_map.get((rs, re_))
        if not target:
            continue
        if wanted and Path(rel).name not in wanted:
            continue
        rows.append(f"  {Path(rel).name[:58]:60s} =>  Season {target[0]:02d}, "
                    f"Episode {target[1]:02d}   (release S{rs:02d}E{re_:02d}, {title[:40]!r})")
    if not rows:
        return "", title_map
    # A 400-row block plus a 400-file listing blows the prompt past every free
    # provider's ceiling. The skeleton carries the complete computed list; the block's
    # job is to state the RULE and enough examples, and the validator enforces the map
    # regardless.
    more = ""
    if max_rows and len(rows) > max_rows:
        more = (f"\n  ... and {len(rows) - max_rows} more computed row(s); the complete "
                f"list is in the skeleton handed to you.\n")
        rows = rows[:max_rows]
    return ("======================================================================\n"
            "RELEASE-ORDER NUMBERING -- COMPUTED BROADCAST NUMBERING\n"
            "======================================================================\n"
            "This release's `SxxEyy` is its OWN catalogue order, not the broadcast\n"
            "order. Each file carries its real episode title (in parentheses, or after\n"
            f"the dash), and the harness matched those titles against {provider or 'the provider'}'s episode list --\n"
            "the same list Jellyfin will show -- so the broadcast slots below are what\n"
            "each file IS. File each file at the computed slot; do NOT copy the release's\n"
            "`SxxEyy` onto the destination.\n"
            + more + "\n".join(rows) + "\n"), title_map


def plan_skeleton(release_files, title_map=None, title="", kind="show"):
    """A deterministic plan pre-filled with EVERY release file and its computed slot.

    The model cannot be trusted to enumerate the plan: above
    `config.IDENTIFY_SKELETON_MIN_FILES` a single `Write` cannot hold it (the Smurfs run
    burned 36 turns investigating and then wrote a 24-file prefix -- 10.9), and in ANY
    reordered pack its copy of the release numbers is known to be wrong. The harness
    enumerates the release, applies the computed title map, and hands the model a
    skeleton to fill and extend instead of re-typing the listing.
    """
    files = []
    non_episode_videos = 0
    parsed = []          # (entry, release_key, target|None)
    # A computed Season-0 target makes the file a SPECIAL, and `validate_plan` refuses a
    # special with no episode_title/plot (they are always locked at apply time). The
    # release's own name carries the title, so the harness supplies that half; the plot
    # is content the model still authors (the prompt names these entries).
    titles = {}
    for _r, _s, _e, _t in (release_title_entries(release_files or ())
                           + release_dash_title_entries(release_files or ())):
        titles.setdefault(str(_r), _t)
    for item in release_files or ():
        rel = str(item[0] if isinstance(item, (tuple, list)) else item)
        entry = {"src": rel, "dst_rel": "", "season": None, "episode": None}
        m = re.search(r"[Ss](\d{1,3})[Ee](\d{1,4})", Path(rel).name)
        if m:
            s, e = int(m.group(1)), int(m.group(2))
            target = (title_map or {}).get((s, e))
            parsed.append((entry, (s, e), target))
        elif Path(rel).suffix.lower() in config.VIDEO_EXTENSIONS:
            non_episode_videos += 1
        files.append(entry)
    # Once a pack's numbering is known to be PERMUTED -- a title map exists -- an
    # unmatched file's release number is evidence of nothing. It may still be right by
    # luck, but this release's own order is measured to disagree with the guide, so
    # filing positionally is the 2026-09-20 silent-collapse shape (32 mapped files
    # dropped for their fallback twins; `Nobody Smurf` would have been filed at another
    # episode's S07E49). Every unmatched entry is left for the model; the merge omits an
    # entry the model did not claim, and the coverage guard parks rather than guesses.
    # A pack with no map (ordinary numbering, fail-open) keeps the release-number
    # fallback; collision/duplicate cases are unresolved in both worlds.
    reordered = bool(title_map)
    claimed = {tuple(t) for _e, _k, t in parsed if t}
    unmatched = {}
    for _e, key, target in parsed:
        if not target:
            unmatched[key] = unmatched.get(key, 0) + 1

    # ALTERNATE CUTS OF ONE EPISODE (HANDOFF 15.2). A release often ships one episode
    # twice -- a main line plus an alternate audio track/scene set under the SAME
    # `SxxEyy` -- and a free model names only one, so the other reads as an unplaced
    # release file and a 700 GB pack parks on one unresolved entry. When every file in
    # a release-key group carries the SAME core title (version tags stripped; see
    # `journal.alternate_title_core`), the harness knows they are one episode: it slots
    # them all at the group's computed destination and `validate_plan` keeps the ranked
    # survivor, recording the sibling in `_deduped_dropped` (accounted-for, so coverage
    # never parks). The core comparison is exact, so `II` vs `III` and `Part 1` vs
    # `Part 2` are never collapsed. A group whose cores differ is left for the model.
    alternate = {}
    groups = {}
    for entry, key, target in parsed:
        groups.setdefault(key, []).append((entry, target))
    for key, members in groups.items():
        if len(members) < 2:
            continue
        cores = {journal.alternate_title_core(str(e.get("src") or ""))
                 for e, _t in members}
        cores.discard("")
        if len(cores) != 1:
            continue
        target = next((t for _e, t in members if t), None)
        if not target and not reordered:
            target = key
        if not target:
            continue
        alternate[key] = target
        for e, _t in members:
            e["_alternate"] = f"S{key[0]:02d}E{key[1]:02d}"

    for entry, key, target in parsed:
        if target:
            entry["season"], entry["episode"] = target
        elif key in alternate:
            entry["season"], entry["episode"] = alternate[key]
        elif reordered or key in claimed or unmatched.get(key, 0) > 1:
            entry["season"], entry["episode"] = None, None
        else:
            entry["season"], entry["episode"] = key
        if entry.get("season") == 0 and titles.get(str(entry["src"])) \
                and not entry.get("episode_title"):
            entry["episode_title"] = titles[str(entry["src"])]
    if non_episode_videos:
        kind = "mixed"
    return {"media_type": kind, "title": title, "files": files}


def _resolve_release_abs(content_path, release_files):
    """Absolute on-disk path for each release-relative name; names not on disk dropped.

    `release_files` arrives with different roots. The `.torrent` mirror the whole-torrent
    path uses names files relative to the torrent root, while qBittorrent's own file list
    (the chunked path) prefixes every name with the torrent's root folder -- and
    `content_path` for a multi-file torrent IS that root folder. Joining blindly doubled
    it (`.../American Dad! (2005)/American Dad! (2005)/Season 01/...`) and every
    provider's merged plan died on `src does not exist` (measured live, 2026-09-23).
    A chunked wave has only its own files on disk and `validate_plan` requires every src
    to exist, so anything that does not resolve is dropped: the wave's skeleton and
    coverage manifest may only name real files. A single-file torrent has no folder and
    `content_path` is the file itself.
    """
    root = Path(content_path)
    out, seen = [], set()
    for item in release_files or ():
        rel = str(item[0] if isinstance(item, (tuple, list)) else item)
        if not rel:
            continue
        candidates = []
        if root.is_file() and Path(rel).name == root.name:
            candidates.append(root)
        candidates.append(root / rel)
        parts = Path(rel).parts
        if len(parts) > 1:
            candidates.append(root / Path(*parts[1:]))
        for cand in candidates:
            try:
                if cand.exists():
                    s = str(cand)
                    if s not in seen:
                        seen.add(s)
                        out.append(s)
                    break
            except OSError:
                continue
    return out


def _skeleton_needed(release_files, title_map=None):
    """Whether the harness must hand the model a deterministic skeleton.

    Two computed reasons, either sufficient: the release is large enough that one
    `Write` cannot hold the plan (`config.IDENTIFY_SKELETON_MIN_FILES`, 10.9), or its own
    numbering is known to be PERMUTED (a computed title map exists). The second is what
    the 70-file Smurfs re-fetch drop needed on 2026-09-20: every one of 14 providers
    produced a plan with a destination the title map contradicted, because the model was
    left to enumerate 70 release-ordered files below the size floor. A reordered pack is
    exactly where the release numbers are wrong, so the enumeration must come from the
    harness whatever the size.
    """
    if not release_files:
        return False
    return (len(release_files) >= config.IDENTIFY_SKELETON_MIN_FILES
            or bool(title_map))


def _show_folder_for(title, year):
    """The library folder `Shows/<Title (Year)>` when it exists, else the canonical name.

    Used to build episode destinations in a skeleton merge; a folder already in the
    library (the Smurfs (1981)) must be continued, not re-invented."""
    try:
        base = config.MEDIAFS_MOUNT / "Shows"
        if not base.is_dir():
            base = config.SHOWS_ROOT
        want = library.normalize_folder_name(title or "")
        if not want:
            return ""
        for p in base.iterdir():
            if not p.is_dir():
                continue
            stem = p.name
            m = re.search(r"\((\d{4})\)\s*$", stem)
            folder_year = int(m.group(1)) if m else None
            if library.normalize_folder_name(re.sub(r"\s*\(\d{4}\)\s*$", "", stem)) != want:
                continue
            if year and folder_year and abs(int(year) - folder_year) > 1:
                continue
            return p.name
    except Exception:                                                # noqa: BLE001
        pass
    return f"{title} ({int(year)})" if year else (title or "")


_EPISODE_VIDEO_EXT = {}


def _pinned_show_tmdb_id(title, year=None):
    """The TMDB id the library folder for `title` pins in `tvshow.nfo`, or None.

    The release->broadcast title map must be computed against the same episode list
    Jellyfin shows. Jellyfin's list comes from the id in `tvshow.nfo`, so the pinned id
    is read here and handed to `release_title_map`; a show that is not in the library
    (or pins no id) falls back to TVMaze inside the guide helper. Fail open everywhere:
    an unreadable folder or nfo is simply "no pinned id".
    """
    if not title:
        return None
    try:
        base = config.MEDIAFS_MOUNT / "Shows"
        if not base.is_dir():
            base = config.SHOWS_ROOT
        want = library.normalize_folder_name(title)
        if not want:
            return None
        for p in base.iterdir():
            if not p.is_dir():
                continue
            stem = p.name
            m = re.search(r"\((\d{4})\)\s*$", stem)
            folder_year = int(m.group(1)) if m else None
            if library.normalize_folder_name(re.sub(r"\s*\(\d{4}\)\s*$", "", stem)) != want:
                continue
            if year and folder_year and abs(int(year) - folder_year) > 1:
                continue
            nfo = p / "tvshow.nfo"
            try:
                text = nfo.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = re.search(r"<tmdbid>\s*(\d+)\s*</tmdbid>", text)
            if m:
                return m.group(1)
    except Exception:                                                # noqa: BLE001
        return None
    return None


def _src_tail(src, n):
    """The last `n` path components of `src` (fewer when the path is shorter)."""
    parts = Path(str(src)).parts
    return tuple(parts[-n:]) if len(parts) >= n else tuple(parts)


def _incomplete_plan_feedback(unresolved):
    """`(error_text, missing_basenames)` for a merge that could not place files.

    HANDOFF 15.3: an unresolved release file is a FIXABLE rejection, not a terminal
    park. The next provider is told exactly which names were unplaced, and those names
    become its `--require-list`, so the run cannot finish without answering for them.
    """
    missing = []
    for u in unresolved or ():
        n = Path(str(u)).name
        if n and n not in missing:
            missing.append(n)
    err = (f"the harness could not place {len(unresolved)} release "
           f"file(s) from your plan and its own computed enumeration: "
           + ", ".join(missing[:8])
           + (" ..." if len(missing) > 8 else "")
           + ". Every release file must appear in `files` with its own "
             "destination -- for a file at one release SxxEyy whose "
             "siblings are alternate cuts, name it at the same "
             "destination; the harness records the ranked survivor.")
    return err, missing


def merge_skeleton_plan(plan, skeleton, log_fn=None):
    """Complete a partial model plan from the deterministic skeleton.

    HANDOFF 10.9: a free model cannot re-type a 409-file plan. The skeleton is the
    harness's enumeration -- every release file with its computed season/episode -- so
    the merge keeps the model's entries where it supplied them and fills every other
    EPISODE entry's destination from the computed slot. A file the harness could not
    place (a movie/special with no episode number) is left out so the coverage guard
    parks the release rather than guessing.

    MATCHING IS BY PATH, NOT BY BASENAME ALONE (HANDOFF 15.3, the Friends Featurettes).
    The model routinely re-types `src` instead of copying the skeleton's path -- it
    dropped the trailing `)` of the torrent root for all 32 entries -- and the merge,
    which only compared exact strings and unique basenames, lost the four whose
    basenames repeat across season folders (`Friends of Friends_new.mkv` exists in
    Season 2 and Season 10). Exact src, then a unique 3-component tail, then a unique
    2-component tail (parent folder + basename), then a unique basename: each fallback
    only claims when exactly ONE skeleton file can match, so a genuinely ambiguous
    entry still goes to `unresolved` rather than to a guess.

    A destination TWO sources claim is a DISAGREEMENT, not a duplicate: it happens when
    an unmapped file keeps its release number and a mapped file's computed slot lands
    on that same number (the guide's order and the release's order differ). The
    intra-torrent duplicate collapse would otherwise pick the larger copy and silently
    delete the mapped episode; measured on the Smurfs pack. Both go to `unresolved`
    (park) instead -- UNLESS every entry at that destination is a marked ALTERNATE
    sibling from `plan_skeleton` (`_alternate`), which is a deliberate same-episode
    collapse the validator will rank and record.

    Never raises; returns `(plan, filled, unresolved)`.
    """
    try:
        skel_files = (skeleton or {}).get("files") or []
    except AttributeError:
        return plan, 0, []
    if not skel_files:
        return plan, 0, []
    by_src = {}
    by_name = {}
    by_tail = {2: {}, 3: {}}
    for f in plan.get("files") or []:
        if not isinstance(f, dict) or not f.get("src"):
            continue
        by_src[str(f["src"])] = f
        by_name.setdefault(Path(str(f["src"])).name, []).append(f)
        for n in (2, 3):
            by_tail[n].setdefault(_src_tail(f["src"], n), []).append(f)
    title = plan.get("title") or (skeleton or {}).get("title") or ""
    year = plan.get("year") or (skeleton or {}).get("year")
    folder = _show_folder_for(title, year)
    pending = []
    unresolved = []
    for sk in skel_files:
        if not isinstance(sk, dict) or not sk.get("src"):
            continue
        entry = dict(sk)
        src = str(sk["src"])
        got = by_src.get(src)
        if got is None:
            for n in (3, 2):
                same = by_tail[n].get(_src_tail(src, n)) or []
                if len(same) == 1:
                    got = same[0]
                    break
        if got is None:
            same = by_name.get(Path(src).name) or []
            got = same[0] if len(same) == 1 else None
        if got:
            for key in ("dst_rel", "season", "episode", "episode_title", "plot",
                        "tmdb_id", "type"):
                if got.get(key) not in (None, ""):
                    entry[key] = got[key]
            # The skeleton's `src` is NOT overridden by the model's. It is the harness's
            # enumeration of a file that exists on disk; the model routinely echoes it
            # with a duplicated root (`.../American Dad! (2005)/American Dad! (2005)/...`,
            # measured 2026-09-23), and `validate_plan` then refuses the whole plan on
            # "src does not exist". The entry was matched by src or unique basename, so
            # the enumeration's path is the right one.
        dst = str(entry.get("dst_rel") or "")
        if not dst and entry.get("season") is not None and entry.get("episode") is not None \
                and folder:
            ext = Path(src).suffix.lower() or ".mkv"
            entry["dst_rel"] = (f"Shows/{folder}/Season {int(entry['season']):02d}/"
                                f"{folder} - S{int(entry['season']):02d}"
                                f"E{int(entry['episode']):02d}{ext}")
        if not str(entry.get("dst_rel") or ""):
            unresolved.append(src)
            continue
        pending.append(entry)
    seen = {}
    for e in pending:
        seen.setdefault(str(e["dst_rel"]), []).append(e)
    merged, filled = [], 0

    def _harness_filled(e):
        return (by_src.get(str(e.get("src"))) is None
                and not any(sk.get("dst_rel") for sk in skel_files
                            if str(sk.get("src")) == str(e.get("src"))))

    for dst, entries in seen.items():
        if len(entries) > 1:
            alt_ids = {str(e.get("_alternate") or "") for e in entries}
            if alt_ids != {""} and "" not in alt_ids:
                # Every entry here is a marked alternate sibling of ONE episode
                # (`plan_skeleton`): a deliberate same-destination set, not the
                # two-sources-originally-disagree shape. Keep them all; `validate_plan`
                # ranks the survivor and records the rest in `_deduped_dropped`.
                filled += sum(1 for e in entries if _harness_filled(e))
                merged.extend(entries)
                continue
            for e in entries:
                unresolved.append(str(e.get("src")))
            continue
        e = entries[0]
        if _harness_filled(e):
            filled += 1
        merged.append(e)
    if not merged:
        return plan, 0, unresolved
    out = dict(plan)
    out["files"] = merged
    out["_skeleton_merged"] = {"filled": filled, "unresolved": unresolved[:50],
                               "model_entries": len(by_src)}
    if log_fn:
        if filled:
            log_fn(f"  skeleton merge: filled {filled} episode destination(s); "
                   f"{len(unresolved)} file(s) still need a decision")
        if len(unresolved) > 0 and filled:
            log_fn(f"  skeleton merge: {len(unresolved)} unresolved file(s) -- the "
                   f"release will park rather than file a wrong slot")
    return out, filled, unresolved


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
                    release_files=None, title_block="", skeleton_path=None,
                    require_count=0, skeleton_slotted=0, skeleton_unslotted=0,
                    unslotted_files=None, metadata_files=None, tmdb_id=None,
                    identity_block="", alternate_files=None, specials_scheme_text=""):
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
    titles = title_block or identity_block or ""
    if not titles and release_files:
        titles, _tm = title_numbering_block(content_path, release_files,
                                            wave_names=_listing_names(file_listing),
                                            tmdb_id=tmdb_id)
    specials_scheme = specials_scheme_text
    provider = _provider_season_block(content_path, release_files)
    arcs = _arc_season_block(content_path, release_files)
    specials = _specials_metadata_block(content_path, release_files,
                                        wave_paths=_listing_names(file_listing))
    ownership = _ownership_block(content_path, release_files)
    # Large or reordered releases get the skeleton and the coverage contract in the
    # prompt: a single `Write` cannot hold a 409-file plan, a free model cannot be
    # trusted to enumerate a release whose numbering is permuted at any size, and the
    # run must know a partial plan is a park, not a success (HANDOFF 10.9).
    coverage_note = ""
    if skeleton_path or require_count:
        bits = []
        if skeleton_path:
            listing = ""
            if unslotted_files:
                shown = "\n".join(f"    {n}" for n in unslotted_files[:25])
                listing = (f"\nFILES THAT NEED YOUR DECISION ({len(unslotted_files)}):\n"
                           f"{shown}\n"
                           + (f"    ... and {len(unslotted_files) - 25} more\n"
                              if len(unslotted_files) > 25 else ""))
            alternates = ""
            if alternate_files:
                shown = "\n".join(f"    {n}" for n in alternate_files[:25])
                alternates = (
                    f"\nALTERNATE CUTS OF ONE EPISODE ({len(alternate_files)}):\n"
                    f"{shown}\n"
                    "  These are the SAME episode under one release `SxxEyy` (alternate\n"
                    "  audio/scene cuts), already placed at one destination by the\n"
                    "  harness. Do NOT give them separate destinations; if you list one,\n"
                    "  its `episode_title` must be the episode's real title.\n")
            specials = ""
            if metadata_files:
                shown = "\n".join(f"    {n}" for n in metadata_files[:25])
                specials = (f"\nSEASON-0 SPECIALS THAT NEED A PLOT ({len(metadata_files)}):\n"
                            f"{shown}\n"
                            "  These were placed in Season 0 by the computed map; the\n"
                            "  harness supplied each episode_title from the release name,\n"
                            "  but a Season-0 entry is LOCKED at apply time and is REFUSED\n"
                            "  without a non-empty `plot`. Include each in `files` with a\n"
                            "  real `plot` (and correct `episode_title` if the computed one\n"
                            "  is wrong).\n")
            bits.append(
                "THE HARNESS COMPLETES THIS PLAN FOR YOU. It has already enumerated every\n"
                "release file and computed the broadcast destination for the "
                f"{skeleton_slotted} episode file(s) from their own titles. After your run\n"
                "the harness merges its enumeration with your `files`, so:\n"
                "  * do NOT read the skeleton file back, and do NOT re-list the episodes;\n"
                "  * put ONLY the file(s) below in `files`, each with its final `dst_rel`\n"
                "    (and `tmdb_id` for a movie), plus any top-level title/year/ids;\n"
                "  * the skeleton's `src` values are absolute paths that exist on disk:\n"
                "    copy one verbatim if you include it, and NEVER retype, re-root or\n"
                "    'tidy' it -- a retyped path matches nothing and parks the release;\n"
                "  * if you have a real episode title or plot to add, include the entry;\n"
                "    the harness keeps it, but an omitted episode is still placed correctly."
                + listing + alternates + specials)
        if require_count:
            bits.append(
                f"COVERAGE IS REQUIRED. All {require_count} release file(s) must appear in\n"
                "`files`; a plan that covers only some of them is INCOMPLETE and parks the\n"
                "whole release. Write a prefix and EXTEND it with `Edit` (append the\n"
                "remaining entries before the closing `]`), or rewrite the file with\n"
                "`Write` as many times as you need. The harness checks coverage before the\n"
                "run ends and will tell you exactly what is missing; do not finish while\n"
                "files remain unaccounted for.")
        coverage_note = "\n" + "\n\n".join(bits) + "\n"
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
{titles}
{specials_scheme}
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
{coverage_note}
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
    # A large release's listing duplicates the skeleton (which carries the same files
    # with computed destinations). Keep a sample so the model sees the shape and points
    # it at the skeleton for the rest, instead of shipping the same 40 KB twice.
    if release_files and len(release_files) >= config.IDENTIFY_SKELETON_MIN_FILES:
        lines = file_listing.splitlines()
        if len(lines) > 60:
            file_listing = ("\n".join(lines[:60])
                            + f"\n  ... and {len(lines) - 60} more entries; the COMPLETE "
                              f"release listing, with its computed slot where the harness "
                              f"could compute one, is in the skeleton file the prompt names "
                              f"below.\n")
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
    # Release-order numbering for title-named packs (HANDOFF 10.9, The Smurfs). Serial
    # packs already have their binding map; do not stack two numbering blocks on one run.
    # `release_abs` is the subset of `release_files` that actually exists on disk, with
    # the root component resolved (see `_resolve_release_abs`): the skeleton, the title
    # block and the coverage manifest may only name files a plan is allowed to carry.
    release_abs = _resolve_release_abs(content_path, release_files)
    title_block, title_map = "", {}
    large = bool(release_files) and len(release_files) >= config.IDENTIFY_SKELETON_MIN_FILES
    show_title = arc_title or _release_title_guess(content_path, release_files)
    # The map must agree with the list Jellyfin SHOWS, so when the library already holds
    # the show its pinned tmdb id decides whose episode names the titles are matched
    # against (TMDB vs TVMaze disagree on real season numbering; see `_guide_for`).
    show_tmdb_id = _pinned_show_tmdb_id(show_title) if release_files else None
    if release_files and not serial_map:
        # When a skeleton follows, the block needs only the RULE and a few examples:
        # the skeleton carries every computed row, and a 400-row block plus a 400-file
        # listing pushed the Smurfs prompt to 125 KB -- over every free provider's
        # ceiling that could not otherwise serve it.
        title_block, title_map = title_numbering_block(
            content_path, release_abs or release_files, max_rows=20 if large else 80,
            tmdb_id=show_tmdb_id)
        if title_map:
            _note(f"identify: computed release->broadcast numbering for "
                  f"{len(title_map)} file(s) from their own titles"
                  + (f" (matched against TMDB {show_tmdb_id})" if show_tmdb_id else ""))
    # The CONVERSE fact (HANDOFF 15.1, American Dad!): the release's own numbering is
    # confirmed broadcast numbering by its titles, so a season remap cannot be invented.
    # Only computed when the pack is NOT reordered (then `title_map` already governs).
    identity_map, identity_block = {}, ""
    if release_files and not serial_map and not title_map:
        identity_map, _prov = release_identity_map(
            content_path, release_abs or release_files, show_hint=show_title,
            tmdb_id=show_tmdb_id)
        if identity_map:
            identity_block = numbering_agreement_block(identity_map, _prov)
            _note(f"identify: release numbering confirms the provider for "
                  f"{len(identity_map)} file(s); a season remap is refused")
    # The dot-titled scene form (`Show.Name.S01E03.The.Third.1080p...`): a file whose own
    # title matches the guide at exactly its own key confirms the release numbering THERE.
    # Read by `_reject_same_season_episode_shift` only -- a plan that keeps the season and
    # shifts the episode is refused; a deliberate cross-season renumber (absolute runs,
    # merged cours) is untouched. Not stated as a prompt block: the dot witness is a
    # per-file validator fact, and the 2026-09-26 replay showed a global "do not remap"
    # block would contradict libraries that deliberately renumber.
    episode_agreement = {}
    if release_files and not serial_map:
        episode_agreement = release_episode_agreement(
            release_abs or release_files, show_hint=show_title, tmdb_id=show_tmdb_id)
        if episode_agreement:
            _note(f"identify: {len(episode_agreement)} file(s) carry a title confirming "
                  f"their own `SxxEyy`; a same-season episode shift is refused")
    # The library's OWN Season-00 scheme, when this show already owns specials
    # (HANDOFF 15.5): a provider's special number is not the library's slot, and the
    # free AI must be told the scheme as fact rather than scraping the provider.
    specials_scheme_text = ""
    if release_files and show_title:
        try:
            _folder = library.find_show_folder(show_title)
            if _folder is not None:
                specials_scheme_text = library.specials_scheme_block(_folder)
        except Exception:                                             # noqa: BLE001
            specials_scheme_text = ""
    # A large OR reordered release gets a deterministic skeleton and a coverage
    # contract: one Write cannot hold a 409-file plan (10.9), and below that floor a
    # release-ordered pack still cannot be enumerated by the model (the 70-file Smurfs
    # re-fetch failed all 14 providers on 2026-09-20). See `_skeleton_needed`.
    skeleton_path = None
    # The trigger stays on the RELEASE list, not the resolved subset: the whole release
    # decides whether this pack needs a computed enumeration at all, and every wave of a
    # large pack gets one (the skeleton itself is built over the on-disk subset, so it
    # only ever promises files the wave holds). A free model that must enumerate 70
    # reordered files failed all 14 providers (the Smurfs drop); the skeleton is the
    # computed answer, and a wave below the floor gets it only when the title map proves
    # its numbering permuted.
    if _skeleton_needed(release_files, title_map):
        skeleton_path = config.TMP_DIR / f"{info_hash}_skeleton.json"
        try:
            _skel = plan_skeleton(release_abs or release_files, title_map,
                                  title=show_title)
            skeleton_path.write_text(json.dumps(_skel, indent=1), encoding="utf-8")
            # The ENTRY count, not the whole release's: a chunked wave's skeleton
            # covers only the files on disk, and a log line saying "390-file skeleton"
            # beside a 32-entry file sends the next reader after the wrong thing.
            _note(f"identify: wrote a {len(_skel.get('files') or [])}-file skeleton for "
                  f"{skeleton_path.name}")
        except OSError:
            skeleton_path = None
    skeleton_slotted = skeleton_unslotted = 0
    unslotted_files, metadata_files, alternate_files = [], [], []
    if skeleton_path is not None:
        try:
            _sk = json.loads(skeleton_path.read_text(encoding="utf-8"))
            for _f in (_sk.get("files") or []):
                if _f.get("season") is not None and _f.get("episode") is not None:
                    skeleton_slotted += 1
                    # A computed slot in Season 0 makes it a SPECIAL: the validator
                    # requires episode_title+plot (always locked at apply). The skeleton
                    # supplies the title from the release name; these are named to the
                    # model so it writes the plot with its entry.
                    if int(_f.get("season")) == 0:
                        metadata_files.append(Path(str(_f.get("src") or "")).name)
                    # An ALTERNATE sibling (`plan_skeleton` marked it) is named to the
                    # model so it never invents a second destination for one episode.
                    if _f.get("_alternate"):
                        alternate_files.append(Path(str(_f.get("src") or "")).name)
                else:
                    skeleton_unslotted += 1
                    unslotted_files.append(Path(str(_f.get("src") or "")).name)
        except (OSError, ValueError):
            pass
    # The files the plan must account for, so ai_client can tell the model exactly what a
    # truncated plan left out BEFORE the run ends instead of parking after it (10.9).
    # On-disk files only: a chunked wave cannot account for files its earlier/later waves
    # hold, and the post-run coverage contract reads the disk walk as the authority.
    require_files = [Path(p).name for p in release_abs
                     if Path(p).suffix.lower() in config.VIDEO_EXTENSIONS]
    # With a skeleton the harness COMPLETES the plan from its own enumeration, so the
    # per-file coverage nudge is wrong here: it would order the model to append the
    # hundreds of episode entries the merge already supplies, wasting its turns (and
    # risking the good slot-less entries it did write). The coverage guard stays the
    # last line of defense for anything the merge could not place.
    if skeleton_path is not None:
        require_files = []
    require_list = config.TMP_DIR / f"{info_hash}_require.json"
    if require_files:
        try:
            require_list.write_text(json.dumps(require_files), encoding="utf-8")
        except OSError:
            require_files = []

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
                                 release_files=release_files,
                                 title_block=title_block,
                                 skeleton_path=str(skeleton_path) if skeleton_path else None,
                                 require_count=len(require_files),
                                 skeleton_slotted=skeleton_slotted,
                                 skeleton_unslotted=skeleton_unslotted,
                                 unslotted_files=unslotted_files,
                                 metadata_files=metadata_files,
                                 tmdb_id=show_tmdb_id,
                                 identity_block=identity_block,
                                 alternate_files=alternate_files,
                                 specials_scheme_text=specials_scheme_text)
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
                                      release_files=release_files,
                                      title_block=title_block,
                                      skeleton_path=str(skeleton_path) if skeleton_path else None,
                                      require_count=len(require_files),
                                      skeleton_slotted=skeleton_slotted,
                                      skeleton_unslotted=skeleton_unslotted,
                                      unslotted_files=unslotted_files,
                                      metadata_files=metadata_files,
                                      tmdb_id=show_tmdb_id,
                                      identity_block=identity_block,
                                      alternate_files=alternate_files,
                                      specials_scheme_text=specials_scheme_text)
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
            "--require-list",
            str(require_list) if require_files else "",
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
                            release_files=release_files, title_block=title_block,
                            skeleton_path=str(skeleton_path) if skeleton_path else None,
                            require_count=len(require_files),
                            skeleton_slotted=skeleton_slotted,
                            skeleton_unslotted=skeleton_unslotted,
                            unslotted_files=unslotted_files,
                            metadata_files=metadata_files,
                            tmdb_id=show_tmdb_id,
                            identity_block=identity_block,
                            alternate_files=alternate_files,
                            specials_scheme_text=specials_scheme_text)
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
            # A large release's plan is completed from the deterministic skeleton BEFORE
            # anything reads it (HANDOFF 10.9): the model supplies what it can (the
            # slot-less movies/specials, titles, ids) and the harness fills every episode
            # destination from the computed slot. A partial prefix therefore stops being
            # a parked release and becomes a correct plan.
            if skeleton_path is not None:
                merge_unresolved = []
                try:
                    skeleton_data = json.loads(
                        skeleton_path.read_text(encoding="utf-8"))
                    plan, filled, unresolved = merge_skeleton_plan(plan, skeleton_data,
                                                                   _note)
                    merge_unresolved = list(unresolved or ())
                    if filled:
                        rationale = ((rationale or "")
                                     + f"\n\n[skeleton merge filled {filled} episode "
                                       f"destination(s) from the computed slots]")
                except (OSError, ValueError):
                    pass
                # AN UNRESOLVED RELEASE FILE IS A FIXABLE REJECTION, NOT A PARK
                # (HANDOFF 15.3, the systemic seam Family Guy and Friends share). The
                # model still gets to place the file it omitted; only when the whole
                # chain has seen the failure does the release park. The missing names
                # become the next attempt's `--require-list`, so ai_client names them
                # before the run ends instead of the wave parking after it.
                if merge_unresolved:
                    err, missing = _incomplete_plan_feedback(merge_unresolved)
                    _note(f"  {provider_name}/{model} plan incomplete: "
                          f"{len(merge_unresolved)} release file(s) unplaced "
                          f"({', '.join(missing[:4])}); handing to the next provider")
                    rejections.append({"provider": provider_name, "model": model,
                                       "plan": plan, "error": err})
                    _save_rejections(info_hash, [rejections[-1]])
                    last_err = err
                    if missing:
                        require_files = missing
                        try:
                            require_list.write_text(json.dumps(require_files),
                                                    encoding="utf-8")
                        except OSError:
                            require_files = []
                    break
            # An empty plan is a legitimate "nothing to place" verdict (repeat/extras), NOT
            # a fixable mistake — the caller decides how to treat it. Return it unchanged.
            # (With a skeleton, an empty model answer has just been replaced by the
            # skeleton's entries, so this only fires when the release truly has none.)
            if not plan.get("files"):
                globals()["_UNAVAILABLE_UNTIL"] = 0.0
                _clear_rejections(info_hash)
                return plan, rationale
            try:
                # Verify the model's provider ids BEFORE any guard uses them and before
                # apply can seed an nfo or fetch art (HANDOFF 10.3). A contradicted id
                # is stripped in place; the plan proceeds without it.
                library.verify_provider_ids(plan)
                library.validate_plan(plan, str(content_path),
                                      sibling_seasons=sibling_seasons,
                                      serial_map=serial_map,
                                      title_map=title_map or None,
                                      identity_map=identity_map or None,
                                      episode_agreement=episode_agreement or None)
                # An id the provider contradicted was stripped in place (10.3). Log it
                # loudly -- the plan is still good, but the next reader must not wonder
                # why the nfo has no provider id.
                for reason in plan.get("_id_rejections", ()):
                    _note(f"  provider id stripped: {reason}")
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
