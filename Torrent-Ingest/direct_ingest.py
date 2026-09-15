"""Direct ingest -- file already-downloaded media into the library, torrent or not.

Not everything comes from a torrent. Comics from GetComics.com (and anywhere else),
light novels / e-books from LibGen / the Internet Archive / Anna's Archive, and -- since
2026-09-13 -- RAW VIDEO (a movie or episode downloaded directly) arrive as loose files
or folders. The owner drops them straight into `config.DIRECT_INGEST_DIR`, or into the
iCloud mirror `config.ICLOUD_DIRECT_INGEST_DIR` that `direct_ingest_bridge.py` empties
into it, and this daemon files them. Each drop is handed to a headless AI run that
decides where it belongs and what to call it, matching the existing library's
conventions, then placed through the exact same pipeline the torrent flow uses:
`identify.run_identify -> library.validate_plan -> library.apply_plan ->
library.verify_applied`.

Destinations, one pipeline:

  * **Comics** (`.cbr`/`.cbz`/`.pdf`/`.zip`) land under `Comics/` in the media library
    (YACReader), and Media-Syncer uploads them to the MEGA pool as usual.
  * **Novels** (`.epub`, and `.pdf` planned as a novel) land in the Google Drive
    `Novels` folder (NOT YACReader) -- the identify step plans them into a `Novels/`
    top-dir and `apply_plan` routes them to Google Drive instead of the media root.
  * **Video** (`.mkv`/`.mp4`/`.avi`/`.m4v`/`.mov`) lands in `Shows/` (with the same
    `.nfo` handling a torrent gets, plus a Jellyfin rescan) or `Movies/`. A dropped
    DIRECTORY is one identify run over the whole tree, exactly like a torrent's
    download directory.

No format conversion (`.cbr`/`.cbz`/`.pdf`/`.epub` are filed as-is; a plain `.zip`
comic archive is renamed to `.cbz` by the apply step). On success the source bytes are
deleted (`verify_applied` has confirmed every planned file is in the library/Google
Drive, so they are pure duplicates). Failures are parked in a `.failed/` subdir with a
`.error.txt` sidecar for inspection.

An empty plan is READ BY DROP TYPE, and the distinction is deliberate:

  * Over a single **archive** it is a verdict -- a single issue already inside a shelved
    collection, or a variant-cover-only rip, genuinely has no library media. It is
    deleted (`DELETE_SKIPPED`; set False to park in `.skipped/` instead).
  * Over **video or a directory** it is NOT taken on the model's word. The file is the
    only local copy and a free model can simply return nothing (the "That 90s Show"
    incident). It is deleted only when the library can PROVE the content is already
    present (`_empty_plan_is_proven`); otherwise it parks in `.failed/` for review.

    python3 direct_ingest.py            # run the watch daemon
    python3 direct_ingest.py --once      # one scan pass then exit
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

# THIS REPO'S DIRECTORY GOES FIRST ON sys.path, before anything else is imported.
#
# Torrent-Ingest and Torrent-Searcher both contain modules named `config.py`, `library.py`
# and `ingest.py`, and both repos land on `sys.path` in some processes -- so a bare
# `import ingest` resolves to whichever repo happens to come first, which is an accident of
# how the process was launched. On 2026-08-27 it resolved to the SEARCHER's `ingest`, which
# imports `discovery`, which reads `config.DISCOVERY_MAX_TOKENS` -- a setting this repo's
# `config` does not have -- and this daemon died at import with an AttributeError
# (~/Library/Logs/DirectIngest.err). Nothing was changed at the time because it had stopped
# failing on its own, so it stayed latent, waiting for the import order to shift back.
#
# Pinning the directory beats importing the one known-colliding module by path: it fixes
# the whole class at once (`config` and `library` collide too), and it keeps module
# IDENTITY intact -- a path-loaded `ingest` would hold a DIFFERENT `config` object than the
# one `identify` and `library` see, which is a subtler version of the same bug.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                                                          # noqa: E402
import identify                                                        # noqa: E402
import ingest                                                          # noqa: E402  (Jellyfin rescan + proofs)
import library                                                         # noqa: E402


WATCH_DIR = config.DIRECT_INGEST_DIR
FAILED_DIR = WATCH_DIR / ".failed"
SKIPPED_DIR = WATCH_DIR / ".skipped"

# True DELETES an ARCHIVE the identify step judged to have no library media in it (a
# single issue already inside a shelved collection, a variant-cover-only rip). A skipped
# archive has NO library copy, so deletion is final — but "skipped" means the content is
# already shelved inside a collection or is not comic/novel content at all, so it is a
# redundant duplicate by definition. Set False to park in `.skipped/` for review instead.
# VIDEO never follows this verdict: see `_empty_plan_is_proven`.
DELETE_SKIPPED = True

LOG_FILE = config.PROJECT_ROOT / "direct_ingest.log"
POLL_SEC = 20
STABLE_SEC = 8          # a file must be size-stable this long before we touch it


def log(msg: str) -> None:
    line = f"[{config.log_stamp()}] {msg}"
    print(line, flush=True)
    try:
        config.rotate_log_if_large(LOG_FILE)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _ensure_dirs() -> None:
    for d in (WATCH_DIR, FAILED_DIR, SKIPPED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _is_control_dir(p: Path) -> bool:
    """The daemon's own subfolders, which are never ingest work."""
    return p.name in (".failed", ".skipped", ".icloud-bridge")


def _has_ingestible(d: Path) -> bool:
    """Whether a dropped directory holds anything the library can take: a media
    extension, or loose page images the identify step can package into a `.cbz`."""
    for _dp, _dn, fns in os.walk(d):
        for n in fns:
            if n.startswith("._") or n == ".DS_Store":
                continue
            ext = os.path.splitext(n)[1].lower()
            if ext in config.DIRECT_INGEST_EXTENSIONS \
                    or ext in config.LOOSE_PAGE_EXTENSIONS:
                return True
    return False


def _find_media() -> list[Path]:
    """Loose files and directories awaiting ingest (skips the .failed/.skipped control
    dirs, the bridge's scratch dir, and AppleDouble/DS_Store junk).

    A dot-prefixed MEDIA file is occasionally a real release title
    (`.Planetes.2003...`), so it is picked up; the hidden things that must stay out are
    the control DIRECTORIES, and those are hidden by name, not by the dot alone."""
    out = []
    try:
        children = sorted(WATCH_DIR.iterdir())
    except OSError:
        return out
    for p in children:
        if p.name == ".DS_Store" or p.name.startswith("._"):
            continue
        if p.is_file():
            if p.suffix.lower() in config.DIRECT_INGEST_EXTENSIONS:
                out.append(p)
        elif p.is_dir() and not p.is_symlink() and not _is_control_dir(p) \
                and _has_ingestible(p):
            out.append(p)
    return out


def _tree_size(p: Path) -> int:
    """A file's size, or a directory's recursive byte total. This is what stability is
    measured on for a folder drop: a season still copying in grows."""
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _is_stable(p: Path) -> bool:
    """True if the file's (or directory tree's) size held steady across STABLE_SEC
    (not still downloading/copying)."""
    try:
        s1 = _tree_size(p)
    except OSError:
        return False
    time.sleep(STABLE_SEC)
    try:
        return p.exists() and _tree_size(p) == s1 and s1 > 0
    except OSError:
        return False


def _synth_id(p: Path) -> str:
    h = hashlib.sha1()
    h.update(p.name.encode("utf-8"))
    try:
        h.update(str(_tree_size(p)).encode("utf-8"))
    except OSError:
        pass
    return "directingest-" + h.hexdigest()[:16]


def _relocate(p: Path, dest_dir: Path) -> Path:
    """Move a failed source out of the watch folder, uniquifying on collision."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / p.name
    n = 1
    while dst.exists():
        dst = dest_dir / f"{p.stem}.{n}{p.suffix}"
        n += 1
    shutil.move(str(p), str(dst))
    return dst


def _attach_video_sidecars(plan: dict, p: Path) -> None:
    """Deterministically file a loose video's subtitle siblings.

    The identify run is handed the video FILE, so it cannot see the `.srt` sitting
    beside it, and those subtitles would otherwise be stranded in the watch folder
    forever. Their destination needs no model -- Jellyfin pairs a subtitle with its
    video by base name, so it is the video's own planned destination with the
    subtitle's name tail kept intact (`.srt` beside `Movie.mkv` -> `Movie.srt`;
    `Movie.en.srt` keeps its language tag -> `Movies/Movie.en.srt`). That is the house
    rule (compute arithmetic, do not ask the model for it) and the single-file analogue
    of `fastpath`'s sidecar pairing for a download directory.

    The match is a PREFIX, which is exactly how Jellyfin pairs them: the subtitle's
    name must begin with the video's stem. So `Show.S01E02.srt` is NOT attached to
    `Show.S01E01.mkv`, while `Show.S01E01-E02.srt` is (it covers E01 too). Only for a
    single-file drop: a DIRECTORY drop is listed to the run whole, so the run itself
    has already planned (or deliberately declined) everything inside it.
    """
    if not p.is_file() or p.suffix.lower() not in config.VIDEO_EXTENSIONS:
        return
    try:
        siblings = [s for s in sorted(p.parent.iterdir())
                    if s.is_file() and len(s.name) > len(p.stem)
                    and s.name.startswith(p.stem)
                    and s.suffix.lower() in config.SUBTITLE_EXTENSIONS]
    except OSError:
        return
    if not siblings:
        return
    video_dsts = []
    already = set()
    for f in (plan.get("files") or []):
        dst = str(f.get("dst_rel") or "")
        if not dst:
            continue
        already.add(dst)
        if Path(dst).suffix.lower() in config.VIDEO_EXTENSIONS:
            video_dsts.append(dst)
    for s in siblings:
        if str(s) in {str(f.get("src")) for f in (plan.get("files") or [])}:
            continue                                   # already planned; do not duplicate
        for dst in video_dsts:
            dst_p = Path(dst)
            new_rel = str(dst_p.with_name(dst_p.stem + s.name[len(p.stem):]))
            if new_rel in already:
                continue
            already.add(new_rel)
            plan.setdefault("files", []).append({"src": str(s), "dst_rel": new_rel})
            break                                       # one video -> one sidecar each


def _movie_in_library(name: str) -> bool:
    """Whether a film matching `name` is already owned -- locally or in the pool.

    The pool half matters: the tier engine evicts local copies freely, so checking the
    local tree alone would call an owned film absent and park a redundant re-drop.
    Titles are folded with `ingest._show_norm` (quality tags and the trailing year
    stripped), the same fold the episode proof uses.
    """
    norm = ingest._show_norm(name)
    if not norm:
        return False
    try:
        if config.MOVIES_ROOT.is_dir():
            for child in config.MOVIES_ROOT.iterdir():
                stem = child.stem if child.is_file() else child.name
                if ingest._show_norm(stem) == norm:
                    return True
    except OSError:
        pass
    try:
        import reconcile
        for rel in (reconcile._remote_keys() or ()):
            parts = str(rel).split("/")
            if len(parts) >= 2 and parts[0] == "Movies" \
                    and ingest._show_norm(Path(parts[-1]).stem) == norm:
                return True
    except Exception:                                              # noqa: BLE001
        pass
    return False


def _empty_plan_is_proven(p: Path) -> tuple[bool, str]:
    """For an empty plan over VIDEO or a DIRECTORY, decide whether the "already in the
    library" claim is PROVEN. Returns (proven, detail). Never raises.

    WHY THIS IS NOT `_empty_plan_already_present`. That helper (the torrent path's)
    treats "could not check" as "fine" -- no episode tag, or a show the library does
    not hold, passes. A torrent has a `.torrent` left for a retry and a journal record;
    a direct drop does not. So here the bar is inverted: only POSITIVE evidence that
    every episode (or the film) is already shelved counts. Everything else parks in
    `.failed/`, where a human can see it, rather than being deleted on a model's word.

    Evidence used, both witnesses the torrent proof already trusts:
      * the download's own SxxExx names (or a film title), and
      * the library's episode set / film titles, from local tree + remote inventory.
    """
    try:
        want = ingest._episode_keys_in_download(p)
        name = p.stem if p.is_file() else p.name
        if want:
            show = ingest._show_norm(name)
            if not show:
                return False, f"no show name derivable from {name!r}"
            have = ingest._episode_keys_in_library(show)
            if not have:
                return False, f"no library episodes found for {show!r} to compare against"
            missing = sorted(want - have)
            if missing:
                shown = ", ".join(f"S{s:02d}E{e:02d}" for s, e in missing[:8])
                return False, (f"{len(missing)} of {len(want)} episode(s) are NOT in the "
                               f"library ({shown}{' ...' if len(missing) > 8 else ''})")
            return True, f"all {len(want)} episode(s) already in the library"
        if p.is_file() and _movie_in_library(p.stem):
            return True, "the film is already in the library"
        return False, "no SxxExx tag and no matching film title in the library"
    except Exception as exc:                                       # noqa: BLE001
        return False, f"proof could not be evaluated: {exc}"


def _remove_planned_sources(plan: dict, p: Path) -> int:
    """Delete the source bytes `apply_plan`/`verify_applied` just proved are in the
    destination. Every planned FILE (a loose video plus its attached subtitles, or a
    torrent-shaped folder) is removed; a planned DIRECTORY of loose pages is removed
    wholesale (apply zipped it into its `.cbz`). A directory drop's unplanned leftovers
    are NOT touched here. Returns the number of sources removed.
    """
    removed = 0
    for f in (plan.get("files") or []):
        src = Path(str(f.get("src") or ""))
        try:
            if src.is_file():
                src.unlink(missing_ok=True)
                removed += 1
            elif src.is_dir():
                shutil.rmtree(src, ignore_errors=True)
                removed += 1
        except OSError as exc:
            log(f"  could not remove source {src}: {exc}")
    if p.is_file() and p.exists():
        # A single-file drop whose plan somehow forgot it: never leave the watch entry
        # behind once apply+verify have passed.
        p.unlink()
        removed += 1
    if p.is_dir():
        _prune_empty_dirs(p)
    return removed


def _remove_watch_entry(p: Path) -> int:
    """Delete a whole source entry (an empty plan proven already-present)."""
    if p.is_file():
        p.unlink()
        return 1
    n = sum(1 for f in p.rglob("*") if f.is_file())
    shutil.rmtree(p)
    return n


def _prune_empty_dirs(path: Path) -> None:
    """Remove now-empty subdirs (bottom-up), and `path` itself if fully emptied."""
    for dp, _dn, _fns in os.walk(path, topdown=False):
        p = Path(dp)
        try:
            if not any(x for x in p.iterdir() if x.name != ".DS_Store"):
                for junk in p.glob(".DS_Store"):
                    junk.unlink(missing_ok=True)
                p.rmdir()
        except OSError:
            pass


def process(p: Path) -> bool:
    cid = _synth_id(p)
    # For a FILE the content root is its parent (the plan may legitimately name a
    # sidecar sibling); for a DIRECTORY the root is the directory itself.
    content_root = p if p.is_dir() else p.parent
    log(f"Ingesting {p.name} (id {cid})...")
    try:
        plan, rationale = identify.run_identify(cid, str(p), log_fn=log)
        _attach_video_sidecars(plan, p)
        # An empty plan is normalised to the validator's own message so the verdict
        # handling below sees EVERY empty-plan shape (a model may write `{}`, omit
        # `files`, or write `files: []`) -- `run_identify` returns those unvalidated.
        if not plan.get("files"):
            raise library.PlanError("plan.files must be a non-empty list")
        library.validate_plan(plan, str(content_root))
    except identify.IdentifyUnavailable:
        # The API could not run. Nothing is wrong with this file, so it is NOT relocated —
        # it stays in the watch folder and the caller idles the scan until the window
        # reopens. Re-raised so scan_once() stops the batch rather than marching the rest
        # of the folder into the same closed door.
        raise
    except library.PlanError as exc:
        if "non-empty" not in str(exc):
            raise
        # An empty plan. Archives keep the historical verdict handling; video and
        # directories require positive proof before anything is deleted.
        if p.is_file() and p.suffix.lower() not in config.VIDEO_EXTENSIONS:
            if DELETE_SKIPPED:
                try:
                    p.unlink()
                    log(f"  no library media in {p.name}; deleted ({exc})")
                    return False
                except OSError as e:
                    log(f"  could not delete {p.name}: {e}; parking in .skipped/")
            else:
                log(f"  no library media in {p.name}; skipping ({exc})")
            _relocate(p, SKIPPED_DIR)
            return False
        proven, detail = _empty_plan_is_proven(p)
        if proven:
            try:
                _remove_watch_entry(p)
                log(f"  {p.name} is already in the library ({detail}); deleted source")
                return False
            except OSError as e:
                log(f"  {p.name} is already in the library ({detail}) but could not be "
                    f"deleted: {e}; leaving for next pass")
                return False
        log(f"  empty plan over {p.name} is NOT proven already-present ({detail}); "
            f"parking in .failed/ for review")
        _relocate(p, FAILED_DIR)
        try:
            (FAILED_DIR / (p.name + ".error.txt")).write_text(
                f"empty plan (the model returned no files). Positive proof it is "
                f"already in the library was required and not found: {detail}\n\n{exc}",
                encoding="utf-8")
        except OSError:
            pass
        return False
    except Exception as exc:                                       # noqa: BLE001
        log(f"  identify/validate failed for {p.name}: {exc}")
        _relocate(p, FAILED_DIR)
        try:
            (FAILED_DIR / (p.name + ".error.txt")).write_text(str(exc), encoding="utf-8")
        except OSError:
            pass
        return False
    try:
        applied = library.apply_plan(plan, cid)
        ok, msg = library.verify_applied(applied)
        if not ok:
            raise RuntimeError(f"verification failed: {msg}")
    except Exception as exc:                                       # noqa: BLE001
        log(f"  apply/verify failed for {p.name}: {exc}")
        _relocate(p, FAILED_DIR)
        return False

    # A verified manga volume retires the chapters its cached map covers. Best-effort,
    # cache-only, and never able to fail the filing (ingest._manga_chapter_reconcile
    # swallows every failure into a log line).
    try:
        ingest._manga_chapter_reconcile(plan)
    except Exception:                                              # noqa: BLE001
        pass

    dsts = [a.get("dst") for a in applied]
    log(f"  filed {p.name} -> {', '.join(str(d) for d in dsts)}")
    # Comics go to YACReader (its own scan) and novels to Google Drive — neither needs a
    # Jellyfin rescan. But a mixed drop could include video, so rescan if anything went to
    # Shows/ or Movies/.
    if any(str(a.get("dst", "")).find("/Shows/") >= 0
           or str(a.get("dst", "")).find("/Movies/") >= 0 for a in applied):
        try:
            ingest._jellyfin_rescan()
        except Exception:                                          # noqa: BLE001
            pass
    # verify_applied() has confirmed every planned file is present in its destination, so
    # the source bytes are now a duplicate. Drop the placed ones.
    try:
        n = _remove_planned_sources(plan, p)
        log(f"  removed source {p.name} ({n} file(s) verified in destination)")
    except OSError as exc:
        log(f"  could not fully remove source {p.name}: {exc}; leaving for next pass")
    # A directory drop can still exist after its planned files are gone: the run
    # deliberately declined something (a sample, a creditless OP), or clutter it was
    # never asked to place. That is not a failure, but it is not ingest work any more
    # either, so park the remainder in `.skipped/` for a human instead of letting it
    # sit in the watch folder forever.
    if p.is_dir() and p.exists():
        log(f"  {p.name} still holds unplanned file(s); parking the remainder in .skipped/")
        _relocate(p, SKIPPED_DIR)
    return True


def scan_once() -> int:
    """One pass over the watch folder. Returns files filed, or -1 if the pass stopped early
    because the identify API is unavailable (the caller then backs off)."""
    _ensure_dirs()
    n = 0
    for p in _find_media():
        if not p.exists():
            continue
        if not _is_stable(p):
            log(f"  {p.name} still changing (downloading/copying?); skipping this pass")
            continue
        try:
            if process(p):
                n += 1
        except identify.IdentifyUnavailable as exc:
            log(f"  identify API unavailable; deferring {p.name} and the rest of this "
                f"pass ({exc})")
            return -1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Direct ingest daemon (comics, novels, video).")
    ap.add_argument("--once", action="store_true", help="one scan pass then exit")
    args = ap.parse_args()
    _ensure_dirs()
    log(f"direct_ingest watching {WATCH_DIR} "
        f"(comics, novels, video; files and folders)")
    if args.once:
        scan_once()
        return 0
    while True:
        deferred = False
        try:
            deferred = scan_once() < 0
        except Exception as exc:                                   # noqa: BLE001
            log(f"scan error (continuing): {exc}")
        time.sleep(config.IDENTIFY_UNAVAILABLE_BACKOFF_SEC if deferred else POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
