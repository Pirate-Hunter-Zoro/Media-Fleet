#!/usr/bin/env python3
"""Move already-filed comics into the franchise layout `config.COMIC_FRANCHISES` now says.

WHAT THIS IS FOR
    Two structural changes to the franchise table need the existing library moved to match,
    because a table row only changes placement for NEW arrivals:

      * §4.86 -- a franchise's own main series now has its own sub-folder instead of
        sitting loose in the master root. The owner: "Battle Angel Alita raw volumes are in
        the same folder as the other Alita comics. The original Alita volumes should have
        their own subfolder inside like all the other Alita series do."
      * §4.87 -- the table grew from 5 franchises to 24, generated from the library's own
        folder layout plus AniList. Series that were flat top-level folders are now members.

    Leaving either half-done is worse than not doing it: new volumes would land in the new
    path while the old ones stayed put, splitting a series across two folders.

WHY IT IS NOT `mv`
    ~4390 of the library's comics live ONLY on the MEGA pool -- the SSD is a cache and cold
    files are evicted. A local-only move duplicates and resurrects: the new local path is
    "on no remote" so the syncer re-uploads it while write-once keeps the old remote copy,
    and the old path is suddenly missing locally so the download phase fetches it back.
    Every move has to land on the remote too. This follows Media-Syncer/README.md
    "Renaming or Restructuring a Title" exactly.

THE FOUR TRAPS IT HANDLES, EACH LEARNED THE HARD WAY
    1. MEGA allows two sibling directories with the SAME NAME and `rclone lsf` resolves the
       name to only one of them. Six parallel `moveto` calls into a destination that did
       not exist yet once created FOUR duplicate directories, and whatever landed in the
       shadowed node read as missing. So moves are GROUPED BY DESTINATION DIRECTORY and the
       groups run one at a time; only files sharing a destination move concurrently, after
       that directory exists. (Never `rclone dedupe` to repair it -- its file handling can
       delete, and this library is write-once.)
    2. `remote_inventory.json` is a SNAPSHOT, not live state, and it collapses each path to
       one residence. A copy orphaned at the old path by a past failed retry is invisible to
       it, and the next scan would re-download that orphan at the old path. So each file's
       remote set is the UNION of its inventory residence and every remote it was ever an
       upload target for in `~/Library/Logs/MediaSync.err`.
    3. A bare `Invalid arguments` from MEGA is almost always a dead cached session, not a
       real error. Those remotes get their `session_id`/`master_key` stripped and one retry.
    4. An exit code of 0 does not mean the move happened. Every move is verified by listing
       the destination, and residuals are reported rather than assumed away.

USAGE
    python3 scripts/migrate_comic_franchises.py                 # plan only, touches nothing
    python3 scripts/migrate_comic_franchises.py --apply         # do it
    python3 scripts/migrate_comic_franchises.py --verify-only   # re-check a finished run

    Stop `mediasync` first (it snapshots the remote index at cycle start), and let it stay
    down until the state keys are rewritten. `scripts/migrate_comics.sh` does that around
    this script and is the supported way to run it.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                          # noqa: E402
import library                                                         # noqa: E402
import rclone_conf                                                     # noqa: E402

SYNCER = Path.home() / "Developer/Media-Fleet/Media-Syncer"
INVENTORY = SYNCER / "remote_inventory.json"
SYNC_STATE = SYNCER / "sync_state.json"
UPLOAD_LOG = Path.home() / "Library/Logs/MediaSync.err"
RCLONE = config.RCLONE_BIN
RCLONE_CONF = config.RCLONE_CONFIG
PLAN_PATH = config.STATE_DIR / "comic_franchise_migration.jsonl"

# Six is the documented ceiling: stripping tokens and reconnecting at higher concurrency
# triggers a MEGA login storm that itself surfaces as `Invalid arguments`.
WORKERS = 5
COMIC_EXT = (".cbz", ".cbr", ".cb7", ".cbt", ".pdf", ".epub")
ALREADY_GONE = ("directory not found", "doesn't exist", "not found", "no such file")


# Files whose IDENTITY is not established, which must therefore not be moved.
#
# Moving a file into `<Franchise>/<Series>/` asserts which series it is. For most files that
# assertion is safe -- the name says so, and the ingest log records what it was filed from.
# A file whose name is the ONLY evidence for its contents does not get moved on that basis:
# §4.92 established that the `ElfQuest vNN` numbering in this library was being INVENTED per
# pass, so the number in such a name is not evidence of anything.
#
# The list is currently EMPTY, and that is the intended end state rather than an oversight.
# It held the two `ElfQuest vNN.pdf` files until 2026-09-01, when they were opened instead
# of guessed about: both are issues of the RUSSIAN Machaon serialisation
# (`Роман в рисунках`, `САГА О ЛЕСНЫХ ВСАДНИКАХ`), not English volumes of The Original
# Quest, and `v05` is a truncated download -- exactly 25 MiB, no xref table, no `%%EOF`, and
# no PDF reader will open it. They are filed under
# `ElfQuest/The Original Quest (Russian, Machaon)/` under names that say so.
#
# That is the §4.49 lesson paying off in the other direction: the costliest failures come
# from acting on contents that were assumed rather than read -- and the way out of a
# refusal is to READ the file, not to loosen the rule.
UNVERIFIED_IDENTITY = set()


# --- discovery ---------------------------------------------------------------

def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _upload_targets() -> dict[str, set[str]]:
    """path -> every remote it was ever uploaded to, from the launchd stderr log.

    The greedy capture is anchored on the `' to <remote>` suffix on purpose: titles contain
    apostrophes (*World's*, *God's*) and a naive `'([^']+)'` truncates at the first one and
    silently drops exactly those titles.
    """
    out: dict[str, set[str]] = defaultdict(set)
    rx = re.compile(r"Uploading '(.+)' to ([A-Za-z0-9_]+)")
    try:
        with UPLOAD_LOG.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "Uploading '" not in line:
                    continue
                m = rx.search(line)
                if m and m.group(1).startswith("Comics/"):
                    out[m.group(1)].add(m.group(2))
    except OSError:
        pass
    return out


def _destination(rel: str) -> str | None:
    """Where `rel` belongs under the current table, or None when it is already right."""
    p = Path(rel)
    parent, name = p.parent, p.name
    for fr in config.COMIC_FRANCHISES:
        root_rel = ("Manga/" if fr["kind"] == "manga" else "") + fr["name"]
        master = Path("Comics") / root_rel
        # Look a folder up by BOTH forms of a member entry. The VALUE is the short
        # sub-folder name a migrated series ends up under ("Flowers"); the KEY is the alias
        # that a not-yet-migrated FLAT folder is actually called ("Shaman King - Flowers",
        # which normalizes to "shaman king flowers"). Matching values alone silently missed
        # every flat folder still carrying its franchise prefix -- Boruto, the Durarara arcs,
        # Parasyte's Full Color Collection and ten already-tabled series had sat unmigrated
        # for exactly that reason, and the plan reported them as nothing to do.
        members = {library.normalize_folder_name(v): v
                   for v in (fr.get("members") or {}).values() if v}
        for k, v in (fr.get("members") or {}).items():
            if k and v:
                members.setdefault(library.normalize_folder_name(k), v)
        # (a) loose in the master root -> the main series' own sub-folder.
        if parent == master:
            sub = (fr.get("members") or {}).get(
                library.normalize_folder_name(fr["name"])) or fr["name"]
            # Prefer the member whose name the FILE carries, so a side series sitting loose
            # goes to its own folder rather than to the main run's.
            stem = library.normalize_folder_name(Path(name).stem)
            best = None
            for key, folder in members.items():
                if key and key in stem and (best is None or len(key) > len(best[0])):
                    best = (key, folder)
            if best:
                sub = best[1]
            return str(master / sub / name)
        # (b) a flat top-level folder that is now a member of this franchise.
        top = Path("Comics") / ("Manga" if fr["kind"] == "manga" else "")
        if parent.parent == top and parent != master:
            folder = members.get(library.normalize_folder_name(parent.name))
            if folder:
                return str(master / folder / name)
    return None


def _queued_for_deletion() -> set[str]:
    """Paths the reaper has been told to purge, from mediafs's deletion queue.

    A file that is queued must NOT be migrated. Moving it changes the path the reaper was
    given, so the purge then fails to find it and the pool copy is orphaned forever while the
    owner believes it was deleted. Both queue files are read because the reaper claims a batch
    by atomic rename, so `.processing` is live work too.
    """
    out: set[str] = set()
    for name in ("mediafs_deletions.jsonl", "mediafs_deletions.jsonl.processing"):
        try:
            for line in (SYNCER / name).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        out.add(json.loads(line)["path"])
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass
    return out


def build_plan() -> list[dict]:
    inv = _load_json(INVENTORY)
    uploads = _upload_targets()
    queued = _queued_for_deletion()

    paths: set[str] = {k for k in inv if k.startswith("Comics/")}
    for p in config.COMICS_ROOT.rglob("*"):
        if p.is_file() and p.suffix.lower() in COMIC_EXT:
            paths.add(str(p.relative_to(config.MEDIA_ROOT)))

    plan = []
    skipped = []
    deleting = 0
    for rel in sorted(paths):
        if rel in UNVERIFIED_IDENTITY:
            skipped.append(rel)
            continue
        # Never migrate a file the reaper has been told to purge. Moving it changes the path
        # the reaper holds, so the purge cannot find it and the pool copy is orphaned forever
        # while the owner believes it was deleted. This fires whenever a migration overlaps a
        # drain, which for a large owner-directed purge is most of a day.
        if rel in queued:
            deleting += 1
            continue
        dst = _destination(rel)
        if not dst or dst == rel:
            continue
        residence = inv.get(rel)
        remotes = set()
        if isinstance(residence, list) and residence:
            remotes.add(residence[0])
        remotes |= uploads.get(rel, set())
        plan.append({"src": rel, "dst": dst, "remotes": sorted(remotes)})
    for rel in skipped:
        print(f"  SKIPPED (identity not established, see UNVERIFIED_IDENTITY): {rel}")
    if deleting:
        print(f"  SKIPPED {deleting} file(s) already queued for deletion (a move would "
              f"orphan the pool copy)")
    return plan


# --- rclone ------------------------------------------------------------------

def _rclone(args: list[str], timeout: int = 180) -> tuple[int, str]:
    proc = subprocess.run([RCLONE, "--config", str(RCLONE_CONF), *args],
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stderr or "") + (proc.stdout or "")


def _strip_session(remote) -> None:
    """Drop a remote's cached MEGA session so the next call logs in fresh.

    Delegates to `rclone_conf.strip_session`, which holds the CROSS-PROCESS lock this used
    to lack. The old copy here took a `threading.Lock`, which serialized this tool's own
    worker pool and nothing else -- while mediasync's session purge and the account
    provisioner rewrite the very same file from other processes. That race was observed
    live (`didn't find section in config file ("automega113")`, 2026-08-31 22:18) and it is
    the worst kind available: losing a section loses an ACCOUNT, and with it the only copy
    of whatever single-residence files live on it.
    """
    rclone_conf.strip_session(config.RCLONE_CONFIG, remote)


def _moveto(remote: str, src: str, dst: str) -> tuple[bool, str]:
    """File-level server-side rename. True when the file is at `dst` afterwards.

    File-level `moveto`, never directory-level `move`: on a remote that does not hold the
    source directory MEGA answers `Server side directory move failed: Invalid arguments`
    rather than a clean "not found", which aborts the batch; and directory move REFUSES to
    move a same-named child into its own parent, which is exactly the shape of the §4.86
    main-series move.
    """
    for attempt in (1, 2):
        rc, out = _rclone(["moveto", f"{remote}:{src}", f"{remote}:{dst}", "--timeout", "120s"])
        low = out.lower()
        if rc == 0:
            return True, "moved"
        if any(sig in low for sig in ALREADY_GONE):
            return True, "absent (nothing to move)"
        if "invalid arguments" in low or "couldn't login" in low:
            if attempt == 1:
                _strip_session(remote)
                _rclone(["about", f"{remote}:"], timeout=90)   # force a clean re-auth
                continue
        return False, out.strip().splitlines()[-1][:160] if out.strip() else f"rc={rc}"
    return False, "unreachable"


def _exists(remote: str, path: str) -> bool | None:
    rc, out = _rclone(["lsjson", f"{remote}:{path}"], timeout=90)
    low = out.lower()
    if rc == 0:
        return True
    if any(sig in low for sig in ALREADY_GONE):
        return False
    return None                                   # errored -> unverified, never "clean"


# --- run ---------------------------------------------------------------------

def apply_plan(plan: list[dict]) -> list[dict]:
    """Execute grouped by destination directory, groups serially, files concurrently."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in plan:
        groups[str(Path(row["dst"]).parent)].append(row)

    results = []
    for gi, (dst_dir, rows) in enumerate(sorted(groups.items()), 1):
        # The local folder has to exist before anything else: `library.resolve_comic_folder`
        # needs it, and the sidecar-free comic layout has no other creator.
        (config.MEDIA_ROOT / dst_dir).mkdir(parents=True, exist_ok=True)
        print(f"[{gi}/{len(groups)}] {dst_dir}  ({len(rows)} file(s))", flush=True)

        def one(row):
            outcome = {"src": row["src"], "dst": row["dst"], "moved": [], "failed": []}
            # A row with NO remotes moves nothing on the pool, and because the loop below
            # simply does not execute, the outcome carries no failures and the run reports
            # "verified clean" for work it never did. That happened: 295 files were reported
            # migrated in 3 seconds, the inventory was rewritten to the new layout so the
            # mount LOOKED reorganized, and the next Media-Syncer rescan read the pool's real
            # (unchanged) paths and reverted the whole thing. A move with nowhere to move is
            # a failure, not a no-op.
            if not row.get("remotes"):
                outcome["failed"].append("NO REMOTE KNOWN for this file -- refusing to "
                                         "record a move that cannot happen on the pool")
                return outcome
            for remote in row["remotes"]:
                ok, detail = _moveto(remote, row["src"], row["dst"])
                (outcome["moved"] if ok else outcome["failed"]).append(f"{remote}: {detail}")
            local_src = config.MEDIA_ROOT / row["src"]
            if local_src.is_file():
                local_dst = config.MEDIA_ROOT / row["dst"]
                if not local_dst.exists():
                    local_src.rename(local_dst)
                    outcome["local"] = "moved"
            return outcome

        # Concurrency WITHIN a destination directory only. Two groups never run at once,
        # because that is what created four same-named sibling directories last time.
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for r in ex.map(one, rows):
                results.append(r)
                with PLAN_PATH.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(r) + "\n")
    return results


def verify(plan: list[dict]) -> tuple[int, int, list[str]]:
    """Assert every file is at its NEW path and nothing remains at the old one."""
    problems, ok = [], 0
    def check(row):
        bad = []
        for remote in row["remotes"]:
            if _exists(remote, row["src"]) is True:
                bad.append(f"RESIDUAL {remote}:{row['src']}")
        return row, bad
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for row, bad in ex.map(check, plan):
            if bad:
                problems.extend(bad)
            else:
                ok += 1
    return ok, len(plan), problems


def rewrite_state(plan: list[dict]) -> None:
    """Rewrite the moved keys in BOTH state files.

    The inventory is rebuilt on the next scan regardless, but leaving stale keys means the
    syncer spends the interim re-uploading the new path and re-downloading the old one.
    """
    mapping = {row["src"]: row["dst"] for row in plan}
    for path in (INVENTORY, SYNC_STATE):
        data = _load_json(path)
        if not data:
            continue
        backup = path.with_suffix(path.suffix + ".bak-franchise")
        if not backup.exists():
            backup.write_text(json.dumps(data), encoding="utf-8")
        changed = 0
        for old, new in mapping.items():
            if old in data:
                data[new] = data.pop(old)
                changed += 1
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
        print(f"  {path.name}: rewrote {changed} key(s) (backup: {backup.name})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    plan = build_plan()
    by_dst = defaultdict(int)
    for row in plan:
        by_dst[str(Path(row["dst"]).parent)] += 1
    no_remote = [r for r in plan if not r["remotes"]]
    print(f"files to move : {len(plan)}")
    print(f"destinations  : {len(by_dst)}")
    print(f"local-only    : {len(no_remote)} (no remote copy known; a plain local move)")
    for d, n in sorted(by_dst.items()):
        print(f"  {n:4}  {d}")

    if args.verify_only:
        ok, total, problems = verify(plan)
        print(f"\nverified clean: {ok}/{total}")
        for p in problems[:40]:
            print(f"  ✗ {p}")
        return 1 if problems else 0
    if not args.apply:
        print("\n(dry run -- pass --apply to execute)")
        return 0

    print(f"\napplying; per-file outcomes appended to {PLAN_PATH}")
    t0 = time.time()
    apply_plan(plan)
    print(f"moves done in {time.time() - t0:.0f}s; verifying...")
    ok, total, problems = verify(plan)
    print(f"verified clean: {ok}/{total}")
    for p in problems[:40]:
        print(f"  ✗ {p}")
    if problems:
        print("\nNOT rewriting state keys while residuals remain. Re-run --apply; it is "
              "idempotent (an absent source counts as already moved).")
        return 1
    rewrite_state(plan)
    print("\nmigration complete. Restart mediasync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
