#!/usr/bin/env python3
"""One-off: repair Mushi-Shi (2005) Season 00, whose sidecars are offset by one slot
(2026-09-11).

THE FAULT. Three specials on disk, each named for the special it actually is:

    S00E01-The Shadow That Devours the Sun.mkv   .nfo said 'Recap'
    S00E02-Path of Thorns.mkv                    .nfo said 'The Shadow That Devours the Sun'
    S00E04-Bell Droplets.mkv                     .nfo said 'Mushistory 2'

`nfo[n]` holds the title belonging to `filename[n-1]`. The provider's Season-0 list
interleaves non-episode entries this release does not have (a "Recap", two
"Mushistory" staff interviews), so its ordering does not line up with the release's
own S00Exx numbering -- and because these sidecars were `lockdata=false`, Jellyfin
scraped that offset ordering straight onto them.

WHY THE FILENAMES ARE THE TRUTH, and the sidecars the guess:
  * The filenames were written once by `apply_plan` from the identify plan, which
    read the source release's own names. The sidecars on disk are Jellyfin-authored
    (BOM, <dateadded>, <fileinfo><streamdetails> -- none of which this fleet emits).
  * They are the three real Mushishi specials in correct chronological order:
    Hihamukage (2014-01-04), Odoro no Michi (2014-08-20), Suzu no Shizuku
    (2015-05-16). A release containing a live staff interview would not name it
    "Bell Droplets".
  * The shift is verbatim, not approximate: S00E02's sidecar title is S00E01's
    filename, character for character.

There is no S00E03 on disk. That is a gap in what was acquired, not a fault.

BOTH HALVES, OR IT DOES NOT SURVIVE. Writing `<lockdata>true</lockdata>` into the
sidecar is necessary but NOT sufficient: Jellyfin's DB is the authority, and an item
it already holds unlocked stays unlocked -- it will re-scrape and overwrite the file
again. So this also adds Name/Overview to each item's `LockedFields` through the API.
That is the same doctrine `media_doctor` states for a junk title: BOTH, and locked.

    python3 scripts/one-off/fix_mushishi_specials.py            # dry run, prints the diff
    python3 scripts/one-off/fix_mushishi_specials.py --apply

Backs up every sidecar it touches next to itself before writing.
"""

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import config                                                        # noqa: E402
import media_doctor as md                                            # noqa: E402

SEASON = config.MEDIAFS_MOUNT / "Shows" / "Mushi-Shi (2005)" / "Season 00"

# filename slot -> the metadata that belongs to the file sitting in it.
# Plots are the real per-episode synopses, not the offset ones.
TRUTH = {
    "Mushi-Shi (2005) - S00E01-The Shadow That Devours the Sun.mkv": {
        "title": "The Shadow That Devours the Sun",
        "aired": "2014-01-04",
        "plot": (
            "The mushi are said to become uneasy during an eclipse. Tanyu sends Ginko "
            "to monitor a village for the appearance of a mushi that causes a "
            "neverending eclipse. Her foreboding is justified when the village is "
            "enveloped in darkness..."
        ),
    },
    "Mushi-Shi (2005) - S00E02-Path of Thorns.mkv": {
        "title": "Path of Thorns",
        "aired": "2014-08-20",
        "plot": (
            "Ginko is sent by Tanyu Karibusa to oversee Kumado Minai's investigation of "
            "an abandoned village where dead wood and ruined structures are putting out "
            "new growth. Accompanying Kumado into a path of thorns -- a place where "
            "mushi flow into the living world -- Ginko learns why the Minai clan was "
            "founded, why it remains bound as retainer to the Karibusa family that keeps "
            "a life-threatening mushi sealed, and what lies behind the clan's "
            "characteristic lack of expression."
        ),
    },
    "Mushi-Shi (2005) - S00E04-Bell Droplets.mkv": {
        "title": "Bell Droplets",
        "aired": "2015-05-16",
        "plot": (
            "A boy once heard bells ringing in the mountain near his home, and years "
            "later his sister vanished on that mountain during a storm. Ginko finds a "
            "young girl there with branches and leaves growing from her body and "
            "realises she has become the mountain's lord -- though he cannot see why a "
            "human would be chosen for it. Adapting chapters 49 and 50 of the manga."
        ),
    },
}


def _set(txt, tag, value):
    """Replace <tag>…</tag> if present; otherwise insert it before </episodedetails>."""
    if re.search(rf"<{tag}>.*?</{tag}>", txt, re.S):
        return re.sub(rf"<{tag}>.*?</{tag}>", f"<{tag}>{value}</{tag}>", txt,
                      count=1, flags=re.S)
    return txt.replace("</episodedetails>", f"  <{tag}>{value}</{tag}>\n</episodedetails>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = ap.parse_args()

    if not SEASON.is_dir():
        sys.exit(f"season folder not found: {SEASON}")

    jf = md.Jellyfin()
    # Map this show's Season-0 episodes by resolved path so each sidecar can be
    # matched to the Jellyfin item that must be locked alongside it.
    by_path = {}
    try:
        series = jf.get("Items", Recursive="true", IncludeItemTypes="Series",
                        SearchTerm="Mushi-Shi", fields="Path")
        for s in (series or {}).get("Items", []):
            eps = jf.get("Items", ParentId=s["Id"], Recursive="true",
                         IncludeItemTypes="Episode",
                         fields="Path,LockedFields,ParentIndexNumber")
            for e in (eps or {}).get("Items", []):
                if e.get("Path"):
                    by_path[Path(e["Path"]).name] = e
    except Exception as exc:                                          # noqa: BLE001
        print(f"! could not reach Jellyfin ({exc}); sidecars only, NOT durable")

    changed = 0
    for fname, want in TRUTH.items():
        video = SEASON / fname
        nfo = video.with_suffix(".nfo")
        if not nfo.exists():
            print(f"! no sidecar for {fname}")
            continue
        txt = nfo.read_text("utf-8-sig", "ignore")
        was_title = md.library._xml_tag(txt, "title")
        was_lock = md.library.nfo_is_locked(txt)

        new = txt
        for tag in ("title", "plot", "aired"):
            new = _set(new, tag, want[tag])
        new = _set(new, "lockdata", "true")
        # Jellyfin writes <outline> alongside <plot> on some items; keep them agreeing.
        if "<outline>" in new:
            new = _set(new, "outline", want["plot"])

        item = by_path.get(fname)
        print(f"\n{fname}")
        print(f"    title   {was_title!r}  ->  {want['title']!r}")
        print(f"    lockdata {was_lock}  ->  True")
        print(f"    jellyfin item {item['Id'] if item else 'NOT FOUND'}"
              f"  LockedFields={item.get('LockedFields') if item else None}")

        if not args.apply:
            continue

        bak = nfo.with_suffix(f".nfo.bak-mushishi-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(nfo, bak)
        nfo.write_text(new, encoding="utf-8")
        changed += 1

        if item:
            # The half that makes it stick. TWO different locks, and both are needed:
            #
            #   LockedFields=[Name, Overview] protects those two fields from the
            #     scraper, but leaves the ITEM unlocked.
            #   LockData=True is the whole-item lock -- the one that IS
            #     `<lockdata>true</lockdata>`. Without it Jellyfin keeps refreshing the
            #     item and, with SaveLocalMetadata=True, writes its own sidecar back
            #     over ours carrying `lockdata=false`. Observed doing exactly that on
            #     the first pass of this script: the titles took and the lockdata edit
            #     was reverted within the second.
            #
            # PremiereDate is set here rather than in the sidecar for the same reason:
            # an unlocked field is only ever as good as the next scan.
            lf = sorted(set((item.get("LockedFields") or []) + ["Name", "Overview"]))
            jf.update_item(item["Id"], Name=want["title"], Overview=want["plot"],
                           PremiereDate=f"{want['aired']}T00:00:00.0000000Z",
                           LockedFields=lf, LockData=True)
            print("    locked in Jellyfin (LockData=True; Name, Overview)")

    if not args.apply:
        print("\n(dry run -- nothing written; re-run with --apply)")
        return
    print(f"\nrewrote {changed} sidecar(s).")
    print("Now re-run the doctor and confirm the item clears:")
    print("  launchctl kickstart -k gui/501/com.mikeyferguson.mediadoctor")


if __name__ == "__main__":
    main()
