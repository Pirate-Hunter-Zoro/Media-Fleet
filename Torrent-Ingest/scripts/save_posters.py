#!/usr/bin/env python3
"""Backstop for shows missing an on-disk poster.

This pipeline never writes artwork -- posters are Jellyfin's job: it fetches them
from its image providers (TMDB/TheTVDB) and writes `folder.jpg` into the show
folder only when the library's "Save artwork into media folders" option is on and
a scan runs. That mostly works for freshly-ingested shows, but a *migrated* show
can end up with a stale `tvshow.nfo` that references a `folder.jpg` Jellyfin never
actually obtained -- the show renders with no cover forever (the bug that hit
"Fist of the North Star (1984)").

This is the poster analogue of the blank-episode audit/repair (§ Metadata
integrity): for every Series folder that has NO poster image on disk, it asks
Jellyfin for the best available remote poster (and backdrop), tells Jellyfin to
adopt it (so its DB + UI stay in sync), and writes the file to disk directly so
the nightly metadata backup can capture it -- independent of Jellyfin's own
save-to-folder setting. Idempotent: a folder that already has a poster is skipped,
so a re-run only fills what is still missing. Non-fatal per show.

Scope is Series only (folder-based, the reported failure class); loose movies
under Movies/ carry their own `<title>-poster.jpg` and are left alone.

Usage:
    python3 scripts/save_posters.py [--dry-run] [--show NAME] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from media_doctor import _generate_series_poster  # noqa: E402

# Any of these on disk means the show already has a poster; nothing to do.
POSTER_NAMES = (
    "folder.jpg", "folder.jpeg", "folder.png",
    "poster.jpg", "poster.png", "cover.jpg", "cover.png",
)


def _log(msg: str) -> None:
    print(f"[save_posters] {msg}", flush=True)


def _api(path: str, params: dict | None = None, method: str = "GET") -> bytes:
    """Call the Jellyfin API and return the raw response body."""
    url = config.JELLYFIN_URL.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("X-Emby-Token", config.JELLYFIN_API_KEY)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _has_poster(folder: Path) -> bool:
    if any((folder / n).exists() for n in POSTER_NAMES):
        return True
    return any(folder.glob("*-poster.*"))


def _series_path_index() -> dict[str, str]:
    """Map absolute series folder path -> Jellyfin item id."""
    body = _api("/Items", {
        "Recursive": "true",
        "IncludeItemTypes": "Series",
        "fields": "Path",
        "enableImages": "false",
    })
    items = json.loads(body).get("Items", [])
    index: dict[str, str] = {}
    for it in items:
        p = it.get("Path")
        if p:
            index[p.rstrip("/")] = it["Id"]
    return index


def _best_remote_url(item_id: str, image_type: str) -> str | None:
    """First (highest-ranked) remote image URL of the given type, or None."""
    try:
        body = _api(f"/Items/{item_id}/RemoteImages",
                    {"type": image_type, "limit": "1"})
    except Exception as e:  # noqa: BLE001
        _log(f"  RemoteImages {image_type} lookup failed: {e}")
        return None
    images = json.loads(body).get("Images", [])
    return images[0]["Url"] if images else None


def _adopt(item_id: str, image_type: str, url: str) -> None:
    """Have Jellyfin download+set the image so its DB/UI match disk."""
    try:
        _api(f"/Items/{item_id}/RemoteImages/Download",
             {"Type": image_type, "ImageUrl": url}, method="POST")
    except Exception as e:  # noqa: BLE001
        _log(f"  Jellyfin adopt {image_type} failed (non-fatal): {e}")


def _download(url: str, dest: Path) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
        dest.write_bytes(data)
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"  download to {dest.name} failed: {e}")
        return False


def _fix_show(folder: Path, item_id: str, dry_run: bool) -> bool:
    """Ensure folder.jpg (and backdrop.jpg) exist. Returns True if it acted."""
    poster_url = _best_remote_url(item_id, "Primary")
    if not poster_url:
        # No provider poster: the series has no identity to search against (a fan
        # re-cut / YouTube show) or the provider simply has no Primary. Derive a cover
        # from the show's own artwork instead, so it is never left with a blank tile.
        if dry_run:
            _log(f"  DRY-RUN {folder.name}: no remote poster; would generate one locally")
            return True
        if _generate_series_poster(folder):
            _log(f"  {folder.name}: generated folder.jpg from the show's own artwork")
            return True
        _log(f"  {folder.name}: no remote poster and no local artwork; skipping")
        return False
    if dry_run:
        _log(f"  DRY-RUN {folder.name}: would fetch poster {poster_url}")
        return True

    _adopt(item_id, "Primary", poster_url)
    acted = False
    if not _has_poster(folder) and _download(poster_url, folder / "folder.jpg"):
        _log(f"  {folder.name}: wrote folder.jpg")
        acted = True
    # Backdrop is a nice-to-have; only fill if entirely absent.
    if not (folder / "backdrop.jpg").exists():
        back_url = _best_remote_url(item_id, "Backdrop")
        if back_url:
            _adopt(item_id, "Backdrop", back_url)
            if _download(back_url, folder / "backdrop.jpg"):
                _log(f"  {folder.name}: wrote backdrop.jpg")
    return acted


def main() -> int:
    ap = argparse.ArgumentParser(description="Fill missing show posters from Jellyfin's providers.")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--show", help="limit to a single show folder name")
    ap.add_argument("--verbose", action="store_true", help="log shows that already have a poster")
    args = ap.parse_args()

    if not config.JELLYFIN_URL or not config.JELLYFIN_API_KEY:
        _log("JELLYFIN_URL / JELLYFIN_API_KEY not set; skipping poster backstop")
        return 0
    if not config.SHOWS_ROOT.exists():
        _log(f"{config.SHOWS_ROOT} not present; skipping")
        return 0

    try:
        index = _series_path_index()
    except Exception as e:  # noqa: BLE001
        _log(f"could not query Jellyfin series list: {e}")
        return 1

    missing = fixed = 0
    for folder in sorted(p for p in config.SHOWS_ROOT.iterdir() if p.is_dir()):
        if args.show and folder.name != args.show:
            continue
        if _has_poster(folder):
            if args.verbose:
                _log(f"  {folder.name}: has poster, ok")
            continue
        missing += 1
        item_id = index.get(str(folder).rstrip("/"))
        if not item_id:
            _log(f"  {folder.name}: no matching Jellyfin item (unscraped yet?); skipping")
            continue
        if _fix_show(folder, item_id, args.dry_run):
            fixed += 1

    _log(f"done: {missing} show(s) missing a poster, {fixed} filled"
         f"{' (dry-run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
