"""iCloud bridge for direct ingest -- drain the Torrents/DirectIngest drop folder.

`iCloud Drive/Torrents/` is the fleet's cross-device drop point. `.torrent` files at
its top level are the torrent pipeline's admission path; since 2026-09-13 a
`Torrents/DirectIngest/` subfolder is the same drop point for RAW media -- a loose
`.mkv`/`.mp4`, a `.cbz`/`.epub`, or a folder of them -- for when the owner downloads
something directly instead of handing over a `.torrent`.

This daemon watches that subfolder and MOVES each drop onto the local
`~/Downloads/DirectIngest/`, where `direct_ingest.py` files it through the normal
`identify -> validate -> apply -> verify` pipeline. The iCloud folder is a drop point,
not a second library: once the local copy is verified the iCloud copy is deleted, so
the folder empties and the drop does not keep reappearing on other devices.

What "seen here" has to mean, and why:

  * **iCloud may hand the drop over as a plain file OR a dataless placeholder**
    (`.Name.mkv.icloud`). A placeholder is materialized with `brctl download` and then
    WAITED for: iCloud returns from the command long before the bytes are local.
  * **A drop can be mid-sync.** A file (or folder) whose size is still changing is left
    alone until it holds still for `STABLE_SEC`, so a half-synced file is never copied
    and never deleted. A folder is settled only when it holds no placeholders and its
    whole tree's byte total is stable.
  * **The move is copy -> verify -> rename -> delete**, never a bare cross-volume
    `shutil.move`: the local copy is staged inside a dot-dir on the destination volume
    and only renamed into the watch folder once its size is confirmed, so a crash can
    never leave a half-file where `direct_ingest.py` would file it. A failure leaves
    the iCloud source untouched for the next pass.
  * **Collisions never clobber.** If a same-named entry is already local, an identical
    copy means the iCloud copy is a leftover from a crashed earlier pass and is removed;
    a different file is uniquified (`.1`, `.2`, ...), never overwritten.
  * **Only ingestible media moves.** Files whose extension is in
    `config.DIRECT_INGEST_EXTENSIONS`, and directories holding those or loose page
    images. Dot-files, AppleDouble junk, `.icloud` placeholders themselves, and the
    fleet's state subfolders are never touched.

    python3 direct_ingest_bridge.py            # run the watch daemon
    python3 direct_ingest_bridge.py --once      # one pass then exit
    python3 direct_ingest_bridge.py --once --dry-run   # report, move nothing
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# This repo's own directory goes FIRST on sys.path. Torrent-Ingest and Torrent-Searcher
# both ship modules named `config.py`, `library.py` and `ingest.py`, and both repos are on
# `sys.path` in some processes -- so a bare `import config` resolves to whichever repo the
# launcher happened to put first. That is how `directingest` died at import on 2026-08-27.
# The pin makes the resolution a property of the FILE rather than of how it was launched.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                                                          # noqa: E402


SOURCE_DIR = config.ICLOUD_DIRECT_INGEST_DIR
DEST_DIR = config.DIRECT_INGEST_DIR
# Staging area for the copy-then-rename, INSIDE the destination folder so the final
# rename is same-volume (atomic). Dot-named so `direct_ingest._find_media` never sees it.
STAGE_DIR = DEST_DIR / ".icloud-bridge"
LOG_FILE = config.PROJECT_ROOT / "direct_ingest_bridge.log"
POLL_SEC = 20
STABLE_SEC = 10         # a drop must hold still this long before we touch it
MATERIALIZE_WAIT_SEC = 600
# Never touched inside the drop folder: the fleet's control state (defensive; this
# folder is new) and Finder/AppleDouble junk.
SKIP_NAMES = {"finished", "failed", "queued", "ingesting", ".DS_Store"}


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
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    STAGE_DIR.mkdir(parents=True, exist_ok=True)


def _is_junk(name: str) -> bool:
    return name.startswith("._") or name in SKIP_NAMES


def _tree_size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _real_name(name: str) -> str:
    """A dataless placeholder's real filename (`.Name.mkv.icloud` -> `Name.mkv`)."""
    if name.startswith(".") and name.endswith(".icloud"):
        return name[1:-len(".icloud")]
    return name


def _ingestible_name(name: str) -> bool:
    """Whether a (possibly placeholder) filename is something we ingest."""
    if name.startswith("._"):
        return False
    ext = os.path.splitext(_real_name(name))[1].lower()
    return ext in config.DIRECT_INGEST_EXTENSIONS or ext in config.LOOSE_PAGE_EXTENSIONS


def _placeholders(p: Path) -> list[str]:
    """Every dataless iCloud placeholder at or under `p`, as the REAL path it stands
    for (`.Name.mkv.icloud` -> `Name.mkv`). A plain materialized tree yields [].

    `p` is always the REAL path (placeholders in the top level are resolved by
    `find_drops`), so the file's own placeholder is checked beside it, then the tree."""
    out = []
    if p.with_name(f".{p.name}.icloud").exists():
        out.append(str(p))
    if p.is_dir():
        for dp, dn, fns in os.walk(p):
            dn[:] = [d for d in dn if d != ".DS_Store"]
            for n in fns:
                if n.startswith(".") and n.endswith(".icloud"):
                    out.append(str(Path(dp) / _real_name(n)))
    return out


def _has_ingestible(d: Path) -> bool:
    for _dp, _dn, fns in os.walk(d):
        for n in fns:
            if _ingestible_name(n):
                return True
    return False


def find_drops() -> list[Path]:
    """The ingestible drops at the top of the iCloud folder, placeholders resolved to
    their real path. Dot-files and control/state names are skipped; a directory is a
    drop only when it holds media (or packageable loose pages)."""
    out: list[Path] = []
    seen: set[str] = set()
    try:
        children = sorted(SOURCE_DIR.iterdir())
    except OSError:
        return out
    for p in children:
        if _is_junk(p.name):
            continue
        if p.name.startswith(".") and p.name.endswith(".icloud"):
            real = p.with_name(_real_name(p.name))
            if _ingestible_name(p.name) and str(real) not in seen:
                seen.add(str(real))
                out.append(real)
            continue
        if p.name.startswith("."):
            continue                            # hidden scratch, not a drop
        if p.is_file():
            if _ingestible_name(p.name) and str(p) not in seen:
                seen.add(str(p))
                out.append(p)
        elif p.is_dir() and not p.is_symlink() and _has_ingestible(p):
            out.append(p)
    return out


def materialize(p: Path, wait_sec: int = MATERIALIZE_WAIT_SEC) -> bool:
    """Force a dataless iCloud drop (and everything under a folder) fully local, then
    wait until no placeholder remains and the tree holds still. Returns False when it
    is still not ready after `wait_sec` -- the caller leaves it for the next pass.

    `brctl download` is the same primitive `ingest.materialize` uses for a `.torrent`,
    but directory-aware: a folder drop may arrive with individual files still dataless,
    so every placeholder under it is requested explicitly."""
    try:
        if not p.exists() and not _placeholders(p):
            # Neither materialized bytes nor a placeholder: iCloud has not surfaced it
            # yet. Do not block the pass on a 10-minute wait -- the next pass retries.
            return False
        wants = _placeholders(p) or [str(p)]
        for real in wants:
            subprocess.run([config.BRCTL_BIN, "download", real],
                           check=False, capture_output=True)
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            if _placeholders(p):
                time.sleep(2)
                continue
            try:
                s1 = _tree_size(p)
            except OSError:
                time.sleep(2)
                continue
            time.sleep(STABLE_SEC)
            try:
                if not p.exists():
                    return False
                if not _placeholders(p) and _tree_size(p) == s1 and s1 > 0:
                    return True
            except OSError:
                pass
        return False
    except OSError:
        return False


def _hash_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_content(src: Path, dst: Path) -> bool:
    """Whether two entries (files or trees) hold identical bytes/sizes. Only used on a
    NAME COLLISION -- the common path never hashes, so a multi-GB movie costs nothing.
    Files fall back to a full SHA-256 (sizes alone can lie); trees compare their
    relative path -> size maps, which is what a resumed cross-volume copy preserves."""
    try:
        if src.is_file() != dst.is_file():
            return False
        if src.is_file():
            if src.stat().st_size != dst.stat().st_size:
                return False
            return _hash_file(src) == _hash_file(dst)
        a = {str(f.relative_to(src)): f.stat().st_size for f in src.rglob("*") if f.is_file()}
        b = {str(f.relative_to(dst)): f.stat().st_size for f in dst.rglob("*") if f.is_file()}
        return a == b
    except OSError:
        return False


def _unique_dest(name: str) -> Path:
    """`DEST_DIR/name`, or the first `stem.N.ext` that does not exist."""
    dst = DEST_DIR / name
    n = 1
    while dst.exists():
        p = Path(name)
        dst = DEST_DIR / f"{p.stem}.{n}{p.suffix}"
        n += 1
    return dst


def _copy_verified(src: Path, dst: Path) -> bool:
    """Copy `src` to `dst` on the destination volume, verifying the byte total. Files
    are copied with `copy2`; directories with `copytree`. `dst` does not exist (the
    caller uniquifies) -- a leftover staging path is removed first."""
    try:
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst, ignore_errors=True)
            else:
                dst.unlink(missing_ok=True)
        if src.is_file():
            shutil.copy2(src, dst)
            return dst.is_file() and dst.stat().st_size == src.stat().st_size
        shutil.copytree(src, dst, symlinks=True)
        return dst.is_dir() and _tree_size(dst) == _tree_size(src)
    except OSError as exc:
        log(f"    copy failed {src.name}: {exc}")
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink(missing_ok=True)
        return False


def move_drop(src: Path, dry_run: bool = False) -> bool:
    """Move one settled iCloud drop into the local watch folder. Returns True when the
    drop is now at the destination (moved, or proven already there). Never deletes the
    iCloud source unless the local copy is on disk and verified."""
    name = src.name
    dst = DEST_DIR / name
    if dst.exists():
        if _same_content(src, dst):
            if dry_run:
                log(f"  [dry-run] {name}: identical local copy present; iCloud copy "
                    f"would be removed")
                return True
            try:
                if src.is_dir():
                    shutil.rmtree(src)
                else:
                    src.unlink()
                log(f"  {name}: identical copy already local; removed the iCloud duplicate")
                return True
            except OSError as exc:
                log(f"  {name}: identical copy local but iCloud removal failed: {exc}")
                return False
        dst = _unique_dest(name)
        log(f"  {name}: a different local file already exists; filing as {dst.name}")
    if dry_run:
        log(f"  [dry-run] {name} -> {dst}")
        return True

    if dst.exists():
        dst = _unique_dest(name)             # re-check: nothing may be clobbered
    stage = STAGE_DIR / f".staging-{os.getpid()}-{name}"
    if not _copy_verified(src, stage):
        log(f"  {name}: could not stage a verified copy; left in iCloud for next pass")
        return False
    try:
        os.replace(stage, dst)                       # same volume: atomic appearance
    except OSError as exc:
        log(f"  {name}: staged copy could not be moved into place: {exc}")
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True) if stage.is_dir() \
                else stage.unlink(missing_ok=True)
        return False
    # The local copy is verified and in place; the iCloud copy is a duplicate now.
    try:
        if src.is_dir():
            shutil.rmtree(src)
        else:
            src.unlink()
    except OSError as exc:
        # The local copy is safe; leaving the iCloud copy means the next pass sees the
        # collision and removes it then.
        log(f"  {name}: moved locally, but could not remove the iCloud copy: {exc}")
        return True
    log(f"  {name} -> {dst}")
    return True


def scan_once(dry_run: bool = False) -> int:
    """One pass. Returns the number of drops moved (or reported, in dry-run).

    Dry-run reports and touches NOTHING -- not even the materialization download -- so
    it is safe to point at the live iCloud folder to see what the daemon sees."""
    if not dry_run:
        _ensure_dirs()
    n = 0
    for p in find_drops():
        if dry_run:
            log(f"  [dry-run] would bridge {p.name}")
            n += 1
            continue
        # A placeholder's real path may not exist until materialized; materialize()
        # handles both that and an already-local tree.
        if not materialize(p):
            log(f"  {p.name} is not fully materialized/stable yet; leaving for next pass")
            continue
        if move_drop(p):
            n += 1
    return n


def _prune_stage() -> None:
    """Remove stale `.staging-*` leftovers from a crashed pass (older than an hour)."""
    try:
        cutoff = time.time() - 3600
        for p in STAGE_DIR.glob(".staging-*"):
            try:
                if p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True) if p.is_dir() \
                        else p.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Move raw-media drops from iCloud Torrents/DirectIngest into the "
                    "local DirectIngest watch folder.")
    ap.add_argument("--once", action="store_true", help="one pass then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would move; touch nothing")
    args = ap.parse_args()
    if not args.dry_run:
        _ensure_dirs()
        _prune_stage()
    log(f"direct_ingest_bridge watching {SOURCE_DIR} -> {DEST_DIR}"
        + (" (dry-run)" if args.dry_run else ""))
    if args.once:
        scan_once(dry_run=args.dry_run)
        return 0
    while True:
        try:
            scan_once(dry_run=args.dry_run)
        except Exception as exc:                                   # noqa: BLE001
            log(f"scan error (continuing): {exc}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
