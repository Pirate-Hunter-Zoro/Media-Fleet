#!/usr/bin/env python3
"""Resolve each manga series to its volume -> chapter SETS, once, and cache it.

WHY THIS IS A CACHE AND NOT A PROMPT
    The identify prompt asks a run to list the chapters a new volume supersedes, and that
    is the one part of placement the release cannot see: the volume->chapter mapping lives
    in the series' bibliographic record, not in the filename. Asking the model per run made
    the decision depend on whatever snapshot the library happened to be in, and the model
    is non-deterministic. The mapping is bibliographic data, so it is fetched from a
    provider, stored per series, and handed to the reconciler as a fact.

TWO PROVIDERS, ONE SOURCE OF RANGES
    AniList (`build_comic_franchises.py` already talks to it, free and key-less) resolves
    the series identity and gives a canonical title. MangaDex's `/manga/{id}/aggregate`
    is the range source: it returns the chapters each volume actually contains. Chapter
    numbers are stored as SETS, never as a min/max range -- manga numbering has gaps and
    bonus/fractional chapters, and a min/max would cover a chapter the volume does not
    contain. Fractional chapter keys (`12.5`) are dropped: a library chapter is filed as
    an integer `cNNNN`, so a fractional key can never prove that integer covered.

FAIL-OPEN, ALWAYS
    Every network failure returns None and writes nothing: no provider answer means no
    update and -- because the reconciler purges only what a fresh cached map proves -- no
    purge. This is the fleet's network rule: a blip must never turn into a deletion.

THE AI IS THE LAST RESORT, ONCE PER SERIES
    If the providers are silent about some volumes, ONE small completion is asked for the
    missing volumes' chapter ranges, walking the same free provider chain the identify
    runs use (`config.enabled_ai_attempts` + `ai_client.complete`). The answer is cached
    with its own confidence marker and is only allowed to author a purge at or above
    `AI_MIN_CONFIDENCE`. If every provider fails, nothing is cached and nothing changes.
    The steady-state AI cost is zero: a fresh cache is served without any call, and the
    periodic refresh only touches stale entries.

USAGE
    python3 scripts/manga_volume_map.py --series "Undead Unluck"            # show cached
    python3 scripts/manga_volume_map.py --series "Undead Unluck" --refresh  # fetch
    python3 scripts/manga_volume_map.py --all --refresh --no-ai             # shelf sweep
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_client                                                       # noqa: E402
import config                                                          # noqa: E402
import library                                                         # noqa: E402

ANILIST = "https://graphql.anilist.co"
MANGADEX = "https://api.mangadex.org"
# Both APIs reject a request with no User-Agent, and AniList's rejection reads as
# "no such series" -- the silent-empty class. `build_comic_franchises.py` learned this.
UA = "Torrent-Ingest-manga-volume-map/1.0 (+https://github.com/Pirate-Hunter-Zoro)"
CACHE_PATH = config.STATE_DIR / "manga_volume_map.json"
CACHE_VERSION = 1
TTL_DAYS = 75
AI_MAX_PROMPT = 1800
AI_MIN_CONFIDENCE = 0.8

_ANILIST_QUERY = """
query($s:String!){
  Page(page:1, perPage:10){
    media(search:$s, type:MANGA, sort:START_DATE){
      id
      format
      volumes
      chapters
      title{ romaji english native }
    }
  }
}"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm(name: str) -> str:
    return library.normalize_folder_name(name)


def _get_json(url: str, data: bytes | None = None, timeout: int = 25):
    """GET/POST a JSON endpoint with the required UA. None on ANY failure (fail-open)."""
    req = urllib.request.Request(
        url, data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:     # noqa: S310
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


# --- cache -------------------------------------------------------------------

def load_cache() -> dict:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": CACHE_VERSION, "series": {}}
    if not isinstance(data, dict) or not isinstance(data.get("series"), dict):
        return {"version": CACHE_VERSION, "series": {}}
    data.setdefault("version", CACHE_VERSION)
    return data


def save_cache(cache: dict) -> None:
    """Atomic write: a half-written cache must never be read as "no mapping"."""
    cache["version"] = CACHE_VERSION
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except OSError:
        pass


def is_stale(entry: dict, now: datetime | None = None) -> bool:
    """Stale when past TTL, or when a provider/AI failure parked it for a retry.

    `retry_at` is a short backoff written after a totally failed lookup: a provider outage
    must not become an AI call every scheduled tick, and it must not freeze the series for
    the whole TTL either.
    """
    now = now or _now()
    retry = entry.get("retry_at")
    if retry:
        try:
            if now < datetime.fromisoformat(str(retry).replace("Z", "+00:00")):
                return False
        except ValueError:
            pass
    if not ((entry.get("volumes") or {}) or (entry.get("ai_volumes") or {})):
        return True                       # nothing usable -> always worth another try
    try:
        fetched = datetime.fromisoformat(str(entry.get("fetched_at", "")).replace("Z", "+00:00"))
    except ValueError:
        return True
    return now - fetched > timedelta(days=TTL_DAYS)


def allowed_source(entry: dict) -> bool:
    """Whether this entry may author ANY purge (kept for the coarse CLI/report test)."""
    if entry.get("source") == "mangadex":
        return True
    if entry.get("source") == "ai":
        try:
            return float(entry.get("confidence", 0)) >= AI_MIN_CONFIDENCE
        except (TypeError, ValueError):
            return False
    return False


def volume_allowed(entry: dict, volume: int) -> bool:
    """Whether THIS volume's chapter set may author a purge.

    Per-volume, because the two provenances have different authority. A provider set is
    exact chapter numbers from MangaDex. An AI set is ranges a free model guessed, so it
    only counts at or above `AI_MIN_CONFIDENCE`. A volume that is neither is unknown, and
    its chapters are kept.
    """
    if not entry:
        return False
    if str(volume) in (entry.get("volumes") or {}):
        return True
    if str(volume) in (entry.get("ai_volumes") or {}):
        try:
            return float(entry.get("confidence", 0)) >= AI_MIN_CONFIDENCE
        except (TypeError, ValueError):
            return False
    return False


# --- AniList identity --------------------------------------------------------

def _pick_title(media: dict) -> str:
    t = media.get("title") or {}
    return t.get("english") or t.get("romaji") or t.get("native") or ""


def anilist_search(name: str) -> dict | None:
    """Best AniList manga match for `name`, or None. Key-less; UA required."""
    body = json.dumps({"query": _ANILIST_QUERY, "variables": {"s": name}}).encode()
    data = _get_json(ANILIST, data=body)
    page = ((data or {}).get("data") or {}).get("Page") or {}
    want = _norm(name)
    best = None
    for m in page.get("media") or []:
        title = _pick_title(m)
        n = _norm(title)
        if not n:
            continue
        score = 0
        if n == want:
            score = 3
        elif n.startswith(want) or want.startswith(n):
            score = 2
        elif want in n:
            score = 1
        if score and (best is None or score > best[0]):
            best = (score, m)
    if not best:
        return None
    m = best[1]
    return {"id": m.get("id"), "title": _pick_title(m), "format": m.get("format"),
            "volumes": m.get("volumes"), "chapters": m.get("chapters")}


# --- MangaDex ranges ---------------------------------------------------------

def mangadex_search(title: str) -> dict | None:
    """Best MangaDex manga match for `title`, or None."""
    url = f"{MANGADEX}/manga?title={urllib.parse.quote(title)}&limit=10"
    data = _get_json(url)
    rows = (data or {}).get("data") or []
    want = _norm(title)
    best = None
    for m in rows:
        attrs = m.get("attributes") or {}
        titles = [attrs.get("title") or {}]
        for alt in attrs.get("altTitles") or []:
            titles.append(alt)
        score = 0
        for t in titles:
            for value in (t or {}).values():
                n = _norm(str(value))
                if not n:
                    continue
                if n == want:
                    score = max(score, 3)
                elif n.startswith(want) or want.startswith(n):
                    score = max(score, 2)
                elif want in n:
                    score = max(score, 1)
        if score and (best is None or score > best[0]):
            best = (score, m.get("id"))
    return {"id": best[1]} if best else None


def mangadex_aggregate(manga_id: str) -> dict | None:
    # Deliberately NOT restricted to `translatedLanguage[]=en`. Scanlation chapters are
    # routinely uploaded with no volume tag, so the English view of a series is often
    # `none`-only (measured: Mashle has no English volume data at all) while the full
    # aggregate carries the tankoubon volumes. Chapter numbers are language-independent.
    return _get_json(f"{MANGADEX}/manga/{manga_id}/aggregate")


def parse_aggregate(data: dict) -> tuple[dict, list]:
    """`{volume: [chapter ints]}` and the volumes it could not place, from an aggregate.

    Pure and exact. Integer chapter keys become the SET the volume contains; fractional
    keys (`12.5`, a bonus) are ignored because a library chapter is filed as an integer
    `cNNNN`, so a fractional key can never prove that integer covered. A volume keyed
    anything but a plain int (`none`, `1.5`) is returned in `unmapped` -- unknown, so the
    reconciler keeps its chapters.
    """
    volumes, unmapped = {}, []
    raw = (data or {}).get("volumes") or {}
    for vkey, vol in raw.items():
        text = str(vkey).strip()
        # A plain digit key ("1", "01") is a volume number; anything else ("1.5",
        # "none") is a volume this parser cannot place, so it is reported unmapped and
        # the reconciler keeps its chapters.
        if not text.isdigit() or int(text) <= 0:
            unmapped.append(text)
            continue
        vnum = int(text)
        chapters = set()
        for ckey in (vol.get("chapters") or {}):
            try:
                cnum = int(str(ckey))
            except (TypeError, ValueError):
                continue                    # fractional / unnumbered: never proves an int
            if cnum > 0:
                chapters.add(cnum)
        volumes[vnum] = sorted(chapters)
    return volumes, sorted(unmapped, key=str)


# --- AI fallback -------------------------------------------------------------

def _parse_ai_ranges(text: str) -> tuple[dict, float] | None:
    """Parse the model's single JSON object. Returns ({volume: [chapters]}, confidence)."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    raw = data.get("volumes")
    if not isinstance(raw, dict):
        return None
    out = {}
    for vkey, spec in raw.items():
        try:
            vnum = int(str(vkey))
        except (TypeError, ValueError):
            continue
        chapters = set()
        if isinstance(spec, dict):
            start, end = spec.get("start"), spec.get("end")
            try:
                lo, hi = int(start), int(end)
            except (TypeError, ValueError):
                continue
            if 0 < lo <= hi and hi - lo <= 500:
                chapters.update(range(lo, hi + 1))
        elif isinstance(spec, list) and spec:
            try:
                nums = [int(x) for x in spec]
            except (TypeError, ValueError):
                continue
            if all(0 < x <= 5000 for x in nums):
                chapters.update(nums)
        if chapters:
            out[vnum] = sorted(chapters)
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if not out:
        return None
    return out, confidence


def _ai_prompt(name: str, missing: list, known: dict) -> str:
    lines = [f"{v} (we already know: {known.get(str(v), 'unknown')})" for v in missing]
    return (
        "You are a manga bibliographer. For the series below, list the chapter numbers "
        "each of these volumes contains.\n"
        f"Series: {name}\n"
        "Volumes: " + "; ".join(lines) + "\n"
        "Answer with ONE JSON object and nothing else:\n"
        '{"volumes": {"<volume>": {"start": <first chapter>, "end": <last chapter>}}, '
        '"confidence": <0.0-1.0>}\n'
        "Only include a volume when you are sure of the inclusive chapter range; its "
        "chapters must be exactly that range. Omit anything uncertain and set confidence "
        "below 0.8. If you cannot answer, return {\"volumes\": {}, \"confidence\": 0}."
    )


def _ai_worker() -> int:
    """Child mode for `ai_fallback`: one bounded attempt over the free chain.

    Runs in a subprocess so the caller can KILL it. `ai_client._post` may pace a rolling
    rate-limit window for minutes; a library module that calls it in-process can wedge a
    scheduled daemon on a provider's clock. The parent imposes a hard wall-clock timeout,
    which subprocess is the only kill-backed way to get (same reasoning as ai_runner).
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        prompt = _ai_prompt(str(payload.get("name") or ""),
                            list(payload.get("missing") or []),
                            payload.get("known") or {})
    except (ValueError, TypeError):
        print("{}")
        return 1
    if len(prompt) > AI_MAX_PROMPT:
        print("{}")
        return 1
    for attempt in config.enabled_ai_attempts():
        try:
            text = ai_client.complete(prompt, model=attempt.get("model", ""),
                                      max_tokens=400,
                                      base_url=attempt.get("base_url", ""),
                                      key=attempt.get("key", ""))
        except Exception:                                                  # noqa: BLE001
            continue
        parsed = _parse_ai_ranges(text)
        if parsed:
            volumes, confidence = parsed
            print(json.dumps({"volumes": {str(k): v for k, v in volumes.items()},
                              "confidence": confidence}))
            return 0
    print("{}")
    return 1


def ai_fallback(name: str, missing: list, known: dict,
                timeout: int = 180) -> tuple[dict, float] | None:
    """ONE small completion for the volumes the provider was silent about.

    Returns ({volume: chapters}, confidence) or None when every attempt fails or the
    wall-clock bound is hit. Never raises; never called per chapter and never per run.
    """
    if not missing:
        return None
    import subprocess                                                     # noqa: PLC0415
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--ai-worker"],
            input=json.dumps({"name": name, "missing": list(missing), "known": known}),
            capture_output=True, text=True, timeout=timeout,
            cwd=str(config.PROJECT_ROOT))
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except ValueError:
        return None
    raw = data.get("volumes")
    if not isinstance(raw, dict) or not raw:
        return None
    out = {}
    for vkey, chapters in raw.items():
        try:
            out[int(vkey)] = [int(c) for c in chapters]
        except (TypeError, ValueError):
            continue
    if not out:
        return None
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return out, confidence


# --- resolution + cache access ----------------------------------------------

def refresh(name: str, allow_ai: bool = True, needed=None) -> dict | None:
    """Fetch the map for `name` and cache it. None and no write on any failure.

    Provider first. Only where MangaDex is silent about volumes we actually OWN (`needed`,
    supplied by the caller that enumerated the shelf) or about volumes it itself reported
    unmapped, ONE AI fallback runs for all of them together.
    """
    al = anilist_search(name)
    search_title = (al or {}).get("title") or name
    md = mangadex_search(search_title)
    volumes, unmapped = {}, []
    ai_volumes, confidence = {}, 0.0
    notes = []
    if md:
        agg = mangadex_aggregate(md["id"])
        if agg:
            volumes, unmapped = parse_aggregate(agg)
            notes.append(f"mangadex:{md['id']}")
    if allow_ai:
        candidates = {int(v) for v in unmapped if str(v).isdigit()}
        candidates |= {int(v) for v in (needed or []) if str(v).isdigit()}
        missing = sorted(v for v in candidates if v not in volumes)
        if missing:
            ai = ai_fallback(name, missing, volumes)
            if ai:
                filled, confidence = ai
                for v, chapters in filled.items():
                    if v not in volumes:
                        ai_volumes[v] = chapters
                    if str(v) in unmapped:
                        unmapped.remove(str(v))
                if ai_volumes:
                    notes.append("ai")
    now = _now()
    if not volumes and not ai_volumes:
        # Nothing usable. Remember the attempt for a day: a provider outage must not
        # become per-tick AI spend, and must not freeze the series for the full TTL.
        entry = {
            "name": name,
            "anilist_id": (al or {}).get("id"),
            "mangadex_id": (md or {}).get("id"),
            "fetched_at": _iso(now),
            "retry_at": _iso(now + timedelta(days=1)),
            "source": "none",
            "confidence": 0.0,
            "volumes": {},
            "ai_volumes": {},
            "unmapped_volumes": sorted(unmapped, key=str),
            "notes": "+".join(notes),
        }
        cache = load_cache()
        cache["series"][_norm(name)] = entry
        save_cache(cache)
        return entry
    source = "mangadex" if volumes and not ai_volumes else ("ai" if ai_volumes else "none")
    entry = {
        "name": name,
        "anilist_id": (al or {}).get("id"),
        "mangadex_id": (md or {}).get("id"),
        "fetched_at": _iso(now),
        "source": source,
        "confidence": confidence,
        "volumes": {str(v): c for v, c in sorted(volumes.items())},
        "ai_volumes": {str(v): c for v, c in sorted(ai_volumes.items())},
        "unmapped_volumes": sorted(unmapped, key=str),
        "notes": "+".join(notes),
    }
    cache = load_cache()
    cache["series"][_norm(name)] = entry
    save_cache(cache)
    return entry


def get(name: str, allow_network: bool = True, allow_ai: bool = True,
        needed=None) -> dict | None:
    """Cached entry if fresh; refresh when allowed; None otherwise. Never blocks filing.

    `allow_network=False` is the ingest hook's mode: a cache miss is a miss there, and the
    caller enqueues a refresh instead of waiting on the network inside the filing cycle.
    """
    entry = load_cache()["series"].get(_norm(name))
    if entry and not is_stale(entry):
        return entry
    if not allow_network:
        return entry                      # stale-but-present still beats no map
    return refresh(name, allow_ai=allow_ai, needed=needed)


def known_volume(entry: dict | None, volume: int) -> list | None:
    """The cached chapter set for one volume, or None when unknown."""
    if not entry:
        return None
    for key in ("volumes", "ai_volumes"):
        v = (entry.get(key) or {}).get(str(volume))
        if isinstance(v, list):
            return list(v)
    return None


# --- CLI ---------------------------------------------------------------------

def _shelf_series() -> list:
    root = config.MEDIAFS_MOUNT / "Comics" / "Manga"
    if not root.exists():
        root = config.COMICS_ROOT / "Manga"
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def main() -> int:
    if sys.argv[1:2] == ["--ai-worker"]:
        return _ai_worker()
    ap = argparse.ArgumentParser(description="Manga volume -> chapter map cache.")
    ap.add_argument("--series", action="append", default=[],
                    help="series name (repeatable)")
    ap.add_argument("--all", action="store_true", help="every series on the manga shelf")
    ap.add_argument("--refresh", action="store_true", help="fetch (else show the cache)")
    ap.add_argument("--no-ai", action="store_true", help="provider only; no AI fallback")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    names = list(args.series)
    if args.all:
        names.extend(_shelf_series())
    if not names:
        ap.error("give --series NAME (repeatable) or --all")

    out = {}
    for name in names:
        entry = refresh(name, allow_ai=not args.no_ai) if args.refresh \
            else get(name, allow_network=False)
        out[name] = entry
        if not args.json:
            if not entry:
                print(f"{name}: no map" + (" (refresh failed)" if args.refresh else ""))
                continue
            counts = {k: len(v) for k, v in (entry.get("volumes") or {}).items()}
            for k, v in (entry.get("ai_volumes") or {}).items():
                counts[f"{k}*ai"] = len(v)
            volbits = ", ".join(f"v{k}:{n}ch" for k, n in
                                sorted(counts.items(),
                                       key=lambda kv: int(re.sub(r"\D", "", kv[0]) or 0)))
            print(f"{name}: source={entry.get('source')} "
                  f"conf={entry.get('confidence')} fetched={entry.get('fetched_at')} "
                  f"unmapped={entry.get('unmapped_volumes')} [{volbits}]")
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
