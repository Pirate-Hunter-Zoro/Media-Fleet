#!/usr/bin/env python3
"""Compute and apply an episode's TRUE slot, then re-file it through reviewed machinery.

WHY THIS IS A TOOL AND NOT A HAND-FIX (HANDOFF 15.1 / 15.5). Two library files sit at the
wrong slot and both were filed by the fleet, not by a human:

  * American Dad! (2005) `Season 04/... - S04E06 - Independent Movie ...` -- the release
    states S10E06 and TMDB agrees; a stale digest made a model remap it a season down.
  * Doctor Who (2005) `Season 00/... - S00E04 The End Of Time Part 1.mp4` -- the library's
    Season-00 shelf is its OWN era-ordered, locked scheme, and the file belongs at
    S00E23, after The Waters Of Mars; its sidecar's `<episode>16</episode>` is TMDB's
    number, not the library's.

The repair is the same shape in both cases: COMPUTE the correct slot from content
identity and the authority that owns the scheme (TMDB for numbered seasons, the locked
Season-00 shelf for specials), then hand `{old,new}` pairs to the `refile_season`
mapping mode, which moves the bytes on the mount/MEGA, deletes the stale sidecars,
rewrites the record and `library.db`, and updates the syncer's inventory. The tool then
writes the locked `.nfo` the destination slot needs.

USAGE
    python3 scripts/repair_slots.py --show "American Dad! (2005)" --file "<basename>" [--apply]
    python3 scripts/repair_slots.py --show "American Dad! (2005)" --file "<basename>" \
        --record 06dd53e1... [--apply]
    python3 scripts/repair_slots.py --show "Doctor Who (2005)" --specials [--apply]

Dry-run unless `--apply`. Nothing moves on a computation that cannot prove its answer.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import identify                                                      # noqa: E402
import library                                                       # noqa: E402
import tmdbguide                                                     # noqa: E402

_MARKER_TITLE_RE = re.compile(r"^\s*(?:Episode|Ep)\s*\d+\s*$", re.I)


def _video_slot(name):
    """`(season, episode)` the FILENAME states, or None."""
    m = re.search(r"[Ss](\d{1,3})[Ee](\d{1,4})", Path(name).name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _episode_title(video):
    """The episode title: a LOCKED `.nfo`'s, else the filename's (the release's own).

    An UNLOCKED sidecar is Jellyfin's scrape of the slot the file sits in, so on a
    MISSFILED file it names the WRONG episode -- American Dad's S04E06 content
    *Independent Movie* had been re-scraped to the slot's *The 42-Year-Old Virgin*.
    The filename carries the release's own title and is the content witness there. A
    locked sidecar is the fleet's authored metadata and wins.
    """
    text = library._read_text(library.episode_nfo_path(video)) or ""
    nfo_title = library._clean_episode_title(library._xml_tag(text, "title"))
    if library.nfo_is_locked(text) and nfo_title and not _MARKER_TITLE_RE.match(nfo_title):
        return nfo_title
    fn_title = library._clean_episode_title(
        library._filename_episode_title(Path(video).name))
    if fn_title:
        return fn_title
    return nfo_title if nfo_title and not _MARKER_TITLE_RE.match(nfo_title) else ""


def _episode_meta(video):
    text = library._read_text(library.episode_nfo_path(video)) or ""
    return {"title": _episode_title(video), "plot": library._xml_tag(text, "plot")}


def _nfo_slot(video):
    """`(season, episode)` the sidecar states, or None."""
    text = library._read_text(library.episode_nfo_path(video)) or ""
    s, e = library._xml_tag(text, "season"), library._xml_tag(text, "episode")
    try:
        return (int(str(s)), int(str(e)))
    except (TypeError, ValueError):
        return None


def _retarget(name, season, episode):
    """The file name at a new slot, its title and tag tail preserved."""
    out = re.sub(r"[Ss]\d{1,3}[Ee]\d{1,4}", f"S{int(season):02d}E{int(episode):02d}",
                 Path(name).name, count=1)
    return out


def compute_numbered_repairs(folder, tmdb_id, files=None):
    """`[repair, ...]` for named video files whose TMDB identity is at another slot.

    The release-vs-provider numbering is TMDB's (HANDOFF 15.1): the file's own episode
    TITLE is matched against TMDB's episode list, and when the match is at a different
    numbered slot the file is misfiled. Unmatched titles and Season-00 files are left
    alone (specials own their scheme; see `compute_specials_repairs`).
    """
    guide = tmdbguide.episode_names(tmdb_id)
    numbered = [e for e in (guide or []) if int(e.get("season") or 0) >= 1]
    if len(numbered) < 4:
        return []
    by_number = {(int(e["season"]), int(e["number"])): e for e in numbered}
    overviews = {}
    out = []
    for v in files or []:
        slot = _video_slot(v)
        if not slot or slot[0] == 0:
            continue
        title = _episode_title(v)
        if not title:
            continue
        claims = identify._match_titles(
            [(str(v), slot[0], slot[1], title)], numbered)
        target = claims.get(slot)
        if not target:
            continue
        target = (int(target[0]), int(target[1]))
        if target == slot:
            continue
        if target not in by_number:
            continue
        # The destination sidecar is authored from TMDB, NOT from the file's current
        # nfo: an unlocked nfo was scraped from the WRONG slot, so its title/plot name
        # the episode that is not there (American Dad's *Independent Movie* carried the
        # slot's *42-Year-Old Virgin* synopsis).
        if target[0] not in overviews:
            overviews[target[0]] = tmdbguide.episode_overviews(tmdb_id, target[0])
        old_rel = _rel(v)
        new_rel = str(Path(*Path(old_rel).parts[:2]) / f"Season {target[0]:02d}"
                      / _retarget(v.name, target[0], target[1]))
        out.append({
            "old": old_rel, "new": new_rel,
            "reason": f"the release names this {by_number[target]['name']!r}, which is "
                      f"TMDB S{target[0]:02d}E{target[1]:02d}, not "
                      f"S{slot[0]:02d}E{slot[1]:02d}",
            "season": target[0], "episode": target[1],
            "episode_title": by_number[target]["name"],
            "plot": overviews[target[0]].get(target, ""),
        })
    return out


def compute_numbered_nfo_fixes(folder, tmdb_id, files):
    """Sidecar rewrites for numbered files whose `.nfo` is missing or names a slot.

    Run only for files the caller NAMED (`--file`): a whole-show scan would author a
    locked nfo for every episode Jellyfin has not scraped yet. The title/plot come from
    TMDB for the slot the file is at (the provider Jellyfin scrapes), unless the existing
    sidecar is LOCKED -- then its authored title/plot are kept and only the slot tags are
    ensured.
    """
    guide = tmdbguide.episode_names(tmdb_id)
    numbered = [e for e in (guide or []) if int(e.get("season") or 0) >= 1]
    if len(numbered) < 4:
        return []
    by_slot = {(int(e["season"]), int(e["number"])): e for e in numbered}
    out = []
    for v in files or []:
        slot = _video_slot(v)
        if not slot or slot[0] == 0:
            continue
        nfo_path = library.episode_nfo_path(v)
        text = library._read_text(nfo_path) or ""
        if text and _nfo_slot(v) == slot:
            continue
        entry = by_slot.get(slot) or {}
        title = library._clean_episode_title(library._xml_tag(text, "title")) \
            if library.nfo_is_locked(text) else ""
        title = title or entry.get("name") or _episode_title(v)
        if library.nfo_is_locked(text):
            plot = library._xml_tag(text, "plot")
        else:
            plot = entry.get("overview") or tmdbguide.episode_overviews(
                tmdb_id, slot[0]).get(slot, "")
        out.append({"rel": _rel(v), "season": slot[0], "episode": slot[1],
                    "episode_title": title, "plot": plot})
    return out


def _scheme_identities(scheme, index):
    """`{file: (scheme_entry, tmdb_entry)}` for the library's specials, or {}.

    Matched ONE FILE AT A TIME, and keyed by FILE. Two shelf files can share a slot --
    Doctor Who's S00E04 collision is exactly that -- so any slot-keyed map silently
    drops one of them and the repair then computes the wrong answer for both. A TMDB
    number claimed by two different files is dropped from both (not an identity).
    """
    guide = [{"season": 0, "number": e["number"], "name": e["name"]}
             for e in index if e.get("name")]
    if not guide:
        return {}
    by_number = {e["number"]: e for e in index}
    out = {}
    for e in scheme:
        if not e["title"]:
            continue
        claims = identify._match_titles(
            [(e["file"], 0, e["slot"], e["title"])], guide)
        target = claims.get((0, e["slot"]))
        if not target:
            continue
        entry = by_number.get(int(target[1]))
        if entry is not None:
            out[e["file"]] = (e, entry)
    claimed = {}
    for file, (_e, entry) in out.items():
        claimed.setdefault(entry["number"], []).append(file)
    for number, files in claimed.items():
        if len(files) > 1:
            for file in files:
                out.pop(file, None)
    return out


def _air_date(entry):
    d = str(entry.get("air_date") or "")
    return d if re.match(r"\d{4}-\d{2}-\d{2}", d) else ""


def compute_specials_repairs(folder, tmdb_id, files=None):
    """`(repairs, nfo_fixes)` for a show's Season-00 shelf.

    THE AUTHORITY IS THE LIBRARY'S OWN LOCKED SCHEME, not TMDB's special number
    (HANDOFF 15.5). A shelf slot with ONE file is the library's own decision and is left
    exactly where it is, even when the provider numbers its episode differently. The
    fault this computes is the COLLISION: two files on one slot, where the shelf order
    says which one does not belong. The fixture is Doctor Who's S00E04 -- *Return Of
    Doctor Mysterio* (2016-12-25) sits correctly between *Husbands Of River Song*
    (E03, 2015) and *Twice Upon A Time* (E05, 2017), while *The End Of Time Part 1*
    (2009-12-25) is out of order there and is the intruder.

    The intruder moves to the shelf's own next free slot (`max(slot) + 1`), which is
    where the append-only scheme puts a new special -- S00E23, after The Waters Of Mars.
    A collision that cannot be resolved by air-date order (no identities, no neighbours,
    or BOTH files fit) is left untouched for review, never guessed.

    Independently, every identified file whose sidecar `<episode>` disagrees with its
    destination filename is queued as an `nfo_fix` (the provider's foreign number).
    """
    scheme = library.specials_scheme(folder)
    if not scheme:
        return [], []
    index = tmdbguide.specials_index(tmdb_id)
    if not index:
        return [], []
    identities = _scheme_identities(scheme, index)
    wanted = {str(v.name) for v in (files or [])}

    def _wanted(e):
        return not wanted or e["file"] in wanted

    by_slot = {}
    for e in scheme:
        by_slot.setdefault(e["slot"], []).append(e)
    identified_slots = sorted(
        slot for slot, entries in by_slot.items()
        if any(f["file"] in identities and _air_date(identities[f["file"]][1])
               for f in entries))
    max_slot = max(by_slot)
    moving = {}
    for slot, entries in sorted(by_slot.items()):
        if len(entries) < 2:
            continue
        identified = [e for e in entries
                      if e["file"] in identities and _air_date(identities[e["file"]][1])]
        if len(identified) < len(entries):
            continue                       # cannot prove an identity: leave it alone
        lower = [s for s in identified_slots if s < slot]
        upper = [s for s in identified_slots if s > slot]
        lo = max((identities[f["file"]][1] for f in by_slot[max(lower)]
                  if f["file"] in identities), key=lambda o: _air_date(o), default=None)             if lower else None
        hi = min((identities[f["file"]][1] for f in by_slot[min(upper)]
                  if f["file"] in identities), key=lambda o: _air_date(o), default=None)             if upper else None
        fits = []
        for e in entries:
            air = _air_date(identities[e["file"]][1])
            ok = ((lo is None or _air_date(lo) <= air)
                  and (hi is None or air <= _air_date(hi)))
            fits.append(ok)
        if sum(fits) != 1:
            continue                       # ambiguous order: leave it for review
        intruder = entries[fits.index(False)]
        moving[intruder["file"]] = (max_slot + 1, identities[intruder["file"]][1])
    if len({cand for cand, _i in moving.values()}) != len(moving):
        return [], []                      # two intruders want one slot: refuse

    repairs, nfo_fixes = [], []
    for e in scheme:
        if not _wanted(e):
            continue
        identity = identities.get(e["file"])
        if identity is None:
            continue
        meta = _episode_meta(folder / "Season 00" / e["file"])
        nfo_slot = _nfo_slot(folder / "Season 00" / e["file"])
        if e["file"] not in moving and nfo_slot and nfo_slot[0] == 0 \
                and nfo_slot[1] != e["slot"]:
            nfo_fixes.append({"rel": str(Path("Shows") / folder.name / "Season 00"
                                         / e["file"]),
                              "season": 0, "episode": e["slot"],
                              "episode_title": meta["title"], "plot": meta["plot"]})
        target = moving.get(e["file"])
        if target is None:
            continue
        cand, ident = target
        old_rel = str(Path("Shows") / folder.name / "Season 00" / e["file"])
        new_rel = str(Path("Shows") / folder.name / "Season 00"
                      / _retarget(e["file"], 0, cand))
        repairs.append({
            "old": old_rel, "new": new_rel,
            "reason": f"{meta['title']!r} ({ident['air_date']}) is out of order at "
                      f"S00E{e['slot']:02d}; the shelf's next free slot is "
                      f"S00E{cand:02d}",
            "season": 0, "episode": cand,
            "episode_title": meta["title"],
            "plot": meta["plot"] or ident.get("overview") or "",
        })
    return repairs, nfo_fixes


def _rel(p):
    for root in (config.MEDIAFS_MOUNT, config.MEDIA_ROOT):
        try:
            return str(Path(p).relative_to(root))
        except (ValueError, OSError):
            continue
    return ""


def _videos(folder, season=None):
    out = []
    for season_dir in sorted(Path(folder).glob("Season *")):
        if season is not None and season_dir.name != f"Season {season:02d}":
            continue
        try:
            for p in sorted(season_dir.iterdir()):
                if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS:
                    out.append(p)
        except OSError:
            continue
    return out


def _place_path(rel):
    """The writable on-disk path for a library-relative video's sidecar: the SSD.

    ALWAYS the SSD (`MEDIA_ROOT`). A sidecar written through the mount into a
    pool-only directory fails -- the virtual directory has no writable lower -- and
    mediafs passes the SSD sidecar through anyway, so the SSD is the one correct
    place whether the video is local or evicted. The writer makes the directory.
    """
    return config.MEDIA_ROOT / rel


def _write_locked(path, folder, season, episode, title, plot):
    if path is None or not str(title or "").strip():
        return 0
    library.write_locked_episode_nfo(
        path, folder.name,
        {"season": int(season), "episode": int(episode),
         "episode_title": title, "plot": plot or ""})
    return 1


def _write_dest_nfos(folder, entries):
    """Write a locked `.nfo` matching each destination slot (bytes already moved)."""
    written = 0
    for e in entries:
        written += _write_locked(_place_path(Path(e["new"])), folder, e["season"],
                                 e["episode"], e.get("episode_title"), e.get("plot"))
    return written


def _write_nfo_fixes(folder, fixes):
    written = 0
    for e in fixes:
        written += _write_locked(_place_path(Path(e["rel"])), folder, e["season"],
                                 e["episode"], e.get("episode_title"), e.get("plot"))
    return written


def _apply(repairs, record, show_title, mapping_path):
    import refile_season                                             # noqa: PLC0415
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(json.dumps(
        {"moves": [{"old": r["old"], "new": r["new"]} for r in repairs]}),
        encoding="utf-8")
    moves, rearm = refile_season.build_moves_from_mapping(mapping_path)
    args = argparse.Namespace(record=record, show_title=show_title, show=show_title)
    rc = refile_season._apply_mapping(moves, rearm, args)
    if rc:
        return rc
    return 0


def _resolve_show(name):
    folder = library.find_show_folder(name)
    if folder is None:
        raise SystemExit(f"REFUSED: no library folder for {name!r}")
    tmdb_id = None
    try:
        text = library._read_text(folder / "tvshow.nfo") or ""
        m = re.search(r"<tmdbid>\s*(\d+)\s*</tmdbid>", text)
        tmdb_id = int(m.group(1)) if m else None
    except (OSError, ValueError):
        tmdb_id = None
    if not tmdb_id:
        raise SystemExit(f"REFUSED: {folder.name} pins no tmdb id; cannot verify slots")
    return folder, tmdb_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", required=True)
    ap.add_argument("--file", action="append", default=[],
                    help="video basename(s) to repair (numbered mode; repeatable)")
    ap.add_argument("--specials", action="store_true",
                    help="compute against the library's locked Season-00 scheme")
    ap.add_argument("--record", help="info hash(es) whose journal record to rewrite")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    folder, tmdb_id = _resolve_show(args.show)
    selected = None
    if args.file:
        selected = []
        for name in args.file:
            hit = [v for v in _videos(folder) if v.name == name]
            if not hit:
                raise SystemExit(f"REFUSED: no video named {name!r} under {folder.name}")
            selected.extend(hit)

    repairs, nfo_fixes = [], []
    if args.specials:
        repairs, nfo_fixes = compute_specials_repairs(folder, tmdb_id, selected)
    else:
        videos = selected if selected is not None else _videos(folder)
        repairs = compute_numbered_repairs(folder, tmdb_id, videos)
        if selected:                       # explicit files only: never mass-author nfos
            nfo_fixes = compute_numbered_nfo_fixes(folder, tmdb_id, selected)

    print(f"{folder.name}: tmdb {tmdb_id}")
    if not repairs and not nfo_fixes:
        print("no repair computed (nothing to prove a different slot)")
        return 0
    for r in repairs:
        print(f"  MOVE {r['old']}\n    -> {r['new']}\n       ({r['reason']})")
    for f in nfo_fixes:
        print(f"  NFO  {f['rel']} -> S{f['season']:02d}E{f['episode']:02d} "
              f"({f['episode_title']!r})")
    if not args.apply:
        print("\n(dry run -- pass --apply)")
        return 0

    if repairs:
        mapping_path = config.STATE_DIR / f"repair_slots_{int(time.time())}.json"
        rc = _apply(repairs, args.record, folder.name, mapping_path)
        if rc:
            return rc
        written = _write_dest_nfos(folder, repairs)
        print(f"wrote {written} destination .nfo(s)")
    if nfo_fixes:
        written = _write_nfo_fixes(folder, nfo_fixes)
        print(f"rewrote {written} sidecar .nfo slot(s)")
    print("repair complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
