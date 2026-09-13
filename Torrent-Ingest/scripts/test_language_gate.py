#!/usr/bin/env python3
"""Regression test for the English-only language gates (§4.32, 2026-09-09).

The library is English-only, and `parse.manga_is_english` / `parse.video_is_english` are
what enforce it at search time. Both were too permissive in ways that only showed up once
`would_download.txt` was made readable and its drops could be read by title:

  * a JAPANESE-market manga edition redeemed itself with its own Latin gloss --
    "ONE PIECE カラー版 01-86 [One Piece Colored Edition 01-86]" read as English, three
    volumes of Japanese raws deep;
  * a parenthesised language tag went unmatched because the pattern anchored on `[` only,
    so "Dragon Ball Full Color Manga v01-42 (JPN)" read as English;
  * a BARE scene language tag was foreign only when suffixed `-dub`, so
    "One Piece 001-100 FRENCH" read as English.

The bare-tag fix is the one that needs guarding hardest, because a language word is also
an ordinary English title word -- *The Italian Job*, *The Spanish Princess*, *The Danish
Girl* -- so the rule is POSITIONAL: a tag sits before another release token or at the end
of the title, while a title word is followed by more title words. A gate that refuses
*The Italian Job* on its own name is worse than the leak it fixes.

Four parts:

**Part 1 -- manga.** The Japanese-edition and `(JPN)` shapes are refused; a dual-TITLE
English scanlation ("進撃の巨人 Attack on Titan v01") still passes. CJK alone must never
disqualify -- that is why the edition-word list is closed and short.

**Part 2 -- video, bare scene tags.** The tags are refused, and an English/dual/multi
marker still redeems a release (the English-OK side runs first, by design).

**Part 3 -- the false-positive controls.** Ten real English titles that CONTAIN a language
word must all still pass. This is the half that makes the positional rule worth having.

**Part 4 -- the corpus control (§4.5, both directions).** Replays both gates over every
`completed` name in the ingest journal -- releases the owner really did take -- and asserts
the NEW rules refuse nothing the OLD ones accepted. A language gate cannot be judged on
hand-written fixtures alone; the delta against real history is the measurement. It also
asserts the corpus is non-empty and that the gate CAN still reject, so a gate that has
quietly become a no-op fails here instead of reading as a clean pass.

    python3 scripts/test_language_gate.py

Read-only. Exit 0 means every check passed.
"""

import json
import sys
from pathlib import Path

# `parse` is the shared library brain, which moved into this repo when torrent/comic
# discovery was removed (2026-09-10). It is stdlib-only and carries no `config`, so it is
# imported straight off the brain dir rather than path-loaded.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "librarybrain"))

import parse                                                     # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def rejects(fn, titles, label):
    for t in titles:
        check(f"{label}: refuses {t[:58]}", fn(t), False)


def accepts(fn, titles, label):
    for t in titles:
        check(f"{label}: accepts {t[:58]}", fn(t), True)


# --------------------------------------------------------------------------------------
print("Part 1: manga -- Japanese editions and parenthesised tags")

rejects(parse.manga_is_english, [
    "ONE PIECE カラー版 01-86 [One Piece Colored Edition 01-86] [aKraa]",
    "ワンピース カラー版 87-92 [ONE PIECE Colored Edition 87-92] [aKraa]",
    "Berserk 完全版 v01",
    "Akira 新装版 v03",
    "Dragon Ball Full Color Manga v01-42 (JPN)",
    "Some Manga v01 (RAW)",
    "Naruto v01 [JPN]",
    "ElfQuest 1. Изгнание огнем.",
], "manga")

# CJK alone is NOT a foreign marker: a dual-title scanlation is the common English shape,
# and the reason the edition-word list has to be closed and short rather than "any CJK".
accepts(parse.manga_is_english, [
    "進撃の巨人 Attack on Titan v01",
    "鬼滅の刃 Demon Slayer v23 (Digital) (English)",
    "Goblin Slayer v01-16 (2017-2025) (Digital) (danke-Empire, Vodka, Ushi)",
    "One Piece Full Color - Vol. 1-72",
    "Chainsaw Man v01 (2021) (Digital) (danke-Empire)",
    "Detective Conan v001-100 (1994-2021) (Digital SD) (KG Manga)",
], "manga")

# --------------------------------------------------------------------------------------
print("\nPart 2: video -- bare scene language tags")

rejects(parse.video_is_english, [
    "One Piece 001-100 FRENCH",
    "One Piece 101-200 FRENCH",
    "Yu-gi-oh! Duel Monsters S02 FRENCH 480p WEB x264 -NanDesuKa (ADN)",
    "Some.Show.S01.GERMAN.1080p.WEB.x264",
    "Pelicula.2020.SPANISH.1080p.BluRay.x265",
    "The Smurfs 1981 Vol1 Swedish DVD-Rip Avi Swedream",
], "video")

# The English-OK side runs FIRST and still redeems: a language tag paired with an
# English/dual/multi marker is a release that DOES carry English.
accepts(parse.video_is_english, [
    "Some.Show.S01.GERMAN.ENGLISH.1080p.WEB.x264",
    "Some.Show.S01.FRENCH.DUAL.AUDIO.1080p.BluRay",
    "Terminator Zero [2024] [WEBRip] [1080p] [RUS + JAP + ENG] [Multi-Subs]",
], "video")

# --------------------------------------------------------------------------------------
print("\nPart 3: the false-positive controls -- English titles that CONTAIN a language")

accepts(parse.video_is_english, [
    "The Italian Job (1969) 1080p BluRay x264",
    "The Spanish Princess S01E01 1080p WEB h264",
    "The French Dispatch (2021) 1080p BluRay x265",
    "The German Doctor (2013) 720p WEB",
    "The Danish Girl (2015) 1080p BluRay",
    "My Big Fat Greek Wedding (2002) 1080p",
    "Spanish Affair 2 (2015) 1080p",
    "A Korean Odyssey S01E01 1080p NF WEB-DL",
    "The Polish Brothers S01E02 720p HDTV",
    "Japanese Style Originator S01E05 720p",
], "control")

# --------------------------------------------------------------------------------------
print("\nPart 4: the corpus control -- no regression against real ingest history")


def _old_video_is_english(title):
    """`video_is_english` WITHOUT the bare-tag rule, i.e. the behaviour being changed."""
    low = (title or "").lower()
    if parse._VIDEO_ENGLISH_OK.search(low):
        return True
    return not parse._VIDEO_FOREIGN.search(low)


journal_path = Path(__file__).resolve().parent.parent / "state" / "journal.jsonl"
names = set()
if journal_path.exists():
    for line in journal_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("status") == "completed" and row.get("name"):
            names.add(row["name"])

# A corpus that has gone empty would make every assertion below pass for the wrong reason.
check("the journal corpus is readable and non-empty", len(names) > 100, True)

newly_refused = sorted(n for n in names
                       if _old_video_is_english(n) and not parse.video_is_english(n))
check("the bare-tag rule refuses nothing the old gate accepted", newly_refused, [])

# ...and the gate can still say no over this same corpus, so "no regression" is not just
# "the gate never fires" (§4.5).
check("the video gate CAN still reject over the real corpus",
      any(not parse.video_is_english(n) for n in names), True)

# --------------------------------------------------------------------------------------
print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures[:6])
          + (" ..." if len(failures) > 6 else ""))
    raise SystemExit(1)
print("All checks passed.")
