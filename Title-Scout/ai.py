"""The two the free-model chain calls this repo makes: understand the request, then verify a match.

Title-Scout is not an agent; it uses two plain completions, the same shape as the rest of
the fleet:

  1. `interpret_title` -- turn the raw `find.txt` text (possibly "Title by Author",
     possibly with a typo) into a structured target: canonical title, author, media
     kind, and the search queries to run. The kind drives which tracker category and
     which archive.org mediatype we search.

  2. `verify_match` -- given that target and the candidates the sources returned, pick
     the single candidate that is genuinely the SAME work (same title AND same author,
     where given), not a same-name different thing. This is the "find the RIGHT book"
     guarantee: a bare title like "Foundation" must not download the wrong author's book.

Both are best-effort: a failure raises and the caller decides whether to retry the inbox
next sweep.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import config


class AIUnavailable(RuntimeError):
    """No credential, a rejected key, or an exhausted balance."""


def complete(prompt: str, max_tokens: int = config.MAX_TOKENS) -> str:
    """One chat completion over the FREE-provider chain, returned as stripped text.

    Walks `config.enabled_ai_attempts()` in order; a provider that cannot run (no key /
    quota / rate-limit) or returns empty text is skipped for the next. Raises AIUnavailable
    when NO provider could run, RuntimeError when providers ran but none returned text.
    """
    attempts = config.enabled_ai_attempts()
    if not attempts:
        raise AIUnavailable("no free AI provider configured: add an OpenRouter key under "
                            "~/.config/api-keys/openrouter_key")
    last_err = "no provider returned a result"
    ran = False
    unavailable = False
    for a in attempts:
        try:
            text = _complete_once(a, prompt, max_tokens)
        except AIUnavailable as exc:
            last_err = str(exc)
            unavailable = True
            continue
        except RuntimeError as exc:
            last_err = str(exc)
            continue
        ran = True
        if text:
            return text
        last_err = f"{a['provider']}/{a['model']} returned empty text"
    if unavailable and not ran:
        raise AIUnavailable(last_err)
    raise RuntimeError(last_err)


def _complete_once(attempt, prompt, max_tokens):
    body = json.dumps({
        "model": attempt["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(
        attempt["base_url"], data=body, method="POST",
        headers={"Authorization": f"Bearer {attempt['key']}",
                 "Content-Type": "application/json",
                 "User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code in (401, 402, 403):
            raise AIUnavailable(f"{attempt['provider']} http {exc.code}: {detail}") from exc
        raise RuntimeError(f"{attempt['provider']} http {exc.code}: {detail}") from exc
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()


def _extract_json(text: str):
    """Pull the first JSON array/object out of a model response, tolerating prose."""
    t = text.strip()
    t = t.replace("```json", "").replace("```", "")
    start = min((i for i in (t.find("["), t.find("{")) if i >= 0), default=-1)
    end = max(t.rfind("]"), t.rfind("}"))
    if start < 0 or end <= start:
        raise ValueError("no JSON in model response")
    return json.loads(t[start:end + 1])


def interpret_title(text: str) -> dict:
    """Turn the raw `find.txt` text into {title, author, kind, queries, note}.

    Raises on an unusable response -- the caller leaves `find.txt` intact for a retry.
    """
    prompt = (
        "You are interpreting a single request the user typed to find and download a "
        "specific book or piece of media (most often a book, but possibly a movie, TV "
        "show, anime, comic, manga, or audiobook).\n\n"
        f"Request: {text!r}\n\n"
        "Return ONLY a JSON object with these keys:\n"
        '- "title": the canonical title.\n'
        '- "author": the author/creator if one is given or inferable, else "".\n'
        '- "kind": one of "book", "audiobook", "manga", "comic", "movie", "tv", '
        '"anime", or "other".\n'
        '- "queries": an array of 2-5 concrete search strings to run. Include the exact '
        'title first, then "title + author" and any well-known alternate spellings. Keep '
        "each query tight -- no years, no vague words, no franchise expansion.\n"
        '- "format": the preferred file format for this work. Use "pdf" for textbooks, '
        'references, manuals, and fixed-layout/illustrated works; "epub" for novels and '
        'narrative books; "audio" for audiobooks; "video" for films/TV; "any" when '
        "indifferent.\n"
        '- "note": a short note or "".\n'
        "Do not invent an author; leave it empty rather than guess."
    )
    out = complete(prompt)
    data = _extract_json(out)
    if not isinstance(data, dict) or not data.get("title"):
        raise ValueError("interpret produced no title")
    queries = [q for q in (data.get("queries") or []) if isinstance(q, str) and q.strip()]
    if not queries:
        queries = [data["title"]]
    return {
        "title": str(data["title"]).strip(),
        "author": str(data.get("author") or "").strip(),
        "kind": str(data.get("kind") or "other").strip().lower(),
        "queries": queries,
        "format": str(data.get("format") or "any").strip().lower(),
        "note": str(data.get("note") or "").strip(),
    }


def verify_match(target: dict, candidates: list[dict]) -> dict:
    """Pick the candidate that is the RIGHT work, or {"index": null}.

    `candidates` is a list of {title, source, size, seeders, format}. Returns
    {"index": int|None, "confidence": "high|medium|low", "reason": str}. The caller only
    downloads a candidate confirmed at "high" or "medium" confidence.
    """
    if not candidates:
        return {"index": None, "confidence": "low", "reason": "no candidates"}
    lines = []
    for i, c in enumerate(candidates):
        lines.append(
            f"{i}. source={c.get('source')} seeders={c.get('seeders', 0)} "
            f"size={c.get('size') or '?'} format={c.get('format') or '?'} "
            f"title={c.get('title')!r}"
        )
    prompt = (
        "A user asked to find and download a specific work. Decide which candidate "
        "(if any) is genuinely that same work -- same title AND same author where given, "
        "NOT a same-name different thing.\n\n"
        f"Requested title: {target['title']!r}\n"
        f"Author: {target.get('author') or '(none given)'!r}\n"
        f"Kind: {target.get('kind')}\n"
        f"Preferred format: {target.get('format') or 'any'!r}\n\n"
        "Candidates:\n" + "\n".join(lines) + "\n\n"
        "Prefer an exact title match and, when an author is given, the same author. "
        "Then strongly prefer a candidate in the preferred format (e.g. epub for a novel, "
        "pdf for a textbook); only among equally-correct same-format candidates prefer "
        "more seeders and a cleaner format. "
        "Return ONLY a JSON object: "
        '{"index": <int or null>, "confidence": "high|medium|low", "reason": "<short>"}. '
        'If none of the candidates is the right work, index is null and confidence is "low".'
    )
    out = complete(prompt)
    data = _extract_json(out)
    if not isinstance(data, dict):
        return {"index": None, "confidence": "low", "reason": "bad verify response"}
    idx = data.get("index")
    if not isinstance(idx, int):
        return {"index": None, "confidence": "low", "reason": str(data.get("reason", ""))}
    return {
        "index": idx,
        "confidence": str(data.get("confidence", "low")).lower(),
        "reason": str(data.get("reason", "")),
    }
