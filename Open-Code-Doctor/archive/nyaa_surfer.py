#!/usr/bin/env python3
"""Surf nyaa.si (current official domain; nyaa.land is an old mirror) and
download .torrent files for the configured Digimon / Gundam / Yu-Gi-Oh! series.

Usage:
  python3 nyaa_surfer.py --dry-run                      # preview best matches
  python3 nyaa_surfer.py --download                     # download top match per target
  python3 nyaa_surfer.py --download --interactive       # choose matches manually
  python3 nyaa_surfer.py --dry-run --only "Gundam SEED"
  python3 nyaa_surfer.py --download --output ./torrents

By default torrents are written to the user's iCloud Drive Torrents folder and
only results with at least one seeder are downloaded. If a search finds nothing
in the requested category, the script retries against all anime (category 1_0)
so raw / non-English releases are not missed.
"""

DEFAULT_OUTPUT = (
    "/Users/mikeyferguson/Library/Mobile Documents/com~apple~CloudDocs/Torrents"
)

import argparse
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DEFAULT_BASE = "https://nyaa.si"
CATEGORY_ANIME = "1_0"
CATEGORY_ANIME_ENGLISH = "1_2"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

PREFER = [
    ("batch", 4), ("complete", 4),
    ("bdrip", 3), ("bluray", 3), ("blu-ray", 3), ("remux", 3),
    ("dual audio", 3), ("dual-audio", 3),
    ("x265", 2), ("hevc", 2), ("x264", 1), ("dual", 1),
]


def resolution_bonus(title):
    low = title.lower()
    if any(k in low for k in ("2160p", "4k", "uhd", " uhd ")):
        return 60
    if "1080p" in low or "1080i" in low:
        return 40
    if "720p" in low:
        return 20
    if any(k in low for k in ("480p", "576p", " dvd", "dvdr")):
        return -30
    return 0


@dataclass
class Target:
    name: str
    query: str
    avoid: tuple = ()
    multi: int = 1


@dataclass
class TorrentInfo:
    title: str
    link: str
    view: str
    infohash: str
    seeders: int
    leechers: int
    size: str
    score: int


TARGETS = [
    # Digimon — TV series
    Target("Digimon Adventure (1999)", "Digimon Adventure 1999", avoid=("Movie", "Episodes")),
    Target("Digimon Adventure 02", "Digimon Adventure 02", avoid=("Beginning", "Revenge", "Diaboromon", "FIX")),
    Target("Digimon Tamers", "Digimon Tamers", avoid=("Runaway Locomon", "Battle of Adventurers")),
    Target("Digimon Frontier", "Digimon Frontier", avoid=("of 8", "Island of Lost", "S01E")),
    Target("Digimon Savers / Data Squad", "Digimon Savers", avoid=("Burst Mode",)),
    Target("Digimon Xros Wars / Fusion", "Digimon Xros Wars", avoid=("Hunter", "Toki wo Kakeru")),
    Target("Digimon Xros Wars: Young Hunters", "Toki wo Kakeru Shounen"),
    Target("Digimon Adventure Tri", "Digimon Adventure tri"),
    Target("Digimon Universe: App Monsters", "Digimon Universe App Monsters"),
    Target("Digimon Adventure: (2020 Reboot)", "Digimon Adventure 2020"),
    Target("Digimon Ghost Game", "Digimon Ghost Game"),
    # Digimon — movies & specials
    Target("Digimon: The Movie (US bundle)", "Digimon The Movie"),
    Target("Digimon Adventure (1999 short film)", "Digimon Adventure Movie 1999"),
    Target("Our War Game!", "Digimon Our War Game"),
    Target("Diablomon Strikes Back / Revenge of Diaboromon", "Diablomon Strikes Back"),
    Target("Tamers: Battle of Adventurers", "Digimon Battle of Adventurers"),
    Target("Tamers: Runaway Locomon", "Digimon Runaway Locomon"),
    Target("Frontier: Island of Lost Digimon", "Digimon Island of Lost Digimon"),
    Target("Digital Monster X-Evolution", "Digital Monster X-Evolution"),
    Target("Savers: Ultimate Power! Activate Burst Mode!!", "Digimon Savers Movie"),
    Target("Adventure 3D: Digimon Grand Prix!", "Digimon Adventure 3D", avoid=("The Movie", "Beatbreak")),
    Target("Savers 3D: Digital World in Imminent Danger!", "Digimon Savers 3D"),
    Target("Adventure: Last Evolution Kizuna", "Digimon Last Evolution Kizuna"),
    Target("Adventure 02: The Beginning", "Digimon Adventure 02 The Beginning"),
    # Gundam — Universal Century
    Target("MS IGLOO: The Hidden One Year War", "Mobile Suit Gundam MS IGLOO"),
    Target("MS IGLOO: Apocalypse 0079", "MS IGLOO Apocalypse 0079"),
    Target("MS IGLOO 2: The Gravity Front", "MS IGLOO 2 Gravity Front"),
    Target("Mobile Suit Gundam: The Origin", "Mobile Suit Gundam The Origin"),
    Target("Mobile Suit Gundam (1979)", "Mobile Suit Gundam 0079"),
    Target("08th MS Team", "Mobile Suit Gundam 08th MS Team"),
    Target("Gundam Thunderbolt", "Mobile Suit Gundam Thunderbolt"),
    Target("0080: War in the Pocket", "Gundam 0080 War in the Pocket"),
    Target("0083: Stardust Memory", "Gundam 0083 Stardust Memory"),
    Target("Zeta Gundam", "Mobile Suit Zeta Gundam"),
    Target("Gundam ZZ", "Mobile Suit Gundam ZZ"),
    Target("Char's Counterattack", "Char's Counterattack", avoid=("Pack",)),
    Target("Gundam Unicorn", "Mobile Suit Gundam Unicorn"),
    Target("Gundam Narrative", "Mobile Suit Gundam Narrative"),
    Target("Hathaway's Flash", "Hathaways Flash"),
    Target("Gundam F91", "Mobile Suit Gundam F91"),
    Target("Victory Gundam", "Mobile Suit Victory Gundam"),
    Target("G-Saviour", "G-Saviour"),
    # Gundam — Alternate Universes & spin-offs
    Target("Mobile Fighter G Gundam", "Mobile Fighter G Gundam"),
    Target("Gundam Wing", "Mobile Suit Gundam Wing"),
    Target("Gundam Wing: Endless Waltz", "Gundam Wing Endless Waltz"),
    Target("After War Gundam X", "After War Gundam X"),
    Target("Turn A Gundam", "Turn A Gundam"),
    Target("Gundam SEED", "Mobile Suit Gundam SEED"),
    Target("Gundam SEED MSV Astray", "Gundam SEED MSV Astray"),
    Target("Gundam SEED Destiny", "Gundam SEED Destiny"),
    Target("Gundam SEED C.E. 73: Stargazer", "Gundam SEED Stargazer"),
    Target("Gundam SEED Freedom", "Gundam SEED Freedom"),
    Target("Mobile Suit Gundam 00", "Gundam 00 S1 S2"),
    Target("Gundam 00: A Wakening of the Trailblazer", "Gundam 00 Trailblazer"),
    Target("Gundam AGE", "Mobile Suit Gundam AGE"),
    Target("Gundam AGE: Memory of Eden", "Gundam AGE Memory of Eden"),
    Target("Gundam Build Fighters", "Gundam Build Fighters"),
    Target("Gundam Build Fighters Try", "Gundam Build Fighters Try"),
    Target("Gundam Reconguista in G", "Gundam Reconguista in G"),
    Target("Iron-Blooded Orphans", "Gundam Iron-Blooded Orphans"),
    Target("Gundam Build Divers", "Gundam Build Divers", avoid=("Re:RISE", "Re-Rise", "ReRISE", "Rerise", "Prologue", "Battlogue", "S00")),
    Target("Gundam Build Divers Re:RISE", "Gundam Build Divers Re:RISE", multi=2),
    Target("Witch from Mercury", "Gundam Witch from Mercury"),
    # Yu-Gi-Oh! — TV series
    Target("Yu-Gi-Oh! (Toei / Season 0)", "Yu-Gi-Oh 1998"),
    Target("Yu-Gi-Oh! Duel Monsters", "Yu-Gi-Oh Duel Monsters"),
    Target("Yu-Gi-Oh! GX", "Yu-Gi-Oh GX"),
    Target("Yu-Gi-Oh! 5D's", "Yu-Gi-Oh 5Ds"),
    Target("Yu-Gi-Oh! ZEXAL", "Yu-Gi-Oh Zexal"),
    Target("Yu-Gi-Oh! ARC-V", "Yu-Gi-Oh Arc-V"),
    Target("Yu-Gi-Oh! VRAINS", "Yu-Gi-Oh VRAINS"),
    Target("Yu-Gi-Oh! SEVENS", "Yu-Gi-Oh SEVENS"),
    Target("Yu-Gi-Oh! Go Rush!!", "Yu-Gi-Oh Go Rush", avoid=("SHIRT", "Episode", "Disneynow"), multi=5),
    # Yu-Gi-Oh! — movies & specials
    Target("Yu-Gi-Oh! The Movie (1999)", "Yu-Gi-Oh The Movie 1999"),
    Target("Pyramid of Light", "Yu-Gi-Oh Pyramid of Light"),
    Target("3D: Bonds Beyond Time", "Yu-Gi-Oh Bonds Beyond Time"),
    Target("The Dark Side of Dimensions", "Yu-Gi-Oh Dark Side of Dimensions"),
    Target("Capsule Monsters", "Yu-Gi-Oh Capsule Monsters"),
    # Pokemon
    Target("Pokemon Complete Series (S01-25 + Movies + Specials + Shorts + OST)", "Pokemon Complete Series"),
    Target("Pokemon Horizons (2023, ongoing)", "Pokemon Horizons", multi=6),
    # Bakugan
    Target("Bakugan Battle Brawlers (2007)", "Bakugan Battle Brawlers Season 1"),
    Target("Bakugan: New Vestroia (2009)", "Bakugan New Vestroia"),
    Target("Bakugan: Gundalian Invaders (2010)", "Bakugan Gundalian Invaders"),
    Target("Bakugan: Mechtanium Surge (2011)", "Bakugan Mechtanium Surge"),
    Target("Bakugan: Battle Planet (2018)", "Bakugan Battle Planet"),
    Target("Bakugan: Armored Alliance (2020)", "Bakugan Armored Alliance", multi=3),
    Target("Bakugan: Geogan Rising (2021)", "Bakugan Geogan Rising"),
    Target("Bakugan: Evolutions (2022)", "Bakugan Evolutions"),
    Target("Bakugan: Legends (2024)", "Bakugan Legends"),
    Target("Bakugan (2023)", "Bakugan 2023"),
]


def human_size(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def keyword_bonus(title):
    low = title.lower()
    return sum(w for kw, w in PREFER if kw in low)


def parse_rss(data, base):
    root = ET.fromstring(data)
    items = []
    for item in root.findall(".//item"):
        def local(name):
            el = item.find(name)
            if el is None:
                el = item.find(f"{{https://nyaa.si/xmlns/nyaa}}{name}")
            return el.text.strip() if el is not None and el.text else ""

        title = local("title")
        link = local("link")
        view = local("guid")
        infohash = local("infoHash")
        seeders = local("seeders")
        leechers = local("leechers")
        size = local("size")

        items.append(
            TorrentInfo(
                title=title,
                link=link,
                view=view,
                infohash=infohash,
                seeders=int(seeders or 0),
                leechers=int(leechers or 0),
                size=size or "?",
                score=0,
            )
        )
    for t in items:
        t.score = t.seeders + resolution_bonus(t.title) + keyword_bonus(t.title)
    return items


def search(base, query, category, trusted, delay, retries=3):
    url = f"{base}/?page=rss&q={quote(query)}&c={category}&f={2 if trusted else 0}&o=desc&s=seeders"
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
            return parse_rss(data, base)
        except (HTTPError, URLError, OSError) as exc:
            if attempt == retries - 1:
                print(f"  ! request failed for '{query}': {exc}", file=sys.stderr)
                return []
            time.sleep(delay * (attempt + 1))
    return []


def sanitize(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


def download_torrent(info, output_dir, delay):
    os.makedirs(output_dir, exist_ok=True)
    fname = sanitize(info.title) + ".torrent"
    path = os.path.join(output_dir, fname)
    if os.path.exists(path):
        print(f"  = already exists: {fname}")
        return True
    req = Request(info.link, headers={"User-Agent": USER_AGENT, "Referer": info.view})
    with urlopen(req, timeout=30) as resp:
        data = resp.read()
    if not data or b"<!DOCTYPE html" in data[:512]:
        print(f"  ! got HTML instead of torrent for {fname}", file=sys.stderr)
        return False
    with open(path, "wb") as fh:
        fh.write(data)
    print(f"  + saved {fname} ({human_size(len(data))})")
    time.sleep(delay)
    return True


def pick(results, interactive, limit):
    if not results:
        return []
    top = sorted(results, key=lambda t: t.score, reverse=True)[:limit]
    if not interactive:
        return [top[0]]
    print("  Choose a match (enter number, blank = best, 's' = skip):")
    for i, t in enumerate(top, 1):
        print(f"    {i}. [{t.seeders} seed] {t.title}  ({t.size})")
    while True:
        choice = input("  > ").strip().lower()
        if choice in ("", "s"):
            return [] if choice == "s" else [top[0]]
        if choice.isdigit() and 1 <= int(choice) <= len(top):
            return [top[int(choice) - 1]]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--download", action="store_true", help="actually download .torrent files")
    parser.add_argument("--interactive", action="store_true", help="prompt to choose a match per target")
    parser.add_argument("--dry-run", action="store_true", help="only print matches (default behaviour)")
    parser.add_argument("--limit", type=int, default=5, help="top N results to consider per target")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="download directory")
    parser.add_argument("--min-seeders", type=int, default=1, help="skip results below this many seeders (default: 1)")
    parser.add_argument("--warn-seeders", type=int, default=3, help="flag downloads with fewer than this many seeders")
    parser.add_argument("--category", default=CATEGORY_ANIME_ENGLISH, help="nyaa category (default: English-translated anime)")
    parser.add_argument("--trusted", action="store_true", help="only trusted uploads")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds to wait between requests")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="nyaa base URL")
    parser.add_argument("--only", help="only run targets whose name contains this substring")
    parser.add_argument("--exclude", help="skip targets whose name contains this substring")
    args = parser.parse_args()

    targets = [t for t in TARGETS if (not args.only or args.only.lower() in t.name.lower()) and (not args.exclude or args.exclude.lower() not in t.name.lower())]

    print(f"Targets: {len(targets)}  |  category: {args.category}  |  mode: {'download' if args.download else 'dry-run'}")
    print(f"Output: {args.output}  |  min-seeders: {args.min_seeders}")
    downloaded = skipped = 0
    missing_names = []
    low_seed_names = []

    for idx, t in enumerate(targets, 1):
        print(f"\n[{idx}/{len(targets)}] {t.name}  ->  \"{t.query}\"")
        results = search(args.base_url, t.query, args.category, args.trusted, args.delay)
        if not results and args.category != CATEGORY_ANIME:
            print("  ! no results in category; retrying all-anime (1_0)")
            results = search(args.base_url, t.query, CATEGORY_ANIME, args.trusted, args.delay)
        if not results:
            print("  ! no results at all")
            missing_names.append(t.name)
            continue
        if t.avoid:
            def norm(s):
                return re.sub(r"[^a-z0-9]+", " ", s.lower())

            results = [r for r in results if not any(norm(a) in norm(r.title) for a in t.avoid)]
        results = [r for r in results if r.seeders >= args.min_seeders]
        if not results:
            print(f"  ! results exist but all have fewer than {args.min_seeders} seeder(s)")
            missing_names.append(t.name)
            continue
        results.sort(key=lambda r: r.score, reverse=True)
        if args.interactive:
            chosen = pick(results, True, args.limit)
        elif t.multi > 1:
            chosen = results[:t.multi]
        else:
            chosen = results[:1]
        if not chosen:
            skipped += 1
            continue
        for c in chosen:
            low = c.seeders < args.warn_seeders
            if low:
                low_seed_names.append(f"{t.name} ({c.seeders} seed) -> {c.title}")
            if args.download:
                ok = download_torrent(c, args.output, args.delay)
                if ok:
                    downloaded += 1
                    if low:
                        print(f"  ! WARNING: only {c.seeders} seeder(s) for this torrent")
            else:
                flag = f"  [LOW SEEDERS]" if low else ""
                print(f"  * [{c.seeders} seed, {c.size}, score {c.score}]{flag} {c.title}")
                print(f"      {c.view}")
        time.sleep(args.delay)

    print(f"\nDone. downloaded={downloaded} skipped={skipped} missing={len(missing_names)}")
    if missing_names:
        print("\nMISSING (no usable result found):")
        for name in missing_names:
            print(f"  - {name}")
    if low_seed_names:
        print("\nLOW SEEDERS (may stall in your client):")
        for line in low_seed_names:
            print(f"  - {line}")


if __name__ == "__main__":
    main()
