#!/usr/bin/env python3
"""The AI budget policy: may auxiliary AI work spend right now?

The answer decides whether an auxiliary caller drains the accounts identify needs. The
policy lives once, in `ai_budget.py`, and the wrapper around it reads the live cap stamps.
A gate whose input no longer exists reads as permanently healthy, so both are asserted:
the policy on synthetic inputs covering every branch, and the live wrapper on today's
state -- today's state only ever exercises one path, which is why the synthetic half
exists.

This test used to assert that Torrent-Ingest and Torrent-Searcher agreed, because a
wrapper is exactly where two repos drift apart while both look healthy. Discovery was
removed on 2026-09-10 and there is only one wrapper now, so the cross-repo half is gone --
`ai_budget.py` has a single caller and cannot disagree with itself.

Both directions, per §4.5: the reservation must be able to FIRE (identify's providers are
the only uncapped ones and work is waiting) and to STAND DOWN (anything else). A policy
that always says "healthy" would pass a test that only ever checked for healthy.

Read-only. It sends nothing and writes nothing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DEV = Path.home() / "Developer"
sys.path.insert(0, str(DEV / "Torrent-Ingest"))

import ai_budget                                                     # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        FAILURES.append(label)


# --- 1. the policy itself, every branch, both directions ---------------------

def test_policy() -> None:
    print("policy branches")
    OR, CF, GQ = "openrouter", "cloudflare", "groq"
    ALL = {OR, CF, GQ}
    CAPABLE = {OR, CF}          # groq's ceiling is below the floor identify prompt

    def ev(capped, pending):
        return ai_budget.evaluate(configured=ALL, capped=set(capped),
                                  capable=CAPABLE, identify_pending=pending)

    # THE RESERVATION MUST BE ABLE TO FIRE. This is the case the whole module exists for:
    # identify's providers are back, nothing else is available, and work is waiting.
    v = ev({GQ}, 4)
    check("reserved when only identify's providers are up and work waits", v.healthy, False)
    check("...and it is reported as a reservation, not a cap", v.reserved_for_identify, True)
    check("...and it names the backlog", "4 download(s)" in v.why, True)

    # ...AND IT MUST STAND DOWN EVERYWHERE ELSE.
    v = ev({GQ}, 0)
    check("not reserved when identify has no backlog", v.healthy, True)
    check("...and that is not a reservation", v.reserved_for_identify, False)

    v = ev({OR, CF}, 4)
    check("healthy when a provider identify cannot use is free", v.healthy, True)
    check("...even with a backlog", v.reserved_for_identify, False)

    v = ev(ALL, 4)
    check("unhealthy when everything is capped", v.healthy, False)
    check("...and that is NOT a reservation (it is a real cap)",
          v.reserved_for_identify, False)

    v = ai_budget.evaluate(configured=set(), capped=set(), capable=set(),
                           identify_pending=99)
    check("no providers configured fails OPEN", v.healthy, True)

    print("\nchain ordering")
    attempts = [{"provider": OR, "model": "a"}, {"provider": GQ, "model": "b"},
                {"provider": CF, "model": "c"}]
    got = [a["provider"] for a in
           ai_budget.order_attempts(attempts, capable=CAPABLE, identify_pending=4)]
    check("with a backlog, identify's providers are withheld", got, [GQ])
    got = [a["provider"] for a in
           ai_budget.order_attempts(attempts, capable=CAPABLE, identify_pending=0)]
    check("with no backlog, groq is still preferred first", got, [GQ, OR, CF])
    check("...and nothing is dropped", sorted(got), sorted([OR, GQ, CF]))

    print("\ncapability from measured ceilings")
    check("a provider below the floor is not capable",
          ai_budget.identify_capable({GQ: 26367}, 58513, ALL), {OR, CF})
    check("a provider above the floor is capable",
          ai_budget.identify_capable({GQ: 99999}, 58513, ALL), ALL)
    check("an unmeasured provider is assumed capable (fails open)",
          ai_budget.identify_capable({}, 58513, ALL), ALL)
    check("with no floor measured, only a stated ceiling disqualifies",
          ai_budget.identify_capable({GQ: 26367}, None, ALL), {OR, CF})


# --- 2. the two wrappers agree on live inputs --------------------------------

_PROBE = r"""
import json, sys
sys.path.insert(0, {repo!r})
import config
v = config.aux_ai_verdict()
print(json.dumps({{
    "healthy": v.healthy,
    "reserved": v.reserved_for_identify,
    "why": v.why,
    "capable": sorted(config.identify_capable_providers()),
    "chain": [a["provider"] + "/" + a["model"] for a in config.aux_ai_attempts()],
}}))
"""


def _ask(repo: Path, python: str) -> dict:
    import json
    out = subprocess.run([python, "-c", _PROBE.format(repo=str(repo))],
                         cwd=str(repo), capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"{repo.name} probe failed:\n{out.stderr[-1500:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_wrapper_answers_on_live_inputs() -> None:
    """The wrapper must still reach a verdict off the real cap stamps.

    A wrapper reading a stamp nobody writes any more answers "healthy" forever, which is
    indistinguishable from a healthy fleet. So this asserts the live probe RUNS and every
    field comes back well-formed -- not what today's answer is, which changes hourly.
    """
    print("\nthe live wrapper, real cap stamps")
    py_ingest = "/opt/homebrew/Caskroom/miniconda/base/envs/torrent_ingest_env/bin/python3"
    if not Path(py_ingest).exists():
        py_ingest = sys.executable
    try:
        a = _ask(DEV / "Torrent-Ingest", py_ingest)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"  FAIL  could not probe the wrapper: {exc}")
        FAILURES.append("probe the wrapper")
        return

    check("'healthy' is a bool", isinstance(a["healthy"], bool), True)
    check("'reserved' is a bool", isinstance(a["reserved"], bool), True)
    check("'why' is a non-empty string", bool(a["why"]) and isinstance(a["why"], str), True)
    check("'capable' is a list", isinstance(a["capable"], list), True)
    check("'chain' is a non-empty list", isinstance(a["chain"], list) and len(a["chain"]) > 0, True)
    print(f"        (live: healthy={a['healthy']} capable={a['capable']} "
          f"chain={a['chain']})")


def main() -> int:
    test_policy()
    test_wrapper_answers_on_live_inputs()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("AI budget contract: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
