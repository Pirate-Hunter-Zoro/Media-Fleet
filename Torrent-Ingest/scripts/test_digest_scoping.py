#!/usr/bin/env python3
"""Regression test for relevance-scoped library digests (2026-09-10).

The whole-library shows digest is ~32,000 characters -- about 300 folders with their season
lists. Every identify run paid for all of it, so placing one anime pack shipped The Office's
season breakdown to a provider on a DAILY budget, and buried the few lines that mattered in
three hundred that did not.

Scoping keeps the full line for shows whose name shares a distinctive word with the
download, and lists every other folder BY NAME so nothing becomes invisible.

BOTH DIRECTIONS (§4.5), and the second is the one that protects correctness:
  Part 1 -- a matching hint really does shrink the digest, and keeps the matched show.
  Part 2 -- it FAILS OPEN: no hint, or a hint matching nothing, yields the FULL digest
            byte-for-byte. A bad guess must cost tokens, never a wrong placement.
  Part 3 -- every show folder is still named, even when its detail is dropped.

    python3 scripts/test_digest_scoping.py

Read-only against the live library.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import library                                                       # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def digest(hint):
    return library.build_library_digest(None, None, sections=("shows",), title_hint=hint)


full = digest(None)
shows = sorted(p.name for p in config.SHOWS_ROOT.iterdir() if p.is_dir()) \
    if config.SHOWS_ROOT.exists() else []

print(f"  library has {len(shows)} show folder(s); full shows digest is {len(full):,} chars")
check("the corpus is real", len(shows) > 50 and len(full) > 5000, True)

print("\nPart 1 -- a matching hint shrinks the digest and keeps its match")
# pick a real show with a distinctive word to avoid depending on any one title existing
target = next((n for n in shows if "Powerpuff" in n), shows[0] if shows else "")
scoped = digest(target)
check(f"scoping on {target[:34]!r} shrinks it", len(scoped) < len(full), True)
check("the matched show keeps its full detail line",
      any(l.strip().startswith(f"- {target}") for l in scoped.splitlines()), True)

print("\nPart 2 -- NO HINT still means the full digest, unchanged")
check("no hint -> byte-identical to the full digest", digest(None), full)
check("empty hint -> byte-identical", digest(""), full)
check("pure release noise tokenises to nothing -> byte-identical",
      digest("[Group] 1080p BluRay x265 Dual Audio"), full)

print("\nPart 2b -- a hint matching NOTHING is names-only, and says so")
# This is the case a NEW show hits, where the prompt is already at its largest. No library
# show shares a distinctive word with the release, so no show's season breakdown can be the
# one being extended. Names stay; detail goes; the run is told plainly what it is looking at.
none_match = digest("zzqqxx-no-such-show-zzqqxx")
check("it is smaller than the full digest", len(none_match) < len(full), True)
check("it says the show looks NEW", "looks like a NEW show" in none_match, True)
check("it tells the run to ListDir a name that looks like the match",
      "ListDir/Read rather" in none_match, True)
check("no show keeps a full detail line",
      any(l.strip().startswith("- ") and ":: " in l for l in none_match.splitlines()),
      False)

print("\nPart 3 -- nothing in the library becomes invisible, in EITHER mode")
missing_nm = [n for n in shows if n not in none_match]
if missing_nm:
    for n in missing_nm[:5]:
        print(f"        not named in the no-match digest: {n}")
check(f"all {len(shows)} folders are named when nothing matched", missing_nm, [])
missing = [n for n in shows if n not in scoped]
if missing:
    for n in missing[:10]:
        print(f"        not named anywhere in the scoped digest: {n}")
check(f"all {len(shows)} folders are still named in the scoped digest", missing, [])

print("\nPart 4 -- the tokeniser ignores release noise but keeps real words")
check("release noise and generic nouns tokenise to nothing",
      library._digest_tokens("Show S01 1080p BluRay Season Complete"), set())
check("a distinctive word survives the noise around it",
      library._digest_tokens("Frieren S01 1080p BluRay Complete"), {"frieren"})
check("a real multiword title tokenises",
      library._digest_tokens("[MTBB] Monogatari Series (BD 1080p)"), {"monogatari"})
check("short words are ignored", library._digest_tokens("Up Dr No"), set())

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
