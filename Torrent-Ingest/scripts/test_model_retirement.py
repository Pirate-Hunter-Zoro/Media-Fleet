#!/usr/bin/env python3
"""Model ids rot; the chain must heal itself. Read-only, no network.

    python3 scripts/test_model_retirement.py

WHAT IS BEING PROVED

  1. A RETIRED model is told apart from a busy, rate-limited or out-of-budget one. This is
     the whole safety property: a 503 under load and a 429 daily cap leave the model
     perfectly valid tomorrow, and switching models because of one would be the fleet
     quietly drifting off the ids a human chose.
  2. A replacement is the same FAMILY, never a non-chat model, never a downgrade, and
     never another vendor's model that merely shares a digit.
  3. A candidate is adopted only after a real tool-calling probe succeeds.
  4. The overlay is what the chain reads, and `config.py` is never rewritten.

Every case below is a real string a provider actually returned on 2026-09-12, or a real
model list read from a provider's own `/v1/models` that day.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_models                                                       # noqa: E402
import config                                                          # noqa: E402

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


# Real `/v1/models` output, trimmed. Google's shim returns a `models/` prefix; NVIDIA's
# list is mostly things that are not chat models at all.
GEMINI = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite",
          "gemini-3-flash-preview", "gemini-3.1-pro-preview", "gemini-3.5-flash",
          "gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.7-flash",
          "gemini-3.8-flash", "gemini-embedding-001", "lyria-3-pro-preview",
          "gemini-2.5-flash-native-audio-latest", "nano-banana-pro-preview"]
NVIDIA = ["nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-ultra-550b-a55b",
          "nvidia/embed-qa-4", "nvidia/llama-3.1-nemoguard-8b-content-safety",
          "moonshotai/kimi-k3", "moonshotai/kimi-k2.6", "openai/gpt-oss-20b",
          "meta/llama-3.2-90b-vision-instruct", "nvidia/nemotron-parse",
          "nvidia/nemotron-4-340b-reward", "nvidia/riva-translate-4b-instruct",
          "z-ai/glm-5.3-flash"]

print("Part 1 -- retired vs merely unavailable (real provider strings)")
RETIRED = [
    ('http 410: {"detail":"The model \'meta/llama-3.3-70b-instruct\' has reached its '
     'end of life on 2026-08-26T09:00:00Z"}'),
    'http 404: [{"error": {"code": 404, "message": "This model models/gemini-2.0-flash '
    'is no longer available."}}]',
    'http 404 "This model is unavailable for free. The paid version is available now"',
]
NOT_RETIRED = [
    'http 503: [{"error": {"code": 503, "message": "This model is currently '
    'experiencing high demand."}}]',
    'http 429 rate limit: Rate limit reached ... on tokens per day (TPD): Limit 200000',
    'http 429: {"errors":[{"message":"you have used up your daily free allocation of '
    '10,000 neurons"}]}',
    'http 413: Request too large ... on tokens per minute (TPM): Limit 8000',
    'connection error: <urlopen error [Errno 61] Connection refused>',
]
for d in RETIRED:
    check(f"RETIRED: {d[:52]}...", ai_models.is_retired(d), True)
for d in NOT_RETIRED:
    check(f"not retired: {d[:52]}...", ai_models.is_retired(d), False)

print("\nPart 2 -- a replacement is a drop-in, not just any live id")
check("newest full-size sibling wins",
      ai_models.rank_replacements("gemini-3.5-flash", GEMINI)[0], "gemini-3.8-flash")
check("a -lite sibling never outranks a full-size one",
      ai_models.rank_replacements("gemini-3.5-flash", GEMINI).index("gemini-3.8-flash")
      < ai_models.rank_replacements("gemini-3.5-flash", GEMINI).index("gemini-3.5-flash-lite"),
      True)
ranked = ai_models.rank_replacements("gemini-3.5-flash", GEMINI)
check("no embedding / audio / image model is ever offered",
      [m for m in ranked if any(b in m for b in ("embedding", "audio", "banana", "lyria"))],
      [])
check("same vendor family only -- a shared DIGIT is not a family",
      "z-ai/glm-5.3-flash" in
      ai_models.rank_replacements("nvidia/nemotron-3-super-120b-a12b", NVIDIA), False)
check("nemotron -> the other nemotron",
      ai_models.rank_replacements("nvidia/nemotron-3-super-120b-a12b", NVIDIA)[0],
      "nvidia/nemotron-3-ultra-550b-a55b")
check("kimi-k3 -> kimi-k2.6",
      ai_models.rank_replacements("moonshotai/kimi-k3", NVIDIA)[0], "moonshotai/kimi-k2.6")
check("no guard / parse / reward / vision / translate model survives",
      [m for m in ai_models.rank_replacements("nvidia/nemotron-3-super-120b-a12b", NVIDIA)
       if any(b in m for b in ("guard", "parse", "reward", "vision", "translate", "embed"))],
      [])
check("a retired model is never its own replacement",
      "gemini-3.5-flash" in ranked, False)

print("\nPart 3 -- nothing is adopted until a real tool-calling probe passes")
tmp = Path(tempfile.mkdtemp())
real_file = ai_models.OVERRIDES_FILE
try:
    ai_models.OVERRIDES_FILE = tmp / "ai_model_overrides.json"
    tried = []

    def probe_all_fail(_p, m):
        tried.append(m)
        return False

    got = ai_models.find_replacement("gemini", "gemini-3.5-flash",
                                     probe=probe_all_fail, lister=lambda _p: GEMINI)
    check("every candidate refusing the probe adopts NOTHING", got, None)
    check("and the search is bounded, not the whole catalogue", len(tried) <= 3, True)
    check("nothing was written to the overlay",
          ai_models.OVERRIDES_FILE.exists(), False)

    tried.clear()

    def probe_second_ok(_p, m):
        tried.append(m)
        return m == "gemini-3.7-flash"

    got = ai_models.find_replacement("gemini", "gemini-3.5-flash",
                                     probe=probe_second_ok, lister=lambda _p: GEMINI)
    check("the first candidate that PASSES the probe is adopted", got, "gemini-3.7-flash")
    check("the overlay records it",
          json.loads(ai_models.OVERRIDES_FILE.read_text())["gemini"]["gemini-3.5-flash"]["to"],
          "gemini-3.7-flash")
    check("a provider that cannot be listed adopts nothing",
          ai_models.find_replacement("gemini", "x", probe=probe_second_ok,
                                     lister=lambda _p: None), None)

    print("\nPart 4 -- the overlay is what the chain reads")
    over = ai_models.load_overrides()
    check("load_overrides returns the substitution",
          over.get("gemini", {}).get("gemini-3.5-flash"), "gemini-3.7-flash")
    stale = json.loads(ai_models.OVERRIDES_FILE.read_text())
    stale["gemini"]["gemini-3.5-flash"]["at"] = 0          # older than any TTL
    ai_models.OVERRIDES_FILE.write_text(json.dumps(stale))
    check("an override past its TTL is dropped, so the question is re-asked",
          ai_models.load_overrides().get("gemini", {}).get("gemini-3.5-flash"), None)
finally:
    ai_models.OVERRIDES_FILE = real_file

print("\nPart 4b -- a BUSY provider is skipped, a blip is retried")
# The chain exists so a bad provider is skipped, and a busy one has to be skipped too:
# each caller-level retry is a WHOLE fresh agent run, so retrying a 503 throws away every
# turn already spent. Measured 2026-09-12: two gemini models x four attempts x ~6 minutes
# each = ~48 minutes before the chain reached OpenRouter, while five funded providers idled.
# But the distinction must stay narrow -- a reset connection IS worth retrying in place.
BUSY = [
    'gemini/gemini-3.5-flash: RuntimeError: http 503: [{"error": {"code": 503, '
    '"message": "This model is currently experiencing high demand."}}]',
    "http 502: bad gateway",
    "http 529: overloaded",
    "the service is temporarily unavailable",
]
NOT_BUSY = [
    "connection error: [Errno 54] Connection reset by peer",
    "connection closed mid-stream",
    "identify run exited 1: connection error: <urlopen error timed out>",
    'http 429 rate limit: ... on tokens per day (TPD): Limit 200000',
    'http 410: the model has reached its end of life',
]
for d in BUSY:
    check(f"BUSY -> next provider: {d[:46]}...", config.identify_provider_busy(d), True)
for d in NOT_BUSY:
    check(f"not busy -> keep retries: {d[:44]}...", config.identify_provider_busy(d), False)
# Busy and retired are disjoint: a 503 must never trigger a model swap, and a 410 must
# never be mistaken for load.
check("no string is both BUSY and RETIRED",
      [d for d in BUSY + NOT_BUSY
       if config.identify_provider_busy(d) and ai_models.is_retired(d)], [])

print("\nPart 4c -- a rejection outlives the chain walk that produced it")
# `validate_plan` rejects WHOLESALE -- 41 raise-sites, first one wins -- so a plan that
# placed 31 of 32 files correctly is discarded in full. The plan is NOT re-applied (that is
# how a bad placement becomes the premise for the next one), but the lesson must survive:
# the rejection list used to be a local that died when the chain ran out of providers, so
# the next cycle began knowing nothing. Monogatari was mis-filed three times that way.
import identify                                                        # noqa: E402
real_rej = identify.REJECTIONS_FILE
try:
    identify.REJECTIONS_FILE = tmp / "identify_rejections.json"
    W = "hash-w0"
    check("nothing remembered for a fresh wave", identify._load_rejections(W), [])
    identify._save_rejections(W, [{"provider": "gemini", "model": "m1",
                                   "error": "one arc split across two seasons: ...",
                                   "plan": {"files": []}}])
    got = identify._load_rejections(W)
    check("a rejection survives the walk", len(got), 1)
    check("and carries the reason the next model needs",
          got[0]["error"].startswith("one arc split"), True)

    # The same reason from three providers is ONE lesson, not three prompts' worth.
    for prov in ("openrouter", "nvidia", "mistral"):
        identify._save_rejections(W, [{"provider": prov, "model": "m",
                                       "error": "one arc split across two seasons: ...",
                                       "plan": None}])
    check("duplicate reasons are one lesson", len(identify._load_rejections(W)), 1)

    identify._save_rejections(W, [{"provider": "nvidia", "model": "m2",
                                   "error": "season over-filled: ...", "plan": None}])
    check("a DIFFERENT reason is kept too", len(identify._load_rejections(W)), 2)
    check("but the count is capped for prompt size",
          len(identify._load_rejections(W)) <= identify.REJECTIONS_KEPT, True)

    # A rejection is a fact about a plan judged by a SPECIFIC set of guards, and guards
    # change -- two were added on 2026-09-12. Stale advice is worse than none.
    import json as _j
    raw = _j.loads(identify.REJECTIONS_FILE.read_text())
    for r in raw[W]:
        r["at"] = 0
    identify.REJECTIONS_FILE.write_text(_j.dumps(raw))
    check("a rejection past its TTL is forgotten", identify._load_rejections(W), [])

    identify._save_rejections(W, [{"provider": "g", "model": "m", "error": "e",
                                   "plan": None}])
    check("a wave that finally lands forgets its lessons",
          (identify._clear_rejections(W), identify._load_rejections(W))[1], [])
    check("one wave's lessons never leak into another's",
          identify._load_rejections("other-w0"), [])
finally:
    identify.REJECTIONS_FILE = real_rej

print("\nPart 5 -- config.py is the declared preference and is never rewritten")
src = (Path(__file__).resolve().parent.parent / "config.py").read_text()
check("nothing in the fleet writes config.py",
      any(bad in src for bad in ('config.py").write_text', "config.py').write_text")), False)
# Prose is not code. `ai_models.py` mentions ship-fleet in its docstring precisely to say
# it does NOT run it, and an earlier draft of this check failed on that sentence -- the
# same trap `audit_free_only._strip_prose` exists for.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_free_only import _strip_prose                              # noqa: E402
code = _strip_prose((Path(__file__).resolve().parent.parent / "ai_models.py").read_text())
check("ai_models never shells out to the deploy script",
      any(bad in code for bad in ("ship-fleet", "subprocess", "os.system")), False)
check("and it never writes a .py file",
      any(bad in code for bad in (".py\").write_text", ".py').write_text")), False)
check("the chain still builds", len(config.enabled_ai_attempts()) > 0, True)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
