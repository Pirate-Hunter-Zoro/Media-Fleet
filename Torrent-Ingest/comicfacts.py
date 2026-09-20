"""What a comic archive's CONTENTS say about itself (HANDOFF 10.5a/b/d).

WHY CONTENTS, NOT FILENAMES. The owner's One Piece shelf holds a COLORED `v01` (PZG /
"Digital CC") and a GREY `v001` (VIZ / 1r0n), and neither filename carries a colour
marker. `dbhook._COLOR` searches the path, so the reconciler's "a coloured volume
supersedes a same-numbered grey one" rule could never fire, the DB's `colored` column
was driven by model prose instead of the files, and the shelf kept both runs
(HANDOFF 10.0 rows 1-2). The archives themselves name their edition in their entries:

    One Piece - c0001 (v001) - p002 [VIZ Media] [Digital] [1r0n].png     -> grey
    One Piece v001 (Colored) (Digital) (PZG)/One Piece - c0001 (v001)-... -> colored
    One Piece - d1078 (NA) - p000 [web] [VIZ Media] [suidana]{LQ}.jpg     -> a chapter

and they carry the chapter markers too, so a volume->chapter map and the volume
ceiling are computable OFFLINE from the shelf -- the §5 "compute the answer" step,
before MangaDex or a model is asked anything.

FAIL OPEN EVERYWHERE. An unreadable archive (pool evicted with no mount, `.cbr`,
a corrupt zip) returns None, and every caller treats None as "no opinion". The cache
records `(size, mtime)` so an archive that is re-downloaded is re-read once.
"""

from __future__ import annotations

import json
import re
import time
import zipfile
from pathlib import Path

import config

CACHE_PATH = config.STATE_DIR / "comic_archive_facts.json"
VOLUME_MAP_PATH = config.STATE_DIR / "manga_volume_map.json"
_CACHE_V = 1
_mem: dict[str, dict] = {}
_disk: dict | None = None

# Chapter markers the archives actually use: `c0001`, `d1078` (a chapter from the
# scanlation's `d` numbering), `ch. 12`, `chapter 12`, `#12`.
_CH_RE = re.compile(r"\b(?:chapter|chap|ch|c|d)\.?\s*(\d{1,4})\b", re.I)
# A volume association, in the entry or in the archive's own name: `(v001)`, `v001`,
# `v01`. Parenthesised wins.
_VOL_PAREN_RE = re.compile(r"\(v\s*(\d{1,4})\)", re.I)
_VOL_RE = re.compile(r"\bv\.?\s*(\d{1,4})\b", re.I)

# Edition evidence. `[Digital]` alone is NOT evidence: most coloured scans are digital
# too, and treating it as grey is what let grey win.
_COLOR_HINTS = (
    "colored", "coloured", "full color", "full-color", "full colour", "digital cc",
    "digital colored", "digital coloured", "colored council", "colorized",
    "colourised", "colorised", "remastered color", "(color)", "(colour)",
)
_GREY_HINTS = (
    "viz media", "1r0n", "grey", "gray", "monochrome", "black and white", "b&w",
    "grayscale", "greyscale",
)
_SKIP_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".webm")


def _disk_cache() -> dict:
    global _disk
    if _disk is None:
        try:
            blob = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            _disk = blob.get("files") if blob.get("v") == _CACHE_V else {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            _disk = {}
        if not isinstance(_disk, dict):
            _disk = {}
    return _disk


def save_cache() -> None:
    """Flush the in-memory facts cache. Called by the shelf-map build; harmless anywhere."""
    try:
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": _CACHE_V, "files": _disk_cache()}),
                       encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except OSError:
        pass


def _parse_entries(entries, name_hint=""):
    """The facts visible in an archive's entry names, or None when nothing can be said."""
    vols: dict[int, int] = {}
    chapters: set[int] = set()
    color = grey = False
    clues = []
    for raw in entries:
        s = str(raw)
        low = s.lower()
        if any(h in low for h in _COLOR_HINTS):
            color = True
            if not clues:
                clues.append(f"color hint in {Path(s).name[:60]}")
        elif any(h in low for h in _GREY_HINTS):
            grey = True
            if not clues:
                clues.append(f"grey hint in {Path(s).name[:60]}")
        base = Path(s).name
        if base.lower().endswith(_SKIP_EXT):
            base = str(Path(base).stem)
        if not base:
            continue
        vp = _VOL_PAREN_RE.search(base) or _VOL_PAREN_RE.search(s)
        vm = vp or _VOL_RE.search(base)
        if vm:
            try:
                n = int(vm.group(1))
            except ValueError:
                n = None
            if n is not None:
                vols[n] = vols.get(n, 0) + 1
        for m in _CH_RE.finditer(base):
            try:
                chapters.add(int(m.group(1)))
            except ValueError:
                pass
    # BARE-NUMBER CHAPTER PAGES. The seven mislabels' entries carry no `c`/`d` marker:
    # `1176-001.png`, `op_1151_t_012.png`. The tell is that EVERY page names the same
    # 3-4 digit number (the chapter); a real volume's raw pages run 001..200 and vary.
    if not chapters and not vols:
        per_entry = []
        for raw in entries:
            base = Path(str(raw)).name
            if base.lower().endswith(_SKIP_EXT):
                base = str(Path(base).stem)
            nums = [int(x) for x in re.findall(r"(?<![A-Za-z])(\d{3,4})(?![\d])", base)]
            per_entry.append(max(nums) if nums else None)
        if per_entry and all(n is not None for n in per_entry) \
                and len(set(per_entry)) == 1:
            chapters = {per_entry[0]}
    name_vp = _VOL_PAREN_RE.search(name_hint) or _VOL_RE.search(name_hint)
    name_m = _CH_RE.search(name_hint)
    name_num = None
    if name_vp:
        try:
            name_num = int(name_vp.group(1))
        except ValueError:
            name_num = None
    marker = None
    if name_m:
        try:
            marker = int(name_m.group(1))
        except ValueError:
            marker = None
    if not chapters and not vols and not color and not grey:
        return None
    if color:
        colored = True
    elif grey:
        colored = False
    else:
        colored = None
    # THE MISLABEL SHAPE (HANDOFF 10.0 row 3): the file is named `v1176.cbz` but its
    # entries carry chapter markers only -- `d1176 (NA)`, no `(v1176)` association.
    # Name the volume in the filename and the chapters in the entries, and the entries
    # win: a name is a label, the association is the data.
    if not vols and chapters and name_num is not None and name_num in chapters:
        return {
            "colored": colored,
            "volume": None,
            "chapters": sorted(chapters),
            "marker": marker,
            "kind": "chapter",
            "clue": "; ".join(clues),
        }
    volume = None
    if vols:
        volume = max(vols.items(), key=lambda kv: (kv[1], kv[0]))[0]
    elif name_num is not None:
        volume = name_num
    kind = None
    if volume is not None and chapters:
        kind = "volume"
    elif volume is None and (chapters or marker):
        kind = "chapter"
    return {
        "colored": colored,
        "volume": volume,
        "chapters": sorted(chapters),
        "marker": marker,
        "kind": kind,
        "clue": "; ".join(clues),
    }


def facts(path, name_hint=None):
    """Content facts for one archive, cached on `(size, mtime)`. None when unreadable."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None
    key = str(p)
    cached = _mem.get(key)
    if not cached:
        disk = _disk_cache().get(key)
        if disk and disk.get("size") == st.st_size and disk.get("mtime") == st.st_mtime:
            cached = disk
            _mem[key] = disk
    if cached and cached.get("size") == st.st_size and cached.get("mtime") == st.st_mtime:
        return cached.get("facts")
    hint = name_hint if name_hint is not None else p.name
    parsed = None
    if zipfile.is_zipfile(p):
        try:
            with zipfile.ZipFile(str(p)) as zf:
                parsed = _parse_entries(zf.namelist(), hint)
        except (zipfile.BadZipFile, OSError, RuntimeError):
            parsed = None
    rec = {"size": st.st_size, "mtime": st.st_mtime, "facts": parsed}
    _mem[key] = rec
    _disk_cache()[key] = rec
    return parsed


def cached_facts(path) -> dict | None:
    """`facts` without opening anything: disk-cache hit only, else None. Never raises.

    The reconcile daemon walks hundreds of shelf files per pass; re-opening archives
    through the mount hydrates from the pool, so only the offline map build pays that.
    """
    try:
        st = Path(path).stat()
    except OSError:
        return None
    rec = _mem.get(str(path)) or (_disk_cache().get(str(path)) or {})
    if rec.get("size") == st.st_size and rec.get("mtime") == st.st_mtime:
        return rec.get("facts")
    return None


def colour(path, name_hint=None):
    """`True`/`False`/`None` -- the archive's own edition evidence, filename fallback."""
    f = facts(path, name_hint=name_hint)
    if f and f.get("colored") is not None:
        return f["colored"]
    low = str(name_hint if name_hint is not None else path).lower()
    if any(h in low for h in _COLOR_HINTS):
        return True
    if any(h in low for h in _GREY_HINTS):
        return False
    return None


def volume_chapters(path, name_hint=None):
    """`(volume, [chapters])` the archive's entries state. `(None, [])` when unreadable."""
    f = facts(path, name_hint=name_hint)
    if not f:
        return None, []
    return f.get("volume"), list(f.get("chapters") or [])


def _series_facts(series_dir):
    sd = Path(series_dir)
    out = {}
    try:
        entries = sorted(p for p in sd.iterdir()
                         if p.is_file() and p.suffix.lower() in
                         (".cbz", ".cbt", ".cb7", ".zip"))
    except OSError:
        return out
    for p in entries:
        f = facts(p)
        if not f:
            continue
        out[p.name] = {"path": str(p), **f}
    return out


def shelf_map(series_dir, persist=True):
    """Volume -> chapter set computed from the shelf's own archives, offline.

    Returns `{volume_number: {"chapters": [ints], "colored": bool|None,
    "files": [names], "source": "shelf"}}`. A volume number is only claimed when an
    archive's entries associate their chapters with it (`(vNNN)`), so a mislabelled
    `v1176.cbz` whose entries say `d1176 (NA)` does not invent a volume 1176.
    """
    out: dict = {}
    for name, f in _series_facts(series_dir).items():
        v = f.get("volume")
        if v is None or f.get("kind") != "volume":
            continue
        rec = out.setdefault(int(v), {"chapters": set(), "colored": None,
                                      "files": [], "source": "shelf"})
        rec["chapters"] |= set(f.get("chapters") or ())
        if f.get("colored") is True:
            rec["colored"] = True
        elif f.get("colored") is False and rec["colored"] is None:
            rec["colored"] = False
        rec["files"].append(name)
    if persist:
        save_cache()
    return {int(k): {"chapters": sorted(v["chapters"]), "colored": v["colored"],
                     "files": sorted(v["files"]), "source": "shelf"}
            for k, v in sorted(out.items())}


def ceiling_from_shelf(series_dir):
    """Highest volume number the shelf's own archives present as a volume, or None."""
    sm = shelf_map(series_dir, persist=False)
    return max(sm) if sm else None


def _norm_name(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def ceiling_for(series_label):
    """The persisted volume ceiling for a series, or None (fail open).

    Reads `state/manga_volume_map.json` directly -- `library.py` uses this at plan
    validation time and cannot import `manga_volume_map` (which imports library).
    The larger of AniList's total and the shelf's highest real volume wins, exactly
    as `manga_volume_map.ceiling` does.
    """
    try:
        blob = json.loads(VOLUME_MAP_PATH.read_text(encoding="utf-8"))
        series = blob.get("series") or {}
        key = _norm_name(series_label)
        entry = series.get(key)
        if entry is None:
            for k, v in series.items():
                if _norm_name(k) == key:
                    entry = v
                    break
        if not isinstance(entry, dict):
            return None
        vals = []
        tv = entry.get("total_volumes")
        if isinstance(tv, int) and tv > 0:
            vals.append(tv)
        sc = entry.get("shelf_ceiling")
        if isinstance(sc, int) and sc > 0:
            vals.append(sc)
        return max(vals) if vals else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def chapter_ceiling_for(series_label):
    """`(total_chapters, finished)` for a series, or `(None, False)` (fail open).

    THE FACT THAT CATCHES A CHAPTER IN THE WRONG SERIES. `Chapter 1093.zip` was filed as
    `Jujutsu Kaisen c1093.cbz` (2026-09-20) -- but Jujutsu Kaisen ended at 271 chapters,
    so 1093 cannot be one of its chapters; it is One Piece's. The total is a bound only
    for a series AniList reports as FINISHED/CANCELLED: an ONGOING series (One Piece)
    legitimately has chapters past its last collected volume, and refusing those would
    reject the latest chapter every week. Unknown total or unknown status -> no bound.

    Reads the persisted map directly, like `ceiling_for`, because `library.py` uses this
    at plan-validation time.
    """
    try:
        blob = json.loads(VOLUME_MAP_PATH.read_text(encoding="utf-8"))
        series = blob.get("series") or {}
        key = _norm_name(series_label)
        entry = series.get(key)
        if entry is None:
            for k, v in series.items():
                if _norm_name(k) == key:
                    entry = v
                    break
        if not isinstance(entry, dict):
            return None, False
        total = entry.get("total_chapters")
        status = str(entry.get("anilist_status") or "").upper()
        if isinstance(total, int) and total > 0 and status in ("FINISHED", "CANCELLED"):
            return total, True
        return None, False
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None, False


def marker_from_name(name):
    """`("volume"|"chapter", int)` a filename claims, or None. Volume wins on `vNNNN`."""
    base = Path(str(name)).name
    m = _VOL_RE.search(base)
    if m:
        try:
            return "volume", int(m.group(1))
        except ValueError:
            pass
    m = _CH_RE.search(base)
    if m:
        try:
            return "chapter", int(m.group(1))
        except ValueError:
            pass
    return None
