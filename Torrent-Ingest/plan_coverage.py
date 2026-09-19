#!/usr/bin/env python3
"""The plan-coverage contract: every downloaded byte is either FILED or provably junk.

WHY THIS EXISTS, in one incident. `27dba0357753fe1c22b0bbad10e7c44d9fcf028f`
("The Smurfs Complete Seasons 1-9 dvdrip") was a 405-file, 34.9 GB release. The
identify run returned a plan for 40 files -- every one of them Season 1 -- and
nothing anywhere required the plan to account for the rest. The 40 were applied,
`_advance_cleanup` ran, and `_delete_local_content` removed the WHOLE download
root: **365 files / ~31.16 GB deleted unfiled**, with no `chunk_*` record that
could even say so. The same seam is what the chunked path expresses as
"not in plan (junk/duplicate); dropping", and it is how Doctor Who (2005) lost 38
files / 60.9 GB to 1963-slot collisions.

THE ASYMMETRY THIS ENCODES, straight from HANDOFF §5. A plan that covers
everything is ordinary work. A plan that covers PART of the release is a
statement about the files it does not name -- and that statement belongs to the
harness, not to a free model or to a cleanup that never read it. So before any
byte is deleted, the release's own file list is compared against the plan:

  * a file the plan names (as a file, or under a planned DIRECTORY -- the loose
    pages -> `.cbz` packaging case) is accounted for;
  * a file the harness itself already disposed of (an intra-torrent duplicate
    collapsed onto a surviving copy) is accounted for;
  * a file in a non-media extension, a `sample`/`screens`/`proof` path, a
    creditless OP/ED/NCOP-style extra, a subtitle beside a planned video, or a
    sub-50 MiB video is JUNK -- deterministic patterns, measured against history;
  * **anything else is UNRESOLVED, and an unresolved file parks the whole
    release.** The bytes stay on disk, the record fails with the unfiled list,
    and the `.torrent` is filed for review. Nothing is guessed away.

Which is why this is one function with a deliberately boring return value: the
caller may delete only when `unresolved` is empty. There is no flag to turn that
off, because HANDOFF §5 is explicit -- "a guard with a known false-positive rate
does not become safe by being optional".
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import config

# A video this small is not an episode or a film; it is a sample, a menu loop, a
# 30-second extra. Sized against the library's real work: the smallest real episode in
# the journal is an 11-minute short at ~55 MB, and release samples run 2-40 MB. The
# token rules below catch the explicitly-named ones first; the size rule is the
# backstop for the anonymous ones.
SAMPLE_MAX_BYTES = 50 * 1024 * 1024

# One token list, shared with ingest's "no library home" detection so the two can
# never disagree about what an extra is. These are deterministic RELEASE conventions,
# not model judgment: a creditless opening, an NCOP/NCED, a TV CM, a trailer/PV,
# a preview, a menu, a promo, a placement placeholder, or a sample.
_NO_HOME_TOKENS = (
    "creditless", "ncop", "nced", "placeholder", "trailer", "preview", "sample",
    "teaser", "menu", "promo",
)
_NO_HOME_RE = re.compile(
    r"creditless|\bncop\w*\b|\bnced\w*\b|\bplaceholder\b|\bcm\d*\b|\bop\d*\b|\bed\d*\b"
    r"|\btrailer\w*\b|\bpv\d*\b|\bpreview\w*\b|\bsample\b|\bteaser\w*\b|\bmenu\b"
    r"|\bpromo\w*\b",
    re.IGNORECASE)
# (`ncopv01`/`ncedv2` are the real-world spellings; `\bncop\d*\b` missed them because
# there is no word boundary between the token and the `v`, so a 1080p NCOP read as a
# real episode and its pack parked. The first replay found this on Made in Abyss.)
# Sample/proof directories and names, matched as path segments so "screenshots" and
# "Sample" both land while "Sampler" (a real episode title) does not.
_SAMPLE_PATH_RE = re.compile(
    r"(?i)(?:^|/)(?:sample|samples|screens|screenshots?|proof|thumbs?)(?:/|$)")
# Subtitle sidecars attach to their video by stem prefix ("Movie.en.srt" for
# "Movie.mkv", "Show - S01E01.1080p.srt" for the same video). Normalized to the
# basename, lowercased.
_SUBTITLE_SUFFIXES = tuple(config.SUBTITLE_EXTENSIONS)


def _norm(s: str) -> str:
    """NFC path half -- macOS hands out decomposed names, torrent metadata composed."""
    return unicodedata.normalize("NFC", str(s or "").replace("\\", "/")).strip("/")


def looks_like_no_home_extra(name: str) -> bool:
    """Whether a filename reads as a no-library-home extra (creditless OP, sample...).

    The same verdict `ingest._looks_like_no_home_extra` has always used, moved here so
    the coverage contract and the wave cleanup consult one list.
    """
    t = re.sub(r"[\[\]\(\)_.]+", " ", (name or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    return bool(_NO_HOME_RE.search(t))


def _is_sample_path(rel: str) -> bool:
    return bool(_SAMPLE_PATH_RE.search(_norm(rel).lower()))


def _basename(rel: str) -> str:
    return _norm(rel).rsplit("/", 1)[-1]


def _is_sidecar_of(rel: str, video_stems: set[str]) -> bool:
    """A subtitle beside a planned video: its stem starts with the video's stem."""
    if not _norm(rel).lower().endswith(_SUBTITLE_SUFFIXES):
        return False
    stem = Path(_basename(rel)).stem.lower()
    return any(stem.startswith(vs) for vs in video_stems if vs)


def release_gaps(release_files, plan_files, content_root=None, resolved_srcs=(),
                 basename_fallback=False):
    """Classify every release file the plan does not account for.

    `release_files` is an iterable of `(relative_path, size|None)` pairs (a bare path
    string is accepted and read as size-unknown). `plan_files` is `plan["files"]`
    AFTER `validate_plan` -- which is what makes directory sources and healed paths
    authoritative. `resolved_srcs` is any additional source the harness already
    disposed of (the plan's `_deduped_dropped`, an intra-torrent duplicate collapsed
    onto a surviving copy; NOT `_collision_parked`, which must stay unresolved).

    Returns `(unresolved, accounted)` -- both `list[str]` of release-relative paths.
    The caller deletes local content only when `unresolved` is empty. Never raises on
    a malformed entry: an entry it cannot interpret is UNRESOLVED, the safe side.
    """
    root = Path(content_root).resolve() if content_root else None
    planned_rels: set[str] = set()
    planned_dirs: set[str] = set()
    planned_names: set[str] = set()
    video_stems: set[str] = set()

    def _absorb(src, fallback_name=None):
        if not src and fallback_name:
            planned_names.add(fallback_name.lower())
            return
        if not src:
            return
        planned_names.add(Path(str(src)).name.lower())
        if root is not None:
            try:
                src_p = Path(str(src)).resolve()
                if src_p == root:
                    # The plan names the whole content root (a single-file release, or a
                    # single loose-pages folder packaged into one .cbz): it covers every
                    # file in it. `relative_to` would say ".", which no release path equals.
                    planned_rels.add(_norm(root.name))
                    planned_dirs.add("")
                    return
                rel = _norm(src_p.relative_to(root))
            except (ValueError, OSError):
                rel = None
            if rel:
                planned_rels.add(rel)
                planned_dirs.add(rel)
                if Path(rel).suffix.lower() in config.VIDEO_EXTENSIONS:
                    video_stems.add(Path(_basename(rel)).stem.lower())
                return
        # No content_root (or the src is outside it): the basename in `planned_names`
        # is the only bridge left, and the caller decides via `basename_fallback`
        # whether to trust it.

    for f in plan_files or []:
        if isinstance(f, dict):
            _absorb(f.get("src"))
    for src in resolved_srcs or ():
        _absorb(src)

    unresolved: list[str] = []
    accounted: list[str] = []
    for item in release_files or ():
        if isinstance(item, (tuple, list)) and item:
            rel, size = item[0], (item[1] if len(item) > 1 else None)
        else:
            rel, size = item, None
        rel = _norm(rel)
        if not rel:
            continue
        covered = rel in planned_rels or "" in planned_dirs
        if not covered:
            # A planned DIRECTORY covers everything under it: a loose-pages folder
            # planned as one `.cbz` must not leave its 200 pages looking unaccounted.
            covered = any(rel.startswith(d + "/") for d in planned_dirs if d)
        if not covered and basename_fallback and _basename(rel).lower() in planned_names:
            covered = True
        if covered:
            accounted.append(rel)
            continue

        suffix = Path(rel).suffix.lower()
        if suffix in _SUBTITLE_SUFFIXES and _is_sidecar_of(rel, video_stems):
            accounted.append(rel)                      # sidecar of a planned video
            continue
        if suffix not in config.MEDIA_EXTENSIONS and suffix not in config.LOOSE_PAGE_EXTENSIONS:
            accounted.append(rel)                      # release .nfo/.sfv/.txt/.url...
            continue
        if looks_like_no_home_extra(_basename(rel)) or _is_sample_path(rel):
            accounted.append(rel)                      # sample / creditless OP / NC
            continue
        if suffix in config.LOOSE_PAGE_EXTENSIONS:
            if size is not None and 0 < size <= SAMPLE_MAX_BYTES:
                accounted.append(rel)                  # cover art / thumbnail
            else:
                unresolved.append(rel)
            continue
        if suffix in config.VIDEO_EXTENSIONS:
            if size is not None and 0 < size <= SAMPLE_MAX_BYTES:
                accounted.append(rel)                  # anonymous sample-sized video
            else:
                unresolved.append(rel)
            continue
        # An archive, novel or unmatched subtitle. Unplanned here means real content
        # the plan declined without saying so -- never guessed away.
        unresolved.append(rel)

    return unresolved, accounted
