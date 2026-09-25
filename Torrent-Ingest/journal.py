"""Append-only, crash-safe journal for the per-torrent state machine.

The journal is the single source of truth for what has happened to every
torrent. It is a JSON-lines file: each line is a full snapshot of one torrent's
record, keyed by its info hash. The *last* line for a given hash wins, so
recovering current state is just "replay the file, last-writer-wins."

Why last-writer-wins append instead of rewriting a dict: an append is atomic
enough that a crash mid-write loses at most the one line being written, never
the history. On restart we can see exactly how far each torrent got and resume
from there rather than re-downloading.

Alongside the machine-readable journal we keep `decisions.log`, a human-readable
record of every identification call the AI makes and why — the "record the calls
you make" requirement. Both files live under state/ and are gitignored.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import config

# Canonical state-machine stages, in order. A torrent advances through these;
# the destructive cleanup only runs once it reaches VERIFIED.
QUEUED = "queued"            # .torrent seen, not yet added to qBittorrent
DOWNLOADING = "downloading"  # added to qBittorrent, transferring
DOWNLOADED = "downloaded"    # transfer complete, files on local disk
IDENTIFIED = "identified"    # the AI run produced a validated placement plan
STAGED = "staged"            # files copied into the the SSD library root staging dir
VERIFIED = "verified"        # files confirmed in final place by size
COMPLETED = "completed"      # local copy + .torrent deleted; done
FAILED = "failed"            # gave up; nothing destructive was done
REFUSED = "refused"          # DECLINED ON PURPOSE; not a failure at all

# `refused` exists because the owner looked at 71 records sitting at `failed` and said
# "a bunch of torrents failed and I don't like that" -- and 13 of those 71 were the system
# working exactly as designed (§4.88): five redundant encodes of a film already held, a
# season pack superseded by a complete pack already downloading, a 480p rip of a season
# owned in full, an Italian-only release the language filter caught. Filing a deliberate
# refusal under the same word as a dead swarm makes the healthy count look alarming and
# buries the failures that ARE real. Both are terminal and neither touches the library;
# they differ only in whether a human should care, which is precisely what a status is for.
REFUSAL_STATUSES = {REFUSED}
# Everything that is over and will not advance again, whichever way it ended.
TERMINAL = {VERIFIED, COMPLETED, FAILED, REFUSED}

# Stages from which it is safe to delete the source: the files provably exist in
# their final library location.
TERMINAL_OK = {VERIFIED, COMPLETED}


def _now():
    return datetime.now(timezone.utc).isoformat()


def load_records():
    """Replay the journal into {info_hash: record}, last-writer-wins."""
    records = {}
    if not config.JOURNAL_FILE.exists():
        return records
    with config.JOURNAL_FILE.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a torn final line from a crash
            h = rec.get("info_hash")
            if h:
                records[h] = rec
    return records


_TITLE_TAG_RE = re.compile(
    r"[Ss]\d{1,3}[Ee]\d{1,4}\s*[\(\[]([^\)\]]{2,90}?)"
    r"(?:[\)\]]|\.(?:mp4|mkv|avi|m4v|mov)$)", re.I)


def title_from_release_name(name):
    """The episode title a release SOURCE filename states, or "".

    Both shapes the fleet's packs use: `The Smurfs S01E06 (The Astrosmurf).mp4` and the
    dvdrip form `The Smurfs S01E01 -The Smurfette.mkv`. The title a plan filed FROM is
    content identity evidence the destination sidecar cannot carry when two files share
    a stem (`E07.mkv` + `E07.mp4` share one `.nfo`).
    """
    s = str(name or "")
    m = _TITLE_TAG_RE.search(s)
    if m:
        return m.group(1).strip()
    m = re.search(r"[Ss]\d{1,3}[Ee]\d{1,4}\s*[-–]\s*(.+?)"
                  r"\.(?:mp4|mkv|avi|m4v|mov)$", s)
    return m.group(1).strip() if m else ""


# An ALTERNATE-VERSION parenthetical: a release tag that says "same episode, different
# cut", never a different episode. `(Part 2)`, `(II)`, `(Season 3)` and a bare `(1)` are
# deliberately absent -- those keep distinct cores, which is what stops a two-part story
# or two numbered episodes from collapsing (HANDOFF 15.2: II vs III must NOT collapse).
_ALT_VERSION_RE = re.compile(
    r"(?i)\b(uncensored|unrated|censored|extended|commentar\w*|director'?s? ?cut|"
    r"remaster(?:ed)?|alternate|\balt\.? ?(?:scene|cut|audio|track)|deleted ?scene|"
    r"bale ?scene|dual ?audio|multi ?audio|re-?encode)\b")


def alternate_title_core(name):
    """The episode title of one cut, version tags stripped, or "" when unreadable.

    Two files at one release `SxxEyy` whose names carry the SAME core are alternate
    versions of ONE episode -- Family Guy's `(Uncensored + Bale Scene)` beside
    `(Uncensored + Commentary Audio Track)` (HANDOFF 15.2). The harness files them at
    one destination and drops the ranked sibling into `_deduped_dropped`, so the
    coverage contract counts the dropped copy as accounted-for instead of parking the
    whole release.

    The strip is deliberately narrow (release version markers only, and only a trailing
    parenthetical), so `II` vs `III` and `Part 1` vs `Part 2` never collapse.
    """
    title = title_from_release_name(Path(name).name)
    core = title
    while True:
        m = re.search(r"\s*[\(\[]([^)\]]*)[\)\]]\s*$", core)
        if not m or not _ALT_VERSION_RE.search(m.group(1)):
            break
        core = core[:m.start()].strip()
    return re.sub(r"[^a-z0-9]+", " ", core.lower()).strip()


_SOURCE_TITLES_CACHE: dict = {}


def source_titles():
    """`{library-relative dst: episode title the plan filed it from}`.

    THE SMURFS LOSS THIS EXISTS FOR (2026-09-20). Two containers for one slot share ONE
    `.nfo` stem, so the sidecar named the slot's correct episode for both files and a
    duplicate cleanup deleted the planner's copy at 38 S01 slots while the older
    wrong-slot file survived (HANDOFF 10.9). The journal is the third witness: each plan
    records the SOURCE filename a destination was filed from, and the source title is
    what the bytes actually contain. Rebuilt when the journal changes.

    THE CHUNKED BLIND SPOT THIS CLOSES (2026-09-24). A chunked wave files its episodes
    with `plan` null on the record, so a witness that read only `plan.files` was blind
    to every file a wave had filed -- exactly the files most likely to collide with the
    next wave. The wave's `applied` entries carry the staging source path, which
    preserves the release filename the bytes came from; they are indexed here too. It
    is what tells The Simpsons' S03E03 (`When Flanders Failed`, filed by wave 22) apart
    from the wave-40 plan's `Homer Defined`, so the collision guard can refuse the plan
    instead of dropping a 700 GB release's file.
    """
    try:
        st = config.JOURNAL_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _SOURCE_TITLES_CACHE.get("key") == key:
        return _SOURCE_TITLES_CACHE.get("map") or {}
    out = {}
    for rec in load_records().values():
        for f in (rec.get("plan") or {}).get("files") or []:
            d, s = f.get("dst_rel"), f.get("src")
            if not d or not s:
                continue
            title = title_from_release_name(str(s).rsplit("/", 1)[-1])
            if title:
                out.setdefault(str(d), title)
        for a in rec.get("applied") or []:
            if not isinstance(a, dict):
                continue
            d, s = a.get("dst"), a.get("src")
            if not d or not s:
                continue
            rel = _library_relative(d)
            if not rel:
                continue
            title = title_from_release_name(str(s).rsplit("/", 1)[-1])
            if title:
                out.setdefault(rel, title)
    _SOURCE_TITLES_CACHE["key"] = key
    _SOURCE_TITLES_CACHE["map"] = out
    return out


def _library_relative(path):
    """`path` relative to MEDIA_ROOT, or None when it lives outside it (a Novels/ or
    extra destination). `applied` records absolute destinations; the identity witness
    indexes library-relative paths, the form the plan side also uses."""
    roots = (config.MEDIA_ROOT.resolve(), config.MEDIA_ROOT)
    for root in roots:
        try:
            return str(Path(path).relative_to(root))
        except (ValueError, OSError):
            continue
    return None


def write_record(record):
    """Append a full snapshot of one torrent's record."""
    record = dict(record)
    record["updated_at"] = _now()
    config.JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with config.JOURNAL_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def new_record(info_hash, torrent_path, name=None):
    """Create the initial QUEUED record for a freshly seen .torrent."""
    return {
        "info_hash": info_hash,
        "name": name or "",
        "torrent_path": str(torrent_path),   # source .torrent in iCloud
        "status": QUEUED,
        "created_at": _now(),
        "content_path": None,                # where qBittorrent put the download
        "plan": None,                        # validated placement plan
        "applied": [],                       # [{src, dst}] actually moved into place
        "error": None,
    }


def compact_if_needed():
    """Rewrite the journal as exactly one line per torrent, dropping superseded snapshots.

    Append-with-last-writer-wins is what makes the journal crash-safe, but it means a
    torrent that is re-snapshotted every cycle (a DEFERRED one waiting on disk space, say)
    writes a line per cycle forever. On 2026-08-06 the file was 83.6 MB of 8,899 lines
    describing 589 torrents -- 15 snapshots each on average, and 3,942 for the worst one.

    Compaction is LOSSLESS with respect to the state machine, which is the only reason it
    is safe to do to this file at all: load_records() already keeps just the last line per
    info hash, so writing exactly what it computed preserves the resumable state bit for
    bit. What it discards is the intermediate transition history, which no code reads --
    the audit trail people actually read is decisions.log, and that is never touched.

    EVERY info hash is kept, including `completed` and `failed` ones. Do not be tempted to
    prune terminal records by age: a completed record is what tells a re-seen .torrent that
    its work is already done, and dropping it invites a re-ingest of content that is
    already in the library.

    Safety is verify-then-swap rather than keep-a-backup: the temp file is written, fsynced,
    re-parsed, and its replayed state compared against what the live file replayed to. Only
    an exact match is swapped in, via os.replace (atomic on POSIX). Any mismatch or error
    leaves the original journal completely untouched -- the worst case is a stale temp file
    and a journal that keeps growing, never a journal that lost a record.

    Returns:
        dict | None: stats when a compaction happened, else None
    """
    path = config.JOURNAL_FILE
    try:
        # Cheap guard first: the common case must cost one stat(), not a parse of 80 MB.
        if not path.exists():
            return None
        size_before = path.stat().st_size
        if size_before < config.JOURNAL_COMPACT_MIN_BYTES:
            return None

        records = {}
        lines = 0
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                lines += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue          # same tolerance load_records() has for a torn line
                h = rec.get("info_hash")
                if h:
                    records[h] = rec
        if not records:
            return None
        # Only worth the rewrite if snapshots actually outnumber torrents.
        if lines < len(records) * config.JOURNAL_COMPACT_MIN_RATIO:
            return None

        tmp = path.with_suffix(path.suffix + ".compacting")
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in records.values():
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

        # Verify the rewrite replays to precisely the same state before trusting it.
        replayed = {}
        with tmp.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                replayed[rec["info_hash"]] = rec
        if replayed != records:
            tmp.unlink(missing_ok=True)
            return None

        os.replace(tmp, path)
        return {
            "bytes_before": size_before,
            "bytes_after": path.stat().st_size,
            "lines_before": lines,
            "records": len(records),
        }
    except (OSError, ValueError):
        # Never let journal maintenance take the daemon down. A failed compaction just
        # means the file stays large, which is survivable; a half-written journal is not.
        try:
            path.with_suffix(path.suffix + ".compacting").unlink(missing_ok=True)
        except OSError:
            pass
        return None


def log_decision(info_hash, name, text):
    """Append a human-readable block to decisions.log."""
    config.DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    header = f"\n===== {_now()}  [{name or info_hash[:12]}]  ({info_hash}) =====\n"
    with config.DECISIONS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(header)
        fh.write(text.rstrip() + "\n")
