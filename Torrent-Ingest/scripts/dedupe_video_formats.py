#!/usr/bin/env python3
"""Resolve same-episode duplicates that differ only by container, MEGA-side.

THE PROBLEM
    The library dedupes by EXACT DESTINATION PATH, so `... - S02E01.mkv` and
    `... - S02E01.mp4` are two different paths and both are kept forever, neither ever
    seeing the other. `library_health.txt` reports 106 items needing review and the
    dominant class is exactly this -- KONOSUBA Season 2 holds a `.mkv` AND a `.mp4` of all
    ten episodes. The owner's instruction was direct: "In diagnosis.txt, instruct it to
    delete such KONOSUBA and ElfQuest repeats."

WHAT IT WILL AND WILL NOT DO
    It deletes a copy ONLY when all four hold, and it prints its reasoning for each:
      1. two files resolve to the same (show, season, episode);
      2. their formats differ, and one format outranks the other (FORMAT_RANK);
      3. the better-ranked file is also the LARGER one -- so format precedence and size
         agree and there is no judgement call. When they disagree it refuses and says so,
         because "prefer .mkv" is a container preference and "bigger is better" is a
         quality one, and a case where they conflict needs a human;
      4. the two `.nfo` sidecars carry the SAME episode title, proving the files really are
         the same episode rather than two things that happen to share a number.
    Anything failing any test is REPORTED, never deleted.

    It deliberately does NOT handle the Powerpuff Girls shape -- a combined
    `S01E01-E02 - Both Titles.mkv` overlapping the individual `S01E01.mkv`. That is a
    multi-episode file spanning its singles, which needs span logic, not extension
    precedence, and guessing there would delete real content.

WHY DELETION IS SAFE HERE WHEN IT IS NOT FOR COMICS
    §4.92 suspended the comic version of this rule because comic volume numbers in this
    library were being INVENTED, so two files sharing a name were often two different
    books. Episode numbers are not invented: they come from the release's own `SxxExx` and
    are corroborated by the `.nfo` title check above. The identity is READ, not assumed --
    which is the §4.49 lesson this whole class of failure keeps teaching.

USAGE
    python3 scripts/dedupe_video_formats.py --show "KONOSUBA ... (2016)" --season 2
    python3 scripts/dedupe_video_formats.py --show "..." --season 2 --apply
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                                          # noqa: E402

SYNCER = Path.home() / "Developer/Media-Fleet/Media-Syncer"
INVENTORY = SYNCER / "remote_inventory.json"
SYNC_STATE = SYNCER / "sync_state.json"
UPLOAD_LOG = Path.home() / "Library/Logs/MediaSync.err"
ALREADY_GONE = ("directory not found", "doesn't exist", "not found", "no such file",
                "object not found")

# Higher is better. `.mkv` outranks `.mp4` because it is the archival container here --
# it carries the subtitle and multi-audio tracks the fleet's dual-audio upgrades exist to
# preserve, which an `.mp4` re-mux routinely drops.
FORMAT_RANK = {".mkv": 3, ".mp4": 2, ".avi": 1}
_EP_RE = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})E(\d{1,3})", re.IGNORECASE)


def _rclone(args, timeout=180):
    p = subprocess.run([config.RCLONE_BIN, "--config", str(config.RCLONE_CONFIG), *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stderr or "") + (p.stdout or "")


def _upload_targets(prefix):
    out = defaultdict(set)
    rx = re.compile(r"Uploading '(.+)' to ([A-Za-z0-9_]+)")
    try:
        with UPLOAD_LOG.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if prefix in line:
                    m = rx.search(line)
                    if m:
                        out[m.group(1)].add(m.group(2))
    except OSError:
        pass
    return out


def _nfo_title(local_dir: Path, stem: str):
    p = local_dir / f"{stem}.nfo"
    try:
        t = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"<title>(.*?)</title>", t, re.S)
    return m.group(1).strip() if m else None


def analyse(show: str, season: int):
    inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
    prefix = f"Shows/{show}/Season {season:02d}/"
    local_dir = config.MEDIA_ROOT / prefix
    by_ep = defaultdict(list)
    for key, meta in inv.items():
        if not key.startswith(prefix):
            continue
        name = key[len(prefix):]
        suffix = Path(name).suffix.lower()
        if suffix not in FORMAT_RANK:
            continue
        m = _EP_RE.search(name)
        if not m:
            continue
        size = meta[2] if isinstance(meta, list) and len(meta) > 2 else 0
        by_ep[(int(m.group(1)), int(m.group(2)))].append(
            {"key": key, "name": name, "stem": Path(name).stem,
             "ext": suffix, "remote": meta[0] if isinstance(meta, list) else None,
             "size": size})

    decisions = []
    for ep, files in sorted(by_ep.items()):
        if len(files) < 2:
            continue
        files.sort(key=lambda f: (FORMAT_RANK[f["ext"]], f["size"]), reverse=True)
        keep, drops = files[0], files[1:]
        for d in drops:
            why = []
            if FORMAT_RANK[keep["ext"]] <= FORMAT_RANK[d["ext"]]:
                why.append("no format precedence between them")
            if keep["size"] <= d["size"]:
                why.append(f"the better-format copy is SMALLER "
                           f"({keep['size']/1e6:.1f}MB vs {d['size']/1e6:.1f}MB)")
            tk, td = _nfo_title(local_dir, keep["stem"]), _nfo_title(local_dir, d["stem"])
            if not tk or not td:
                why.append("a .nfo title is missing, so identity is unproven")
            elif tk != td:
                why.append(f"the .nfo titles differ ({tk!r} vs {td!r})")
            decisions.append({"ep": ep, "keep": keep, "drop": d,
                              "refuse": "; ".join(why) or None, "title": tk})
    return decisions, prefix


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", required=True)
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--normalize-names", action="store_true",
                    help="also strip a trailing ' -' from surviving filenames")
    args = ap.parse_args()

    decisions, prefix = analyse(args.show, args.season)
    doable = [d for d in decisions if not d["refuse"]]
    refused = [d for d in decisions if d["refuse"]]
    print(f"duplicate pairs found: {len(decisions)}   deletable: {len(doable)}   "
          f"refused: {len(refused)}")
    for d in decisions:
        s, e = d["ep"]
        mark = "DELETE" if not d["refuse"] else "REFUSE"
        print(f"  {mark} S{s:02d}E{e:02d}  keep {d['keep']['ext']} "
              f"{d['keep']['size']/1e6:7.1f}MB   drop {d['drop']['ext']} "
              f"{d['drop']['size']/1e6:7.1f}MB   {d['title'] or ''}")
        if d["refuse"]:
            print(f"         reason: {d['refuse']}")
    if not args.apply or not doable:
        print("\n(dry run -- pass --apply)" if not args.apply else "\nnothing to do")
        return 0

    uploads = _upload_targets(prefix)
    inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
    freed = 0
    failures = []
    for d in doable:
        key = d["drop"]["key"]
        remotes = set(filter(None, [d["drop"]["remote"]])) | uploads.get(key, set())
        for r in sorted(remotes):
            rc, out = _rclone(["deletefile", f"{r}:{key}", "--timeout", "120s"])
            low = out.lower()
            if rc != 0 and not any(s in low for s in ALREADY_GONE):
                failures.append(f"{r}:{key} -> {out.strip()[:120]}")
        # Verify, because a delete on a dead session can exit 0 having done nothing.
        for r in sorted(remotes):
            rc, out = _rclone(["lsjson", f"{r}:{key}"], timeout=90)
            if rc == 0:
                failures.append(f"STILL PRESENT {r}:{key}")
        freed += d["drop"]["size"]
        # Local sidecars of the DROPPED copy only (the media itself is usually already
        # evicted to the pool). Matched by exact name, never `glob(stem + "*")`: the two
        # copies' stems are `... - S02E01` and `... - S02E01 -`, so the wildcard form also
        # matches the SURVIVOR's sidecars and deletes the metadata of the file being kept.
        # It did exactly that on the KONOSUBA run before this was tightened.
        ld = config.MEDIA_ROOT / prefix
        stem = d["drop"]["stem"]
        for suffix in (".nfo", "-thumb.jpg", "-thumb.png", "-fanart.jpg", ".srt"):
            sc = ld / f"{stem}{suffix}"
            if sc.is_file():
                sc.unlink()

    print(f"\nfailures: {len(failures)}")
    for f in failures[:20]:
        print(f"  ✗ {f}")
    if failures:
        print("NOT pruning state keys while deletions are unverified.")
        return 1
    for path in (INVENTORY, SYNC_STATE):
        data = json.loads(path.read_text(encoding="utf-8"))
        bak = path.with_suffix(path.suffix + ".bak-dedupe")
        if not bak.exists():
            bak.write_text(json.dumps(data), encoding="utf-8")
        n = 0
        for d in doable:
            if data.pop(d["drop"]["key"], None) is not None:
                n += 1
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
        print(f"  {path.name}: pruned {n} key(s)")
    print(f"\nreclaimed {freed/1e9:.2f} GB across the pool.")
    if args.normalize_names:
        _normalize_names(doable, prefix, uploads)
    return 0


_TRAILING_DASH_RE = re.compile(r"\s+-$")


def _normalize_names(doable, prefix, uploads):
    """Strip a trailing ' -' from a surviving filename's stem, MEGA-side.

    Deleting the duplicate and stopping would leave the library holding ONLY the
    badly-named copy -- `... - S02E01 -.mkv`, where the trailing space-hyphen is where an
    episode title would have gone -- which trades one complaint for another. The library's
    own layout contract is `<Title> (<year>) - SxxEyy.ext` with NO episode title, so the
    correct name is simply the stem without that dangling separator; it is also exactly the
    name the deleted copy already used, which is now free.
    """
    inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
    renamed, failures = 0, []
    for d in doable:
        keep = d["keep"]
        new_stem = _TRAILING_DASH_RE.sub("", keep["stem"])
        if new_stem == keep["stem"]:
            continue
        old_key = keep["key"]
        new_key = prefix + new_stem + keep["ext"]
        remotes = set(filter(None, [keep["remote"]])) | uploads.get(old_key, set())
        ok = True
        for r in sorted(remotes):
            rc, out = _rclone(["moveto", f"{r}:{old_key}", f"{r}:{new_key}",
                               "--timeout", "120s"])
            low = out.lower()
            if rc != 0 and not any(s in low for s in ALREADY_GONE):
                failures.append(f"{r}:{old_key} -> {out.strip()[:120]}")
                ok = False
        if ok:
            if old_key in inv:
                inv[new_key] = inv.pop(old_key)
            renamed += 1
    tmp = INVENTORY.with_suffix(".tmp")
    tmp.write_text(json.dumps(inv), encoding="utf-8")
    tmp.replace(INVENTORY)
    print(f"names normalized: {renamed}   failures: {len(failures)}")
    for f in failures[:10]:
        print(f"  ✗ {f}")


if __name__ == "__main__":
    raise SystemExit(main())
