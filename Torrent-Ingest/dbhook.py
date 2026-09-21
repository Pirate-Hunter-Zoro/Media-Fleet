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

import json
import re
import sys
from pathlib import Path

import config

# Load the searcher's stdlib-only `librarydb` by file path, WITHOUT inserting the
# searcher's directory onto sys.path. Inserting it shadowed this repo's own `ingest`/
# `config` modules for every later import in the process (see identify.py).
import importlib.util as _ilu

_librarydb_spec = _ilu.spec_from_file_location(
    "librarydb", str(config.PROJECT_ROOT / "librarybrain" / "librarydb.py"))
librarydb = _ilu.module_from_spec(_librarydb_spec)
sys.modules["librarydb"] = librarydb
_librarydb_spec.loader.exec_module(librarydb)

# `comic` is deliberately NOT in this map's reach any more. A comic plan's DB kind
# depends on WHERE it is filed: `Comics/Manga/<Series>/` is a manga, `Comics/<Series>/`
# is a western comic. The old blanket `"comic": "manga"` created a duplicate `manga`
# series beside every library-seeded western one -- 25 live norm pairs by 2026-09-14,
# and the reason the identify model then split the ElfQuest re-acquisition across
# `Comics/ElfQuest` and `Comics/Manga/ElfQuest`. `_comic_kind()` reads the plan's own
# destinations instead.
_KIND = {"show": "anime", "movie": "movie",
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


def _plan_facts(f, name):
    """The source archive's content facts for a comic plan file, or None (fail open)."""
    try:
        import comicfacts
        src = f.get("src")
        if src:
            return comicfacts.facts(src, name_hint=name)
    except Exception:                                            # noqa: BLE001
        pass
    return None


def _plan_colored(f, rel, name) -> bool:
    """Whether a comic filing is the coloured edition (10.5d): the archive's entries
    first, the path/filename as fallback, unknown treated as grey."""
    facts = _plan_facts(f, name)
    if facts and facts.get("colored") is not None:
        return bool(facts["colored"])
    return bool(_COLOR.search(rel))


def _plan_comic_kind(f, name) -> tuple[str, int | None]:
    """`(mtype, number)` for a comic filing. The archive's entries win over the name:
    a `v1176.cbz` whose entries are chapter pages is a chapter, not volume 1176."""
    facts = _plan_facts(f, name)
    if facts and facts.get("kind") == "chapter" and _VOL.search(name):
        ch = next((c for c in (facts.get("chapters") or []) if c), None)
        if ch:
            return "chapter", int(ch)
    m = _VOL.search(name)
    if m:
        return "volume", int(m.group(1))
    m = _CH.search(name) or _C_BARE.search(name) or _HASH.search(name)
    if m:
        return "chapter", int(m.group(1))
    return "collection", None


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
        # Colour and kind come from the SOURCE ARCHIVE's own entries when it is readable
        # (HANDOFF 10.5d) -- the plan's destination filenames carry no colour marker, and
        # a `vNNNN` whose contents are chapter pages must be recorded as a chapter.
        colored = _plan_colored(f, rel, name)
        mtype, number = _plan_comic_kind(f, name)
        if mtype in ("volume", "chapter") and number is not None:
            librarydb.upsert_media(conn, sid, mtype, None, int(number), title=name,
                                   colored=colored)
            return
        librarydb.upsert_media(conn, sid, "collection", None, None, title=name,
                               colored=colored)
    elif top == "Novels":
        m = _VOL.search(name) or re.search(r"^\s*(\d{1,4})\s*[-–—]", name)
        librarydb.upsert_media(conn, sid, "volume", None, int(m.group(1)) if m else None,
                            title=name)


def _path_colored(rel):
    """The colour of a library comic path, from its archive contents (10.5d). None when
    it cannot be read -- and None means grey-only for supersede, never coloured."""
    try:
        import comicfacts
        for root in (config.MEDIAFS_MOUNT, config.MEDIA_ROOT):
            p = root / rel
            if p.exists():
                c = comicfacts.colour(p, name_hint=Path(rel).name)
                if c is not None:
                    return c
    except Exception:                                            # noqa: BLE001
        pass
    return None


def _record_supersede(conn, sid, rel) -> None:
    name = rel.split("/")[-1]
    colored = _path_colored(rel)
    m = _CH.search(name) or _C_BARE.search(name) or _HASH.search(name)
    if m:
        librarydb.mark_superseded(conn, sid, "chapter", None, int(m.group(1)),
                                  int(m.group(1)), colored=colored)
        return
    m = _VOL.search(name)
    if m:
        librarydb.mark_superseded(conn, sid, "volume", None, int(m.group(1)),
                                  int(m.group(1)), colored=colored)
        return
    m = _EP.search(name)
    if m:
        librarydb.mark_superseded(conn, sid, "episode", None, int(m.group(1)), int(m.group(1)))


def _supersede(conn, sid: int, mtype: str, season, number, colored=None) -> int:
    """Mark one series' item rows superseded. `season`/`number` NULL matches NULL.

    Comic rows hold BOTH editions as separate rows (10.5d). When only one row exists for
    the number, that row is the record and it goes, whatever its colour -- a stale
    pre-colour row must not survive a purge forever. When both exist, only the edition
    the purged path's own archive names is superseded; `colored=None` means unknown and
    then ONLY the grey row goes, because a grey or unknown file may never supersede a
    coloured one (owner rule, 10.5d).
    """
    if season is None and number is None:
        cur = conn.execute(
            "UPDATE media SET status='superseded' WHERE series_id=? AND mtype=? "
            "AND status!='superseded'", (sid, mtype))
        return cur.rowcount
    if mtype in ("volume", "chapter") and number is not None:
        rows = conn.execute(
            "SELECT id, colored FROM media WHERE series_id=? AND mtype=? "
            "AND season IS ? AND number IS ? AND status!='superseded'",
            (sid, mtype, season, number)).fetchall()
        if len(rows) <= 1:
            if not rows:
                return 0
            conn.execute("UPDATE media SET status='superseded' WHERE id=?",
                         (rows[0]["id"],))
            return 1
        want = 1 if colored else 0
        cur = conn.execute(
            "UPDATE media SET status='superseded' WHERE series_id=? AND mtype=? "
            "AND season IS ? AND number IS ? AND colored=? AND status!='superseded'",
            (sid, mtype, season, number, want))
        return cur.rowcount
    cur = conn.execute(
        "UPDATE media SET status='superseded' WHERE series_id=? AND mtype=? "
        "AND season IS ? AND number IS ? AND status!='superseded'",
        (sid, mtype, season, number))
    return cur.rowcount


def _comic_item(name: str, colored=None) -> tuple[str, int | None]:
    """(mtype, number) for a comic filename, parsed exactly as `_record_file` recorded it.

    `colored` is accepted for symmetry with the supersede SQL and is not used to change
    the parse; the kind/number a purged path names is the same in both editions."""
    _ = colored
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


def _supersede_comic(conn, parts: list[str], name: str, rel: str = "") -> int:
    """Supersede a purged comic's row, matched by folder chain with the first hit winning."""
    colored = _path_colored(rel) if rel else None
    mtype, number = _comic_item(name, colored)
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
            changed += _supersede(conn, row["id"], mtype, None, number, colored=colored)
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
        changed += _supersede_comic(conn, parts, name, rel)
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


def _item_state(conn, sid: int, mtype: str, season, number) -> str:
    """`superseded` | `owned` | `none` for one series' rows of one item.

    Color-aware by construction: a colored row still owned makes the item `owned` even
    when its grey twin was superseded, which is the owner's rule that a grey purge must
    never read as "the content is gone" while the colored copy is the one kept (10.5d).
    """
    rows = conn.execute(
        "SELECT status FROM media WHERE series_id=? AND mtype=? AND season IS ? "
        "AND number IS ?", (sid, mtype, season, number)).fetchall()
    if not rows:
        return "none"
    if any(r["status"] != "superseded" for r in rows):
        return "owned"
    return "superseded"


def _states_verdict(states: list[str]) -> bool:
    """True when at least one series recorded the item and none still owns it.

    A same-named duplicate series that never recorded the item (`none`) does not veto a
    sibling's supersede: `_supersede_path` updates every series that HAS the row, and a
    series with no row never claimed the item in the first place."""
    return any(s == "superseded" for s in states) \
        and all(s in ("superseded", "none") for s in states)


def _path_superseded(conn, rel: str) -> bool:
    """True when `rel` names content library.db records as deliberately superseded.

    The read-only mirror of `_supersede_path`'s own path->row mapping, so the statement
    it writes and the statement this reads cannot drift. A shape it cannot identify (a
    numberless special, a collection, an unknown series) answers False: no evidence is
    never evidence of a purge, because the caller's fallback is to re-queue.
    """
    parts = rel.split("/")
    if not parts or not parts[0]:
        return False
    top, name = parts[0], parts[-1]
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
                return False                 # a special with no number: cannot match
            number = int(m.group(1))
        norm = librarydb._normalize(title)
        rows = conn.execute("SELECT id FROM series WHERE norm=? AND kind IN "
                            "('anime','tv')", (norm,)).fetchall()
        return _states_verdict([_item_state(conn, r["id"], "episode", season, number)
                                for r in rows])
    if top == "Movies" and len(parts) >= 2:
        title = parts[1] if len(parts) > 2 else Path(name).stem
        norm = librarydb._normalize(title)
        rows = conn.execute("SELECT id FROM series WHERE norm=? AND kind='movie'",
                            (norm,)).fetchall()
        return _states_verdict([_item_state(conn, r["id"], "movie", None, None)
                                for r in rows])
    if top == "Comics" and len(parts) >= 2:
        mtype, number = _comic_item(name)
        if mtype == "collection":
            # `_supersede_comic` only drops a collection when exactly one row could be
            # meant; once dropped that "exactly one" test no longer holds, so the read
            # side refuses to guess instead of pretending there is evidence.
            return False
        for cand in _comic_candidates(parts, name):
            norm = librarydb._normalize(cand)
            if not norm:
                continue
            rows = conn.execute(
                "SELECT id FROM series WHERE norm=? AND kind IN ('manga','comic')",
                (norm,)).fetchall()
            if not rows:
                continue
            return _states_verdict([_item_state(conn, r["id"], mtype, None, number)
                                    for r in rows])
    return False


def purged_evidence(relpaths, conn=None) -> bool:
    """True only when EVERY path names an item the DB records as deliberately superseded.

    Called by `reconcile.audit` before it re-queues a completion whose files are absent
    from the inventory and both local tiers: a chapter the fleet purged because a volume
    covers it must not be re-downloaded, re-filed and purged again. False whenever the
    DB cannot say (unknown series, an owned row, a collection, any error) and false for
    an empty set -- an uncertain record keeps the historical re-queue path."""
    rels = [r for r in (relpaths or []) if r]
    if not rels:
        return False
    own = conn is None
    if own:
        try:
            conn = librarydb.connect()
        except Exception:  # noqa: BLE001
            return False
    try:
        return all(_path_superseded(conn, rel) for rel in rels)
    except Exception:  # noqa: BLE001
        return False
    finally:
        if own and conn is not None:
            conn.close()


def _comic_kind(plan: dict) -> str | None:
    """`manga`/`comic` from where the plan's own files land; None when none are comics.

    The destination is the only witness that survives `apply_plan`: a manga is filed under
    `Comics/Manga/`, a western comic directly under `Comics/`. Anything mixed falls back to
    `comic` (the western root), which is the safe side of the ElfQuest split.
    """
    rels = [str(f.get("dst_rel") or "") for f in (plan.get("files") or [])]
    comics = [r for r in rels if r.startswith("Comics/")]
    if not comics:
        return None
    manga = [r for r in comics if r.startswith("Comics/Manga/")]
    return "manga" if len(manga) == len(comics) else "comic"


def _plan_kind(plan: dict) -> str:
    media_type = plan.get("media_type")
    if media_type == "comic":
        return _comic_kind(plan) or "comic"
    if media_type == "mixed":
        return _comic_kind(plan) or _KIND.get(media_type, "anime")
    return _KIND.get(media_type, "anime")


def _request_yacreader_refresh(plan: dict) -> None:
    """Drop the marker `library_supervisor` watches so YacReader re-indexes new comics.

    The app never notices the filesystem on its own, and with a stale index a filed comic
    simply does not exist to the reader (the 2026-09-14 ElfQuest report). The supervisor
    consumes this on its next tick. Never raises.
    """
    if not any(str(f.get("dst_rel") or "").startswith("Comics/")
               for f in (plan.get("files") or [])):
        return
    try:
        config.YACREADER_REFRESH_MARKER.parent.mkdir(parents=True, exist_ok=True)
        config.YACREADER_REFRESH_MARKER.write_text(
            f"comics filed at {librarydb.now()}\n", encoding="utf-8")
    except OSError:
        pass


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
        sid = librarydb.add_series(conn, title, _plan_kind(plan), source="ingest")
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
        _request_yacreader_refresh(plan)
    except Exception:  # noqa: BLE001
        conn.rollback()
    finally:
        conn.close()


# --- comic-kind reconciliation (the reaper's other half) ----------------------
#
# WHY THIS EXISTS
#     `record_plan` used to record EVERY comic as kind `manga`, so each western series
#     the library had already seeded as kind `comic` acquired a duplicate `manga` twin --
#     25 live norm pairs by 2026-09-14, and the reason the identify model split the
#     ElfQuest re-acquisition across `Comics/ElfQuest` and `Comics/Manga/ElfQuest`. The
#     kind is fixed at the source now, but the history remains: owned rows sit under the
#     wrong twin, and the twin that owns them can steer a future drop into the wrong root.
#
#     The reaper calls `reconcile_comics()` after every verified purge, so a title's
#     cleanup now includes its DB kind split -- the manual `sqlite3` session the owner
#     had to ask for (2026-09-14) is not part of the runbook any more.
#
# WHAT MAKES THIS SAFE
#     The only witness used is the POOL, and only when it is unambiguous: a norm whose
#     files all live under one root is folded to that kind; a norm with files under both
#     roots, or none at all, is left alone. Item-level supersede needs a numbered series
#     (a comic filename with no `vNN`/`cNNN` has no key, so `collection` rows are never
#     judged). Every failure is fail-open: a broken inventory or one odd norm cannot
#     touch the ledger, and the purge it followed is unaffected.

_COMIC_EXT = tuple(config.COMIC_EXTENSIONS)


def comic_inventory(inventory_path=None) -> dict:
    """`{norm: {"kinds": {...}, "volumes": {...}, "chapters": {...}}}` from the pool.

    Every file is attributed to EVERY folder-chain join candidate -- the same matcher a
    purge uses -- because a comic series nests at an unpredictable depth
    (`Comics/Star Wars Comics/Star Wars Modern Era Epic Collection/Darth Vader/...`).
    """
    path = Path(inventory_path) if inventory_path else config.MEDIA_SYNCER_INVENTORY
    try:
        keys = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for rel in keys:
        parts = str(rel).split("/")
        if len(parts) < 2 or parts[0] != "Comics":
            continue
        name = parts[-1]
        if not name.lower().endswith(_COMIC_EXT):
            continue
        root_kind = "manga" if parts[1] == "Manga" else "comic"
        for cand in _comic_candidates(parts, name):
            norm = librarydb._normalize(cand)
            if not norm:
                continue
            e = out.setdefault(norm, {"kinds": set(), "volumes": set(), "chapters": set()})
            e["kinds"].add(root_kind)
            m = _VOL.search(name)
            if m:
                e["volumes"].add(int(m.group(1)))
                continue
            m = _CH.search(name) or _C_BARE.search(name) or _HASH.search(name)
            if m:
                e["chapters"].add(int(m.group(1)))
    return out


def fold_comic_pairs(conn, inv: dict) -> dict:
    """Fold each same-norm comic/manga pair into the kind the pool shelves its files under.

    Rows move to the survivor; a row whose item is already recorded there is dropped
    (never losing ownership: if the survivor's copy was superseded and the loser's is
    owned, the survivor's row becomes owned first). Torrents and aliases follow. A pair
    whose norm the inventory cannot place, or places under both roots, is skipped.
    """
    res = {"folded": 0, "moved": 0, "dropped": 0, "skipped": 0}
    groups: dict[str, list] = {}
    for r in conn.execute("SELECT id,name,norm,kind FROM series "
                          "WHERE kind IN ('comic','manga') ORDER BY id").fetchall():
        groups.setdefault(r["norm"], []).append(r)
    for _norm, group in sorted(groups.items()):
        if {r["kind"] for r in group} != {"comic", "manga"}:
            continue
        entry = inv.get(_norm)
        if not entry or len(entry["kinds"]) != 1:
            res["skipped"] += 1
            continue
        target_kind = next(iter(entry["kinds"]))
        target = next(r for r in group if r["kind"] == target_kind)
        for loser in (r for r in group if r["id"] != target["id"]):
            for m in conn.execute(
                    "SELECT id,mtype,season,number,status,colored FROM media "
                    "WHERE series_id=?",
                    (loser["id"],)).fetchall():
                # Colour is part of a comic row's identity (10.5d): a coloured row must
                # not be "deduped" against the loser's grey row of the same number.
                dup = conn.execute(
                    "SELECT id,status FROM media WHERE series_id=? AND mtype IS ? "
                    "AND season IS ? AND number IS ? AND colored IS ? ORDER BY id",
                    (target["id"], m["mtype"], m["season"], m["number"],
                     m["colored"])).fetchall()
                if dup:
                    keep = dup[0]
                    if m["status"] == "owned" and keep["status"] != "owned":
                        conn.execute("UPDATE media SET status='owned' WHERE id=?",
                                     (keep["id"],))
                    conn.execute("DELETE FROM media WHERE id=?", (m["id"],))
                    res["dropped"] += 1
                else:
                    conn.execute("UPDATE media SET series_id=? WHERE id=?",
                                 (target["id"], m["id"]))
                    res["moved"] += 1
            conn.execute("UPDATE torrents SET series_id=? WHERE series_id=?",
                         (target["id"], loser["id"]))
            for alias, anorm in conn.execute(
                    "SELECT alias,norm FROM series_alias WHERE series_id=?",
                    (loser["id"],)).fetchall():
                conn.execute("INSERT OR IGNORE INTO series_alias (series_id,alias,norm) "
                             "VALUES (?,?,?)", (target["id"], alias, anorm))
            conn.execute("DELETE FROM series_alias WHERE series_id=?", (loser["id"],))
            conn.execute("DELETE FROM series WHERE id=?", (loser["id"],))
            res["folded"] += 1
    return res


def supersede_absent_comics(conn, inv: dict) -> dict:
    """Supersede numbered comic rows the pool no longer holds. Fail-open by design.

    Only a series the pool places under exactly ONE root and with at least one numbered
    item is judged. `collection` rows (no marker in the filename) are never touched, and
    a norm with no pool files is not judged at all -- the unverifiable is skipped, not
    guessed, which is the rule the manual reconcile already follows.
    """
    res = {"superseded": 0, "judged_series": 0}
    for r in conn.execute("SELECT id,norm FROM series "
                          "WHERE kind IN ('comic','manga') ORDER BY id").fetchall():
        entry = inv.get(r["norm"])
        if not entry or len(entry["kinds"]) != 1:
            continue
        if not (entry["volumes"] or entry["chapters"]):
            continue
        keys = {librarydb.item_key("volume", None, n) for n in entry["volumes"]}
        keys |= {librarydb.item_key("chapter", None, n) for n in entry["chapters"]}
        res["judged_series"] += 1
        for m in librarydb.owned_media(conn, r["id"]):
            if m["mtype"] not in ("volume", "chapter"):
                continue
            if librarydb.item_key(m["mtype"], m["season"], m["number"]) not in keys:
                conn.execute("UPDATE media SET status='superseded' WHERE id=?", (m["id"],))
                res["superseded"] += 1
    return res


def reconcile_comics(conn=None, inventory_path=None) -> dict:
    """Fold kind-split comic series and supersede absent items on one connection.

    Called by the reaper after every verified purge (`_sweep_library_db`) and by
    `scripts/reconcile_library_db.py --apply`. Never raises: the purge it follows must
    not fail because the ledger could not be tidied.
    """
    empty = {"folded": 0, "moved": 0, "dropped": 0, "skipped": 0,
             "superseded": 0, "judged_series": 0}
    inv = comic_inventory(inventory_path)
    if not inv:
        return dict(empty, note="no comic inventory")
    own = conn is None
    if own:
        try:
            conn = librarydb.connect()
        except Exception:  # noqa: BLE001
            return dict(empty, note="library.db unopenable")
    try:
        res = fold_comic_pairs(conn, inv)
        res.update(supersede_absent_comics(conn, inv))
        if own:
            conn.commit()
        return res
    except Exception:  # noqa: BLE001
        if own:
            conn.rollback()
        return dict(empty, note="reconcile failed")
    finally:
        if own:
            conn.close()
