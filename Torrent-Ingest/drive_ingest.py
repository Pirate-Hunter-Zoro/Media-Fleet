"""Drive ingest -- auto-organize a newly-attached drive's pre-existing files.

Plug in a drive that already has media on it (a friend's flash drive, an old disk) and this
daemon organizes those loose files into the SAME `Media/{Shows,Movies,Comics}` layout and
naming scheme used everywhere else -- via the same headless AI identify pipeline the
torrent flow uses. Once organized under `<drive>/Media/`, the drive becomes a first-class
library drive: mediafs serves it, and Media-Syncer uploads its contents to the MEGA pool
(so it's backed up and survives losing the drive).

For each attached external volume each cycle:
  * find top-level entries holding loose media that is NOT already under `<drive>/Media/`
    (skips system/dot dirs; only ever touches media files -- never other data);
  * run `identify.run_identify` on each entry to get a placement plan;
  * MOVE each planned file to `<drive>/Media/<dst_rel>` (same-volume rename -- instant),
    then prune emptied source dirs;
  * drop a `.media-library` marker so discovery treats it as a library drive.

Safety:
  * Only MEDIA extensions are ever moved; all other files on the drive are left untouched.
  * A drive carrying a `.no-media-library` opt-OUT marker is skipped entirely (drop that file
    on any drive you do NOT want auto-organized/uploaded).
  * The boot volume and Time Machine backup disks are skipped.
  * Files are MOVED, never deleted; a crash mid-run re-converges on the next cycle.

    python3 drive_ingest.py            # run the watch daemon
    python3 drive_ingest.py --once      # one pass then exit
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import time
from pathlib import Path

# This repo's own directory goes FIRST on sys.path. Torrent-Ingest and Torrent-Searcher
# both ship modules named `config.py`, `library.py` and `ingest.py`, and both repos are on
# `sys.path` in some processes -- so a bare `import config` resolves to whichever repo the
# launcher happened to put first. That is how `directingest` died at import on 2026-08-27,
# reading Torrent-Ingest's `config` through Torrent-Searcher's `ingest` (§5 item 4a). The
# pin makes the resolution a property of the FILE rather than of how it was launched.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import config
import identify

VOLUMES = Path("/Volumes")
LIBRARY_SUBDIR = "Media"
MARKER = ".media-library"
OPT_OUT = ".no-media-library"
LOG_FILE = config.PROJECT_ROOT / "drive_ingest.log"
POLL_SEC = 60
STABLE_SEC = 10
MEDIA_EXTS = config.VIDEO_EXTENSIONS | config.COMIC_EXTENSIONS | config.COMIC_CONVERT_EXTENSIONS
SKIP_DIRS = {".Trashes", ".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100",
             ".TemporaryItems", ".vol", "Backups.backupdb", ".claude", LIBRARY_SUBDIR,
             "MediaStore"}   # MediaStore = a legacy already-organized library folder


def log(msg: str) -> None:
    line = f"[{config.log_stamp()}] {msg}"
    print(line, flush=True)
    try:
        config.rotate_log_if_large(LOG_FILE)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _external_volumes() -> list[Path]:
    out = []
    try:
        vols = list(VOLUMES.iterdir())
    except OSError:
        return out
    for v in vols:
        try:
            if not v.is_dir() or v.resolve() == Path("/"):
                continue                       # skip boot volume symlink ("Macintosh HD")
            if (v / "Backups.backupdb").exists():
                continue                       # Time Machine
            if (v / OPT_OUT).exists():
                continue                       # user opted this drive out
            # must be writable (we move files into it)
            if not os.access(v, os.W_OK):
                continue
            out.append(v)
        except OSError:
            continue
    return out


def _entries_to_organize(vol: Path) -> list[Path]:
    """Top-level entries on the drive that hold loose media not already under Media/."""
    out = []
    try:
        children = list(vol.iterdir())
    except OSError:
        return out
    for c in children:
        try:
            if c.name in SKIP_DIRS or c.name.startswith("."):
                continue
            if c.is_file():
                if c.suffix.lower() in MEDIA_EXTS:
                    out.append(c)
            elif c.is_dir():
                if _has_media(c):
                    out.append(c)
        except OSError:
            continue
    return out


def _has_media(d: Path) -> bool:
    for dp, dn, fns in os.walk(d):
        for n in fns:
            if os.path.splitext(n)[1].lower() in MEDIA_EXTS:
                return True
    return False


def _stable(p: Path) -> bool:
    try:
        s1 = _size(p)
        time.sleep(STABLE_SEC)
        return _size(p) == s1 and s1 > 0
    except OSError:
        return False


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _synth_id(p: Path) -> str:
    h = hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:16]
    return f"driveingest-{h}"


def _safe_move(src: Path, dst: Path) -> bool:
    """Move src -> dst, verifying size. Same-volume => instant rename; cross-dir on the same
    disk is still a rename. Never overwrites an existing correct file (idempotent re-runs)."""
    try:
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            src.unlink(missing_ok=True)         # already organized identically
            return True
        dst.parent.mkdir(parents=True, exist_ok=True)
        want = src.stat().st_size
        os.replace(src, dst)
        return dst.exists() and dst.stat().st_size == want
    except OSError as e:
        log(f"    move failed {src.name}: {e}")
        return False


def organize_volume(vol: Path) -> int:
    media_root = vol / LIBRARY_SUBDIR
    entries = _entries_to_organize(vol)
    if not entries:
        return 0
    log(f"organizing {len(entries)} entr(y/ies) on {vol}")
    moved = 0
    for entry in entries:
        if not _stable(entry):
            log(f"  {entry.name} still changing; skipping this pass")
            continue
        try:
            plan, _rat = identify.run_identify(_synth_id(entry), str(entry), log_fn=log)
        except identify.IdentifyUnavailable as exc:
            # Usage window exhausted: stop this volume's pass entirely rather than marching
            # the remaining entries into the same closed door. Nothing is moved and no
            # `.media-library` marker is dropped, so the next pass re-organizes this drive
            # from exactly where it left off.
            log(f"  identify API unavailable; deferring {entry.name} and the rest of "
                f"this volume ({exc})")
            return moved
        except Exception as exc:                                       # noqa: BLE001
            log(f"  identify failed for {entry.name}: {exc}")
            continue
        for f in plan.get("files", []):
            src = Path(f.get("src", ""))
            dst_rel = str(f.get("dst_rel", "")).lstrip("/")
            top = dst_rel.split("/", 1)[0] if "/" in dst_rel else ""
            if top not in ("Shows", "Movies", "Comics"):
                log(f"  bad dst_rel (skipped): {dst_rel!r}")
                continue
            if not src.is_file() or src.suffix.lower() not in MEDIA_EXTS:
                continue
            if VOLUMES not in src.resolve().parents:
                continue                                              # never move off-volume srcs
            dst = media_root / dst_rel
            if _safe_move(src, dst):
                moved += 1
                log(f"  organized -> Media/{dst_rel}")
        _prune_empty(entry)
    # mark it a library drive so discovery + serving + upload pick it up
    try:
        media_root.mkdir(parents=True, exist_ok=True)
        (media_root / MARKER).touch()
    except OSError:
        pass
    if moved:
        log(f"organized {moved} file(s) into {media_root}")
    return moved


def _prune_empty(path: Path) -> None:
    """Remove now-empty source dirs left after moving files out (bottom-up)."""
    if not path.is_dir():
        return
    for dp, dn, fns in os.walk(path, topdown=False):
        p = Path(dp)
        try:
            if not any(x for x in p.iterdir() if x.name != ".DS_Store"):
                for junk in p.glob(".DS_Store"):
                    junk.unlink(missing_ok=True)
                p.rmdir()
        except OSError:
            pass


def scan_once() -> int:
    total = 0
    for vol in _external_volumes():
        try:
            total += organize_volume(vol)
        except Exception as exc:                                       # noqa: BLE001
            log(f"error organizing {vol}: {exc}")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Auto-organize attached drives' pre-existing media.")
    ap.add_argument("--once", action="store_true", help="one pass then exit")
    args = ap.parse_args()
    log(f"drive_ingest watching {VOLUMES} (organizes loose media into <drive>/{LIBRARY_SUBDIR}/)")
    if args.once:
        scan_once()
        return 0
    while True:
        try:
            scan_once()
        except Exception as exc:                                       # noqa: BLE001
            log(f"scan error (continuing): {exc}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
