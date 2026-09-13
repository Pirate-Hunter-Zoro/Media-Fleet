#!/usr/bin/env python3
"""Open-Code-Doctor: idempotent, self-healing hygiene for opencode and the `coder` command.

The `coder` command has a habit of breaking (it was wired through `npx`, which re-resolves
the npm package every invocation and fails on a stale cache, a local `package.json`, or a
bumped platform binary). This doctor replaces that fragility with a set of checks it
re-runs every minute while opencode is NOT running:

  1. the `opencode` binary exists and runs -- otherwise a clean global reinstall,
  2. `~/bin/coder` is a thin wrapper that execs the real binary directly (no npx),
  3. no stale `alias coder=...` survives in ~/.zshrc / ~/.bashrc,
  4. ~/.config/opencode/opencode.jsonc carries `"permission": "allow"` (permissions
     bypassed by default).

The reinstall (1) is the only disruptive step and runs only when opencode is not running;
the others are cheap and idempotent. Stdlib-only, so it runs under the system python.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

HOME = Path.home()
LOG = HOME / "Library" / "Logs" / "OpenCodeDoctor.log"

OPENCODE_BIN = Path("/opt/homebrew/bin/opencode")
GLOBAL_PKG = Path("/opt/homebrew/lib/node_modules/opencode-ai")
CONFIG_FILE = HOME / ".config" / "opencode" / "opencode.jsonc"
CODER_BIN = HOME / "bin" / "coder"
RC_FILES = (HOME / ".zshrc", HOME / ".bashrc")

CODER_TEMPLATE = (
    "#!/bin/bash\n"
    "# 'coder' == opencode, with permissions auto-approved (--auto).\n"
    "exec opencode --auto \"$@\"\n"
)


def _ensure_path() -> None:
    os.environ["PATH"] = (
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:"
        + os.environ.get("PATH", "")
    )


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def opencode_running() -> bool:
    """True when an opencode process is alive. `pgrep` is unreliable on this host, so
    match the comm name via `ps` instead."""
    try:
        out = subprocess.run(["ps", "-axo", "comm="], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.strip() == "opencode" for line in out.splitlines())


def binary_ok() -> bool:
    try:
        r = subprocess.run([str(OPENCODE_BIN), "--version"], capture_output=True,
                           text=True, timeout=30)
        return r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def reinstall() -> None:
    """A clean global reinstall of opencode-ai (the old fix_opencode.bash, in code)."""
    steps = [
        ["npm", "config", "set", "ignore-scripts", "false"],
        ["npm", "uninstall", "-g", "opencode-ai"],
        ["npm", "cache", "clean", "--force"],
        ["npm", "install", "-g", "opencode-ai", "--foreground-scripts"],
    ]
    for cmd in steps:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            log(f"  $ {' '.join(cmd)} -> exit {r.returncode}")
        except subprocess.TimeoutExpired:
            log(f"  ! timed out: {' '.join(cmd)}")
    post = GLOBAL_PKG / "postinstall.mjs"
    if post.exists():
        try:
            subprocess.run(["node", str(post)], capture_output=True,
                           text=True, timeout=600, cwd=str(GLOBAL_PKG))
            log("  ran postinstall.mjs")
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"  ! postinstall failed: {exc}")


def ensure_coder() -> bool:
    """Ensure ~/bin/coder is the direct wrapper (not an npx alias, not an old wrapper)."""
    CODER_BIN.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = CODER_BIN.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if "exec opencode --auto" not in current:
        CODER_BIN.write_text(CODER_TEMPLATE, encoding="utf-8")
        CODER_BIN.chmod(0o755)
        log("  repaired ~/bin/coder (direct wrapper)")
        return True
    if not os.access(CODER_BIN, os.X_OK):
        CODER_BIN.chmod(0o755)
    return False


def remove_stale_aliases() -> bool:
    """Drop any `alias coder=...` (the npx alias) from the shell rc files."""
    changed = False
    for rc in RC_FILES:
        if not rc.exists():
            continue
        text = rc.read_text(encoding="utf-8")
        lines = text.splitlines()
        kept = [ln for ln in lines if not re.match(r"^\s*alias\s+coder\b", ln)]
        if len(kept) != len(lines):
            rc.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            log(f"  removed stale coder alias from {rc.name}")
            changed = True
    return changed


def ensure_config() -> bool:
    """Ensure opencode.jsonc carries `"permission": "allow"`, preserving other keys."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, json.JSONDecodeError):
        data = {}
    changed = False
    if not data.get("$schema"):
        data["$schema"] = "https://opencode.ai/config.json"
        changed = True
    if data.get("permission") != "allow":
        data["permission"] = "allow"
        changed = True
    if changed:
        CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        log("  repaired opencode.jsonc (permission=allow)")
    return changed


def main() -> None:
    _ensure_path()
    if opencode_running():
        return  # opencode is up; nothing to do this pass

    # opencode is idle: run the full check-and-repair.
    if not binary_ok():
        log("opencode binary broken and not running; reinstalling")
        reinstall()
        if not binary_ok():
            log("  ! still broken after reinstall; will retry next pass")

    ensure_coder()
    remove_stale_aliases()
    ensure_config()


if __name__ == "__main__":
    main()
