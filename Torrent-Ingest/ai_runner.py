#!/usr/bin/env python3
"""The command-line front end every daemon in the fleet spawns for a judgment call.

`config.AI_BIN` points here. A caller writes its prompt to stdin, names the tools the
task is allowed to use, and reads a JSON envelope back:

    {"result": "<the run's closing prose>", "is_error": false, "num_turns": 12,
     "stop_reason": "stop", "usage": {...}, "model": "nvidia/nemotron-3-super-120b-a12b:free"}

The daemons run the agent in a SUBPROCESS rather than importing `ai_client` in-process,
and that is deliberate. A run lasts up to ninety minutes and executes arbitrary shell on
the model's say-so; in-process, a wedged run would take the ingest daemon down with it
and `subprocess.run(timeout=...)` -- the only hard, kill-backed time bound available --
would not apply. Out of process, the worst a bad run can do is exit non-zero.

Exit codes, because the callers branch on them:
    0  the run finished; `result` holds its closing text
    1  the run failed in a way a retry might fix (a blip, a bad response)
    2  the run never happened: no credential, a rejected key, an exhausted balance.
       `config.identify_unavailable` matches the message, and every caller DEFERS its
       work instead of blaming the content for a failure that was never about it.

    echo "list /tmp" | python3 ai_runner.py -p --tools Bash --output-format json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import ai_client


def _resolve_provider(name: str):
    """`(base_url, key)` for a named free provider, or (None, None) when unresolved.

    Kept OUT of ai_client so the provider registry (and its key-file lookup) lives in one
    place (`config`), and the key never touches argv or the environment."""
    if not name:
        return None, None
    try:
        import config
        prov = config.ai_provider(name)
    except Exception:                                             # noqa: BLE001
        return None, None
    if not prov:
        return None, None
    return prov["base_url"], prov["key"]


def _attempts(provider: str, model: str):
    """The ordered attempts this run may try, as `[{provider, base_url, key, model}]`.

    An explicit `--provider` pins a single attempt, so a caller that knows what it wants
    (or a test pinning one model) keeps the old behaviour exactly.

    With NO provider named we walk `config.enabled_ai_attempts()`, which is the same chain
    `discovery.complete()` already walks. This is the fix for the failure where one capped
    model silenced every judgment call in the fleet: `media_doctor.escalate()` and
    `budget_available()` pass no provider, so they both rode `ai_client.DEFAULT_MODEL`
    alone -- and when that model hit its daily cap the healer reported "no budget" while
    other providers on the chain were answering normally.
    """
    if provider:
        base_url, key = _resolve_provider(provider)
        if base_url is None:
            return None                                   # caller reports the bad name
        return [{"provider": provider, "base_url": base_url, "key": key,
                 "model": model or ai_client.DEFAULT_MODEL}]
    try:
        import config
        chain = config.enabled_ai_attempts()
    except Exception:                                                     # noqa: BLE001
        chain = []
    if model:
        # A model without a provider: keep the caller's model, but try it on each
        # provider that can actually run rather than only the default one.
        seen, out = set(), []
        for a in chain:
            if a["base_url"] in seen:
                continue
            seen.add(a["base_url"])
            out.append({**a, "model": model})
        return out or None
    return chain or None


def _ceiling(provider: str) -> int:
    """The provider's measured prompt ceiling in characters, or 0 when none is known.

    A free tier's tokens-per-MINUTE allowance is a bound on the whole TRANSCRIPT, not
    just on the opening prompt, and `run_agent` needs it to know how big a tool result
    may be and when to start eliding. Without it a run whose prompt fits comfortably dies
    two turns later on an accumulated transcript nothing was trimming.
    """
    try:
        import config
        return int(config.load_prompt_ceilings().get(provider) or 0)
    except Exception:                                                 # noqa: BLE001
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description="Run one AI agent task.")
    ap.add_argument("-p", "--prompt", nargs="?", const="", default=None,
                    help="The task. Omit the value to read it from stdin.")
    ap.add_argument("--tools", default=",".join(ai_client.DEFAULT_TOOLS),
                    help="Comma-separated tool names the run may use.")
    ap.add_argument("--max-turns", type=int, default=40,
                    help="Ceiling on model turns before the run is cut off.")
    ap.add_argument("--model", default="", help="Model id for the provider.")
    ap.add_argument("--provider", default="",
                    help="Named free provider from config.AI_PROVIDERS (resolves its "
                         "base URL + key from disk). Empty = the free OpenRouter "
                         "default.")
    ap.add_argument("--timeout", type=float, default=0,
                    help="Wall-clock budget in seconds. The loop stops cleanly at it, "
                         "leaving whatever the run already wrote to disk intact.")
    ap.add_argument("--output-format", choices=("json", "text"), default="json")
    ap.add_argument("--cwd", default="", help="Working directory for Bash/Glob/Grep.")
    ap.add_argument("--verbose", action="store_true",
                    help="Log each tool call to stderr.")
    ap.add_argument("--require-file", default="",
                    help="Absolute path the run MUST write. If the model finishes "
                         "without it, it is asked once more to write it -- a run that "
                         "ends with prose instead of its output file has produced "
                         "nothing at all.")
    ap.add_argument("--require-list", default="",
                    help="Path to a JSON array of release basenames the plan at "
                         "--require-file must cover. A truncated plan is reported with "
                         "the exact missing slice before the run ends (HANDOFF 10.9).")
    args = ap.parse_args()

    require_files = None
    if args.require_list:
        try:
            loaded = json.loads(Path(args.require_list).read_text(encoding="utf-8"))
            if isinstance(loaded, list) and loaded:
                require_files = [str(x) for x in loaded]
        except (OSError, ValueError):
            require_files = None

    prompt = args.prompt if args.prompt else sys.stdin.read()
    if not prompt.strip():
        print("ai_runner: no prompt on the command line or stdin", file=sys.stderr)
        return 1

    attempts = _attempts(args.provider, args.model)
    if attempts is None:
        if args.provider:
            return _fail(f"unknown provider or missing key: {args.provider}",
                         args.output_format, 2)
        return _fail("no free AI provider configured: add a key under "
                     "~/.config/api-keys/", args.output_format, 2)

    tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    deadline = time.monotonic() + args.timeout if args.timeout > 0 else None
    on_event = (lambda m: print(m, file=sys.stderr)) if args.verbose else None

    # Walk the chain: a provider that cannot run (no key / capped / rate-limited) or that
    # returns no usable text is skipped for the next. `ran` distinguishes "every provider
    # is unavailable" (exit 2 -- callers DEFER) from "providers ran but none produced
    # anything" (exit 1 -- a retry might fix it).
    last_err = "no provider returned a result"
    ran = False
    for a in attempts:
        if deadline is not None and time.monotonic() >= deadline:
            last_err = "wall-clock budget exhausted before a provider answered"
            break
        try:
            out = ai_client.run_agent(prompt, allowed_tools=tools,
                                      max_turns=args.max_turns, model=a["model"],
                                      require_file=args.require_file,
                                      require_files=require_files,
                                      cwd=args.cwd, deadline=deadline, on_event=on_event,
                                      base_url=a["base_url"], key=a["key"],
                                      max_context_chars=_ceiling(a["provider"]))
        except ai_client.AIUnavailable as exc:
            last_err = f'{a["provider"]}/{a["model"]}: {exc}'
            continue
        except Exception as exc:                                          # noqa: BLE001
            ran = True
            last_err = f'{a["provider"]}/{a["model"]}: {type(exc).__name__}: {exc}'
            continue
        ran = True
        # An empty run is NOT success. escalate() branches on `result`, so returning exit 0
        # with blank text is read as "handled", which is how a show burned its escalation
        # budget on runs that never said anything (see METADATA-REPAIR notes).
        if not (out.get("result") or "").strip():
            last_err = f'{a["provider"]}/{a["model"]} returned empty text'
            continue
        if args.output_format == "json":
            print(json.dumps({**out, "is_error": False,
                              "provider": a["provider"], "model": a["model"]}))
        else:
            print(out["result"])
        return 0

    return _fail(last_err, args.output_format, 1 if ran else 2)


def _fail(detail: str, output_format: str, code: int) -> int:
    """Report a failure on BOTH streams.

    Callers differ in where they look: the identify paths classify on the `result` text
    pulled out of the JSON envelope, while the cheap budget pings just scan stdout+stderr
    for limit wording. Writing the detail to both means no caller has to know which one
    this failure came out of.
    """
    if output_format == "json":
        print(json.dumps({"result": detail, "is_error": True, "num_turns": 0,
                          "stop_reason": "error", "usage": {}}))
    else:
        print(detail)
    print(f"ai_runner: {detail}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
