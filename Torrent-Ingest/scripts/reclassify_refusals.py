#!/usr/bin/env python3
"""Re-file historical `failed` journal records that were deliberate REFUSALS.

The owner's complaint on 2026-08-31 was "a bunch of torrents failed and I don't like
that". 70 records sat at `failed`, and reading their verbatim `error` strings (never a
regex guess at what they probably said -- an earlier pass bucketed by regex and mislabelled
four) shows a large minority are the pipeline working exactly as designed: a redundant
encode of a film already held, a season pack superseded by a better complete pack already
downloading, a 480p rip of a season owned in full, an Italian-only release the language
filter caught. Those are decisions, not faults, and `journal.REFUSED` now exists for them.

New refusals are recorded correctly at the point of decision (`ingest._fail(..,
refused=True)`). This script is the one-time backfill for the records written before that
existed. It is idempotent and append-only -- the journal is a log, so this appends a
corrected record rather than rewriting history.

    python3 scripts/reclassify_refusals.py            # report only
    python3 scripts/reclassify_refusals.py --apply    # write the corrected records
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import journal                                                         # noqa: E402

# Matched against the START of the record's own error string, which is written by exactly
# one call site each, so these are identities rather than guesses at natural language.
REFUSAL_PREFIXES = (
    "redundant:",          # content the library already holds, or a queued pack covers
    "superseded:",         # a materially better copy of the same span is already coming
    "480p rip of a season the library already owns",
    "Italian-only release",
)


def is_refusal(err: str) -> bool:
    e = (err or "").strip()
    return any(e.startswith(p) for p in REFUSAL_PREFIXES)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the corrected records")
    args = ap.parse_args()

    records = journal.load_records()
    failed = [r for r in records.values() if r.get("status") == journal.FAILED]
    hits = [r for r in failed if is_refusal(r.get("error"))]

    print(f"failed records: {len(failed)}")
    print(f"deliberate refusals among them: {len(hits)}")
    for r in hits:
        print(f"  {(r.get('name') or '?')[:66]:68} {(r.get('error') or '')[:70]}")
    print(f"\nreal failures after reclassification: {len(failed) - len(hits)}")
    if not args.apply:
        print("\n(dry run -- pass --apply to write)")
        return
    for r in hits:
        r["status"] = journal.REFUSED
        journal.write_record(r)
    print(f"\nwrote {len(hits)} corrected record(s) as '{journal.REFUSED}'.")


if __name__ == "__main__":
    main()
