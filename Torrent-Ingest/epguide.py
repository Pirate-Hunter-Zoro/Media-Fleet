"""Authoritative season/episode numbering for a show, from a FREE key-less source.

The gap this closes. When a release names its files by EPISODE TITLE rather than by
number -- "1-It.Takes.Ahhh!.Valley...mkv", "Gran.the.Unfriendly.Ghost.mkv" -- the identify
step has nothing to resolve them with. It sees the torrent's filenames and a digest of the
existing library, and no episode list at all. So it guesses, and the season-gap guard then
turns that guess into a confident wrong answer: a Dawn of the Croods pack of SEASON 3
episodes was filed as Season 02 purely because Season 02 did not exist on disk yet (its own
pack was still downloading), and the Season 4 pack was refused outright.

Jellyfin is not the oracle either, despite holding real metadata: it labels what we have
ALREADY placed, so it confirms the guess rather than checking it.

TVMaze is: a complete season/episode/title list, no API key, no account, no cost -- which
keeps the fleet inside its free-only rule (the owner pays for the Mullvad exit node and
nothing else). One request per show, cached on disk for CACHE_TTL_SEC, so a re-ingest of the
same series costs nothing.

Everything here FAILS SOFT: any error returns None and the caller keeps its existing
behaviour. A metadata lookup must never be able to block an ingest.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import config

_API = "https://api.tvmaze.com/singlesearch/shows"
_CACHE_V = 3                            # bumped when the cached shape changes
CACHE_TTL_SEC = 30 * 24 * 3600          # episode lists change on the scale of new seasons
# A show TVMaze does not know is cached too, for much less time. Without this every lookup
# for an unknown show pays a fresh request -- and, for a name that resolves to nothing, the
# full _TIMEOUT -- on every process start. The ingest daemon is long-lived so it pays that
# once, but the replay tools (`scripts/test_placement_guards.py`) are not, and a guard whose
# regression test costs an hour of timeouts is a guard nobody re-measures. Short, because a
# miss is far more likely to be temporary (a rename, an outage) than a hit is to be wrong.
MISS_CACHE_TTL_SEC = 24 * 3600
_TIMEOUT = 20

_mem: dict[str, list | None] = {}


def _cache_path(norm: str):
    d = config.STATE_DIR / "epguide"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")[:80] or "unknown"
    return d / f"{safe}.json"


def normalize_title(s: str) -> str:
    """Fold an episode title for comparison: accents, punctuation, case, part markers.

    Release filenames mangle titles freely -- "It.Takes.Ahhh!.Valley" for
    "It Takes Ahhh! Valley (1)" -- so the comparison drops everything that is not a letter
    or digit, and strips a trailing part/number marker so a two-part episode still matches
    the whole it belongs to.
    """
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"\((\d+)\)\s*$", " ", s)                 # "(1)" / "(2)" part markers
    s = re.sub(r"\b(pt|part)\.?\s*\d+\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


_TAG = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    """TVMaze summaries are HTML fragments; .nfo plots are plain text."""
    t = _TAG.sub(" ", s or "")
    for ent, ch in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"),
                    ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        t = t.replace(ent, ch)
    return " ".join(t.split())


def _fetch(series_name: str):
    """`(episodes, answered, specials)` -- the list, whether TVMaze ANSWERED, and the
    show's unnumbered specials.

    The two failures must not be confused. A 404 means the guide genuinely does not carry
    this show, which is a durable fact worth caching. A timeout or a connection error means
    nothing at all, and caching it would silently disable the season ceiling for that show
    for as long as the entry lived -- turning one bad minute of network into a day with a
    safety guard switched off. So only an answered request is cacheable.
    """
    ua = getattr(config, "USER_AGENT", None) or "Torrent-Ingest/1.0"

    def _get(url):
        req = urllib.request.Request(url, headers={"User-Agent": ua,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:   # noqa: S310
            return json.loads(resp.read().decode("utf-8", "replace"))

    url = f"{_API}?{urllib.parse.urlencode({'q': series_name, 'embed': 'episodes'})}"
    try:
        data = _get(url)
    except urllib.error.HTTPError as e:
        return None, e.code == 404, []      # 404 == "no such show", a real answer
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None, False, []
    eps = ((data or {}).get("_embedded") or {}).get("episodes") or []

    # SPECIALS, in a second request. `embed=episodes` returns only the REGULAR run, and
    # for a multi-arc franchise that is most of what the release holds: every arc of
    # Monogatari that is not one of the six broadcast seasons -- Nekomonogatari (Black),
    # Hanamonogatari, Tsukimonogatari, Koyomimonogatari, Owarimonogatari's 2017 run -- is
    # carried here as an unnumbered special, with its real title and synopsis.
    #
    # It costs one extra cached request a month per show, and it is the difference between
    # the identify run being HANDED an arc's episode titles and going to find them. A run
    # measured on 2026-09-12 spent every one of its 19 turns web-searching for exactly
    # this and wrote no plan at all.
    specials = []
    show_id = (data or {}).get("id")
    if show_id:
        try:
            for e in _get(f"https://api.tvmaze.com/shows/{int(show_id)}/episodes"
                          f"?specials=1") or []:
                if e.get("number") is None and e.get("name"):
                    specials.append({"name": e["name"],
                                     "airdate": e.get("airdate") or "",
                                     "season": e.get("season"),
                                     "summary": strip_html(e.get("summary") or "")})
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError,
                TypeError, TimeoutError):
            specials = []          # fails soft: the caller simply has no specials list

    out = []
    for e in eps:
        s, n, nm = e.get("season"), e.get("number"), e.get("name")
        if isinstance(s, int) and isinstance(n, int) and nm:
            # `summary` comes back as an HTML fragment ("<p>...</p>"). It costs nothing --
            # the same request already carries it -- and it is what lets a metadata repair
            # fill a blank episode deterministically instead of asking a model
            # (`scripts/repair_metadata.py`). Absent for unaired episodes; that is normal.
            out.append({"season": s, "number": n, "name": nm,
                        "airdate": e.get("airdate") or "",
                        "summary": strip_html(e.get("summary") or "")})
    return (out or None), True, specials


def _blob(series_name: str):
    """The cached `{episodes, specials}` blob for a show, fetching it when stale. `{}` on
    any failure -- every caller here fails soft."""
    if not series_name:
        return {}
    norm = normalize_title(series_name)
    if norm in _mem:
        return _mem[norm]
    p = _cache_path(norm)
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        if blob.get("v") != _CACHE_V:
            raise ValueError("stale cache layout")   # pre-specials blob; refetch once
        ttl = CACHE_TTL_SEC if blob.get("episodes") else MISS_CACHE_TTL_SEC
        if time.time() - float(blob.get("fetched_at", 0)) < ttl:
            _mem[norm] = blob
            return blob
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    eps, answered, sp = _fetch(series_name)
    if not answered:
        return {}
    blob = {"v": _CACHE_V, "fetched_at": time.time(), "series": series_name,
            "episodes": eps or [], "specials": sp or []}
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass
    _mem[norm] = blob
    return blob


def episodes(series_name: str) -> list | None:
    """The show's full [{season, number, name, airdate, summary}] list, or None."""
    return (_blob(series_name).get("episodes") or None)


def specials(series_name: str) -> list:
    """The show's UNNUMBERED specials, in air order: `[{name, airdate, season, summary}]`.

    For a multi-arc franchise this is where most of a release actually lives. TVMaze
    carries Nekomonogatari (Black), Hanamonogatari, Tsukimonogatari, Koyomimonogatari and
    Owarimonogatari's 2017 run as specials of `Monogatari Series` -- each with its real
    per-episode title and synopsis, which is exactly what `validate_plan` demands of a
    Season-0 file and exactly what a run otherwise goes to the web for.

    `[]` whenever the guide cannot say, so a caller that has no specials block simply
    behaves as it did before.
    """
    return list(_blob(series_name).get("specials") or [])


def locate(series_name: str, text: str):
    """`(season, number, title)` for the episode whose TITLE appears in `text`, else None.

    Deliberately conservative: the episode title must appear as a contiguous run of words in
    the normalized filename, it must be at least MIN_TITLE_WORDS words long (so a one-word
    title like "Home" cannot match half the library), and it must be UNAMBIGUOUS -- if two
    different episodes match, nothing is returned. Being silent is always better than being
    confidently wrong here; that is the failure this module exists to stop.
    """
    eps = episodes(series_name)
    if not eps:
        return None
    hay = normalize_title(text)
    if not hay:
        return None
    hits = []
    for e in eps:
        t = normalize_title(e["name"])
        if not t or len(t.split()) < config.EPGUIDE_MIN_TITLE_WORDS:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", hay):
            hits.append((e["season"], e["number"], e["name"], len(t)))
    if not hits:
        return None
    hits.sort(key=lambda h: (-h[3], h[0], h[1]))      # longest (most specific) title first
    best = hits[0]
    top = [h for h in hits if h[3] == best[3]]
    # A two-part episode ("It Takes Ahhh! Valley (1)" and "(2)") folds to ONE normalized
    # title, so both parts match equally well. They disagree only on the episode number and
    # agree on the SEASON -- which is the answer the placement actually needs -- so that is
    # not ambiguity, and the earliest part is returned. Genuine ambiguity is two matches in
    # DIFFERENT seasons, and there nothing is returned: silence beats a confident wrong
    # answer, which is the failure this module exists to stop.
    if len({h[0] for h in top}) > 1:
        return None
    return best[0], best[1], best[2]


def season_shape(series_name: str) -> dict | None:
    """`{season: episode_count}` for every AIRED season, or None when the guide cannot say.

    The shape, not just the ceiling, because two different questions need it and both are
    answered wrongly by a bare maximum:

      * "IS the season the source names a season this show actually has?" TVMaze numbers
        some long-running shows by YEAR -- Bleach's seasons are 2004..2012 -- so a release
        named `Bleach.S17E42` names a season that is not in the provider's vocabulary at
        all. `17 <= 2012` says yes and is meaningless; `17 in {2004..2012}` says no, which
        is the truth. Anything comparing the two numbering systems by magnitude is
        comparing apples to a calendar.
      * "Is the source numbering EPISODES absolutely?" A pack of
        `Pocket.Monsters.2023.S01E78` files is not claiming a season 1 that runs to 78
        episodes; it is numbering the whole run from 1 and leaving the season at 1. The
        provider's episode count for that season is what separates the two: 78 against a
        45-episode season 1 is absolute numbering, and the plan's remap to Season 03 is the
        correct reading, not a fault.

    Season 0 (specials) is excluded: it is not part of the aired ordering.
    """
    eps = episodes(series_name)
    if not eps:
        return None
    shape: dict[int, int] = {}
    for e in eps:
        s = e.get("season")
        if isinstance(s, int) and s > 0:
            shape[s] = shape.get(s, 0) + 1
    return shape or None


def max_season(series_name: str):
    """The highest season number the provider knows for `series_name`, or None.

    The CEILING a placement may never exceed. A show that has aired four seasons cannot
    receive a Season 05 — that is not a judgement call about numbering conventions, it is
    a fact, and it is the one check that would have blocked the Dawn of the Croods S04
    pack from being filed as `Season 05` (§4.91). Season 0 (specials) is excluded: it is
    not part of the aired ordering and a show may hold specials without them raising the
    ceiling.

    Fails soft like everything else here: None whenever the guide cannot say, which leaves
    the caller's existing behaviour untouched.
    """
    shape = season_shape(series_name)
    return max(shape) if shape else None
