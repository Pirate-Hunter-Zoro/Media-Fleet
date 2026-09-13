"""Which model ids a provider ACTUALLY serves today, and what to use when one retires.

THE PROBLEM THIS SOLVES. `config.AI_PROVIDERS` names model ids, and model ids rot -- fast,
silently, and all at once. Measured on 2026-09-12 while wiring up three new providers:

    meta/llama-3.3-70b-instruct        HTTP 410  "reached its end of life on 2026-08-26"
    openai/gpt-oss-120b       (nvidia) HTTP 410  "reached its end of life on 2026-09-03"
    qwen/qwen2.5-coder-32b-instruct    HTTP 410  "reached its end of life on 2026-05-12"
    gemini-2.0-flash                   HTTP 404  "no longer available"
    gemini-2.5-flash                   HTTP 404  "no longer available to new users"

Five of the eight ids a careful reader would have written down were already dead. OpenRouter
retired two `:free` slugs on 2026-09-07 the same way, and the fleet answered 404 on every
identify pass for days without anything saying so: a retired model is not classified as
"unavailable" (that means no budget) nor "transient" (a retry would fix it), so the chain
just spends an attempt on it, logs a line nobody reads, and moves on. Forever.

WHAT THIS DOES, AND DELIBERATELY DOES NOT DO.

  * It does NOT ask a model which model to use. Every provider here exposes `GET /v1/models`;
    listing is an HTTP GET and choosing is a policy. Spending a model call to find a working
    model is circular -- you need a working model to run it -- and spends the exact budget
    that is scarce.
  * It does NOT edit `config.py`, and it does NOT ship. A daemon that rewrites its own
    source and runs `ship-fleet.sh` would restart every daemon including itself (killing any
    identify in flight -- see HANDOFF §4b), push a machine-chosen slug to git where every
    repo pulls it, and bypass the `verify_fleet.sh` gate that is supposed to stand in front
    of a deploy. `config.py` stays the human's DECLARED PREFERENCE.
  * It writes a runtime OVERLAY instead (`state/ai_model_overrides.json`). The chain merges
    it, so the fleet heals on the next cycle; `fleet_health` raises it so a human promotes
    the choice into config deliberately, when they choose.

THE LOAD-BEARING STEP IS THE PROBE. A provider's model list says nothing about function
calling, and this whole fleet is an agent loop -- a model without tool support does not
degrade here, it fails outright. So a candidate is adopted only after a REAL request with a
REAL tool definition comes back with a tool call. That is exactly the check that found the
five dead ids above, done by hand; this is that check, automated.

Fails soft in every direction, like `epguide`: any error leaves the caller with precisely
the behaviour it had before.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

import config

# How long a discovered replacement is trusted before the whole question is re-asked. A
# retirement is permanent, so this is not about the retired model -- it is about the
# REPLACEMENT, which can itself retire.
OVERRIDE_TTL_SEC = int(config.os.environ.get("AI_MODEL_OVERRIDE_TTL_SEC",
                                             str(14 * 24 * 3600)))
_TIMEOUT = 30
OVERRIDES_FILE = config.STATE_DIR / "ai_model_overrides.json"

# A model id that is GONE, as distinct from one that is busy, rate-limited or out of budget.
# Only these three shapes may trigger a replacement search; everything else is someone
# else's problem and must not cause the fleet to silently switch models.
_RETIRED = (
    "no longer available", "end of life", "has been deprecated", "is deprecated",
    "decommissioned", "model not found", "unknown model", "does not exist",
    "unavailable for free",          # OpenRouter's phrasing when a :free slug goes paid
)

# Model ids that are not chat models at all. A provider's list is full of them -- NVIDIA's
# 82 entries include embedders, rerankers, safety guards, OCR, vision and video models --
# and adopting one because it sorted well would be far worse than the retirement it
# replaced. Matched as substrings against the lowercased id.
_NOT_CHAT = (
    "embed", "rerank", "guard", "safety", "moderation", "tts", "whisper", "audio",
    "image", "video", "vision", "ocr", "parse", "clip", "diffusion", "reward",
    "translate", "deplot", "kosmos", "neva", "vila", "nemoretriever", "banana", "lyria",
)


def _get_json(url, key):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        # Groq and Cloudflare-fronted hosts 403 the default urllib UA (see ai_client).
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:      # noqa: S310
        return json.loads(resp.read().decode("utf-8", "replace"))


def models_url(base_url: str) -> str:
    """The `/models` endpoint beside a provider's chat-completions URL.

    Every provider in `config.AI_PROVIDERS` is OpenAI-compatible, which fixes the shape:
    `.../chat/completions` -> `.../models`. Cloudflare's URL carries an account id and
    still follows it.
    """
    return re.sub(r"/chat/completions/?$", "/models", base_url or "")


def live_models(provider_name: str):
    """Every model id `provider_name` serves right now, or None when it cannot be asked.

    None and `[]` are different answers and callers must not conflate them: None is "the
    question could not be put", `[]` is "the provider says it serves nothing".
    """
    prov = config.ai_provider(provider_name)
    if not prov or not prov.get("base_url") or not prov.get("key"):
        return None
    try:
        data = _get_json(models_url(prov["base_url"]), prov["key"])
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError,
            TimeoutError):
        return None
    items = (data or {}).get("data")
    if not isinstance(items, list):
        return None
    out = []
    for m in items:
        mid = (m or {}).get("id") if isinstance(m, dict) else None
        if isinstance(mid, str) and mid.strip():
            # Google's shim returns `models/gemini-3.5-flash`; its chat endpoint wants the
            # bare id. Normalising here keeps the difference out of every caller.
            out.append(mid.strip().removeprefix("models/"))
    return out


def is_retired(detail: str) -> bool:
    """Whether a provider's refusal means this MODEL ID is gone for good.

    Deliberately narrow. A rate limit, an out-of-budget day and a 503 under load all leave
    the model perfectly valid tomorrow, and switching models because of one would be the
    fleet quietly drifting off the ids a human chose.
    """
    low = (detail or "").lower()
    if any(sig in low for sig in _RETIRED):
        return True
    # A bare 410 Gone is unambiguous. A bare 404 is not -- it is also what a typo'd URL
    # returns -- so 404 counts only alongside the word "model".
    if "http 410" in low or '"status":410' in low:
        return True
    return ("http 404" in low or '"code": 404' in low) and "model" in low


# Size/capability markers. A model carrying one of these is a smaller sibling, and a
# smaller sibling is a fallback, never a preferred replacement.
_SMALLER = {"lite", "mini", "nano", "tiny", "small", "flashlite"}


def _family(model_id: str):
    """The comparable tokens of a model id, for judging 'same family as'."""
    return [t for t in re.split(r"[^a-z0-9]+", (model_id or "").lower()) if t]


def _version_key(model_id: str):
    """Sortable version-ish numbers in an id, so a NEWER sibling outranks an older one."""
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", model_id or "")] or [0.0]


def rank_replacements(retired: str, live) -> list:
    """`live` ordered by how plausibly each is a drop-in for `retired`. Best first.

    Three rules, in order of weight:

      1. Never a non-chat model (`_NOT_CHAT`). A ranking that can return an embedder is
         not a ranking, it is a hazard.
      2. Same family first, measured by shared tokens. `gemini-3.5-flash` should be
         replaced by `gemini-3.6-flash`, never by `gemini-pro-latest` and certainly never
         by some other vendor's model that happens to sort well.
      3. Newer over older, and a stable id over a `preview`/`experimental` one -- but a
         preview is still allowed, because sometimes it is all there is.
    """
    want = set(_family(retired))
    want_words = {t for t in want if not t.isdigit()}
    downgrade_of = _SMALLER.intersection(want)      # already a lite model? then lite is fine
    out = []
    for mid in live or ():
        low = mid.lower()
        if any(bad in low for bad in _NOT_CHAT):
            continue
        if mid == retired:
            continue
        toks = set(_family(mid))
        # Family must share a WORD, never just a number. Without this,
        # `nvidia/nemotron-3-super-120b-a12b` and `z-ai/glm-5.3-flash` are "the same
        # family" because both contain a 3, and a retirement could silently move the
        # fleet to a different vendor's model.
        if not (want_words & {t for t in toks if not t.isdigit()}):
            continue
        overlap = len(want & toks)
        # A `-lite`/`-mini`/`-nano` sibling is a capability DOWNGRADE, not a drop-in, and
        # it would otherwise WIN: `gemini-3.5-flash-lite` shares every token of
        # `gemini-3.5-flash` plus one, so raw overlap ranks it above `gemini-3.8-flash`.
        # It is still allowed last, because a downgrade beats nothing at all.
        smaller = 0 if downgrade_of else (1 if _SMALLER & toks else 0)
        preview = 1 if any(w in low for w in ("preview", "experimental", "-exp", "beta")) else 0
        out.append((smaller, preview, -overlap, [-v for v in _version_key(mid)], mid))
    out.sort()
    return [mid for *_rest, mid in out]


def probe_usable(provider_name: str, model: str) -> bool:
    """One real request with a real tool definition. True only if it comes back tool-calling.

    This is the check that matters and the reason discovery is not just a list lookup: the
    models endpoint says nothing about function calling, and `ai_client` is an agent loop.
    A model that cannot call a tool is useless here however new it is.
    """
    prov = config.ai_provider(provider_name)
    if not prov:
        return False
    tools = [{"type": "function", "function": {
        "name": "Write", "description": "Write a file",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path", "content"]}}}]
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user",
                      "content": "Call the Write tool to write 'ok' to /tmp/probe.txt."}],
        "tools": tools, "tool_choice": "auto", "max_tokens": 256}).encode()
    req = urllib.request.Request(prov["base_url"], data=body, method="POST", headers={
        "Authorization": f"Bearer {prov['key']}", "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:                                                # noqa: BLE001
        return False
    msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    return bool(msg.get("tool_calls"))


# --- the overlay ---------------------------------------------------------------------

def load_overrides(now=None):
    """`{provider: {retired_model: replacement}}` for every override inside its TTL."""
    now = time.time() if now is None else now
    try:
        raw = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for prov, entries in (raw or {}).items():
        if not isinstance(entries, dict):
            continue
        for dead, rec in entries.items():
            if not isinstance(rec, dict):
                continue
            if now - float(rec.get("at", 0)) < OVERRIDE_TTL_SEC and rec.get("to"):
                out.setdefault(prov, {})[dead] = rec["to"]
    return out


def save_override(provider_name: str, dead: str, replacement: str, now=None):
    """Record that `dead` is gone and `replacement` was PROBED and works."""
    now = time.time() if now is None else now
    try:
        raw = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw.setdefault(provider_name, {})[dead] = {"to": replacement, "at": now}
    try:
        OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OVERRIDES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=1), encoding="utf-8")
        tmp.replace(OVERRIDES_FILE)
    except OSError:
        pass
    return replacement


def find_replacement(provider_name: str, dead: str, probe=None, lister=None,
                     max_probes: int = 3):
    """Discover, PROBE and record a live stand-in for `dead`. Returns it, or None.

    Bounded on purpose: at most `max_probes` candidates are tried, so a provider whose
    whole catalogue is unusable costs three requests once rather than eighty every cycle.
    `probe`/`lister` are injectable so the tests never touch the network.
    """
    live = (lister or live_models)(provider_name)
    if not live:
        return None
    probe = probe or probe_usable
    for cand in rank_replacements(dead, live)[:max_probes]:
        if probe(provider_name, cand):
            return save_override(provider_name, dead, cand)
    return None
