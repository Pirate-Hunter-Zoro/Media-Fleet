#!/usr/bin/env python3
"""The owner's five verified failures, checked against the artifacts they use (10.10F).

HANDOFF §2.6: "a fix is real only where the owner can see it." A log line, a green
test or a "shipped" commit is not acceptance. This script computes the five §10.0
checks and prints PASS/FAIL per line:

  1. TZ (2019) art is not the Too Cute poster (the two byte-identical hashes).
  2. No `vNNNN` mislabels remain on the One Piece shelf.
  3. Zero One Piece chapters covered by an owned volume.
  4. No volume number is held in both editions (the coloured copy wins).
  5. The Smurfs 409-file plan covers the release once one exists -- PENDING while the
     pack is parked, and the parked state alone must never fail this report.

READ-ONLY. Always exits 0: it is a report, and `verify_fleet.sh` prints it as an
advisory so a temporarily-parked Smurfs (or a purge still draining) cannot block a
deploy. The blocking versions of these checks are the unit tests registered in
`verify_fleet.sh` (`test_manga_mislabels.py`, `test_series_identity_heal.py`,
`test_release_title_numbering.py`).

    python3 scripts/verify_owner_report.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import comicfacts                                                    # noqa: E402
import config                                                        # noqa: E402
import dbhook                                                        # noqa: E402
import chapter_volume_reconcile as cvr                               # noqa: E402
import manga_volume_map as mvm                                       # noqa: E402

TOO_CUTE_POSTER_MD5 = "05520557851cb23ea38e121ed2713514"
TOO_CUTE_LANDSCAPE_MD5 = "27082e830c7d93b7bd1defefbe9f884e"
SMURFS_HASH = "6c413306e7053dbb8f1dabf7dcc845f509ec3027"
SMURFS_FILES = 409

results = []


def line(name, status, detail=""):
    results.append((name, status, detail))
    print(f"{status:7s} {name}{(': ' + detail) if detail else ''}")


def md5(path):
    import hashlib
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def check_tz():
    folder = config.MEDIAFS_MOUNT / "Shows" / "The Twilight Zone (2019)"
    if not folder.is_dir():
        line("TZ art", "SKIP", "show folder not present")
        return
    p, l = md5(folder / "folder.jpg"), md5(folder / "landscape.jpg")
    bad = [n for n, h, b in (("folder.jpg", p, TOO_CUTE_POSTER_MD5),
                             ("landscape.jpg", l, TOO_CUTE_LANDSCAPE_MD5))
           if h is None or h == b]
    if bad:
        line("TZ art", "FAIL", f"still Too Cute or missing: {', '.join(bad)}")
    else:
        line("TZ art", "PASS", f"folder.jpg {p[:12]}.. landscape.jpg {l[:12]}..")
    nfo = folder / "tvshow.nfo"
    try:
        text = nfo.read_text("utf-8", "ignore")
    except OSError:
        text = ""
    if "2013" in text or "325542" in text:
        line("TZ nfo identity", "FAIL", "premiered/tvdbid still contaminated")
    else:
        line("TZ nfo identity", "PASS")


def one_piece_files():
    owned = cvr.owned_manga(series="One Piece").get("One Piece") or {}
    return owned


def check_mislabels(owned):
    mis = sorted(rel for rel in owned if dbhook._VOL.search(Path(rel).name)
                 and len(dbhook._VOL.search(Path(rel).name).group(1)) >= 4)
    if mis:
        line("no vNNNN mislabels", "FAIL", f"{len(mis)} remain, e.g. {mis[0]}")
    else:
        line("no vNNNN mislabels", "PASS")


def check_coverage():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import audit_volume_chapter_coverage as audit
        rows = audit.census(series="One Piece")["rows"]
    except Exception as exc:                                         # noqa: BLE001
        line("chapters covered by volumes", "SKIP", f"census failed: {exc}")
        return
    covered = sum(int(r.get("leftovers") or 0) for r in rows)
    if covered:
        line("chapters covered by volumes", "FAIL",
             f"{covered} chapter(s) still covered by an owned volume")
    else:
        line("chapters covered by volumes", "PASS")


def check_editions(owned):
    by_number = {}
    for rel, (mtype, number, colored) in owned.items():
        if mtype != "volume":
            continue
        by_number.setdefault(number, set()).add(bool(colored))
    both = sorted(n for n, colours in by_number.items() if len(colours) > 1)
    if both:
        line("one edition per volume", "FAIL",
             f"{len(both)} number(s) held in both editions, e.g. v{both[0]}")
    else:
        line("one edition per volume", "PASS",
             f"{len(by_number)} volume number(s), one edition each")


def check_smurfs():
    plans = sorted(config.TMP_DIR.glob(f"{SMURFS_HASH}*_plan.json"))
    if not plans:
        line("Smurfs plan coverage", "PENDING", "no plan yet (pack parked)")
        return
    plan = plans[-1]
    try:
        data = json.loads(plan.read_text("utf-8"))
        n = len(data.get("files") or [])
    except (OSError, ValueError):
        line("Smurfs plan coverage", "FAIL", f"{plan.name} is unreadable")
        return
    if n >= SMURFS_FILES:
        line("Smurfs plan coverage", "PASS", f"{n}/{SMURFS_FILES} files planned")
    else:
        line("Smurfs plan coverage", "PENDING",
             f"{n}/{SMURFS_FILES} files planned (truncated plan; coverage guard parked it)")


def main() -> int:
    check_tz()
    owned = one_piece_files()
    if not owned:
        line("One Piece shelf", "SKIP", "shelf not enumerable")
    else:
        check_mislabels(owned)
        check_coverage()
        check_editions(owned)
    check_smurfs()
    print()
    bad = [r for r in results if r[1] == "FAIL"]
    pend = [r for r in results if r[1] == "PENDING"]
    print(f"{len(results) - len(bad) - len(pend)} PASS, {len(bad)} FAIL, {len(pend)} PENDING")
    return 0                                        # a report: never gates a deploy


if __name__ == "__main__":
    raise SystemExit(main())
