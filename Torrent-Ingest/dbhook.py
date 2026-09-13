"""Record a filed plan into the shared library DB (series + media + supersede).

Torrent-Ingest's `apply_plan` lands files into the library; this module mirrors that into
`library.db` so the library's master manifest stays in step with what is actually on disk.
Every filed file is inserted as a `media` row under its SERIES, and anything the plan
supersedes (a volume replacing its chapters, a higher-definition replacing a lower one) is
marked `superseded`, so the manifest records the better copy and not both.

The DB module is `librarybrain/librarydb.py`. It used to live in Torrent-Searcher and moved
here when discovery was removed (2026-09-10); it is still imported BY PATH rather than as a
normal module, because it is stdlib-only by contract and must never pull a `config`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Load the searcher's stdlib-only `librarydb` by file path, WITHOUT inserting the
# searcher's directory onto sys.path. Inserting it shadowed this repo's own `ingest`/
# `config` modules for every later import in the process (see identify.py).
import importlib.util as _ilu

_librarydb_spec = _ilu.spec_from_file_location(
    "librarydb", "/Users/mikeyferguson/Developer/Media-Fleet/Torrent-Ingest/librarybrain/librarydb.py")
librarydb = _ilu.module_from_spec(_librarydb_spec)
sys.modules["librarydb"] = librarydb
_librarydb_spec.loader.exec_module(librarydb)

_KIND = {"show": "anime", "movie": "movie", "comic": "manga",
         "novel": "lightnovel", "mixed": "anime"}

_EP = re.compile(r"[Ss]\d+[Ee](\d+)")
_VOL = re.compile(r"\bv(\d{1,4})\b", re.IGNORECASE)
_CH = re.compile(r"\b(?:ch|chapter|chap)\.?\s*(\d{1,4})\b", re.IGNORECASE)
# Bare "cNNN" chapter marker ("c0001.cbz"), the SAME pattern the searcher's
# parse.manga_kind trusts. The ingest used to miss it, so a manga filed as "c0001.cbz"
# chapters was recorded as `collection` and the searcher then re-downloaded the volume it
# already held -- the classification mismatch behind the redundant Dumbbell manga grab.
_C_BARE = re.compile(r"\bc\.?\s*(\d{2,4})\b", re.IGNORECASE)
_HASH = re.compile(r"#\s*(\d{1,4})\b")
_COLOR = re.compile(r"colored|full[ -]?color|colour", re.IGNORECASE)


def _res(name: str) -> int:
    """Resolution tier on the SAME 0-5 scale the searcher compares against
    (`parse.resolution_rank` / `config.resolution_bonus`): 5=UHD/4K, 4=1080p, 3=720p,
    2=480p/576p, 1=DVD, 0=unknown. The old 1-4 scale disagreed with the searcher, so a
    correctly-recorded 1080p copy still read as "worse" than a 1080p candidate and got
    re-downloaded as an "upgrade"."""
    low = name.lower()
    if any(k in low for k in ("2160p", "4k", "uhd")):
        return 5
    if "1080p" in low or "1080i" in low:
        return 4
    if "720p" in low:
        return 3
    if any(k in low for k in ("480p", "576p")):
        return 2
    if any(k in low for k in (" dvd", "dvdr", "dvdrip")):
        return 1
    return 0


def _record_file(conn, sid, f) -> None:
    rel = f.get("dst_rel") or ""
    parts = rel.split("/")
    top = parts[0] if parts else ""
    name = parts[-1] if parts else ""
    # The library's own filename is clean (no resolution), but the plan's `src` is the
    # original release path, which DOES carry the resolution marker -- record it so the
    # searcher's upgrade test has a real quality to compare against (not the default 0).
    src_name = f.get("src") or name
    if top == "Shows":
        season = f.get("season")
        ep = f.get("episode")
        if season is None:
            m = re.search(r"Season\s*(\d+)", rel, re.IGNORECASE)
            season = int(m.group(1)) if m else 1
        if ep is None:
            m = _EP.search(name)
            ep = int(m.group(1)) if m else None
        if ep is not None:
            librarydb.upsert_media(conn, sid, "episode", int(season), ep, title=name,
                                resolution=_res(src_name))
    elif top == "Movies":
        librarydb.upsert_media(conn, sid, "movie", None, None, title=name,
                            resolution=_res(src_name))
    elif top == "Comics":
        colored = bool(_COLOR.search(rel))
        m = _VOL.search(name)
        if m:
            librarydb.upsert_media(conn, sid, "volume", None, int(m.group(1)), title=name,
                                colored=colored)
            return
        m = _CH.search(name) or _C_BARE.search(name) or _HASH.search(name)
        if m:
            librarydb.upsert_media(conn, sid, "chapter", None, int(m.group(1)), title=name,
                                colored=colored)
            return
        librarydb.upsert_media(conn, sid, "collection", None, None, title=name, colored=colored)
    elif top == "Novels":
        m = _VOL.search(name) or re.search(r"^\s*(\d{1,4})\s*[-–—]", name)
        librarydb.upsert_media(conn, sid, "volume", None, int(m.group(1)) if m else None,
                            title=name)


def _record_supersede(conn, sid, rel) -> None:
    name = rel.split("/")[-1]
    m = _CH.search(name) or _C_BARE.search(name) or _HASH.search(name)
    if m:
        librarydb.mark_superseded(conn, sid, "chapter", None, int(m.group(1)), int(m.group(1)))
        return
    m = _VOL.search(name)
    if m:
        librarydb.mark_superseded(conn, sid, "volume", None, int(m.group(1)), int(m.group(1)))
        return
    m = _EP.search(name)
    if m:
        librarydb.mark_superseded(conn, sid, "episode", None, int(m.group(1)), int(m.group(1)))


def _supersede(conn, sid: int, mtype: str, season, number) -> int:
    """Mark one series' item rows superseded. `season`/`number` NULL matches NULL."""
    if season is None and number is None:
        cur = conn.execute(
            "UPDATE media SET status='superseded' WHERE series_id=? AND mtype=? "
            "AND status!='superseded'", (sid, mtype))
    else:
        cur = conn.execute(
            "UPDATE media SET status='superseded' WHERE series_id=? AND mtype=? "
            "AND season IS ? AND number IS ? AND status!='superseded'",
            (sid, mtype, season, number))
    return cur.rowcount


def _comic_item(name: str) -> tuple[str, int | None]:
    """(mtype, number) for a comic filename, parsed exactly as `_record_file` recorded it."""
    m = _VOL.search(name)
    if m:
        return "volume", int(m.group(1))
    m = _CH.search(name) or _C_BARE.search(name) or _HASH.search(name)
    if m:
        return "chapter", int(m.group(1))
    return "collection", None


def _comic_candidates(parts: list[str], name: str) -> list[str]:
    """Folder/file names that might BE the series, most specific first.

    Manga nests two ways: `Comics/Manga/<Series>/` and, under the franchise layout,
    `Comics/Manga/<Franchise>/<Series>/`; western comics nest under their own category
    (`Comics/Star Wars Comics/Omnibuses/...`). Joining the trailing folder chain walks
    from the most specific to the least, so `Parasyte/Full Color Collection/` tries
    "Parasyte Full Color Collection" before "Full Color Collection". The file stem is
    the fallback for a loose comic, with its volume/chapter marker stripped so an
    orphaned `Darth Vader v01.cbz` can still name the Darth Vader series.
    """
    if len(parts) == 2:                       # a loose file at the Comics root
        return [Path(name).stem]
    dirs = parts[1:-1]
    if dirs and dirs[0] == "Manga":
        dirs = dirs[1:]
    # Every contiguous join, longest first. The series can be the WHOLE chain
    # (`Parasyte/Full Color Collection/`), the first folder with the rest as a
    # grouping (`Asterix the Gaul/Extras/`), or a suffix (`.../Rebellion/`).
    out = [" ".join(dirs[i:j]) for i in range(len(dirs)) for j in range(i + 1, len(dirs) + 1)]
    out.sort(key=len, reverse=True)
    stem = Path(name).stem
    out.append(stem)
    plain = _HASH.sub(" ", _C_BARE.sub(" ", _CH.sub(" ", _VOL.sub(" ", stem))))
    plain = re.sub(r"\s+", " ", plain).strip(" -_")
    if plain and plain != stem:
        out.append(plain)
    return out


def _supersede_comic(conn, parts: list[str], name: str) -> int:
    """Supersede a purged comic's row, matched by folder chain with the first hit winning."""
    mtype, number = _comic_item(name)
    for cand in _comic_candidates(parts, name):
        norm = librarydb._normalize(cand)
        if not norm:
            continue
        rows = conn.execute(
            "SELECT id FROM series WHERE norm=? AND kind IN ('manga','comic')",
            (norm,)).fetchall()
        if not rows:
            continue
        changed = 0
        for row in rows:
            if mtype == "collection":
                # A collection filename carries no number, so it can only name which row
                # to drop when there is exactly one candidate; guessing otherwise would
                # mark a file still on the shelf not-owned.
                n = conn.execute(
                    "SELECT COUNT(*) FROM media WHERE series_id=? AND mtype='collection' "
                    "AND status!='superseded'", (row["id"],)).fetchone()[0]
                if n != 1:
                    continue
            changed += _supersede(conn, row["id"], mtype, None, number)
        return changed
    return 0


def _supersede_path(conn, rel: str) -> int:
    """Supersede the rows a single purged library path names. 0 when it cannot say."""
    parts = rel.split("/")
    if not parts or not parts[0]:
        return 0
    top, name = parts[0], parts[-1]
    changed = 0
    if top == "Shows" and len(parts) >= 4:
        title = parts[1]
        season = next((int(m.group(1)) for p in parts[2:-1]
                       for m in [re.fullmatch(r"[Ss]eason\s*(\d{1,3})", p)] if m), None)
        m = re.search(r"[Ss](\d{1,3})[Ee](\d{1,4})", name)
        if m:
            season = season if season is not None else int(m.group(1))
            number = int(m.group(2))
        else:
            m = re.search(r"[Ee](\d{1,4})", name)
            if not m or season is None:
                return 0                      # a special with no number: cannot match
            number = int(m.group(1))
        norm = librarydb._normalize(title)
        for row in conn.execute("SELECT id FROM series WHERE norm=? AND kind IN "
                                "('anime','tv')", (norm,)).fetchall():
            changed += _supersede(conn, row["id"], "episode", season, number)
    elif top == "Movies" and len(parts) >= 2:
        title = parts[1] if len(parts) > 2 else Path(name).stem
        norm = librarydb._normalize(title)
        for row in conn.execute("SELECT id FROM series WHERE norm=? AND kind='movie'",
                                (norm,)).fetchall():
            changed += _supersede(conn, row["id"], "movie", None, None)
    elif top == "Comics" and len(parts) >= 2:
        changed += _supersede_comic(conn, parts, name)
    # Novels/Books are not reached: the reaper does not track `.pdf`/`.epub`, so a
    # purge can never name one.
    return changed


def supersede_purged(conn, relpaths) -> dict:
    """Mark the rows for explicitly-purged paths `superseded`. Returns counts.

    The inverse of `record_plan`, called by the reaper once a purge is VERIFIED
    (a survivor's pool copy still exists, so only `purged_ok` paths qualify). Without
    it the ledger keeps claiming purged content and the acceptance gate refuses the
    re-drop as "already owned" -- the exact loop OPERATING §8 documents. Unlike the
    inventory-driven stale pass, the evidence here is an explicit deletion, so no
    source is spared: a purged `new.txt` title is not an in-flight acquisition.
    """
    res = {"superseded": 0, "paths": 0}
    for rel in sorted(relpaths or ()):
        try:
            n = _supersede_path(conn, rel)
        except Exception:  # noqa: BLE001 -- one odd path must not drop the rest
            continue
        if n:
            res["superseded"] += n
            res["paths"] += 1
    return res


def record_purge(relpaths) -> dict:
    """Mirror a verified purge into the library DB. Never raises, like `record_plan`."""
    try:
        conn = librarydb.connect()
    except Exception:  # noqa: BLE001
        return {"superseded": 0, "paths": 0}
    try:
        res = supersede_purged(conn, relpaths)
        conn.commit()
        return res
    except Exception:  # noqa: BLE001
        conn.rollback()
        return {"superseded": 0, "paths": 0}
    finally:
        conn.close()


def record_plan(plan: dict) -> None:
    """Mirror a successfully-applied plan into the library DB. Never raises: a DB write
    problem must not undo an already-filed torrent."""
    title = (plan or {}).get("title") or ""
    if not title:
        return
    try:
        conn = librarydb.connect()
    except Exception:  # noqa: BLE001
        return
    try:
        sid = librarydb.add_series(conn, title, _KIND.get(plan.get("media_type"), "anime"),
                                   source="ingest")
        for f in plan.get("files") or []:
            try:
                _record_file(conn, sid, f)
            except Exception:  # noqa: BLE001 -- one unparseable file must not drop the rest
                continue
        for sp in plan.get("supersedes") or []:
            try:
                _record_supersede(conn, sid, sp)
            except Exception:  # noqa: BLE001
                continue
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
    finally:
        conn.close()
