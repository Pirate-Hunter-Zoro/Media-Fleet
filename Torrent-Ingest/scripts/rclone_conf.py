"""The one safe way to rewrite `rclone.conf` from this repository.

WHY THIS MODULE EXISTS. `~/.config/rclone/rclone.conf` is the single most dangerous file in
the fleet: it is where ~800 MEGA accounts live, and because replication is
single-residence, losing one section loses an ACCOUNT and with it the only copy of whatever
files sit on it. It also has four independent writers -- Media-Syncer's session purge,
Media-Syncer's account provisioner, this repository's migration tools, and rclone itself,
which writes a fresh `session_id` back on every successful auth.

Two tools here (`migrate_comic_franchises`, `refile_season`) had grown their own copy of
the same read-modify-write, each guarded by its own `threading.Lock`. A thread lock
serializes one process and nothing else, so the guard was absent in exactly the case that
matters. Worse, two copies of one rule drift: that is §4.106, where two implementations of
a single lock used one path with different primitives and both sides believed they held it.

So there is one implementation, and it holds the CROSS-PROCESS lock Media-Syncer takes:

    ~/.config/rclone/.rclone-conf.lock

THAT PATH IS A CROSS-REPO CONTRACT. It is declared here and in
`Media-Syncer/scripts/config.py` (RCLONE_CONF_LOCK_PATH), and the two must stay equal or
the repositories stop excluding each other. It deliberately lives next to the live config
rather than inside either repo, because no repo owns it.

rclone itself stays outside the lock -- it cannot be taught to take ours. Its own writes are
atomic, so a reader never sees a partial file; what remains is a lost-update window between
an rclone auth and one of our rewrites, which costs a re-login rather than a section.
"""
from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# Must equal Media-Syncer's config.RCLONE_CONF_LOCK_PATH.
LOCK_PATH = Path.home() / ".config/rclone/.rclone-conf.lock"
LOCK_TIMEOUT_SEC = 30

# Serializes this process's own threads; the flock below serializes everyone else's.
_CONF_LOCK = threading.Lock()


@contextmanager
def conf_lock(owner: str):
    """Hold the cross-process rclone.conf lock. Yields True if it was acquired.

    Blocking with a timeout rather than non-blocking: a rewrite that silently skipped would
    leave the caller believing it had happened. On timeout it yields False, and every caller
    must then decline to write -- refusing is always available, a half-written config is not.
    """
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = open(LOCK_PATH, "w")
    except OSError as exc:
        logging.error(f"could not open the rclone.conf lock ({LOCK_PATH}): {exc}")
        yield False
        return
    got = False
    deadline = time.time() + LOCK_TIMEOUT_SEC
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() >= deadline:
                    logging.error(f"could not take the rclone.conf lock within "
                                  f"{LOCK_TIMEOUT_SEC}s for {owner}; NOT rewriting it.")
                    break
                time.sleep(0.2)
        if got:
            try:
                fh.write(f"{owner} pid={os.getpid()}\n")
                fh.flush()
            except OSError:
                pass
        yield got
    finally:
        if got:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def strip_session(conf_path: Path, remote: str) -> bool:
    """Drop `remote`'s cached MEGA session so the next call logs in fresh. True if written.

    Two refusals, both deliberate:

      * no lock, no write -- see `conf_lock`;
      * any rewrite that would change the SECTION COUNT is abandoned rather than trusted.
        The loop below should only ever remove `session_id`/`master_key` lines, so a
        changed count means the read was torn or the parse was wrong, and laundering that
        into a write is how an account disappears.
    """
    with _CONF_LOCK, conf_lock(f"strip_session:{remote}") as got:
        if not got:
            return False
        try:
            text = conf_path.read_text(encoding="utf-8")
        except OSError as exc:
            logging.error(f"could not read {conf_path} to strip {remote}: {exc}")
            return False
        out, in_section = [], False
        for line in text.splitlines(keepends=True):
            if line.startswith("["):
                in_section = line.strip() == f"[{remote}]"
            if in_section and line.split("=")[0].strip() in ("session_id", "master_key"):
                continue
            out.append(line)
        before = sum(1 for x in text.splitlines(keepends=True) if x.startswith("["))
        if sum(1 for x in out if x.startswith("[")) != before:
            logging.error(f"refusing to rewrite {conf_path} while stripping {remote}: the "
                          f"section count would change ({before} before)")
            return False
        tmp = conf_path.with_suffix(".conf.tmp")
        try:
            tmp.write_text("".join(out), encoding="utf-8")
            tmp.replace(conf_path)     # atomic, so no reader ever sees a partial file
        except OSError as exc:
            logging.error(f"could not rewrite {conf_path} for {remote}: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True
