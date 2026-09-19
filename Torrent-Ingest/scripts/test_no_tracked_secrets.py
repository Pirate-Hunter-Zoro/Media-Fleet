#!/usr/bin/env python3
"""The tracked tree of a public repository must contain no credentials.

    python3 scripts/test_no_tracked_secrets.py

WHY THIS EXISTS. The repository is public. Before 2026-09-19 it was not, and it
tracked two things that are only acceptable in private: `Media-Syncer/rclone.conf`
(the MEGA account pool -- 822 remotes with `user`/`pass` lines) and a live Jellyfin
API key embedded in nine launchd plists. Both were moved out of the tree on
2026-09-19 (machine-local `.env` + an untracked pool conf + an install-time
placeholder substitution). A `.gitignore` rule and a documented convention are
not a guard: the next session that adds "just one conf file" or pastes a real key
into a plist reproduces the exposure silently. This is the guard.

WHAT IT ASSERTS (over `git ls-files`, i.e. exactly what a commit to the public
repo would contain):

  1. No tracked path is a secret store: `rclone.conf` (any directory; the tracked
     template is `rclone.conf.example`), `.env`/`.env.*` (the template is
     `.env.example`), private keys, and rclone's cached session files.
  2. No tracked TEXT FILE contains a credential-shaped value:
       * a rclone `pass = <long opaque>` line (the pool passwords);
       * a `session_id`/`master_key` assignment at line start;
       * a 32-hex Jellyfin API key beside `JELLYFIN_API_KEY`;
       * a PEM private key;
       * a well-known API token shape (GitHub, AWS, Google, Slack, OpenAI).
  3. No tracked plist still carries a real Jellyfin key; it must carry the
     `__JELLYFIN_API_KEY__` placeholder that startup.sh substitutes.

Deliberately checks the WORKING TREE of tracked files, not HEAD: verify_fleet.sh
runs before a commit, and a staged secret is exactly the thing this must catch.
False positives are the dangerous direction, so patterns are anchored and
value-shaped -- prose about `session_id` and the placeholder fixtures in the
other tests do not match (each exclusion is named where it is applied).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SELF = Path(__file__).resolve()

failures: list[str] = []

# Tracked-path rules: (regex on the repo-relative path, why). The `.example`
# templates are matched first and exempted -- they are tracked ON PURPOSE and
# carry placeholder values only.
FORBIDDEN_PATHS = (
    (re.compile(r"(^|/)rclone\.conf$"),
     "the MEGA account pool (untracked; use rclone.conf.example)"),
    (re.compile(r"(^|/)\.env$"), "machine-local paths/keys (undocumented)"),
    (re.compile(r"(^|/)\.env\.[^/]+$"),
     "machine-local env files; the tracked template is .env.example"),
    (re.compile(r"\.(pem|p12|key)$"), "key material"),
    (re.compile(r"(^|/)id_rsa"), "an SSH private key"),
)
EXEMPT_PATHS = (
    re.compile(r"(^|/)rclone\.conf\.example$"),
    re.compile(r"(^|/)\.env\.example$"),
)

# Content rules, each requiring a VALUE that only a real credential has. Patterns
# are anchored or length-bounded so documentation describing the shape (the
# pre-commit hook's own regexes, this file, the strip tests' `pass005` fixtures)
# cannot match.
CONTENT_RULES = (
    (re.compile(r"(?m)^[ \t]*pass[ \t]*=[ \t]*[A-Za-z0-9_+/=-]{30,}[ \t]*$"),
     "an rclone `pass =` credential (account passwords)"),
    (re.compile(r"(?m)^[ \t]*(session_id|master_key)[ \t]*=[ \t]*\S+"),
     "a cached MEGA session token"),
    (re.compile(r"<key>JELLYFIN_API_KEY</key>[ \t\r\n]*<string>[0-9a-f]{32}"),
     "a live Jellyfin API key in a plist"),
    (re.compile(r"(?m)^[ \t]*JELLYFIN_API_KEY[ \t]*=[ \t]*[0-9a-f]{32}[ \t]*$"),
     "a live Jellyfin API key"),
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
     "a PEM private key"),
    (re.compile(r"\b(ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})"),
     "a GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "a Google API key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "an OpenAI-style API key"),
)

# The tracked plists must carry the install-time placeholder, not a value.
PLIST_KEY_PLACEHOLDER = "__JELLYFIN_API_KEY__"


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}{(' -- ' + detail) if detail else ''}")
        failures.append(label)


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True, check=True,
    ).stdout
    return [p.decode() for p in out.split(b"\0") if p]


def is_text(path: Path) -> bool:
    try:
        head = path.open("rb").read(4096)
    except OSError:
        return False
    return b"\0" not in head


def main() -> int:
    files = tracked_files()
    check("git reports a non-empty tracked tree", len(files) > 0, f"{len(files)} files")

    print("\nPart 1 -- no tracked path is a secret store")
    bad_paths = []
    for rel in files:
        if any(p.search(rel) for p in EXEMPT_PATHS):
            continue
        for rule, why in FORBIDDEN_PATHS:
            if rule.search(rel):
                bad_paths.append(f"{rel} ({why})")
    check("no forbidden tracked paths", not bad_paths, "; ".join(bad_paths))

    print("\nPart 2 -- no tracked text file carries a credential value")
    hits = []
    for rel in files:
        if any(p.search(rel) for p in EXEMPT_PATHS):
            continue
        path = REPO / rel
        if path == SELF or not path.is_file() or not is_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rule, why in CONTENT_RULES:
            m = rule.search(text)
            if m:
                # Name the shape, never the matched value -- this output is public.
                hits.append(f"{rel}: {why}")
    check("no credential-shaped values", not hits, "; ".join(hits[:5]))

    print("\nPart 3 -- tracked plists carry the install-time placeholder")
    plists = [f for f in files if f.endswith(".plist")]
    with_key = [f for f in plists
                if "JELLYFIN_API_KEY" in (REPO / f).read_text(
                    encoding="utf-8", errors="replace")]
    missing_placeholder = [f for f in with_key
                           if PLIST_KEY_PLACEHOLDER not in (REPO / f).read_text(
                               encoding="utf-8", errors="replace")]
    check("plists that name a Jellyfin key use the placeholder",
          not missing_placeholder, ", ".join(missing_placeholder))

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
