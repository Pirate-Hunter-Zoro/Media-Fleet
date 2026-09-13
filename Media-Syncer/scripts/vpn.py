from .utils import run_command
from typing import Optional
from .config import TAILSCALE_PATH
from . import config
import json
import logging
import time


def _country_of(hostname: str) -> str:
    """Mullvad exit hostnames begin with the 2-letter country code (`us-atl-wg-001` -> us)."""
    return hostname.split("-", 1)[0].lower() if hostname else ""


def _fast_allowlist() -> set[str]:
    """IPs measured fast by `scripts/benchmark_exit_nodes.py`, or an empty set.

    Empty is a legitimate answer and every caller treats it as "no allowlist, use the country
    filter" -- never as "no nodes". A missing file, a corrupt one, and an expired one all take
    that path, because rotation losing its node list is a worse failure than rotating onto a
    slow node.
    """
    path = getattr(config, "FAST_EXIT_NODES_FILE", None)
    if not path:
        return set()
    try:
        raw = json.loads(path.read_text())
        age = time.time() - path.stat().st_mtime
    except (OSError, ValueError) as e:
        logging.debug(f"No usable fast-exit-node allowlist ({e}); falling back to country filter.")
        return set()
    if age > config.FAST_EXIT_MAX_AGE_SEC:
        logging.warning(f"Fast-exit-node allowlist is {age / 86400:.0f} days old "
                        f"(max {config.FAST_EXIT_MAX_AGE_SEC / 86400:.0f}); ignoring it and using "
                        f"every allowed node. Re-measure with "
                        f"`python3 -m scripts.benchmark_exit_nodes`.")
        return set()
    return {n["ip"] for n in raw.get("nodes", []) if n.get("ip")}


def get_exit_nodes() -> list[str]:
    """Return the exit-node IPs rotation is allowed to select, best filter first.

    Two filters, applied in order, each with a reason it cannot be skipped:

    1. **Country.** The exit node is machine-global, so routing through a country where the
       the free-model chain API is unavailable (config.BLOCKED_EXIT_COUNTRIES) breaks every unattended
       AI call on the machine. Blocking the short unsupported set costs almost no IP diversity.

    2. **Measured speed.** Only nodes the benchmark clocked at or above
       config.FAST_EXIT_MIN_UPLOAD_BPS. The node sets the fleet's aggregate upload ceiling, and
       a slow one takes throughput to ZERO rather than merely reducing it -- transfers stop
       fitting inside TIMEOUT(), so nothing ever completes and the same files retry forever.
       See config.FAST_EXIT_MIN_UPLOAD_BPS for the measurements behind this.

    The fallback chain is deliberate and ordered by how bad each outcome is: allowlist ∩ live,
    then country-filtered live, then the unfiltered live list. Returning an empty list would
    make `rotate_exit_node()` raise on the modulo and take rotation out permanently, so every
    branch here yields something. A slow node is a bad day; no rotation at all is a dead daemon.
    """
    output, _ = run_command([TAILSCALE_PATH, "exit-node", "list"])
    allowed, all_ips = set(), set()
    blocked = getattr(config, "BLOCKED_EXIT_COUNTRIES", set())
    for line in output.strip().splitlines()[1:]:  # skip header
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        ip, hostname = parts[0], parts[1]
        if not ip[0].isdigit():
            continue                              # header/blank row guard
        all_ips.add(ip)
        if _country_of(hostname) not in blocked:
            allowed.add(ip)

    # Intersect with what Tailscale currently offers rather than trusting the file: an IP that
    # was fast last month and has since been retired is not a candidate, and a list of retired
    # IPs would have rotation cycling through nodes that cannot be selected at all.
    # Sorted, not set order: rotate_exit_node() steps through this list by index, so a stable
    # order is what makes rotation actually cyclic. With set order the sequence differs between
    # processes and between calls, and a small allowlist can then revisit the node it just left.
    fast = _fast_allowlist() & allowed
    if fast:
        return sorted(fast)
    if _fast_allowlist():
        logging.warning("None of the measured-fast exit nodes are currently offered by "
                        "Tailscale; falling back to every allowed node. Re-measure with "
                        "`python3 -m scripts.benchmark_exit_nodes`.")
    return sorted(allowed) if allowed else sorted(all_ips)

def get_current_exit_node() -> Optional[str]:
    """Return the current active exit node IP

    Returns:
        Optional[str]: Said current IP - technically defautls to None but that should never happen if Tailscale is running
    """
    output, _ = run_command([TAILSCALE_PATH, "exit-node", "list"])
    for line in output.strip().splitlines()[1:]:  # skip header
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 0:
            continue
        ip = parts[0]
        selected_status = parts[-1]
        if selected_status == "selected":
            return ip
    return None

def _rotation_throttled(claim_only: bool = False) -> bool:
    """True if we switched exit nodes too recently (cross-process, via a shared timestamp
    file). Each switch resets every TCP connection on the machine, including the fleet's
    the free-model chain calls, so we cap the rate. Non-fatal on any error (never
    block rotation because the stamp file misbehaved).

    `claim_only` skips the rate check and only claims the slot -- see rotate_exit_node's
    `bypass_throttle`."""
    import time
    try:
        stamp = config.ROTATE_STAMP_FILE
        now = time.time()
        try:
            last = stamp.stat().st_mtime
        except OSError:
            last = 0.0
        if not claim_only and now - last < config.ROTATE_MIN_INTERVAL_SEC:
            return True
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()          # claim this rotation slot for all processes
        return False
    except Exception:          # noqa: BLE001
        return False


def rotate_exit_node(bypass_throttle: bool = False) -> bool:
    """
    Move to the next exit node -- but at most once per config.ROTATE_MIN_INTERVAL_SEC across
    all processes, so exit-node churn stops killing in-flight AI API requests.

    `bypass_throttle` is for ONE caller: the watchdog, once its probe has confirmed that
    nothing at all gets off this box. The rate limit exists to protect in-flight work from
    being reset -- and in that state there is no in-flight work, because nothing can reach
    anywhere. Paying it anyway is what turned the 2026-09-01 outage into a five-hour one:
    Mullvad had a long run of nodes carrying no traffic, rotation is the only repair for
    that and it eventually worked, but a 180-second floor between attempts meant sweeping
    the list took all evening while the library sat unreachable. The throttle still applies
    to every other caller, where live connections really are at stake.

    The stamp is still claimed on a bypassed rotation, so other processes see that one
    happened and do not immediately add their own.

    Returns:
        bool: True only if the exit node actually changed. Callers that count failures toward a
            rotation threshold need this: resetting their counter after a throttled or failed
            call would discard the evidence without ever acting on it, and the node they were
            trying to escape would keep its work.
    """
    if _rotation_throttled(claim_only=bypass_throttle):
        return False           # switched too recently; keep the current node (spare the API)
    # Cyclic switching
    current_ip = get_current_exit_node()
    node_ips = get_exit_nodes()
    if not node_ips:
        # get_exit_nodes' fallback chain makes this near-impossible, but the modulo below would
        # raise ZeroDivisionError and kill the caller's thread, so it is checked rather than
        # assumed away.
        logging.error("No exit nodes available to rotate to.")
        return False
    try:
        idx = node_ips.index(current_ip)
    except ValueError:
        idx = -1 # So next round lands on zero
    next_idx = (idx + 1) % len(node_ips)
    next_ip = node_ips[next_idx]
    logging.info(f"Switching from {current_ip} to {next_ip}....")
    _, error = run_command([TAILSCALE_PATH, "set", f"--exit-node={next_ip}"])
    if error != "":
        logging.error(f"Error switching IPs: {error}...")
        return False
    return True
