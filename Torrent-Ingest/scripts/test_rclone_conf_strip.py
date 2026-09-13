#!/usr/bin/env python3
"""`mega.strip_session` may never lose an account while healing a dead session.

    python3 scripts/test_rclone_conf_strip.py

WHAT IS BEING PROVED

  1. Stripping a remote removes ONLY that section's `session_id`/`master_key` and
     leaves every other section byte-for-byte intact.
  2. Concurrent strips -- the reaper's 16-way fleet probe does exactly this -- cannot
     tear the file into an empty one, which would make every remote read as dead and
     send the purge after every account. This test drives that concurrent shape
     against a fixture.
  3. The reaper's path goes through the locked, section-count-refusing primitive
     (`scripts/rclone_conf.strip_session`), not a private copy. That is verified by
     delegating and by refusing to write when the primitive refuses.

The live `~/.config/rclone/rclone.conf` is never touched: `config.RCLONE_CONFIG` is
monkeypatched to a temp file for the duration. The lock file itself is the module's
real one, which is harmless -- taking it is what every writer is supposed to do.
"""
from __future__ import annotations

import configparser
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                          # noqa: E402
import mega                                                            # noqa: E402
import rclone_conf                                                     # noqa: E402

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def build_conf(n: int, with_sessions: bool = True) -> str:
    out = []
    for i in range(n):
        out.append(f"[acct{i:03d}]\n")
        out.append("type = mega\n")
        out.append(f"user = user{i:03d}@example.invalid\n")
        out.append(f"pass = pass{i:03d}\n")
        if with_sessions:
            out.append(f"session_id = session{i:03d}\n")
            out.append(f"master_key = master{i:03d}\n")
        out.append("\n")
    return "".join(out)


def sections(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("[")]


def parse(text: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read_string(text)
    return cp


def session_lines(text: str) -> dict[str, bool]:
    """{remote: has-a-session} for every section."""
    out, current = {}, None
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            current = s[1:-1]
            out[current] = False
        elif current and s.startswith(("session_id =", "master_key =")):
            out[current] = True
    return out


tmp = tempfile.TemporaryDirectory(prefix="rclone-conf-strip-")
live_backup = config.RCLONE_CONFIG
config.RCLONE_CONFIG = Path(tmp.name) / "rclone.conf"    # EVERYTHING below is a fixture

try:
    print("Part 1 -- one strip removes one session and nothing else")
    config.RCLONE_CONFIG.write_text(build_conf(40), encoding="utf-8")
    mega.strip_session("acct005")
    text = config.RCLONE_CONFIG.read_text(encoding="utf-8")
    sessions = session_lines(text)
    check("section count unchanged", len(sections(text)), 40)
    check("target session gone", sessions["acct005"], False)
    check("neighbour above untouched", sessions["acct004"], True)
    check("neighbour below untouched", sessions["acct006"], True)
    check("file still parses", parse(text).get("acct005", "user"), "user005@example.invalid")
    check("target keeps its credentials", parse(text).get("acct005", "pass"), "pass005")

    print("\nPart 2 -- 22 concurrent strips cannot tear or zero the file")
    config.RCLONE_CONFIG.write_text(build_conf(22), encoding="utf-8")
    errors: list[str] = []

    def strip(i: int):
        try:
            mega.strip_session(f"acct{i:03d}")
        except Exception as exc:                                       # noqa: BLE001
            errors.append(f"acct{i:03d}: {exc!r}")

    threads = [threading.Thread(target=strip, args=(i,)) for i in range(22)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    text = config.RCLONE_CONFIG.read_text(encoding="utf-8")
    sessions = session_lines(text)
    check("no worker raised", errors, [])
    check("all sections survive", len(sections(text)), 22)
    check("every section still has its password",
          all(parse(text).get(s.strip("[]"), "pass") for s in sections(text)), True)
    check("every stripped session is gone", any(sessions.values()), False)
    check("file is not empty", len(text) > 0, True)

    print("\nPart 3 -- the reaper delegates to the locked primitive")
    calls: list[tuple] = []
    real = mega._strip_session_locked

    def recorder(conf_path, remote):
        calls.append((Path(conf_path), remote))
        return False                       # refuse: the wrapper must NOT write anything

    before = config.RCLONE_CONFIG.read_text(encoding="utf-8")
    mega._strip_session_locked = recorder
    try:
        config.RCLONE_CONFIG.write_text(build_conf(3), encoding="utf-8")
        before = config.RCLONE_CONFIG.read_text(encoding="utf-8")
        mega.strip_session("acct001")
    finally:
        mega._strip_session_locked = real
    check("delegate was called with the active conf",
          calls and calls[0][0] == config.RCLONE_CONFIG, True)
    check("delegate got the right remote", calls and calls[0][1], "acct001")
    check("a refused strip rewrites nothing",
          config.RCLONE_CONFIG.read_text(encoding="utf-8"), before)
finally:
    config.RCLONE_CONFIG = live_backup
    tmp.cleanup()

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
