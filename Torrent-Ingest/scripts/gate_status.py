#!/usr/bin/env python3
"""Is the fleet's acceptance gate still being REACHED, and what is it deciding?

The one question §4.120 could not answer. The gate that decides whether a torrent is
worth downloading existed on only the `.torrent` drop path; when every `.torrent` fetch
began failing and 100% of drops became magnets, it stopped being CALLED — and because a
guard that is never reached looks exactly like a guard that is passing, it stayed dark for
four days while the queue filled with repeats.

Exits non-zero when the gate looks dark, so it can be a check rather than a thing someone
remembers to read. Read-only.

    python3 scripts/gate_status.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import acceptance_gate                                            # noqa: E402
import config                                                     # noqa: E402


def _newest_drop_age():
    newest = None
    for d in (config.TORRENTS_DIR, config.QUEUED_DIR, config.INGESTING_DIR):
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.suffix.lower() not in (".torrent", ".magnet"):
                continue
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return None if newest is None else max(0.0, time.time() - newest)


def _drop_mix():
    """magnets vs `.torrent` per watch-folder stage. The RATIO is the health of the gate's
    old single path: all-magnet is what took it dark (§4.120)."""
    out = []
    base = config.TORRENTS_DIR
    for label, d in (("top", base), ("queued", base / "queued"),
                     ("ingesting", base / "ingesting"), ("finished", base / "finished"),
                     ("failed", base / "failed")):
        try:
            names = [p.suffix.lower() for p in d.iterdir()]
        except OSError:
            continue
        out.append((label, names.count(".magnet"), names.count(".torrent")))
    return out


def main() -> int:
    live = acceptance_gate.liveness()
    age = live.get("age_sec")
    counts = live.get("counts") or {}
    drop_age = _newest_drop_age()

    print("ACCEPTANCE GATE (§4.120)")
    if age is None:
        print("  last verdict:   NEVER — no source has recorded one")
    else:
        print(f"  last verdict:   {age / 3600:.1f}h ago")
    if drop_age is None:
        print("  newest drop:    none in the watch folder")
    else:
        print(f"  newest drop:    {int(drop_age // 60)}m ago")

    print("  verdicts:       "
          + (" ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    for src, data in sorted(live.get("sources", {}).items()):
        c = data.get("counts") or {}
        print(f"    {src:10} last {data.get('last_verdict_iso') or '?'} "
              f"({data.get('last_decision') or '?'})  "
              + " ".join(f"{k}={v}" for k, v in sorted(c.items())))
    if not live.get("sources"):
        print("    (no heartbeat files yet)")

    print("  drop mix (magnet/.torrent):")
    for label, mag, tor in _drop_mix():
        print(f"    {label:10} {mag:5} magnet   {tor:5} .torrent")

    # A quiet gate is only a fault when there is work it should have judged.
    dark = (age is None or age > 24 * 3600) and drop_age is not None and drop_age < 24 * 3600
    if dark:
        print("\n  VERDICT: DARK — drops are arriving and the gate is not judging them.")
        return 1
    if age is None:
        print("\n  VERDICT: no verdicts yet, and no recent drops either.")
        return 0
    print("\n  VERDICT: alive.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
