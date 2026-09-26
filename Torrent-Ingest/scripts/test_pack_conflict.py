#!/usr/bin/env python3
"""The displaced-duplicate resolver: supersede a proven-redundant pack, or fail open.

WHAT THIS PINS. A pack that mis-files itself by a uniform same-season episode shift and
is then fully re-covered by another in-flight release is superseded automatically -- its
library footprint purged through the sanctioned path, its record retired REFUSED, its
payload kept -- so the blocked release can finish without a human making the call. Every
weaker shape must leave the existing park standing.

Fixtures are synthetic: no real show title, season number or episode number is written
into the check (the incident the feature exists for is described in the docstrings).

    python3 scripts/test_pack_conflict.py

No qBittorrent, no network, no journal/db writes: every collaborator is stubbed or
pointed into a temp directory.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import dbhook                                                        # noqa: E402
import identify                                                      # noqa: E402
import ingest                                                        # noqa: E402
import journal                                                       # noqa: E402
import library                                                       # noqa: E402
import pack_conflict                                                 # noqa: E402

failures: list[str] = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


GUIDE = ([{"season": 1, "number": n, "name": f"Story Number {n:02d}"} for n in range(1, 21)]
         + [{"season": 2, "number": 1, "name": "Second Run Opener"},
            {"season": 2, "number": 2, "name": "The Aftermath"},
            {"season": 2, "number": 3, "name": "Finale"}])

FOLDER = "Fixture Show (2019)"


def _rel(s, e, name):
    return f"Shows/{FOLDER}/Season {s:02d}/{name}"


def dot_name(n):
    return f"Fixture.Show.S01E{n:02d}.Story.Number.{n:02d}.1080p.WEB.mkv"


def swap_name(key, guide_n):
    """A dot name whose own number differs from the guide title it carries."""
    return f"Fixture.Show.S01E{key:02d}.Story.Number.{guide_n:02d}.1080p.WEB.mkv"


def bracket_name(n):
    return f"Fixture Show S01E{n:02d} (Story Number {n:02d}).mkv"


def make_record(h, names, filed, status="downloading", plan=None):
    """A record whose release list is `names` and whose chunk_filed maps index -> rel."""
    return {
        "info_hash": h,
        "name": f"fixture {h[:4]}",
        "status": status,
        "torrent_path": None,          # filled by the harness below
        "created_at": "2026-09-01T00:00:00+00:00",
        "chunk_filed": {str(i): rel for i, rel in enumerate(filed)},
        "plan": plan,
    }


def blame_shifts(n=16, shift=15, season=1):
    """`n` copies whose source keys are guide-confirmed and whose destinations are the
    source keys shifted by `shift` within the same season."""
    names, filed = [], []
    for i in range(1, n + 1):
        names.append(dot_name(i))
        filed.append(_rel(season, i + shift,
                          f"{FOLDER} - S{season:02d}E{i + shift:02d}.mkv"))
    return names, filed


class Harness:
    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.media = self.tmp / "media"
        self.mount = self.tmp / "mount"
        self.queue = self.tmp / "deletions.jsonl"
        self.torrents = {}
        self.saved = {k: getattr(config, k) for k in
                      ("MEDIA_ROOT", "MEDIAFS_MOUNT", "MEDIAFS_DELETIONS_QUEUE",
                       "FAILED_DIR", "FINISHED_DIR", "STATE_DIR")}
        config.MEDIA_ROOT = self.media
        config.MEDIAFS_MOUNT = self.mount
        config.MEDIAFS_DELETIONS_QUEUE = self.queue
        config.FAILED_DIR = self.tmp / "failed"
        config.FINISHED_DIR = self.tmp / "finished"
        config.STATE_DIR = self.tmp / "state"
        for d in (self.media, self.mount / "Shows", config.FAILED_DIR,
                  config.FINISHED_DIR, config.STATE_DIR):
            d.mkdir(parents=True, exist_ok=True)
        self.saved_qbt = pack_conflict.qbt.file_list_from_file
        self.saved_guide = identify._guide_for
        self.saved_tmdb = identify.folder_tmdb_id
        self.saved_purge = dbhook.record_purge
        self.saved_fail = ingest._fail
        self.saved_decision = journal.log_decision
        pack_conflict.qbt.file_list_from_file = self._files
        identify._guide_for = lambda _title, _tid=None: (GUIDE, "TEST")
        identify.folder_tmdb_id = lambda _folder: 1
        self.purged_rows = []
        dbhook.record_purge = lambda rels: (self.purged_rows.append(list(rels))
                                            or {"superseded": len(list(rels)),
                                                "paths": len(list(rels))})
        self.failed = []
        ingest._fail = lambda rec, reason, refused=False: self.failed.append(
            (rec["info_hash"], reason, refused))
        journal.log_decision = lambda *a, **k: None

    def _files(self, path):
        return [(n, 1) for n in self.torrents.get(str(path), [])]

    def add_record(self, h, names, filed, **kw):
        rec = make_record(h, names, filed, **kw)
        tp = config.STATE_DIR / f"{h}.torrent"
        tp.write_bytes(b"x")
        rec["torrent_path"] = str(tp)
        self.torrents[str(tp)] = names
        return rec

    def materialize(self, records):
        """Write every filed copy to the fake SSD so the supersede is observable."""
        for rec in records:
            for rel in (rec.get("chunk_filed") or {}).values():
                p = config.MEDIA_ROOT / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"copy")

    def restore(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        pack_conflict.qbt.file_list_from_file = self.saved_qbt
        identify._guide_for = self.saved_guide
        identify.folder_tmdb_id = self.saved_tmdb
        dbhook.record_purge = self.saved_purge
        ingest._fail = self.saved_fail
        journal.log_decision = self.saved_decision


def main() -> int:
    print("Part 1 -- the uniform-shift duplicate is superseded")
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp)
        try:
            b_names, b_filed = blame_shifts()
            blocker = h.add_record("b" * 40, b_names, b_filed)
            target = h.add_record("a" * 40, [bracket_name(n) for n in range(1, 17)],
                                  [_rel(1, 1, f"{FOLDER} - S01E01.mkv")])
            records = {blocker["info_hash"]: blocker, target["info_hash"]: target}
            h.materialize([blocker])
            plan = pack_conflict.plan_resolution(target, records)
            check("the blocker is found", plan.get("blocker_hash") == "b" * 40)
            check("every blamed copy would be purged", len(plan.get("copies") or []) == 16)
            summary = pack_conflict.apply_resolution(plan, client=None)
            check("the copies are gone from the SSD", all(
                not (config.MEDIA_ROOT / c["rel"]).exists() for c in plan["copies"]))
            check("the MEGA purge is queued once per copy",
                  len(config.MEDIAFS_DELETIONS_QUEUE.read_text().splitlines()) == 16)
            check("library.db rows are superseded",
                  h.purged_rows and len(h.purged_rows[0]) == 16)
            check("the blocker is retired REFUSED",
                  h.failed and h.failed[0][0] == "b" * 40 and h.failed[0][2] is True)
            check("its payload is never deleted by the resolver",
                  all(not (config.MEDIA_ROOT / c["rel"]).exists()
                      for c in plan["copies"]))
            check("the summary reports the purge", len(summary["purged"]) == 16)
        finally:
            h.restore()

    print("\nPart 2 -- every weaker shape leaves the park standing")
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp)
        try:
            def decide(blocker_names, blocker_filed, target_names, **kw):
                b = h.add_record("b" * 40, blocker_names, blocker_filed, **kw)
                t = h.add_record("a" * 40, target_names,
                                 [_rel(1, 1, f"{FOLDER} - S01E01.mkv")])
                return pack_conflict.plan_resolution(
                    t, {"b" * 40: b, "a" * 40: t})

            target_names = [bracket_name(n) for n in range(1, 17)]
            # A consistent pack at its own keys is ordinary work, not a mistake.
            names, filed = blame_shifts(shift=0)
            check("shift zero is not displaced",
                  "blocker" not in decide(names, filed, target_names))
            # Varying shifts are a release's own catalogue order (the reorder witness).
            names, filed = blame_shifts()
            filed[3] = _rel(1, 3, f"{FOLDER} - S01E03.mkv")     # shift 0 on one copy
            check("mixed shifts are not one uniform mistake",
                  "blocker" not in decide(names, filed, target_names))
            # A cross-season move is a deliberate library renumber.
            names, filed = blame_shifts(season=2)
            check("a cross-season move is not displaced",
                  "blocker" not in decide(names, filed, target_names))
            # Titles that match no guide episode prove nothing.
            unknown = [f"Fixture.Show.S01E{n:02d}.Unknown.Words.{n:02d}.mkv"
                       for n in range(1, 17)]
            check("unreadable titles fail open",
                  "blocker" not in decide(unknown, blame_shifts()[1], target_names))
            # The target must NAME every episode the blocker holds.
            check("a coverage gap blocks the purge",
                  "blocker" not in decide(*blame_shifts(),
                                          [bracket_name(n) for n in range(1, 16)]))
            # Two candidates are ambiguous.
            names, filed = blame_shifts()
            b1 = h.add_record("b" * 40, names, filed)
            b2 = h.add_record("c" * 40, names[:8], filed[:8])
            t = h.add_record("a" * 40, target_names,
                             [_rel(1, 1, f"{FOLDER} - S01E01.mkv")])
            plan = pack_conflict.plan_resolution(
                t, {"b" * 40: b1, "c" * 40: b2, "a" * 40: t})
            check("two displaced candidates are ambiguous",
                  "blocker" not in plan and "ambiguous" in plan.get("reason", ""))
            # An all-errored release list is no evidence either.
            h.torrents[str(Path(b1["torrent_path"]))] = []
            plan = pack_conflict.plan_resolution(t, {"b" * 40: b1, "a" * 40: t})
            check("an unreadable release list fails open", "blocker" not in plan)
        finally:
            h.restore()

    print("\nPart 2b -- a swapped pair rides the shift; an unanchored shift never does")
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp)
        try:
            def decide(blocker_names, blocker_filed, target_names):
                b = h.add_record("b" * 40, blocker_names, blocker_filed)
                t = h.add_record("a" * 40, target_names,
                                 [_rel(1, 1, f"{FOLDER} - S01E01.mkv")])
                return pack_conflict.plan_resolution(
                    t, {"b" * 40: b, "a" * 40: t})

            target_names = [bracket_name(n) for n in range(1, 17)]
            # The release swaps its own E01/E02 titles; the shift is still one constant.
            names, filed = blame_shifts()
            names[0] = swap_name(1, 2)
            names[1] = swap_name(2, 1)
            plan = decide(names, filed, target_names)
            check("a title-swapped pair rides the uniform shift",
                  plan.get("blocker_hash") == "b" * 40
                  and len(plan.get("copies") or []) == 16)
            # Every key unconfirmed: one shift is not an anchor by itself.
            unconfirmed = [f"Fixture.Show.S01E{n:02d}.Unknown.Words.{n:02d}.mkv"
                           for n in range(1, 17)]
            check("an unanchored shift fails open",
                  "blocker" not in decide(unconfirmed, filed, target_names))
        finally:
            h.restore()

    print("\nPart 3 -- coverage counts a combined file's two titles")
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp)
        try:
            # A real release drops the guide's leading article (`Mystery` for `The
            # Mystery`); an exact comparison would read plainly-present content as absent.
            check("a dropped leading article still matches",
                  identify.match_guide_titles(["Aftermath"], GUIDE) == {(2, 2)})
            check("a bare one-word guide name matches only the whole title",
                  identify.match_guide_titles(["Finale"], GUIDE) == {(2, 3)}
                  and identify.match_guide_titles(["Finale Music"], GUIDE) == set())
            names, filed = blame_shifts()
            blocker = h.add_record("b" * 40, names, filed)
            combined = [f"Fixture.Show.S01E{n:02d}.Story.Number.{n:02d}.-."
                        f"Story.Number.{n + 1:02d}.1080p.WEB.mkv"
                        for n in range(1, 16, 2)]
            target = h.add_record("a" * 40, combined,
                                  [_rel(1, 1, f"{FOLDER} - S01E01.mkv")])
            plan = pack_conflict.plan_resolution(
                target, {"b" * 40: blocker, "a" * 40: target})
            check("a combined-name target covers both episodes of each file",
                  plan.get("blocker_hash") == "b" * 40
                  and len(plan.get("content_slots") or []) == 16)
        finally:
            h.restore()

    print("\nPart 4 -- a queued-for-purge copy no longer blocks its replacement")
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(tmp)
        try:
            existing_rel = _rel(1, 16, f"{FOLDER} - S01E16.mkv")
            existing = config.MEDIAFS_MOUNT / existing_rel
            existing.parent.mkdir(parents=True, exist_ok=True)
            existing.write_bytes(b"old")
            src = config.STATE_DIR / dot_name(16)
            src.write_bytes(b"new")
            planned = {"src": str(src),
                       "dst_rel": _rel(1, 16, f"{FOLDER} - S01E16.Story.Number.16.mkv"),
                       "season": 1, "episode": 16}
            kept, dropped = library._collapse_existing_episode_collisions([dict(planned)])
            check("without the queue the misplaced copy still parks the plan",
                  len(kept) == 0 and len(dropped) == 1)
            config.MEDIAFS_DELETIONS_QUEUE.write_text(
                '{"path": "%s"}\n' % existing_rel, encoding="utf-8")
            kept, dropped = library._collapse_existing_episode_collisions([dict(planned)])
            check("with the queue the superseded copy is gone to the guard",
                  len(kept) == 1 and len(dropped) == 0)
        finally:
            h.restore()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
