"""Tailscale watchdog: keep the VPN path alive, because the whole fleet is bound to it.

Tailscale is an unsupervised single point of failure on this box:

  * qBittorrent is bound to the 100.x CGNAT address, so when it disappears Torrent-Ingest
    refuses to start or continue ANY download ("Tailscale down; ... Idling.") -- it does not
    leak traffic, which is correct, but it also never recovers on its own.
  * Every MEGA transfer (sync, pre-download, streaming reads) rides the Mullvad exit node,
    so a dead tunnel stalls replication too.

The Mac app's network extension is started by the GUI app, NOT by launchd, so nothing brings
it back when it dies. On 2026-08-04 it dropped at 02:22 local and Torrent-Ingest idled for
~6.5 hours until the app was relaunched by hand. This daemon closes that hole.

Health is the same env-independent test Torrent-Ingest uses -- is a 100.64.0.0/10 CGNAT
address bound to an interface -- NOT `tailscale status`, whose socket the CLI cannot always
reach under launchd's stripped environment. That test is also the *exact* condition that
matters, since it is the address qBittorrent binds.

Recovery escalates, cheapest first, and each step is given a grace period to take effect:

  1. `open -a Tailscale`     -- relaunches the app if it is not running (the common case).
  2. `tailscale up`          -- app alive but tunnel down: bring the tunnel back up.
  3. quit + `open -a Tailscale` -- a wedged extension needs a full restart.

Separately, a tunnel that is up but has NO exit node selected is also a fault worth fixing:
MEGA throttle avoidance depends on the exit node, and losing it silently degrades every
transfer. When that happens the watchdog selects one (respecting the country blocklist, so
it never parks on a node that breaks the AI APIs).

And a tunnel that is up with an exit node selected is still not necessarily a tunnel that
carries anything. A Mullvad node can go dead while staying selected and "active", at which
point the box has no internet while every local check reads healthy. `ensure_traffic_flows`
probes for that by IP and rotates away from the node when it stops passing traffic.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import re
import subprocess
import sys
import time

from . import config
from . import utils
from . import vpn


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[RotatingFileHandler(config.TS_WATCHDOG_LOG,
                                      maxBytes=config.LOG_MAX_BYTES,
                                      backupCount=config.LOG_BACKUP_COUNT),
                  logging.StreamHandler()],
    )


# --- health ------------------------------------------------------------------

def tailscale_address() -> str | None:
    """The bound Tailscale CGNAT address (100.64.0.0/10), or None if the tunnel is down.

    Deliberately reads `ifconfig` rather than asking the Tailscale CLI: this machine runs the
    Tailscale *Mac app*, whose socket the CLI cannot reach under launchd's stripped
    environment, and the bound address is the precise thing qBittorrent depends on anyway.
    """
    try:
        r = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for m in re.finditer(r"inet (100\.\d+\.\d+\.\d+)", r.stdout):
        if 64 <= int(m.group(1).split(".")[1]) <= 127:      # 100.64.0.0/10 == CGNAT
            return m.group(1)
    return None


# The probe itself lives in utils, because the stale-session healer asks the same question
# ("does anything get off this box?") to tell a dead network from a dead MEGA session, and
# two implementations of one test are how they come to disagree (§4.106).
internet_reachable = utils.internet_reachable


def app_running() -> bool:
    try:
        r = subprocess.run(["/usr/bin/pgrep", "-f", "Tailscale.app/Contents/MacOS/Tailscale"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# --- recovery ----------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stderr or r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def _wait_for_address(grace_sec: int) -> str | None:
    """Poll for the CGNAT address to appear, up to `grace_sec`. Re-auth and interface setup take
    tens of seconds, so a single post-fix check would report failure while recovery is working."""
    deadline = time.time() + grace_sec
    while time.time() < deadline:
        addr = tailscale_address()
        if addr:
            return addr
        time.sleep(3)
    return tailscale_address()


def _open_app() -> None:
    rc, err = _run(["/usr/bin/open", "-a", "Tailscale"])
    logging.info(f"launched Tailscale.app (rc={rc})" + (f": {err}" if err else ""))


def _tailscale_up() -> None:
    rc, err = _run([config.TAILSCALE_PATH, "up"], timeout=120)
    logging.info(f"`tailscale up` (rc={rc})" + (f": {err}" if err else ""))


def _restart_app() -> None:
    rc, err = _run(["/usr/bin/osascript", "-e", 'quit app "Tailscale"'])
    logging.info(f"quit Tailscale.app (rc={rc})" + (f": {err}" if err else ""))
    time.sleep(5)
    _open_app()


def recover() -> str | None:
    """Escalating repair. Returns the restored address, or None if every step failed."""
    grace = config.TS_WATCHDOG_RECOVER_GRACE_SEC

    if not app_running():
        logging.warning("Tailscale.app is not running -- launching it")
        _open_app()
        addr = _wait_for_address(grace)
        if addr:
            return addr

    logging.warning("tunnel still down -- bringing it up")
    _tailscale_up()
    addr = _wait_for_address(grace)
    if addr:
        return addr

    logging.error("still down -- restarting Tailscale.app")
    _restart_app()
    return _wait_for_address(grace)


# Exit nodes probed dead since the last successful probe, and whether the widespread-outage
# escalation has already been logged. Cleared the moment traffic flows again.
_dead_nodes: set[str] = set()
_breaker_state = {"escalated": False}


def ensure_traffic_flows(dead_streak: int) -> int:
    """Rotate away from an exit node that is selected but carrying nothing. Returns the streak.

    This is the failure the address check cannot see. On 2026-08-14 a Mullvad node went dead
    while still reading as the active exit node; the box lost DNS and all routing for
    minutes, torrents stalled, MEGA stalled, and the watchdog logged nothing because the
    CGNAT address never went away. Recovery only happened because Media-Syncer's transfer
    layer independently rotated on its own failures -- which is luck, not supervision, and
    would not have come at all on an idle box.

    Rotation goes through vpn.rotate_exit_node so the country blocklist (nodes where the
    an AI API is unreachable) and the cross-process rate limit both still apply -- this must
    not become a second, competing rotator. It is also debounced hard: mid-rotation the path
    is legitimately dead for a few seconds, and a watchdog that fired on that would chase the
    rotator around the node list forever.

    No escalation past rotation. If the tunnel itself were the problem the address check would
    have caught it; here the tunnel is fine and the node is not, and the only repair is a
    different node. Each successive round moves one further along the list.

    Two things change once the path is CONFIRMED dead, and both exist because this took five
    hours on 2026-09-01 when it should have taken well under one. The sweep stops paying the
    rotation rate limit, which protects in-flight connections that by definition do not
    exist in that state; and it stays in the confirmed-dead state between rotations rather
    than re-earning the probe streak each time. Entering the state still costs the full
    streak, so a blip cannot start a sweep. TS_WATCHDOG_DEAD_NODE_LIMIT only controls when
    the log escalates from "this node" to "most of the list" -- it never stops the sweep,
    because sweeping is the only repair that exists here.
    """
    if internet_reachable():
        if _dead_nodes:
            logging.info(f"traffic flows again through exit node "
                         f"{vpn.get_current_exit_node()} after {len(_dead_nodes)} dead "
                         f"node(s) this outage")
            _dead_nodes.clear()
            _breaker_state["escalated"] = False
        return 0
    dead_streak += 1
    if dead_streak < config.TS_WATCHDOG_PROBE_FAIL_STREAK:
        return dead_streak
    node = vpn.get_current_exit_node()
    _dead_nodes.add(node or "(none)")

    # SAY WHAT IS HAPPENING, ONCE. A run of dead nodes and a single dead node produce the
    # same line over and over, so five hours of this outage read as one repeated complaint
    # rather than "most of the exit-node list is not carrying traffic". The behaviour below
    # does not change -- rotation is the only repair there is, and on 2026-09-01 it did
    # eventually work -- but the log should distinguish the two, because they need different
    # things from a human.
    if (len(_dead_nodes) >= config.TS_WATCHDOG_DEAD_NODE_LIMIT
            and not _breaker_state["escalated"]):
        _breaker_state["escalated"] = True
        logging.error(
            f"{len(_dead_nodes)} distinct exit nodes have now carried no traffic with no "
            f"success between them. This is a widespread exit-node outage, not one bad "
            f"node: MEGA replication, mediafs reads and every torrent are down for its "
            f"duration. Continuing to sweep the node list, which is the only repair; if it "
            f"does not clear, check the Mullvad add-on on the Tailscale account.")

    # Once the outage has been reported as widespread, the per-node line is progress, not
    # news: at ERROR it would put ~87 identical-looking failures in front of whoever comes
    # to read why the fleet was down, which is how the real message gets lost.
    say = logging.info if _breaker_state["escalated"] else logging.error
    say(f"exit node {node or '(none)'} is selected but no traffic reaches the internet "
        f"({dead_streak} consecutive probes) -- rotating away")

    # BYPASS THE ROTATION RATE LIMIT while the path is confirmed dead. That limit exists to
    # keep exit-node churn from resetting in-flight connections; with nothing reaching the
    # internet there are none to protect, so paying its 180 seconds per node buys nothing
    # and costs the sweep. It is what made this outage last five hours instead of one: the
    # list was being swept correctly, just at 3.4 minutes a node.
    if vpn.rotate_exit_node(bypass_throttle=True):
        logging.info(f"rotated to {vpn.get_current_exit_node()}")
        # NOT reset to 0. Returning the streak keeps the state machine in "confirmed dead",
        # so the next failed probe rotates again on the following poll instead of spending
        # four more probes re-establishing what is already known. Re-entering the dead state
        # still costs the full streak, so a single blip cannot start a sweep.
        return dead_streak
    logging.warning("rotation failed; will retry next poll")
    return dead_streak


def ensure_exit_node(missing_streak: int) -> int:
    """Re-select an exit node if none has been active for a sustained stretch. Returns the new
    streak count.

    Losing the exit node does not break connectivity, so nothing else on the box notices -- but
    MEGA throttle avoidance quietly stops working. Two things make this dangerous to fix eagerly,
    which is why it is debounced AND throttle-aware:

      * `media_sync` and `mediafs` rotate the exit node constantly, and there is a window mid-
        switch where no node reads as `selected`. Acting on that single observation would have the
        watchdog racing the rotator and pinning a node it just moved off.
      * Setting the exit node resets every TCP connection on the machine (the same reason
        `ROTATE_MIN_INTERVAL_SEC` exists), so doing it on a whim kills in-flight AI calls.

    So: only after TS_WATCHDOG_FAIL_STREAK consecutive pollings with no node, and only when no
    rotation has been claimed recently. Selection goes through vpn.py so the country blocklist
    (nodes where an AI API is unavailable) is honoured.
    """
    try:
        if vpn.get_current_exit_node():
            return 0
        missing_streak += 1
        if missing_streak < config.TS_WATCHDOG_FAIL_STREAK:
            return missing_streak
        try:                                    # a live rotation owns the node; do not fight it
            last = config.ROTATE_STAMP_FILE.stat().st_mtime
            if time.time() - last < config.ROTATE_MIN_INTERVAL_SEC:
                return missing_streak
        except OSError:
            pass
        nodes = vpn.get_exit_nodes()
        if not nodes:
            logging.warning("no exit node selected and none available to select")
            return missing_streak
        node = sorted(nodes)[0]
        rc, err = _run([config.TAILSCALE_PATH, "set", f"--exit-node={node}"], timeout=60)
        logging.warning(f"no exit node for {missing_streak} polls -- set {node} (rc={rc})"
                        + (f": {err}" if err else ""))
        return 0
    except Exception as exc:                                            # noqa: BLE001
        logging.error(f"exit-node check failed: {exc}")
        return missing_streak


# --- heartbeat ---------------------------------------------------------------

def _write_status(**fields) -> None:
    """Record the watchdog's view of the world. Never allowed to raise: a supervisor that
    dies while reporting its own health is worse than one that does not report."""
    try:
        fields["ts"] = time.time()
        fields["iso"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = config.TS_WATCHDOG_STATUS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(fields, indent=1), encoding="utf-8")
        tmp.replace(config.TS_WATCHDOG_STATUS)          # atomic: a reader sees old or new
    except (OSError, TypeError, ValueError):
        pass


def read_status() -> dict:
    """The last heartbeat, plus `age_sec` and `stale`. `{}` when there has never been one.

    `stale` is the load-bearing field: it is the only way to detect that the WATCHDOG is
    down rather than the tunnel, and a supervisor cannot report its own absence.
    """
    try:
        d = json.loads(config.TS_WATCHDOG_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    d["age_sec"] = max(0.0, time.time() - float(d.get("ts") or 0))
    d["stale"] = d["age_sec"] > config.TS_WATCHDOG_STALE_SEC
    return d


def status_line() -> str:
    """One human-readable line about the VPN, for fleet health and `--status`."""
    d = read_status()
    if not d:
        return ("VPN: UNKNOWN -- the tailscale watchdog has never written a heartbeat "
                "(is com.mikeyferguson.tailscalewatchdog loaded?)")
    if d["stale"]:
        return (f"VPN: STALE -- the tailscale watchdog has not reported for "
                f"{d['age_sec'] / 60:.0f} min. It is not running, so NOTHING is supervising "
                f"the tunnel. `launchctl kickstart -k gui/501/com.mikeyferguson.tailscalewatchdog`")
    if not d.get("address"):
        return "VPN: DOWN -- no CGNAT address bound; qBittorrent will refuse to download."
    if not d.get("traffic_ok"):
        mins = (d.get("outage_sec") or 0) / 60
        return (f"VPN: NO TRAFFIC through exit node {d.get('exit_node') or '(none)'} for "
                f"{mins:.0f} min ({d.get('dead_nodes', 0)} node(s) tried). MEGA, mediafs "
                f"and every torrent are stalled for the duration.")
    if not d.get("exit_node"):
        return "VPN: up, but NO EXIT NODE selected -- MEGA throttle avoidance is off."
    return (f"VPN: ok -- {d.get('address')} via {d.get('exit_node')}, traffic flowing "
            f"(checked {d['age_sec']:.0f}s ago)")


# --- loop --------------------------------------------------------------------

def main() -> int:
    _setup_logging()
    addr = tailscale_address()
    logging.info(f"tailscale watchdog started (address: {addr or 'DOWN'})")

    fails = 0
    no_exit_node = 0
    dead_path = 0
    was_down = addr is None
    outage_started = None            # when traffic last stopped flowing, for the heartbeat
    while True:
        addr = tailscale_address()
        traffic_ok = None
        node = None
        if addr:
            if was_down:
                logging.info(f"tailscale is back up ({addr})")
                was_down = False
            fails = 0
            no_exit_node = ensure_exit_node(no_exit_node)
            # Only meaningful once a node is actually selected: with none, there is nothing
            # to rotate away from and ensure_exit_node above owns the repair.
            node = vpn.get_current_exit_node()
            if node:
                before = dead_path
                dead_path = ensure_traffic_flows(dead_path)
                traffic_ok = dead_path == 0
                if traffic_ok:
                    outage_started = None
                elif outage_started is None and before == 0:
                    outage_started = time.time()
            else:
                dead_path = 0
        else:
            fails += 1
            # Debounce: a rotation or a brief reconfigure drops the address for a few seconds,
            # and restarting the app over that would cause the very outage we are guarding
            # against. Only a sustained absence is a fault.
            if fails >= config.TS_WATCHDOG_FAIL_STREAK:
                was_down = True
                logging.error(f"tailscale down for {fails} consecutive polls -- recovering")
                restored = recover()
                if restored:
                    logging.info(f"recovered; tailscale address {restored}")
                    fails = 0
                    was_down = False
                else:
                    logging.error("recovery FAILED; will retry next poll")
                    fails = 0        # reset the streak so the next attempt re-escalates cleanly
        # Heartbeat every poll, healthy or not -- its FRESHNESS is what proves this
        # supervisor is alive, which is the one thing it cannot assert by staying silent.
        _write_status(
            address=addr,
            exit_node=node,
            traffic_ok=traffic_ok,
            dead_streak=dead_path,
            dead_nodes=len(_dead_nodes),
            outage_sec=(time.time() - outage_started) if outage_started else 0,
            address_fail_streak=fails,
        )
        time.sleep(config.TS_WATCHDOG_POLL_SEC)


if __name__ == "__main__":
    if "--status" in sys.argv:
        print(status_line())
        d = read_status()
        if d:
            for k in ("address", "exit_node", "traffic_ok", "dead_nodes", "outage_sec", "iso"):
                if k in d:
                    print(f"    {k}: {d[k]}")
        raise SystemExit(0 if d and not d.get("stale") and d.get("traffic_ok") is not False else 1)
    raise SystemExit(main())
