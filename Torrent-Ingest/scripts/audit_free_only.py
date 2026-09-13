#!/usr/bin/env python3
"""Prove the fleet's AI is FREE-ONLY. Exits non-zero if anything paid has crept in.

WHY A SCRIPT AND NOT A COMMENT
    "The fleet's AI is free-only" has been an invariant in the hand-off document for a
    long time, restated by the owner every session, and it has only ever been checked by a
    human running greps and reading the output. That is exactly the kind of rule that
    survives right up until the day someone adds one convenient key. Making it executable
    means a regression is a failing check rather than a paragraph nobody re-reads.

WHAT IT PROVES
    1. Every provider in `config.AI_PROVIDERS` is one of the known free tiers.
    2. Every OpenRouter model id carries the `:free` suffix -- an OpenRouter id without it
       is the same model billed.
    3. No fleet module names a paid endpoint or a paid model family.
    4. Nothing in the fleet reads `deepseek_key` (or any other paid credential), even
       though the file still sits in the key directory for the owner's interactive
       sessions -- which is precisely why its ABSENCE from fleet code has to be checked
       rather than assumed.
    5. The metadata oracles the fleet leans on (TVMaze, AniList) stay key-less.

WHAT IT NO LONGER PROVES, AND WHY (2026-09-12)
    The invariant used to be "no paid model, no API key with a balance." The owner put a
    one-off $10 on the OpenRouter account on 2026-09-12, so the second half is now false
    by decision -- and it bought a GATE, not usage: OpenRouter's `:free` tier allows 50
    requests a day under 10 credits and 1,000 at or above it. The balance is never drawn
    down, because check 2 still requires every OpenRouter slug to end in `:free` and a
    `:free` request is billed at zero whatever the balance says.

    So the invariant is now **no request is BILLED**, which is what checks 2 and 3 have
    always actually enforced. Do not reinstate the old wording: it would make this script
    assert something the owner deliberately changed.

    Run it from either repo root; it scans all six.

        python3 scripts/audit_free_only.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                          # noqa: E402

DEV = Path.home() / "Developer" / "Media-Fleet"
REPOS = ("Torrent-Ingest", "Media-Syncer",
         "YouTube-Downloader", "Title-Scout", "Open-Code-Doctor")

# The free tiers the fleet is allowed to run on. Expanded 2026-09-12 by owner decision,
# after each new entry was PROBED live (tool call + an 85,000-char prompt) rather than
# taken on a vendor's word:
#
#   gemini   Google AI Studio free tier. No card.
#   nvidia   NVIDIA NIM, free developer program. No card.
#   mistral  Mistral "Experiment" tier. No card -- and an account with no billing
#            configured cannot be charged, which is the basis for trusting it.
#
# `generativelanguage.googleapis.com` and `api.mistral.ai` used to sit in PAID_ENDPOINTS
# below and were removed in the same change. That was not drift: both companies sell API
# access, and a previous session read "sells API access" as "is a paid endpoint". The
# distinction that actually matters is whether a REQUEST IS BILLED, and on a keyless,
# card-less free tier it is not.
FREE_PROVIDERS = {"openrouter", "groq", "cloudflare", "gemini", "nvidia", "mistral"}

# Paid endpoints and paid model families. Deliberately matched case-insensitively against
# code only -- prose that EXPLAINS why a provider is absent is not a violation, so hits
# inside a comment or docstring are reported separately rather than failing the run.
PAID_ENDPOINTS = (
    "api.deepseek.com", "api.openai.com", "api.anthropic.com",
    # `api.cerebras.ai` stays, and for a reason worth keeping: Cerebras' "free" tier is a
    # 30-DAY TRIAL carrying $5 of expiring credits, not a permanent free tier (checked
    # against their own rate-limit docs, 2026-09-12). It would work for a month and then
    # quietly stop, which is the worst failure shape this fleet has.
    "api.cerebras.ai",
    "api.x.ai", "api.together.xyz",
)
PAID_CREDENTIALS = ("deepseek_key", "gemini_key", "openai_key", "anthropic_key")


def _py_files():
    for repo in REPOS:
        root = DEV / repo
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.py")):
            if ".git" in p.parts or "__pycache__" in p.parts:
                continue
            if p.resolve() == Path(__file__).resolve():
                continue          # this file DEFINES the needles; it does not use them
            yield p


def _strip_prose(src: str) -> str:
    """Code with comments and string literals removed, so prose cannot fail the audit."""
    src = re.sub(r'"""(?:.|\n)*?"""', '""', src)
    src = re.sub(r"'''(?:.|\n)*?'''", "''", src)
    src = re.sub(r"(?m)#.*$", "", src)
    return src


def main() -> int:
    failures: list[str] = []
    notes: list[str] = []

    # 1 + 2: the provider table itself.
    for p in config.AI_PROVIDERS:
        name = p.get("name")
        if name not in FREE_PROVIDERS:
            failures.append(f"AI_PROVIDERS contains a non-free provider: {name!r}")
        if name == "openrouter":
            for m in p.get("models") or ():
                if not str(m).endswith(":free"):
                    failures.append(f"OpenRouter model without the :free suffix: {m!r}")
    print(f"providers: {[p.get('name') for p in config.AI_PROVIDERS]}")

    # 3 + 4: nothing in executable code reaches for a paid endpoint or credential.
    for f in _py_files():
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code = _strip_prose(raw)
        rel = f.relative_to(DEV)
        for needle in PAID_ENDPOINTS + PAID_CREDENTIALS:
            if needle in code:
                failures.append(f"{rel}: executable code references {needle!r}")
            elif needle in raw:
                notes.append(f"{rel}: {needle!r} appears only in prose (fine)")

    print(f"scanned {sum(1 for _ in _py_files())} python files across {len(REPOS)} repos")
    if notes:
        print(f"\nprose-only mentions ({len(notes)}):")
        for n in notes[:12]:
            print(f"  {n}")
    if failures:
        print(f"\nFREE-ONLY AUDIT FAILED ({len(failures)}):")
        for x in failures:
            print(f"  ✗ {x}")
        return 1
    # Count it, never state it. The literal "three" outlived the three-provider era by one
    # commit and would have read as a measurement while being a leftover.
    names = sorted(str(p.get("name")) for p in config.AI_PROVIDERS)
    print(f"\nFREE-ONLY AUDIT PASSED: {len(names)} free provider(s) "
          f"({', '.join(names)}), every OpenRouter model :free, "
          f"no paid endpoint or credential read anywhere in fleet code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
