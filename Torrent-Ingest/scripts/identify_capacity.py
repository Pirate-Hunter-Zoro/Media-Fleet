#!/usr/bin/env python3
"""Can ANY free provider actually run an identify prompt right now? Read-only.

WHY THIS EXISTS. On 2026-09-05 fourteen Made in Abyss volumes, the Yu Yu Hakusho pack and
the Hunter x Hunter (1999) pack sat downloaded-but-unfiled for hours, and the only trace
was one line per record per cycle in `torrent_ingest.log` saying "identify API
unavailable". The fleet had no way to ask the question directly, so the cause -- two
providers out of daily budget and the third structurally unable to take a prompt this
size -- had to be reconstructed from log archaeology.

Two DIFFERENT things stop identify, and they need different answers:

  * OUT OF BUDGET (a daily cap) -- transient. It fixes itself at the provider's reset.
  * PROMPT TOO LARGE (a tokens-per-minute ceiling) -- permanent at this prompt size. No
    amount of waiting helps; the prompt has to shrink or the tier has to change.

The second is the one that hides, because it looks identical to the first in the log.
Groq's free tier enforces 8,000 TPM against an identify prompt of ~30,000 tokens, so it
can never serve one -- while answering a small `cull`/`media_doctor` call in five seconds.
"Groq is up" and "Groq can run identify" are not the same claim.

Read-only: it builds prompts and measures them. It sends nothing.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                       # noqa: E402
import identify                                                     # noqa: E402
import library                                                      # noqa: E402


def _prompt_chars(sections=None):
    """Size of a REAL identify prompt with an empty file listing -- the FLOOR a one-file
    torrent would actually send, so a provider that cannot take this can take nothing."""
    return identify.prompt_chars(sections)


def _probe(name):
    """One tiny live call PER MODEL: which of this provider's models answer right now?

    § diagnosis 4.178: a log line saying a component was "unavailable" tells you that ONE
    call failed; it is not a measurement of the provider's state, and the measurement costs
    one command. This is that command. Off by default because it spends a free request.

    Every model is probed, not just enough of them to reach a verdict. Stopping at the
    first answer is the §4.9 trap one level down -- "openrouter usable" was true of the
    PROVIDER and false of two of its five models, whose `:free` slugs OpenRouter had
    retired and which answered `404 unavailable for free` on every identify pass for days.
    The provider-level line read healthy throughout, and the dead models were only found
    by reading the daemon's log by hand. A per-model line makes that visible here instead.
    """
    import ai_client
    prov = config.ai_provider(name)
    if not prov:
        return "no key", []
    models = [a["model"] for a in config.enabled_ai_attempts() if a["provider"] == name]
    per_model, answered, last = [], False, "no model"
    for model in models:
        try:
            out = ai_client.run_agent("Reply with exactly: OK", allowed_tools=[],
                                      max_turns=1, model=model,
                                      base_url=prov["base_url"], key=prov["key"])
        except Exception as exc:                                        # noqa: BLE001
            last = str(exc)
            if config.identify_account_capped(last):
                per_model.append((model, "OUT OF DAILY BUDGET (said so just now)"))
                return "OUT OF DAILY BUDGET (provider said so just now)", per_model
            per_model.append((model, f"ERROR {last[:90]}"))
            continue
        if (out.get("result") or "").strip():
            per_model.append((model, "answers"))
            answered = True
        else:
            per_model.append((model, "returned empty text"))
    if answered:
        return None, per_model                             # answered: nothing to report
    return f"no model answered: {last[:120]}", per_model


def main() -> int:
    probe = "--probe" in sys.argv
    attempts = config.enabled_ai_attempts()
    if not attempts:
        print("identify capacity: NO PROVIDER CONFIGURED (no key under ~/.config/api-keys/)")
        return 1

    full = _prompt_chars()
    # The CONFIRM-mode floor (see `identify._confirm_prompt`). A provider whose ceiling
    # blocks the full prompt is not necessarily unable to file: when the harness has
    # already settled a release's arc->season mapping, the run is confirming an answer
    # rather than deriving one, and that prompt is a third the size. Reporting only the
    # full floor is what made groq read as permanently useless.
    confirm = identify.confirm_prompt_chars()
    # Also stamps the floor where the budget reservation can read it. This script is on
    # the read-only path a session and `verify_fleet.sh` both already run, so the stamp
    # stays fresh without a daemon whose only job is to refresh a number.
    narrow = identify.stamp_identify_floor()
    print(f"smallest real identify prompt: {narrow} chars (~{int(narrow / config.IDENTIFY_CHARS_PER_TOKEN)} tokens, "
          f"narrowed digest)")
    print(f"typical identify prompt:       {full} chars (~{int(full / config.IDENTIFY_CHARS_PER_TOKEN)} tokens, "
          f"full digest)")
    print(f"smallest CONFIRM-mode prompt:  {confirm} chars "
          f"(~{int(confirm / config.IDENTIFY_CHARS_PER_TOKEN)} tokens; used when the "
          f"harness has settled the release's arc->season mapping)")
    capped = config.capped_ai_providers()
    ceilings = config.load_prompt_ceilings()
    print()

    usable, confirm_usable = [], []
    for name in sorted({a["provider"] for a in attempts}):
        ceiling = ceilings.get(name)
        bits = []
        live, per_model = _probe(name) if probe else (None, [])
        if live:
            bits.append(live)
        elif probe:
            # The provider ANSWERED. That outranks any cap stamp, which is a record of
            # something that was true when it was written -- OpenRouter's daily quota
            # resets at 00:00 UTC while the stamp's backoff runs a flat two hours, so for
            # up to two hours after a reset the stamp says "capped" about a provider that
            # is demonstrably serving. Reporting the stamp over a live answer would make
            # this tool lie in exactly the way it exists to prevent, so the stale stamp is
            # cleared rather than believed.
            if name in capped:
                try:
                    config._budget_stamp(name).unlink(missing_ok=True)
                except OSError:
                    pass
                capped.discard(name)
                bits.append("usable (cleared a stale cap stamp -- it answered just now)")
        elif name in capped:
            bits.append("OUT OF DAILY BUDGET (cap stamp)")
        confirm_only = False
        if ceiling is not None and ceiling < narrow:
            if ceiling >= confirm:
                confirm_only = True
                bits.append(f"prompt ceiling {ceiling} chars: too small for a full "
                            f"identify prompt, big enough for a CONFIRM-mode one -- it "
                            f"can file a release whose arcs the harness has settled")
            else:
                bits.append(f"prompt ceiling {ceiling} chars < the smallest prompt we "
                            f"can build")
        elif ceiling is not None:
            bits.append(f"prompt ceiling {ceiling} chars")
        blocked = bool(live) or name in capped or (
            ceiling is not None and ceiling < narrow and not confirm_only)
        if not bits:
            bits.append("usable")
        if not blocked:
            (confirm_usable if confirm_only else usable).append(name)
        print(f"  {name:12s} {'; '.join(bits)}")
        # A dead model inside a live provider is invisible in the line above, and that is
        # the shape that cost six days. Print every model that is not plainly answering.
        for model, state in per_model:
            if state != "answers":
                print(f"    {'':10s} {model}: {state}")

    deferred = identify._budget_deferred_for()
    if deferred:
        print(f"\n  identify is currently DEFERRED for {int(deferred)}s "
              f"(every provider reported no budget)")

    print()
    if usable:
        extra = (f"; plus {', '.join(confirm_usable)} for a settled arc mapping"
                 if confirm_usable else "")
        print(f"identify capacity: OK ({len(usable)} provider(s) can serve a prompt: "
              f"{', '.join(usable)}{extra})")
        return 0
    if confirm_usable:
        print(f"identify capacity: PARTIAL -- {', '.join(confirm_usable)} can only serve "
              f"a CONFIRM-mode prompt, so a release whose arc->season mapping the harness "
              f"has settled WILL file and anything else will sit UNFILED until a daily "
              f"cap resets.")
        return 0
    print("identify capacity: NONE -- downloads will finish and sit UNFILED. "
          "A daily cap clears itself at the provider's reset; a prompt ceiling does not.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
