#!/usr/bin/env python3
"""Propose `config.COMIC_FRANCHISES` rows from EVIDENCE, never from memory.

WHY THIS EXISTS
    `COMIC_FRANCHISES` decides which comic/manga series nest under one master folder.
    It had five rows while the library holds at least fourteen franchises sitting flat
    (§4.87): the Toaru family is four sibling top-level folders, Fairy Tail has seven,
    Shaman King five. Every one of them is the layout the owner asked NOT to have.

    The table could be widened by typing names in. That is exactly the hard-coding the
    owner objected to on 2026-08-31, and it is also how a table goes stale. So this script
    derives the rows from two sources that can be re-read at any time:

      1. THE LIBRARY ITSELF. Sibling top-level folders sharing a long name prefix are a
         franchise sitting flat -- that is a measurement of what is on disk, not a guess.
      2. ANILIST (free, key-less, no account). It confirms the group really is one
         franchise and contributes members the library does not own yet, so the table also
         places a spin-off's FIRST volume correctly (§4.40).

    Nothing here is a model call, so it cannot hallucinate a title or cost anything. Run it
    again whenever the library grows; it prints Python ready to paste into config.py, and
    prints ONLY groups that are not already in the table.

USAGE
    python3 scripts/build_comic_franchises.py            # propose rows
    python3 scripts/build_comic_franchises.py --no-net   # library evidence only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                          # noqa: E402
import library                                                         # noqa: E402

ANILIST = "https://graphql.anilist.co"
# AniList answers 403 to a request with no User-Agent, and the failure is
# indistinguishable from "this franchise has no entries" -- the silent-empty class of bug.
UA = "Torrent-Ingest-franchise-builder/1.0 (+https://github.com/Pirate-Hunter-Zoro)"
_QUERY = """
query($s:String!,$p:Int!){
  Page(page:$p, perPage:50){
    pageInfo{hasNextPage}
    media(search:$s, type:MANGA, sort:START_DATE){ format title{romaji english} }
  }
}"""

# A prefix has to be substantial before it means "franchise". "The" and "My" are not
# franchises; three words or fifteen characters is where a shared prefix stops being a
# coincidence of English and starts being a title.
MIN_PREFIX_WORDS = 2
MIN_PREFIX_CHARS = 9
MIN_GROUP = 1                     # a master plus at least one spin-off


def _norm(s: str) -> str:
    return library.normalize_folder_name(s or "")


def _series_folders(root: Path):
    try:
        return sorted(d.name for d in root.iterdir()
                      if d.is_dir() and not d.name.startswith("."))
    except OSError:
        return []


# A COLORED edition is not a spin-off, it is the same series in colour, and the library
# already has a convention for it (`resolve_comic_folder(colored=True)` -> `<Series>
# Colored`). Nesting it as a franchise member would break that path, so it is never a
# member. Same for an explicit omnibus/box-set of the main run.
# An EDITION of a series is not a spin-off. `Dragon Ball Colored`, `Vinland Saga 2-in-1
# Edition` and `Land of the Lustrous Minimalist Color` are the same work repackaged, and
# the library already has conventions for them (`resolve_comic_folder(colored=True)` ->
# `<Series> Colored`; an omnibus supersedes the volumes it covers). Nesting one as a
# franchise member would break those paths, so editions are never members -- and a folder
# that is ITSELF only an edition is never a master either, which is what stopped
# `Land of the Lustrous Colored` from becoming the master of its own colour variant.
_EDITION_RE = re.compile(
    r"(?i)(?:^|\s)(colou?red|full colou?r|minimalist colou?r|omnibus|"
    r"\d+[- ]in[- ]\d+(?:\s+edition)?|deluxe(?:\s+edition)?|complete\s+edition|"
    r"box\s*set|artbook)(?:\s|$)")


def _prefix_groups(names):
    """Group sibling series folders that share a long leading word-run.

    Two shapes, and the second is the one the owner actually asked about:

      * the shared prefix IS a folder -- `Fairy Tail` beside `Fairy Tail - Ice Trail`.
        The prefix folder is the master.
      * the shared prefix is NOT a folder -- `A Certain Magical Index`,
        `A Certain Scientific Railgun`, `A Certain Scientific Accelerator`. Nothing is
        called "A Certain", so a detector that only looks for an existing prefix folder
        (which is what the first version of this script did) finds nothing at all, which
        is why the Toaru family sat flat as four top-level folders. Here the master is the
        SHORTEST member, which in every real case is the franchise's parent series.

    Returns {master_folder: [member_folders]}, maximal groups only.
    """
    norm = {n: _norm(n) for n in names}
    buckets = {}                                  # prefix -> set of folder names
    for a in names:
        for b in names:
            if a >= b:
                continue
            wa, wb = norm[a].split(), norm[b].split()
            common = []
            for x, y in zip(wa, wb):
                if x != y:
                    break
                common.append(x)
            pref = " ".join(common)
            if len(common) < MIN_PREFIX_WORDS or len(pref) < MIN_PREFIX_CHARS:
                continue
            buckets.setdefault(pref, set()).update((a, b))

    # Keep only maximal groups: drop a prefix whose member set is contained in a longer
    # prefix's, so "a certain" does not compete with "a certain scientific".
    groups = {}
    for pref, members in buckets.items():
        if any(pref != other and members <= om and len(other) > len(pref)
               for other, om in buckets.items()):
            continue
        exact = [m for m in members if norm[m] == pref]
        master = exact[0] if exact else min(members, key=lambda m: (len(norm[m]), m))
        rest = sorted(m for m in members if m != master)
        if _EDITION_RE.search(norm[master]):
            continue                      # an edition folder is not a franchise master
        rest = [m for m in rest
                if not _EDITION_RE.search(norm[m][len(norm[master]):].strip())]
        if rest:
            groups[master] = rest

    # Finally drop a group whose master is itself a MEMBER of another group. Without this
    # the Toaru family yields both "A Certain Magical Index" (correct, three members) and a
    # nested "A Certain Scientific Railgun" master for Astral Buddy -- one franchise
    # proposed twice, at two depths. The longer prefix has the smaller member set, so the
    # maximal-set rule above cannot see it; membership is what settles it.
    owned = {m for members in groups.values() for m in members}
    return {k: v for k, v in groups.items() if k not in owned}


def _anilist_members(term: str):
    """Every manga title AniList knows whose search matches `term`. [] on any failure."""
    seen, page = [], 1
    while page <= 3:
        body = json.dumps({"query": _QUERY, "variables": {"s": term, "p": page}}).encode()
        req = urllib.request.Request(
            ANILIST, data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:      # noqa: S310
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return seen
        pg = ((data or {}).get("data") or {}).get("Page") or {}
        for m in pg.get("media") or []:
            t = (m.get("title") or {})
            name = t.get("english") or t.get("romaji")
            if name and _norm(name).startswith(_norm(term)):
                seen.append(name)
        if not (pg.get("pageInfo") or {}).get("hasNextPage"):
            break
        page += 1
        time.sleep(1.2)
    return list(dict.fromkeys(seen))


def _known_masters():
    out = set()
    for fr in config.COMIC_FRANCHISES:
        out.add(_norm(fr["name"]))
        for member in (fr.get("members") or {}):
            out.add(_norm(member))
    return out


def _row(master: str, members, kind: str) -> str:
    """The config.py literal for one franchise."""
    entries = {_norm(master): master}
    for m in members:
        sub = m[len(master):].strip(" -:") if _norm(m).startswith(_norm(master)) else m
        entries[_norm(m)] = sub or m
    # json.dumps, not an f-string with literal quotes: a real title can contain one.
    # `"The Seven Deadly Sins" - Pilot Story` is an actual AniList entry, and interpolating
    # it raw produced a config.py that would not parse.
    body = "\n".join(f"            {json.dumps(k)}: {json.dumps(v)},"
                     for k, v in entries.items())
    return (f'    {{\n        "name": {json.dumps(master)},\n'
            f'        "kind": {json.dumps(kind)},\n'
            f'        "members": {{\n{body}\n        }},\n    }},')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-net", action="store_true",
                    help="use the library's own folders only; skip AniList")
    args = ap.parse_args()

    known = _known_masters()
    proposals = []
    for kind, root in (("manga", config.COMICS_ROOT / "Manga"),
                       ("western", config.COMICS_ROOT)):
        names = [n for n in _series_folders(root) if kind == "manga" or n != "Manga"]
        for master, members in sorted(_prefix_groups(names).items()):
            if _norm(master) in known:
                continue
            if len(members) + 1 < MIN_GROUP + 1:
                continue
            extra = [] if args.no_net else [
                m for m in _anilist_members(master)
                if _norm(m) != _norm(master)
                and not any(_norm(m) == _norm(x) for x in members)]
            proposals.append((kind, master, members, extra))

    if not proposals:
        print("# no new franchise groups found -- the table already covers the library")
        return
    print("# Generated by scripts/build_comic_franchises.py from the library's own folder")
    print("# layout plus AniList. Re-run it after the library grows; do not hand-edit.")
    for kind, master, members, extra in proposals:
        print(f"\n# {master} ({kind}): {len(members)} sibling folder(s) on disk"
              + (f", {len(extra)} more known to AniList" if extra else ""))
        for m in members:
            print(f"#     on disk: {m}")
        for m in extra[:12]:
            print(f"#     anilist: {m}")
        print(_row(master, members + extra, kind))


if __name__ == "__main__":
    main()
