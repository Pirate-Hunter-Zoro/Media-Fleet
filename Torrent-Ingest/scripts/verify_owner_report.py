#!/usr/bin/env python3
"""Library-wide acceptance report (HANDOFF 10.10F).

WHY THIS IS GENERIC. The first cut of this script named the five artifacts (a show
folder, two poster hashes, the Smurfs info hash). That is incident evidence, not a
check: it cannot see the same fault on another show, and it rots when the incident is
gone. The durable guards are the registered tests, whose incident details live in their
fixtures and commit messages. This report instead computes the INVARIANTS the five
faults violated, across the whole library, and is safe to keep running forever:

  1. sidecar identity  -- no `tvshow.nfo` whose `enddate` precedes its own premiere
                           (the TZ shape: a stale other-show end date beside a
                           corrected premiere);
  2. manga shelf        -- no `vNNNN` mislabels, no chapter covered by an owned volume,
                           and no volume number held in both editions, across EVERY
                           two-tier series (computed by the reconciler's own functions,
                           so it cannot disagree with an apply);
  3. large releases     -- completed journal records that collapsed duplicate
                           destinations (content-verification review), and terminal
                           records that parked unaccounted files.

READ-ONLY. Always exits 0: it is a report, and `verify_fleet.sh` prints it as an
advisory so a queued purge or a parked pack cannot block a deploy. The blocking
versions are the tests registered in `verify_fleet.sh`.

    python3 scripts/verify_owner_report.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                                                        # noqa: E402
import chapter_volume_reconcile as cvr                               # noqa: E402
import audit_volume_chapter_coverage as audit                        # noqa: E402

results = []


def line(name, status, detail=""):
    results.append((name, status, detail))
    print(f"{status:7s} {name}{(': ' + detail) if detail else ''}")


def _tag(text, tag):
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.I | re.S)
    return (m.group(1).strip() if m else "")


def _year(value):
    m = re.search(r"(\d{4})", str(value or ""))
    return int(m.group(1)) if m else None


def _shows_root():
    root = config.MEDIAFS_MOUNT / "Shows"
    return root if root.is_dir() else config.SHOWS_ROOT


def _resident(rel):
    """True when the path is still visible in the library (mount) or on the SSD.

    The inventory keeps a purged path until Media-Syncer reconciles, so a purge that
    already happened can look "still present" for a minute. The mount is the owner's
    view and the SSD is the local truth; absence from both means the bytes are gone,
    whatever the inventory still says.
    """
    for root in (config.MEDIAFS_MOUNT, config.MEDIA_ROOT):
        try:
            if (root / rel).exists():
                return True
        except OSError:
            return True
    return False


def _queued_deletions():
    """Paths already queued for the reaper's purge (queue + in-flight batch)."""
    out = set()
    q = config.MEDIAFS_DELETIONS_QUEUE
    for p in (q, q.with_name(q.name + ".processing")):
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("path"):
                    out.add(str(item["path"]))
        except OSError:
            continue
    return out


def check_sidecar_identity():
    """A sidecar whose end date precedes its own premiere is self-contradictory."""
    root = _shows_root()
    bad = []
    for nfo in sorted(root.glob("*/tvshow.nfo")):
        try:
            text = nfo.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        start = _year(_tag(text, "premiered") or _tag(text, "year"))
        end = _year(_tag(text, "enddate"))
        if start and end and end < start:
            bad.append(f"{nfo.parent.name} (end {end} < start {start})")
    if bad:
        line("sidecar identity", "FAIL", f"{len(bad)} contradictory sidecar(s): "
             + "; ".join(bad[:3]))
    else:
        line("sidecar identity", "PASS")


def _manga_series():
    owned = cvr.owned_manga()
    return {label: files for label, files in owned.items() if files}


def check_manga():
    owned = _manga_series()
    if not owned:
        line("manga shelf", "SKIP", "shelf not enumerable")
        return
    queued = _queued_deletions()
    mislabels = sorted(rel for files in owned.values() for rel in files
                       if re.search(r"\bv\d{4}\b", Path(rel).name))
    fresh = [r for r in mislabels if r not in queued and _resident(r)]
    if fresh:
        line("no vNNNN mislabels", "FAIL", f"{len(fresh)} remain, e.g. {fresh[0]}")
    elif mislabels:
        line("no vNNNN mislabels", "PENDING",
             f"{len(mislabels)} queued for purge")
    else:
        line("no vNNNN mislabels", "PASS")

    # Covered chapters: the reconciler's own decision function, all series.
    purges = []
    for label, files in sorted(owned.items()):
        kinds = {k for _r, (k, _n, _c) in files.items()}
        if not {"volume", "chapter"} <= kinds:
            continue
        entry = cvr.mvm.get(label, allow_network=False)
        series_purges, _keeps = cvr.plan_decisions(
            label, files, entry, cvr.policy_for(label))
        purges.extend(series_purges)
    resident = [r for r in purges if r not in queued and _resident(r)]
    queued_now = [r for r in purges if r in queued]
    gone = [r for r in purges if r not in queued and not _resident(r)]
    if resident:
        line("chapters covered by volumes", "FAIL",
             f"{len(resident)} still present, e.g. {resident[0]}")
    elif queued_now:
        line("chapters covered by volumes", "PENDING",
             f"{len(queued_now)} queued for purge")
    elif gone:
        line("chapters covered by volumes", "PASS",
             f"{len(gone)} purged (inventory still catching up)")
    else:
        line("chapters covered by volumes", "PASS")

    # One edition per volume number, per series.
    both = []
    for label, files in sorted(owned.items()):
        colours = {}
        for rel, (mtype, number, colored) in files.items():
            if mtype == "volume":
                colours.setdefault(number, set()).add(bool(colored))
        both.extend(f"{label} v{n}" for n, c in colours.items() if len(c) > 1)
    if both:
        line("one edition per volume", "FAIL",
             f"{len(both)} number(s) in both editions, e.g. {both[0]}")
    else:
        total = sum(1 for files in owned.values()
                    for (_r, (k, _n, _c)) in files.items() if k == "volume")
        line("one edition per volume", "PASS", f"{total} volume copy(ies), one per number")


def check_large_releases():
    """Journal outcomes for big packs: collapsed destinations, parked unfiled files."""
    last = {}
    try:
        for raw in (config.STATE_DIR / "journal.jsonl").read_text(
                encoding="utf-8").splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("info_hash"):
                last[rec["info_hash"]] = rec
    except OSError:
        line("large releases", "SKIP", "journal unreadable")
        return
    review = []
    parked = []
    for rec in last.values():
        plan = rec.get("plan") or {}
        if rec.get("status") == "completed" and (plan.get("_deduped_dropped") or []):
            review.append(f"{rec.get('name', '?')[:48]} "
                          f"({len(plan['_deduped_dropped'])} collapsed)")
        if rec.get("status") in ("failed", "refused") and rec.get("unfiled_count"):
            parked.append(f"{rec.get('name', '?')[:48]} "
                          f"({rec['unfiled_count']} unfiled)")
    if parked:
        line("large releases", "PENDING", "parked: " + "; ".join(parked[:3]))
    elif review:
        # Biggest first: the Smurfs (32 collapsed) must be the name a reader sees, not
        # three one-file legacy records that happen to sort earlier.
        review.sort(key=lambda s: int(re.search(r"\((\d+) collapsed\)", s).group(1)),
                    reverse=True)
        more = f" (+{len(review) - 3} more)" if len(review) > 3 else ""
        line("large releases", "REVIEW",
             "destination collapses need content verification: "
             + "; ".join(review[:3]) + more)
    else:
        line("large releases", "PASS")


def main() -> int:
    check_sidecar_identity()
    check_manga()
    check_large_releases()
    print()
    bad = [r for r in results if r[1] == "FAIL"]
    pend = [r for r in results if r[1] == "PENDING"]
    rev = [r for r in results if r[1] == "REVIEW"]
    print(f"{len(results) - len(bad) - len(pend) - len(rev)} PASS, {len(bad)} FAIL, "
          f"{len(pend)} PENDING, {len(rev)} REVIEW")
    return 0                                        # a report: never gates a deploy


if __name__ == "__main__":
    raise SystemExit(main())
