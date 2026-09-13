#!/usr/bin/env python3
"""Regression test: turn-budget pressure and the tool loop breaker (2026-09-10).

THE FAILURE. Identify runs were ending with rc 0 and NO plan file -- the one failure mode
that tells the harness nothing, because there is no wrong answer to reject and feed back,
just silence. The caller could only call it transient and retry into the same wall. The
fleet's old hand-off notes recorded it as "6-10 minute runs ending in empty text,
unexplained".

The runtime's own turn log explained it. On `[MTBB] Monogatari Series (BD 1080p)`:

    turn 13: Grep -> 375 chars      turn 22: Grep -> 375 chars
    turn 14: Grep -> 375 chars      turn 23: Grep -> 375 chars
    ... six identical Greps in a row, then more, then ListDir twice ...

The model was looping on an identical call, and burned all 40 turns without ever writing
its output. Two things were missing, and both are things the model cannot know on its own:

  1. It does not track its turn count, so it cannot tell it is about to run out.
  2. It cannot tell that a result is identical to one it already has.

BOTH DIRECTIONS (§4.5):
  Part 1 -- a repeated identical call trips the breaker, and the model is told so.
  Part 2 -- ordinary varied work is NEVER touched: different arguments, different tools,
            and two repeats (a legitimate re-read) all pass through untouched.
  Part 3 -- the turn-pressure notices fire once each, at the right points.

    python3 scripts/test_agent_loop_guard.py

No network: the HTTP layer is stubbed.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_client                                                     # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def run(script, max_turns=10):
    """Drive run_agent with a scripted sequence of model replies.

    `script` is a list of tool-call argument dicts (or None to finish). Returns
    (events, tool_results_seen_by_model).
    """
    seen, events = [], []
    state = {"i": 0}

    def fake_post(payload, key, timeout, base_url, on_event=None):
        # record what the model was shown since its last turn
        for m in payload["messages"]:
            if m.get("role") == "tool" and m["content"] not in seen:
                seen.append(m["content"])
        i = state["i"]
        state["i"] += 1
        if i >= len(script) or script[i] is None:
            return {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                    "usage": {}}
        args = script[i]
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": f"c{i}", "function": {"name": "ListDir",
                                         "arguments": json.dumps(args)}}]}}], "usage": {}}

    real_post, real_dispatch = ai_client._post, ai_client._dispatch
    try:
        ai_client._post = fake_post
        ai_client._dispatch = lambda name, args, cwd: f"RESULT for {args.get('path')}"
        ai_client.run_agent("go", allowed_tools=["ListDir"], max_turns=max_turns,
                            key="k", base_url="http://x", on_event=events.append)
    finally:
        ai_client._post, ai_client._dispatch = real_post, real_dispatch
    return events, seen


print("Part 1 -- an identical call repeated trips the breaker")
same = [{"path": "/same"}] * 6
events, seen = run(same)
fired = [e for e in events if "loop breaker fired" in e]
check("the breaker fires", bool(fired), True)
check(f"it fires from the {ai_client._LOOP_REPEAT_LIMIT}rd identical call on",
      len(fired), len(same) - (ai_client._LOOP_REPEAT_LIMIT - 1))
check("the model is TOLD it is repeating",
      any("you have now made this exact" in s for s in seen), True)
check("and told the result will not change",
      any("It will not change" in s for s in seen), True)

print("\nPart 2 -- ordinary varied work is untouched")
varied = [{"path": f"/dir{i}"} for i in range(6)]
events, seen = run(varied)
check("six DIFFERENT calls never trip it",
      any("loop breaker" in e for e in events), False)
check("their results are passed through verbatim",
      all(s.startswith("RESULT for") for s in seen), True)
twice = [{"path": "/same"}, {"path": "/same"}]
events, _ = run(twice)
check("a legitimate second look is not a loop",
      any("loop breaker" in e for e in events), False)

print("\nPart 3 -- the turn budget is made visible, once each")
events, seen = run([{"path": f"/d{i}"} for i in range(20)], max_turns=20)
notices = [s for s in seen if "HARNESS NOTICE" in s]
# the notices are user messages, not tool results, so look at what the model was sent
_, all_msgs = run([{"path": f"/d{i}"} for i in range(20)], max_turns=20)


def pressure_texts(max_turns=20):
    got = []

    def fake_post(payload, key, timeout, base_url, on_event=None):
        for m in payload["messages"]:
            if m.get("role") == "user" and "HARNESS NOTICE" in (m.get("content") or ""):
                if m["content"] not in got:
                    got.append(m["content"])
        i = len([x for x in payload["messages"] if x.get("role") == "assistant"])
        if i >= max_turns:
            return {"choices": [{"message": {"content": "done"}}], "usage": {}}
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": f"c{i}", "function": {"name": "ListDir",
                                         "arguments": json.dumps({"path": f"/d{i}"})}}]}}],
                "usage": {}}

    real_post, real_dispatch = ai_client._post, ai_client._dispatch
    try:
        ai_client._post = fake_post
        ai_client._dispatch = lambda n, a, c: "ok"
        ai_client.run_agent("go", allowed_tools=["ListDir"], max_turns=max_turns,
                            key="k", base_url="http://x")
    finally:
        ai_client._post, ai_client._dispatch = real_post, real_dispatch
    return got


texts = pressure_texts()
check("both pressure notices are delivered", len(texts), len(ai_client._TURN_PRESSURE))
check("the first says to converge",
      any("Begin converging" in t for t in texts), True)
check("the last says to STOP and write",
      any("STOP INVESTIGATING NOW" in t for t in texts), True)
check("and explains why an unwritten answer is worthless",
      any("never gets written" in t for t in texts), True)
check("each is delivered exactly once", len(texts), len(set(texts)))

print("\nPart 4 -- a run that finishes without its output file is asked for it")
import os, tempfile, json as _json


def run_requiring(write_on_prompt):
    """Drive a model that replies with prose; optionally writes the file when asked."""
    td = tempfile.mkdtemp()
    target = os.path.join(td, "plan.json")
    events, state = [], {"i": 0}

    def fake_post(payload, key, timeout, base_url, on_event=None):
        state["i"] += 1
        asked = sum(1 for m in payload["messages"]
                    if m.get("role") == "user"
                    and "You have not written the file" in (m.get("content") or ""))
        if asked and write_on_prompt:
            Path(target).write_text('{"files": []}')
        return {"choices": [{"message": {"content": "here is my answer in prose"},
                             "finish_reason": "stop"}], "usage": {}}

    real_post = ai_client._post
    try:
        ai_client._post = fake_post
        out = ai_client.run_agent("go", allowed_tools=["ListDir"], max_turns=10,
                                  key="k", base_url="http://x", on_event=events.append,
                                  require_file=target)
    finally:
        ai_client._post = real_post
    return events, os.path.exists(target), state["i"]


events, wrote, calls = run_requiring(write_on_prompt=True)
check("the model is asked for the missing file",
      any("finished WITHOUT writing" in e for e in events), True)
check("and it gets written", wrote, True)

events, wrote, calls = run_requiring(write_on_prompt=False)
check("a model that never writes it is asked a bounded number of times",
      sum(1 for e in events if "finished WITHOUT writing" in e),
      ai_client._REQUIRE_FILE_MAX_PROMPTS)
check("and the run still terminates", wrote, False)

# and a run that DOES write it first time is never nagged
def run_writing_immediately():
    td = tempfile.mkdtemp(); target = os.path.join(td, "plan.json")
    events = []

    def fake_post(payload, key, timeout, base_url, on_event=None):
        Path(target).write_text("{}")
        return {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {}}
    real_post = ai_client._post
    try:
        ai_client._post = fake_post
        ai_client.run_agent("go", allowed_tools=["ListDir"], max_turns=10, key="k",
                            base_url="http://x", on_event=events.append,
                            require_file=target)
    finally:
        ai_client._post = real_post
    return events


check("a run that writes its file is never nagged",
      any("finished WITHOUT" in e for e in run_writing_immediately()), False)
check("and require_file='' disables the whole mechanism",
      any("finished WITHOUT" in e for e in run([None])[0]), False)

print("\nPart 5 -- the DEADLINE is a budget too, and gets a last chance to write")
import time as _t


def run_with_deadline(budget_sec, write_when_out_of_time):
    """A model that only ever calls tools; the wall clock is what stops it."""
    td = tempfile.mkdtemp(); target = os.path.join(td, "plan.json")
    events, seen_user = [], []

    def fake_post(payload, key, timeout, base_url, on_event=None):
        for m in payload["messages"]:
            if m.get("role") == "user" and m["content"] not in seen_user:
                seen_user.append(m["content"])
        if write_when_out_of_time and any("OUT OF TIME" in u for u in seen_user):
            Path(target).write_text('{"files": []}')
            return {"choices": [{"message": {"content": "written"},
                                 "finish_reason": "stop"}], "usage": {}}
        _t.sleep(0.05)
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c", "function": {"name": "ListDir",
                                     "arguments": _json.dumps({"path": "/x"})}}]}}],
                "usage": {}}

    real_post, real_dispatch = ai_client._post, ai_client._dispatch
    real_grace = ai_client._DEADLINE_WRITE_GRACE_SEC
    try:
        ai_client._post = fake_post
        ai_client._dispatch = lambda n, a, c: "ok"
        ai_client._DEADLINE_WRITE_GRACE_SEC = 0.4   # the real 60s would take minutes here
        ai_client.run_agent("go", allowed_tools=["ListDir"], max_turns=10000,
                            key="k", base_url="http://x", on_event=events.append,
                            require_file=target,
                            deadline=_t.monotonic() + budget_sec)
    finally:
        ai_client._post, ai_client._dispatch = real_post, real_dispatch
        ai_client._DEADLINE_WRITE_GRACE_SEC = real_grace
    return events, seen_user, os.path.exists(target)


import json as _json
events, users, wrote = run_with_deadline(1.5, write_when_out_of_time=True)
check("wall-clock pressure fires even with turns to spare",
      any("HARNESS NOTICE" in u for u in users), True)
check("the deadline grants a last chance to write",
      any("granting" in e and "to write it" in e for e in events), True)
check("the model is told it is OUT OF TIME",
      any("OUT OF TIME" in u for u in users), True)
check("and the file gets written", wrote, True)

events, users, wrote = run_with_deadline(1.5, write_when_out_of_time=False)
check("a model that still will not write it is bounded",
      sum(1 for e in events if "granting" in e) <= ai_client._REQUIRE_FILE_MAX_PROMPTS,
      True)
check("and the run terminates anyway", wrote, False)

print("\nPart 6 -- a per-MINUTE token limit is a PAUSE, a daily one is a door")
#
# Groq's free tier is 200,000 tokens a DAY against 8,000 a MINUTE, and the fleet used to
# read both as "this provider is unavailable" -- so it abandoned a provider with a whole
# day's budget left over a twelve-second pause the provider itself had measured. That is
# what made groq, the only provider that was up on 2026-09-12, unable to file anything.
#
# Three things have to stay true, and the third is the one that keeps the fleet moving:
# a short stated wait is slept through, an UNSTATED or long wait fails over, and a single
# request bigger than the whole allowance is never waited on at all -- no amount of
# waiting shrinks it, and pretending otherwise would hang every judgment call behind it.
import urllib.error as _ue


class _Resp:
    def __init__(self, body, headers=None):
        self._body = body.encode()
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body, headers=None):
    return _ue.HTTPError("http://x", code, "err", headers or {}, _Resp(body))


_TPM_SHORT = ('{"error":{"message":"Rate limit reached for model X on tokens per minute '
              '(TPM): Limit 8000, Used 7000, Requested 1500. Please try again in 11.3s.",'
              '"code":"rate_limit_exceeded"}}')
_TPD_LONG = ('{"error":{"message":"Rate limit reached on tokens per day (TPD): Limit '
             '200000, Used 195693, Requested 5693. Please try again in 9m58.752s.",'
             '"code":"rate_limit_exceeded"}}')
_TOO_BIG = ('{"error":{"message":"Request too large on tokens per minute (TPM): Limit '
            '8000, Requested 8763, please reduce your message size.",'
            '"code":"rate_limit_exceeded"}}')

check("a stated short wait is understood",
      round(ai_client._retry_after_sec(_http_error(429, _TPM_SHORT), _TPM_SHORT)), 12)
check("a Retry-After header wins over the body",
      ai_client._retry_after_sec(_http_error(429, _TPM_SHORT, {"Retry-After": "7"}),
                                 _TPM_SHORT), 7.0)
check("a DAILY cap states a long wait, over the per-call ceiling",
      ai_client._retry_after_sec(_http_error(429, _TPD_LONG), _TPD_LONG)
      > ai_client.MAX_RATE_LIMIT_WAIT_SEC, True)
check("a rate limit that says nothing is not waited on",
      ai_client._retry_after_sec(_http_error(429, "slow down"), "slow down"), None)
check("a request bigger than the whole allowance is never waited on",
      ai_client._request_exceeds_limit(_TOO_BIG), True)
check("a request UNDER the allowance is only a pause",
      ai_client._request_exceeds_limit(_TPM_SHORT), False)

# End to end: one short refusal is slept through and the call then succeeds.
_slept = []
_real_sleep, _real_post_ok = ai_client.time.sleep, None
_state = {"n": 0}


def _flaky_urlopen(req, timeout=None):
    _state["n"] += 1
    if _state["n"] == 1:
        raise _http_error(429, _TPM_SHORT)
    return _Resp(_json.dumps({"choices": [{"message": {"content": "done"},
                                           "finish_reason": "stop"}], "usage": {}}))


_real_urlopen = ai_client.urllib.request.urlopen
try:
    ai_client.time.sleep = _slept.append
    ai_client.urllib.request.urlopen = _flaky_urlopen
    _out = ai_client.run_agent("go", allowed_tools=[], max_turns=2, key="k",
                               base_url="http://x")
finally:
    ai_client.time.sleep = _real_sleep
    ai_client.urllib.request.urlopen = _real_urlopen

check("the call is retried after the pause rather than failing over",
      _out.get("result"), "done")
check("and it actually slept roughly what the provider asked",
      bool(_slept) and 10 <= _slept[0] <= 14, True)

# The transcript budget follows the PROVIDER's ceiling, not the context window. Without
# this a run whose opening prompt fits comfortably dies two turns later on an accumulated
# transcript nothing was trimming -- measured on groq, where one 24,000-char web page is
# three times the entire per-minute allowance.
def _tool_result_chars(ceiling):
    """How many characters of a huge tool result survive into the next request."""
    seen, state = [], {"n": 0}

    def fake_post(payload, key, timeout, base_url, on_event=None):
        state["n"] += 1
        seen.append([m for m in payload["messages"] if m.get("role") == "tool"])
        if state["n"] == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "1", "type": "function",
                 "function": {"name": "ListDir", "arguments": '{"path": "/tmp"}'}}]},
                "finish_reason": "tool_calls"}], "usage": {}}
        return {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {}}

    real_post, real_dispatch = ai_client._post, ai_client._dispatch
    try:
        ai_client._post = fake_post
        ai_client._dispatch = lambda *a, **k: "x" * 60_000
        ai_client.run_agent("go", allowed_tools=["ListDir"], max_turns=3, key="k",
                            base_url="http://x", max_context_chars=ceiling)
    finally:
        ai_client._post, ai_client._dispatch = real_post, real_dispatch
    return len(seen[-1][0]["content"]) if seen and seen[-1] else 0


_capped = _tool_result_chars(21892)
_uncapped = _tool_result_chars(0)
check("a provider ceiling shrinks a huge tool result",
      _capped < ai_client.MAX_TOOL_RESULT_CHARS and _capped < _uncapped, True)
check("and what survives fits inside that ceiling",
      _capped < int(21892 * 0.85), True)
# `_truncate` appends a notice saying it truncated, so the result is the cap plus that
# line -- the point is that 60,000 characters did not reach the transcript.
check("with no ceiling the context-window cap still applies",
      ai_client.MAX_TOOL_RESULT_CHARS <= _uncapped
      <= ai_client.MAX_TOOL_RESULT_CHARS + 400, True)

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
