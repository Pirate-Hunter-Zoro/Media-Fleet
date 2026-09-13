#!/usr/bin/env python3
"""Regression test for the magnet acceptance gate (§4.120 / §5 item 1).

Two halves, because they prove different things and only one of them can be trusted to
mean the same thing tomorrow.

**Part 1 — frozen synthetic cases.** Every corroboration rule, exercised against a
temporary in-memory library it builds itself. These inputs never change, so a failure here
is always a code regression. §7: a regression test whose inputs are live mutable state
cannot tell a broken guard from a repaired record — so the cases that PROVE a rule fires
are frozen, and each is a real shape found in the live queue, not an invention.

**Part 2 — counterfactual replay over the whole journal.** For every completed record,
rebuild the library as it was before that record landed (owned-now minus the items its own
plan says it FILED — read from the destination paths, so the mapper cannot vouch for
itself) and check the gate would not have refused it. A refusal there is content the fleet
did acquire and would now throw away. This half reads live state, so its counts drift; the
assertion is the safety property, which does not: **zero false refusals.**

    python3 scripts/test_acceptance_gate.py

Read-only. Exit 0 means every check passed.
"""

import json
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import acceptance_gate                                           # noqa: E402
import config                                                    # noqa: E402

librarydb = acceptance_gate.librarydb
acceptance = acceptance_gate.acceptance

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


# --------------------------------------------------------------------------------------
# Part 1: frozen synthetic cases
# --------------------------------------------------------------------------------------

def _fresh_db():
    """A throwaway library DB with the real schema."""
    tmp = Path(tempfile.mkdtemp(prefix="gate-test-")) / "library.db"
    conn = sqlite3.connect(str(tmp))
    conn.row_factory = sqlite3.Row
    conn.executescript(librarydb._SCHEMA)
    conn.commit()
    return conn


def _series(conn, name, kind, episodes=(), volumes=(), movies=0):
    sid = librarydb.add_series(conn, name, kind, source="library")
    for season, number in episodes:
        librarydb.add_media(conn, sid, "episode", season, number, title=f"E{number}")
    for number in volumes:
        librarydb.add_media(conn, sid, "volume", None, number, title=f"v{number}")
    for _ in range(movies):
        librarydb.add_media(conn, sid, "movie", None, None, title=name)
    conn.commit()
    return sid


def part1():
    print("\nPart 1 — corroboration rules (frozen inputs)")

    # (a) A fully-read, fully-owned release IS refusable. The live-queue case was
    #     "Archer (2009) - Complete + Specials - [IT+EN]": 142 files, 142 items, all held.
    conn = _fresh_db()
    sid = _series(conn, "Archer (2009)", "tv",
                  episodes=[(s, e) for s in range(1, 5) for e in range(1, 11)])
    names = [f"Archer (2009) - S{s:02d}E{e:02d} - x.mkv"
             for s in range(1, 5) for e in range(1, 11)]
    v = acceptance.evaluate(conn, sid, "Archer (2009)", "tv", names,
                            "Archer (2009) Complete", refusal_is_terminal=True)
    check("a fully-mapped, fully-owned release is REFUSED", v.decision, acceptance.REFUSE)

    # ...and the same input must still be refused on the non-terminal drop path.
    v = acceptance.evaluate(conn, sid, "Archer (2009)", "tv", names,
                            "Archer (2009) Complete", refusal_is_terminal=False)
    check("...and refused on the drop path too", v.decision, acceptance.REFUSE)

    # (b) One unowned episode makes it an ACCEPT, terminal or not.
    v = acceptance.evaluate(conn, sid, "Archer (2009)", "tv",
                            names + ["Archer (2009) - S05E01 - x.mkv"],
                            "Archer (2009) Complete", refusal_is_terminal=True)
    check("one unowned episode -> ACCEPT", v.decision, acceptance.ACCEPT)
    check("...and it counts exactly one new item", v.new_count, 1)

    # (c) A SLIVER of a megapack must not be refusable. The live case was "Dragon Ball
    #     Complete Collection DB DBZ Z GT Super Daima Movies": 750 files, 20 mapped
    #     (Daima's S01Exx, colliding with Dragon Ball Z's own S01 numbering), all "owned".
    conn = _fresh_db()
    sid = _series(conn, "Dragon Ball Z", "anime",
                  episodes=[(1, e) for e in range(1, 30)])
    sliver = ([f"Daima - S01E{e:02d}.mkv" for e in range(1, 21)]
              + [f"Some Other Movie {i}.mkv" for i in range(730)])
    v = acceptance.evaluate(conn, sid, "Dragon Ball Z", "anime", sliver,
                            "Dragon Ball Complete Collection", refusal_is_terminal=True)
    check("a 3%-mapped megapack is UNKNOWN, not refused", v.decision, acceptance.UNKNOWN)
    v = acceptance.evaluate(conn, sid, "Dragon Ball Z", "anime", sliver,
                            "Dragon Ball Complete Collection", refusal_is_terminal=False)
    check("...but the drop path still refuses it (a refusal there is free)",
          v.decision, acceptance.REFUSE)

    # (d) A DEGENERATE mapping must not be refusable. The live case was "Digimon Adventure
    #     1999 S01": the ledger pointed at a `movie`-kind series row holding ONE item, so
    #     all 339 episode files typed `movie` -> one key -> "owned" -> a whole series
    #     refused. "Unknown" is not "one" (§7).
    conn = _fresh_db()
    sid = _series(conn, "Digimon Adventure (1999)", "movie", movies=1)
    many = [f"Digimon - Digital Monsters - S01E{e:02d} - x.mkv" for e in range(1, 40)]
    v = acceptance.evaluate(conn, sid, "Digimon Adventure (1999)", "movie", many,
                            "Digimon Adventure 1999 S01", refusal_is_terminal=True)
    check("39 files collapsed onto 1 item is UNKNOWN, not refused",
          v.decision, acceptance.UNKNOWN)
    check("...and the reason names the collapse",
          "degenerate" in v.reason, True)

    # (e) A refusal may not rest ENTIRELY on specials. The one false refusal in the
    #     journal replay was `Heaven.Officials.Blessing.S00E05`, which this library files
    #     as S00E04 — the release's own number matched a DIFFERENT special we held.
    conn = _fresh_db()
    sid = _series(conn, "Heaven Official's Blessing (2020)", "anime",
                  episodes=[(0, n) for n in range(1, 8)] + [(1, n) for n in range(1, 12)])
    v = acceptance.evaluate(conn, sid, "Heaven Official's Blessing (2020)", "anime",
                            ["Heaven.Officials.Blessing.S00E05.1080p.mkv"],
                            "Heaven.Officials.Blessing.S00E05.1080p", refusal_is_terminal=True)
    check("a specials-only 'already owned' is UNKNOWN, not refused",
          v.decision, acceptance.UNKNOWN)
    check("...and the reason names specials", "special" in v.reason, True)

    # (f) Subtitles must not dilute the mapped fraction: a file that cannot speak abstains
    #     rather than disagreeing (§7).
    conn = _fresh_db()
    sid = _series(conn, "Show X", "tv", episodes=[(1, e) for e in range(1, 11)])
    with_subs = ([f"Show X - S01E{e:02d}.mkv" for e in range(1, 11)]
                 + [f"Show X - S01E{e:02d}.srt" for e in range(1, 11)])
    v = acceptance.evaluate(conn, sid, "Show X", "tv", with_subs, "Show X S01",
                            refusal_is_terminal=True)
    check("a fully-owned release with sidecars is still REFUSED",
          v.decision, acceptance.REFUSE)

    # (g) An unreadable file list is UNKNOWN — never a refusal, and never a silent pass.
    #     This is the fansub form the parser still cannot read (§5 item 4).
    v = acceptance.evaluate(conn, sid, "Show X", "tv",
                            ["[HorribleSubs] Show X - 12 [1080p].mkv"], "Show X - 12",
                            refusal_is_terminal=True)
    check("an unreadable fansub file list is UNKNOWN", v.decision, acceptance.UNKNOWN)

    # (h) The gate never guesses a series: no ledger row -> UNKNOWN.
    v = acceptance.evaluate(conn, None, "Show X", "tv", ["Show X - S01E01.mkv"], "x",
                            refusal_is_terminal=True)
    check("no resolved series -> UNKNOWN", v.decision, acceptance.UNKNOWN)


# --------------------------------------------------------------------------------------
# Part 2: counterfactual replay over the journal
# --------------------------------------------------------------------------------------

_EP = re.compile(r"[Ss](\d+)[Ee](\d+)")
_VOL = re.compile(r"\bv(\d{1,4})\b", re.IGNORECASE)
_CH = re.compile(r"\bc(\d{2,4})\b", re.IGNORECASE)
_KIND = {"show": "anime", "movie": "movie", "comic": "manga",
         "novel": "lightnovel", "mixed": "anime"}


def _delivered_keys(plan):
    """The item keys a record actually FILED, read from its plan's destination paths."""
    keys = set()
    for f in plan.get("files") or []:
        dst = f.get("dst_rel") or ""
        if not dst:
            continue
        base = Path(dst).name
        m = _EP.search(base)
        if m:
            keys.add(librarydb.item_key("episode", int(m.group(1)), int(m.group(2))))
            continue
        m = _VOL.search(base)
        if m:
            keys.add(librarydb.item_key("volume", None, int(m.group(1))))
            continue
        m = _CH.search(base)
        if m:
            keys.add(librarydb.item_key("chapter", None, int(m.group(1))))
            continue
        if dst.startswith("Movies/"):
            keys.add(librarydb.item_key("movie", None, None))
    return keys


def part2():
    print("\nPart 2 — counterfactual replay over the ingest journal")
    jpath = config.STATE_DIR / "journal.jsonl"
    if not jpath.exists():
        print("  (no journal; skipped)")
        return

    by = {}
    with jpath.open(errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("info_hash"):
                by[r["info_hash"].lower()] = r

    done = [r for r in by.values()
            if r.get("status") == "completed" and (r.get("plan") or {}).get("files")]

    conn = librarydb.connect()
    real_owned = librarydb.owned_items
    tally = Counter()
    false_refusals = []

    for r in done:
        plan = r["plan"]
        names = [Path(f.get("src_rel") or f.get("src") or "").name
                 for f in plan.get("files") or []]
        names = [n for n in names if n]
        if not names:
            continue
        t = librarydb.torrent_by_hash(conn, r["info_hash"])
        s = librarydb.series_by_id(conn, t.get("series_id")) if t else None
        if s is None:
            kind = _KIND.get(plan.get("media_type"), "anime")
            sid = (librarydb.resolve_series_id(conn, plan.get("title") or "", kind)
                   or librarydb.resolve_series_id(conn, plan.get("title") or ""))
            s = librarydb.series_by_id(conn, sid) if sid else None
        if s is None:
            tally["series unresolvable"] += 1
            continue
        gone = _delivered_keys(plan)
        if not gone:
            tally["no destination items derivable"] += 1
            continue

        def owned_before(c, series_id, _gone=gone):
            d = dict(real_owned(c, series_id))
            for k in _gone:
                d.pop(k, None)
            return d

        librarydb.owned_items = owned_before
        try:
            v = acceptance.evaluate(conn, s["id"], s["name"], s["kind"], names,
                                    r.get("name") or "", ai_mapper=None,
                                    refusal_is_terminal=True)
        finally:
            librarydb.owned_items = real_owned
        tally[v.decision] += 1
        if v.decision == acceptance.REFUSE:
            false_refusals.append((r.get("name") or "", s, v))

    print(f"  {len(done)} completed record(s) with a plan; verdicts: "
          + " ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    for name, s, v in false_refusals[:10]:
        print(f"    would have REFUSED: {name[:70]}")
        print(f"      series={s['name']!r} kind={s['kind']}  {v.reason}")
    check("no completed record would have been refused", len(false_refusals), 0)


def main() -> int:
    print("=========== ACCEPTANCE GATE (§4.120) ===========")
    part1()
    part2()
    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: " + ", ".join(failures))
        return 1
    print("ALL ACCEPTANCE-GATE CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
