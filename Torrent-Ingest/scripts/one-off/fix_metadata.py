#!/usr/bin/env python3
"""One-off metadata repair for the titles the fleet's own AI healer could not fix.

Written 2026-09-03. This is NOT a fleet module and is not wired into any daemon: it is
the hand-repair the owner asked for after the free-model escalation failed three times on
these same shows (see METADATA-REPAIR-2026-09-03.md for why it failed).

Every title/plot written here was verified against TVMaze -- the fleet's own free,
key-less episode source (`epguide.py`, § diagnosis 4.51) -- not recalled from memory.

It touches ONLY `.nfo` sidecars and Jellyfin item fields. It never touches a media file.
Sidecars are written on the SSD library root (`~/Media`), which is where they actually
live; the mediafs mount presents them and Media-Syncer uploads them.

    python3 fix_metadata.py            # dry run: print every change, write nothing
    python3 fix_metadata.py --apply    # write the sidecars and update Jellyfin
"""
from __future__ import annotations

import argparse
import os
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MEDIA = Path.home() / "Media" / "Shows"
JF_URL = os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8096")
JF_KEY = os.environ.get("JELLYFIN_API_KEY", "")   # never hard-code the token

# --- the verified repairs ----------------------------------------------------
#
# Each entry: (nfo path relative to ~/Media/Shows, new title, new plot or None).
# A plot of None leaves the existing <plot> alone.

BABYLONIA_DIR = ("Fate/Grand Order - Absolute Demonic Front: Babylonia (2019)"
                 "/Season 01/Fate")
BABYLONIA_STEM = "Grand Order - Absolute Demonic Front: Babylonia (2019) - S01E"

MONOGATARI_DIR = "Monogatari (2009)/Season 15"
MONOGATARI_STEM = "Zoku Owarimonogatari - S15E"


def babylonia_repairs(data: dict) -> list[tuple[str, str, str]]:
    """S01E00-E12. E01-E10 currently carry Fate/kaleid liner Prisma Illya's titles and
    plots -- the wrong show entirely -- because the `/` in both series titles collapsed
    them into one phantom `Shows/Fate` folder that Jellyfin identified as Prisma Illya.
    E00/E11/E12 kept the raw fansub filename because Prisma Illya has no episode there.
    Only the latter three were ever flagged by the health scanner."""
    out = []
    for n in range(0, 13):
        e = data[str(n)]
        out.append((f"{BABYLONIA_DIR}/{BABYLONIA_STEM}{n:02d}.nfo", e["title"], e["plot"]))
    return out


# Zoku Owarimonogatari is the 6-episode "Koyomi Reverse" arc. TVMaze carries it as
# Monogatari season 5; this library files it as season 15. Titles verified there.
MONOGATARI_REPAIRS = [
    (f"{MONOGATARI_DIR}/{MONOGATARI_STEM}{n:02d}.nfo", f"Koyomi Reverse - Part {n}", None)
    for n in range(1, 7)
]

# Dr. STONE S04E38 is not a 38th episode -- season 4 has exactly 37 (TVMaze), and the
# source file is "[Erai-raws] Dr Stone - Science Future Part 3 - 12". The season's own
# aired dates split it Part 1 = E01-12, Part 2 = E13-24, Part 3 = E25-37, so Part 3
# episode 12 is E36 "Why-Man" -- and the scraper had already stamped this file
# aired 2026-06-18, which is E36's date. It is a DUPLICATE of E36 from another release
# group. The file is left alone; only its release-group title is corrected, so it stops
# reading as a real (nonexistent) episode. The duplicate itself is a purge decision.
DRSTONE_REPAIRS = [
    ("Dr. STONE (2019)/Season 04/Dr. STONE (2019) - S04E38.nfo", "Why-Man",
     "The team sets foot on the moon to confront Why-Man."),
]


# --- .nfo editing ------------------------------------------------------------

def _sub_tag(text: str, tag: str, value: str) -> tuple[str, bool]:
    """Replace <tag>...</tag> or a self-closing <tag />. Returns (text, changed)."""
    esc = (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    pair = re.compile(rf"<{tag}>.*?</{tag}>", re.S)
    if pair.search(text):
        new = pair.sub(f"<{tag}>{esc}</{tag}>", text, count=1)
        return new, new != text
    empty = re.compile(rf"<{tag}\s*/>")
    if empty.search(text):
        return empty.sub(f"<{tag}>{esc}</{tag}>", text, count=1), True
    return text, False


def repair_nfo(path: Path, title: str, plot: str | None, apply: bool) -> dict:
    if not path.is_file():
        return {"path": str(path), "status": "MISSING"}
    raw = path.read_text(encoding="utf-8")
    old_title = (re.search(r"<title>(.*?)</title>", raw, re.S) or [None, ""])[1]
    old_plot = (re.search(r"<plot>(.*?)</plot>", raw, re.S) or [None, ""])[1]
    new = raw
    changes = []
    if old_title != title:
        new, ok = _sub_tag(new, "title", title)
        if ok:
            changes.append(f"title {old_title[:40]!r} -> {title!r}")
    if plot is not None and old_plot.strip() != plot.strip():
        new, ok = _sub_tag(new, "plot", plot)
        if ok:
            changes.append(f"plot ({len(old_plot)} -> {len(plot)} chars)")
    # Lock it, or the next Jellyfin refresh puts the junk straight back.
    if "<lockdata>false</lockdata>" in new:
        new = new.replace("<lockdata>false</lockdata>", "<lockdata>true</lockdata>", 1)
        changes.append("lockdata false -> true")
    if not changes:
        return {"path": str(path), "status": "already correct"}
    if apply:
        path.write_text(new, encoding="utf-8")
    return {"path": str(path), "status": "WRITTEN" if apply else "would write",
            "changes": changes}


# --- Jellyfin ----------------------------------------------------------------

_UID = None


def jf(path: str, method="GET", body=None, **params):
    """One Jellyfin call. Mirrors media_doctor.Jellyfin._req exactly -- the api_key goes
    in BOTH the query string and X-Emby-Token, and a GET of a full item DTO needs a
    userId or the server answers 400."""
    params = dict(params)
    params["api_key"] = JF_KEY
    url = f"{JF_URL}/{path.lstrip('/')}?{urllib.parse.urlencode(params, doseq=True)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Emby-Token", JF_KEY)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        if raw and r.headers.get("content-type", "").startswith("application/json"):
            return json.loads(raw)
        return None


def uid():
    global _UID
    if _UID is None:
        _UID = jf("Users")[0]["Id"]
    return _UID


def jf_set_title(item_id: str, title: str, plot: str | None, apply: bool) -> str:
    """Set Name/Overview on the Jellyfin item and LOCK both fields.

    Writing the .nfo alone is not enough: Jellyfin keeps its own copy in the DB and a
    populated-but-wrong field is not overwritten by an ordinary refresh. Locking the
    field is what stops a future scrape reverting it."""
    dto = jf(f"Items/{item_id}", userId=uid())
    if dto is None:
        return "no DTO"
    before = dto.get("Name")
    dto["Name"] = title
    if plot is not None:
        dto["Overview"] = plot
    locked = set(dto.get("LockedFields") or [])
    locked.update({"Name"} | ({"Overview"} if plot is not None else set()))
    dto["LockedFields"] = sorted(locked)
    if not apply:
        return f"would set Name {before!r} -> {title!r}"
    jf(f"Items/{item_id}", method="POST", body=dto)
    return f"set Name {before!r} -> {title!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    data = json.loads((here / "babylonia.json").read_text())

    batches = [
        ("Fate/Grand Order - Absolute Demonic Front: Babylonia (2019)",
         "d735c974bd6eb3c0aceff365ea5d34dd", babylonia_repairs(data)),
        ("Monogatari (2009) / Zoku Owarimonogatari",
         "5529ce55726af747e344c6b88d664932", MONOGATARI_REPAIRS),
        ("Dr. STONE (2019)",
         "c5a92aa4ead3e8ed277616816149ca6a", DRSTONE_REPAIRS),
    ]

    for label, series_id, repairs in batches:
        print(f"\n=== {label} ===")
        # Map (season, episode) -> Jellyfin item id, by the .nfo path's stem.
        eps = jf(f"Shows/{series_id}/Episodes", Fields="Path", userId=uid())["Items"]
        by_stem = {}
        for e in eps:
            p = e.get("Path") or ""
            by_stem[Path(p).stem] = e["Id"]
        for rel, title, plot in repairs:
            res = repair_nfo(MEDIA / rel, title, plot, args.apply)
            print(f"  {res['status']:14s} {Path(rel).name}")
            for c in res.get("changes", []):
                print(f"                   - {c}")
            item = by_stem.get(Path(rel).stem)
            if item:
                print(f"                   jellyfin: {jf_set_title(item, title, plot, args.apply)}")
            else:
                print(f"                   jellyfin: NO ITEM for stem {Path(rel).stem!r}")
    print("\n(dry run -- nothing written)" if not args.apply else "\nApplied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
