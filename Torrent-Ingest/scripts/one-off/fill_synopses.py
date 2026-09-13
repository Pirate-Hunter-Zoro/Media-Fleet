#!/usr/bin/env python3
"""Fill blank episode synopses (and generic titles) from TVMaze, but ONLY where the
library's own correct episodes prove the mapping.

Written 2026-09-03 as a one-off hand repair. Not a fleet module.

WHY IT IS ANCHORED. This library does not use provider season numbers. Monogatari is
filed as 15 seasons where TVMaze has 6; Dr. STONE files a whole cour as one season. Any
tool that assumes library S/E == provider S/E writes the wrong show's plot onto an
episode, which is exactly the corruption being cleaned up here. So nothing is written
until the mapping is PROVEN:

  For each library season, search every (provider season, offset) pair. A pair is only
  accepted when the episodes that ALREADY have a correct-looking title agree with the
  provider under it -- at least MIN_ANCHORS of them -- and NOT ONE disagrees. A season
  with no anchors, or with any contradiction, is skipped and reported, never guessed.

It writes `.nfo` sidecars on the SSD (`~/Media`) and the matching Jellyfin fields, and
locks both. It never touches a media file.

    python3 fill_synopses.py            # dry run: print the derived mapping + every change
    python3 fill_synopses.py --apply
"""
from __future__ import annotations

import argparse
import os
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MEDIA = Path.home() / "Media" / "Shows"
JF_URL = os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8096")
JF_KEY = os.environ.get("JELLYFIN_API_KEY", "")   # never hard-code the token

MIN_ANCHORS = 2          # two independent title matches before a season mapping is trusted
MIN_ANCHOR_COVERAGE = 0.6  # ...and most of the season must actually land on a provider episode
_UID = None

# (library show folder, TVMaze query). The folder is what the health report names.
TARGETS = [
    ("Gundam Build Divers Re:Rise (2019)", "Gundam Build Divers Re:Rise"),
    ("Cells at Work! (2018)",              "Cells at Work!"),
    ("Yu-Gi-Oh! ARC-V (2014)",             "Yu-Gi-Oh! ARC-V"),
    ("Yu-Gi-Oh! VRAINS (2017)",            "Yu-Gi-Oh! VRAINS"),
    ("Yu-Gi-Oh! Go Rush!! (2022)",         "Yu-Gi-Oh! Go Rush!!"),
    ("Pokémon Horizons: The Series (2023)", "Pokémon Horizons: The Series"),
    ("Fairy Tail (2009)",                  "Fairy Tail"),
    ("Monogatari (2009)",                  "Monogatari"),
    ("Creature Commandos (2024)",          "Creature Commandos"),
    ("Slow Start (2018)",                  "Slow Start"),
    ("That '90s Show (2023)",              "That '90s Show"),
    ("Digimon Adventure (1999)",           "Digimon Adventure"),
    ("Dr. STONE (2019)",                   "Dr. Stone"),
]


# --- helpers -----------------------------------------------------------------

def norm(s: str) -> str:
    """Compare titles ignoring case, accents and punctuation -- providers and fansubs
    disagree constantly on those and none of it changes which episode it is."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def jf(path: str, method="GET", body=None, **params):
    params = dict(params)
    params["api_key"] = JF_KEY
    url = f"{JF_URL}/{path.lstrip('/')}?{urllib.parse.urlencode(params, doseq=True)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Emby-Token", JF_KEY)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()
        if raw and r.headers.get("content-type", "").startswith("application/json"):
            return json.loads(raw)
        return None


def uid():
    global _UID
    if _UID is None:
        _UID = jf("Users")[0]["Id"]
    return _UID


def tvmaze(query: str):
    q = urllib.parse.quote(query)
    url = f"https://api.tvmaze.com/singlesearch/shows?q={q}&embed=episodes"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        return None, f"tvmaze: {exc}"
    out = {}
    for e in d.get("_embedded", {}).get("episodes", []):
        summary = re.sub(r"<[^>]+>", "", e.get("summary") or "").strip()
        out[(e["season"], e["number"])] = {
            "title": e["name"], "plot": html.unescape(summary)}
    return out, d.get("name")


def _sub_tag(text: str, tag: str, value: str):
    esc = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    pair = re.compile(rf"<{tag}>.*?</{tag}>", re.S)
    if pair.search(text):
        return pair.sub(f"<{tag}>{esc}</{tag}>", text, count=1), True
    empty = re.compile(rf"<{tag}\s*/>")
    if empty.search(text):
        return empty.sub(f"<{tag}>{esc}</{tag}>", text, count=1), True
    return text, False


# --- the mapping proof -------------------------------------------------------

def derive_mapping(lib_eps, prov, show_name):
    """lib_eps: {(season, number): {"title":..., "plot":...}}. Returns
    {lib_season: (prov_season, offset)} for every season that could be PROVEN, plus a
    list of human-readable notes for the ones that could not."""
    mapping, notes = {}, []
    lib_seasons = sorted({s for s, _ in lib_eps})
    prov_seasons = sorted({s for s, _ in prov})
    show_norm = norm(show_name)
    for ls in lib_seasons:
        nums = sorted(n for s, n in lib_eps if s == ls)
        # An episode whose stored title is a real title (not blank, not the series name,
        # not "Season N", not the filename) is a usable anchor.
        anchors = {}
        for n in nums:
            t = (lib_eps[(ls, n)]["title"] or "").strip()
            if not t or norm(t) == show_norm or re.fullmatch(r"(?i)season\s*\d+", t):
                continue
            if re.search(r"S\d{1,2}E\d{1,2}", t):
                continue
            anchors[n] = norm(t)
        if len(anchors) < MIN_ANCHORS:
            notes.append(f"  S{ls:02d}: only {len(anchors)} usable anchor(s) — SKIPPED")
            continue
        best = None
        for ps in prov_seasons:
            pnums = sorted(n for s, n in prov if s == ps)
            if not pnums:
                continue
            for off in range(min(pnums) - max(nums), max(pnums) - min(nums) + 1):
                hit = miss = 0
                for n, want in anchors.items():
                    p = prov.get((ps, n + off))
                    if p is None:
                        continue
                    if norm(p["title"]) == want:
                        hit += 1
                    else:
                        miss += 1
                # COVERAGE, not just agreement. An offset that slides most of the season
                # past the end of a provider season leaves those anchors unresolvable, so
                # they count as neither hit nor miss -- and a handful of survivors then
                # "agree" with nothing contradicting them. Pokemon Horizons S03 matched
                # 5 of 28 anchors at offset -45 that way. Require most of the season to
                # actually land on a provider episode before believing the alignment.
                if len(anchors) and hit / len(anchors) < MIN_ANCHOR_COVERAGE:
                    continue
                if miss == 0 and hit >= MIN_ANCHORS and (best is None or hit > best[2]):
                    best = (ps, off, hit)
        if best is None:
            notes.append(f"  S{ls:02d}: no provider season/offset agrees with all "
                         f"{len(anchors)} anchor(s) — SKIPPED")
            continue
        ps, off, hit = best
        mapping[ls] = (ps, off)
        notes.append(f"  S{ls:02d}: -> provider S{ps:02d} offset {off:+d} "
                     f"({hit}/{len(anchors)} anchors matched, 0 contradictions)")
    return mapping, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", help="restrict to one show folder")
    args = ap.parse_args()

    index = {p.rstrip("/").split("/")[-1]: i
             for p, i in ((it.get("Path") or "", it["Id"])
                          for it in jf("Items", Recursive="true",
                                       IncludeItemTypes="Series",
                                       Fields="Path", userId=uid())["Items"])}
    total_written = 0
    for folder, query in TARGETS:
        if args.only and args.only.lower() not in folder.lower():
            continue
        sid = index.get(folder)
        print(f"\n=== {folder} ===")
        if not sid:
            print("  no Jellyfin series item — SKIPPED")
            continue
        eps = jf(f"Shows/{sid}/Episodes", Fields="Path", userId=uid())["Items"]
        lib = {}
        paths = {}
        for e in eps:
            s, n = e.get("ParentIndexNumber"), e.get("IndexNumber")
            if s is None or n is None:
                continue
            lib[(s, n)] = {"title": (e.get("Name") or "").strip(),
                           "plot": (e.get("Overview") or "").strip(), "id": e["Id"]}
            paths[(s, n)] = e.get("Path") or ""
        prov, name = tvmaze(query)
        if prov is None:
            print(f"  {name} — SKIPPED")
            continue
        print(f"  TVMaze: {name!r}, {len(prov)} episodes; library {len(lib)}")
        mapping, notes = derive_mapping(lib, prov, name)
        for ln in notes:
            print(ln)
        for (s, n), cur in sorted(lib.items()):
            if s not in mapping:
                continue
            ps, off = mapping[s]
            p = prov.get((ps, n + off))
            if not p:
                continue
            need_title = (not cur["title"] or norm(cur["title"]) == norm(name)
                          or re.fullmatch(r"(?i)season\s*\d+", cur["title"]))
            need_plot = not cur["plot"]
            if not (need_title or need_plot):
                continue
            new_title = p["title"] if need_title else cur["title"]
            new_plot = p["plot"] if (need_plot and p["plot"]) else None
            if need_title and not p["title"]:
                continue
            # The provider can know the episode exists and still carry no summary
            # (TVMaze is sparse on some anime). Nothing to write is not a repair -- don't
            # touch the file and don't count it.
            if not need_title and not new_plot:
                continue
            # --- sidecar
            src = paths[(s, n)]
            rel = src.split("/MediaLibrary/Shows/", 1)[-1] if "/MediaLibrary/" in src else None
            nfo = (MEDIA / rel).with_suffix(".nfo") if rel else None
            bits = []
            if nfo and nfo.is_file():
                raw = nfo.read_text(encoding="utf-8")
                new = raw
                if need_title:
                    new, _ = _sub_tag(new, "title", new_title)
                # Jellyfin's Overview can be empty while the sidecar holds a good plot
                # (a scrape that never re-read the .nfo). Filling Jellyfin from the
                # provider is right; clobbering a populated sidecar is not.
                nfo_plot = (re.search(r"<plot>(.*?)</plot>", raw, re.S) or [None, ""])[1]
                if new_plot and not nfo_plot.strip():
                    new, _ = _sub_tag(new, "plot", new_plot)
                new = new.replace("<lockdata>false</lockdata>",
                                  "<lockdata>true</lockdata>", 1)
                if new != raw:
                    if args.apply:
                        nfo.write_text(new, encoding="utf-8")
                    bits.append("nfo")
            else:
                bits.append("NO-NFO")
            # --- Jellyfin
            dto = jf(f"Items/{cur['id']}", userId=uid())
            if dto:
                if need_title:
                    dto["Name"] = new_title
                if new_plot:
                    dto["Overview"] = new_plot
                locked = set(dto.get("LockedFields") or [])
                locked.update({"Name"} if need_title else set())
                locked.update({"Overview"} if new_plot else set())
                dto["LockedFields"] = sorted(locked)
                if args.apply:
                    jf(f"Items/{cur['id']}", method="POST", body=dto)
                bits.append("jellyfin")
            what = []
            if need_title:
                what.append(f"title {cur['title'][:28]!r}->{new_title!r}")
            if new_plot:
                what.append(f"plot +{len(new_plot)}c")
            print(f"    S{s:02d}E{n:02d} [{'+'.join(bits)}] " + "; ".join(what))
            total_written += 1
        time.sleep(0.3)          # be polite to TVMaze
    print(f"\n{total_written} episode(s) "
          + ("updated." if args.apply else "would be updated (dry run)."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
