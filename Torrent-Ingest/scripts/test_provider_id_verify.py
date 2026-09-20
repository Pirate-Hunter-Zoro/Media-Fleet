#!/usr/bin/env python3
"""A provider id the provider contradicts may not pick art (HANDOFF 10.3).

THE INCIDENT. Both Twilight Zone (2019) plans carried `tmdb_id 80979, tvdb_id 325542`
and the run log says it "confirmed" them. TMDB 80979 is *Too Cute* (`萌宠成长记（精编版）`,
2013) and TVDB 325542 is an unrelated 1995 Italian series; the correct TMDB is 83135.
The wrong id then selected the Too Cute cover and locked the Too Cute `originaltitle`
and `premiered` over the owner's series, and because local art outranks remote the
fault survived every later Jellyfin refresh.

This pins `library.verify_provider_ids` in both directions:
  * a contradicted tmdb id is STRIPPED (and a tvdb id from the same wrong lookup with it);
  * a tvdb id that disagrees with TMDB's own `external_ids` is stripped alone;
  * a 404 is a mismatch; a transport error keeps the id (fail open);
  * a romaji-vs-English title with agreeing years is NOT a false positive.

No network: `tmdbguide.show_identity` is stubbed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import library                                                       # noqa: E402
import tmdbguide                                                     # noqa: E402

failures: list[str] = []


def check(label, cond):
    print(f"{'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


IDENT = {"name": "The Twilight Zone", "original_name": "The Twilight Zone",
         "year": 2019, "first_air_date": "2019-04-01", "tvdb_id": "362579"}
TOO_CUTE = {"name": "萌宠成长记（精编版）", "original_name": "萌宠成长记（精编版）",
            "year": 2013, "first_air_date": "2013-01-30", "tvdb_id": "325542"}


def plan(**over):
    p = {"media_type": "show", "title": "The Twilight Zone", "year": 2019,
         "tmdb_id": 83135, "tvdb_id": "362579", "files": []}
    p.update(over)
    return p


def with_ident(answer, fn):
    old = tmdbguide.show_identity
    tmdbguide.show_identity = lambda _id: answer
    try:
        return fn()
    finally:
        tmdbguide.show_identity = old


# 1. Agreeing identity and tvdb id are kept, nothing is stripped.
p = plan()
with_ident(IDENT, lambda: library.verify_provider_ids(p))
check("matching identity keeps tmdb+tvdb",
      p.get("tmdb_id") == 83135 and p.get("tvdb_id") == "362579"
      and not p.get("_id_rejections"))

# 2. The Too Cute id (wrong show, wrong year) is stripped, tvdb with it.
p = plan(tmdb_id=80979, tvdb_id="325542")
reasons = with_ident(TOO_CUTE, lambda: library.verify_provider_ids(p))
check("wrong-show tmdb id is stripped",
      "tmdb_id" not in p and "tvdb_id" not in p)
check("the strip is recorded with the provider's own title",
      reasons and "Too Cute" in p["_id_rejections"][0]
      or reasons and "萌宠成长记" in p["_id_rejections"][0])

# 3. 404 (dead id) is a mismatch.
p = plan(tmdb_id=99999999, tvdb_id="362579")
with_ident({"dead": True}, lambda: library.verify_provider_ids(p))
check("dead tmdb id is stripped", "tmdb_id" not in p and "tvdb_id" not in p)

# 4. tvdb lone disagreement is stripped, tmdb kept.
p = plan(tvdb_id="325542")
with_ident(IDENT, lambda: library.verify_provider_ids(p))
check("contradicted tvdb id is stripped, tmdb kept",
      p.get("tmdb_id") == 83135 and "tvdb_id" not in p)

# 5. Network error -> fail open, id kept, nothing recorded.
p = plan()
with_ident(None, lambda: library.verify_provider_ids(p))
check("transport error keeps the id (fail open)",
      p.get("tmdb_id") == 83135 and p.get("tvdb_id") == "362579"
      and not p.get("_id_rejections"))

# 6. Romaji-vs-English, same year: no title overlap, years agree -> no strip.
ROMAJI = {"name": "Attack on Titan", "original_name": "進撃の巨人",
          "year": 2013, "first_air_date": "2013-04-07", "tvdb_id": "267440"}
p = plan(title="Shingeki no Kyojin", year=2013, tmdb_id=1429, tvdb_id="267440")
with_ident(ROMAJI, lambda: library.verify_provider_ids(p))
check("romaji title with agreeing year is not a false positive",
      p.get("tmdb_id") == 1429 and not p.get("_id_rejections"))

# 7. Off-by-one year is ordinary production-vs-premiere drift -> no strip.
OFF_BY_ONE = dict(IDENT, year=2018)
p = plan(tmdb_id=83135, tvdb_id="362579")
with_ident(OFF_BY_ONE, lambda: library.verify_provider_ids(p))
check("one-year drift keeps the id", p.get("tmdb_id") == 83135)

# 8. Movies are out of scope here (the movie-id guards own them).
p = {"media_type": "movie", "title": "Whatever", "year": 2000, "tmdb_id": 80979}
with_ident(TOO_CUTE, lambda: library.verify_provider_ids(p))
check("movie plans are untouched", p.get("tmdb_id") == 80979)

# 9. Idempotent: a second pass over the stripped plan changes nothing.
p = plan(tmdb_id=80979, tvdb_id="325542")
with_ident(TOO_CUTE, lambda: library.verify_provider_ids(p))
before = list(p["_id_rejections"])
with_ident(TOO_CUTE, lambda: library.verify_provider_ids(p))
check("stripping is idempotent", p["_id_rejections"] == before)

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
