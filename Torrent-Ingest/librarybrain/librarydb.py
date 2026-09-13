"""The media library database -- the single source of truth for what we own and track.

A tiny, self-contained SQLite store (stdlib `sqlite3`, no deps) shared by the whole
fleet. It answers the two questions every other daemon asks:

  * "what SERIES do we own / track?"         -> the `series` table
  * "which episodes/chapters/volumes/issues/movies do we already have, and how good
    are they?"                                -> the `media` table
  * "what torrents are in the pipeline, for which series, and at what quality?" -> `torrents`

Everything is SERIES-CENTRIC. A torrent is only ever dropped for a series that is already
in `series` (owned, or explicitly added from new.txt / the curated watchlist). When a
torrent is filed, its files are inserted into `media` with a `series_id`. When a better
copy supersedes an older one (a volume replacing its chapters, SD -> HD, single -> dual
audio), the older `media` rows are marked `superseded` and the worse queued torrent is
purged -- never re-downloaded, never kept.

The DB lives at `LIBRARY_DB` (see `path()`); both Torrent-Searcher and Torrent-Ingest open
the same file. It is rebuilt/updated from the library scan and from each filed torrent, so
it is runtime state (gitignored), not a hand-edited artifact.

Self-contained normalization keeps the `series.norm` key identical across repos without a
cross-repo import.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path

# --- location ----------------------------------------------------------------

_DB_PATH = "/Users/mikeyferguson/Developer/Media-Fleet/Torrent-Ingest/state/library.db"


def path() -> Path:
    return Path(_DB_PATH)


# --- schema ------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,               -- canonical display name
    norm        TEXT NOT NULL,               -- normalized key (cross-repo stable)
    kind        TEXT NOT NULL,               -- anime|tv|movie|manga|comic|lightnovel
    source      TEXT NOT NULL DEFAULT 'library',  -- library|new.txt|watchlist|discovery
    created_at  TEXT NOT NULL,
    UNIQUE(norm, kind)
);

CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY,
    series_id   INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    mtype       TEXT NOT NULL,               -- episode|chapter|movie|volume|issue|collection|omnibus
    season      INTEGER,
    number      INTEGER,
    title       TEXT,
    resolution  INTEGER NOT NULL DEFAULT 0,  -- 5=UHD .. 0=unknown
    dual_audio  INTEGER NOT NULL DEFAULT 0,  -- 0/1
    colored     INTEGER NOT NULL DEFAULT 0,  -- 0/1 (manga/comic)
    status      TEXT NOT NULL DEFAULT 'owned',  -- owned|superseded
    info_hash   TEXT,
    path        TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_series ON media(series_id);
CREATE INDEX IF NOT EXISTS idx_media_num ON media(series_id, season, number);

CREATE TABLE IF NOT EXISTS torrents (
    id           INTEGER PRIMARY KEY,
    info_hash    TEXT UNIQUE,
    series_id    INTEGER REFERENCES series(id) ON DELETE SET NULL,
    title        TEXT,
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued|ingesting|finished|failed|purged
    content_key  TEXT,                       -- dedup key (episode range / vol:N / ...)
    resolution   INTEGER NOT NULL DEFAULT 0,
    dual_audio   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_torrents_series ON torrents(series_id);

CREATE TABLE IF NOT EXISTS series_alias (
    id          INTEGER PRIMARY KEY,
    series_id   INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,               -- display form of the alias
    norm        TEXT NOT NULL,               -- normalized alias (cross-repo stable)
    UNIQUE(series_id, norm)
);
CREATE INDEX IF NOT EXISTS idx_series_alias_norm ON series_alias(norm);

CREATE TABLE IF NOT EXISTS torrent_plan (
    info_hash   TEXT PRIMARY KEY,            -- the torrent's identity
    series_id   INTEGER,                     -- the series the plan resolves to
    plan        TEXT NOT NULL,               -- JSON: per-file torrent->library-item map
    created_at  TEXT NOT NULL
);
"""


def _normalize(title: str) -> str:
    """A loose, cross-repo-stable key: lowercase, drop a leading [Group]/year, fold
    accents to ASCII, collapse non-alphanumerics to single spaces."""
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii")
    t = t.lower()
    t = re.sub(r"^\s*[\[\(][^\]\)]{1,40}[\]\)]\s*", " ", t)
    t = re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def now() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(db_path=None) -> sqlite3.Connection:
    """Open the library DB, or a specific file when one is given.

    `db_path` exists so a caller can run the whole searcher against a COPY -- the harness
    QUARANTINE.md §R3 prescribes for a safe sweep, and what record-only mode now does on
    every run so that a rehearsal cannot write the real ledger.
    """
    p = Path(db_path) if db_path is not None else path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _rowdict(r) -> dict:
    return {k: r[k] for k in r.keys()}


# --- series ------------------------------------------------------------------

def add_series(conn, name: str, kind: str, source: str = "library") -> int:
    """Insert a series if absent; return its id. Idempotent on `(norm, kind)` — the same
    title may exist as both an anime and its manga (One Piece), as distinct series."""
    norm = _normalize(name)
    if not norm:
        raise ValueError(f"empty normalized name for {name!r}")
    kind = (kind or "anime").lower()
    row = conn.execute("SELECT id FROM series WHERE norm = ? AND kind = ?",
                       (norm, kind)).fetchone()
    if row:
        conn.execute(
            "UPDATE series SET name = ?, source = ? WHERE id = ?",
            (name, source, row[0]))
        return row[0]
    cur = conn.execute(
        "INSERT INTO series (name, norm, kind, source, created_at) VALUES (?,?,?,?,?)",
        (name, norm, kind, source, now()))
    return cur.lastrowid


def get_series_id(conn, name: str, kind: str | None = None) -> int | None:
    if kind:
        row = conn.execute("SELECT id FROM series WHERE norm = ? AND kind = ?",
                           (_normalize(name), kind.lower())).fetchone()
    else:
        row = conn.execute("SELECT id FROM series WHERE norm = ? ORDER BY id LIMIT 1",
                           (_normalize(name),)).fetchone()
    return row[0] if row else None


def resolve_series_id(conn, name: str, kind: str | None = None) -> int | None:
    """Resolve a name to a series row by canonical `norm` OR any registered alias.

    This is the canonical-name/alias resolution the acceptance gate depends on: "Attack on
    Titan" and "Shingeki no Kyojin" are one series, "3-gatsu no Lion" and "March Comes In
    Like a Lion" are one series, and so on. Matching is done on the normalized form of the
    name, first against the series's own `norm`, then against every alias's `norm`."""
    norm = _normalize(name)
    if not norm:
        return None
    if kind:
        kinds = (kind.lower(),)
    else:
        kinds = None
    row = conn.execute("SELECT id FROM series WHERE norm = ?" + (" AND kind = ?" if kind else "")
                       + " ORDER BY id LIMIT 1",
                       (norm,) if not kind else (norm, kinds[0])).fetchone()
    if row:
        return row[0]
    q = ("SELECT a.series_id FROM series_alias a JOIN series s ON s.id = a.series_id "
         "WHERE a.norm = ?")
    args: list = [norm]
    if kind:
        q += " AND s.kind = ?"
        args.append(kinds[0])
    q += " ORDER BY a.series_id LIMIT 1"
    row = conn.execute(q, args).fetchone()
    return row[0] if row else None


def add_alias(conn, series_id: int, alias: str) -> None:
    """Register an alias for a series (idempotent on `(series_id, norm)`)."""
    norm = _normalize(alias)
    if not norm or norm == _normalize(_series_norm(conn, series_id) or ""):
        return
    conn.execute(
        "INSERT OR IGNORE INTO series_alias (series_id, alias, norm) VALUES (?,?,?)",
        (series_id, (alias or "").strip(), norm))


def _series_norm(conn, series_id: int) -> str | None:
    row = conn.execute("SELECT norm FROM series WHERE id = ?", (series_id,)).fetchone()
    return row[0] if row else None


def aliases_for(conn, series_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT alias FROM series_alias WHERE series_id = ? ORDER BY alias", (series_id,))]


# Season/franchise-suffix markers that make "X S2" a FRAGMENT of "X", not a separate
# series. Stripped off a name to find its franchise base. Deliberately NARROW: "Fate/Zero",
# "Dragon Ball GT", "Fate/stay night: Unlimited Blade Works" have NO season suffix and must
# never be folded into "Fate/stay night" / "Dragon Ball" — those are distinct series the
# library owns. Only "X S2" / "X: Final Season" / "X Season N" shapes fold.
_SEASON_SUFFIX_RE = re.compile(
    r"[:\-]?\s*(?:"
    r"season\s*\d+|\bs\d{1,2}\b|\bpart\s*\d+\b|"
    r"(?:1st|2nd|3rd|4th|5th|6th|7th|8th|9th|\d+th)\s+season|"
    r"final\s+season(?:\s+part\s*\d+)?|\b\d{1,2}(?:nd|rd|th)\s+season"
    r")\s*$",
    re.IGNORECASE,
)


def _franchise_base(name: str) -> str:
    return _SEASON_SUFFIX_RE.sub("", (name or "").strip()).strip().rstrip(":").strip()


def consolidate_series(conn) -> int:
    """Fold season-suffix fragment rows ("X S2", "X: Final Season") into their base "X".

    The wantlist used to split one series across several rows ("Attack on Titan", "Attack
    on Titan S2", "Attack on Titan: Final Season"). This pass merges a row whose name is
    `base + season suffix` into an EXISTING row named `base` of the SAME kind: media and
    torrents are re-pointed at the base and the fragment is deleted. Deterministic (a regex
    strip + exact normalized-name match), so it can never merge two genuinely different
    series -- "Dragon Ball GT" and "Dragon Ball" are distinct and both survive. Kind-scoped
    so an anime and its manga of the same name stay distinct. Returns rows merged."""
    merged = 0
    by_norm: dict[tuple[str, str], int] = {}
    for r in conn.execute("SELECT id, norm, kind FROM series"):
        by_norm.setdefault((r[1], r[2]), r[0])

    rows = conn.execute("SELECT id, name, norm, kind FROM series ORDER BY id").fetchall()
    for row in rows:
        sid, name, norm, kind = row
        base = _franchise_base(name)
        base_norm = _normalize(base)
        if not base_norm or base_norm == norm:
            continue
        target = by_norm.get((base_norm, kind))
        if target is None or target == sid:
            continue
        alive = conn.execute("SELECT 1 FROM series WHERE id = ?", (target,)).fetchone()
        if alive is None:
            continue
        # `name` is base + season suffix -> a fragment of the canonical `base` row.
        conn.execute("UPDATE media SET series_id = ? WHERE series_id = ?", (target, sid))
        conn.execute("UPDATE torrents SET series_id = ? WHERE series_id = ?", (target, sid))
        for (alias, anorm) in conn.execute(
                "SELECT alias, norm FROM series_alias WHERE series_id = ?", (sid,)).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO series_alias (series_id, alias, norm) VALUES (?,?,?)",
                (target, alias, anorm))
        conn.execute("DELETE FROM series_alias WHERE series_id = ?", (sid,))
        conn.execute("DELETE FROM series WHERE id = ?", (sid,))
        merged += 1
    conn.execute("DELETE FROM series_alias WHERE id NOT IN "
                 "(SELECT MIN(id) FROM series_alias GROUP BY series_id, norm)")
    return merged


def consolidate_kinds(conn) -> int:
    """Fold a library-seeded `anime` show row into a new.txt-seeded `tv`/`movie` row of
    the SAME normalized name.

    `seed_from_inventory` files every library show as kind `anime` (a blanket default from
    the fleet's anime-first past), so a western show the owner typed into new.txt as `tv`
    exists as BOTH an `anime` row (searched only on anime trackers, and whose `(YYYY)`
    name pollutes the relevance gate) and a `tv` row. The want's kind is the owner's
    explicit classification, so it wins: media, torrents, torrent plans and aliases are
    re-pointed at the want's row and the redundant `anime` row is deleted. Only a
    library-source `anime` row with a same-norm new.txt `tv`/`movie` sibling is merged --
    never two genuinely different series. Returns rows merged.
    """
    merged = 0
    want_rows: dict[str, int] = {}
    for r in conn.execute(
            "SELECT id, norm, kind FROM series "
            "WHERE source = 'new.txt' AND kind IN ('tv','movie') ORDER BY id"):
        want_rows.setdefault(r["norm"], r["id"])

    for r in conn.execute(
            "SELECT id, norm, kind FROM series "
            "WHERE source = 'library' AND kind = 'anime' ORDER BY id"):
        target = want_rows.get(r["norm"])
        if target is None or target == r["id"]:
            continue
        if conn.execute("SELECT 1 FROM series WHERE id = ?", (target,)).fetchone() is None:
            continue
        conn.execute("UPDATE media SET series_id = ? WHERE series_id = ?", (target, r["id"]))
        conn.execute("UPDATE torrents SET series_id = ? WHERE series_id = ?", (target, r["id"]))
        conn.execute("UPDATE torrent_plan SET series_id = ? WHERE series_id = ?", (target, r["id"]))
        for (alias, anorm) in conn.execute(
                "SELECT alias, norm FROM series_alias WHERE series_id = ?", (r["id"],)).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO series_alias (series_id, alias, norm) VALUES (?,?,?)",
                (target, alias, anorm))
        conn.execute("DELETE FROM series_alias WHERE series_id = ?", (r["id"],))
        conn.execute("DELETE FROM series WHERE id = ?", (r["id"],))
        merged += 1
    conn.execute("DELETE FROM series_alias WHERE id NOT IN "
                 "(SELECT MIN(id) FROM series_alias GROUP BY series_id, norm)")
    return merged


def series_exists(conn, name: str) -> bool:
    return resolve_series_id(conn, name) is not None


def all_series(conn) -> list[dict]:
    return [_rowdict(r) for r in conn.execute(
        "SELECT id, name, norm, kind, source FROM series ORDER BY name")]


def series_kinds(conn) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT id, kind FROM series")}


# A franchise's VIDEO rows describe one another's neighbours — a series, its films and its
# spin-offs are all "the show" for ownership purposes, which is why `The Croods` (a movie
# row) must be able to reach `The Croods: Family Tree` (a tv row). Its BOOK rows do not: a
# manga's volumes and an anime's episodes are different works that merely share a name, and
# a release of one is never evidence about the other.
_VIDEO_KINDS = ("anime", "tv", "movie")
_BOOK_KINDS = ("manga", "comic", "lightnovel")


def _kind_family(kind: str) -> tuple:
    return _BOOK_KINDS if (kind or "").lower() in _BOOK_KINDS else _VIDEO_KINDS


def franchise_members(conn, series_id: int) -> list[dict]:
    """Populated series rows whose canonical name EXTENDS this row's name, same kind family.

    A want-list entry may name a franchise (`Digimon`) while every episode the library
    actually holds is filed under a longer, more specific name (`Digimon Ghost Game`,
    `Digimon Tamers`, ...). Such an umbrella row owns nothing itself, and that is not the
    same fact as "we own nothing of this" — see `series_id_for_release`.

    Matching is on the normalized name with an explicit word boundary, so `Digimon` finds
    `Digimon Tamers` but `86` never matches `86 Eighty-Six`'s unrelated neighbours by digit
    prefix, and `Fate/Zero` is not a member of `Fate` unless a `Fate` row exists to ask.

    Members are confined to the asking row's kind family, so a manga release can never be
    judged against an anime row's episodes. Nothing crosses that line in the data today; the
    restriction is here so that staying true does not depend on the data staying this shape.
    """
    row = conn.execute("SELECT norm, kind FROM series WHERE id = ?", (series_id,)).fetchone()
    if not row or not row[0]:
        return []
    fam = _kind_family(row["kind"])
    like = (row["norm"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")) + " %"
    return [_rowdict(r) for r in conn.execute(
        "SELECT s.id, s.name, s.norm, s.kind, COUNT(m.id) AS items "
        "FROM series s JOIN media m ON m.series_id = s.id "
        f"WHERE s.id != ? AND s.norm LIKE ? ESCAPE '\\' AND s.kind IN ({','.join('?' * len(fam))}) "
        "GROUP BY s.id",
        (series_id, like, *fam))]


def series_id_for_release(conn, series_id: int, release_title: str) -> int:
    """The series row whose ownership actually governs this release.

    Normally the row the query was made against, unchanged. The exception this exists for:
    the query row is an umbrella that owns NOTHING while the library holds the content
    under more specific names, so `owned_items` answers "you own none of this" and every
    file in the release reads as NEW. That is how one `Digimon` want-list entry accepted 49
    drops of Ghost Game / Adventure 2020 / Beatbreak episodes the library already held.

    So when — and only when — the row owns nothing and has populated franchise members, the
    release's OWN TITLE picks the member: the longest member name the title actually names.
    The title is the evidence; the query row is only what made us look. No member named by
    the title means a genuinely new franchise entry, and the umbrella row (owning nothing,
    so everything NEW) is then the correct answer and is kept.

    Never widens ownership: it can only move the lookup to a row the release names outright,
    so a release cannot be judged against episodes belonging to a sibling series.
    """
    if not series_id or not release_title:
        return series_id
    if owned_media(conn, series_id):
        return series_id
    rn = _normalize(release_title)
    if not rn:
        return series_id
    best = None
    for m in franchise_members(conn, series_id):
        n = m["norm"]
        if rn == n or rn.startswith(n + " ") or f" {n} " in f" {rn} ":
            if best is None or len(n) > len(best["norm"]):
                best = m
    return best["id"] if best else series_id


# --- media -------------------------------------------------------------------

def add_media(conn, series_id: int, mtype: str, season=None, number=None, title=None,
              resolution=0, dual_audio=False, colored=False, info_hash=None,
              path=None, status="owned") -> int:
    cur = conn.execute(
        "INSERT INTO media (series_id, mtype, season, number, title, resolution, "
        "dual_audio, colored, status, info_hash, path, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (series_id, mtype, season, number, title, int(resolution or 0),
         int(bool(dual_audio)), int(bool(colored)), status, info_hash, path, now()))
    return cur.lastrowid


def upsert_media(conn, series_id: int, mtype: str, season=None, number=None, title=None,
                 resolution=0, dual_audio=False, colored=False, info_hash=None,
                 path=None, status="owned") -> int:
    """add_media, but idempotent on the item's identity.

    `add_media` is a bare INSERT. Nothing constrains (series_id, mtype, season, number), so
    re-filing an episode the library already holds appends ANOTHER owned row -- and the
    fleet re-files constantly (a re-cut replacing a file, a re-drop, a repaired wave).
    Measured on the live DB 2026-09-12 before this existed: 41,586 owned rows for 30,831
    distinct items, 25.9% redundant, with one episode of `That '70s Show` holding 59 rows.

    A row per FILING is not a useful ledger -- `owned_media` is read as "what do we have",
    and duplicates inflate every coverage answer built on it. So an existing row for the
    same item is UPDATED in place instead, which also lets a better copy (higher
    resolution, dual audio) overwrite the record of a worse one.

    `add_media` is left exactly as it was: it is the right primitive for a caller that
    genuinely wants a new row, and the acceptance-gate tests build fixtures with it.
    """
    ident = item_key(mtype, season, number)
    for row in conn.execute(
            "SELECT id, mtype, season, number, resolution FROM media WHERE series_id = ?",
            (series_id,)).fetchall():
        if item_key(row["mtype"], row["season"], row["number"]) != ident:
            continue
        conn.execute(
            "UPDATE media SET title = COALESCE(?, title), "
            "resolution = MAX(resolution, ?), dual_audio = MAX(dual_audio, ?), "
            "colored = MAX(colored, ?), info_hash = COALESCE(?, info_hash), "
            "path = COALESCE(?, path), status = ?, updated_at = ? WHERE id = ?",
            (title, int(resolution or 0), int(bool(dual_audio)), int(bool(colored)),
             info_hash, path, status, now(), row["id"]))
        return row["id"]
    return add_media(conn, series_id, mtype, season, number, title, resolution,
                     dual_audio, colored, info_hash, path, status)


def owned_media(conn, series_id: int) -> list[dict]:
    return [_rowdict(r) for r in conn.execute(
        "SELECT id, mtype, season, number, resolution, dual_audio, colored, status, title "
        "FROM media WHERE series_id = ? AND status != 'superseded'", (series_id,))]


# Extensions that mark a `media.title` as a FILENAME rather than an episode title.
_TITLE_FILE_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm", ".srt", ".ass",
                   ".sub", ".idx", ".nfo", ".jpg", ".png", ".cbz", ".cbr", ".cb7", ".pdf",
                   ".epub")
_TITLE_EPISODE_RE = re.compile(r"[Ss]\d{1,2}[Ee]\d{1,4}")


def looks_like_filename(title: str | None) -> bool:
    """Whether a `media.title` is really the file's NAME and not the episode's title.

    Ingest records what it placed, so an ingest-written row carries
    `The Devil Is a Part-Timer! (2013) - S02E21.mkv`. That is not a title, and treating it
    as one is not merely untidy -- `media.title` exists so a release's own season numbering
    can be reconciled to ours by TITLE MATCH (the JJK "S3E01 == our S1E48" case), and a
    filename matches no episode title anywhere. Worse, the merge that fills titles in from
    the `.nfo` sidecars is written `e["title"] or etitles.get(...)`, so a filename -- being
    truthy -- WINS over the real title and the sidecar is never consulted.

    Measured on the live DB: of 36,792 episode rows, 33,543 were blank and 2,522 held a
    filename. Essentially none carried a usable title.
    """
    t = (title or "").strip()
    if not t:
        return True
    if t.lower().endswith(_TITLE_FILE_EXT):
        return True
    return bool(_TITLE_EPISODE_RE.search(t))


def backfill_episode_titles(conn, series_id: int, titles: dict) -> int:
    """Persist `.nfo`-derived episode titles onto the series' media rows. Returns the count.

    `media.title` is what lets a release's own season numbering be reconciled to OURS by
    TITLE rather than by guesswork -- the JJK "S3E01 == our S1E48" case -- and 89% of the
    rows were blank, because ingest-recorded rows store a filename and nothing ever wrote
    the title back. The searcher already reads every sidecar for this on each sweep and
    throws the result away at the end of it, so the fix is to keep what was read rather than
    to run a migration: the column heals itself for whatever the sweep touched, and stays
    healed.

    Only rows carrying no REAL title are filled -- blank, or holding the filename ingest
    recorded (see `looks_like_filename`). A row that already carries a genuine title is
    never overwritten: a sidecar can be regenerated by Jellyfin from a WRONG provider match
    (that is the Gundam Build Divers fault in §5 item 3), and letting one of those replace a
    known-good title would spread the error into the mapper placement depends on.
    """
    if not titles:
        return 0
    rows = conn.execute(
        "SELECT id, season, number, title FROM media "
        "WHERE series_id = ? AND mtype = 'episode' AND status != 'superseded'",
        (series_id,)).fetchall()
    pairs = []
    for r in rows:
        if not looks_like_filename(r["title"]):
            continue                      # already a real title; never overwrite one
        t = titles.get((r["season"] or 1, r["number"]))
        if t and t != r["title"]:
            pairs.append((t, now(), r["id"]))
    if not pairs:
        return 0
    conn.executemany("UPDATE media SET title = ?, updated_at = ? WHERE id = ?", pairs)
    conn.commit()
    return len(pairs)


def library_episode_list(conn, series_id: int) -> list[dict]:
    """The series's own library episode list, as WE file them: [{season, number, title}].

    This is what the AI filename→item mapper (§ issues.txt 6) is handed so it can MATCH BY
    TITLE instead of recollecting season boundaries -- the absolute-numbering reconciliation
    (JJK "S3E01" == our S1E48) becomes a title lookup, not a guess. `title` is the episode
    title when the media row carries one (ingest-recorded rows store the filename; titles
    come from the library `.nfo` sidecars, attached by the searcher before mapping).
    """
    rows = conn.execute(
        "SELECT season, number, title FROM media "
        "WHERE series_id = ? AND mtype = 'episode' AND status != 'superseded' "
        "ORDER BY season, number", (series_id,)).fetchall()
    return [{"season": r["season"] or 1, "number": r["number"], "title": r["title"]}
            for r in rows if r["number"] is not None]


def coverage(conn, series_id: int) -> dict:
    """A compact per-series coverage index, shaped like the searcher expects:
    seasons -> {str(s): [episode...]}, max_season, volumes -> {n: colored}, chapters -> [n],
    movies -> best resolution tier."""
    cov = {"seasons": {}, "max_season": 0, "volumes": {}, "chapters": [],
           "movies_res": 0}
    for m in owned_media(conn, series_id):
        mt = m["mtype"]
        n = m["number"]
        if mt == "episode" and n is not None:
            s = m["season"] or 1
            cov["seasons"].setdefault(str(s), set()).add(n)
            cov["max_season"] = max(cov["max_season"], s)
        elif mt == "volume" and n is not None:
            cov["volumes"][n] = bool(cov["volumes"].get(n) or m["colored"])
        elif mt == "chapter" and n is not None:
            cov["chapters"].append(n)
        elif mt == "movie":
            cov["movies_res"] = max(cov["movies_res"], m["resolution"] or 0)
    cov["seasons"] = {s: sorted(lst) for s, lst in cov["seasons"].items()}
    cov["chapters"] = sorted(set(cov["chapters"]))
    return cov


def item_key(mtype: str, season, number) -> tuple:
    """The stable identity of one owned item: (mtype, season, number). Movies collapse to
    (movie, None, None); episodes key on (episode, season, number); volumes/chapters on
    their number. This is the granularity the acceptance rule (a)/(b) compares on."""
    if mtype == "episode":
        return ("episode", season or 1, number)
    if mtype in ("volume", "chapter"):
        return (mtype, None, number)
    if mtype == "movie":
        return ("movie", None, None)
    return (mtype, season, number)


def owned_items(conn, series_id: int) -> dict:
    """Map item_key -> the best non-superseded quality for each owned item, so the searcher
    can answer two questions without re-walking files: is this item NEW (key absent), and is
    it an UPGRADE (key present at strictly lower quality). Quality is a dict of
    {resolution, dual_audio, colored}."""
    out: dict = {}
    for m in owned_media(conn, series_id):
        key = item_key(m["mtype"], m["season"], m["number"])
        q = {"resolution": m["resolution"] or 0,
             "dual_audio": bool(m["dual_audio"]),
             "colored": bool(m["colored"])}
        cur = out.get(key)
        if cur is None:
            out[key] = q
            continue
        # Keep the Pareto-dominating row so "is this an upgrade" stays sound: a row that is
        # >= on every axis and > on at least one dominates the other.
        if all(q[a] >= cur[a] for a in q):
            out[key] = q
    return out


def mark_superseded(conn, series_id: int, mtype: str, season=None,
                    min_number: int | None = None, max_number: int | None = None) -> int:
    """Mark owned `mtype` rows superseded (a volume replacing its chapters, a higher
    definition replacing a lower one, ...). Returns rows affected."""
    q = "UPDATE media SET status = 'superseded' WHERE series_id = ? AND mtype = ?"
    args: list = [series_id, mtype]
    if season is not None:
        q += " AND season = ?"
        args.append(season)
    if min_number is not None:
        q += " AND number >= ?"
        args.append(min_number)
    if max_number is not None:
        q += " AND number <= ?"
        args.append(max_number)
    cur = conn.execute(q, args)
    return cur.rowcount


# --- torrents ----------------------------------------------------------------

def add_torrent(conn, info_hash, series_id, title, status="queued",
                content_key=None, resolution=0, dual_audio=False) -> int:
    # UPSERT: a torrent is keyed by info_hash, and the same hash legitimately re-enters the
    # ledger when the searcher re-drops it (re-acquire after a purge, or a fresh encode of
    # the same release re-discovered by a later sweep). A plain INSERT raised "UNIQUE
    # constraint failed: torrents.info_hash" on every such re-drop, which both spammed the
    # log and -- worse -- left the row stuck at its old status so the re-drop was never
    # recorded as queued. Update the mutable fields and keep the row.
    cur = conn.execute(
        "INSERT INTO torrents (info_hash, series_id, title, status, content_key, "
        "resolution, dual_audio, created_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(info_hash) DO UPDATE SET "
        "series_id=excluded.series_id, title=excluded.title, status=excluded.status, "
        "content_key=excluded.content_key, resolution=excluded.resolution, "
        "dual_audio=excluded.dual_audio",
        (info_hash, series_id, title, status, content_key,
         int(resolution or 0), int(bool(dual_audio)), now()))
    return cur.lastrowid


def torrent_by_hash(conn, info_hash) -> dict | None:
    """The ledger row for a torrent, by infohash, or None.

    This is how Torrent-Ingest learns which SERIES a queued magnet belongs to. The
    searcher records every drop here (`_record_torrent`) at the moment it knows the
    answer, so ingest never has to re-derive a series from a release title -- which it
    cannot do reliably, and guessing it is how a gate ends up refusing the wrong thing.
    Carries the drop-time `resolution`/`dual_audio` too, so the upgrade test has the same
    candidate quality the searcher saw."""
    if not info_hash:
        return None
    row = conn.execute(
        "SELECT info_hash, series_id, title, status, content_key, resolution, dual_audio "
        "FROM torrents WHERE info_hash = ?", (str(info_hash).lower(),)).fetchone()
    return _rowdict(row) if row else None


def series_by_id(conn, series_id) -> dict | None:
    """`{id, name, kind}` for a series row, or None."""
    if not series_id:
        return None
    row = conn.execute("SELECT id, name, kind FROM series WHERE id = ?",
                       (int(series_id),)).fetchone()
    return _rowdict(row) if row else None


def set_torrent_status(conn, info_hash, status: str) -> None:
    conn.execute("UPDATE torrents SET status = ? WHERE info_hash = ?",
                 (status, info_hash))


def torrents_for_series(conn, series_id: int, status=None) -> list[dict]:
    q = "SELECT info_hash, title, status, content_key, resolution, dual_audio FROM torrents WHERE series_id = ?"
    args: list = [series_id]
    if status:
        q += " AND status = ?"
        args.append(status)
    return [_rowdict(r) for r in conn.execute(q, args)]


def purge_torrent(conn, info_hash) -> None:
    conn.execute("UPDATE torrents SET status = 'purged' WHERE info_hash = ?", (info_hash,))


def save_torrent_plan(conn, info_hash, series_id, plan: dict) -> None:
    """Persist the SETTLED per-file torrent→library-item map against a torrent's infohash.

    This is § issues.txt 6.4: the moment a torrent is accepted, the AI's file→item mapping
    is recorded so a later ingest (or a re-download of the same infohash) reuses the stored
    plan instead of re-deriving it. The plan JSON is `{series, kind, files: [{src, ...item}]}`.
    """
    if not info_hash:
        return
    conn.execute(
        "INSERT INTO torrent_plan (info_hash, series_id, plan, created_at) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(info_hash) DO UPDATE SET "
        "series_id=excluded.series_id, plan=excluded.plan, created_at=excluded.created_at",
        (info_hash, series_id, json.dumps(plan, ensure_ascii=False), now()))


def load_torrent_plan(conn, info_hash) -> dict | None:
    """Return the stored per-infohash plan, or None. Read by Torrent-Ingest so a torrent
    that the searcher already mapped is never re-judged at identify time."""
    if not info_hash:
        return None
    row = conn.execute("SELECT plan FROM torrent_plan WHERE info_hash = ?",
                       (info_hash,)).fetchone()
    if not row:
        return None
    try:
        plan = json.loads(row["plan"])
    except (json.JSONDecodeError, TypeError):
        return None
    return plan if isinstance(plan, dict) else None


# --- seeding from a library scan ---------------------------------------------

def seed_from_inventory(conn, inv: dict) -> dict:
    """Populate `series`/`media` from a coverage index shaped like Torrent-Searcher's
    `inventory.load()` output ({shows, movies, comics, novels}). Idempotent: re-running
    merges into existing rows (an owned episode is never duplicated). Returns counts."""
    added_series = added_media = 0

    def series_id(name, kind, source="library"):
        nonlocal added_series
        sid = get_series_id(conn, name, kind)
        if sid is None:
            sid = add_series(conn, name, kind, source)
            added_series += 1
        return sid

    def have(series_id, mtype, season, number):
        q = ("SELECT 1 FROM media WHERE series_id=? AND mtype=? "
             "AND season IS ? AND number IS ? AND status != 'superseded' LIMIT 1")
        return conn.execute(q, (series_id, mtype, season, number)).fetchone() is not None

    for norm, cov in (inv.get("shows") or {}).items():
        sid = series_id(cov.get("name", norm), "anime")
        for s, eps in (cov.get("seasons") or {}).items():
            season = int(s)
            for ep in eps:
                if not have(sid, "episode", season, ep):
                    add_media(conn, sid, "episode", season, ep, title=None)
                    added_media += 1

    for norm, mov in (inv.get("movies") or {}).items():
        sid = series_id(mov.get("name", norm), "movie")
        if not have(sid, "movie", None, None):
            add_media(conn, sid, "movie", None, None, title=mov.get("name"),
                      resolution=mov.get("res", 0))
            added_media += 1

    for norm, comic in (inv.get("comics") or {}).items():
        # Manga and western comics are searched through DIFFERENT SOURCES: kind "comic"
        # reaches GetComics/LibGen/the Internet Archive, kind "manga" is torrent-only
        # (nyaa's Literature bucket). Seeding every comic as "manga" meant the western half
        # of the library -- Invincible, ElfQuest, Star Wars, Calvin and Hobbes -- was being
        # searched on an anime tracker and never expanded.
        sid = series_id(comic.get("name", norm), "comic" if comic.get("western") else "manga")
        for n, colored in (comic.get("volumes") or {}).items():
            n = int(n)
            if not have(sid, "volume", None, n):
                add_media(conn, sid, "volume", None, n, colored=bool(colored))
                added_media += 1
        for n in (comic.get("chapters") or []):
            n = int(n)
            if not have(sid, "chapter", None, n):
                add_media(conn, sid, "chapter", None, n)
                added_media += 1

    for norm, novel in (inv.get("novels") or {}).items():
        sid = series_id(novel.get("name", norm), "lightnovel")
        for n in (novel.get("volumes") or []):
            n = int(n)
            if not have(sid, "volume", None, n):
                add_media(conn, sid, "volume", None, n)
                added_media += 1

    conn.commit()
    return {"series": added_series, "media": added_media}


def reconcile_media(conn, inv: dict) -> dict:
    """Mirror the `media` table against the current inventory, BOTH directions.

    `seed_from_inventory` only ADDS rows, so a file that was evicted locally without ever
    landing on MEGA (the ~512 lost-torrent bug) leaves a stale `media` row that reads as
    "owned" forever -- the searcher then never re-acquires the content. This pass:

      * marks OWNED media whose item is absent from the current inventory `superseded`
        (it is gone from MEGA AND the local tree, so it is a stale lie), and
      * restores any SUPERSEDED media whose item IS present again (a re-acquired file).

    The inventory is the union of remote_inventory.json (authoritative) + the local scan, so
    "present in inventory" is exactly "the searcher must treat this as owned". Returns counts.
    """
    removed = restored = 0
    # Authoritative owned item sets, keyed by (normalized name, kind) -> {item_key}.
    owned: dict[tuple[str, str], set] = {}
    for norm, cov in (inv.get("shows") or {}).items():
        keys: set = set()
        for s, eps in (cov.get("seasons") or {}).items():
            for ep in eps:
                keys.add(item_key("episode", int(s), ep))
        owned[(norm, "anime")] = keys
    for norm, mov in (inv.get("movies") or {}).items():
        owned[(norm, "movie")] = {item_key("movie", None, None)}
    for norm, comic in (inv.get("comics") or {}).items():
        keys = {item_key("volume", None, int(n)) for n in (comic.get("volumes") or {})}
        keys |= {item_key("chapter", None, int(n)) for n in (comic.get("chapters") or [])}
        owned[(norm, "manga")] = keys
    for norm, novel in (inv.get("novels") or {}).items():
        owned[(norm, "lightnovel")] = {item_key("volume", None, int(n))
                                       for n in (novel.get("volumes") or [])}

    for row in conn.execute("SELECT id, norm, kind, source FROM series").fetchall():
        sid, norm, kind, source = row
        keys = owned.get((norm, kind))
        if keys is None:
            # The series's normalized name is not in the current inventory at all. If it was
            # seeded from the library scan itself, it has vanished from the pool entirely and
            # every owned media row is a stale lie -> supersede them all so the searcher can
            # re-acquire. (A brand-new new.txt/watchlist/ingest series has no library media
            # yet, so this is a no-op for it, and we never supersede media the ingest daemon
            # is in the middle of filing for an explicitly-requested series.)
            if source == "library":
                for m in owned_media(conn, sid):
                    conn.execute("UPDATE media SET status = 'superseded' WHERE id = ?",
                                 (m["id"],))
                    removed += 1
            continue
        for m in owned_media(conn, sid):
            k = item_key(m["mtype"], m["season"], m["number"])
            if k not in keys:
                conn.execute(
                    "UPDATE media SET status = 'superseded' WHERE id = ?", (m["id"],))
                removed += 1
        # Restore superseded rows whose item is present again.
        for m in conn.execute(
                "SELECT id, mtype, season, number FROM media WHERE series_id = ? "
                "AND status = 'superseded'", (sid,)).fetchall():
            if item_key(m["mtype"], m["season"], m["number"]) in keys:
                conn.execute("UPDATE media SET status = 'owned' WHERE id = ?", (m["id"],))
                restored += 1
    return {"superseded": removed, "restored": restored}
