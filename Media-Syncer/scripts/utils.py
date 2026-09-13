"""
The Lesser Arts.

This module provides a collection of general-purpose helper functions used
throughout the application. It handles tasks such as executing shell commands
and setting up logging.
"""

import fcntl
import json
import logging
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
import socket
import subprocess
import tempfile
import threading
import os
import time
from pathlib import Path
from typing import Optional
import shutil

# Import constants from the scroll of edicts
from . import config
from .config import LOG_FILE, GIT_PATH, RCLONE_CONF_PATH

def setup_logging():
    """Configures the logging for the entire construct."""
    # RotatingFileHandler, not FileHandler: the handler keeps the file open, so rotating
    # this log from outside would leave the daemon writing to a renamed inode forever.
    # Letting the handler own the rotation is the only version that actually works here.
    #
    # The StreamHandler stays. It looks like pure duplication of the file above, and it is
    # NOT: launchd captures this process's stderr into ~/Library/Logs/MediaSync.err, which
    # is the archive the remote-purge runbook reads upload history out of (see
    # config.LOG_MAX_BYTES). Dropping this handler to save disk would quietly delete that
    # procedure's only complete data source.
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            RotatingFileHandler(LOG_FILE,
                                maxBytes=config.LOG_MAX_BYTES,
                                backupCount=config.LOG_BACKUP_COUNT),
            logging.StreamHandler()
        ]
    )

def run_command(command, timeout=None):
    """Executes a command and returns its output."""
    logging.debug(f"Executing command: {command}")
    try:
        argv = list(map(str, command))
        result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
        if result.returncode != 0:
            logging.error(f"Command failed: {argv}\nStderr: {result.stderr.strip()}")
        return result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        logging.error(f"Command timed out after {timeout} seconds: {command}")
        return None, "timeout"
    except Exception as e:
        logging.error(f"Unexpected error running {command}: {e}")
        return None, str(e)

def write_json_atomic(path, obj, indent: int = 4) -> bool:
    """Write `obj` as JSON to `path` so a concurrent reader never sees a partial file.

    Every state file in this repo is read LIVE by a different process than the one writing it:
    `remote_inventory.json` by mediafs (on every inventory change) and predownload, and
    `free_space.json` by predownload, mega_accounts and check_space. A plain `open(path,'w')`
    truncates before writing, so a multi-megabyte dump leaves a real window in which a reader
    gets an empty or half-written file and a JSONDecodeError on data that is perfectly valid a
    moment later.

    It also makes the mtime lie, which matters because mediafs decides whether to reload from
    `(mtime_ns, size)`: truncation stamps a new time while the content is still incomplete, so a
    poller can latch a torn read as the current view. `os.replace` swaps the name to a fully
    written inode in one step, so the stamp changes exactly when complete content appears.

    Returns True on success. On failure the ORIGINAL file is left untouched -- a failed write
    must never destroy the last good state -- and the temp file is cleaned up.
    """
    path = Path(path)
    # A UNIQUE staging name per call, not a fixed `<name>.tmp`, because that is only safe with
    # exactly one writer and there are routinely several. Across processes: launchd SIGTERMs the
    # outgoing daemon and starts the incoming one while it is still finishing an iteration (the
    # same overlap the config-pull lock exists for). Across threads: the upload workers all reach
    # _persist_inventory. Sharing one temp path lets two writers interleave into the same file,
    # and whoever renames second either publishes a spliced document or fails outright because
    # the first already renamed the inode out from under it -- an update silently lost.
    #
    # mkstemp in the DESTINATION directory: unique by construction, and same-filesystem so the
    # rename stays atomic (a temp in /tmp would be a cross-device copy, which is not).
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                        prefix=path.name + ".", suffix=".tmp")
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=indent)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as e:
        logging.error(f"Failed to write {path}: {e}")
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _worktree_git_dir(start: Path) -> Path:
    """The `.git` directory of the work tree containing `start`, walking up.

    The lock path must not be derived as `<this project>/.git`: the fleet is one
    repository, so `.git` lives at its root, not in any project directory. Locking a
    path under a project directory would create a second, empty lock domain while the
    launchers (which resolve via `git rev-parse`) lock the real one, and two domains
    exclude nobody. Walking up matches `git_pull_locked.sh`'s resolution exactly.
    """
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d / ".git"
    return start.parent / ".git"       # not a work tree: keep the old best effort


@contextmanager
def git_tree_lock(block: bool, timeout_sec: float = 180.0):
    """Serialize every operation that touches the repo WORKING TREE, not just `git pull`.

    Yields True if the lock was acquired, False otherwise. Callers decide what a miss means,
    because the right answer differs:

    * **The config pull passes `block=False` and skips on contention.** Whoever holds the lock is
      either pulling the same tree or committing to it, so the tree ends up current either way,
      and a cycle must never block on config propagation.
    * **The account provisioner passes `block=True` and waits.** Its commit is not optional --
      skipping it would leave a registered MEGA account in the working tree but never pushed, so
      no other host learns the account exists.

    THE LOCK IS AN ATOMIC `mkdir`, NOT A FLOCKED FILE, and that is not a style choice.
    `.git/pull.lock` is shared with `scripts/git_pull_locked.sh`, which every launcher sources
    to serialize its `git pull --ff-only` before exec. macOS ships no `flock(1)`, so the shell
    has to use `mkdir` -- the lock is a DIRECTORY. This function used to `open(path, "w")` and
    flock it, so the two implementations disagreed about the type of the same path, and they
    could not exclude each other at all:

      * with the shell holding the lock, `open()` raised `IsADirectoryError`, which reached
        `main()`'s top-level `Top-level loop error (continuing)` handler, so the cycle's
        config pull was skipped entirely. Observed 2026-08-31 22:14:03, when a launcher's
        pull overlapped the daemon coming up after a fleet restart;
      * the reverse was quieter and worse: the shell's `until mkdir` loop deliberately
        `rm -f`s anything at that path that is not a directory, so it would DELETE the file
        this function was holding its flock on, and both sides would then believe they held
        the lock.

    So both sides now use the same primitive. The stale-reclaim window matches the shell's
    (5 minutes), for the same reason: a pull that legitimately runs longer than that is a
    broken network, not a live peer.

    Widening the lock to cover the provisioner as well as the pull is the original point:
    serializing pull-against-pull left pull-against-local-commit wide open, and that gap is
    the whole bug. A pull landing between
    the provisioner's `rclone.conf` append and its `git commit` sees a dirty tree and aborts with
    `Your local changes to the following files would be overwritten by merge`; landing during the
    commit itself it aborts with `Cannot fast-forward your working tree` and helpfully suggests
    `git reset --hard`, which would discard the in-flight provisioner commit if anyone obeyed it.
    Observed 4 aborts in 12 minutes while the pool was provisioning 31 accounts.

    The wait is BOUNDED rather than indefinite. 180 s comfortably exceeds
    config.GIT_PULL_TIMEOUT_SEC (120 s), so a legitimately running pull always releases in time,
    while a wedged or killed holder cannot stall provisioning forever -- the provisioner falls
    back to proceeding unlocked, which is exactly today's behaviour and never loses an account.
    """
    path = _worktree_git_dir(config.SCRIPT_DIR) / "pull.lock"
    acquired = False
    deadline = time.monotonic() + timeout_sec
    while True:
        try:
            os.mkdir(path)
            acquired = True
            break
        except FileExistsError:
            # A lock orphaned by a process killed mid-pull would otherwise be immortal.
            # `shutil.rmtree`, not `rmdir`: a lock dir that somehow holds a file (or a
            # leftover regular FILE from the flock era) must not survive either.
            try:
                if time.time() - path.stat().st_mtime > 300:
                    shutil.rmtree(path, ignore_errors=True)
                    if path.exists():
                        path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if not block or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        except OSError as exc:
            logging.warning(f"could not lock {path}: {exc}; proceeding unlocked")
            break        # unwritable .git -- proceed unlocked rather than fail the cycle
    try:
        yield acquired
    finally:
        if acquired:
            shutil.rmtree(path, ignore_errors=True)


def get_root_for_path(local_path: Path, roots: list[Path]):
    """Finds which root directory from the config contains the given local_path."""
    for root in roots:
        try:
            local_path.relative_to(root)
            return root
        except ValueError:
            continue
    return None

def get_expected_local_path(relative_path: Path) -> Path:
    """Return the expected local path of a remote file

    Args:
        relative_path (Path): File as it appears remotely

    Returns:
        Path: Resulting local destination
    """
    _, extension = os.path.splitext(relative_path)

    # Do we even care about this file
    if extension.lower() not in (config.COMICS_EXTENSIONS | config.VIDEO_EXTENSIONS):
        return None

    # local_root_for owns the One-Pace-on-Mini exception; this stays role-agnostic.
    return config.local_root_for(relative_path) / relative_path

def stream_binary_command(command: list[str], fp: Path, timeout: int, append: bool=False) -> str:
    """Method to stream the results of a command that yields binary

    Args:
        command (list[str]): Said command
        fp (Path): Where to stream results
        timeout (int): Allowed time for command to run in seconds
        append (bool, optional): Flag for overwriting or appending. Defaults to False.

    Returns:
        str: Return error if present
    """
    with open(fp, 'ab' if append else 'wb') as f:
        try:
            proc = subprocess.run(command, stdout=f, stderr=subprocess.PIPE, timeout=timeout)
            error_output = proc.stderr.decode("utf-8").strip()
            return error_output
        except subprocess.TimeoutExpired:
            logging.error(f"Command timed out after {timeout} seconds: {command}")
            return "timeout"
        
def wait_while_streaming() -> None:
    """No-op (kept for its call sites): the daemons NO LONGER pause for playback.

    The old behavior blocked every MEGA op while a client was streaming, on the theory
    that the ~390-remote scan's VPN churn + per-IP budget use would stutter cold
    playback. In practice that (a) never mattered for a physically-present file --
    playback of it is a mediafs passthrough read that never touches MEGA -- and (b) was
    actively counter-productive for a cold show, where the whole job of the
    pre-downloader is to fetch the NEXT episodes while you watch the first. It also let
    a Jellyfin background scan (which reads cold files, stamping the same flag) starve
    all sync for hours. So pausing is removed; VPN churn stays bounded by the rotation
    throttle (ROTATE_MIN_INTERVAL_SEC) and the the free-model chain split-tunnel. `mediafs` still
    stamps STREAM_ACTIVE_FLAG and prioritizes interactive reads over background fill
    workers internally -- that read-serving priority is unaffected; only the
    daemon-wide pause is gone.
    """
    return


def repeat_command(args: list[str], timeout: int=None, just_tried: bool=False,
                   rotate: bool=True) -> tuple[str, str]:
    """Run a command and on failure switch IP addresses a maximum number of times before reporting a failure

    Args:
        args (list[str]): Arguments to run
        timeout (int, optional): Timeout to pass into the command. Defaults to None.
        just_tried (bool, optional): Flag for if this is the first recursive call or not - useful if we try a session purge for a remote
        rotate (bool, optional): Rotate the exit node between failed attempts. Defaults to
            True. Pass False for the PARALLEL rescan (see below).

    Returns:
        tuple[str, str]: stdout, stderr
    """
    from .vpn import rotate_exit_node
    wait_while_streaming()   # yield the VPN/bandwidth to live playback before any MEGA op
    remote = extract_remote_from_args(args)
    if remote and is_quarantined(remote):
        return (None, f"{remote} is quarantined")
    success = False
    std_out = None
    std_err = None
    for _ in range(config.MAX_DOWNLOAD_TRIES):
        std_out, std_err = run_command(args, timeout)
        if std_err != "":
            # Callers doing many concurrent operations pass rotate=False. A rotation resets
            # EVERY TCP connection on the machine, so under a parallel sweep one rotation
            # fails all the other in-flight calls, each of which then asks to rotate again --
            # and it kills any uploads running alongside. Rotation is also pointless for a
            # listing: exit-node churn exists to dodge MEGA's TRANSFER throttling, and a
            # listing moves no payload, so a failure there is almost always a blip that a
            # plain retry clears.
            logging.info(f"Error running command with args {args}..."
                         + (" switching IPs..." if rotate else " retrying..."))
            if rotate:
                rotate_exit_node()
        else:
            success = True
            break
    if success:
        return (std_out, std_err)
    # Failure after repeat...
    if just_tried or (not is_stale_session_error(std_err)):
        if remote:
            quarantine_remote(remote)
        return (None, std_err)
    # If we make it here, it was a stale session
    if not remote:
        logging.info(f"Stale session detected from {args} but no remote could be parsed...")
        return (None, std_err)
    # Otherwise, we likely need to purge the session of this remote. Routed through
    # heal_stale_session rather than calling purge_mega_session directly so this shares one
    # dedup window and one budget with the transfer paths -- the parallel rescan touches all
    # ~390 remotes at once, and a burst of simultaneous purges is its own failure mode.
    if not heal_stale_session(remote):
        quarantine_remote(remote)
        return (None, std_err)
    # Now try again
    return repeat_command(args, timeout, just_tried=True, rotate=rotate)

# Every way a dead cached MEGA session surfaces in rclone's stderr. One cause, four faces:
#
#   * `panic: runtime error` / `SIGSEGV` -- go-mega dereferences a nil root Node when MEGA
#     answers the reused session with no tree.
#   * `couldn't login` -- MEGA returns an empty or malformed auth response.
#   * `invalid arguments` -- MEGA's EARGS. It reads like a caller mistake and almost never is:
#     the arguments were fine on the same command five minutes earlier, and a purge fixes it.
#     rclone reports genuine flag mistakes as `unknown flag`, so nothing legitimate is caught
#     here.
#   * `failed to create file system` -- the backend could not be constructed at all, which for
#     a mega remote means the login behind it failed.
#
# NOT in this list, deliberately: `didn't find section in config file`. That is a torn READ of
# rclone.conf mid-rewrite, not a dead session, and treating it as one makes a purge trigger the
# next purge. purge_mega_session's atomic os.replace is what prevents it; matching it here would
# reintroduce the cascade from the other side.
STALE_SESSION_SIGNATURES = (
    "panic: runtime error",
    "sigsegv",
    "couldn't login",
    "invalid arguments",
    "failed to create file system",
)


def is_stale_session_error(std_err: str) -> bool:
    """Determine if the cached mega session is dead

    Args:
        std_err (str): Error message

    Returns:
        bool: Whether cached mega session is dead or not
    """
    if not std_err:
        return False
    lowered = std_err.lower()
    return any(signature in lowered for signature in STALE_SESSION_SIGNATURES)


@contextmanager
def rclone_conf_lock(owner: str):
    """Hold the CROSS-PROCESS lock on rclone.conf. Yields True if it was acquired.

    `_conf_lock` below is a threading.Lock, so it serializes this process and nothing else.
    The writers of this file are in three different processes and two different repositories
    -- mediasync's session purge, the account provisioner's append, and Torrent-Ingest's
    comic-migration tools -- so a thread lock leaves the read-modify-write of the file the
    entire MEGA pool lives in unserialized where it matters most. Both are needed, and both
    are taken: the thread lock for this process's own concurrency, this one for everyone
    else's. Losing a section here loses an ACCOUNT, and with it the only copy of whatever
    single-residence files live on it.

    Observed live on 2026-08-31 at 22:18: `Failed to create file system for "automega113:":
    didn't find section in config file` -- a read that landed inside another process's
    rewrite. Nothing was lost that time.

    rclone itself remains outside this lock: it writes a fresh `session_id` back on every
    successful auth and cannot be taught to take ours. Its own writes are atomic, so a
    reader never sees a partial file; what remains is a lost-update window between an rclone
    auth and one of our rewrites, which costs a re-login rather than a section.

    Blocking with a timeout, unlike the uploader lock: a purge that silently skipped would
    leave a dead session in place and the remote would keep failing. On timeout it yields
    False and the caller declines to write -- refusing is always available, a half-written
    config is not.
    """
    path = config.RCLONE_CONF_LOCK_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "w")
    except OSError as e:
        logging.error(f"could not open the rclone.conf lock ({path}): {e}")
        yield False
        return
    got = False
    deadline = time.time() + config.RCLONE_CONF_LOCK_TIMEOUT_SEC
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() >= deadline:
                    logging.error(
                        f"could not take the rclone.conf lock within "
                        f"{config.RCLONE_CONF_LOCK_TIMEOUT_SEC}s for {owner}; NOT rewriting "
                        f"the config. Another writer is holding it or died holding it.")
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


def internet_reachable() -> bool:
    """Whether traffic actually gets off this machine THROUGH THE EXIT NODE.

    Lives here, not in the watchdog, because two callers need the same answer and two
    implementations of one test are how they come to disagree (§4.106): the watchdog asks
    it to decide whether to rotate away from a dead node, and `heal_stale_session` asks it
    to decide whether a login failure is a dead session or a dead network.

    Deliberately a raw TCP connect to a literal IP. DNS dies with the exit node, so a
    hostname probe could not tell "no exit node" from "no resolver"; and the probe targets
    must be ones split_tunnel.sh does NOT pin to the physical gateway, or a dead node reads
    as healthy. See config.TS_WATCHDOG_PROBE_IPS.
    """
    for ip in config.TS_WATCHDOG_PROBE_IPS:
        try:
            with socket.create_connection((ip, config.TS_WATCHDOG_PROBE_PORT),
                                          timeout=config.TS_WATCHDOG_PROBE_TIMEOUT_SEC):
                return True
        except OSError:
            continue
    return False


# remote -> [last heal timestamp, heals this cycle]
_session_heals: dict[str, list] = {}
_heal_lock = threading.Lock()


def heal_stale_session(remote: str) -> bool:
    """Discard `remote`'s cached MEGA session so the next rclone call re-authenticates.

    The caller-facing contract is a single bool: **True means try that remote again**, False
    means stop handing it work for now. Everything below exists to keep that answer cheap and
    to keep it from becoming a spin.

    Three properties, each load-bearing:

    * **Deduplicated.** Concurrent callers hitting the same dead remote produce ONE purge.
      Within SESSION_HEAL_COOLDOWN_SEC the later callers get True without touching the config
      at all -- the fix they wanted has already been applied.
    * **Budgeted.** SESSION_HEAL_MAX_PER_CYCLE purges per remote per cycle. A remote that is
      still failing after that is not suffering from a stale session, so False is returned and
      the caller benches it. clear_session_heals() restores the budget each cycle.
    * **Off the happy path.** Nothing here runs unless a command already failed with a
      stale-session signature, so a healthy fleet pays nothing.

    Args:
        remote (str): Respective mega account as seen in rclone.conf

    Returns:
        bool: True if the remote is worth retrying, False once its heal budget is spent
    """
    if not remote:
        return False
    # A DEAD NETWORK IS NOT A STALE SESSION. `couldn't login: unexpected end of JSON input`
    # is what a truncated HTTP response looks like, and when nothing at all gets off the box
    # EVERY remote produces it at once -- so the healer reads a single outage as ~800
    # simultaneous stale sessions and rewrites rclone.conf once per remote to "fix" it.
    # Observed on 2026-09-01: the Mullvad exit node stopped carrying traffic at ~17:00 and
    # 45 purges followed in five hours, every one of them a read-modify-write of the file the
    # whole MEGA pool lives in, none of them capable of helping. Losing a section there loses
    # an ACCOUNT. So ask first whether anything can reach the internet, and if it cannot,
    # decline to heal: the caller quarantines the remote for this cycle, which is the correct
    # response to an outage, and the config is left alone.
    if not internet_reachable():
        logging.warning(f"{remote} failed to log in, but nothing reaches the internet from "
                        f"this box -- that is the exit node, not a stale session. Not "
                        f"touching rclone.conf; benching this remote for the cycle.")
        return False
    now = time.time()
    with _heal_lock:
        last, count = _session_heals.get(remote, (0.0, 0))
        # The budget is per WINDOW, not per process lifetime. mediasync resets it at each cycle
        # boundary, but mediafs and predownload are resident and never reach one -- with a
        # lifetime budget they would permanently lose a remote to three unlucky heals spread
        # over weeks. A remote that has behaved for a whole window starts clean.
        if now - last > config.SESSION_HEAL_WINDOW_SEC:
            count = 0
        if count >= config.SESSION_HEAL_MAX_PER_CYCLE:
            return False
        if now - last < config.SESSION_HEAL_COOLDOWN_SEC:
            # Another caller just purged this remote; a second purge would only race the
            # fresh session rclone is in the middle of minting.
            return True
        _session_heals[remote] = (now, count + 1)
        attempt = count + 1
    logging.warning(f"Stale session on {remote}; healing "
                    f"(attempt {attempt}/{config.SESSION_HEAL_MAX_PER_CYCLE} this cycle)...")
    purge_mega_session(remote)
    return True


def clear_session_heals():
    """Reset every remote's per-cycle heal budget. Called at the top of each cycle, next to
    clear_quarantine(), so a remote that exhausted its budget in cycle N is tried again in
    cycle N+1 against that cycle's fresh scan."""
    with _heal_lock:
        _session_heals.clear()

_quarantined_remotes: set[str] = set()

def is_quarantined(remote: str) -> bool:
    """Return if the given remote is in the quarantined set

    Args:
        remote (str): Said remote

    Returns:
        bool: Whether remote is quarantined
    """
    return remote in _quarantined_remotes

def quarantine_remote(remote: str):
    """Add remote to quarantine set and log event

    Args:
        remote (str): Remote to quarantine
    """
    _quarantined_remotes.add(remote)
    logging.info(f"Quarantined remote {remote}...")

def clear_quarantine():
    """Clear quarantine set
    """
    _quarantined_remotes.clear()

def extract_remote_from_args(args: list[str]) -> Optional[str]:
    """Helper function to extract the MEGA remote associated with the argument list

    Args:
        args (list[str]): Full argv list passed to run_command

    Returns:
        Optional[str]: Respective mega remote if any were present
    """
    for arg in args:
        if arg.startswith("-"):
            continue
        if ":" in arg:
            # Grab the remote
            return arg.split(":", 1)[0]
    return None

# Serializes the read-modify-write of rclone.conf below. See purge_mega_session.
_conf_lock = threading.Lock()


def purge_mega_session(remote: str):
    """Purge the login credentials for MEGA - login with fresh username and password

    LOCKED AND ATOMIC, and both halves are load-bearing.

    This is a read-modify-write of the single rclone.conf that every rclone process on the
    machine reads, and concurrent MEGA stale-session panics make concurrent purges routine.
    Two distinct hazards follow, needing two distinct fixes:

      * **Lost updates** -- two threads read the same original and each writes back its own
        edit, so one remote's purge vanishes. The lock prevents this.
      * **Torn reads** -- a non-atomic rewrite leaves a window in which the file is partial
        or empty, and any rclone subprocess reading inside it dies with
        `didn't find section in config file ("<remote>")`. That looks like a broken remote
        and is really a broken read, and it cascades, because the message is also a
        stale-session signature that triggers another purge. Only `os.replace` prevents
        this; the lock cannot, because rclone is a separate process.

    Args:
        remote (str): Respective mega account as seen in rclone.conf
    """
    logging.info(f"Purging session for remote {remote}...")
    rclone_conf = config.RCLONE_CONF_LIVE_PATH
    rclone_conf.parent.mkdir(parents=True, exist_ok=True)
    # Both locks: `_conf_lock` for this process's own threads, `rclone_conf_lock` for the
    # other processes and the other repository that write this same file.
    with _conf_lock, rclone_conf_lock(f"purge:{remote}") as got_lock:
        if not got_lock:
            return
        try:
            with open(rclone_conf, 'r') as f:
                lines = f.readlines()
        except OSError as e:
            logging.error(f"Could not read rclone.conf to purge {remote}: {e}")
            return
        in_section = False
        output_lines = []
        for line in lines:
            if line.strip() == f"[{remote}]":
                in_section=True
                output_lines.append(line)
            elif line.strip().startswith("["):
                # Different remote
                output_lines.append(line)
                in_section=False
            elif in_section:
                # Within the remote whose session we need to purge
                if line.strip().startswith("session_id =") or line.strip().startswith("master_key ="):
                    continue
                else:
                    output_lines.append(line)
            else:
                output_lines.append(line)

        # Refuse to write a config that lost its sections -- a truncated read must never be
        # laundered into a truncated write.
        if not any(l.strip().startswith("[") for l in output_lines):
            logging.error(f"Refusing to write a section-less rclone.conf while purging "
                          f"{remote}; leaving the existing file untouched.")
            return

        tmp = rclone_conf.with_suffix(".conf.tmp")
        try:
            with open(tmp, 'w') as f:
                f.write("".join(output_lines))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, rclone_conf)      # atomic: readers see old or new, never partial
        except OSError as e:
            logging.error(f"Could not write rclone.conf while purging {remote}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
    logging.info(f"Session purge for {remote} complete...")

# Markers of a git pull that actually FAILED, as opposed to one that merely spoke.
# Deliberately matched against the real failure vocabulary (`fatal:`, `error:`, a
# resolver/auth/connection refusal, and run_command's own "timeout" sentinel) rather than
# against "did stderr contain anything", which misreads a chatty success as a failure.
_GIT_PULL_FAILURE_MARKERS = (
    "fatal:",
    "error:",
    "could not resolve",
    "connection refused",
    "authentication failed",
    "permission denied",
    "timeout",
)


def is_git_pull_failure(std_err: str) -> bool:
    """Decide whether a config-propagation `git pull` genuinely failed.

    Args:
        std_err (str): stderr from the pull

    Returns:
        bool: True only for real failures; a successful pull's chatter returns False
    """
    if not std_err:
        return False
    return any(marker in std_err.lower() for marker in _GIT_PULL_FAILURE_MARKERS)


def git_pull() -> None:
    """
    Run 'git pull' command
    """
    repo_root = config.SCRIPT_DIR.parent
    # Explicit `origin main` + --ff-only: a bare `git pull` occasionally aborts with
    # "Cannot fast-forward to multiple branches" when the fetch marks more than one ref
    # for merge; naming the branch and forcing fast-forward-only keeps the config-
    # propagation pull deterministic (and it never creates a merge commit on the Mini).
    #
    # NOT repeat_command, which judges success by an EMPTY stderr. `git pull` writes to
    # stderr even when it succeeds, and the explicit `origin main` above guarantees it:
    # naming a refspec always prints the fetch banner --
    #     From https://github.com/Pirate-Hunter-Zoro/Media-Syncer
    #      * branch            main       -> FETCH_HEAD
    # -- 102 bytes of stderr on a pull whose stdout is a contented "Already up to date."
    # (transfer progress lands there too). So every successful config pull was read as a
    # failure and burned the whole MAX_DOWNLOAD_TRIES budget of exit-node rotations, three
    # per cycle, 43 logged before it was caught on 2026-08-06. The fix for one bug had
    # created the next: the explicit refspec added to dodge the multiple-branches abort is
    # exactly what makes the banner unconditional.
    #
    # This is the same rule that already keeps the rclone `copyto` transfers on
    # run_command: a command that is chatty on success cannot be judged by whether it
    # spoke, only by WHAT it said. Bounded by a timeout because the pull is best-effort
    # config propagation -- a cycle must never block on it, and the next cycle retries
    # anyway, which is the retry budget repeat_command was being asked to provide.
    #
    # SERIALIZED, because `origin main` alone does not actually prevent the multiple-branches
    # abort. Two pulls running against one working tree each append a mergeable line to the
    # shared .git/FETCH_HEAD, and the second one to read it sees two candidates for `main`
    # and dies with exactly that error no matter how precisely it named the refspec. The
    # daemon has one pull site, so the overlap needs two PROCESSES -- which is precisely what
    # a restart produces: launchctl SIGTERMs the outgoing daemon and starts the incoming one
    # while it is still finishing its iteration. Skipping on contention rather than waiting is
    # correct here: the process holding the lock is pulling the same tree this one wanted, so
    # the update lands either way, and a cycle must never block on config propagation.
    # (Torrent-Ingest hits this across six launchers sharing one tree and solves it the same
    # way -- see its README, *The launchers pull this repo, and that pull must be serialized*.)
    # block=False: skip rather than queue. The holder is either pulling this same tree or
    # committing to it (see git_tree_lock), so the tree lands current either way.
    with git_tree_lock(block=False) as got_lock:
        if not got_lock:
            logging.info("Another process holds the repo tree lock (a pull, or the account "
                         "provisioner committing rclone.conf); skipping this pull and deploying "
                         "the tree as it stands.")
        else:
            _, std_err = run_command(
                [GIT_PATH, '-C', str(repo_root), 'pull', '--ff-only', '--no-rebase', 'origin', 'main'],
                timeout=config.GIT_PULL_TIMEOUT_SEC,
            )
            if is_git_pull_failure(std_err):
                logging.warning(f"Config-propagation git pull failed; continuing on local files: {std_err}")
    # The purpose of that was to change local rclone.conf in case we added more MEGA remotes - now we need to copy that to our rclone executable directory
    rclone_conf_dest_path = Path.home() / ".config/rclone/rclone.conf"
    rclone_conf_dest_path.parent.mkdir(parents=True, exist_ok=True)
    install_rclone_conf(RCLONE_CONF_PATH, rclone_conf_dest_path)


def install_rclone_conf(src: Path, dst: Path) -> bool:
    """Publish the repo's rclone.conf to the live location ATOMICALLY.

    This was a plain `shutil.copy2`, which opens the destination for writing and TRUNCATES
    it before copying a byte. Every rclone process on the machine reads that file, so the
    copy has always had a window in which the config is empty or half-written -- it was just
    harmless while the only readers were a serial scan that ran after the copy finished.

    Against the 16-way parallel rescan it stopped being harmless: readers land inside the
    window and die with `didn't find section in config file ("<remote>")`, which reads like a
    dead remote and is really a dead read. Worse, that error is a *stale-session* signature
    to repeat_command, so it triggers purge_mega_session -- a second writer to the same file
    -- and the two corrupt each other. One production sweep logged 130 such errors and a
    purge that found the file missing outright.

    Copy to a temp file on the same filesystem, fsync, then `os.replace`, which is atomic:
    a reader sees either the whole old config or the whole new one. Held under the same lock
    as purge_mega_session so the repo-publish and a session-purge cannot interleave.
    """
    with _conf_lock, rclone_conf_lock(f"install:{Path(src).name}") as got_lock:
        if not got_lock:
            return False
        try:
            data = Path(src).read_bytes()
        except OSError as e:
            logging.error(f"Could not read repo rclone.conf ({src}): {e}")
            return False
        # Never publish an empty/section-less config over a working one.
        if b"[" not in data:
            logging.error(f"Refusing to install a section-less rclone.conf from {src}")
            return False
        tmp = Path(str(dst) + ".tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dst)
            shutil.copystat(src, dst)
            return True
        except OSError as e:
            logging.error(f"Could not install rclone.conf to {dst}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
