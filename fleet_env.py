"""Load this repository's machine-local `.env` into the process environment.

WHY THIS EXISTS. The fleet runs on one Mac and knows where that Mac keeps things --
the iCloud watch folder, the SSD library root, the FUSE mount, the Google Drive
novels folder. Those are facts about the MACHINE, not about the pipeline, and the
repository is public: a tracked `config.py` must not carry a home directory path, an
email address, or a server token. They live in `.env`, which is untracked (the root
`.gitignore` covers it), backed up with the rest of `~/Developer`, and documented in
the tracked `.env.example`.

PRECEDENCE. `os.environ` always wins. launchd plists set `JELLYFIN_URL`/
`JELLYFIN_API_KEY` per agent, and a shell can export any override for a one-off run;
this loader must never clobber those. It only fills in keys that are absent, which is
also what makes repeated calls cheap and order-independent.

FORMAT. Deliberately the smallest thing that covers the file: blank lines and `#`
comments ignored, an optional `export ` prefix, `KEY=value` split at the FIRST `=`,
optional matched single/double quotes, no interpolation and no trailing comments --
values run to end-of-line so paths with spaces need no quoting. That is the entire
grammar `.env.example` promises.

Nothing here raises. A missing or unreadable `.env` leaves every key to the caller's
default, so a fresh clone still imports every module; it just runs on generic paths
until the file is created.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"

_loaded = False


def load() -> None:
    """Read `.env` once and fill in any variable the environment does not already set."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export"):
            line = line[len("export"):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value


def env(name: str, default: str = "") -> str:
    """The variable's value (loading `.env` first), or `default` when absent/empty."""
    load()
    return os.environ.get(name, "").strip() or default


def env_path(name: str, default):
    """The variable as a `Path` (with `~` expanded), or `default` when absent/empty."""
    load()
    raw = os.environ.get(name, "").strip()
    return Path(os.path.expanduser(raw)) if raw else Path(default)
