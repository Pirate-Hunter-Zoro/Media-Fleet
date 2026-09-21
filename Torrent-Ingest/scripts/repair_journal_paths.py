#!/usr/bin/env python3
"""Repair journal records whose filed paths were left behind by a library-internal move.

    python3 scripts/repair_journal_paths.py            # dry run; prints every change
    python3 scripts/repair_journal_paths.py --apply

WHY THIS EXISTS (2026-09-20)

    `reconcile.py` re-queues a completed torrent whose applied files are absent from the
    inventory and both local tiers. That check is deliberately path-and-content based --
    it must not guess -- and it compares the inventory by CONTENT IDENTITY now (basename +
    byte size), so a move can no longer read as a loss. But the records that drifted
    BEFORE that fix still name the vacated paths, and the running daemon keeps auditing
    them with the old code until it restarts.

    The 2026-09-20 One Piece franchise migration (`migrate_comics.sh`, 191 files) moved
    files without rewriting the journal records that named them. Every moved chapter
    completion then read as "gone": it was re-queued, re-downloaded and re-filed, and the
    five chapters a volume already covered were purged again by the chapter reconciler --
    the loop repeated every cycle, spending the constrained free-provider budget the
    Smurfs re-fetch was waiting on.

WHAT IT DOES

  * REWRITES `applied`, the plan's `dst_rel`/`_dst_abs` and `chunk_filed` for every entry
    whose exact path is gone but whose (basename, byte size) is in the inventory under
    exactly one other key. Ambiguous matches (several keys, same bytes) are present but
    never rewritten, and unknown ones are reported -- never guessed.
  * CLOSES a record ONLY when NO applied file exists under any witness and library.db
    proves every missing one was deliberately superseded (`reconcile_closed`, plus the
    older `reconcile_dead` switch the running daemon already honors). A multi-file record
    that still holds any of its files is never closed -- that is reconcile's own
    "present" verdict, and closing it would hide the files that remain.
  * CLEARS a stale `re-queued:` error on a record whose files are all present but which
    had already been re-queued before the paths were repaired.

    Nothing is deleted, no file is moved, and a record it cannot prove is left untouched.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                          # noqa: E402
import journal                                                         # noqa: E402
import reconcile                                                       # noqa: E402


def _rel_of(dst) -> str | None:
    if not dst:
        return None
    try:
        return Path(dst).relative_to(config.MEDIA_ROOT.resolve()).as_posix()
    except ValueError:
        return None


def _moved_candidates(rel: str, size, keys: set, index: dict) -> list[str]:
    if rel in keys or reconcile._is_present_local(rel) or size is None:
        return []
    return [c for c in index.get((Path(rel).name, size), []) if c != rel]


def _rewrite(record, moves: dict) -> int:
    """Point the record's applied/plan/chunk data at the moved files. Returns entries."""
    n = 0
    for f in (record.get("applied") or []):
        rel = _rel_of(f.get("dst"))
        if rel in moves:
            f["dst"] = str(config.MEDIA_ROOT / moves[rel])
            n += 1
    for f in (record.get("plan") or {}).get("files") or []:
        rel = f.get("dst_rel")
        if rel in moves:
            f["dst_rel"] = moves[rel]
            if f.get("_dst_abs"):
                f["_dst_abs"] = str(config.MEDIA_ROOT / moves[rel])
            n += 1
    cf = record.get("chunk_filed")
    if isinstance(cf, dict):
        for key, rel in list(cf.items()):
            if rel in moves:
                cf[key] = moves[rel]
                n += 1
    return n


def classify(record, keys: set, index: dict) -> tuple[dict, int, int, list]:
    """`(moves, exact, ambiguous, unresolved)` for one record's applied files."""
    moves: dict = {}
    exact = ambiguous = 0
    unresolved: list[tuple[str, int | None]] = []
    for rel, size in reconcile._applied_entries(record):
        if rel in keys or reconcile._is_present_local(rel):
            exact += 1
            continue
        cands = _moved_candidates(rel, size, keys, index)
        if len(cands) == 1:
            moves[rel] = cands[0]
        elif cands:
            ambiguous += 1
        else:
            unresolved.append((rel, size))
    return moves, exact, ambiguous, unresolved


def close_verdict(moves: dict, exact: int, ambiguous: int, unresolved: list) -> bool:
    """The close gate: NO applied file exists under any witness, and something remains.

    A record with a present file, a present-but-ambiguous moved file, or a path to
    rewrite is never closed -- closing it would hide content the library still holds.
    """
    return not moves and not exact and not ambiguous and bool(unresolved)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the repairs (default: dry run)")
    ap.add_argument("--record", help="only this info hash")
    args = ap.parse_args()

    keys, index = reconcile._remote_views()
    if keys is None:
        raise SystemExit("REFUSED: remote inventory unreadable; nothing can be proven")

    counts = {"moved_records": 0, "moved_entries": 0, "closed": 0,
              "stale_error": 0, "unresolved": 0, "already": 0}
    for h, rec in sorted(journal.load_records().items()):
        if args.record and h != args.record:
            continue
        if rec.get("status") != journal.COMPLETED:
            continue
        if rec.get("reconcile_dead") or rec.get("reconcile_closed"):
            continue
        entries = reconcile._applied_entries(rec)
        if not entries:
            continue
        name = str(rec.get("name") or h[:12])
        moves, exact, ambiguous, unresolved = classify(rec, keys, index)

        if not moves and not ambiguous and not unresolved:
            counts["already"] += 1
            if (rec.get("error") or "").startswith("re-queued"):
                rec["error"] = None
                print(f"  {name}: stale re-queued error cleared (all files present)")
                counts["stale_error"] += 1
                if args.apply:
                    journal.write_record(rec)
            continue

        if moves:
            n = _rewrite(rec, moves)
            counts["moved_records"] += 1
            counts["moved_entries"] += n
            if (rec.get("error") or "").startswith("re-queued"):
                rec["error"] = None
            sample = ", ".join(sorted(set(moves.values()))[:2])
            print(f"  {name}: {len(moves)} path(s) -> {sample}"
                  + (" ..." if len(set(moves.values())) > 2 else ""))
            if args.apply:
                journal.write_record(rec)

        # Close ONLY when nothing is held under any witness and every missing file has
        # library.db's deliberate-supersede verdict. A record with even one present or
        # ambiguous file is reconcile's "present" and must never be closed.
        if close_verdict(moves, exact, ambiguous, unresolved) \
                and reconcile._superseded_evidence([rel for rel, _s in unresolved]):
            rec["reconcile_closed"] = "superseded"
            rec["reconcile_dead"] = True        # the switch the running daemon honors
            rec["error"] = "reconcile: content deliberately superseded; not re-acquiring"
            counts["closed"] += 1
            print(f"  {name}: deliberately superseded -> closed, not re-queued")
            if args.apply:
                journal.write_record(rec)
                journal.log_decision(
                    h, name,
                    "repair_journal_paths: content deliberately superseded by library.db "
                    "(reconcile closed; not re-acquiring). Paths:\n  " +
                    "\n  ".join(rel for rel, _s in unresolved))
            continue
        if unresolved:
            counts["unresolved"] += 1
            print(f"  {name}: {len(unresolved)} file(s) unresolved (left untouched): " +
                  ", ".join(rel for rel, _s in unresolved[:2]) +
                  (" ..." if len(unresolved) > 2 else ""))

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"\n{mode}: {counts['moved_records']} record(s) rewritten "
          f"({counts['moved_entries']} entr(ies)), {counts['closed']} closed as superseded, "
          f"{counts['stale_error']} stale error(s) cleared, "
          f"{counts['already']} already exact, {counts['unresolved']} unresolved")
    if not args.apply:
        print("Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
