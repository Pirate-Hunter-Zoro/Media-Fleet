#!/usr/bin/env python3
"""Regression test: identify is handed the provider's season shape (2026-09-10).

WHY. The fleet already holds this data. `epguide.season_shape(title)` returns
{season: episode count} and `epguide.episodes(title)` the per-episode names, cached on
disk and used by the metadata repair. identify never saw it -- so every run re-derived the
season layout by web search (twenty-odd turns of it on Monogatari) and STILL got a
boundary wrong:

    Season 03   04 - Nekomonogatari (Black)  ->  provider S03E01 'Tsubasa Tiger (1)'

The guide says Season 03 has 23 episodes beginning "Tsubasa Tiger - Part 1" -- which is
exactly the 23 files that release labels "Monogatari Series Second Season - 01..23". Both
the mistake and its answer were one lookup away.

BOTH DIRECTIONS (§4.5):
  Part 1 -- a title the guide knows produces a block with real season sizes.
  Part 2 -- it is BEST-EFFORT and silent: an unknown title, an empty path, or a guide that
            raises returns "" and the run proceeds exactly as it did before. A lookup that
            cannot help must never be able to break a placement.
  Part 3 -- the title guess strips release noise without eating real title words.

    python3 scripts/test_provider_season_block.py

Read-only; uses the on-disk guide cache.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import identify                                                      # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


print("Part 1 -- a known title yields real season sizes")
block = identify._provider_season_block("/x/[MTBB] Monogatari Series (BD 1080p)")
check("a block is produced", bool(block), True)
check("it names the show", "Monogatari Series" in block, True)
check("it lists per-season episode counts", "episode(s)" in block, True)
check("it carries each season's FIRST episode", "first = E01" in block, True)
check("it says not to re-derive it by web search",
      "do not re-derive this by web search" in block, True)
check("it tells the run what to do with an arc that matches no season",
      "special, a film, or an entry the provider carries separately" in block, True)

print("\nPart 2 -- best effort: it can never break a run")
check("an unknown title yields nothing",
      identify._provider_season_block("/x/[Grp] Zzqq No Such Show Xyzzy (BD 1080p)"), "")
check("an empty path yields nothing", identify._provider_season_block(""), "")
check("pure release noise yields nothing",
      identify._provider_season_block("/x/[Grp] (BD 1080p)"), "")


class _Boom:
    def season_shape(self, *a, **k):
        raise RuntimeError("guide exploded")

    def episodes(self, *a, **k):
        raise RuntimeError("guide exploded")


saved = sys.modules.get("epguide")
try:
    sys.modules["epguide"] = _Boom()
    check("a guide that RAISES yields nothing, not an exception",
          identify._provider_season_block("/x/[MTBB] Monogatari Series (BD 1080p)"), "")
finally:
    if saved is not None:
        sys.modules["epguide"] = saved
    else:
        sys.modules.pop("epguide", None)

print("\nPart 3 -- the title guess strips noise, not real words")
cases = [
    ("[MTBB] Monogatari Series (BD 1080p)", "Monogatari Series"),
    ("One Pace [1080p]", "One Pace"),
    ("[Judas] Frieren - Beyond Journey's End (BD 1080p)",
     "Frieren - Beyond Journey's End"),
]
for raw, want in cases:
    check(f"{raw[:38]!r}", identify._release_title_guess("/x/" + raw), want)
check("'Series' is kept (it is part of real titles)",
      "Series" in identify._release_title_guess("/x/[G] Monogatari Series (BD)"), True)
check("an empty name guesses nothing", identify._release_title_guess(""), "")

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
