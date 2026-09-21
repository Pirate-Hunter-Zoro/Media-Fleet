#!/usr/bin/env python3
"""A completion is re-queued only when every computed witness says its files are gone.

    python3 scripts/test_reconcile_presence.py

WHY THIS EXISTS (2026-09-20)

    `reconcile.reconcile` re-queues a `completed` torrent whose applied files it cannot
    find, which is right for a genuine loss and catastrophic when the witness is blind.
    It was blind twice, and the two faults compound:

      1. Presence was checked against the SSD (`MEDIA_ROOT`) and the inventory keys only.
         The SSD is a cache -- the tier engine evicts freely and the MOUNT still serves
         the file (HANDOFF §2.1) -- and a library-internal MOVE rewrites the inventory key
         without touching the journal record. The 2026-09-20 One Piece franchise migration
         moved 191 files, so every moved chapter read as "gone" and was re-queued,
         re-downloaded and re-filed on a loop.
      2. Absence was treated as loss even when the fleet itself had deliberately purged
         the content (a volume covers its chapters). To library.db the item is
         `superseded`; to reconcile it was missing-but-recoverable, so five covered One
         Piece chapters were re-fetched into the shelf only for the chapter reconciler to
         purge them again.

    Both loops burned the same constrained free-provider budget the Smurfs re-fetch was
    waiting on.

WHAT IS PROVED HERE

  1. Presence by exact inventory key, by MOUNT-only file, and by SSD-only file.
  2. Presence by content identity after a move: same basename AND same byte count under a
     different inventory key is still held; a same-name file with a different size is NOT
     (that is a different encode, not a move).
  3. library.db's deliberate-supersede verdict: a purged item closes the completion
     instead of re-queueing; an owned item and an unknown item both re-queue (fail open).
  4. The end-to-end decision, through `reconcile.reconcile`: moved -> untouched,
     superseded -> closed, genuinely missing -> re-queued.
  5. A read-only replay over the live `state/journal.jsonl` that prints how many
     completions the path-only check would have re-queued versus the new audit.

Uses fixture directories and a fixture library.db; the live library is only READ.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config                                                          # noqa: E402
import dbhook                                                          # noqa: E402
import journal                                                         # noqa: E402
import reconcile                                                       # noqa: E402
import repair_journal_paths as rjp                                     # noqa: E402

librarydb = dbhook.librarydb
failures: list[str] = []

# The live paths, so the read-only replay at the end can run against the real library
# after the fixture parts have pointed config at a temp dir.
LIVE = {"MEDIA_ROOT": config.MEDIA_ROOT, "MEDIAFS_MOUNT": config.MEDIAFS_MOUNT,
        "MEDIA_SYNCER_INVENTORY": config.MEDIA_SYNCER_INVENTORY,
        "JOURNAL_FILE": config.JOURNAL_FILE}


def check(label: str, cond: bool) -> None:
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


def note(label: str, detail) -> None:
    print(f"        {label}: {detail}")


def moved_record(root: Path, rel_old: str, rel_new: str, size: int) -> dict:
    return {"status": journal.COMPLETED,
            "applied": [{"dst": str(root / rel_old), "size": size}]}


def audit_with(record, keys, index, evidence=None):
    return reconcile.audit(record, keys, index,
                           evidence=evidence if evidence is not None
                           else (lambda rels: False))


# ===========================================================================
print("=== fixture library ===")
tmp = Path(tempfile.mkdtemp(prefix="reconcile-presence-")).resolve()
root = tmp / "media"                       # MEDIA_ROOT, the SSD tier
mount = tmp / "MediaLibrary"               # MEDIAFS_MOUNT, the FUSE view
inv_path = tmp / "remote_inventory.json"
root.mkdir()
mount.mkdir()

config.MEDIA_ROOT = root
config.MEDIAFS_MOUNT = mount
config.MEDIA_SYNCER_INVENTORY = inv_path


def write_inventory(mapping: dict) -> None:
    inv_path.write_text(json.dumps(
        {rel: ["automega000", "2026-09-20T00:00:00-05:00", size]
         for rel, size in mapping.items()}), encoding="utf-8")


# --- 1. the three exact views -------------------------------------------------
print("=== presence: inventory key, mount, SSD ===")
write_inventory({"Comics/Manga/Shelf/Series/Series c0001.cbz": 111})
keys, index = reconcile._remote_views()
check("inventory key is present",
      audit_with(moved_record(root, "Comics/Manga/Shelf/Series/Series c0001.cbz", "", 111),
                 keys, index) == "present")

rel_mount = "Comics/Manga/Shelf/Series/Series c0002.cbz"
(mount / rel_mount).parent.mkdir(parents=True, exist_ok=True)
(mount / rel_mount).write_bytes(b"x" * 4)
check("a MOUNT-only file is present (the SSD check used to say gone)",
      audit_with(moved_record(root, rel_mount, "", 4), keys, index) == "present")

rel_ssd = "Comics/Manga/Shelf/Series/Series c0003.cbz"
(root / rel_ssd).parent.mkdir(parents=True, exist_ok=True)
(root / rel_ssd).write_bytes(b"y" * 5)
check("an SSD-only file is present",
      audit_with(moved_record(root, rel_ssd, "", 5), keys, index) == "present")

# --- 2. moved content identity ------------------------------------------------
print("=== presence: a move keeps name + bytes ===")
write_inventory({"Comics/Manga/One Piece/One Piece/One Piece c1181.cbz": 6606152})
keys, index = reconcile._remote_views()
old = "Comics/Manga/One Piece/One Piece c1181.cbz"
new = "Comics/Manga/One Piece/One Piece/One Piece c1181.cbz"
rec = moved_record(root, old, "", 6606152)
check("a moved file (same name + bytes) is present",
      audit_with(rec, keys, index) == "present")
check("and resolves to its one new key",
      reconcile.resolve_moved(old, 6606152, keys, index) == new)

rec_wrong = moved_record(root, old, "", 6606999)
check("a same-name file with a different size is NOT a move",
      audit_with(rec_wrong, keys, index) == "missing")
check("and resolves to nothing",
      reconcile.resolve_moved(old, 6606999, keys, index) is None)

# Ambiguity must never be rewritten: two same-name+size keys -> present (content is
# held) but no single resolution.
write_inventory({"A/Series c0009.cbz": 999, "B/Series c0009.cbz": 999})
keys, index = reconcile._remote_views()
amb = moved_record(root, "C/Series c0009.cbz", "", 999)
check("two candidate keys still read as present", audit_with(amb, keys, index) == "present")
check("but do not resolve to one path",
      reconcile.resolve_moved("C/Series c0009.cbz", 999, keys, index) is None)

# --- 3. the deliberate-supersede ledger ---------------------------------------
print("=== library.db: a deliberate purge closes the completion ===")
conn = librarydb.connect(str(tmp / "library.db"))
sid = librarydb.add_series(conn, "Shelf Series", "manga", source="ingest")
librarydb.upsert_media(conn, sid, "chapter", None, 9)
purged_rel = "Comics/Manga/Shelf Series/Shelf Series c0009.cbz"
owned_rel = "Comics/Manga/Shelf Series/Shelf Series c0010.cbz"
librarydb.upsert_media(conn, sid, "chapter", None, 10)
check("an owned row is not purge evidence",
      dbhook.purged_evidence([purged_rel], conn=conn) is False)
dbhook._supersede_path(conn, purged_rel)
check("a superseded row is purge evidence",
      dbhook.purged_evidence([purged_rel], conn=conn) is True)
check("a still-owned row is not",
      dbhook.purged_evidence([owned_rel], conn=conn) is False)
check("an unknown series is not",
      dbhook.purged_evidence(["Comics/Manga/Nothing/Nothing c0001.cbz"], conn=conn) is False)
check("an empty set is not (never close on nothing)",
      dbhook.purged_evidence([], conn=conn) is False)
check("one owned path vetoes a mixed set",
      dbhook.purged_evidence([purged_rel, owned_rel], conn=conn) is False)

evidence = lambda rels: dbhook.purged_evidence(rels, conn=conn)          # noqa: E731
purged_rec = moved_record(root, purged_rel, "", 12345)
check("audit says superseded, not missing",
      audit_with(purged_rec, keys, index, evidence) == "superseded")
owned_rec = moved_record(root, owned_rel, "", 12346)
check("an owned absence is still missing",
      audit_with(owned_rec, keys, index, evidence) == "missing")

# --- 4. the decision, end to end ----------------------------------------------
print("=== reconcile.reconcile: moved kept, superseded closed, lost re-queued ===")
config.JOURNAL_FILE = tmp / "journal.jsonl"
config.TORRENTS_DIR = tmp / "Torrents"
config.TORRENTS_DIR.mkdir()
finished = tmp / "finished"
finished.mkdir()

write_inventory({"Comics/Manga/One Piece/One Piece/One Piece c1181.cbz": 6606152})

absent = tmp / "finished" / "absent.torrent"
absent.write_bytes(b"torrent")
purged_t = tmp / "finished" / "purged.torrent"
purged_t.write_bytes(b"torrent")
moved_t = tmp / "finished" / "moved.torrent"
moved_t.write_bytes(b"torrent")
records = {
    "moved": {"info_hash": "moved", "status": journal.COMPLETED, "name": "moved",
              "torrent_path": str(moved_t),
              "applied": [{"dst": str(root / old), "size": 6606152}]},
    "lost": {"info_hash": "lost", "status": journal.COMPLETED, "name": "lost",
             "torrent_path": str(absent),
             "applied": [{"dst": str(root / "Comics/Manga/Shelf Series/Series c0001.cbz"),
                          "size": 111}]},
    "purged": {"info_hash": "purged", "status": journal.COMPLETED, "name": "purged",
               "torrent_path": str(purged_t),
               "applied": [{"dst": str(root / purged_rel), "size": 12345}]},
}
reconcile._superseded_evidence = lambda rels: dbhook.purged_evidence(rels, conn=conn)
logs: list[str] = []
n = reconcile.reconcile(records, log_fn=logs.append)
check("exactly one re-queue", n == 1)
check("the genuinely missing record was re-queued",
      records["lost"]["status"] == journal.QUEUED
      and records["lost"]["torrent_path"] == str(config.TORRENTS_DIR / "absent.torrent"))
check("the moved record was left completed",
      records["moved"]["status"] == journal.COMPLETED
      and "reconcile_closed" not in records["moved"]
      and "reconcile_dead" not in records["moved"])
check("the moved record's .torrent was not touched",
      moved_t.exists() and not (config.TORRENTS_DIR / "moved.torrent").exists())
check("the deliberately purged record was closed",
      records["purged"].get("reconcile_closed") == "superseded"
      and records["purged"]["status"] == journal.COMPLETED)
check("the purge close was logged", any("deliberately superseded" in m for m in logs))

# --- 5. the repair tool: rewrite moves, gate the close ------------------------
print("=== repair tool: rewrite the moved, close only the empty-and-superseded ===")
write_inventory({"Comics/Manga/One Piece/One Piece/One Piece c1181.cbz": 6606152})
keys, index = reconcile._remote_views()
rec_moved = {"applied": [{"dst": str(root / old), "size": 6606152}],
             "plan": {"files": [{"dst_rel": old, "_dst_abs": str(root / old)}]},
             "chunk_filed": {"3": old}}
moves, exact, ambiguous, unresolved = rjp.classify(rec_moved, keys, index)
check("a uniquely moved file classifies as a rewrite", moves == {old: new})
check("and is not a close candidate",
      rjp.close_verdict(moves, exact, ambiguous, unresolved) is False)
rjp._rewrite(rec_moved, moves)
check("_rewrite moves applied, plan and chunk_filed",
      rec_moved["applied"][0]["dst"] == str(root / new)
      and rec_moved["plan"]["files"][0]["dst_rel"] == new
      and rec_moved["chunk_filed"]["3"] == new)

rel_keep = "Comics/Manga/Shelf Series/Shelf Series c0010.cbz"
(root / rel_keep).parent.mkdir(parents=True, exist_ok=True)
(root / rel_keep).write_bytes(b"x" * 3)
write_inventory({rel_keep: 3})
keys, index = reconcile._remote_views()
mixed = {"applied": [{"dst": str(root / rel_keep), "size": 3},
                     {"dst": str(root / purged_rel), "size": 12345}]}
moves, exact, ambiguous, unresolved = rjp.classify(mixed, keys, index)
check("a partly-present record is not a close candidate",
      exact == 1 and moves == {} and len(unresolved) == 1
      and rjp.close_verdict(moves, exact, ambiguous, unresolved) is False)
only_purged = {"applied": [{"dst": str(root / purged_rel), "size": 12345}]}
moves, exact, ambiguous, unresolved = rjp.classify(only_purged, keys, index)
check("a fully-missing superseded record is a close candidate",
      rjp.close_verdict(moves, exact, ambiguous, unresolved) is True)

# --- 6. replay over the live journal ------------------------------------------
print("=== replay: state/journal.jsonl through the path-only check and the new audit ===")
for name, value in LIVE.items():
    setattr(config, name, value)
reconcile._inv_cache.update({"stamp": None, "keys": None, "index": None})
if config.JOURNAL_FILE.exists():
    live_records = journal.load_records()
    keys, index = reconcile._remote_views()
    if keys is None:
        print("  (inventory unreadable; replay skipped)")
    else:
        live_conn = librarydb.connect()
        live_evidence = lambda rels: dbhook.purged_evidence(rels, conn=live_conn)  # noqa: E731
        audited = present = superseded = missing = legacy_would = 0
        for h, rec in live_records.items():
            if rec.get("status") != journal.COMPLETED:
                continue
            if rec.get("reconcile_dead") or rec.get("reconcile_closed"):
                continue
            entries = reconcile._applied_entries(rec)
            if not entries:
                continue
            audited += 1
            verdict = reconcile.audit(rec, keys, index, evidence=live_evidence)
            if verdict == "present":
                present += 1
            elif verdict == "superseded":
                superseded += 1
            elif verdict == "missing":
                missing += 1
            if not any(rel in keys or (config.MEDIA_ROOT / rel).exists()
                       for rel, _s in entries):
                legacy_would += 1
        live_conn.close()
        note("completed records audited", audited)
        note("present (inventory / mount / SSD / moved identity)", present)
        note("deliberately superseded -> closed, not re-queued", superseded)
        note("genuinely missing -> re-queue", missing)
        note("the path-only check would have re-queued", legacy_would)
        check("the new audit re-queues no more than the old one",
              missing <= legacy_would)
        check("the moved-identity witness finds real completions", present > 0)
else:
    print("  (no live journal; replay skipped)")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    sys.exit(1)
print("ALL RECONCILE PRESENCE CHECKS PASSED")
