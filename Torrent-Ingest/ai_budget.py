"""Who may spend the free AI budget, when identify and everything else want the same pool.

THE FAULT THIS FIXES, measured 2026-09-07
------------------------------------------
`config.ai_budget_healthy()` answers "is ANY configured provider uncapped?" That was
right when every provider could run every job. It stopped being right the moment one
provider turned out to be structurally unable to run identify:

    measured ceilings          groq 26,367 chars
    floor identify prompt      89,220 chars      (the smallest one that can be built)
    capped right now           openrouter, cloudflare
    ai_budget_healthy()        True

So the fleet reads "healthy" while BOTH providers identify can use are out, because groq
is up -- and groq can never serve identify. Auxiliary work (the searcher's cull, the
completeness and continuation audits, media_doctor, the playlist curator) then keeps
spending, and at the daily reset it races identify for the same two accounts. One sweep
of the searcher was measured at 2,212 cull calls, so it does not merely compete: it wins.
The visible symptom is downloads that finish and sit UNFILED, which is the state the
fleet has been in.

This is §4.9 one level down. "The provider is up", "the budget is healthy" and "identify
can run" are three different claims, and the first two were standing in for the third.

THE POLICY
----------
Auxiliary work yields ONLY in the one case where it would actually take food off
identify's plate -- when every uncapped provider is one identify needs AND identify has
work waiting. Everywhere else it spends freely, because the documented cost of being
wrong in that direction is some wasted free requests, while being wrong the other way
silently stops the library expanding itself.

WHY THIS MODULE IMPORTS NOTHING
-------------------------------
Torrent-Ingest and the (now removed) Torrent-Searcher both shipped a `config.py`, and a module one repo
path-loads must never do a bare `import config` -- that resolves to whichever repo
reached `sys.path` first, which is how the ingest daemon once died at import. Both repos
need this policy, and a policy duplicated in two repos is the divergence the searcher's
own config comments call "exactly the silent failure this fleet keeps paying for". So the
policy lives here once, as pure functions over explicit arguments, and each repo's
`config.py` supplies its own paths. `scripts/test_ai_budget_contract.py` asserts the two
wrappers still agree, so the drift is caught by the build rather than by a dark month.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    """Whether auxiliary AI work may spend, and the measured reason.

    `why` is load-bearing, not decoration: a stand-down that cannot say WHICH providers
    were capped and HOW MUCH was waiting is indistinguishable from a bug, and the fleet
    has twice concluded the wrong thing from a status line that stated no measurement.
    """

    healthy: bool
    why: str
    #: True only in the reservation case, so callers can say "yielding to identify"
    #: rather than the misleading "out of budget" -- they are different facts.
    reserved_for_identify: bool = False


def identify_capable(ceilings: dict, floor_chars, providers) -> set:
    """The providers that could run an identify prompt if they had budget.

    A provider with NO measured ceiling is treated as capable. That is the fail-open
    direction on purpose: an unmeasured provider has never refused a prompt for size, and
    assuming it incapable would reserve nothing and quietly restore the old bug. A ceiling
    only ever appears here because the provider itself stated one.

    `floor_chars` may be None, which happens when identify has not yet measured the
    smallest prompt it can build. The comparison then degrades to the weaker but still
    sound proxy: a provider that has EVER stated a size ceiling has refused an identify
    prompt for being too large, so it is not capable. This keeps the reservation working
    off measurements only -- it never guesses a provider incapable, and it never guesses
    one capable either.
    """
    capable = set()
    for name in providers:
        ceiling = ceilings.get(name)
        if ceiling is None:
            capable.add(name)
        elif floor_chars is not None and int(ceiling) >= int(floor_chars):
            capable.add(name)
    return capable


def evaluate(*, configured, capped, capable, identify_pending: int) -> Verdict:
    """The whole policy. Pure: same inputs, same verdict, in either repo.

    `configured` all providers with a usable key
    `capped`     those whose cap stamp is still inside the backoff window
    `capable`    those that could run an identify prompt (see `identify_capable`)
    `identify_pending`  how many downloads are waiting to be filed
    """
    configured = set(configured)
    capped = set(capped)
    capable = set(capable)

    if not configured:
        return Verdict(True, "no AI provider is configured; nothing to ration")

    uncapped = configured - capped
    if not uncapped:
        return Verdict(False, "every configured provider "
                              f"({', '.join(sorted(configured))}) is out of budget")

    # A provider identify cannot use is free money: spending it cannot delay a filing.
    # The caller must actually be STEERED onto it, though -- see `order_attempts`, without
    # which this verdict is true and useless.
    spare = uncapped - capable
    if spare:
        return Verdict(True, f"{', '.join(sorted(spare))} is uncapped and identify "
                             f"cannot use it anyway, so spending it costs identify nothing")

    # Every uncapped provider is one identify depends on.
    if identify_pending <= 0:
        return Verdict(True, f"the only uncapped provider(s) ({', '.join(sorted(uncapped))}) "
                             f"are identify's, but identify has nothing waiting")

    return Verdict(
        False,
        f"yielding to identify: the only uncapped provider(s) "
        f"({', '.join(sorted(uncapped))}) are the ones identify needs, and "
        f"{identify_pending} download(s) are waiting to be filed",
        reserved_for_identify=True,
    )


def order_attempts(attempts, *, capable, identify_pending: int):
    """The provider chain auxiliary work should walk, given what identify needs.

    A boolean "may I spend?" is not enough, and shipping only that would have left the
    bug in place. The chain is walked IN ORDER and openrouter is first, so an auxiliary
    caller told "healthy" still spends openrouter -- identify's provider -- even at the
    moment groq is sitting uncapped and unused. The gate said yes and the caller spent the
    wrong account: the model proposes, the harness disposes, and here the harness is the
    ordering.

    So auxiliary work gets a REORDERED chain:

      * providers identify cannot use come FIRST -- spending them is free, in the exact
        sense that it cannot delay a single file being filed;
      * providers identify CAN use come last, and are removed entirely while identify has
        a backlog, because at that moment every request on one of them is a request
        identify does not get.

    Returns a possibly-empty list. Empty means auxiliary work must not run this pass --
    which the caller reports as "yielding to identify", never as "out of budget": one is
    a scheduling decision, the other is a resource fact, and the fleet has already been
    burned once by a status line that stated the wrong one.
    """
    capable = set(capable)
    free = [a for a in attempts if a.get("provider") not in capable]
    reserved = [a for a in attempts if a.get("provider") in capable]
    if identify_pending > 0:
        return free
    return free + reserved


def count_pending_identify(journal_path) -> int:
    """How many torrents have finished downloading and not yet been filed.

    Reads the journal file directly rather than importing `journal`, for the collision
    reason in the module docstring. The journal is append-only with one JSON object per
    line and LATER lines supersede earlier ones for the same `info_hash`, so a naive count
    of `"status": "downloaded"` lines would count every record that ever passed through
    that stage -- 963 completed torrents were all `downloaded` once. Collapse by info_hash
    first, then count; that is what `journal.load_records` does and what makes this number
    the same one the daemon acts on.

    Fails to ZERO on any read error. An unreadable journal must not reserve the budget
    forever: this gate's safe direction is to let auxiliary work continue.
    """
    import json

    latest: dict = {}
    try:
        with open(journal_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ih = rec.get("info_hash")
                if ih:
                    latest[ih] = rec.get("status")
    except OSError:
        return 0
    return sum(1 for status in latest.values() if status == "downloaded")


__all__ = ["Verdict", "identify_capable", "evaluate", "count_pending_identify"]
