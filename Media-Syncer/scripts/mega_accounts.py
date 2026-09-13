"""Automatic MEGA account provisioning -- keeps the pool from ever filling.

Monitors PLACEABLE free space across the pool -- raw free minus each remote's per-claim
fill margin, excluded remotes dropped (see `pool_placeable_bytes`) -- and, when it drops
below a floor, creates fresh MEGA accounts and adds them to rclone.conf (committed + pushed
so the whole fleet picks them up on its next pull). Each new account is a free 20 GB tier --
we NEVER count more than REMOTE_CAP_BYTES per account, because anything above 20 GB is an
expiring promo.

How an account is created (fully unattended):
  1. Pick the next `<ALIAS_PREFIX><n>` name and the email alias `base+<name>@domain`
     (gmail/icloud deliver every +alias to the one base inbox), with a random password.
  2. `megatools reg --register ...` -> prints an ephemeral STATE and sends a confirm email.
  3. Read that email from the base inbox over IMAP, extract the mega.nz confirm link.
  4. `megatools reg --verify STATE LINK` -> the account is live.
  5. Verify login (`megatools df`), then append a `[<name>]` mega remote to rclone.conf
     (password rclone-obscured), commit, and push.

SAFE BY DEFAULT: account creation is INERT unless the base-inbox IMAP app-password is
present at MEGA_APP_PASSWORD_FILE (outside the repo). Without it the module only MONITORS
and logs when capacity is low -- it never half-registers or edits rclone.conf. Creation is
rate-limited and each account is proven to log in before it's written to the config.

    python3 -m scripts.mega_accounts --status          # pool capacity + config state
    python3 -m scripts.mega_accounts --ensure          # create accounts now if low (needs the app-password)
    python3 -m scripts.mega_accounts --create 1        # create exactly N (for a supervised first run)
"""
from __future__ import annotations

import argparse
import configparser
import email as emaillib
import imaplib
import json
import logging
import os
import re
import secrets
import string
import subprocess
import time
from pathlib import Path

from . import config
from .operations import (
    find_mega_remotes,
    get_remote_free_space,
    inventory_bytes_by_remote,
    placeable_free,
    usable_free,
)
from .utils import git_tree_lock, install_rclone_conf


# --- capacity ----------------------------------------------------------------

def pool_free_bytes() -> int:
    """Total usable free space across the pool (each account capped at REMOTE_CAP_BYTES),
    read from the free_space.json the sync cycle already maintains -- no rclone calls.

    Cross-checked against the inventory through `usable_free`, for the same reason the upload
    ledger is: `about` figures are only as fresh as the last sweep, so between sweeps this
    would otherwise count bytes already spent as still available and under-provision.
    """
    try:
        fs = json.loads(Path(config.FREE_SPACE_PATH).read_text())
    except (OSError, ValueError):
        return 0
    placed = inventory_bytes_by_remote()
    return sum(usable_free(d, placed.get(name, 0)) for name, d in fs.items())


def pool_placeable_bytes() -> int:
    """Total placeable free space across the pool -- the figure provisioning must use.

    Same read as `pool_free_bytes`, but each remote contributes only what it can actually
    accept for a new file (`placeable_free`: raw free minus the per-claim fill margin), and
    remotes excluded from allocation (e.g. the metadata backup remote) are dropped entirely.

    This is the fragmentation-aware measure. Aggregate free counts every remote's margin
    sliver and the excluded backup remote as spendable capacity, which is how a pool with
    ~224 GB "free" but ~1 GB placeable could stall with the auto-provisioner believing it had
    room to spare.
    """
    try:
        fs = json.loads(Path(config.FREE_SPACE_PATH).read_text())
    except (OSError, ValueError):
        return 0
    placed = inventory_bytes_by_remote()
    excluded = config.upload_excluded_remotes()
    return sum(
        placeable_free(d, placed.get(name, 0))
        for name, d in fs.items()
        if name not in excluded
    )


def placeable_free_sum(ledger) -> int:
    """Placeable bytes across a live free-space ledger, for the upload phase.

    The ledger maps remote -> `usable_free` (raw free, margin not yet reserved). Each claim
    reserves REMOTE_FILL_MARGIN_BYTES before placing anything, so a remote's contribution to
    real headroom is `max(0, free - margin)`. Excluded remotes are already seeded at 0 in the
    ledger, so they contribute nothing here. Summing this is the fragmentation-aware figure:
    it counts only space that can actually receive a file.
    """
    return sum(max(0, v - config.REMOTE_FILL_MARGIN_BYTES) for v in ledger.values())


# --- naming ------------------------------------------------------------------

def _next_index() -> int:
    prefix = config.ACCOUNT_ALIAS_PREFIX
    n = 0
    if config.RCLONE_CONF_PATH.exists():
        for line in config.RCLONE_CONF_PATH.read_text(encoding="utf-8").splitlines():
            m = re.match(rf"\[{re.escape(prefix)}(\d+)\]", line.strip())
            if m:
                n = max(n, int(m.group(1)))
    return n + 1


def _alias_email(name: str) -> str:
    local, _, domain = config.ACCOUNT_EMAIL_BASE.partition("@")
    return f"{local}+{name}@{domain}"


def _random_password(n: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n)) + "aA1!"


# --- IMAP: read the confirmation email ---------------------------------------

def _app_password() -> str | None:
    try:
        pw = config.MEGA_APP_PASSWORD_FILE.read_text(encoding="utf-8").strip()
        return pw or None
    except OSError:
        return None


def _imap_confirm_link(to_alias: str, since_ts: float, app_pw: str,
                       tries: int = 20, delay: int = 15) -> str | None:
    """Poll the base inbox for the MEGA confirm email addressed to `to_alias` and return the
    mega.nz confirm link. Gmail/iCloud file +aliases under the base account, matched on the
    To: header. CRITICAL: MEGA's verification mail (from welcome@mega.io) routinely lands in
    SPAM, so we search Spam and All Mail too, not just INBOX -- searching INBOX alone was why
    unattended creation silently never found the email."""
    host = config.IMAP_HOST_FOR(config.ACCOUNT_EMAIL_BASE)
    folders = ["INBOX", '"[Gmail]/Spam"', '"[Gmail]/All Mail"', "Junk", "Spam"]
    for _ in range(tries):
        try:
            M = imaplib.IMAP4_SSL(host)
            M.login(config.ACCOUNT_EMAIL_BASE, app_pw)
            try:
                for folder in folders:
                    try:
                        rv, _ = M.select(folder, readonly=True)
                        if rv != "OK":
                            continue
                    except imaplib.IMAP4.error:
                        continue
                    typ, data = M.search(None, '(TO "%s")' % to_alias)
                    ids = data[0].split() if data and data[0] else []
                    for mid in reversed(ids):
                        _t, md = M.fetch(mid, "(RFC822)")
                        msg = emaillib.message_from_bytes(md[0][1])
                        m = re.search(r"https://mega\.nz/#confirm\S+", _email_text(msg))
                        if m:
                            return m.group(0).rstrip(">).,\r\n")
            finally:
                M.logout()
        except Exception as e:                                    # noqa: BLE001
            logging.info(f"IMAP poll error (retrying): {e}")
        time.sleep(delay)
    return None


def _email_text(msg) -> str:
    if msg.is_multipart():
        out = []
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    out.append(part.get_payload(decode=True).decode("utf-8", "ignore"))
                except Exception:                                 # noqa: BLE001
                    pass
        return "\n".join(out)
    try:
        return msg.get_payload(decode=True).decode("utf-8", "ignore")
    except Exception:                                             # noqa: BLE001
        return str(msg.get_payload())


# --- registration ------------------------------------------------------------

# Return value from `_register` when MEGA reports EEXIST: the email alias already has a
# live account but no rclone.conf entry for it (an orphaned alias from a prior run that
# registered it and then died before appending). The caller must skip to the NEXT alias,
# not abort the batch -- retrying the same index would EEXIST forever.
REGISTER_EXISTS = "exists"

def _register(name: str, email: str, password: str, app_pw: str):
    """Register + email-verify one account. Returns True once it logs in, REGISTER_EXISTS
    when the email alias already has a MEGA account, or False on any other failure."""
    since = time.time()
    try:
        proc = subprocess.run(
            [config.MEGATOOLS_BIN, "reg", "--scripted", "--register",
             "--email", email, "--name", name, "--password", password],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logging.error(f"megatools register failed for {name}: {e}")
        return False
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"reg --verify\s+(\S+)", out) or re.search(r"@(\S+)", out)
    state = m.group(1) if m else None
    if not state:
        if "EEXIST" in out:
            return REGISTER_EXISTS
        logging.error(f"register: no verify STATE parsed for {name}: {out[:200]}")
        return False
    link = _imap_confirm_link(email, since, app_pw)
    if not link:
        logging.error(f"register: no confirm email found for {email} within timeout")
        return False
    try:
        v = subprocess.run([config.MEGATOOLS_BIN, "reg", "--scripted", "--verify", state, link],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        logging.error(f"megatools verify failed for {name}: {e}")
        return False
    if v.returncode != 0:
        logging.error(f"verify failed for {name}: {(v.stderr or v.stdout)[:200]}")
        return False
    # Prove it logs in before trusting it.
    df = subprocess.run([config.MEGATOOLS_BIN, "df", "-u", email, "-p", password],
                        capture_output=True, text=True, timeout=60)
    if df.returncode != 0:
        logging.error(f"new account {name} won't log in yet: {(df.stderr or '')[:150]}")
        return False
    return True


def _append_remote(name: str, email: str, password: str) -> bool:
    """Append a mega remote to rclone.conf (password rclone-obscured). Only the durable
    user/pass are written -- never session tokens."""
    obs = subprocess.run([config.RCLONE_PATH, "obscure", password],
                         capture_output=True, text=True, timeout=30)
    if obs.returncode != 0:
        logging.error(f"rclone obscure failed for {name}")
        return False
    block = f"\n[{name}]\ntype = mega\nuser = {email}\npass = {obs.stdout.strip()}\n"
    try:
        with config.RCLONE_CONF_PATH.open("a", encoding="utf-8") as fh:
            fh.write(block)
        # Publish through install_rclone_conf, NOT shutil.copy2. copy2 opens the destination and
        # TRUNCATES it before copying a byte, so every rclone process on the machine -- including
        # 16 parallel scan workers -- has a window in which the active config is empty or partial.
        # A reader inside it dies with `didn't find section in config file`, which is also a
        # stale-session signature, so it triggers purge_mega_session and a SECOND writer to the
        # same file. install_rclone_conf writes a temp file and os.replace()s it, which is atomic,
        # and holds the same lock as purge_mega_session so publish and purge cannot interleave.
        # This is the identical bug install_rclone_conf was written to fix, reintroduced here by a
        # separate copy path.
        dst = Path.home() / ".config/rclone/rclone.conf"
        if not install_rclone_conf(config.RCLONE_CONF_PATH, dst):
            logging.error(f"appended {name} to the repo rclone.conf but could not publish it to "
                          f"{dst}; the account is committed and the next cycle's config deploy "
                          f"will install it.")
        return True
    except OSError as e:
        logging.error(f"failed to write rclone.conf for {name}: {e}")
        return False


def _commit_push() -> None:
    repo = str(config.SCRIPT_DIR.parent)
    subprocess.run([config.GIT_PATH, "-C", repo, "add", "rclone.conf"], capture_output=True)
    subprocess.run([config.GIT_PATH, "-C", repo, "commit", "-m",
                    "Auto-add MEGA account(s) to the pool"], capture_output=True)
    r = subprocess.run([config.GIT_PATH, "-C", repo, "push", "origin", "main"],
                       capture_output=True, text=True)
    logging.info("rclone.conf committed" + ("; pushed." if r.returncode == 0
                 else f"; push failed ({r.stderr.strip()[:120]})"))


def create_accounts(count: int) -> int:
    """Create up to `count` accounts. Returns how many succeeded. Inert (0) without the
    IMAP app-password."""
    app_pw = _app_password()
    if not app_pw:
        logging.warning("account creation needed but MEGA_APP_PASSWORD_FILE is absent "
                        f"({config.MEGA_APP_PASSWORD_FILE}); MONITORING only. Drop the base "
                        "inbox's IMAP app-password there to enable auto-creation.")
        return 0
    made = 0
    idx = _next_index()
    skips = 0
    while made < count and skips < config.ACCOUNTS_MAX_PER_RUN:
        name = f"{config.ACCOUNT_ALIAS_PREFIX}{idx}"
        email = _alias_email(name)
        pw = _random_password()
        logging.info(f"creating MEGA account {name} ({email})...")
        result = _register(name, email, pw, app_pw)
        if result is REGISTER_EXISTS:
            # Orphaned alias: the email already has a MEGA account with no rclone.conf entry
            # (and we never stored its password), so it is unrecoverable. Skip past it instead
            # of aborting -- _next_index() derives from rclone.conf, which does not know this
            # alias exists, so we must advance by hand or the same index would EEXIST forever.
            # `skips` bounds how many orphans we walk past before giving up, so a pathological
            # run of them can't spin the batch forever.
            logging.warning(f"{name} ({email}) already registered at MEGA (orphaned alias); "
                            f"skipping to the next index.")
            idx += 1
            skips += 1
            continue
        if not result:
            logging.error(f"account {name} creation failed; stopping this batch.")
            break
        # The append and the commit are ONE unit as far as the config pull is concerned. A pull
        # that lands between them sees an uncommitted rclone.conf and aborts; the lock closes
        # that window. Registration deliberately sits OUTSIDE it -- it takes minutes (MEGA
        # signup plus an IMAP confirmation round-trip) and holding a git lock across it would
        # block every pull on the machine for the whole batch.
        with git_tree_lock(block=True) as got_lock:
            if not got_lock:
                logging.warning(f"could not take the repo tree lock within the timeout while "
                                f"adding {name}; proceeding unlocked. A concurrent config pull "
                                f"may abort and retry next cycle -- the account is not lost.")
            if not _append_remote(name, email, pw):
                logging.error(f"account {name} creation failed; stopping this batch.")
                break
            _commit_push()
        made += 1
        skips = 0
        logging.info(f"account {name} live and added to the pool.")
        time.sleep(config.ACCOUNT_CREATE_MIN_INTERVAL_SEC)
        idx = _next_index()
    return made


def accounts_for_bytes(deficit: int) -> int:
    """How many accounts cover `deficit` bytes, at REMOTE_CAP_BYTES usable each, capped."""
    if deficit <= 0:
        return 0
    per = max(1, config.REMOTE_CAP_BYTES)
    return min(config.ACCOUNTS_MAX_PER_RUN, -(-int(deficit) // per))   # ceil-div


def provision_floor_bytes(drain_bps: float = 0.0) -> int:
    """The free-space floor, as max(absolute floor, POOL_PROVISION_LEAD_HOURS of drain).

    Expressing it in HOURS is the point. The old fixed 200 GB was silently a time budget --
    23 h of runway at the serial uploader's 8.5 GB/h, and only 3 h once 12 workers took the
    drain to ~68 GB/h. Nobody restated it when the rate changed, so the safety margin
    evaporated without a single line of config looking wrong. Deriving it from the OBSERVED
    rate keeps it correct through the next throughput change as well.
    """
    lead = int(drain_bps * config.POOL_PROVISION_LEAD_HOURS * 3600)
    return max(config.POOL_LOW_FREE_BYTES, lead)


def ensure_capacity(free: int = None, pending: int = 0, drain_bps: float = 0.0) -> int:
    """Provision accounts if the pool is short. Returns how many were created.

    Two independent triggers, and the second is the one that stops a backlog stalling:

    * **Reactive** -- placeable free space has fallen below the drain-derived floor. Batch
      size is whatever closes the gap, not a fixed 5: at 68 GB/h a 5-account batch (~100 GB)
      buys 88 minutes, so a fixed batch just means provisioning again almost immediately.
    * **Proactive** -- `pending` (bytes present locally but on no remote) exceeds placeable
      free space. That deficit is knowable the moment new content lands, long before free
      space approaches any floor, so there is no reason to wait for the floor. This is what
      makes "a big drop appeared" provision capacity up front instead of discovering the
      shortfall 2 TB into the upload.

    `free` is PLACEABLE free (raw free minus the per-claim fill margin, excluded remotes
    dropped), not aggregate free -- see `pool_placeable_bytes` and `placeable_free_sum`. It
    may be passed in (the upload phase has a live ledger and should not pay for an `rclone
    about` sweep); omitted, it is measured from free_space.json.
    """
    if free is None:
        free = pool_placeable_bytes()
    G = 1024**3
    floor = provision_floor_bytes(drain_bps)

    need = 0
    why = ""
    if config.POOL_PROVISION_PROACTIVE and pending > free:
        deficit = int((pending - free) * config.POOL_PROVISION_DEFICIT_MARGIN)
        need = accounts_for_bytes(deficit)
        why = (f"pending {pending/G:.0f}GB exceeds placeable free {free/G:.0f}GB "
               f"by {(pending-free)/G:.0f}GB")
    elif free < floor:
        need = accounts_for_bytes(floor - free)
        why = (f"placeable free {free/G:.0f}GB < floor {floor/G:.0f}GB"
                + (f" ({drain_bps*3600/G:.0f}GB/h drain x "
                   f"{config.POOL_PROVISION_LEAD_HOURS:.0f}h lead)" if drain_bps else ""))
    if need <= 0:
        return 0

    logging.info(f"provisioning {need} account(s): {why}")
    return create_accounts(need)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Automatic MEGA account provisioning.")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--ensure", action="store_true")
    ap.add_argument("--create", type=int, metavar="N")
    args = ap.parse_args()
    if args.status or not (args.ensure or args.create):
        G = 1024**3
        print(f"pool free (capped):   {pool_free_bytes()/G:.0f} GB")
        print(f"pool placeable:       {pool_placeable_bytes()/G:.0f} GB")
        print(f"low-water floor:      {config.POOL_LOW_FREE_BYTES/G:.0f} GB")
        print(f"next account name:    {config.ACCOUNT_ALIAS_PREFIX}{_next_index()}")
        print(f"email base:           {config.ACCOUNT_EMAIL_BASE}")
        print(f"app-password present: {_app_password() is not None} "
              f"({config.MEGA_APP_PASSWORD_FILE})")
        print(f"megatools:            {config.MEGATOOLS_BIN}")
        return 0
    if args.create:
        print(f"created {create_accounts(args.create)} account(s)")
        return 0
    ensure_capacity()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
