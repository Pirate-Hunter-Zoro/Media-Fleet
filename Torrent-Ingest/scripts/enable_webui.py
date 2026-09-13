#!/usr/bin/env python3
"""Idempotently enable qBittorrent's Web API on the configured port with
localhost authentication bypassed, and disable the "recursive download" prompt,
by editing qBittorrent.ini in place.

qBittorrent rewrites its ini on exit, so this MUST run while qBittorrent is
stopped (startup.sh quits it first, edits, then relaunches). We edit only our
own keys (the `WebUI\\...` block and the recursive-download guard) and preserve
every other line verbatim, writing `Key=Value` with no spaces the way QSettings
expects.
"""

import base64
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

INI = Path.home() / ".config" / "qBittorrent" / "qBittorrent.ini"

DESIRED = {
    r"WebUI\Enabled": "true",
    r"WebUI\Port": str(config.QBT_PORT),
    r"WebUI\Address": "127.0.0.1",          # loopback only; not exposed on Tailscale
    r"WebUI\LocalHostAuth": "false",        # localhost clients skip auth
    r"WebUI\CSRFProtection": "false",
    r"WebUI\HostHeaderValidation": "false",
    r"WebUI\Username": "admin",
    # qBittorrent's "recursive download" guard: when a torrent's file list contains a
    # `.torrent` file (a "torrent bomb" — a release group's promo/nested torrent), the
    # GUI pops a modal "contains .torrent files, do you want to proceed?" prompt and
    # blocks until someone clicks it. The ingest pipeline adds torrents headlessly via
    # the WebUI, so there is nobody to click: turn the feature off entirely (we never
    # want qBittorrent auto-adding nested .torrent files anyway — they are junk that
    # would cascade un-curated torrents into the client).
    r"Advanced\DisableRecursiveDownload": "true",
}


def _pbkdf2_value(password):
    """qBittorrent v5 WebUI password: @ByteArray(<b64 salt>:<b64 hash>), where
    hash = PBKDF2-HMAC-SHA512(password, 16-byte salt, 100000 iters, 64 bytes).

    qBittorrent REFUSES to start the WebUI with no credentials set, even when
    localhost auth is bypassed — so we must write a valid-looking password even
    though loopback clients never actually authenticate against it.
    """
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 100000, 64)
    payload = base64.b64encode(salt).decode() + ":" + base64.b64encode(digest).decode()
    return f'"@ByteArray({payload})"'


def main():
    if not INI.exists():
        print(f"qBittorrent.ini not found at {INI}; launch qBittorrent once first.")
        sys.exit(1)

    raw = INI.read_text(encoding="utf-8")
    # Only set a password if one isn't already present, so re-running startup.sh
    # stays idempotent and doesn't churn the hash (or lock you out of an existing
    # WebUI login you set by hand).
    if r"WebUI\Password_PBKDF2" not in raw:
        DESIRED[r"WebUI\Password_PBKDF2"] = _pbkdf2_value("torrent-ingest")

    lines = raw.splitlines()
    out = []
    in_prefs = False
    prefs_start = None
    handled = set()

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            # Leaving a section: if we were in [Preferences], flush missing keys.
            if in_prefs:
                for key, val in DESIRED.items():
                    if key not in handled:
                        out.append(f"{key}={val}")
                        handled.add(key)
            in_prefs = stripped == "[Preferences]"
            if in_prefs:
                prefs_start = len(out)
            out.append(line)
            continue

        if in_prefs and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in DESIRED:
                out.append(f"{key}={DESIRED[key]}")
                handled.add(key)
                continue
        out.append(line)

    # File ended while still inside [Preferences], or the section was last.
    if in_prefs:
        for key, val in DESIRED.items():
            if key not in handled:
                out.append(f"{key}={val}")
                handled.add(key)

    # No [Preferences] section at all — create one.
    if prefs_start is None:
        out.append("[Preferences]")
        for key, val in DESIRED.items():
            out.append(f"{key}={val}")

    INI.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"WebUI enabled on 127.0.0.1:{config.QBT_PORT} (localhost auth bypassed).")


if __name__ == "__main__":
    main()
