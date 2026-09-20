"""What TMDB — the provider JELLYFIN actually scrapes — has for a show.

NARROW ON PURPOSE. This does NOT decide placement. `epguide` (TVMaze, key-less) still owns
the arc->season mapping, and nothing here may change it. This answers exactly one question:

    for a file we are about to file at S<season>E<episode>, does TMDB HAVE that slot?

Because that is the question that decides whether Jellyfin will show the owner a real title
or a blank. The fleet's guide and Jellyfin's are different providers and they disagree, and
the disagreement is small but real. Measured for `Monogatari Series` on 2026-09-12:

    season   we file (per TVMaze)          TMDB has                        result
    S01      Bakemonogatari (15)           Bakemonogatari (12)             E13-E15 BLANK
    S02      Nisemonogatari (11)           Nisemonogatari (11)             fine
    S03      Second Season (23)            Second Season (23)              fine
    S04      Owarimonogatari S1 (13)       Owarimonogatari (12)            E13 BLANK
    S05      Zoku Owarimonogatari (6)      OFF & MONSTER Season (15)       6 WRONG titles

Ten files of 103. The other 93 are served perfectly well by TMDB, and the owner's
instruction is explicit: **let TMDB work for every file it works for; own only what is
necessary.** So this module exists to find the ten, not to replace the ninety-three.

WHY A KEY IS ACCEPTABLE HERE. `epguide`'s key-less-ness is a real virtue and is unchanged.
The TMDB key is free, carries no balance and cannot be billed, so it sits inside the
free-only rule as the owner restated it on 2026-09-12 (`config.AI_PROVIDERS`, §2a of the
hand-off): the invariant is that no REQUEST IS BILLED, and a TMDB read is not.

Fails soft in every direction. No key, no id, a network error or an unknown show all return
None, and the caller then behaves exactly as it did before this module existed — which is
to say, it owns nothing extra and Jellyfin scrapes as it always has.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import config

_API = "https://api.themoviedb.org/3"
_CACHE_V = 2                         # bumped when the cached shape changes (count+name)
CACHE_TTL_SEC = 7 * 24 * 3600        # a season's episode count changes rarely
MISS_CACHE_TTL_SEC = 6 * 3600
_TIMEOUT = 20

_mem: dict = {}


def _key():
    """The TMDB v3 key: environment first, then the fleet's key directory. "" when absent."""
    k = (config.os.environ.get("TMDB_API_KEY", "") or "").strip()
    if k:
        return k
    try:
        return (config.API_KEYS_DIR / "tmdb_api_key").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cache_path(tmdb_id):
    d = config.STATE_DIR / "tmdbguide"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{int(tmdb_id)}.json"


def _fetch(tmdb_id):
    key = _key()
    if not key:
        return None
    url = f"{_API}/tv/{int(tmdb_id)}?{urllib.parse.urlencode({'api_key': key})}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": getattr(config, "USER_AGENT", None) or "Torrent-Ingest/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:   # noqa: S310
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError,
            TimeoutError):
        return None
    shape = {}
    for s in (data or {}).get("seasons") or []:
        try:
            n, cnt = int(s.get("season_number")), int(s.get("episode_count"))
        except (TypeError, ValueError):
            continue
        shape[n] = {"count": cnt, "name": str(s.get("name") or "")}
    return shape or None


def season_shape(tmdb_id):
    """`{season_number: {"count": int, "name": str}}` as TMDB has it, or None.

    Season 00 matters here and is NOT excluded the way `epguide.season_shape` excludes it:
    a Season-0 slot TMDB does not have is just as blank in Jellyfin as a Season-1 one.
    """
    if not tmdb_id:
        return None
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return None
    if tmdb_id in _mem:
        return _mem[tmdb_id]
    p = _cache_path(tmdb_id)
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        if blob.get("v") == _CACHE_V:
            shape = {int(k): v for k, v in (blob.get("shape") or {}).items()} or None
            ttl = CACHE_TTL_SEC if shape else MISS_CACHE_TTL_SEC
            if time.time() - float(blob.get("fetched_at", 0)) < ttl:
                _mem[tmdb_id] = shape
                return shape
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    shape = _fetch(tmdb_id)
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": _CACHE_V, "fetched_at": time.time(),
                                   "shape": shape or {}}), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass
    _mem[tmdb_id] = shape
    return shape


def serves(tmdb_id, season, episode) -> bool:
    """Whether TMDB has this exact slot, so Jellyfin can render a real title for it.

    **Returns True when it cannot tell.** That is the safe direction and it is deliberate:
    an unknown answer must not cause the fleet to lock its own metadata over a provider
    that would have done fine. Owning a file is not free — a locked sidecar is the fleet's
    word forever, and Jellyfin will never improve on it.
    """
    shape = season_shape(tmdb_id)
    if not shape:
        return True
    try:
        season, episode = int(season), int(episode)
    except (TypeError, ValueError):
        return True
    have = (shape.get(season) or {}).get("count")
    if have is None:
        return False                      # TMDB has no such season at all
    return 1 <= episode <= have


# Words that carry no identity -- every season of every franchise has them.
_GENERIC = {"season", "series", "part", "the", "a", "of", "and", "vol", "volume"}


def season_identity_matches(tmdb_id, season, arc_labels) -> bool:
    """Whether TMDB's NAME for this season is describing the same thing the arcs are.

    THE CASE THIS EXISTS FOR, and `serves()` alone gets it exactly backwards. TMDB's
    Season 05 for Monogatari is *MONOGATARI Series OFF & MONSTER Season*, a 2024 show with
    15 episodes. The fleet's guide calls Season 05 *Zoku Owarimonogatari*, 6 episodes from
    2019. So `serves(5, 1..6)` is True -- TMDB has those slot NUMBERS -- and every one of
    those six files would render a title from a completely different show.

    That is worse than a blank, because a blank is obviously wrong and a plausible wrong
    title is not.

    True when it cannot tell, like everything else here: an unknown answer must never cause
    the fleet to lock its own text over a provider that would have done fine.
    """
    shape = season_shape(tmdb_id)
    if not shape:
        return True
    name = (shape.get(season) or {}).get("name") or ""
    if not name or not arc_labels:
        return True
    def toks(t):
        return {w for w in re_split(t.lower()) if w and w not in _GENERIC}
    theirs = toks(name)
    ours = set()
    for lab in arc_labels:
        ours |= toks(str(lab))
    if not theirs or not ours:
        return True
    return bool(theirs & ours)


def re_split(t):
    import re as _re
    return _re.split(r"[^a-z0-9]+", t)


def specials_runs(tmdb_id):
    """TMDB's Season-0 episodes grouped into ARCS: `[(stem, [ep, ...]), ...]`, in order.

    WHY THIS IS WORTH A REQUEST. TVMaze -- the fleet's placement oracle -- carries some arcs
    GROUPED where the release splits them. Owarimonogatari's 2017 run is 7 files in the
    release and 3 entries in TVMaze ("Mayoi Hell", "Hitagi Rendezvous", "Ougi Dark"), so
    `arcmap.metadata_block` can only hedge: it hands over the three names and asks the run
    to work out which file is which part.

    Measured 2026-09-12, that hedge is expensive. Wave 3 of the Monogatari pack is 32 files
    of which only those 6 lacked per-file titles, and two successive 30-minute identify runs
    (`nemotron`, then `kimi-k3`) spent nearly every turn web-searching for exactly them and
    wrote no plan at all.

    TMDB has all seven individually -- `Owarimonogatari: Mayoi Hell (1)` .. `Ougi Dark (3)`
    -- each with a real synopsis. One cached request removes the only thing those runs could
    not be told.

    Grouped by the name prefix before the colon, which is how TMDB names this show's
    specials, and kept in episode order so a run maps onto files positionally.
    """
    key = _key()
    if not key or not tmdb_id:
        return []
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return []
    cached = _mem.get(("s0", tmdb_id))
    if cached is not None:
        return cached
    p = _cache_path(tmdb_id).with_name(f"{tmdb_id}-s0.json")
    eps = None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        if blob.get("v") == _CACHE_V and time.time() - float(blob.get("fetched_at", 0)) < CACHE_TTL_SEC:
            eps = blob.get("episodes")
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        eps = None
    if eps is None:
        url = (f"{_API}/tv/{tmdb_id}/season/0?"
               + urllib.parse.urlencode({"api_key": key}))
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"Accept": "application/json"}),
                    timeout=_TIMEOUT) as resp:                         # noqa: S310
                data = json.loads(resp.read().decode("utf-8", "replace"))
            eps = [{"number": e.get("episode_number"), "name": e.get("name") or "",
                    "overview": (e.get("overview") or "").strip()}
                   for e in (data or {}).get("episodes") or []
                   if e.get("episode_number") is not None]
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError,
                TimeoutError):
            eps = []
        try:
            p.write_text(json.dumps({"v": _CACHE_V, "fetched_at": time.time(),
                                     "episodes": eps}), encoding="utf-8")
        except OSError:
            pass
    runs = []
    for e in eps or ():
        stem = (e["name"].split(":")[0] or "").strip() if ":" in e["name"] else e["name"]
        if runs and runs[-1][0] == stem:
            runs[-1][1].append(e)
        else:
            runs.append((stem, [e]))
    _mem[("s0", tmdb_id)] = runs
    return runs


def movie_exists(tmdb_id):
    """Whether TMDB still has this FILM id. None when the question cannot be put.

    THE FAILURE THIS EXISTS FOR. `Ghost in the Shell - Arise - Another Mission (2013)` sat
    in `library_health.txt` for weeks as "movie has no Primary image; movie has no
    plot/overview", marked `[auto]` — meaning the doctor believed it was fixing it. It was
    not, and could not: the film's `.nfo` pins `<tmdbid>573120</tmdbid>`, and TMDB answers
    **404** for that id. Every repair pass searched against a dead id, found no candidate,
    filled nothing, and reported the same two lines again next cycle. Forever, silently.

    A dead pinned id is a different problem from missing metadata and needs a different
    answer — re-identify the film, do not re-fetch it — so it has to be told apart.
    """
    key = _key()
    if not key or not tmdb_id:
        return None
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return None
    url = f"{_API}/movie/{tmdb_id}?{urllib.parse.urlencode({'api_key': key})}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:   # noqa: S310
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def find_movie(title, year=None):
    """Best TMDB film match for a title: `{id, title, year, overview}` or None.

    The fallback for a film whose pinned id is dead. Conservative: returns nothing rather
    than a guess when TMDB offers nothing with a real overview.
    """
    key = _key()
    if not key or not title:
        return None
    q = {"api_key": key, "query": str(title)}
    if year:
        q["year"] = str(year)
    try:
        with urllib.request.urlopen(
                f"{_API}/search/movie?{urllib.parse.urlencode(q)}",
                timeout=_TIMEOUT) as resp:                            # noqa: S310
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError,
            TimeoutError):
        return None
    for m in (data or {}).get("results") or []:
        if (m.get("overview") or "").strip():
            return {"id": m.get("id"), "title": m.get("title"),
                    "year": (m.get("release_date") or "")[:4],
                    "overview": m["overview"]}
    return None


def unserved(tmdb_id, slots):
    """The `(season, episode)` slots in `slots` that TMDB cannot render. `[]` when unknown.

    This is the whole public point of the module: hand it what a plan intends to file and
    it names the files that need the fleet's own locked metadata.
    """
    if not season_shape(tmdb_id):
        return []
    return [(s, e) for s, e in slots if not serves(tmdb_id, s, e)]


# --- who the id actually is (HANDOFF 10.3) -----------------------------------
#
# THE INCIDENT THIS EXISTS FOR. Both Twilight Zone (2019) plans carried
# `tmdb_id: 80979, tvdb_id: 325542` and the run log says it "confirmed" them. TMDB
# 80979 is NOT the Jordan Peele series -- it is *Too Cute* (`萌宠成长记（精编版）`,
# 2013, Henry Strozier); TVDB 325542 is an unrelated 1995 Italian series; the correct
# TMDB for the Peele show is 83135. Nothing had ever asked the provider who the id
# belonged to, so the wrong `originaltitle`, the wrong `premiered` and byte-identical
# Too Cute artwork were locked over the owner's series for days -- and the local art
# outranks remote, so Jellyfin kept showing it even after a correct re-identification.
#
# THE RULE (owner, 2026-09-20): an id that names a different show may not pick art or
# author metadata. A mismatch STRIPS the id; a network error FAILS OPEN, because a
# blip must never fail an ingest (HANDOFF §5) -- but an unverified id is not evidence
# and may not be trusted later either.
#
# `external_ids` is TMDB's own record of the TVDB id for the same show, which is how a
# tvdb id is checked without a TVDB client: the fleet has no TVDB API key, and TMDB's
# mapping is the same one Jellyfin uses to cross-identify.

_IDENTITY_CACHE_V = 2                      # bumped when the identity shape changes
_IMAGE_BASE = "https://image.tmdb.org/t/p/original"


def _get_json(path, params):
    """GET a TMDB path. The key is read here and never returned. None on any failure."""
    key = _key()
    if not key:
        return None
    q = dict(params or ())
    q["api_key"] = key
    url = f"{_API}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": getattr(config, "USER_AGENT", None) or "Torrent-Ingest/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:   # noqa: S310
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def show_identity(tmdb_id):
    """Provider identity for a TMDB series id, or None when the question cannot be put.

    Returns:
      * `{"dead": True}` when TMDB answers 404 -- the plan carries an id that names
        nothing there, which is itself a mismatch;
      * `{"name", "original_name", "year", "first_air_date", "tvdb_id", "poster_path",
        "backdrop_path"}` when TMDB answers. `year` is `first_air_date[:4]` or None;
        `tvdb_id` is from `/external_ids` and None when TMDB records none;
      * None on a missing key or any transport error -- callers fail open.

    Cached on disk (7-day TTL) because this runs at plan-validation time for every
    plan; the id -> identity mapping only changes when providers merge entries.
    """
    if not tmdb_id:
        return None
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return None
    mem_key = ("ident", tmdb_id)
    if mem_key in _mem:
        return _mem[mem_key]
    p = _cache_path(tmdb_id).with_name(f"{tmdb_id}-identity.json")
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        if blob.get("v") == _IDENTITY_CACHE_V:
            if time.time() - float(blob.get("fetched_at", 0)) < CACHE_TTL_SEC:
                _mem[mem_key] = blob.get("identity")
                return blob.get("identity")
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    data = _get_json(f"/tv/{tmdb_id}", {})
    if data is None:
        return None                       # transport: no cache write, no opinion
    if data.get("_http_error") == 404:
        ident = {"dead": True}
    elif data.get("_http_error"):
        return None
    else:
        ext = _get_json(f"/tv/{tmdb_id}/external_ids", {}) or {}
        first = str(data.get("first_air_date") or "")
        last = str(data.get("last_air_date") or "")
        ident = {
            "name": (data.get("name") or "").strip(),
            "original_name": (data.get("original_name") or "").strip(),
            "first_air_date": first,
            "last_air_date": last,
            "year": int(first[:4]) if first[:4].isdigit() else None,
            "tvdb_id": str(ext.get("tvdb_id")) if ext.get("tvdb_id") else None,
            "poster_path": data.get("poster_path"),
            "backdrop_path": data.get("backdrop_path"),
        }
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": _IDENTITY_CACHE_V, "fetched_at": time.time(),
                                   "identity": ident}), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass
    _mem[mem_key] = ident
    return ident


def art_urls(tmdb_id):
    """`{"poster": url|None, "backdrop": url|None}` for a series, or None when unknown.

    Used by `media_doctor` to replace contaminated on-disk art with the verified
    identity's own images. An `art_urls` answer is only as good as the id passed in,
    so callers must verify the id first (`show_identity`).
    """
    ident = show_identity(tmdb_id)
    if not ident or ident.get("dead"):
        return None
    if ident.get("_http_error"):
        return None
    return {
        "poster": f"{_IMAGE_BASE}{ident['poster_path']}" if ident.get("poster_path") else None,
        "backdrop": (f"{_IMAGE_BASE}{ident['backdrop_path']}"
                     if ident.get("backdrop_path") else None),
    }


def season_info(tmdb_id, season):
    """`{"name", "year", "air_date", "poster_url"}` for one season, or None.

    Used by `media_doctor` when it rewrites the contaminated `season.nfo` year and
    `seasonNN-poster.jpg` after an identity repair: the season's own air date is the
    correct `<year>`, not the series premiere.
    """
    if not tmdb_id or season is None:
        return None
    try:
        tmdb_id, season = int(tmdb_id), int(season)
    except (TypeError, ValueError):
        return None
    data = _get_json(f"/tv/{tmdb_id}/season/{season}", {})
    if not data or data.get("_http_error"):
        return None
    air = str(data.get("air_date") or "")
    return {
        "name": (data.get("name") or "").strip(),
        "air_date": air,
        "year": int(air[:4]) if air[:4].isdigit() else None,
        "poster_url": (f"{_IMAGE_BASE}{data['poster_path']}"
                       if data.get("poster_path") else None),
    }


def season_poster_url(tmdb_id, season):
    """The season poster URL for `(tmdb_id, season)`, or None when TMDB has none."""
    info = season_info(tmdb_id, season)
    return info["poster_url"] if info else None


def episode_overviews(tmdb_id, season):
    """`{(season, episode): overview}` for one season, or {} when the question cannot
    be put. The synopsis source `repair_metadata` falls back to when TVMaze carries
    episode NAMES but no summaries -- Toriko's case (146 names, 0 summaries, 10.4)."""
    if not tmdb_id or season is None:
        return {}
    try:
        tmdb_id, season = int(tmdb_id), int(season)
    except (TypeError, ValueError):
        return {}
    data = _get_json(f"/tv/{tmdb_id}/season/{season}", {})
    if not data or data.get("_http_error"):
        return {}
    out = {}
    for e in data.get("episodes") or []:
        n, ep = e.get("episode_number"), (e.get("overview") or "").strip()
        if n is None or not ep:
            continue
        try:
            out[(int(season), int(n))] = ep
        except (TypeError, ValueError):
            continue
    return out


def find_show(title, year=None):
    """Best TMDB series match for a title/year: `{id, name, year}` or None.

    The re-identification fallback for a series whose pinned id is dead. Conservative:
    requires a non-empty name; TDMB search itself ranks by relevance and the caller
    should only use this when the old id answered 404 (never to override a live one).
    """
    if not title:
        return None
    data = _get_json("/search/tv", {"query": str(title),
                                    **({"first_air_date_year": str(year)} if year else {})})
    if not data or data.get("_http_error"):
        return None
    for m in data.get("results") or []:
        first = str(m.get("first_air_date") or "")
        if not (m.get("name") or "").strip():
            continue
        return {"id": m.get("id"), "name": m["name"].strip(),
                "year": int(first[:4]) if first[:4].isdigit() else None}
    return None
