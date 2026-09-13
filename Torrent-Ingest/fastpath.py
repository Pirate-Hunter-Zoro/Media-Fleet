"""Deterministic placement fast-path — skip the AI identify for the common case.

diagnosis.txt §6.3.1 / §6.4. The searcher already settles each torrent's file→item map
(`{series, kind, files:[{src, type, season, number, …}]}`) and persists it keyed by
infohash; the ingest loads it but then still ran the full multi-turn AI agent. For the
common case — a show / volume / chapter with a complete stored map, NOT an owned show,
NOT a movie — the destination is fully derivable (`Shows/<name>/Season NN/<name> -
SxxEyy.ext`) without any model call.

`build_plan` derives that plan directly, gated to stay safe:

  * (a) the series name resolves UNAMBIGUOUSLY to an EXISTING library folder under the
        same normalization the searcher uses (strip trailing `(YYYY)`, fold
        punctuation/accent) — `library.resolve_*_folder`;
  * (b) the numbering is CONFIRMED — for every episode file the release filename either
        carries a clean `SxxEyy` that EQUALS the stored plan's `(season, number)`, or a
        loose SEASONED number ("S2 - 08", "Part 3 - 12", "Episode 114", a lone "01") whose
        episode equals the plan's number and whose season (from the filename, else the
        plan) EQUALS the plan's season AND is already on disk (so a "Part 5 -> Season 3"
        mis-number is refused), and the release filename shares an identifying token with
        the resolved folder (so it really is about this series, not a sibling/spin-off).

This is deliberately STRICTER than §6.4's draft gate ("the library has titles ⇒ the plan
matched by title"): re-running Test B against the journal found that the item_map is
sometimes wrong about numbering even when the library HAS titles — a bare-numbered
release ("… - 01", "Episode 114") is mapped by filename inference, and a release of a
different series in the same family ("BanG Dream! Yume∞Mita" filed under "BanG Dream!")
is matched against the wrong episode list. Trusting the map on titles alone would
reproduce those mis-placements; requiring a clean `SxxEyy` that matches, plus a token
overlap, eliminates them.

Everything else — movies (need a TMDB id), owned shows (need authored titles/plots),
title-less absolute shows, Japanese/English name mismatches, Season-0 specials (always
locked + authored), loose page images (packageable into `.cbz`) — returns None so the
caller falls back to the AI identify, which remains the authority for exactly those
ambiguous cases. A plan that returns is still re-validated by `library.validate_plan`
and, on any rejection, the caller falls back to the AI — so the fast-path can only ever
skip a model call, never place a file the harness would not accept.
"""

from __future__ import annotations

import re
from pathlib import Path

import config
import library


_TVSHOW_NFO = "tvshow.nfo"
_EP_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})")
_RELEASE_EP_RE = re.compile(r"[Ss]\s*(\d{1,2})\s*[Ee]\s*(\d{1,4})")
# An episode RANGE in the filename ("S01E01-E13", "- 01-13") is a multi-episode batch the
# fast-path must not place as a single episode.
_EP_RANGE_RE = re.compile(r"[Ee]?\d{1,4}\s*[-~–]\s*(?:[Ee]?\d{1,4})")
# A loose SEASONED episode: a season label followed by a dash and an episode number
# ("S2 - 08", "Part 3 - 12", "Season 1 - 04"). The clean SxxEyy form is handled separately
# by `_RELEASE_EP_RE`; this is the bare-numbered seasoned shape gate (b) must now also
# confirm (§ diagnosis 6.3.1). An explicit season label wins over the range scan below, so
# "S2 - 08" reads as (season 2, episode 8), not an "episodes 2-8" range.
_LOOSE_SEASON_EP_RE = re.compile(
    r"\b(?:[Ss]\s*|(?:[Pp]art|[Ss]eason)\s+)(\d{1,2})\s*[-–—~]\s*(\d{1,4})\b")
# A bare episode marker with no season ("Episode 114", "Ep 08", "E 05") — the other bare-
# numbered shape. Returns the episode only; the season must come from the stored map.
_EPISODE_MARKER_RE = re.compile(r"\b(?:[Ee]pisode\s*|[Ee]ps?\.?\s*)(\d{1,4})\b")
# Markers that make a bare number ambiguous: a season label, a special/OAV/movie. A file
# named "Show S2.mkv" or "Show OVA 01.mkv" must not have its lone number read as a bare
# episode.
_AMBIGUOUS_MARKER_RE = re.compile(
    r"\b(?:[Ss]\s*\d|(?:[Pp]art|[Ss]eason)\s*\d|[Oo][Vv][Aa]|[Oo][Aa][Vv]"
    r"|[Ss]pecial|[Mm]ovie)\b")
_BARE_NUMBER_RE = re.compile(r"\b(\d{1,3})\b")
_COLOR_MARKERS = ("colored", "full color", "full-color", "color edition", "colour")

# Tokens that never identify a series — the same idea as the searcher's relevance gate, so
# "The ... - S01E01" can never share a generic word with every folder and slip a wrong
# series through.
_GENERIC_TOKENS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for",
    "is", "are", "was", "were", "it", "as", "by", "with", "from",
    "that", "this", "these", "those", "there", "their", "they", "them", "then", "than",
    "not", "no", "nor", "so", "but", "if", "be", "been", "being", "am",
    "have", "has", "had", "do", "does", "did", "will", "would", "can", "could",
    "should", "may", "might", "must", "shall", "just", "only", "also", "very",
    "more", "most", "some", "any", "each", "every", "own", "same", "other", "another",
    "we", "you", "he", "she", "i", "me", "my", "your", "his", "her", "its", "our",
    "when", "where", "who", "whom", "whose", "which", "why", "how",
    "anime", "tv", "movie", "movies", "ova", "season", "seasons", "series",
    "show", "shows", "part", "special", "specials", "complete", "batch", "dual", "audio",
    "sub", "subs", "subbed", "dub", "dubbed", "multi", "all", "eng", "jpn",
    "ep", "eps", "episode", "episodes", "film", "films",
    "web", "bd", "bdr", "bdrip", "bluray", "remux", "hevc", "x264", "x265", "h264",
    "aac", "flac", "dts", "ac3", "ddp", "1080p", "2160p", "720p", "480p", "576p",
    "dvd", "webdl", "webrip", "hdtv", "amzn", "dsnp", "nf",
    "edition", "collection", "compendium",
}


def _tokens(name):
    import unicodedata
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    out = set()
    for w in t.split():
        if w.isdigit() or w in _GENERIC_TOKENS:
            continue
        out.add(w)
    return out


def _looks_colored(name):
    low = (name or "").lower()
    return any(m in low for m in _COLOR_MARKERS)


def _release_se(src):
    """(season, number) the release filename advertises via a clean `SxxEyy`, else None."""
    name = Path(src).name
    if _EP_RANGE_RE.search(name):
        return None
    m = _RELEASE_EP_RE.search(name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _release_loose_se(src):
    """(season, episode) a bare-numbered SEASONED release advertises, else None.

    The clean SxxEyy gate (`_release_se`) misses the loose shapes this fleet actually
    receives (§ diagnosis 6.3.1). Returns a 2-tuple on a confident parse and None on
    anything ambiguous (an episode range, a special, or a name with multiple candidate
    numbers):

      * "S2 - 08" / "Part 3 - 12" / "Season 1 - 04" -> (season, episode)
      * "Episode 114" / "Ep 08"                    -> (None, episode)
      * "… - 01" (a lone number)                   -> (None, episode)

    A `None` season means "the release does not state the season"; the caller takes the
    season from the stored map and requires it to already be on disk."""
    name = Path(src).name
    m = _LOOSE_SEASON_EP_RE.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    if _EP_RANGE_RE.search(name):
        return None                     # an episode range, not a single episode
    m = _EPISODE_MARKER_RE.search(name)
    if m:
        return None, int(m.group(1))
    if _AMBIGUOUS_MARKER_RE.search(name):
        return None                     # a season/special label makes the number ambiguous
    numbers = _BARE_NUMBER_RE.findall(name)
    if len(numbers) == 1:
        return None, int(numbers[0])
    return None


def _existing_seasons(folder):
    """The positive season numbers already on disk under a show folder — the same
    subdirectory scan `library._reject_season_gap` uses to know the on-disk seasons."""
    seasons = set()
    try:
        for sub in folder.iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                m = re.search(r"(\d+)", sub.name)
                if m and int(m.group(1)) > 0:
                    seasons.add(int(m.group(1)))
    except OSError:
        pass
    return seasons


def _show_profile(folder):
    """One-pass profile of a show folder from its .nfo sidecars (which survive eviction):
    `owned` (any locked episode), `titled` (count of episodes with a real <title>),
    `absolute` (one non-special season holding >60 episodes), and `episodes` (total count).
    Iterates lazily and stops early the moment a locked episode proves the show owned, so
    a big owned show costs one .nfo read, not a full walk."""
    owned = False
    titled = 0
    total = 0
    seasons = {}
    try:
        nfos = folder.rglob("*.nfo")
    except OSError:
        nfos = ()
    for nfo in nfos:
        if nfo.name.lower() == _TVSHOW_NFO:
            continue
        m = _EP_RE.search(nfo.name)
        if not m:
            continue
        s, e = int(m.group(1)), int(m.group(2))
        total += 1
        seasons[s] = max(seasons.get(s, 0), e)
        text = library._read_text(nfo)
        if library.nfo_is_locked(text):
            return {"owned": True, "titled": titled, "absolute": False,
                    "episodes": total, "season_gap": False}
        if text and library._xml_tag(text, "title"):
            titled += 1
    content = {s: e for s, e in seasons.items() if s > 0}
    absolute = len(content) == 1 and content.get(1, 0) > 60
    gap = len(content) > 1 and set(range(min(content), max(content) + 1)) != set(content)
    return {"owned": False, "titled": titled, "absolute": absolute, "episodes": total,
            "season_gap": gap}


def _index_media(content):
    """basename -> list of on-disk media files under `content` (a file or a directory)."""
    idx = {}
    root = Path(content)
    try:
        if root.is_file():
            if root.suffix.lower() in config.MEDIA_EXTENSIONS:
                idx.setdefault(root.name.lower(), []).append(root)
            return idx
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in config.MEDIA_EXTENSIONS:
                idx.setdefault(p.name.lower(), []).append(p)
    except OSError:
        pass
    return idx


def _has_loose_pages(content):
    root = Path(content)
    try:
        if root.is_file():
            return root.suffix.lower() in config.LOOSE_PAGE_EXTENSIONS
        return any(p.is_file() and p.suffix.lower() in config.LOOSE_PAGE_EXTENSIONS
                   for p in root.rglob("*"))
    except OSError:
        return False


def _locate(src, idx):
    """The on-disk file a stored plan `src` names, only when it is unambiguous. Returns
    None on 0 or >1 matches — the caller then falls back to the AI."""
    base = Path(src).name.lower()
    hits = idx.get(base)
    if not hits or len(hits) != 1:
        return None
    return hits[0]


def _episodes_confirmed(profile, episodes, folder):
    """Gate (b): is the stored plan's (season, number) trustworthy? (§ diagnosis 6.4 / 6.3.1)

    The searcher's item_map can be wrong about numbering even when the library HAS
    titles: a bare-numbered release ("… - 01", "Episode 114") is mapped by filename
    inference, and a release of a *different* series in the same family ("BanG Dream!
    Yume∞Mita" filed under the "BanG Dream!" series) is matched against the wrong episode
    list. So the fast-path does NOT trust the map's numbers blindly. It fires only when,
    for every episode file:

      * the release filename carries a clean `SxxEyy` AND it equals the plan's
        (season, number) — i.e. the release numbering IS the library numbering; or

      * the release filename carries a loose SEASONED number ("S2 - 08", "Part 3 - 12",
        "Episode 114", a lone "01") whose episode equals the plan's number, whose season
        (from the filename, else from the plan) EQUALS the plan's season AND is already on
        disk — so a "Part 5 -> Season 3" mis-number (a season that isn't on disk) is
        refused here, leaving `_reject_season_gap` the deeper backstop; and

      * the release filename shares an identifying token with the resolved folder — so the
        release really is about this series, not a sibling/spin-off.

    Everything else — absolute shows, owned shows, title-only matches, Japanese/English
    name mismatches, ranges, specials — falls back to the AI identify, which remains the
    authority for those.
    """
    if profile["absolute"]:
        return False
    folder_tokens = _tokens(folder.name)
    if not folder_tokens:
        return False
    existing = _existing_seasons(folder)
    for src, s, n in episodes:
        if not (_tokens(Path(src).name) & folder_tokens):
            return False
        r = _release_se(src)
        if r is not None and r == (s, n):
            continue                        # clean SxxEyy matches the plan — confirmed
        loose = _release_loose_se(src)
        if loose is None:
            return False
        ps, pe = loose
        season = ps if ps is not None else s
        if season != s or pe != n:
            return False                    # the release disagrees with the plan's numbers
        if season not in existing:
            return False                    # a season that is not on disk is not trusted
    return True


def _accounted(idx, files, delete_set):
    """Every on-disk media file is either placed, or a file the searcher already declined
    (`delete`). Anything else means the fast-path cannot account for it and must not fire."""
    placed = {str(f.get("src")) for f in files}
    for base, paths in idx.items():
        for p in paths:
            if str(p) in placed:
                continue
            if base in delete_set:
                continue
            return False
    return True


def _show_dst(folder, season, episode, ext):
    return (f"Shows/{folder.name}/Season {season:02d}/{folder.name} - "
            f"S{season:02d}E{episode:02d}{ext}")


def _swap_ext(dst_rel, ext):
    return str(Path(dst_rel).with_suffix(ext))


def build_plan(info_hash, content_path, stored_plan):
    """Derive a validated-shaped placement plan from the stored file→item map, or None.

    Returns a dict ready for `library.validate_plan` (which the caller still runs) when
    the common case is fully derivable; None otherwise, so the caller runs the AI."""
    if not config.FAST_PATH_ENABLED:
        return None
    if not stored_plan or not isinstance(stored_plan, dict):
        return None
    series_name = stored_plan.get("series") or ""
    kind = (stored_plan.get("kind") or "").lower()
    raw_files = [f for f in stored_plan.get("files") or [] if isinstance(f, dict)]
    if not series_name or not raw_files:
        return None
    if kind not in ("anime", "tv", "manga", "comic", "lightnovel"):
        return None

    content = Path(content_path)
    if not content.exists():
        return None
    if _has_loose_pages(content):
        return None
    idx = _index_media(content)
    if not idx:
        return None

    episodes, volumes, chapters = [], [], []
    delete_set = set()
    for f in raw_files:
        t = (f.get("type") or "").lower()
        src = f.get("src") or ""
        if t == "movie":
            return None
        if t == "episode":
            s, n = f.get("season"), f.get("number")
            if s is None or n is None:
                return None
            try:
                episodes.append((src, int(s), int(n)))
            except (TypeError, ValueError):
                return None
        elif t == "volume":
            if f.get("number") is None:
                return None
            try:
                volumes.append((src, int(f.get("number"))))
            except (TypeError, ValueError):
                return None
        elif t == "chapter":
            if f.get("number") is None:
                return None
            try:
                chapters.append((src, int(f.get("number"))))
            except (TypeError, ValueError):
                return None
        elif t == "delete":
            delete_set.add(Path(src).name.lower())
        # "other" (subtitles, .nfo/.txt) is handled by the subtitle pairing below or is
        # non-media and never appears in idx.

    if not episodes and not volumes and not chapters:
        return None

    if kind in ("anime", "tv"):
        return _plan_show(series_name, kind, episodes, idx, delete_set)
    if kind in ("manga", "comic"):
        return _plan_comic(series_name, kind, volumes, chapters, idx, delete_set)
    return _plan_novel(series_name, volumes, idx, delete_set)


def _plan_show(series_name, kind, episodes, idx, delete_set):
    folder = library.resolve_show_folder(series_name)
    if folder is None:
        return None
    profile = _show_profile(folder)
    if profile["owned"]:
        return None
    if profile.get("season_gap"):
        return None            # non-contiguous seasons: must be OWNED with authored titles
    if profile["episodes"] == 0:
        return None            # an empty folder has nothing to anchor numbering against
    if any(s == 0 for _, s, _ in episodes):
        return None                                    # specials are always locked+authored
    if not _episodes_confirmed(profile, episodes, folder):
        return None
    files = []
    placed = []
    for src, s, n in episodes:
        p = _locate(src, idx)
        if p is None or p.suffix.lower() not in config.VIDEO_EXTENSIONS:
            return None
        dst = _show_dst(folder, s, n, p.suffix.lower())
        files.append({"src": str(p), "dst_rel": dst, "season": s, "episode": n})
        placed.append((p, dst))
    # Pair sidecar subtitles with their episode by base name (Jellyfin pairs them too).
    for base, paths in idx.items():
        for p in paths:
            if p.suffix.lower() not in config.SUBTITLE_EXTENSIONS:
                continue
            m = [d for (v, d) in placed if v.stem == p.stem]
            if len(m) != 1:
                return None
            files.append({"src": str(p), "dst_rel": _swap_ext(m[0], p.suffix.lower())})
    if not _accounted(idx, files, delete_set):
        return None
    title = re.sub(r"\s*\(\d{4}\)\s*$", "", folder.name).strip()
    year_m = re.search(r"\((\d{4})\)\s*$", folder.name)
    return {
        "media_type": "show",
        "title": title,
        "year": int(year_m.group(1)) if year_m else None,
        "owned": False,
        "anime": kind == "anime",
        "existing_match": True,
        "reasoning": "deterministic fast-path (§ diagnosis 6.4): stored map + resolved "
                     "folder + confirmed numbering",
        "files": files,
    }


def _plan_comic(series_name, kind, volumes, chapters, idx, delete_set):
    items = [("volume", src, n) for (src, n) in volumes] + \
            [("chapter", src, n) for (src, n) in chapters]
    files = []
    for form, src, n in items:
        p = _locate(src, idx)
        if p is None:
            return None
        ext = p.suffix.lower()
        if ext in config.COMIC_CONVERT_EXTENSIONS:
            dst_ext = ".cbz"
        elif ext in config.COMIC_EXTENSIONS:
            dst_ext = ext
        else:
            return None
        colored = _looks_colored(p.name)
        folder = library.resolve_comic_folder(series_name, kind, colored)
        if folder is None:
            return None
        if not (_tokens(p.name) & _tokens(folder.name)):
            return None            # the archive is not clearly about this series
        num = f"v{n:02d}" if form == "volume" else f"c{n:04d}"
        # Use the resolved folder's REAL relative path (franchise-nested or not), so a
        # franchise member lands under its master folder automatically.
        rel = str(folder.relative_to(config.MEDIA_ROOT))
        dst = f"{rel}/{folder.name} {num}{dst_ext}"
        files.append({"src": str(p), "dst_rel": dst})
    if not files:
        return None
    if not _accounted(idx, files, delete_set):
        return None
    return {
        "media_type": "comic",
        "title": series_name,
        "existing_match": True,
        "reasoning": "deterministic fast-path (§ diagnosis 6.4): stored map + resolved "
                     "folder",
        "files": files,
    }


def _plan_novel(series_name, volumes, idx, delete_set):
    files = []
    for src, n in volumes:
        p = _locate(src, idx)
        if p is None:
            return None
        ext = p.suffix.lower()
        if ext not in config.NOVEL_EXTENSIONS:
            return None
        folder = library.resolve_novel_folder(series_name)
        if folder is None:
            return None
        if not (_tokens(p.name) & _tokens(folder.name)):
            return None            # the book is not clearly about this series
        dst = f"Novels/{folder.name}/{folder.name} v{n:02d}{ext}"
        files.append({"src": str(p), "dst_rel": dst})
    if not files:
        return None
    if not _accounted(idx, files, delete_set):
        return None
    return {
        "media_type": "novel",
        "title": series_name,
        "existing_match": True,
        "reasoning": "deterministic fast-path (§ diagnosis 6.4): stored map + resolved "
                     "folder",
        "files": files,
    }
