"""Direct ingest -- file already-downloaded comics and novels into the library.

Not everything comes from a torrent. Comics from GetComics.com (and anywhere else) and
light novels / e-books from LibGen, the Internet Archive, or Anna's Archive arrive as
loose `.cbr`/`.cbz`/`.pdf`/`.epub` files — the owner drops them
straight into `config.DIRECT_INGEST_DIR` and this daemon files them. Each file is handed
to a headless AI run that decides where it belongs and what to call it, matching the
existing library's conventions, then placed through the exact same pipeline the torrent
flow uses: `identify.run_identify -> library.validate_plan -> library.apply_plan ->
library.verify_applied`.

Two destinations, one pipeline:

  * **Comics** (`.cbr`/`.cbz`/`.pdf`/`.zip`) land under `Comics/` in the media library
    (YACReader), and Media-Syncer uploads them to the MEGA pool as usual.
  * **Novels** (`.epub`) land in the Google Drive `Novels` folder (NOT YACReader) — the
    identify step plans them into a `Novels/` top-dir and `apply_plan` routes them to
    Google Drive instead of the media root.

No format conversion (`.cbr`/`.cbz`/`.pdf`/`.epub` are filed as-is; a plain `.zip` comic
archive is renamed to `.cbz` by the apply step). On success the source file is deleted
(`verify_applied` has confirmed the bytes are in the library/Google Drive, so it is a pure
duplicate). A file the identify step judges to have no library media (a redundant single
already inside a shelved collection) is also deleted — see `DELETE_SKIPPED`. Failures are
parked in a `.failed/` subdir with a `.error.txt` sidecar for inspection.

    python3 direct_ingest.py            # run the watch daemon
    python3 direct_ingest.py --once      # one scan pass then exit
"""
from __future__ import annotations

import argparse
import hashlib
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
import ingest                                                          # noqa: E402  (Jellyfin rescan helper)
import library                                                         # noqa: E402


WATCH_DIR = config.DIRECT_INGEST_DIR
FAILED_DIR = WATCH_DIR / ".failed"
SKIPPED_DIR = WATCH_DIR / ".skipped"

# True DELETES a file the identify step judged to have no library media in it (a single
# issue already inside a shelved collection, a variant-cover-only rip). A skipped file has
# NO library copy, so deletion is final — but "skipped" means the content is already
# shelved inside a collection or is not comic/novel content at all, so it is a redundant
# duplicate by definition. Set False to park in `.skipped/` for review instead.
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


def _find_media() -> list[Path]:
    """Loose comic/novel files awaiting ingest (skips the .failed/.skipped control dirs
    and any dot-file/partial)."""
    out = []
    for p in sorted(WATCH_DIR.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() in config.DIRECT_INGEST_EXTENSIONS:
            out.append(p)
    return out


def _is_stable(p: Path) -> bool:
    """True if the file's size held steady across STABLE_SEC (not still downloading)."""
    try:
        s1 = p.stat().st_size
    except OSError:
        return False
    time.sleep(STABLE_SEC)
    try:
        return p.exists() and p.stat().st_size == s1 and s1 > 0
    except OSError:
        return False


def _synth_id(p: Path) -> str:
    h = hashlib.sha1()
    h.update(p.name.encode("utf-8"))
    try:
        h.update(str(p.stat().st_size).encode("utf-8"))
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


def process(p: Path) -> bool:
    cid = _synth_id(p)
    log(f"Ingesting {p.name} (id {cid})...")
    try:
        plan, rationale = identify.run_identify(cid, str(p), log_fn=log)
        library.validate_plan(plan, str(p.parent))
    except identify.IdentifyUnavailable:
        # The API could not run. Nothing is wrong with this file, so it is NOT relocated —
        # it stays in the watch folder and the caller idles the scan until the window
        # reopens. Re-raised so scan_once() stops the batch rather than marching the rest
        # of the folder into the same closed door.
        raise
    except library.PlanError as exc:
        if "non-empty" not in str(exc):
            raise
        # An empty plan on a single archive is a verdict, not a breakdown: the run looked
        # at it against the library and found no media worth filing (a single issue already
        # inside a shelved collection, a variant-cover-only rip). Delete it.
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
    # the source file is now a duplicate sitting in the watch folder. Drop it.
    try:
        p.unlink()
        log(f"  removed source {p.name} (verified in destination)")
    except OSError as exc:
        log(f"  could not remove source {p.name}: {exc}; leaving for next pass")
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
            log(f"  {p.name} still changing (downloading?); skipping this pass")
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
    ap = argparse.ArgumentParser(description="Direct ingest daemon (comics + novels).")
    ap.add_argument("--once", action="store_true", help="one scan pass then exit")
    args = ap.parse_args()
    _ensure_dirs()
    log(f"direct_ingest watching {WATCH_DIR}")
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
