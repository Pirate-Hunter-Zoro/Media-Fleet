#!/usr/bin/env python3
"""Blocklist entries that match NOTHING — the mirror of `audit_blocklist_collisions.py`.
Read-only.

`audit_blocklist_collisions.py` finds rows that match too MUCH (a bare franchise word
hiding the original series, §4.182). Nothing found rows that match too LITTLE, and that
cost the owner a purge that never happened (§4.186):

    blocklist row 'Saiki? no'  ->  norm 'saiki no'  ->  matches nothing, anywhere

It sat there for weeks. `purge_sweeper` never marked the title, the searcher was never told
to stop wanting it, and a July purge stalled half-applied — 56 of 83 files queued, 0 ever
purged from the pool, 27 manga volumes never touched. The owner found it by looking at
Infuse. **A blocklist row that matches nothing is indistinguishable from no row at all, and
both look exactly like a completed purge.**

WHAT THIS SEPARATES, AND WHY THE SEPARATION IS THE WHOLE POINT. A row matching no live
title has two completely different meanings, and §5b item 11 asks for them apart:

  LIVE     the row matches a title on the mount, or a name in `wants.json`. It is doing
           work right now — holding a purge down, or keeping the searcher off a title.
  DRAINED  no live match, but the row matches HISTORY: a `series`/`series_alias` row in
           `library.db`, a line in `reap_purges.log`, or a queued pool deletion. The row
           named something real, that thing is gone, and the row is still correctly
           standing guard against re-acquisition (§4.155). Nothing to do.
  ORPHAN   no match anywhere, ever, in any corpus. The row is doing nothing at all.

AND A THIRD CASE THE ITEM DID NOT ANTICIPATE, WHICH IS MOST OF THEM. §8.1 step 5 *requires*
adding a purged title "with its ROMAJI form". A romaji row for content that was only ever
FILED under its English title matches nothing by construction — `'Goburin Sureiya'` is an
orphan, and it is also exactly what the runbook asked for. It is not a typo; it is a guard
against a future release arriving under that name. Measured 2026-09-05: of 31 orphans, the
large majority are these.

So "matches nothing" is NOT by itself a defect, and this tool must not claim it is. What
separates a §4.186 typo from a deliberate alias is whether the WORK is covered by some other
blocklist row: `'Goburin Sureiya'` sits beside `'Goblin Slayer'`, `'Saiki Kusuo no Sainan'`
beside `'The Disastrous Life of Saiki K.'`. A row that is orphaned AND has no covering
sibling is the one worth a human's attention — that is the shape `'Saiki? no'` had.

AND THIS TOOL DELIBERATELY DOES NOT COMPUTE THAT PAIRING, BECAUSE IT CANNOT. A string
near-miss pass was written, measured against the live blocklist, and deleted: at any usable
cutoff it paired `'Rezero'` with `'Ga-Rei: Zero'` and `'Shingeki no Bahamut'` with
`'Shokugeki no Soma'`, while scoring every genuine romaji/English pair far too low to see —
because romaji and English translations of the same title share almost no characters. It was
wrong in both directions at once, which is the §4.174 lesson exactly. Pairing them needs a
dictionary or a person, and §5b item 4 is the standing reminder not to guess an alignment.

So the verdict this tool delivers is deliberately modest, and it is still the thing §5b item
11 asked for: **581 rows narrowed to the 31 that provably do nothing**, plus the one fact
about them that can be stated rather than guessed — which rows normalize IDENTICALLY to
another row, and are therefore exact duplicates. Everything past that is a reader's call.

WHY `library.db` COUNTS AS HISTORY AND NOT AS A LIVE MATCH. §4.176: the DB over-claims
ownership by 32 % — 14,262 of 44,698 owned `media` rows belong to purged titles, because a
purge never reconciled it. That makes it a poor witness for "we hold this" and an excellent
one for "this title once existed under this name", which is exactly the question here.

WHY THIS READS `wants.json` RAW AND NEVER `load_wants()`. `Torrent-Searcher/ingest.load_wants()`
APPLIES THE BLOCKLIST (`ingest.py:257-272`) — it drops every blocked want, and blocked
sub-entries of unblocked wants too. Asking it "does any want match this blocklist row" can
only ever answer no, for every row, including the ones working perfectly. That is §4.148 and
§4.153's bug wearing a new hat: a filter that structurally cannot be true, reporting a clean
result. §3's standing advice to measure `load_wants()` rather than the file is about counting
search PRESSURE; for this question the raw file is the only honest corpus.

FAILURE DIRECTION: DELIBERATELY GENEROUS WITH EVIDENCE. Every ambiguity resolves toward
"this row matched something", because ORPHAN is the accusatory verdict — it invites someone
to go and edit a blocklist row, and §4.182 is what a careless blocklist edit costs. A missed
orphan costs another look; a false orphan can un-hide a franchise.

DELIBERATELY REPORT-ONLY. What a typo'd row was MEANT to say is a content decision only the
owner can make (§4.167), and the repair is never just "fix the spelling": re-running the
purge the row failed to enforce is a §8.1 job, and any new row must be re-checked with
`audit_blocklist_collisions.py` before it lands (§4.182).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                       # noqa: E402

MOUNT = config.MEDIAFS_MOUNT
BLOCKLIST = config.STATE_DIR / "blocklist.json"
WANTS = config.STATE_DIR / "wants.json"
LIBRARY_DB = config.STATE_DIR / "library.db"
PURGE_LOG = config.STATE_DIR / "reap_purges.log"
DELETIONS_DIR = config.MEDIA_SYNCER_DIR
DELETIONS_GLOB = "mediafs_deletions.jsonl*"
ROOTS = ["Shows", "Movies", "Comics/Manga", "Comics"]

# Corpora that mean "this row is doing work right now" vs. "this row names a real thing
# that is already gone". Order matters only for which label a row is reported under.
LIVE_SOURCES = ("library", "wants")
HISTORY_SOURCES = ("db", "purged", "queued")


def _norm(s: str) -> str:
    """Byte-identical to `purge_sweeper._norm` and the collisions audit, so this reports
    what the sweeper actually does rather than a second opinion about it."""
    s = re.sub(r"\((?:19|20)\d{2}\)", " ", s or "")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _matches(title_norm: str, blocked_norm: str) -> bool:
    """The SAME rule `purge_sweeper._is_blocked` applies (purge_sweeper.py:90-93)."""
    return bool(title_norm) and (
        title_norm == blocked_norm
        or title_norm.startswith(blocked_norm + " ")
        or f" {blocked_norm} " in f" {title_norm} ")


def _blocked() -> dict:
    """{original spelling: norm}. Rows whose norm is empty are reported separately —
    an all-punctuation row matches nothing for a different reason than a typo does."""
    raw = json.loads(BLOCKLIST.read_text(encoding="utf-8"))
    names = raw.get("titles", []) if isinstance(raw, dict) else raw
    return {n: _norm(n) for n in names if isinstance(n, str)}


def _library_titles() -> set:
    """Title directories on the MOUNT. `~/Media` is a cache and 511 of 518 show folders
    hold zero local files (§1) — only the mount sees the whole library."""
    out = set()
    for root in ROOTS:
        base = MOUNT / root
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if d.name.startswith("."):
                continue
            if root == "Comics" and d.name == "Manga":
                continue
            # Movies/ is FLAT (§1): one file per film, so the film's title is the stem.
            out.add(d.name if d.is_dir() else d.stem)
    return out


def _want_titles() -> set:
    """Every name in the RAW file — top-level wants and the nested manga / comics /
    light-novel / series arms, which carry names of their own that a row may target."""
    out = set()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("name"), str):
                out.add(node["name"])
            for key in ("manga", "comics", "light_novels", "series"):
                walk(node.get(key))
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(WANTS.read_text(encoding="utf-8")))
    return out


def _db_titles() -> set:
    """`series` display names plus every `series_alias`. Historical by §4.176."""
    out = set()
    try:
        con = sqlite3.connect(f"file:{LIBRARY_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        for table, col in (("series", "name"), ("series_alias", "alias")):
            try:
                out.update(r[0] for r in con.execute(f"SELECT {col} FROM {table}")
                           if isinstance(r[0], str))
            except sqlite3.Error:
                continue          # a missing table is not evidence of absence
    finally:
        con.close()
    return out


def _titles_from_relpath(rel: str) -> set:
    """Candidate title names in a library-relative path.

    Generous on purpose (see the failure-direction note): under `Comics/` both the second
    and third components are offered, because `Comics/Manga/<Title>/…` and
    `Comics/<Collection>/…` are both real layouts and guessing wrong would manufacture an
    orphan. A file at depth 2 is a flat `Movies/` entry, so its stem is the title.
    """
    parts = Path(rel).parts
    if len(parts) < 2:
        return set()
    out = {parts[1], Path(parts[1]).stem}
    if parts[0] == "Comics" and len(parts) > 2:
        out.add(parts[2])
    return {p for p in out if p and not p.startswith(".")}


def _purged_titles() -> set:
    """Titles named in `reap_purges.log` — pool copies actually destroyed."""
    out = set()
    try:
        with PURGE_LOG.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.search(r"\bPURGE\s+(.*?)\s+->", line)
                if m:
                    out |= _titles_from_relpath(m.group(1))
    except OSError:
        pass
    return out


def _queued_titles() -> set:
    """Titles with pool deletions queued or in flight. Every rotation of the journal is
    read: `.processing` alone held 8,240 lines this session, and a title that finished
    draining yesterday is still evidence that the row once matched something."""
    out = set()
    for p in sorted(DELETIONS_DIR.glob(DELETIONS_GLOB)):
        try:
            with p.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rel = json.loads(line).get("path") or ""
                    except ValueError:
                        continue
                    out |= _titles_from_relpath(rel)
        except OSError:
            continue
    return out


def classify(blocked: dict, corpora: dict) -> dict:
    """{original spelling: sorted list of corpus names it matched}. The pure core, so the
    self-test can drive it with fixtures instead of the live fleet."""
    normed = {name: {_norm(t) for t in titles} for name, titles in corpora.items()}
    out = {}
    for orig, bn in blocked.items():
        hits = []
        if bn:
            for source, title_norms in normed.items():
                if any(_matches(tn, bn) for tn in title_norms):
                    hits.append(source)
        out[orig] = sorted(hits)
    return out


def _selftest() -> int:
    """§7: when a check reports nothing, prove it CAN report something — in BOTH
    directions (§4.174). Drives `classify` with fixtures covering all three verdicts,
    including the exact string that caused §4.186.
    """
    corpora = {
        "library": {"The Disastrous Life of Saiki K. (2016)", "Sailor Moon (1992)"},
        "wants": {"One Piece"},
        "db": {"Fairy Tail"},
        "purged": {"Fairy Tail"},
        "queued": set(),
    }
    cases = [
        # (row, expected verdict, why)
        ("Saiki? no", "ORPHAN", "the real §4.186 string: norm 'saiki no' matches nothing"),
        ("Sailor Moon", "LIVE", "matches a title on the mount"),
        ("One Piece", "LIVE", "matches a raw wants.json name"),
        ("Fairy Tail", "DRAINED", "gone from the mount, still named by db + purge log"),
        ("!!!", "EMPTY", "normalizes to the empty string, matches nothing by construction"),
        ("Saiki", "LIVE", "the PREFIX rule does match, proving the matcher is not just off"),
    ]
    blocked = {row: _norm(row) for row, _e, _w in cases}
    hits = classify(blocked, corpora)
    bad = 0
    for row, expected, why in cases:
        got = _verdict(_norm(row), hits[row])
        ok = got == expected
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {row!r:<14} -> {got:<8} "
              f"(expected {expected}) — {why}")
    print("\nself-test: " + ("all verdicts reachable and correct."
                             if not bad else f"{bad} case(s) WRONG."))
    return 1 if bad else 0


def _verdict(bn: str, hits: list) -> str:
    if not bn:
        return "EMPTY"
    if any(h in LIVE_SOURCES for h in hits):
        return "LIVE"
    if any(h in HISTORY_SOURCES for h in hits):
        return "DRAINED"
    return "ORPHAN"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true",
                    help="prove every verdict can fire, then exit (touches no live state)")
    ap.add_argument("--all", action="store_true",
                    help="also list the LIVE and DRAINED rows, not just the orphans")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    # An absent mount reads as an empty library, and an empty library would classify EVERY
    # row as an orphan — the §4.108 failure mode, pointed at the blocklist. Refuse.
    if not MOUNT.is_dir() or not any((MOUNT / "Shows").iterdir()):
        print("the mediafs mount is absent or empty; refusing to judge anything.")
        return 1
    try:
        blocked = _blocked()
    except (OSError, ValueError) as e:
        print(f"cannot read {BLOCKLIST}: {e}")
        return 1
    if not blocked:
        print(f"{BLOCKLIST} lists no titles; nothing to audit.")
        return 1

    corpora = {"library": _library_titles(), "db": _db_titles(),
               "purged": _purged_titles(), "queued": _queued_titles()}
    try:
        corpora["wants"] = _want_titles()
    except (OSError, ValueError) as e:
        # Without wants.json a live row can look drained, never orphaned, so the ORPHAN
        # verdict stays trustworthy and the run is still worth completing.
        print(f"note: cannot read {WANTS} ({e}); 'wants' evidence unavailable.\n")
        corpora["wants"] = set()

    for name in ("library", "wants"):
        if not corpora[name]:
            print(f"the {name} corpus is EMPTY -- every row would look orphaned. Refusing.")
            return 1

    hits = classify(blocked, corpora)
    buckets = {"LIVE": [], "DRAINED": [], "ORPHAN": [], "EMPTY": []}
    for orig, bn in blocked.items():
        buckets[_verdict(bn, hits[orig])].append(orig)

    print("corpus sizes: " + "  ".join(f"{k}={len(v)}" for k, v in sorted(corpora.items())))
    print(f"blocklist rows: {len(blocked)}   "
          + "  ".join(f"{k}={len(v)}" for k, v in buckets.items()) + "\n")

    if args.all:
        for label in ("LIVE", "DRAINED"):
            print(f"--- {label} ({len(buckets[label])}) ---")
            for orig in sorted(buckets[label], key=str.lower):
                print(f"  {orig!r:<44} {','.join(hits[orig])}")
            print()

    if buckets["EMPTY"]:
        print(f"!! {len(buckets['EMPTY'])} row(s) normalize to the EMPTY STRING and can "
              f"never match anything:")
        for orig in sorted(buckets["EMPTY"], key=str.lower):
            print(f"     {orig!r}")
        print()

    if not buckets["ORPHAN"]:
        print("No orphans. Every blocklist row matches a title that exists now or "
              "provably existed once.")
        return 0

    # Rows that normalize IDENTICALLY to another row are exact duplicates -- the one thing
    # about an orphan that can be stated as fact rather than guessed. A near-miss heuristic
    # was tried here and REMOVED: measured 2026-09-05 it paired 'Rezero' with 'Ga-Rei: Zero'
    # and 'Shingeki no Bahamut' with 'Shokugeki no Soma', while missing every real pair
    # ('Goburin Sureiya'/'Goblin Slayer' scores far below any usable cutoff). Romaji and
    # English share no characters to compare; string distance cannot see through a
    # translation, and a confident wrong pairing is worse here than no pairing at all.
    by_norm = {}
    for orig, bn in blocked.items():
        by_norm.setdefault(bn, []).append(orig)

    print(f"{len(buckets['ORPHAN'])} row(s) match NOTHING in any corpus -- not the library, "
          f"not wants.json, not library.db, not the purge log, not the deletion queue:\n")
    for orig in sorted(buckets["ORPHAN"], key=str.lower):
        dupes = [o for o in by_norm[blocked[orig]] if o != orig]
        tail = f"   EXACT DUPLICATE of {', '.join(repr(d) for d in dupes)}" if dupes else ""
        print(f"  {orig!r:<52} (norm: {blocked[orig]!r}){tail}")

    print("\nREAD THIS BEFORE TOUCHING ANYTHING. 'Matches nothing' is NOT by itself a "
          "defect. §8.1 step 5 REQUIRES adding a purged title's ROMAJI form, and a romaji "
          "row for content only ever filed in English matches nothing by construction --\n"
          "measured 2026-09-05, that is most of this list. This is a REVIEW list, not a "
          "bug list, and nothing here should be edited on the strength of appearing on it.\n"
          "\nThe one worth a human's eye is a row that is NOT a romaji or alternate "
          "spelling of some other row, because that is the shape 'Saiki? no' had: a purge "
          "nobody enforced (§4.186). Separating those two needs someone who knows what the "
          "title MEANT -- string distance provably cannot (see the note in the source), and "
          "guessing is what §5b item 4 exists to warn against.\n"
          "\nIf one IS a typo: the repair is to re-run §8.1 for the title it meant, not "
          "merely to fix the spelling, and to re-run audit_blocklist_collisions.py before "
          "landing any replacement row (§4.182).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
