"""playlist.py -- build curated Jellyfin playlists from backed-up manifests.

The problem this solves
-----------------------
Some shows are too long/uneven to watch straight through (Gintama, Naruto,
Bleach, the Dragon Balls, ...). The fix is a *curated* ordered playlist -- only
the episodes worth watching, in watch order -- that shows up pinnable in Infuse.

Manifest-as-truth
-----------------
A Jellyfin playlist by itself is a bad place to keep that curation: it lives in
Jellyfin's own data dir (``~/Library/Application Support/jellyfin/data/playlists``),
NOT on the SSD library root, so neither Media-Syncer nor this repo's
metadata backup touches it; it references volatile internal item ids; and there
is a live Jellyfin bug where a library scan silently deletes UI/XML playlists.
Build hours of curation into Jellyfin alone and one bad scan or data-dir wipe
erases it.

So the durable source of truth is a **manifest file** under ``state/playlists/``
(``config.PLAYLISTS_DIR``). ``scripts/backup_metadata.py`` already mirrors the
whole ``state/`` tree to MEGA -- the same path that protects the irreplaceable
locked ``.nfo`` -- so a manifest is backed up exactly like the rest of the
metadata this pipeline creates. Jellyfin's playlist is then just a *rebuildable
projection*: run the build again and every curated order comes back, keyed to
stable library paths rather than Jellyfin's internal ids.

This mirrors the whole repo's ethos (see README, "Identification"): a durable
file is the truth; the serving layer (Jellyfin) is reconstructible from it.

Manifest schema (state/playlists/<slug>.json)
----------------------------------------------
    {
      "name": "Gintama - Watchable",          # the playlist name shown in Infuse
      "description": "one-line note",          # optional
      "show": "Shows/Gintama (2006)",          # library-relative show folder
      "items": [                                # ORDERED watch list
        {"ep": 18},                             # Season 01 episode 18
        {"ep": [43, 44]},                       # inclusive range 43..44
        {"ep": 90, "season": 2},                # episode 90 of Season 02 (default season = 1)
        {"special": 1},                         # Season 00 special 1
        {"special": [1, 2]},                    # inclusive range of specials
        {"movie": "Gintama - The Movie (2010)"},# a film in Movies/ by filename stem
        {"path": "Shows/X/Season 01/Y.mkv"}     # escape hatch: raw library-relative path
      ]
    }

A merged multi-episode file (``S01E01-E02``) satisfies every episode number in
its span; if two tokens resolve to the same file it is included once, keeping
watch order.

The model proposes; the harness disposes. Curation is decided up front and written
into the manifest; this module only resolves + pushes it deterministically, and
never touches a library file.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402

# Resolve/ID-map against the mount (where the whole library appears -- drive + pool),
# not the local store (files may live on a drive or be pool-only).
LIBRARY_ROOT = getattr(config, "MEDIAFS_MOUNT", None) or config.MEDIA_ROOT
# Films live FLAT under here. Always derived from LIBRARY_ROOT (the mount), never from
# config.MOVIES_ROOT (the lower) -- see _movie_index for why that distinction is
# load-bearing rather than cosmetic.
MOVIES_DIR = LIBRARY_ROOT / "Movies"

try:
    import requests
except Exception:                                                     # noqa: BLE001
    requests = None  # surfaced with a clear message when a build is attempted


# --- filename parsing --------------------------------------------------------

# Matches "... - S01E18.mkv", "S01E01-E02", "S00E01-The Semi-Final Part 1", etc.
# Only the leading S<season>E<episode>[-E<episode>] is significant; whatever
# follows (a title, a dash, an extension) is ignored.
_EP_RE = re.compile(r"[Ss](\d{1,4})[Ee](\d{1,4})(?:-[Ee](\d{1,4}))?")

VIDEO_EXTS = config.VIDEO_EXTENSIONS


def _log(msg: str) -> None:
    print(f"[playlist] {msg}", flush=True)


def _parse_span(name: str) -> tuple[int, int, int] | None:
    """Return (season, first_ep, last_ep) parsed from a filename, or None."""
    m = _EP_RE.search(name)
    if not m:
        return None
    season = int(m.group(1))
    first = int(m.group(2))
    last = int(m.group(3)) if m.group(3) else first
    return season, first, last


def _season_index(show_dir: Path, season: int) -> dict[int, Path]:
    """Map every episode number in ``show_dir/Season NN`` to its file.

    A merged file (S01E01-E02) maps each covered number to the same Path.
    """
    season_dir = show_dir / f"Season {season:02d}"
    index: dict[int, Path] = {}
    if not season_dir.is_dir():
        return index
    for f in sorted(season_dir.iterdir()):
        if f.suffix.lower() not in VIDEO_EXTS:
            continue
        span = _parse_span(f.name)
        if span is None:
            continue
        _, first, last = span
        for n in range(first, last + 1):
            index.setdefault(n, f)
    return index


def _movie_index() -> dict[str, Path]:
    """Map each film's filename stem (case-insensitive) to its video file.

    Reads the mediafs MOUNT (``MOVIES_DIR``), not the SSD lower. This matters: in the
    virtual-library model a film's payload is EVICTED from the lower once it is safely
    in the MEGA pool, leaving only its sidecars (`.nfo`, posters) on disk — so scanning
    the lower finds a handful of films out of hundreds. Every film the library holds is
    visible at the mount whether or not its bytes are local, which is exactly the set a
    playlist should be able to reference. (Scanning the lower silently made every
    ``{"movie": ...}`` token unresolvable, which — because a manifest is promoted only
    when it resolves CLEANLY — quietly blocked any playlist that slots a film in.)
    """
    index: dict[str, Path] = {}
    if not MOVIES_DIR.is_dir():
        return index
    for f in MOVIES_DIR.iterdir():
        if f.suffix.lower() in VIDEO_EXTS:
            index[f.stem.lower()] = f
    return index


# --- manifest resolution -----------------------------------------------------

def _as_range(value) -> list[int]:
    """Accept 18 or [43, 44] and return the inclusive list of ints."""
    if isinstance(value, int):
        return [value]
    if isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) for v in value):
        lo, hi = value
        return list(range(lo, hi + 1))
    raise ValueError(f"expected an int or a [lo, hi] pair, got {value!r}")


def resolve_items(manifest: dict) -> tuple[list[Path], list[str]]:
    """Resolve a manifest's ordered tokens to absolute file paths.

    Returns (paths_in_order, problems). ``paths`` is de-duplicated while
    preserving first-seen order (so a merged episode file appears once).
    ``problems`` lists every token that could not be resolved -- the caller
    fails closed on a non-empty list rather than shipping a partial playlist.
    """
    default_show = manifest.get("show") or (manifest.get("shows") or [""])[0]
    movie_index = _movie_index()
    # Season index cached per (show_rel, season) so one manifest can draw episodes
    # from several show folders (a combined franchise playlist, e.g. Dragon Ball)
    # via a per-item "show" override.
    season_cache: dict[tuple[str, int], dict[int, Path]] = {}

    def season_index(show_rel: str, season: int) -> dict[int, Path]:
        key = (show_rel, season)
        if key not in season_cache:
            sd = LIBRARY_ROOT / show_rel if show_rel else None
            season_cache[key] = _season_index(sd, season) if sd else {}
        return season_cache[key]

    ordered: list[Path] = []
    seen: set[Path] = set()
    problems: list[str] = []

    def add(p: Path) -> None:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            ordered.append(p)

    for tok in manifest.get("items", []):
        try:
            if "ep" in tok:
                show_rel = tok.get("show", default_show)
                season = int(tok.get("season", 1))
                idx = season_index(show_rel, season)
                for n in _as_range(tok["ep"]):
                    f = idx.get(n)
                    if f is None:
                        problems.append(f"{show_rel} Season {season:02d} episode {n}: no file")
                    else:
                        add(f)
            elif "special" in tok:
                show_rel = tok.get("show", default_show)
                idx = season_index(show_rel, 0)
                for n in _as_range(tok["special"]):
                    f = idx.get(n)
                    if f is None:
                        problems.append(f"{show_rel} Season 00 special {n}: no file")
                    else:
                        add(f)
            elif "movie" in tok:
                f = movie_index.get(str(tok["movie"]).lower())
                if f is None:
                    problems.append(f"movie {tok['movie']!r}: no file in {MOVIES_DIR}")
                else:
                    add(f)
            elif "path" in tok:
                f = LIBRARY_ROOT / tok["path"]
                if not f.exists():
                    problems.append(f"path {tok['path']!r}: does not exist")
                else:
                    add(f)
            else:
                problems.append(f"unrecognized item token: {tok!r}")
        except Exception as exc:                                      # noqa: BLE001
            problems.append(f"{tok!r}: {exc}")

    return ordered, problems


# --- manifest mutation (used by the auto-updater, playlist_watch.py) ---------

def manifest_path_for(slug: str) -> Path:
    return config.PLAYLISTS_DIR / f"{slug}.json"


def load_or_init_manifest(slug: str, show_rel: str, name: str) -> dict:
    """Return the manifest for ``slug``, creating a minimal one if absent."""
    p = manifest_path_for(slug)
    if p.exists():
        return json.loads(p.read_text())
    return {"name": name, "show": show_rel, "items": []}


def manifest_has_path(manifest: dict, rel_path: str) -> bool:
    """True if a raw-path item for ``rel_path`` is already in the manifest."""
    return any(it.get("path") == rel_path for it in manifest.get("items", []))


def manifest_has_movie(manifest: dict, stem: str) -> bool:
    """True if a ``{"movie": ...}`` item for this film stem is already present
    (case-insensitively, matching how ``_movie_index`` resolves it)."""
    low = stem.lower()
    return any(str(it.get("movie", "")).lower() == low for it in manifest.get("items", []))


def insert_movie_item(slug: str, name: str, stem: str, after_stem: str = "",
                      note: str = "") -> bool:
    """Add a ``{"movie": <stem>}`` item to a manifest at the right WATCH POSITION,
    idempotently, and persist.

    This is the movie analogue of ``append_path_item``, and the position is the whole
    point: a curated film collection is an ORDERED watch list, so a newly-acquired
    entry usually belongs *in the middle* (a prequel, a mid-saga instalment), not at
    the end. ``after_stem`` names the film it should follow; when that film isn't in
    the manifest (or isn't given) the item is appended.

    Returns True if the item was added, False if it was already present.
    """
    manifest = load_or_init_manifest(slug, "", name)
    if manifest_has_movie(manifest, stem):
        return False
    item = {"movie": stem}
    if note:
        item["_note"] = note
    items = manifest.setdefault("items", [])
    pos = None
    if after_stem:
        low = after_stem.lower()
        for i, it in enumerate(items):
            if str(it.get("movie", "")).lower() == low:
                pos = i + 1
                break
    if pos is None:
        items.append(item)
    else:
        items.insert(pos, item)
    p = manifest_path_for(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return True


def append_path_item(slug: str, show_rel: str, name: str,
                     rel_path: str, note: str = "") -> bool:
    """Append a ``{"path": ...}`` item to a manifest, idempotently, and persist.

    Returns True if the item was added, False if it was already present. This is
    how the auto-updater records a keep decision durably (the manifest rides the
    state/ MEGA backup); the Jellyfin playlist is rebuilt from it separately.
    """
    manifest = load_or_init_manifest(slug, show_rel, name)
    if manifest_has_path(manifest, rel_path):
        return False
    item = {"path": rel_path}
    if note:
        item["_note"] = note
    manifest.setdefault("items", []).append(item)
    p = manifest_path_for(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return True


# --- Jellyfin client ---------------------------------------------------------

class Jellyfin:
    """Thin Jellyfin API wrapper, api_key-authenticated (same creds the ingest
    daemon and repair step already use: config.JELLYFIN_URL/API_KEY)."""

    def __init__(self, url: str = "", api_key: str = "", timeout: int = 30):
        self.base = (url or config.JELLYFIN_URL).rstrip("/")
        self.key = api_key or config.JELLYFIN_API_KEY
        self.timeout = timeout
        if requests is None:
            raise RuntimeError("the 'requests' package is required for Jellyfin calls")
        if not self.base:
            raise RuntimeError("JELLYFIN_URL is empty; set it in the environment/plist")
        self.s = requests.Session()
        self.s.headers.update({"X-Emby-Token": self.key})

    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def get(self, path: str, **params):
        r = self.s.get(self._url(path), params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else None

    def post(self, path: str, json_body=None, **params):
        r = self.s.post(self._url(path), params=params, json=json_body, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else None

    def delete(self, path: str, **params):
        r = self.s.delete(self._url(path), params=params, timeout=self.timeout)
        r.raise_for_status()
        return True

    # -- higher-level helpers --

    def user_id(self) -> str:
        """The playlist owner. config.JELLYFIN_USER_ID if set, else the first
        user (a single-user home server has exactly one)."""
        configured = getattr(config, "JELLYFIN_USER_ID", "")
        if configured:
            return configured
        users = self.get("/Users") or []
        if not users:
            raise RuntimeError("Jellyfin reports no users")
        return users[0]["Id"]

    def _items(self, **params) -> list[dict]:
        data = self.get("/Items", Recursive="true", **params) or {}
        return data.get("Items", [])

    def series_id(self, show_dir: Path) -> str | None:
        """Resolve a series item by matching its on-disk folder Path exactly."""
        target = str(show_dir.resolve())
        for it in self._items(IncludeItemTypes="Series", Fields="Path"):
            if it.get("Path") and str(Path(it["Path"]).resolve()) == target:
                return it["Id"]
        return None

    def build_id_map(self, show_dirs: list[Path]) -> dict[str, str]:
        """{absolute file path -> Jellyfin item id} for every episode of each
        listed series (all seasons/specials) plus every movie in the library.
        Accepts several series so a combined franchise playlist (e.g. Dragon
        Ball spanning five show folders) resolves in one pass. Matched by exact
        Path."""
        mapping: dict[str, str] = {}
        uid = self.user_id()
        for show_dir in show_dirs:
            sid = self.series_id(show_dir)
            if not sid:
                continue
            data = self.get(f"/Shows/{sid}/Episodes", userId=uid, Fields="Path") or {}
            for it in data.get("Items", []):
                if it.get("Path"):
                    mapping[str(Path(it["Path"]).resolve())] = it["Id"]
        for it in self._items(IncludeItemTypes="Movie", Fields="Path"):
            if it.get("Path"):
                mapping[str(Path(it["Path"]).resolve())] = it["Id"]
        return mapping

    def find_playlist(self, uid: str, name: str) -> str | None:
        data = self.get(f"/Users/{uid}/Items",
                        IncludeItemTypes="Playlist", Recursive="true") or {}
        for it in data.get("Items", []):
            if it.get("Name") == name:
                return it["Id"]
        return None

    def playlist_item_ids(self, playlist_id: str, uid: str) -> list[str]:
        data = self.get(f"/Playlists/{playlist_id}/Items", userId=uid) or {}
        return [it["Id"] for it in data.get("Items", [])]

    def create_playlist(self, name: str, ids: list[str], uid: str) -> str:
        resp = self.post("/Playlists",
                         json_body={"Name": name, "Ids": ids,
                                    "UserId": uid, "MediaType": "Video"})
        return resp["Id"] if resp else ""

    def delete_item(self, item_id: str) -> None:
        self.delete(f"/Items/{item_id}")


# --- build -------------------------------------------------------------------

def build_playlist(manifest_path: Path, jf: Jellyfin | None, dry_run: bool) -> bool:
    """Resolve one manifest and create/replace its Jellyfin playlist.

    Idempotent: if the playlist already exists with the exact same ordered item
    set it is left untouched; otherwise it is deleted and recreated so the
    manifest order wins (the manifest is the source of truth). Fails closed if
    any manifest token can't be resolved to a file -- never ships a partial
    playlist.
    """
    manifest = json.loads(manifest_path.read_text())
    name = manifest.get("name") or manifest_path.stem
    _log(f"'{name}' <- {manifest_path.name}")

    if not manifest.get("items"):
        _log("  manifest has no items yet; nothing to build (skipped)")
        return True

    paths, problems = resolve_items(manifest)
    if problems:
        _log(f"  FAILED: {len(problems)} unresolved item(s):")
        for p in problems[:20]:
            _log(f"    - {p}")
        if len(problems) > 20:
            _log(f"    ... and {len(problems) - 20} more")
        return False
    _log(f"  resolved {len(paths)} file(s) in order")

    if dry_run or jf is None:
        for i, p in enumerate(paths, 1):
            _log(f"    {i:>3}. {p.relative_to(LIBRARY_ROOT)}")
        _log("  DRY-RUN: nothing pushed to Jellyfin")
        return True

    # Which series' episodes must be resolved to ids: every distinct "Shows/<X>"
    # folder that any resolved path lives under (covers multi-show playlists).
    series_dirs: list[Path] = []
    seen_dirs: set[str] = set()
    for p in paths:
        try:
            rel = p.resolve().relative_to(LIBRARY_ROOT.resolve())
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) >= 2 and parts[0] == "Shows":
            key = f"Shows/{parts[1]}"
            if key not in seen_dirs:
                seen_dirs.add(key)
                series_dirs.append(LIBRARY_ROOT / "Shows" / parts[1])
    id_map = jf.build_id_map(series_dirs)
    ids: list[str] = []
    missing: list[Path] = []
    for p in paths:
        item_id = id_map.get(str(p.resolve()))
        if item_id:
            ids.append(item_id)
        else:
            missing.append(p)
    if missing:
        _log(f"  FAILED: {len(missing)} file(s) not yet indexed by Jellyfin "
             f"(let the library scan finish, then re-run):")
        for p in missing[:20]:
            _log(f"    - {p.relative_to(LIBRARY_ROOT)}")
        return False

    uid = jf.user_id()
    existing = jf.find_playlist(uid, name)
    if existing:
        if jf.playlist_item_ids(existing, uid) == ids:
            _log(f"  up to date ({len(ids)} items); left untouched")
            return True
        _log(f"  replacing existing playlist (item set changed)")
        jf.delete_item(existing)

    new_id = jf.create_playlist(name, ids, uid)
    _log(f"  created playlist '{name}' with {len(ids)} items (id {new_id})")
    return True


def _discover(paths: Iterable[str]) -> list[Path]:
    if paths:
        return [Path(p) for p in paths]
    d = config.PLAYLISTS_DIR
    return sorted(d.glob("*.json")) if d.is_dir() else []


def main() -> int:
    ap = argparse.ArgumentParser(description="Build curated Jellyfin playlists from manifests.")
    ap.add_argument("manifests", nargs="*",
                    help="manifest .json paths (default: all in state/playlists/)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and print the order; touch Jellyfin not at all")
    args = ap.parse_args()

    manifests = _discover(args.manifests)
    if not manifests:
        _log(f"no manifests found in {config.PLAYLISTS_DIR}")
        return 0

    jf = None
    if not args.dry_run:
        try:
            jf = Jellyfin()
        except Exception as exc:                                      # noqa: BLE001
            _log(f"Jellyfin unavailable ({exc}); nothing built. "
                 f"Use --dry-run to validate manifests offline.")
            return 1

    ok = True
    for mp in manifests:
        try:
            ok &= build_playlist(mp, jf, args.dry_run)
        except Exception as exc:                                      # noqa: BLE001
            _log(f"{mp.name}: ERROR {exc}")
            ok = False
    _log("done" if ok else "done WITH ERRORS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
