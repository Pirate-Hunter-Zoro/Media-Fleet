"""The one AI call this repo makes: a plain free chat completion.

Media-Syncer moves bytes. The single judgment it needs is the predownload daemon's
"what will they watch after this film?", and that question is answered entirely from
text already in the prompt -- the film just finished and the catalogue of films on
disk. There is nothing to inspect and nothing to look up, so this is a completion, not
an agent run: no tools, no turn loop, one request.

Runs on free tiers across several independent providers (the fleet is free-only; no paid
model is ever used). The predownload judgment walks the same provider chain as the other
repos — OpenRouter `:free` first, then Groq / Cerebras / Mistral free tiers — so a daily
cap on one shared pool does not starve the uploader's prediction.
(Torrent-Ingest and YouTube-Downloader ask harder questions -- place this download,
heal this show's metadata -- and use the agent runtime in `Torrent-Ingest/ai_client.py`
for them. Do not import that from here: this repo's daemons run under `media_sync_env`,
which does not carry its dependencies, and reaching across for one completion would
couple the uploader's start-up to the ingest repo being present.)

Stdlib only, for the same reason: `media_sync_env` has no `requests`.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_KEYS_DIR = Path.home() / ".config" / "api-keys"

# Groq/Cloudflare 403 the default `Python-urllib` User-Agent with a Cloudflare 1010 block,
# so every request sends a browser UA.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# (name, base_url, key_env, key_file, models-in-preference-order, account_env, account_file).
# A provider is enabled simply by having its key present, so the owner drops a key file to
# add a fallback. `account_*` are only needed by Cloudflare (its account id lives in the
# URL); providers without one pass "" and the base_url is used as-is.
_PROVIDERS = (
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions",
     "OPENROUTER_API_KEY", "openrouter_key",
     ("nvidia/nemotron-3-super-120b-a12b:free",
      "minimax/minimax-m3:free",
      "minimax/minimax-m2.7:free",
      "dots-studio/dots-3-note-preview:free",
      "cohere/north-mini-code:free"), "", ""),
    ("groq", "https://api.groq.com/openai/v1/chat/completions",
     "GROQ_API_KEY", "groq_key",
     ("openai/gpt-oss-120b", "openai/gpt-oss-20b"), "", ""),
    ("cloudflare",
     "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions",
     "CLOUDFLARE_API_TOKEN", "cloudflare_token",
     ("@cf/openai/gpt-oss-20b",
      "@cf/google/gemma-4-26b-a4b-it"),
     "CLOUDFLARE_ACCOUNT_ID", "cloudflare_account"),
)


class AIUnavailable(RuntimeError):
    """No credential, a rejected key, or an exhausted balance."""


def _provider_key(key_env, key_file):
    key = os.environ.get(key_env, "").strip()
    if key:
        return key
    try:
        key = (_KEYS_DIR / key_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return key


def _attempts():
    """The ordered `[{provider, base_url, key, model}]` for every provider with a key."""
    out = []
    for name, base_url, key_env, key_file, models, account_env, account_file in _PROVIDERS:
        key = _provider_key(key_env, key_file)
        if not key:
            continue
        if "{account_id}" in base_url:
            acct = _provider_key(account_env, account_file)
            if not acct:
                continue
            base_url = base_url.replace("{account_id}", acct)
        for model in models:
            out.append({"provider": name, "base_url": base_url, "key": key, "model": model})
    return out


def complete(prompt: str, max_tokens: int = 512, timeout: int = 120) -> str:
    """One completion over the free-provider chain. Returns the reply text; raises when
    every provider failed.

    Deliberately does NOT retry any single provider. Every caller here is best-effort --
    a failed prediction means the next film is not pre-cached, which costs one slow first
    play and nothing else -- so a hung retry loop inside a reconcile pass would be the
    more expensive outcome. Instead the chain just moves to the next free provider.
    """
    attempts = _attempts()
    last_err = "no free AI provider configured"
    ran = False
    unavailable = False
    for a in attempts:
        body = json.dumps({
            "model": a["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }).encode("utf-8")
        req = urllib.request.Request(
            a["base_url"], data=body, method="POST",
            headers={"Authorization": f"Bearer {a['key']}",
                     "Content-Type": "application/json",
                     "User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code in (401, 402, 403):
                unavailable = True
                last_err = f"{a['provider']} http {exc.code}: {detail}"
                continue
            last_err = f"{a['provider']} http {exc.code}: {detail}"
            continue
        except (urllib.error.URLError, OSError) as exc:
            last_err = f"{a['provider']} {exc}"
            continue
        ran = True
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
        if text:
            return text
        last_err = f"{a['provider']}/{a['model']} returned empty text"
    if unavailable and not ran:
        raise AIUnavailable(last_err)
    raise RuntimeError(last_err)


def complete_json(prompt: str, **kw) -> dict:
    """A completion whose reply is expected to be a JSON object.

    Strips a markdown fence if the model wrapped the object in one, then takes the
    outermost braces -- a reply that opens with a sentence before the JSON is common
    enough to be worth surviving, and the alternative is discarding a good answer over
    formatting.
    """
    text = complete(prompt, **kw)
    if not text:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
