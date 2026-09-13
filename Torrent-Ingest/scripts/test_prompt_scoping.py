#!/usr/bin/env python3
"""The identify base prompt may be SCOPED by media kind, never shortened by deletion.

WHY SCOPING AND NOT CUTTING. identify's smallest real prompt is 62,218 chars, of which
prompts/identify.md is 46,155 (74%). Both remaining providers are capped by DAILY budget
(OpenRouter 1,000 requests, Cloudflare 10,000 neurons), and Cloudflare's is consumption-
based, so a smaller prompt is directly more files filed per day. But the file has ZERO
verbatim repeated lines -- it is 695 lines of distinct engineered rules -- so every
character removed by editing is a rule removed, and a placement rule cannot be regression-
tested without spending the very budget the shrink is meant to save.

Scoping is the shrink that is not a cut. `_relevant_sections` already narrows the library
DIGEST to the kinds a download actually holds; the same signal narrows the base prompt. A
comics-only download cannot be One Pace; a video-only download has no .epub to route to
Google Drive. Nothing is deleted -- each rule still reaches every kind it governs.

THE ASSERTION THAT MATTERS is the last one: every line of the original prompt must survive
in at least one scoped variant. That is what makes this a scoping and not a quiet deletion,
and it is the check that fails if someone later adds a heading to _KIND_SCOPED_SECTIONS
whose rules nothing else covers.

And the fail-safe: when `_relevant_sections` cannot tell what a download holds it returns
None, and the answer to "I cannot verify this" is never to drop rules (§4.4) -- the full
prompt goes.

    python3 scripts/test_prompt_scoping.py

Read-only, no model call, no network. Exit 0 means every check passed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import identify  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    shown = got if len(repr(got)) <= 120 else f"<{len(repr(got))} chars>"
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}: {shown!r}")
    if not ok:
        failures.append(label)


BASE = config.IDENTIFY_PROMPT_FILE.read_text(encoding="utf-8")
scope = identify._scope_base_prompt

VIDEO = ("movies", "shows")
BOOKS = ("comics", "novels")

EPUB_MARK = "## Light novels / e-books"
ONEPACE_MARK = "## One Pace"
ALWAYS = ["## The prime directive", "## Naming conventions",
          "## The hard calls you exist to make", "## Output JSON schema"]

print("the fail-safe: an undetermined download gets the WHOLE prompt (§4.4)")
check("sections=None is unchanged", scope(BASE, None) == BASE, True)
check("sections=() is unchanged", scope(BASE, ()) == BASE, True)

print("\neach kind still receives every rule that governs it")
books = scope(BASE, BOOKS)
video = scope(BASE, VIDEO)
check("a book download keeps the .epub routing", EPUB_MARK in books, True)
check("a video download keeps One Pace", ONEPACE_MARK in video, True)
for head in ALWAYS:
    check(f"video keeps {head!r}", head in video, True)
    check(f"books keeps {head!r}", head in books, True)

print("\nand only rules it cannot possibly use are withheld")
check("a video download drops the .epub routing", EPUB_MARK in video, False)
check("a book download drops One Pace", ONEPACE_MARK in books, False)
check("video is smaller than the whole prompt", len(video) < len(BASE), True)
check("books is smaller than the whole prompt", len(books) < len(BASE), True)

print("\nNOTHING IS DELETED -- every line survives in at least one variant")
variants = [scope(BASE, VIDEO), scope(BASE, BOOKS), scope(BASE, None)]
covered = set()
for v in variants:
    covered |= set(v.splitlines())
missing = [ln for ln in BASE.splitlines() if ln not in covered]
check("no line of the prompt is unreachable", len(missing), 0)
check("the union of the kind variants is the whole prompt",
      set(scope(BASE, VIDEO).splitlines()) | set(scope(BASE, BOOKS).splitlines())
      == set(BASE.splitlines()), True)

print("\nevery scoped heading is one that actually exists in the prompt")
for head in identify._KIND_SCOPED_SECTIONS:
    check(f"{head!r} is a real heading", head in BASE, True)

print("\nthe runtime prompt reflects the scoping")
from pathlib import Path  # noqa: E402
full_rt = identify._runtime_prompt("/x", "", Path("/x"), sections=None)
video_rt = identify._runtime_prompt("/x", "", Path("/x"), sections=VIDEO)
check("scoped runtime prompt is shorter", len(video_rt) < len(full_rt), True)
check("and still carries the output schema", "## Output JSON schema" in video_rt, True)

print("\nTitle-scoped sections: a section about ONE show is dead weight for every other")
import config as _cfg
_base = _cfg.IDENTIFY_PROMPT_FILE.read_text(encoding="utf-8")
_not_op = identify._scope_base_prompt(_base, ("shows",), "[MTBB] Monogatari Series (BD 1080p)")
_is_op = identify._scope_base_prompt(_base, ("shows",), "[One Pace] Wano 60 [1080p]")
_no_hint = identify._scope_base_prompt(_base, ("shows",), "")
check("a non-One-Pace show drop loses the One Pace section",
      "## One Pace" in _not_op, False)
check("a One Pace drop KEEPS it", "## One Pace" in _is_op, True)
check("an EMPTY title hint drops nothing title-scoped (not knowing is not evidence)",
      "## One Pace" in _no_hint, True)
check("it is a real saving", len(_not_op) < len(_is_op), True)
for _keep in ("## The prime directive", "## Naming conventions",
              "## The hard calls you exist to make", "## Output JSON schema"):
    check(f"{_keep!r} is ALWAYS sent", _keep in _not_op and _keep in _is_op, True)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")
