#!/usr/bin/env python3
"""Which Season-0 sidecars are unlocked, and which of those can we actually adjudicate?

HANDOFF §6 carried this as unfixable: "The sidecar-lock fix closes the race for NEW filings
only. Any sidecar Jellyfin already clobbered stays clobbered. There is no sweep that finds
them; writing one means deciding, from outside, which unlocked Season-0 sidecars carry
*wrong* metadata versus merely unlocked-but-right metadata, and re-locking a wrong title
freezes the error."

Every word of that is still true about RE-LOCKING, which is why this tool does not re-lock
anything and has no --apply. What is NOT true is "there is no sweep that finds them". The
population is findable in seconds, and -- more usefully -- it splits cleanly in two:

  ADJUDICABLE -- the fleet filed this episode itself and recorded the title it authored in
      the journal plan. That record is independent of whatever Jellyfin later scraped into
      the sidecar, so the two can simply be compared. Where they disagree, the fleet's own
      title is the better one BY CONSTRUCTION: it was written from the release the bytes
      actually came from.

  UNDECIDABLE -- no journal plan covers this file (filed before the journal kept plans, or
      by hand). Nothing outside the sidecar knows what it should say, so nothing here can
      judge it. Listed separately and counted, never guessed at.

That split is the whole contribution. §6's objection is about the undecidable half, and it
stands; the adjudicable half was never undecidable, just unexamined.

Read-only. Writes nothing, locks nothing.

    python3 scripts/audit_unlocked_specials.py
    python3 scripts/audit_unlocked_specials.py --show "Adventure Time (2010)"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402

_TITLE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_PLOT = re.compile(r"<plot>(.*?)</plot>", re.IGNORECASE | re.DOTALL)


def _fleet_titles() -> dict:
    """`library-relative .nfo path -> title the fleet authored`, from the journal's plans.

    Keyed off `dst_rel` with the video extension swapped for `.nfo`, which is exactly how
    `apply_plan` names the sidecar it writes beside a file.
    """
    out: dict = {}
    jpath = config.STATE_DIR / "journal.jsonl"
    if not jpath.exists():
        return out
    with jpath.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            plan = rec.get("plan")
            if not isinstance(plan, dict):
                continue
            for f in plan.get("files") or []:
                try:
                    if int(f.get("season")) != 0:
                        continue
                except (TypeError, ValueError):
                    continue
                title = str(f.get("episode_title") or "").strip()
                dst = str(f.get("dst_rel") or "")
                if not title or not dst:
                    continue
                out[str(Path(dst).with_suffix(".nfo"))] = title
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", help="limit to one library show folder")
    args = ap.parse_args()

    root = config.MEDIAFS_MOUNT / "Shows"
    if not root.is_dir():
        print(f"{root} is not readable (is the mount up?)")
        return 1

    fleet = _fleet_titles()
    pattern = f"{args.show}/Season 00/*.nfo" if args.show else "*/Season 00/*.nfo"

    total = locked = 0
    agree = []
    disagree = []
    undecidable = []

    for p in sorted(root.glob(pattern)):
        if p.name == "season.nfo":
            # Season-level seed, deliberately written unlocked. Not an episode.
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total += 1
        if "<lockdata>true</lockdata>" in text.lower():
            locked += 1
            continue

        rel = str(p.relative_to(config.MEDIAFS_MOUNT))
        on_disk = (_TITLE.search(text).group(1).strip() if _TITLE.search(text) else "")
        plot = (_PLOT.search(text).group(1).strip() if _PLOT.search(text) else "")
        ours = fleet.get(rel)
        if not ours:
            undecidable.append((rel, on_disk, len(plot)))
        elif _norm(ours) == _norm(on_disk):
            agree.append((rel, on_disk))
        else:
            disagree.append((rel, on_disk, ours))

    print("=== unlocked Season-0 sidecars ===\n")
    print(f"{total} Season-0 episode sidecar(s) examined")
    print(f"  {locked} locked (the fleet's own metadata, safe)")
    print(f"  {total - locked} UNLOCKED -- the §6 population\n")

    print(f"-- ADJUDICABLE, and DISAGREEING ({len(disagree)}) --")
    if disagree:
        print("   The fleet authored a title for these and the sidecar now says something")
        print("   else -- i.e. it was scraped over. The fleet's is the better title: it was")
        print("   written from the release the bytes came from.\n")
        for rel, on_disk, ours in disagree:
            print(f"   {rel}")
            print(f"      sidecar says : {on_disk!r}")
            print(f"      fleet  says  : {ours!r}")
    else:
        print("   none -- no unlocked sidecar contradicts a title the fleet authored.\n")

    print(f"\n-- ADJUDICABLE, and AGREEING ({len(agree)}) --")
    print("   Unlocked but correct. Jellyfin has not clobbered these (yet); locking them")
    print("   would be safe but is not urgent, and this tool will not do it for you.")

    print(f"\n-- UNDECIDABLE ({len(undecidable)}) --")
    print("   No journal plan covers these, so nothing outside the sidecar knows what they")
    print("   SHOULD say. This is exactly the set §6 says cannot be swept, and it is not")
    print("   swept here. Shown so you know how large it is.\n")
    for rel, on_disk, plotlen in undecidable[:15]:
        blank = "  <-- NO PLOT" if plotlen == 0 else ""
        print(f"   {rel}")
        print(f"      title: {on_disk!r}  (plot {plotlen} chars){blank}")
    if len(undecidable) > 15:
        print(f"   ... and {len(undecidable) - 15} more")

    print("\nNOTHING WAS CHANGED. Re-locking a wrong title freezes the error permanently,")
    print("which is why there is no --apply: the fix for a disagreeing row is to re-file")
    print("that special `owned` with the right title, so apply_plan locks it properly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
