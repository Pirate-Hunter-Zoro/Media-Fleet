"""Fleet free-space report, written to the iCloud Torrents folder for phone viewing.

Called after each sync cycle by the daemon. It is deliberately SILENT -- no
`logging` at all -- so it never pollutes `media_sync.log`, and it makes NO rclone
calls of its own: it simply reads `free_space.json`, which the sync cycle already
refreshes for every remote each pass. The point is a glanceable answer to "do I
need to add more MEGA accounts yet?" that syncs to the phone via iCloud.
"""
import json
from datetime import datetime

from . import config
from .operations import inventory_bytes_by_remote, usable_free


def human_size(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.2f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.2f} EB"


def _capped_total(r: dict) -> int:
    # Cap usable capacity at REMOTE_CAP_BYTES so a temporary bonus (a 25 GB account)
    # never counts as durable space -- same rule the sync's upload decisions use.
    return min(r.get("total", 0), config.REMOTE_CAP_BYTES)


def write_report() -> None:
    """Read the per-remote free space the cycle cached and write a compact,
    phone-friendly summary to config.FREE_SPACE_REPORT_PATH. Best-effort and
    exception-swallowing: a report failure must never disturb the sync loop."""
    try:
        if not config.FREE_SPACE_PATH.exists():
            return
        remotes = json.loads(config.FREE_SPACE_PATH.read_text())
        if not isinstance(remotes, dict) or not remotes:
            return

        # Free space comes from operations.usable_free -- ONE definition of "how much room
        # does this account have", shared with the upload ledger and the provisioner. A local
        # copy of the arithmetic here is how the report ends up disagreeing with the allocator
        # about which accounts are full.
        placed = inventory_bytes_by_remote()
        free_of = {name: usable_free(r, placed.get(name, 0)) for name, r in remotes.items()}

        n = len(remotes)
        total_cap = sum(_capped_total(r) for r in remotes.values())
        total_used = sum(r.get("used", 0) for r in remotes.values())
        total_free = sum(free_of.values())
        full = sum(1 for f in free_of.values() if f < 100 * 1024**2)  # <100 MB
        # Accounts our own accounting has pushed past REMOTE_CAP_BYTES. Should be zero; a
        # non-zero count here is the fingerprint of an allocator that has lost track.
        over = sum(1 for r in remotes.values() if r.get("used", 0) > config.REMOTE_CAP_BYTES)
        # Auto-provisioned accounts (scripts/mega_accounts.py names them <prefix><n>) --
        # a growing count here is your at-a-glance proof that auto-creation still works.
        prefix = getattr(config, "ACCOUNT_ALIAS_PREFIX", "automega")
        auto = sum(1 for name in remotes if name.startswith(prefix))
        top = sorted(remotes, key=lambda name: free_of[name], reverse=True)

        lines = [
            "MEGA fleet free space",
            f"updated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            f"TOTAL FREE:  {human_size(total_free)}",
            f"used {human_size(total_used)} of {human_size(total_cap)}",
            f"{n} accounts, {full} full (<100 MB), {over} over cap",
            f"{auto} auto-created (self-provisioned)",
            "",
            "most room (next uploads land here):",
        ]
        for name in top[:10]:
            f = free_of[name]
            if f <= 0:
                break
            lines.append(f"  {human_size(f):>10}  {name}")
        if total_free < config.FREE_SPACE_LOW_WARN_BYTES:
            lines += ["", "*** LOW -- add more MEGA accounts soon ***"]
        if over:
            lines += ["", f"*** {over} account(s) OVER the {human_size(config.REMOTE_CAP_BYTES)} "
                          f"cap -- run scripts/rebalance_overfull.py ***"]

        report = "\n".join(lines) + "\n"
        config.FREE_SPACE_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = config.FREE_SPACE_REPORT_PATH.with_suffix(".txt.tmp")
        tmp.write_text(report, encoding="utf-8")
        tmp.replace(config.FREE_SPACE_REPORT_PATH)   # atomic swap
    except Exception:
        pass


def main() -> None:
    # Manual invocation just writes the report and says where (no logging).
    write_report()
    print(f"wrote {config.FREE_SPACE_REPORT_PATH}")


if __name__ == "__main__":
    main()
