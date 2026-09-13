"""Filing audio tracks into a flat, cloud-synced music folder.

This is the one route that does NOT go through the library pipeline, and deliberately so.
Each destination (`ytconfig.AUDIO_PLAYLIST_DIRS`) is a folder of `<Track Title>.mp3` that
already exists and is used by hand -- it is not a Jellyfin library, so a track here gets no
placement plan, no `.nfo`, no episode number, and is not replicated to the MEGA pool (the
cloud tree it sits in is its durability). Keeping it outside the plan machinery is what
stops a two-minute battle theme turning up in the library as `S01E43`.

Every function takes the destination folder explicitly rather than reading one constant,
because there is more than one: an OST rip files into iCloud `Soundtracks/` and a song from
the Music playlist files into Google Drive `Music/`. The playlist decides which; this module
only ever does what it is told, so a routing bug cannot hide in here.

Tracks are small (a few MB), so there is no wave budget here -- the cost is the download
itself and nothing else.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import ytconfig


def existing_titles(dest: Path) -> set:
    """Track names already in `dest`, with their REAL capitalisation.

    Searched RECURSIVELY, because a destination need not be flat: the Google Drive `Music`
    folder sorts part of itself into subfolders (`Church/`, `Folk music/`, …) by hand, and a
    song already filed into one of those is still a song we have. A top-level-only listing
    would re-download it and drop a second copy at the root every time it is re-added.

    Deliberately not lowercased: these are compared against a freshly cleaned track title,
    and callers lowercase at the comparison. Keeping the real names means anything that
    displays them shows the folder's own convention rather than a flattened version of it.
    """
    if not dest.is_dir():
        return set()
    return {p.stem for p in dest.rglob("*")
            if p.is_file() and not p.name.startswith(".")}


def _unique_dest(dest: Path, title: str) -> Path:
    """The path for a track inside `dest`, never overwriting an existing file.

    Write-once, matching the library's stance: an existing `Katakuri Theme.mp3` is
    assumed to be the copy you want, so a second upload of the same piece becomes
    `Katakuri Theme (2).mp3` rather than replacing it.
    """
    base = dest / f"{title}.{ytconfig.AUDIO_FORMAT}"
    if not base.exists():
        return base
    n = 2
    while True:
        cand = dest / f"{title} ({n}).{ytconfig.AUDIO_FORMAT}"
        if not cand.exists():
            return cand
        n += 1


def file_track(src: Path, title: str, dest: Path) -> Path:
    """Move one extracted audio file into `dest` under its clean track name.

    The destination's PARENT must already exist. Both destinations live inside a
    cloud-synced tree (iCloud Drive, Google Drive's File Provider mount), and an unmounted
    tree is indistinguishable from an empty path -- so `mkdir(parents=True)` would happily
    build the whole chain locally and file every track into a plain directory that nothing
    ever syncs. Refusing is recoverable (the video is marked failed and retried next
    cycle); a folder of orphaned mp3s under a phantom mount point is not, because nothing
    ever reports it.

    Copied then unlinked rather than renamed: the destination is in a synced tree and the
    scratch dir is on the Downloads volume, so this is a cross-volume move. The copy lands
    complete before the original goes away, so an interrupted run leaves the source in
    place to retry rather than a truncated file in the cloud.
    """
    if not dest.parent.is_dir():
        raise OSError(f"audio destination {dest} is not reachable "
                      f"({dest.parent} does not exist -- cloud folder not mounted?)")
    dest.mkdir(parents=True, exist_ok=True)
    dst = _unique_dest(dest, title)
    tmp = dst.with_name(f".{dst.name}.part")
    shutil.copy2(src, tmp)
    if tmp.stat().st_size != src.stat().st_size:
        tmp.unlink(missing_ok=True)
        raise OSError(f"size mismatch filing track {title!r}")
    tmp.replace(dst)
    src.unlink(missing_ok=True)
    return dst
