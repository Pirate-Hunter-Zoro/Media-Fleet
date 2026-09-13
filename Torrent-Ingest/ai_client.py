"""The fleet's AI runtime: a tool-calling agent loop over a free chat API (OpenRouter).

Every judgment call in this fleet -- placing a download, judging an episode, healing a
show's metadata, curating a playlist -- is an AGENTIC run, not a chat completion. The
model has to look at the actual files (`ffprobe` a runtime, read a sibling `.nfo`),
look things up on the web (an episode guide, a TMDB id), and write a JSON plan to a
known path. OpenRouter serves many free chat endpoints with function calling; none of them
ship an agent. This module is that agent: a conversation loop that hands the model a fixed tool
set, executes each tool call locally, feeds the result back, and stops when the model
stops asking for tools.

The engine never trusts what comes out of here. A run PROPOSES -- it writes a plan file
and returns prose -- and `library.validate_plan` / `apply_plan` / `verify_applied` decide
whether any of it touches the library. That split is why a cheaper, less predictable
model is safe to put behind this seam at all: the guards downstream are unchanged, and
they were always the thing standing between a bad plan and the library.

Two safety properties this module owns, because the model no longer supplies them:

  * DESTROYING MEDIA IS NOT EXPRESSIBLE. Several prompts say "never delete a media file"
    and used to rely on the model honouring it. It is not a promise any more: there is no
    shell tool, so no tool here can unlink or relocate anything, and `Write`/`Edit` refuse
    a media path under the library on the RESOLVED destination. Through the mediafs mount
    a delete propagates to every drive and can destroy the only copy of something not yet
    uploaded, so this is not a lint -- it is the difference between a bad run and lost
    content. See the tools section for the incident that settled the design.
  * THE CONVERSATION CANNOT GROW WITHOUT BOUND. An 80-turn run over a complete-series
    pack would otherwise walk off the end of the context window mid-plan. Tool RESULTS
    are elided oldest-first once the transcript passes `MAX_CONTEXT_CHARS`; the messages
    themselves stay, because the chat API rejects a request whose `tool_calls` lack their
    matching `tool` replies.

    python3 ai_client.py "your prompt"      # one ad-hoc run, for debugging
"""
from __future__ import annotations

import fnmatch
import html
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Deliberately stdlib-only for HTTP. The three repos run under three different conda
# envs and only `torrent_ingest_env` has `requests`; Media-Syncer's daemons import this
# module from `media_sync_env`, where an `import requests` at module scope would be an
# ImportError at daemon start rather than a missing feature at call time.

# --- credentials -------------------------------------------------------------

API_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_FILE = Path.home() / ".config" / "api-keys" / "openrouter_key"

# The fleet is free-only: DEFAULT_MODEL is OpenRouter's `:free` tier (a strong NVIDIA
# model that supports function calling). The work behind this seam is placement and
# identity judgment, but it runs against a HARD guardrail (the harness verifies every
# plan before anything is applied, and a wrong answer surfaces as a failed verify rather
# than a mis-filed show). There is no paid tier to opt up to: the escalation path is the
# free fallback chain in config.AI_PROVIDERS, where a rejected plan is handed to the next
# free model as "fix exactly this" context.
#
# Function calling is the load-bearing requirement -- this module is an agent loop, so a
# model without tool support does not degrade here, it fails outright. Verify it before
# changing this line.
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

# Per-request ceiling on the model's own output. A plan for a complete-series pack is
# written through the Write tool, not returned as prose, so the reply itself is never
# long; this only has to fit a tool call plus a rationale. (The pro model is a REASONING
# model whose chain-of-thought is billed against the same max_tokens; the cheap tier is
# not, so this budget is more than enough either way -- kept large so opting up to pro
# still works.)
MAX_OUTPUT_TOKENS = 32768

# Transport retry. Distinct from the CALLER's retry (config.IDENTIFY_MAX_ATTEMPTS, which
# re-runs the whole agent): this one covers a single HTTP call blipping, where re-running
# the entire conversation would be absurd.
HTTP_ATTEMPTS = 4
HTTP_BACKOFF_SEC = 5

# --- pacing a per-MINUTE token limit ------------------------------------------
#
# A free tier's rate limit is not one thing. Groq's is 200,000 tokens a DAY against 8,000
# tokens per MINUTE, and the fleet treated both halves as the same door: any 429 raised
# `AIUnavailable`, which made the caller abandon the provider and move down the chain. So
# a provider with a whole day's budget left was written off over a twelve-second pause it
# had measured for us and named in its own reply.
#
# That is the difference between groq being usable at all and groq being permanently
# excluded -- which is what `OPERATING.md` §6 recorded it as. The limit that
# actually excludes it is the per-request one (a single request larger than the per-minute
# allowance can never succeed, whatever we wait); the rolling window is just a pause.
#
# Bounded on both axes so a genuinely dead door still fails over: no single wait longer
# than MAX_RATE_LIMIT_WAIT_SEC, and no more than MAX_RATE_LIMIT_WAIT_TOTAL_SEC of waiting
# across one HTTP call. A daily cap states minutes or hours, or states nothing at all, and
# fails both tests -- so it still takes the deferral path it always did.
MAX_RATE_LIMIT_WAIT_SEC = 90
MAX_RATE_LIMIT_WAIT_TOTAL_SEC = 240

# "please try again in 12.5s" / "... in 1m30s" / "... in 2.5 minutes"
_RETRY_IN = re.compile(
    r"try again in\s+(?:(\d+)\s*m(?:in(?:ute)?s?)?\s*)?(\d+(?:\.\d+)?)\s*(m(?:in(?:ute)?s?)?|s)",
    re.IGNORECASE)
# "Limit 8000, Requested 8763" -- the request itself is over the whole allowance.
_LIMIT_REQUESTED = re.compile(r"limit\s+(\d+)[^\d]{0,20}requested\s+(\d+)", re.IGNORECASE)


def _retry_after_sec(exc, detail: str):
    """How long the provider says to wait, in seconds, or None when it does not say.

    Prefers the `Retry-After` header (the standard), then the wording of the body, which
    is where Groq puts it. Returning None is the honest answer and makes the caller fail
    over -- guessing a wait for a door that is closed for the day would stall every
    judgment call in the fleet behind it.
    """
    try:
        hdr = (exc.headers or {}).get("Retry-After")
    except Exception:                                                   # noqa: BLE001
        hdr = None
    if hdr:
        try:
            return max(1.0, float(str(hdr).strip()))
        except (TypeError, ValueError):
            pass
    m = _RETRY_IN.search(detail or "")
    if not m:
        return None
    mins, value, unit = m.group(1), float(m.group(2)), m.group(3).lower()
    secs = value * 60 if unit.startswith("m") else value
    if mins:
        secs += float(mins) * 60
    # A sub-second wait still needs a real pause: the window is a rolling one and coming
    # back at the same instant just gets refused again.
    return max(1.0, secs + 1.0)


def _request_exceeds_limit(detail: str) -> bool:
    """Whether THIS request is bigger than the provider's entire per-window allowance.

    No amount of waiting fixes that, so it must not be paced -- it is the prompt-ceiling
    signal `identify` records and skips the provider on.
    """
    m = _LIMIT_REQUESTED.search(detail or "")
    return bool(m) and int(m.group(2)) > int(m.group(1))


class AIUnavailable(RuntimeError):
    """The API could not be reached in a way that re-running would fix: no credential,
    a rejected key, or an exhausted balance. Distinct from a bad answer -- the run never
    happened, so callers defer the work instead of blaming the content for it.
    """


def api_key() -> str:
    """The OpenRouter key: the environment first, then the on-disk key file.

    The env var exists so a run can be pointed at a different account without editing
    the file every daemon reads; the file is the normal path, because launchd hands a
    LaunchAgent an environment with nothing useful in it.
    """
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AIUnavailable(f"no OpenRouter credential: cannot read {KEY_FILE} ({exc})") from exc
    if not key:
        raise AIUnavailable(f"no OpenRouter credential: {KEY_FILE} is empty")
    return key


# --- tools -------------------------------------------------------------------
#
# THERE IS NO SHELL TOOL, and that is the single most important design decision in this
# module. It is not caution -- it is the result of the migration's own test.
#
# The first version of this file did ship a `Bash` tool, guarded by a blocklist that
# refused `rm`/`mv`/`unlink` aimed at the library. An adversarial run was told to delete a
# real episode and to route around any refusal. It hit the guard on `rm`, said so, and
# then deleted the file with `find -delete` -- which the blocklist never matched. A
# 447 MB episode went off the SSD and off the mount. (It was restored from the MEGA pool;
# no purge had been queued, so the pool copy was intact. That was luck about timing, not
# a property of the design.)
#
# The lesson generalises past `find`: `python3 -c "os.remove(...)"`, `perl -e unlink`,
# `install /dev/null f`, a bare `> f`. Enumerating the ways a shell can destroy a file is
# not a solvable problem, and every miss is content. So deletion is not FILTERED here, it
# is made INEXPRESSIBLE: the model is handed the three capabilities the prompts actually
# need a shell for -- probe a media file, list a directory, call the Jellyfin API -- as
# narrow tools with fixed argument vectors and no shell interpretation anywhere.
#
# Write and Edit are the remaining way to damage media (truncate an .mkv by writing to
# its path), so they refuse a destination that is media under a protected root. That check
# reads the RESOLVED path, not command text, so there is no spelling that evades it.

# How much of any single tool result the model is allowed to see. A recursive listing of
# a 250-file pack or a fetched web page will exceed this; the truncation is announced in
# the result so the model knows it is looking at a prefix rather than the whole thing.
MAX_TOOL_RESULT_CHARS = 24_000
PROBE_TIMEOUT_SEC = 60
JELLYFIN_TIMEOUT_SEC = 60

TOOL_SCHEMAS = {
    "Probe": {
        "type": "function",
        "function": {
            "name": "Probe",
            "description": (
                "Inspect a media file with ffprobe: duration, resolution, codecs, "
                "track languages. Use it for the genuinely ambiguous calls (is this a "
                "movie or a long special?), not to confirm a clearly-named episode."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to probe."},
                },
                "required": ["file_path"],
            },
        },
    },
    "ListDir": {
        "type": "function",
        "function": {
            "name": "ListDir",
            "description": "List a directory's entries with sizes. Optionally recursive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute directory path."},
                    "recursive": {"type": "boolean",
                                  "description": "Walk subdirectories too."},
                },
                "required": ["path"],
            },
        },
    },
    "Jellyfin": {
        "type": "function",
        "function": {
            "name": "Jellyfin",
            "description": (
                "Call the Jellyfin HTTP API. Authentication is added for you -- never "
                "put an api_key or token in the path. Example paths: "
                "'/Items?Recursive=true&IncludeItemTypes=Series&Fields=Path', "
                "'/Items/<id>/Refresh?metadataRefreshMode=Default', '/Users'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["GET", "POST"],
                               "description": "HTTP method."},
                    "path": {"type": "string",
                             "description": "API path beginning with '/', query string included."},
                    "body": {"type": "string",
                             "description": "JSON body for a POST, as a string."},
                },
                "required": ["method", "path"],
            },
        },
    },
    "Read": {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read a UTF-8 text file (a .nfo sidecar, a manifest, a plan).",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to read."},
                    "offset": {"type": "integer", "description": "First line to return (1-based)."},
                    "limit": {"type": "integer", "description": "How many lines to return."},
                },
                "required": ["file_path"],
            },
        },
    },
    "Write": {
        "type": "function",
        "function": {
            "name": "Write",
            "description": (
                "Write a file, creating parent directories and overwriting any existing "
                "content. This is how a run delivers its plan/manifest/verdict."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to write."},
                    "content": {"type": "string", "description": "The complete file contents."},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    "Edit": {
        "type": "function",
        "function": {
            "name": "Edit",
            "description": "Replace an exact substring in an existing text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to edit."},
                    "old_string": {"type": "string", "description": "Exact text to replace."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean",
                                    "description": "Replace every occurrence, not just one."},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
    "Glob": {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": "List paths matching a glob pattern, newest first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string",
                                "description": "Glob pattern, e.g. '**/*.nfo'."},
                    "path": {"type": "string",
                             "description": "Directory to search in. Defaults to the run's cwd."},
                },
                "required": ["pattern"],
            },
        },
    },
    "Grep": {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": "Search file contents for a regular expression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression."},
                    "path": {"type": "string", "description": "File or directory to search."},
                    "glob": {"type": "string",
                             "description": "Only search files matching this glob, e.g. '*.nfo'."},
                    "ignore_case": {"type": "boolean", "description": "Case-insensitive match."},
                },
                "required": ["pattern"],
            },
        },
    },
    "WebSearch": {
        "type": "function",
        "function": {
            "name": "WebSearch",
            "description": (
                "Search the web and return result titles, URLs and snippets. Use it to "
                "find episode guides, wiki pages and TMDB/TVDB entries, then WebFetch "
                "the promising result for the actual text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                },
                "required": ["query"],
            },
        },
    },
    "WebFetch": {
        "type": "function",
        "function": {
            "name": "WebFetch",
            "description": "Fetch a URL and return its readable text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The absolute URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
}

DEFAULT_TOOLS = ("Read", "Write", "Glob", "Grep", "Probe", "ListDir",
                 "WebSearch", "WebFetch")


# --- the media guard ---------------------------------------------------------
#
# With no shell tool, the only remaining way to destroy a media file is to WRITE over it.
# So Write and Edit refuse a destination that is media under a protected root. The check
# resolves the path first, which is what makes it different in kind from the command-text
# blocklist it replaced: `..` traversal, a symlink, and an absolute path all normalise to
# the same answer, so there is no spelling of "that episode" that gets through.
#
# Sidecars stay writable -- `.nfo`, artwork and manifests are what these runs exist to
# fix, and every one of them is regenerable. Media is not.

_MEDIA_SUFFIXES = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm", ".flv", ".wmv",
                   ".mpg", ".mpeg", ".m2ts", ".iso", ".cbz", ".cbr", ".pdf", ".epub",
                   ".mp3", ".flac", ".m4a", ".opus", ".srt", ".ass"}


def _protected_roots() -> list[Path]:
    """Absolute roots under which media files may not be written.

    Read from config lazily and defensively: this module is imported by a bare runner
    process that must still work if config is mid-edit. A missing attribute narrows the
    guard rather than crashing the run -- but the two that matter (the SSD library root
    and the mediafs mount) are the first things config defines, so in practice both are
    always present.
    """
    roots = []
    try:
        import config
        for attr in ("MEDIA_ROOT", "MEDIAFS_MOUNT", "SHOWS_ROOT", "MOVIES_ROOT",
                     "COMICS_ROOT"):
            val = getattr(config, attr, None)
            if val:
                try:
                    roots.append(Path(str(val)).resolve())
                except OSError:
                    continue
    except Exception:                                                     # noqa: BLE001
        pass
    return roots


def _guard_write(path: Path) -> str | None:
    """The refusal text for writing to `path`, or None if the write may proceed."""
    if path.suffix.lower() not in _MEDIA_SUFFIXES:
        return None
    try:
        target = path.resolve()
    except OSError:
        target = path
    for root in _protected_roots():
        if target == root or root in target.parents:
            return (f"REFUSED: {target.name} is a media file inside the library "
                    f"({root}). This run may not create, overwrite or truncate media -- "
                    f"only sidecars (.nfo, artwork) and its own output files. Writing "
                    f"here would destroy the only copy on some drives. Describe what you "
                    f"wanted to change in your summary instead.")
    return None


# --- tool implementations ----------------------------------------------------

def _truncate(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated, {len(text) - limit} more characters]"


def _probe(args: dict) -> str:
    """ffprobe one file. A FIXED argument vector, never a shell string: the path comes
    from the model, and `shell=True` anywhere near model-supplied text is how a probe
    becomes arbitrary command execution."""
    path = Path(str(args.get("file_path", "")))
    if not path.is_absolute():
        return "ERROR: file_path must be absolute."
    if not path.exists():
        return f"ERROR: no such file {path}"
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_SEC, errors="replace")
    except FileNotFoundError:
        return "ERROR: ffprobe is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return f"ERROR: ffprobe timed out after {PROBE_TIMEOUT_SEC}s."
    if proc.returncode != 0:
        return f"ERROR: ffprobe failed: {(proc.stderr or '').strip()[:400]}"

    # Summarised, not dumped: the raw JSON for one file is thousands of tokens of
    # bitrate/disposition minutiae, and an 80-turn run probing a dozen files on the raw
    # output would spend its whole context window on it.
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _truncate(proc.stdout)
    fmt = data.get("format") or {}
    lines = [f"path: {path.name}",
             f"duration: {float(fmt.get('duration', 0) or 0):.1f}s",
             f"size: {fmt.get('size', '?')} bytes",
             f"container: {fmt.get('format_name', '?')}"]
    attachments = 0
    for st in data.get("streams") or []:
        kind = st.get("codec_type", "?")
        # A subtitled anime release ships dozens of font attachments. They decide nothing
        # and would otherwise be most of the probe's output, so they are counted, not listed.
        if kind == "attachment":
            attachments += 1
            continue
        bits = [f"{kind}: {st.get('codec_name', '?')}"]
        if kind == "video":
            bits.append(f"{st.get('width', '?')}x{st.get('height', '?')}")
        lang = (st.get("tags") or {}).get("language")
        title = (st.get("tags") or {}).get("title")
        if lang:
            bits.append(f"lang={lang}")
        if title:
            bits.append(f"title={title!r}")
        lines.append("  " + "  ".join(bits))
    if attachments:
        lines.append(f"  ({attachments} font/attachment stream(s), omitted)")
    return "\n".join(lines)


def _list_dir(args: dict) -> str:
    path = Path(str(args.get("path", "")))
    if not path.is_absolute():
        return "ERROR: path must be absolute."
    if not path.is_dir():
        return f"ERROR: not a directory: {path}"
    entries, count = [], 0
    walker = path.rglob("*") if args.get("recursive") else path.iterdir()
    for p in sorted(walker):
        count += 1
        if count > 2000:
            entries.append(f"  ... and more (listing capped at 2000 entries)")
            break
        try:
            rel = p.relative_to(path)
            entries.append(f"  {rel}/" if p.is_dir() else f"  {rel}  ({p.stat().st_size} bytes)")
        except OSError:
            continue
    return _truncate("\n".join(entries) or "(empty directory)")


def _jellyfin(args: dict) -> str:
    """One authenticated Jellyfin API call.

    The credential is injected here rather than handed to the model, so the API key never
    enters the transcript that goes to the model -- and a run cannot leak it by quoting its
    own tool call back in a summary.
    """
    try:
        import config
        base = str(getattr(config, "JELLYFIN_URL", "") or "").rstrip("/")
        key = str(getattr(config, "JELLYFIN_API_KEY", "") or "")
    except Exception as exc:                                              # noqa: BLE001
        return f"ERROR: cannot read Jellyfin settings ({exc})"
    if not base or not key:
        return "ERROR: JELLYFIN_URL / JELLYFIN_API_KEY are not configured."

    method = str(args.get("method", "GET")).upper()
    if method not in ("GET", "POST"):
        return "ERROR: method must be GET or POST."
    path = str(args.get("path", ""))
    if not path.startswith("/"):
        return "ERROR: path must begin with '/'."

    url = base + path
    body = args.get("body")
    data = str(body).encode("utf-8") if body else (b"" if method == "POST" else None)
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-Emby-Token": key, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=JELLYFIN_TIMEOUT_SEC) as resp:  # noqa: S310
            text = resp.read().decode("utf-8", errors="replace")
            return _truncate(text.strip() or f"(HTTP {resp.status}, empty body)")
    except urllib.error.HTTPError as exc:
        return f"ERROR: jellyfin http {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}"
    except (urllib.error.URLError, OSError) as exc:
        return f"ERROR: jellyfin request failed ({exc})"


def _read(args: dict) -> str:
    path = Path(str(args.get("file_path", "")))
    if not path.is_absolute():
        return "ERROR: file_path must be absolute."
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"ERROR: cannot read {path} ({exc})"
    offset, limit = args.get("offset"), args.get("limit")
    if offset or limit:
        lines = text.splitlines()
        start = max(0, int(offset or 1) - 1)
        end = start + int(limit) if limit else len(lines)
        text = "\n".join(lines[start:end])
    return _truncate(text) if text.strip() else "(file is empty)"


def _write(args: dict) -> str:
    path = Path(str(args.get("file_path", "")))
    if not path.is_absolute():
        return "ERROR: file_path must be absolute."
    content = args.get("content")
    if content is None:
        return "ERROR: no content given."
    refusal = _guard_write(path)
    if refusal:
        return refusal
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
    except OSError as exc:
        return f"ERROR: cannot write {path} ({exc})"
    return f"Wrote {len(str(content))} bytes to {path}"


def _edit(args: dict) -> str:
    path = Path(str(args.get("file_path", "")))
    if not path.is_absolute():
        return "ERROR: file_path must be absolute."
    old, new = str(args.get("old_string", "")), str(args.get("new_string", ""))
    if not old:
        return "ERROR: old_string must be non-empty. Use Write to create a file."
    refusal = _guard_write(path)
    if refusal:
        return refusal
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"ERROR: cannot read {path} ({exc})"
    count = text.count(old)
    if count == 0:
        return "ERROR: old_string not found in the file."
    if count > 1 and not args.get("replace_all"):
        return (f"ERROR: old_string appears {count} times. Pass replace_all, or include "
                f"more surrounding text to make it unique.")
    try:
        path.write_text(text.replace(old, new), encoding="utf-8")
    except OSError as exc:
        return f"ERROR: cannot write {path} ({exc})"
    return f"Replaced {count if args.get('replace_all') else 1} occurrence(s) in {path}"


def _glob(args: dict, cwd: str) -> str:
    root = Path(str(args.get("path") or cwd))
    pattern = str(args.get("pattern", "*"))
    try:
        hits = [p for p in root.glob(pattern) if p.exists()]
    except (OSError, ValueError, NotImplementedError) as exc:
        # `pathlib` raises NotImplementedError ("Non-relative patterns are
        # unsupported") for an absolute pattern — the model handing back the full
        # path instead of a relative glob. Return it as a tool error rather than
        # letting it crash the whole run.
        return f"ERROR: bad glob ({exc})"
    hits.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    if not hits:
        return "(no matches)"
    return _truncate("\n".join(str(p) for p in hits[:500])
                     + (f"\n... and {len(hits) - 500} more" if len(hits) > 500 else ""))


def _grep(args: dict, cwd: str) -> str:
    pattern = str(args.get("pattern", ""))
    flags = re.I if args.get("ignore_case") else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return f"ERROR: bad regular expression ({exc})"
    target = Path(str(args.get("path") or cwd))
    file_glob = args.get("glob")
    files = []
    if target.is_file():
        files = [target]
    elif target.is_dir():
        for p in target.rglob("*"):
            if not p.is_file():
                continue
            if file_glob and not fnmatch.fnmatch(p.name, str(file_glob)):
                continue
            files.append(p)
            if len(files) >= 5000:
                break
    else:
        return f"ERROR: no such path {target}"
    out = []
    for p in files:
        try:
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{p}:{n}:{line.strip()[:300]}")
                    if len(out) >= 400:
                        return _truncate("\n".join(out) + "\n[... more matches suppressed]")
        except OSError:
            continue
    return _truncate("\n".join(out)) if out else "(no matches)"


# A browser UA: the search endpoint serves a stub to anything that looks automated.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SEARCH_URL = "https://html.duckduckgo.com/html/"
_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _detag(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def _unwrap(href: str) -> str:
    """DuckDuckGo wraps every hit in a redirect; the real URL is the `uddg` parameter."""
    if "uddg=" in href:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            return qs.get("uddg", [href])[0]
        except ValueError:
            return href
    return href


def _http_get(url: str, timeout: int) -> tuple[str, str]:
    """Fetch a URL. Returns (body_text, content_type); raises OSError on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:            # noqa: S310
        raw = resp.read(8 * 1024 * 1024)
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace"), resp.headers.get("Content-Type", "")


def _encode_url(url: str) -> str:
    """Percent-encode anything urlopen would reject (spaces, control chars, non-ASCII).

    The model sometimes hand-builds a URL and leaves the query raw — e.g. a Wikidata
    `wbsearchentities` call `...&search=劇場版 STEINS;GATE 負荷領域のデジャヴ...`. urlopen
    rejects that with http.client.InvalidURL (a ValueError, not an OSError). Re-encoding
    defensively first turns it into a fetch that either succeeds or fails as an ordinary
    HTTP error the tool already handles. Already-encoded octets (`%`) and the URL's
    structural characters pass through unchanged, so a well-formed URL is a no-op.
    """
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%.")


def _web_search(args: dict) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "ERROR: no query given."
    url = _SEARCH_URL + "?" + urllib.parse.urlencode({"q": query})
    try:
        body, _ = _http_get(url, 30)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return f"ERROR: search failed ({exc})"
    titles = _RESULT_RE.findall(body)
    snippets = _SNIPPET_RE.findall(body)
    lines, rank = [], 0
    for i, (href, title) in enumerate(titles[:12]):
        url = _unwrap(href)
        # Sponsored hits route through DuckDuckGo's own ad redirector and are never the
        # episode guide we are after.
        if "duckduckgo.com/y.js" in url or "bing.com/aclick" in url:
            continue
        rank += 1
        lines.append(f"{rank}. {_detag(title)}\n   {url}")
        if i < len(snippets):
            lines.append(f"   {_detag(snippets[i])[:400]}")
    if not lines:
        # Either a genuinely-empty query or — far more often — DuckDuckGo serving its
        # HTTP-202 bot-challenge page instead of results (it rate-limits this host). In
        # both cases "no results" is a dead end that makes the model spin on re-worded
        # queries, so steer it to the direct lookups that always work: TMDB's HTML search
        # carries the id in each result link, Wikidata's API answers by name.
        q = urllib.parse.quote(query)
        return ("(no results; the search backend is rate-limited or returned nothing. "
                "Do NOT retry with re-worded search queries — instead WebFetch one of "
                f"these directly: https://www.themoviedb.org/search/tv?query={q} "
                f"(or /search/movie?query={q}); "
                "https://www.wikidata.org/w/api.php?action=wbsearchentities"
                f"&search={q}&language=en&format=json . Read the /tv/NNNN-slug or "
                "/movie/NNNN-slug ids out of the links.)")
    return _truncate("\n".join(lines))


_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_FANDOM_RE = re.compile(r"^https?://([a-z0-9-]+)\.fandom\.com/wiki/(.+)$", re.I)


def _fandom_wikitext(url: str) -> str | None:
    """The MediaWiki source of a Fandom page, or None if this is not one / it failed.

    Fandom serves 403 to anything it does not take for a browser, and the fandom
    episode wikis are the single most-cited source in this fleet's prompts -- an
    episode's real title, its adapted chapters, whether it is filler. The `api.php`
    endpoint on the same host answers the same question without the bot check, and
    the raw wikitext is *better* input than the rendered page: the infobox fields
    (Translation, Chapters, Air Date) arrive as labelled key/value lines instead of
    being buried in table markup.
    """
    m = _FANDOM_RE.match(url)
    if not m:
        return None
    wiki, page = m.group(1), urllib.parse.unquote(m.group(2)).replace("_", " ")
    api = (f"https://{wiki}.fandom.com/api.php?"
           + urllib.parse.urlencode({"action": "parse", "page": page,
                                     "prop": "wikitext", "format": "json"}))
    try:
        body, _ = _http_get(api, 45)
        text = json.loads(body)["parse"]["wikitext"]["*"]
    except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError,
            json.JSONDecodeError):
        return None
    return text or None


def _web_fetch(args: dict) -> str:
    url = str(args.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return "ERROR: url must be an absolute http(s) URL."
    url = _encode_url(url)
    try:
        body, ctype = _http_get(url, 45)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        wikitext = _fandom_wikitext(url)
        if wikitext:
            return _truncate(wikitext)
        return f"ERROR: fetch failed ({exc})"
    if "json" in ctype:
        return _truncate(body)
    text = _SCRIPT_RE.sub(" ", body)
    text = _detag(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return _truncate(text.strip() or "(page had no readable text)")


def _dispatch(name: str, args: dict, cwd: str) -> str:
    if name == "Probe":
        return _probe(args)
    if name == "ListDir":
        return _list_dir(args)
    if name == "Jellyfin":
        return _jellyfin(args)
    if name == "Read":
        return _read(args)
    if name == "Write":
        return _write(args)
    if name == "Edit":
        return _edit(args)
    if name == "Glob":
        return _glob(args, cwd)
    if name == "Grep":
        return _grep(args, cwd)
    if name == "WebSearch":
        return _web_search(args)
    if name == "WebFetch":
        return _web_fetch(args)
    return f"ERROR: unknown tool {name!r}."


# --- the conversation --------------------------------------------------------

SYSTEM_PROMPT = """\
You are the automation engine for a self-hosted Jellyfin media library. You work \
unattended: there is no human to ask, so make the best decision the evidence supports \
and say what you were unsure about.

Use the tools to gather evidence before deciding. Look at the actual files rather than \
guessing from names when the two could disagree, and look a fact up on the web rather \
than recalling it when the task names a specific episode, title or id.

When the task tells you to write a file at an exact path, that file IS your answer: \
write it with the Write tool, as strict JSON with no markdown fences around it, before \
you finish. A reply that describes the JSON instead of writing it has failed the task.

Never invent a title, plot or id. If you genuinely cannot establish one, leave it out \
and say so.

You have no shell and no way to delete or move a media file, by design. Sidecars \
(.nfo, artwork) and your own output files are yours to write; the media itself is not. \
If a task seems to need a file removed or relocated -- a duplicate, a stub, a \
misfiled episode -- that is a decision for a human: name the exact paths in your \
summary and leave them alone. Do not treat a refusal as an obstacle to work around.

When you have nothing left to do, stop calling tools and reply with a short \
plain-English summary of the calls you made and why.
"""

# Roughly 60-80k tokens of transcript. Past this the OLDEST tool results are replaced
# with a placeholder -- never the messages themselves, because the provider rejects a
# request in which an assistant `tool_calls` block has no matching `tool` reply.
MAX_CONTEXT_CHARS = 240_000
ELIDED = "[earlier tool output dropped to stay within the context window]"


def _transcript_chars(messages: list) -> int:
    return sum(len(json.dumps(m)) for m in messages)


def _elide_oldest(messages: list) -> bool:
    """Blank the oldest un-elided tool result. Returns False when there is none left."""
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("content") != ELIDED:
            msg["content"] = ELIDED
            return True
    return False


def _trim(messages: list, limit: int = MAX_CONTEXT_CHARS) -> None:
    while _transcript_chars(messages) > limit and _elide_oldest(messages):
        pass


def _post(payload: dict, key: str, timeout: int, base_url: str = API_URL,
          on_event=None) -> dict:
    """One completion, with transport retry. Raises AIUnavailable for a dead credential."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url, data=body, method="POST",
        # A browser User-Agent is required: Groq (and Cloudflare-gated hosts) 403 the
        # default `Python-urllib` UA with a Cloudflare 1010 block, which would otherwise
        # look like a dead credential and skip the provider before it is ever tried.
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": _UA})

    last = None
    waited = 0.0
    attempt = 0
    while attempt < HTTP_ATTEMPTS:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:    # noqa: S310
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            # 401 is a rejected key, 402 an exhausted balance, 403 a disabled account.
            # None is fixed by trying again, and all three mean the run never happened --
            # so they take the deferral path instead of burning the caller's retries.
            if exc.code in (401, 402, 403):
                raise AIUnavailable(
                    f"http {exc.code}: {detail}"
                    + (" (insufficient balance)" if exc.code == 402 else "")) from exc
            # A rate limit is a clock-gated door -- but the clock is not always a DAY, and
            # treating a twelve-second door as a daily one is what kept the fleet's largest
            # free budget switched off.
            #
            # Groq's free tier is 200,000 tokens a day against 8,000 per MINUTE. A run that
            # stays under 8,000 tokens per request is perfectly servable; it just
            # has to wait out the rolling window between turns. Failing over to the next
            # provider instead -- which is what happened for months -- burns a provider
            # with a whole day's budget left over a pause the provider itself measured and
            # told us the length of.
            #
            # So: when the provider states how long to wait and it is short, WAIT. When it
            # does not, or the wait is long, it is a daily door and the caller should move
            # on. The distinction is the provider's own number, not a guess.
            if exc.code in (429, 413):
                wait = _retry_after_sec(exc, detail)
                if (wait is not None and wait <= MAX_RATE_LIMIT_WAIT_SEC
                        and waited + wait <= MAX_RATE_LIMIT_WAIT_TOTAL_SEC
                        and not _request_exceeds_limit(detail)):
                    waited += wait
                    if on_event:
                        on_event(f"[rate limit] waiting {wait:.0f}s for the provider's "
                                 f"token window ({waited:.0f}s total)")
                    time.sleep(wait)
                    attempt -= 1          # a wait is not a try; it costs no attempt
                    continue
                if exc.code == 429:
                    raise AIUnavailable(f"http 429 rate limit: {detail}") from exc
            last = f"http {exc.code}: {detail}"
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last = f"connection error: {exc}"
        if attempt < HTTP_ATTEMPTS:
            time.sleep(HTTP_BACKOFF_SEC * attempt)
    raise RuntimeError(last or "request failed")


# How many times one IDENTICAL tool call (same tool, same arguments) may be made before
# the harness tells the model it is looping. Three is deliberate: twice can be a legitimate
# re-read after something changed, and nothing this runtime exposes changes between calls
# anyway. Measured: a single run made the same Grep six times in a row, then the same
# ListDir twice more, and wrote nothing.
_LOOP_REPEAT_LIMIT = 3

# How many times a run that finishes without writing its required output file is asked to
# write it. Two is enough to convert the common case (the model simply answered in prose)
# without letting a model that cannot produce the file spin forever.
_REQUIRE_FILE_MAX_PROMPTS = 2

# Extra seconds granted past a reached deadline, once, purely so the model can WRITE its
# output file. The deadline exists to stop a run investigating forever, not to throw away
# an answer it already has.
_DEADLINE_WRITE_GRACE_SEC = 60

# Where in the turn budget to remind the model what it has left, and what to do about it.
# A free model does not track its own turn count, and the one failure mode that produces
# NOTHING -- rather than something wrong, which the harness can reject and feed back -- is
# running out of turns mid-investigation.
_TURN_PRESSURE = (
    (0.55, "HARNESS NOTICE: you have used {used} of your {total} tool turns. Begin "
           "converging. Stop broad investigation and gather only what you still "
           "genuinely need."),
    (0.80, "HARNESS NOTICE: {left} tool turns remain of {total}. STOP INVESTIGATING NOW "
           "and produce your final output -- write the file you were asked to write, "
           "with your best answer from what you already know. An incomplete answer that "
           "is written is worth infinitely more than a perfect one that never gets "
           "written, and a run that ends without writing its file is a total failure "
           "that tells the harness nothing. Do it in your next message."),
)


def run_agent(prompt: str, allowed_tools=DEFAULT_TOOLS, max_turns: int = 40,
              model: str = "", cwd: str = "", deadline: float | None = None,
              on_event=None, base_url: str = "", key: str = "",
              require_file: str = "", max_context_chars: int = 0) -> dict:
    """Run one agent conversation to completion.

    Returns {"result": <final assistant text>, "num_turns": int, "tool_calls": int,
    "usage": {...}, "stop_reason": str}. Raises AIUnavailable when the API refused the
    credential, RuntimeError for anything else terminal.

    `base_url`/`key` select a provider (a free fallback in the identify chain). When
    empty the free OpenRouter defaults are used, so a caller that never names a provider
    still runs on the free tier.

    `deadline` is an absolute `time.monotonic()` value; the loop stops cleanly at it
    rather than being killed mid-tool by the caller's subprocess timeout, so whatever
    the model had already written to disk is still there and the reason is reported.
    `on_event`, if given, receives one-line progress strings (the runner logs them to
    stderr, where each caller's existing failure-detail plumbing already looks).

    `max_context_chars` is the PROVIDER's own ceiling, when one has been measured. The
    default 240,000 is a context-window bound and says nothing about a free tier's
    tokens-per-MINUTE allowance: groq serves 8,000 tokens a minute, about 21,900
    characters, so a transcript trimmed at 240,000 blows the limit on the third turn no
    matter how small the prompt was. Two things scale with it -- how large a single tool
    result may be, and when the oldest results start being elided -- because a 24,000-char
    web page is three times the whole allowance on its own.
    """
    key = key or api_key()
    base_url = base_url or API_URL
    cwd = cwd or os.getcwd()
    # Leave headroom under the stated ceiling: the tool SCHEMAS travel with every request
    # and count against the same allowance, and they are not in `messages`.
    ctx_limit = int(max_context_chars * 0.85) if max_context_chars else MAX_CONTEXT_CHARS
    tool_limit = min(MAX_TOOL_RESULT_CHARS, max(1_500, ctx_limit // 6))
    names = [t for t in allowed_tools if t in TOOL_SCHEMAS]
    tools = [TOOL_SCHEMAS[n] for n in names]

    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}]
    tool_calls_made = 0
    final_text = ""
    stop_reason = "stop"
    usage: dict = {}
    # (tool name, exact arguments) -> how many times it has been called. A free model
    # with no memory of its own loop will re-run the same Grep forever; see
    # `_LOOP_REPEAT_LIMIT`.
    call_counts: dict = {}
    nudged: set = set()
    file_prompts = 0

    started = time.monotonic()
    total_budget = (deadline - started) if deadline is not None else None

    for turn in range(1, max(1, max_turns) + 1):
        if deadline is not None and time.monotonic() >= deadline:
            # LAST CHANCE BEFORE THE CLOCK. A deadline stop with no output file is the
            # same total failure as running out of turns, and for a big pack the DEADLINE
            # is the binding constraint, not the turn ceiling: identify allows 120 turns
            # but only 900s + 30s/file, and a run that leans on web search reaches the
            # clock around turn 60. Spend one final turn asking for the file rather than
            # returning nothing at all.
            if (require_file and not os.path.exists(require_file)
                    and file_prompts < _REQUIRE_FILE_MAX_PROMPTS):
                file_prompts += 1
                deadline += _DEADLINE_WRITE_GRACE_SEC
                if on_event:
                    on_event(f"turn {turn}: deadline reached with no {require_file} "
                             f"-- granting {_DEADLINE_WRITE_GRACE_SEC}s to write it")
                messages.append({"role": "user", "content": (
                    f"You are OUT OF TIME. Write the file you were asked to produce, at "
                    f"{require_file}, in this turn and nothing else. Use your best answer "
                    f"from what you already know -- do not investigate, do not explain. "
                    f"A file written now is the only thing that counts; anything else is "
                    f"a total loss.")})
                continue
            stop_reason = "deadline"
            break

        # TURN BUDGET PRESSURE. A run that spends every turn investigating and none
        # writing produces nothing at all -- rc 0, no output file, and the caller can only
        # call it "transient" and retry into the same wall. Measured 2026-09-10 on
        # Monogatari: openrouter/nemotron burned all 40 turns (six identical Greps in a
        # row among them) and wrote no plan, twice, for ~9 minutes each.
        #
        # So the budget is made VISIBLE, twice, as a user turn the model cannot miss. This
        # is not a limit change -- it is telling the model what its limit already is.
        # Pressure follows whichever budget is closer to running out. Turns alone is the
        # wrong signal: identify allows 120 turns but only ~31 minutes for a 32-file wave,
        # and a web-heavy run reaches the clock around turn 60 -- so turn-based notices at
        # 66 and 96 would never fire at all.
        spent = max(
            turn / float(max_turns),
            ((time.monotonic() - started) / total_budget) if total_budget else 0.0,
        )
        for frac, msg in _TURN_PRESSURE:
            if spent >= frac and frac not in nudged:
                nudged.add(frac)
                messages.append({"role": "user", "content": msg.format(
                    used=turn, total=max_turns, left=max(0, max_turns - turn))})

        _trim(messages, ctx_limit)

        payload = {
            "model": model or DEFAULT_MODEL,
            "messages": messages,
            "max_tokens": MAX_OUTPUT_TOKENS,
            # Judgment work, not prose: a low temperature keeps a re-run of the same
            # pack from landing on a different season numbering than the last one.
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools

        # Leave the HTTP call a real budget even near the deadline: a request cut off at
        # 1s produces nothing, while one allowed to finish may be the turn that writes
        # the plan.
        http_timeout = 300
        if deadline is not None:
            http_timeout = max(60, min(300, int(deadline - time.monotonic())))

        data = _post(payload, key, http_timeout, base_url, on_event=on_event)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        calls = msg.get("tool_calls") or []
        text = (msg.get("content") or "").strip()

        if not calls:
            # THE RUN IS ONLY DONE IF IT PRODUCED WHAT IT WAS ASKED FOR. A model that
            # finishes with prose instead of writing its output file is the worst
            # outcome available: rc 0, nothing on disk, and a caller that can only call
            # it "transient" and retry into the same wall. Measured 2026-09-10 --
            # openrouter/nemotron investigated Monogatari for nine minutes, looped on an
            # identical Grep, then replied in text and wrote no plan. Twice.
            #
            # Asking once, plainly, costs one turn and converts a total failure into a
            # likely success. It is asked at most `_REQUIRE_FILE_MAX_PROMPTS` times so a
            # model that cannot write it never becomes an infinite loop.
            if (require_file and not os.path.exists(require_file)
                    and file_prompts < _REQUIRE_FILE_MAX_PROMPTS):
                file_prompts += 1
                if on_event:
                    on_event(f"turn {turn}: finished WITHOUT writing {require_file} "
                             f"-- asking for it ({file_prompts}/"
                             f"{_REQUIRE_FILE_MAX_PROMPTS})")
                messages.append({"role": "assistant", "content": text or "(no text)"})
                messages.append({"role": "user", "content": (
                    f"You have not written the file you were asked to produce. It does "
                    f"not exist at:\n{require_file}\n\n"
                    f"Nothing you have said so far is usable to the system that called "
                    f"you -- it reads that file and nothing else, so this run has "
                    f"produced NOTHING. Write it NOW, in this turn, using the Write tool "
                    f"and the exact schema you were given. Use your best answer from what "
                    f"you already know; do not investigate further, and do not reply with "
                    f"prose instead. If you genuinely cannot decide some detail, write "
                    f"the file anyway with your best judgement for it.")})
                final_text = text or final_text
                continue
            final_text = text
            stop_reason = choice.get("finish_reason") or "stop"
            if on_event:
                on_event(f"turn {turn}: done ({stop_reason})")
            break

        # Keep the assistant message verbatim: the ids in `tool_calls` are what the
        # replies below are matched against.
        messages.append({"role": "assistant", "content": text, "tool_calls": calls})
        if text:
            final_text = text

        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                result = f"ERROR: could not parse the arguments you sent: {raw[:200]}"
            else:
                if name not in names:
                    result = (f"ERROR: {name} is not available in this run. "
                              f"Available tools: {', '.join(names)}.")
                else:
                    # The safety net: a single tool call must never be able to crash
                    # the whole run. Each tool already returns "ERROR: ..." strings for
                    # the failures it expects; this catches the rest (a ValueError the
                    # tool didn't anticipate, an unexpected NotImplementedError, ...) and
                    # turns them into a recoverable tool result instead of an exception
                    # that aborts the run and fails the torrent.
                    try:
                        result = _dispatch(name, args, cwd)
                    except Exception as exc:                              # noqa: BLE001
                        result = (f"ERROR: tool {name} raised "
                                  f"{type(exc).__name__}: {exc}")
                    tool_calls_made += 1
            # LOOP BREAKER. An identical call returns an identical result, and a model
            # that cannot tell it is repeating itself will do so until its turns run out.
            # Saying so in the tool result is the only channel that reaches it mid-run.
            sig = (name, raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True))
            call_counts[sig] = call_counts.get(sig, 0) + 1
            if call_counts[sig] >= _LOOP_REPEAT_LIMIT:
                result = (
                    f"NOTE FROM THE HARNESS: you have now made this exact {name} call "
                    f"{call_counts[sig]} times and the result has not changed. It will not "
                    f"change -- nothing is modifying it between your calls. Repeating it "
                    f"again will only spend the turns you have left. Use what you already "
                    f"have, and if something you looked for is genuinely absent, say so in "
                    f"your answer and proceed without it.\n\n"
                    f"(the unchanged result follows)\n{result[:1500]}")
                if on_event:
                    on_event(f"turn {turn}: {name} REPEATED x{call_counts[sig]} "
                             f"-- loop breaker fired")
            elif on_event:
                on_event(f"turn {turn}: {name} -> {len(result)} chars")
            # Second, tighter truncation against the PROVIDER's allowance. The tools
            # already cap themselves at MAX_TOOL_RESULT_CHARS, which is a context-window
            # number; on a provider serving 8,000 tokens a minute a single 24,000-char web
            # page is three times the entire per-request budget, and the run dies on the
            # turn after it fetches one. `_trim` would elide it eventually, but only after
            # the request it broke.
            if len(result) > tool_limit:
                result = _truncate(result, tool_limit)
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": result})
    else:
        stop_reason = "max_turns"

    return {
        "result": final_text,
        "num_turns": tool_calls_made,
        "tool_calls": tool_calls_made,
        "stop_reason": stop_reason,
        "usage": usage,
        "model": model or DEFAULT_MODEL,
    }


def complete(prompt: str, model: str = "", max_tokens: int = 1024,
             system: str = "", base_url: str = "", key: str = "") -> str:
    """One plain completion, no tools. For the tasks that are pure judgment over text
    already in the prompt -- there is nothing to inspect, so an agent loop would just be
    an expensive way to make a single call.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    data = _post({"model": model or DEFAULT_MODEL, "messages": messages,
                  "max_tokens": max_tokens, "temperature": 0.2},
                 key or api_key(), 180, base_url or API_URL)
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()


def main() -> int:
    import sys
    prompt = " ".join(sys.argv[1:]) or sys.stdin.read()
    out = run_agent(prompt, on_event=lambda m: print(m, file=sys.stderr))
    print(out["result"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
