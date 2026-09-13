"""Self-contained rclone/MEGA operations for the remote-deletion reaper.

This module encodes the load-bearing pieces of Media-Syncer's hand-run *purge*
ritual (its README, § "Purging a Title") as code, scoped to individual files:

  * enumerate the MEGA pool from the ACTIVE, sessioned rclone.conf;
  * probe whether a given path exists on a remote (tri-state: present / absent /
    unknown-because-the-session-is-dead — never conflate "errored" with "clean");
  * delete a file, empty the rubbish bin, remove now-empty parents, purge a dir;
  * detect a dead MEGA session in stderr and heal it by stripping the cached
    `session_id`/`master_key` so the next call re-authenticates from user/pass.

Everything runs against `config.RCLONE_CONFIG` — the machine-local
`~/.config/rclone/rclone.conf` that Media-Syncer keeps populated *with* session
tokens (the committed conf deliberately has none). We reuse the same file so a
session we heal here is the same session Media-Syncer rides afterward.

Design notes that mirror hard-won lessons in Media-Syncer's README:
  * `deletefile` accepts only `--timeout` (plus -v/-n/-i), NOT `--retries` /
    `--low-level-retries` like `purge` does — passing them aborts the delete.
  * A delete/lsf can exit 0 while doing NOTHING on a dead session, so callers
    must VERIFY with a follow-up existence probe, never trust the exit code.
  * `directory not found` / `doesn't exist` is success for a delete (idempotent).
"""
from __future__ import annotations

import configparser
import json
import logging
import subprocess
from pathlib import Path

import config
from scripts.rclone_conf import strip_session as _strip_session_locked

log = logging.getLogger("reap.mega")

# Signatures that mean the cached MEGA session is dead and must be discarded.
# Superset of Media-Syncer's list, plus the `lsf`/`moveto` EARGS shape and the
# filesystem-init failure its purge notes call out.
_STALE_SIGNATURES = (
    "panic: runtime error",
    "SIGSEGV",
    "couldn't login: unexpected end of JSON input",
    "invalid arguments",
    "failed to create file system",
)

# stderr fragments that mean "the thing you tried to delete/list isn't there" —
# a clean, idempotent success for a delete or a definitive absence for a probe.
_GONE_SIGNATURES = (
    "directory not found",
    "not found",
    "doesn't exist",
    "no such",
)


def is_stale_session(stderr: str) -> bool:
    if not stderr:
        return False
    low = stderr.lower()
    return any(sig.lower() in low for sig in _STALE_SIGNATURES)


def _is_gone(stderr: str) -> bool:
    if not stderr:
        return False
    low = stderr.lower()
    return any(sig in low for sig in _GONE_SIGNATURES)


def mega_remotes() -> list[str]:
    """Every `type = mega` remote in the active rclone.conf, sorted (uniform fill
    order, same as Media-Syncer). This is the full fleet the probe sweeps."""
    conf = config.RCLONE_CONFIG
    if not conf.exists():
        log.error("rclone config not found at %s", conf)
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(conf)
    except configparser.Error as exc:
        log.error("failed to parse rclone config %s: %s", conf, exc)
        return []
    remotes = [
        s for s in parser.sections()
        if parser.has_option(s, "type") and parser.get(s, "type") == "mega"
    ]
    remotes.sort()
    return remotes


def _run(args: list[str], timeout: int | None = None) -> tuple[str, str, int]:
    """Run an rclone invocation with the active config injected. Returns
    (stdout, stderr, returncode); on timeout, ('', 'timeout', -1)."""
    argv = [config.RCLONE_BIN, *args, "--config", str(config.RCLONE_CONFIG)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except OSError as exc:
        return "", str(exc), -1


def strip_session(remote: str) -> None:
    """Discard a remote's cached MEGA session by removing its `session_id` and
    `master_key` from the ACTIVE rclone.conf, forcing a fresh user/pass login on
    the next call. Mirrors Media-Syncer's purge_mega_session, operating on the
    same machine-local file.

    Delegates to `rclone_conf.strip_session`, which holds the CROSS-PROCESS lock and
    refuses any rewrite that would change the section count. A bare read-modify-write
    here is dangerous: the fleet probe heals dead sessions from 16 workers at once, and
    two racing non-atomic writes can persist a torn read -- an empty file, which makes
    every remote read as dead (`didn't find section in config file`) and sends the purge
    after every account. The lock and the section-count refusal prevent exactly that.
    """
    conf = config.RCLONE_CONFIG
    if not conf.exists():
        return
    if _strip_session_locked(conf, remote):
        log.info("stripped dead session for %s", remote)
    else:
        log.warning("did NOT rewrite rclone.conf while healing %s (lock held, or the "
                    "rewrite would have changed the section count)", remote)


def _run_healing(args: list[str], remote: str, timeout: int | None) -> tuple[str, str, int]:
    """Run an rclone command; if it fails on a dead-session signature, strip the
    session once and retry. Returns the (possibly retried) result."""
    out, err, rc = _run(args, timeout)
    if rc != 0 and remote and is_stale_session(err):
        strip_session(remote)
        out, err, rc = _run(args, timeout)
    return out, err, rc


def exists_on(remote: str, relpath: str) -> bool | None:
    """Tri-state existence probe for one path on one remote.

    Returns True (present), False (definitively absent), or None (UNKNOWN — the
    session was dead or the call errored even after a heal). None is NOT "clean":
    callers treat it as "still possibly there" so a dead-session remote is never
    silently scored as purged. Uses `lsjson` on the exact path."""
    out, err, rc = _run_healing(
        ["lsjson", f"{remote}:{relpath}"], remote, timeout=120)
    if rc == 0:
        try:
            return len(json.loads(out or "[]")) > 0
        except json.JSONDecodeError:
            return None
    if _is_gone(err):
        return False
    return None


def deletefile(remote: str, relpath: str) -> tuple[bool, str]:
    """Delete one file from one remote. Idempotent: an already-absent file is a
    success. `deletefile` takes only --timeout (see module docstring). Returns
    (ok, detail)."""
    out, err, rc = _run_healing(
        ["deletefile", f"{remote}:{relpath}", "--timeout", "120s"], remote, timeout=180)
    if rc == 0:
        return True, "deleted"
    if _is_gone(err):
        return True, "already absent"
    return False, err or "unknown error"


def rmdir(remote: str, path: str) -> bool:
    """Remove a single remote directory only if it is empty (rclone rmdir refuses
    a non-empty dir). Best-effort; used to tidy parents a purge just emptied."""
    _out, err, rc = _run_healing(["rmdir", f"{remote}:{path}"], remote, timeout=120)
    return rc == 0 or _is_gone(err)


def purge_dir(remote: str, path: str) -> tuple[bool, str]:
    """Recursively delete a remote directory and everything under it (rclone
    purge). Used for title-level metadata-backup dirs. Idempotent."""
    out, err, rc = _run_healing(
        ["purge", f"{remote}:{path}", "--retries", "2", "--low-level-retries", "10"],
        remote, timeout=600)
    if rc == 0:
        return True, "purged"
    if _is_gone(err):
        return True, "already absent"
    return False, err or "unknown error"


def cleanup(remote: str) -> bool:
    """Empty a remote's MEGA rubbish bin so a delete actually reclaims space.
    Cheap per-account call; run sequentially (Media-Syncer README warns against
    fancy piped-parallel cleanup in this harness)."""
    _out, err, rc = _run_healing(["cleanup", f"{remote}:"], remote, timeout=180)
    ok = rc == 0 and not err
    if not ok:
        log.warning("cleanup of %s failed: %s", remote, err or f"rc={rc}")
    return ok


def lsf_recursive_files(remote: str, base: str) -> tuple[list[str] | None, str]:
    """List every file under `base` on `remote`, recursively, as paths relative
    to `base`. Returns (files, status) where status is 'ok' | 'absent' | 'err'.
    A dead session yields ('err') AFTER a heal attempt — never a false 'absent'."""
    out, err, rc = _run_healing(
        ["lsf", f"{remote}:{base}", "-R", "--files-only"], remote, timeout=300)
    if rc == 0:
        return [line for line in out.splitlines() if line.strip()], "ok"
    if _is_gone(err):
        return [], "absent"
    return None, "err"


def lsf_top_level(remote: str, base: str) -> tuple[list[str] | None, str]:
    """List names directly under `base` on `remote` (non-recursive). Used to
    match loose films under Movies/ by stem. Same tri-state as lsf_recursive."""
    out, err, rc = _run_healing(
        ["lsf", f"{remote}:{base}", "--files-only"], remote, timeout=180)
    if rc == 0:
        return [line for line in out.splitlines() if line.strip()], "ok"
    if _is_gone(err):
        return [], "absent"
    return None, "err"
