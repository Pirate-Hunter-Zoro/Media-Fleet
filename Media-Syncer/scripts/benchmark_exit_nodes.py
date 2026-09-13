"""
Measure every Tailscale/Mullvad exit node's throughput and publish the fast ones.

The aggregate upload ceiling of the whole fleet is the selected exit node, not MEGA and not
the ethernet. That ceiling varies by more than two orders of magnitude between nodes, and a
slow one does not merely reduce throughput -- it takes it to zero, because a transfer that
cannot finish inside `config.TIMEOUT(size)` is killed and retried forever. So which nodes are
fast is a fact the daemon has to know, and the only way to know it is to measure.

What is measured is UPLOAD throughput, because that is the direction the fleet is bottlenecked
in: the daemon's job is pushing bytes to MEGA. Download is recorded too, as a secondary signal.

Both probes run against `speed.cloudflare.com`, which is deliberately NOT one of the
split-tunnelled destinations (`scripts/split_tunnel.sh` pins the AI providers, Google,
and the torrent indexers around the tunnel), so the measurement rides the exit node
exactly as a MEGA transfer does.

The probe is a tunnel-ceiling measurement, not a MEGA one. That is on purpose: rclone's mega
backend uploads a file over a SINGLE connection capped around 2.4 MB/s, so a single-stream
MEGA probe is MEGA-bound and cannot tell a 24 MB/s node from a 42 MB/s one. It can only spot
the catastrophic case -- which the cheap tunnel probe already spots, and spots in seconds
rather than minutes. Verified against the live daemon: this probe read 0.060 MB/s on
`za-jnb-wg-001` at the same moment the running upload phase was managing 0.054 MB/s per
worker through it.

Usage (the daemon must be stopped first -- see below):

    python3 -m scripts.benchmark_exit_nodes                 # every allowed node
    python3 -m scripts.benchmark_exit_nodes --countries us,ca
    python3 -m scripts.benchmark_exit_nodes --min-upload 12 # stricter allowlist
    python3 -m scripts.benchmark_exit_nodes --report        # re-print last results, no probing

Writes two files at the repo root:

  * `exit_node_speeds.json`  -- the full ranked measurement, for the record.
  * `fast_exit_nodes.json`   -- the allowlist `vpn.get_exit_nodes()` actually consumes.

Both are gitignored (`*.json`), so the allowlist is per-host measured fact rather than a
committed constant -- which is correct, since the fast set depends on where the machine is.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .utils import run_command, setup_logging, write_json_atomic
from .vpn import _country_of, get_current_exit_node

# Cloudflare's `__down` endpoint 403s above ~32 MB, so the download probe asks for 25 MB.
# The upload probe sends a fixed random payload; random rather than zeroes so nothing in the
# path can compress it into a flattering number.
_PROBE_UPLOAD_BYTES = 24 * 1024 * 1024
_PROBE_DOWNLOAD_BYTES = 25 * 1024 * 1024
_DOWN_URL = f"https://speed.cloudflare.com/__down?bytes={_PROBE_DOWNLOAD_BYTES}"
_UP_URL = "https://speed.cloudflare.com/__up"
_READY_URL = "https://speed.cloudflare.com/__down?bytes=1000"

# Per-probe wall clock. curl reports speed over what it actually managed to transfer, so a
# node too slow to finish still yields a valid (small) number instead of stalling the sweep.
_PROBE_MAX_SEC = 12
_READY_MAX_SEC = 3
_READY_TRIES = 8


def _daemon_running() -> bool:
    """True if media_sync is up. Its 16 concurrent uploads would both poison every reading
    and be destroyed by the switching, so the sweep refuses to run alongside it.

    Not via run_command: pgrep exits 1 to mean "no match", which is the ANSWER here, and
    run_command logs every non-zero exit as a command failure. Reading "ERROR - Command failed"
    at the top of a clean run sends you looking for a fault that does not exist.
    """
    try:
        proc = subprocess.run(["/usr/bin/pgrep", "-f", "scripts.media_sync"],
                              capture_output=True, text=True, timeout=10)
        return bool(proc.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return False        # cannot tell; the sweep's own readings will show if it is wrong


def _list_nodes() -> list[tuple[str, str]]:
    """Every exit node as (ip, hostname), deduplicated by IP, blocked countries removed.

    The blocked set is the same one `vpn.get_exit_nodes()` enforces (countries where the
    an AI API is unavailable). Measuring a node we would never select is wasted time, and
    actually selecting one mid-sweep would break the AI calls the rest of the fleet
    makes.
    """
    output, _ = run_command([config.TAILSCALE_PATH, "exit-node", "list"])
    blocked = getattr(config, "BLOCKED_EXIT_COUNTRIES", set())
    seen, nodes = set(), []
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0][0].isdigit():
            continue
        ip, hostname = parts[0], parts[1]
        if ip in seen or _country_of(hostname) in blocked:
            continue
        seen.add(ip)
        nodes.append((ip, hostname))
    return nodes


def _curl_speed(args: list[str], field: str) -> float:
    """Run curl and return the requested `-w` field as a float, 0.0 on any failure.

    Every flag and its value are SEPARATE argv elements. macOS curl rejects the `--flag=value`
    form outright (`option --max-time=12: is unknown`) -- and because the readiness probe uses
    this same helper, getting it wrong does not surface as a bad number, it surfaces as every
    node in the fleet reading exactly 0.00 and being scored unreachable. Which is indistinguishable
    from a genuinely broken tunnel, so: keep them separate.
    """
    try:
        proc = subprocess.run(
            ["/usr/bin/curl", "-s", "-o", "/dev/null", "-w", f"%{{{field}}}", *args],
            capture_output=True, text=True, timeout=_PROBE_MAX_SEC + 10,
        )
        return float(proc.stdout.strip() or 0.0)
    except (subprocess.TimeoutExpired, ValueError):
        return 0.0


def _select(ip: str) -> bool:
    """Point Tailscale at `ip` and wait until traffic actually flows through it.

    The readiness poll is not optional: `tailscale set` returns in ~0.1 s, long before the
    WireGuard handshake completes, and probing inside that window measures the handshake
    rather than the node.
    """
    _, err = run_command([config.TAILSCALE_PATH, "set", f"--exit-node={ip}"])
    if err:
        logging.warning(f"Could not select exit node {ip}: {err}")
        return False
    for _ in range(_READY_TRIES):
        if _curl_speed(["--max-time", str(_READY_MAX_SEC), _READY_URL], "http_code") == 200:
            return True
        time.sleep(1)
    return False


def _measure(ip: str, hostname: str, payload: Path) -> dict:
    """Probe one node. Upload first: it is the metric the allowlist is built on, so it gets
    the freshest tunnel, before the download probe has had a chance to load it."""
    if not _select(ip):
        return {"ip": ip, "hostname": hostname, "up_bps": 0.0, "down_bps": 0.0,
                "ok": False, "note": "unreachable"}
    up = _curl_speed(["--max-time", str(_PROBE_MAX_SEC),
                      "--data-binary", f"@{payload}", _UP_URL], "speed_upload")
    down = _curl_speed(["--max-time", str(_PROBE_MAX_SEC), _DOWN_URL], "speed_download")
    return {"ip": ip, "hostname": hostname, "up_bps": up, "down_bps": down, "ok": up > 0}


def _mbps(bps: float) -> float:
    return bps / (1024 * 1024)


def _print_table(results: list[dict], threshold_bps: float) -> None:
    print(f"\n{'HOSTNAME':<34} {'UP MB/s':>9} {'DOWN MB/s':>10}   VERDICT")
    print("-" * 72)
    for r in results:
        verdict = "FAST" if r["up_bps"] >= threshold_bps else ("slow" if r["ok"] else "DEAD")
        print(f"{r['hostname']:<34} {_mbps(r['up_bps']):>9.2f} {_mbps(r['down_bps']):>10.2f}   {verdict}")


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Measure exit-node throughput; publish the fast set.")
    parser.add_argument("--countries", default="",
                        help="Comma-separated 2-letter codes to restrict the sweep to (default: all allowed).")
    parser.add_argument("--min-upload", type=float, default=None,
                        help=f"Allowlist threshold in MB/s (default: config.FAST_EXIT_MIN_UPLOAD_BPS "
                             f"= {_mbps(config.FAST_EXIT_MIN_UPLOAD_BPS):.0f}).")
    parser.add_argument("--report", action="store_true",
                        help="Re-print the last sweep and re-derive the allowlist without probing.")
    args = parser.parse_args()

    threshold = (args.min_upload * 1024 * 1024) if args.min_upload is not None \
        else config.FAST_EXIT_MIN_UPLOAD_BPS

    if args.report:
        try:
            results = json.loads(config.EXIT_NODE_SPEEDS_FILE.read_text())["results"]
        except (OSError, KeyError, ValueError) as e:
            print(f"No usable previous sweep at {config.EXIT_NODE_SPEEDS_FILE}: {e}")
            return 1
    else:
        if _daemon_running():
            print("media_sync is running. Stop it first -- 16 concurrent uploads would poison "
                  "every reading, and the exit-node switching would kill them all:\n"
                  "  launchctl bootout gui/$UID/com.mikeyferguson.mediasyncwatchdog\n"
                  "  launchctl bootout gui/$UID/com.mikeyferguson.mediasync")
            return 1

        nodes = _list_nodes()
        if args.countries:
            wanted = {c.strip().lower() for c in args.countries.split(",") if c.strip()}
            nodes = [n for n in nodes if _country_of(n[1]) in wanted]
        if not nodes:
            print("No candidate exit nodes matched.")
            return 1

        original = get_current_exit_node()
        payload = Path(config.SCRIPT_DIR.parent) / ".exit_probe_payload.bin"
        payload.write_bytes(os.urandom(_PROBE_UPLOAD_BYTES))

        results = []
        try:
            for i, (ip, hostname) in enumerate(nodes, 1):
                r = _measure(ip, hostname, payload)
                results.append(r)
                print(f"[{i:>3}/{len(nodes)}] {hostname:<34} "
                      f"up {_mbps(r['up_bps']):>7.2f} MB/s   down {_mbps(r['down_bps']):>7.2f} MB/s"
                      f"{'' if r['ok'] else '   (unreachable)'}")
        finally:
            payload.unlink(missing_ok=True)
            # Always hand the tunnel back, even on Ctrl-C: leaving the machine parked on
            # whatever node the sweep happened to die on is how a "quick benchmark" becomes
            # the next day-long stall.
            if original:
                run_command([config.TAILSCALE_PATH, "set", f"--exit-node={original}"])

        results.sort(key=lambda r: r["up_bps"], reverse=True)
        write_json_atomic(config.EXIT_NODE_SPEEDS_FILE,
                          {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                           "probe_upload_bytes": _PROBE_UPLOAD_BYTES,
                           "results": results}, indent=2)

    results.sort(key=lambda r: r["up_bps"], reverse=True)
    fast = [r for r in results if r["up_bps"] >= threshold]
    _print_table(results, threshold)

    if not fast:
        print(f"\nNothing cleared {_mbps(threshold):.1f} MB/s. Allowlist NOT written -- an empty "
              f"one would strand rotation on the fallback. Lower --min-upload and re-run.")
        return 1

    # Atomic: vpn.get_exit_nodes() reads this live in media_sync, mediafs and predownload,
    # so a truncating rewrite mid-sweep would hand a rotating daemon an empty allowlist.
    write_json_atomic(config.FAST_EXIT_NODES_FILE,
                      {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "min_upload_bps": threshold,
                       "nodes": [{"ip": r["ip"], "hostname": r["hostname"],
                                  "up_bps": r["up_bps"]} for r in fast]}, indent=2)

    print(f"\n{len(fast)} of {len(results)} nodes cleared {_mbps(threshold):.1f} MB/s upload.")
    print(f"Allowlist -> {config.FAST_EXIT_NODES_FILE}")
    print(f"Full sweep -> {config.EXIT_NODE_SPEEDS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
