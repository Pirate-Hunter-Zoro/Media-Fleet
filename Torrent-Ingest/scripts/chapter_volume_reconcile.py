#!/usr/bin/env python3
"""Reconcile manga chapters against the volumes that cover them.

WHY THIS EXISTS
    A chapter is a first-class filing, and a volume is a better copy of the same content.
    Both tiers are shelved -- so the moment an owned volume proves a chapter is redundant,
    the chapter has to go, files and database rows, and it has to go through the SAME path
    `apply_plan` uses: `library.supersede_paths` unlinks locally and queues the remote
    purge; the reaper does the rest. The identify prompt already lists covered chapters in
    `supersedes` when a run happens to know the map, but that decision was made per run
    from an arbitrary library snapshot and never revisited. This tool is the deterministic
    half: it asks `manga_volume_map` for the bibliographic volume->chapter map, intersects
    it with what the shelf actually holds, and acts only on the intersection.

THE FIVE-PART GATE (all must hold before a chapter is purged)
    1. The covering volume's file is verified present -- the shelf enumeration that finds
       the volume is the same one that finds the chapter, so a volume absent from the pool
       and the mount covers nothing.
    2. The mapping is authoritative for THAT volume: MangaDex chapter sets directly, or an
       AI range cached at/above `manga_volume_map.AI_MIN_CONFIDENCE`. Unknown volumes
       cover nothing.
    3. The chapter number is in that volume's chapter SET.
    4. No keep rule applies (`state/manga_chapter_policy.json`).
    5. A colored volume never authors a chapter purge: colored editions are numbered
       differently from the provider's regular volumes, so the map does not describe them.
       It may supersede a same-numbered grey volume in the same series instead.
    Any failure keeps the chapter and reports why. There is no delete-on-a-guess path.

FAIL-OPEN, EVERYWHERE
    Mount and pool both unreadable -> no enumeration -> nothing is judged. A provider (or
    refresh) failure -> the cached entry is used if present, else the series is queued for
    a refresh and nothing is purged. `--apply` only ever acts on the intersection above; a
    dry run is the default.

TRIGGERS
    * `after_plan()` is called by the ingest paths once a plan's manga volume is verified.
      It uses the cache only; a miss/stale entry enqueues a refresh and never blocks the
      filing on the network.
    * The launchd one-shot (`--scheduled`) drains queued refresh requests, refreshes stale
      maps for series that hold both tiers, then reconciles. That is the intermittent scan.

STATE
    `state/manga_volume_map.json`         the cached maps (see manga_volume_map.py)
    `state/manga_chapter_policy.json`     per-series keep rules
    `state/manga_map_refresh_request.json` refresh queue written by the ingest hook

USAGE
    python3 scripts/chapter_volume_reconcile.py                      # dry run, whole shelf
    python3 scripts/chapter_volume_reconcile.py --series "Mashle" --apply
    python3 scripts/chapter_volume_reconcile.py --scheduled          # refresh + apply
    python3 scripts/chapter_volume_reconcile.py --refresh-stale --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config                                                          # noqa: E402
import dbhook                                                          # noqa: E402
import journal                                                         # noqa: E402
import library                                                         # noqa: E402
import manga_volume_map as mvm                                        # noqa: E402

MANGA_PREFIX = "Comics/Manga/"
POLICY_PATH = config.STATE_DIR / "manga_chapter_policy.json"
REFRESH_PATH = config.STATE_DIR / "manga_map_refresh_request.json"
DEFAULT_POLICY = "keep_volumes"
POLICIES = ("keep_volumes", "keep_chapters", "keep_all")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm(name: str) -> str:
    return library.normalize_folder_name(name)


# --- keep rules --------------------------------------------------------------

def load_policy() -> dict:
    try:
        data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    series = data.get("series")
    return series if isinstance(series, dict) else {}


def policy_for(series: str) -> str:
    row = load_policy().get(_norm(series)) or {}
    mode = str(row.get("mode") or DEFAULT_POLICY)
    return mode if mode in POLICIES else DEFAULT_POLICY


# --- refresh queue -----------------------------------------------------------

def load_refresh_requests() -> dict:
    try:
        data = json.loads(REFRESH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def enqueue_refresh(series: str, reason: str = "") -> None:
    """Remember a series whose map we do not have (yet). Never raises."""
    try:
        reqs = load_refresh_requests()
        reqs[_norm(series)] = {"name": series, "at": _now(), "reason": reason}
        REFRESH_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = REFRESH_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(reqs, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(REFRESH_PATH)
    except OSError:
        pass


def drain_refresh_requests() -> dict:
    reqs = load_refresh_requests()
    try:
        REFRESH_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return reqs


# --- shelf enumeration -------------------------------------------------------

def _series_root() -> Path | None:
    mount = config.MEDIAFS_MOUNT / "Comics" / "Manga"
    if mount.is_dir():
        return mount
    local = config.COMICS_ROOT / "Manga"
    if local.is_dir():
        return local
    return None


def _kind(rel: str, name: str):
    """`("volume"|"chapter", number, colored)` for a manga filename, or None."""
    colored = bool(dbhook._COLOR.search(rel))
    m = dbhook._VOL.search(name)
    if m:
        return "volume", int(m.group(1)), colored
    m = dbhook._CH.search(name) or dbhook._C_BARE.search(name) or dbhook._HASH.search(name)
    if m:
        return "chapter", int(m.group(1)), colored
    return None


def series_label_for_rel(rel_dir: str) -> str:
    """The provider-search name for a series folder: its whole chain under Comics/Manga.

    NOT the leaf folder. Measured on the live shelf: the leaf `Restoration` resolved to an
    unrelated manga named "Restoration" with a v01 that contains chapter 1 -- exactly the
    shape that would have purged `Rurouni Kenshin - Restoration c0001.cbz` on the wrong
    evidence. The chain ("Rurouni Kenshin Restoration") is what names the actual series,
    and it is the same longest-join `dbhook._comic_candidates` already uses.
    """
    parts = str(rel_dir).split("/")
    return " ".join(p for p in parts[2:] if p) or (parts[-1] if parts else "")


def owned_manga(series: str | None = None, series_dir: str | None = None) -> dict:
    """`{series_label: {library_rel: (mtype, number, colored)}}` from pool + mount.

    The series label is the file's parent folder (manga shelves as
    `Comics/Manga/[<franchise>/]<Series>/<file>`). `series_dir` restricts the walk to one
    folder -- the post-plan hook passes the exact folder a volume just landed in, so the
    filing cycle never re-walks the shelf. Both sources unreadable -> `{}`, which means
    "nothing can be judged", never "nothing is owned".
    """
    files: dict = {}

    def _add(rel: str, label: str) -> None:
        name = rel.rsplit("/", 1)[-1]
        if not name.lower().endswith(tuple(config.COMIC_EXTENSIONS)):
            return
        kind = _kind(rel, name)
        if not kind:
            return
        files.setdefault(label, {})[rel] = kind

    if series_dir:
        root = config.MEDIA_ROOT / series_dir
        label = series_label_for_rel(series_dir)
        if root.is_dir():
            for p in root.rglob("*"):
                if p.is_file():
                    try:
                        _add(str(p.relative_to(config.MEDIA_ROOT)), label)
                    except ValueError:
                        continue

        return files

    # Inventory first: it sees the pool even when a file has been evicted. The mount scan
    # then adds anything the pool has not seen yet (an upload still in flight).
    try:
        inv = json.loads(config.MEDIA_SYNCER_INVENTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        inv = {}
    if isinstance(inv, dict):
        for rel in inv:
            rel = str(rel)
            if rel.startswith(MANGA_PREFIX):
                parts = rel.split("/")
                if len(parts) >= 4:
                    label = series_label_for_rel("/".join(parts[:-1]))
                    if not series or _norm(label) == _norm(series):
                        _add(rel, label)
    root = _series_root()
    if root is not None:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = str(p.relative_to(config.MEDIAFS_MOUNT)) \
                    if str(p).startswith(str(config.MEDIAFS_MOUNT)) \
                    else str(p.relative_to(config.MEDIA_ROOT))
            except ValueError:
                continue
            if not rel.startswith(MANGA_PREFIX):
                continue
            parts = rel.split("/")
            if len(parts) < 4:
                continue
            label = series_label_for_rel("/".join(parts[:-1]))
            if not series or _norm(label) == _norm(series):
                _add(rel, label)
    return files


# --- decisions (pure; the tests exercise this directly) ----------------------

def plan_decisions(series: str, owned: dict, entry: dict | None,
                   policy: str, log=None) -> tuple[list, list]:
    """What would be purged, and what is kept with a reason. Pure and deterministic.

    `owned` is `{rel: (mtype, number, colored)}` from `owned_manga`; every rel in it is
    present (that is what enumeration means). Returns `(purges, keeps)` where `keeps` is a
    list of `(rel, reason)`.
    """
    purges, keeps = [], []

    def keep(rel, reason):
        keeps.append((rel, reason))

    if policy == "keep_all":
        for rel in sorted(owned):
            keep(rel, "keep rule: keep_all")
        return purges, keeps

    volumes = {}     # volume number -> [(rel, colored)]
    chapters = {}    # chapter number -> [rel]
    for rel, (mtype, number, colored) in sorted(owned.items()):
        if mtype == "volume":
            volumes.setdefault(number, []).append((rel, colored))
        elif mtype == "chapter":
            chapters.setdefault(number, []).append(rel)

    # 5. A colored volume supersedes a same-numbered grey volume in the same series.
    if policy != "keep_all":
        for number, rows in sorted(volumes.items()):
            colored = [(r, c) for r, c in rows if c]
            grey = [(r, c) for r, c in rows if not c]
            if colored and grey:
                for rel, _c in grey:
                    purges.append(rel)
                    if log:
                        log(f"{series}: purging grey v{number:02d} ({rel}); colored copy held")

    for number, rels in sorted(chapters.items()):
        if policy == "keep_chapters":
            for rel in rels:
                keep(rel, "keep rule: keep_chapters")
            continue
        cover = None
        unknown_volumes = []
        for vnum, rows in sorted(volumes.items()):
            if len(rows) != 1 or rows[0][1]:
                # A colored or duplicated volume number is not a provider-numbered
                # volume; it covers nothing (colored editions number differently).
                unknown_volumes.append(vnum)
                continue
            if not mvm.volume_allowed(entry, vnum):
                unknown_volumes.append(vnum)
                continue
            chset = mvm.known_volume(entry, vnum) or []
            if number in chset:
                cover = vnum
                break
        for rel in rels:
            if cover is not None:
                purges.append(rel)
                if log:
                    log(f"{series}: purging chapter c{number:04d} ({rel}); "
                        f"covered by v{cover:02d}")
            elif unknown_volumes:
                keep(rel, f"volume map unknown for v{unknown_volumes}; keep")
            else:
                keep(rel, "not covered by any owned volume")
    return sorted(set(purges)), keeps


# --- driver ------------------------------------------------------------------

def reconcile(series: str | None = None, series_dir: str | None = None,
              apply: bool = False, allow_ai: bool = False, log_fn=print) -> dict:
    """Judge every manga series that holds both tiers. Dry run unless `apply`."""
    owned_all = owned_manga(series=series, series_dir=series_dir)
    out = {"series": 0, "purged": 0, "kept": 0, "no_map": 0, "actions": []}
    for label, files in sorted(owned_all.items()):
        kinds = {k for _r, (k, _n, _c) in files.items()}
        if series is None and not {"volume", "chapter"} <= kinds:
            # Reconciliation is only defined where both tiers exist; a volume-only or
            # chapter-only series has nothing to compare and must not be refresh-queued
            # on every sweep (that would put the whole shelf on the network).
            continue
        norm = _norm(label)
        policy = policy_for(label)
        entry = mvm.get(label, allow_network=False, allow_ai=allow_ai)
        if entry is None:
            enqueue_refresh(label, "reconcile: no cached map")
            out["no_map"] += 1
            log_fn(f"{label}: no cached volume map; refresh queued, nothing purged")
            continue
        out["series"] += 1
        purges, keeps = plan_decisions(label, files, entry, policy, log=log_fn)
        out["kept"] += len(keeps)
        if not purges:
            log_fn(f"{label}: {len(keeps)} chapter(s) kept, nothing to purge")
            continue
        out["purged"] += len(purges)
        out["actions"].extend(purges)
        if apply:
            library.supersede_paths(purges)
            try:
                dbhook.record_purge(purges)
            except Exception as exc:                                       # noqa: BLE001
                log_fn(f"{label}: library.db supersede failed: {exc}")
            journal.log_decision("", label,
                                 f"chapter reconcile purged {len(purges)} chapter(s) "
                                 f"covered by cached volume map: " + ", ".join(purges))
        else:
            log_fn(f"{label}: would purge {len(purges)} chapter(s) (dry run)")
    return out


def refresh_stale(allow_ai: bool = True, only=None, log_fn=print) -> dict:
    """Refresh maps for series that hold both a volume and chapters (intermittent scan).

    `only`, when given, is a set of normalized series keys to refresh regardless of tiers
    (the ingest hook queued them because a volume just landed). Otherwise only stale or
    missing entries on two-tier series are fetched, so the steady-state cost is zero.
    """
    owned_all = owned_manga()
    out = {"refreshed": 0, "failed": 0}
    for label, files in sorted(owned_all.items()):
        kinds = {k for _r, (k, _n, _c) in files.items()}
        if only is None:
            if not {"volume", "chapter"} <= kinds:
                continue
        elif _norm(label) not in only:
            continue
        entry = mvm.get(label, allow_network=False)
        if entry is not None and not mvm.is_stale(entry):
            continue
        needed = sorted(n for _r, (k, n, _c) in files.items() if k == "volume")
        fresh = mvm.refresh(label, allow_ai=allow_ai, needed=needed)
        if fresh and (fresh.get("volumes") or fresh.get("ai_volumes")):
            out["refreshed"] += 1
            log_fn(f"{label}: map refreshed ({fresh.get('source')}, "
                   f"{len(fresh.get('volumes') or {})} volume(s) mapped + "
                   f"{len(fresh.get('ai_volumes') or {})} AI-filled)")
        else:
            out["failed"] += 1
            log_fn(f"{label}: map refresh failed or provider silent; keeps unchanged")
    return out


def after_plan(plan: dict, log_fn=print) -> None:
    """Ingest hook: a verified plan filed a manga volume -> reconcile that one series.

    Cache only by design. A missing map queues a refresh and returns; the filing cycle is
    never blocked on MangaDex. Never raises into the caller.
    """
    try:
        targets = {}
        for f in (plan or {}).get("files") or []:
            rel = str(f.get("dst_rel") or "")
            if not rel.startswith(MANGA_PREFIX):
                continue
            name = rel.rsplit("/", 1)[-1]
            if not dbhook._VOL.search(name):
                continue
            parent = rel.rsplit("/", 1)[0]
            targets[parent] = series_label_for_rel(parent)
        for parent, label in sorted(targets.items()):
            entry = mvm.get(label, allow_network=False)
            if entry is None:
                enqueue_refresh(label, "volume filed")
                log_fn(f"{label}: volume filed but no cached map; refresh queued")
                continue
            if mvm.is_stale(entry):
                enqueue_refresh(label, "volume filed on stale map")
            res = reconcile(series=label, series_dir=parent, apply=True, log_fn=log_fn)
            if res["purged"]:
                log_fn(f"{label}: {res['purged']} chapter(s) superseded by the new volume")
    except Exception as exc:                                               # noqa: BLE001
        log_fn(f"manga chapter reconcile hook skipped: {exc}")


# --- CLI ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile manga chapters against volumes.")
    ap.add_argument("--series", help="one series folder name")
    ap.add_argument("--apply", action="store_true", help="delete + queue purge (default dry)")
    ap.add_argument("--refresh-stale", action="store_true",
                    help="refresh stale maps for series holding both tiers first")
    ap.add_argument("--scheduled", action="store_true",
                    help="drain the refresh queue, refresh stale maps, then apply")
    ap.add_argument("--no-ai", action="store_true", help="never call the AI fallback")
    args = ap.parse_args()

    allow_ai = not args.no_ai
    if args.scheduled:
        reqs = drain_refresh_requests()
        queued = {_norm(row.get("name") or key) for key, row in reqs.items()}
        if queued:
            refresh_stale(allow_ai=allow_ai, only=queued)
        refresh_stale(allow_ai=allow_ai)
    elif args.refresh_stale:
        refresh_stale(allow_ai=allow_ai)

    res = reconcile(series=args.series, apply=args.apply or args.scheduled,
                    allow_ai=allow_ai)
    mode = "APPLIED" if (args.apply or args.scheduled) else "DRY RUN"
    print(f"\n{mode}: {res['series']} series judged, {res['purged']} chapter(s) "
          f"purged/would purge, {res['kept']} kept, {res['no_map']} without a map")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
