"""Title-Scout: find one specific title anywhere, and start its download.

Drop a single title into `find.txt` (in the iCloud Torrents folder) and the daemon:

  1. asks the free-model chain what the request means (canonical title, author, media kind, and the
     search queries to run),
  2. searches nyaa / 1337x / The Pirate Bay / the Internet Archive for it,
  3. asks the free-model chain to confirm which candidate is genuinely the SAME work -- not a
     same-name different thing -- and
  4. starts the download: a torrent staged on the local disk (~/Downloads/.title-scout)
     and moved into Torrents/Scouted once complete, or a `.pdf`/`.epub`/... fetched
     directly into Torrents/Scouted.

It is deliberately NOT part of the fleet's media pipeline. It never reads the library,
never writes `.torrent` files for Torrent-Ingest, and never places anything into ~/Media.
A found-and-downloaded title is purged from `find.txt`; a transient failure (the free-model chain
down, qBittorrent down) keeps the title and retries with backoff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

import ai
import config
import sources


def log(msg: str) -> None:
    line = f"[{config.log_stamp()}] {msg}"
    print(line, flush=True)
    try:
        with config.LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --- state -------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _chosen_dict(chosen) -> dict:
    """A serialisable summary of a chosen candidate (a sources.Result, or a dict)."""
    if isinstance(chosen, dict):
        return chosen
    return {
        "title": getattr(chosen, "title", ""),
        "source": getattr(chosen, "source", ""),
        "format": _fmt_of(chosen),
        "size": _human_size(getattr(chosen, "size_bytes", None)),
    }


def record(target: dict, chosen, status: str, reason: str = "") -> None:
    """Append one outcome to state/found.json (the human-readable history)."""
    rec = {
        "ts": config.log_stamp_iso(),
        "title": target.get("title"),
        "author": target.get("author") or "",
        "kind": target.get("kind"),
        "status": status,
        "reason": reason,
    }
    if chosen is not None:
        rec["chosen"] = _chosen_dict(chosen)
    found = load_json(config.FOUND_FILE, [])
    found = found if isinstance(found, list) else []
    found.append(rec)
    save_json(config.FOUND_FILE, found)


def mark_seen(key: str) -> None:
    seen = load_json(config.SEEN_FILE, {})
    seen = seen if isinstance(seen, dict) else {}
    seen[key] = {"ts": config.log_stamp_iso()}
    save_json(config.SEEN_FILE, seen)


# --- retry backoff (transient failures keep the inbox) -----------------------

def _text_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def retry_ok(text: str) -> bool:
    data = load_json(config.STATE_DIR / ".retry.json", {})
    rec = data.get(_text_key(text)) if isinstance(data, dict) else None
    return not rec or time.time() >= rec.get("until", 0)


def bump_backoff(text: str) -> None:
    f = config.STATE_DIR / ".retry.json"
    data = load_json(f, {})
    data = data if isinstance(data, dict) else {}
    key = _text_key(text)
    rec = data.get(key, {"fails": 0, "until": 0})
    rec["fails"] = rec.get("fails", 0) + 1
    delay = min(3600, 300 * (2 ** (rec["fails"] - 1)))
    rec["until"] = time.time() + delay
    data[key] = rec
    save_json(f, data)


# --- qBittorrent Web API (stdlib urllib; no qbittorrentapi dep) ---------------

def _multipart(fields: list[tuple[str, str]],
               files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----titlescout-" + uuid.uuid4().hex
    buf = bytearray()

    def add(s) -> None:
        buf.extend(s if isinstance(s, bytes) else s.encode("utf-8"))

    for name, value in fields:
        add(f"--{boundary}\r\n")
        add(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        add(value)
        add("\r\n")
    for name, filename, ctype, data in files:
        add(f"--{boundary}\r\n")
        add(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n')
        add(f"Content-Type: {ctype}\r\n\r\n")
        buf.extend(data)
        add("\r\n")
    add(f"--{boundary}--\r\n")
    return bytes(buf), boundary


def _qbt_post(endpoint: str, fields: list[tuple[str, str]],
              files: list[tuple[str, str, str, bytes]] | None = None) -> str:
    body, boundary = _multipart(fields, files or [])
    req = urllib.request.Request(
        config.QBT_BASE + endpoint, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def _launch_app() -> None:
    subprocess.run(["/usr/bin/open", "-b", config.QBT_BUNDLE_ID],
                   check=False, capture_output=True)


def qbt_available() -> bool:
    try:
        with urllib.request.urlopen(f"{config.QBT_BASE}/app/version", timeout=5) as resp:  # noqa: S310
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def qbt_ensure_category() -> None:
    try:
        _qbt_post("/torrents/createCategory",
                  [("category", config.QBT_CATEGORY), ("savePath", str(config.TORRENT_STAGING_DIR))])
    except Exception:  # noqa: BLE001 -- category is a nicety, not load-bearing
        pass


def _qbt_savepath_fields() -> list[tuple[str, str]]:
    return [("savepath", str(config.TORRENT_STAGING_DIR)),
            ("category", config.QBT_CATEGORY),
            ("autoTMM", "false")]


def qbt_add_torrent(data: bytes, stopped: bool = True) -> str:
    fields = _qbt_savepath_fields() + ([("stopped", "true")] if stopped else [])
    return _qbt_post("/torrents/add", fields,
                     [("torrents", "download.torrent", "application/x-bittorrent", data)])


def qbt_add_magnet(magnet: str, stopped: bool = True) -> str:
    fields = [("urls", magnet)] + _qbt_savepath_fields() + ([("stopped", "true")] if stopped else [])
    return _qbt_post("/torrents/add", fields)


def qbt_exempt(infohash: str) -> None:
    """Pin this torrent's share limits to 'no limit' so qBittorrent's global
    auto-remove-on-completion (`max_seeding_time = 0`) does not reap it before we can
    move its finished files into Scouted."""
    _qbt_post("/torrents/setShareLimits",
              [("hashes", infohash), ("ratioLimit", "-1"),
               ("seedingTimeLimit", "-1"), ("inactiveSeedingTimeLimit", "-1")])


def qbt_start(infohash: str) -> None:
    _qbt_post("/torrents/start", [("hashes", infohash)])


def qbt_delete(infohash: str) -> None:
    _qbt_post("/torrents/delete", [("hashes", infohash), ("deleteFiles", "false")])


def qbt_info() -> list[dict]:
    with urllib.request.urlopen(  # noqa: S310
            f"{config.QBT_BASE}/torrents/info?category={config.QBT_CATEGORY}",
            timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    return data if isinstance(data, list) else []


def qbt_ready() -> bool:
    if qbt_available():
        qbt_ensure_category()
        return True
    log("  qBittorrent WebUI down; launching app ...")
    _launch_app()
    for _ in range(15):
        time.sleep(2)
        if qbt_available():
            qbt_ensure_category()
            return True
    log("  ! qBittorrent WebUI unreachable")
    return False


# --- helpers -----------------------------------------------------------------

def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")[:120] or "download"


def _unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(1, 1000):
        cand = dest.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    return dest


def _human_size(n: int | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return "?"


_FMT_EXTS = ("pdf", "epub", "mobi", "azw3", "djvu", "cbz", "cbr",
             "mp3", "m4b", "flac", "ogg", "aac", "mp4", "mkv", "avi", "m4v")
_FMT_RE = re.compile(r"\b(" + "|".join(map(re.escape, _FMT_EXTS)) + r")\b")

_FORMAT_FAMILY = {
    "audio": {"mp3", "m4b", "flac", "ogg", "aac", "m4a"},
    "video": {"mp4", "mkv", "avi", "m4v", "webm", "ts"},
}


def _fmt_of(r) -> str | None:
    """The file format a result carries, read from its metadata or from its title."""
    if getattr(r, "fmt", None):
        return r.fmt
    m = _FMT_RE.search((getattr(r, "title", "") or "").lower())
    return m.group(1) if m else None


def _fmt_bonus(pref: str, fmt: str | None) -> int:
    """Bonus for a candidate whose format matches the request's preferred format."""
    if not pref or pref == "any" or not fmt:
        return 0
    if fmt == pref:
        return 50_000
    if fmt in _FORMAT_FAMILY.get(pref, ()):
        return 50_000
    return 0


# --- the find -----------------------------------------------------------------

def download(r: sources.Result) -> bool:
    """Start the download for a chosen result. Returns True on success."""
    if r.kind == "direct":
        name = r.filename or f"{sanitize(r.title)}.{r.fmt or 'bin'}"
        dest = _unique_path(config.DOWNLOADS_DIR / name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        log(f"  direct download -> {dest}")
        if r.source == "libgen":
            n = sources.libgen_download(r, dest)
        elif r.source == "annas":
            n = sources.annas_download(r, dest)
        else:
            n = sources.download_direct(r.direct_url, dest)
        log(f"  + saved {dest.name} ({_human_size(n)})")
        return n > 0

    if not qbt_ready():
        return False

    try:
        infohash: str | None
        if r.source == "nyaa" and r.torrent_url:
            data = sources.download_torrent_bytes(r.torrent_url)
            infohash = sources.infohash_of(data)
            qbt_add_torrent(data, stopped=bool(infohash))
        elif r.source == "1337x" and r.torrent_url:
            sources.fetch_1337x_page(r)
            if not r.magnet:
                log("  ! 1337x page yielded no magnet")
                return False
            infohash = r.infohash or sources.magnet_infohash(r.magnet)
            qbt_add_magnet(r.magnet, stopped=bool(infohash))
        elif r.magnet:
            infohash = r.infohash or sources.magnet_infohash(r.magnet)
            qbt_add_magnet(r.magnet, stopped=bool(infohash))
        else:
            return False

        if infohash:
            # Exempt from the global auto-remove rule BEFORE starting, so a tiny book that
            # completes in seconds cannot be reaped before we get a chance to move it.
            qbt_exempt(infohash)
            qbt_start(infohash)
            log(f"  + qBittorrent downloading to {config.TORRENT_STAGING_DIR.name} (will move when done)")
        else:
            log("  ! no infohash to track; torrent added running but will not auto-move")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"  ! torrent add failed: {exc}")
        return False


def _is_complete(t: dict) -> bool:
    prog = t.get("progress")
    if isinstance(prog, (int, float)) and prog >= 1.0:
        return True
    return t.get("state") in {
        "uploading", "stalledUP", "forcedUP", "queuedUP", "pausedUP", "stoppedUP",
        "checkingUP",
    }


def reap_completed_torrents() -> int:
    """Move finished torrents from the local staging dir into Scouted, then drop them
    from qBittorrent. Returns the number moved this pass."""
    try:
        info = qbt_info()
    except Exception as exc:  # noqa: BLE001 -- qBittorrent may be down; try next sweep
        return 0

    moved = 0
    for t in info:
        if not _is_complete(t):
            continue
        ih = t.get("hash") or ""
        name = t.get("name") or "download"
        src = t.get("content_path") or ""
        src_path = Path(src) if src else None
        try:
            if src_path and src_path.exists():
                dest = _unique_path(config.DOWNLOADS_DIR / src_path.name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_path), str(dest))
                log(f"  + moved {name} -> {dest}")
                moved += 1
                _rmdir_if_empty(src_path.parent)
            else:
                log(f"  ~ {name}: complete but no files on disk; dropping torrent")
            qbt_delete(ih)
        except (OSError, ValueError) as exc:
            log(f"  ! move failed for {name}: {exc}")
    return moved


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _shortlist(results: list[sources.Result], target: dict) -> list[sources.Result]:
    """Dedupe, keep only downloadable candidates, score, and return the top 20."""
    uniq: dict[str, sources.Result] = {}
    for r in results:
        key = r.key()
        if key and key not in uniq:
            uniq[key] = r
    results = list(uniq.values())

    live = [r for r in results if r.kind == "direct" or r.seeders >= config.MIN_SEEDERS]
    t_norm = sources.normalize(target["title"])
    pref = target.get("format") or "any"
    for r in live:
        r_norm = sources.normalize(r.title)
        exact = bool(t_norm) and (r_norm == t_norm or t_norm in r_norm or r_norm in t_norm)
        r.score = ((1_000_000 if exact else 0) + _fmt_bonus(pref, _fmt_of(r))
                   + min(max(r.seeders, 0), 999))
    live.sort(key=lambda r: r.score, reverse=True)
    return live[:20]


def process_request(text: str, dry_run: bool) -> str:
    """Run one find for a single title. Returns a status string ("downloaded",
    "already_seen", "dry_run", "unmatched", "no_candidates", or "failed"). Raises on
    transient AI failures."""
    target = ai.interpret_title(text)
    log(f"Request: {target['title']!r}"
        + (f" by {target['author']}" if target["author"] else "")
        + f" (kind={target['kind']})")

    results: list[sources.Result] = []
    for q in target["queries"]:
        results.extend(sources.search_all(q, target["kind"]))
        time.sleep(config.REQUEST_DELAY_SEC)

    annas_ok = config.ANNAS_ENABLED and target["kind"] in config.LIBGEN_KINDS
    annas_done = False

    def hunt_annas() -> None:
        """Broader hunt via Anna's Archive (headless browser). Runs at most once."""
        nonlocal annas_done
        if annas_done or not annas_ok:
            return
        annas_done = True
        log("  broader hunt via Anna's Archive (headless browser) ...")
        for q in target["queries"]:
            results.extend(sources.search_annas_archive(q))
            time.sleep(config.REQUEST_DELAY_SEC)

    def candidates_of(sl: list[sources.Result]) -> list[dict]:
        return [{
            "title": r.title,
            "source": r.source,
            "seeders": r.seeders,
            "size": _human_size(r.size_bytes),
            "format": _fmt_of(r) or ("magnet" if r.kind == "magnet" else "torrent"),
        } for r in sl]

    shortlist = _shortlist(results, target)
    if not shortlist:
        hunt_annas()
        shortlist = _shortlist(results, target)

    if not shortlist:
        log("  no candidates found across sources.")
        record(target, None, "no_candidates", "nothing found")
        return "no_candidates"

    verdict = ai.verify_match(target, candidates_of(shortlist))
    log(f"  verify -> index={verdict['index']} confidence={verdict['confidence']}"
        f" ({verdict['reason']})")

    if verdict["index"] is None or verdict["confidence"] == "low":
        # The primary sources had candidates, but none was the right work; broaden.
        hunt_annas()
        shortlist = _shortlist(results, target)
        if not shortlist:
            record(target, None, "unmatched", verdict["reason"])
            return "unmatched"
        verdict = ai.verify_match(target, candidates_of(shortlist))
        log(f"  verify -> index={verdict['index']} confidence={verdict['confidence']}"
            f" ({verdict['reason']})")

    if verdict["index"] is None or verdict["confidence"] == "low":
        record(target, None, "unmatched", verdict["reason"])
        return "unmatched"

    idx = verdict["index"]
    if not (0 <= idx < len(shortlist)):
        record(target, None, "unmatched", "bad verify index")
        return "unmatched"

    chosen = shortlist[idx]
    log(f"  chosen: [{chosen.source}] {chosen.title}")

    if chosen.key() in load_json(config.SEEN_FILE, {}):
        record(target, chosen, "already_seen", "already downloaded before")
        return "already_seen"

    if dry_run:
        log(f"  ? (dry-run) would download: {chosen.title}")
        record(target, chosen, "dry_run", "dry-run, nothing downloaded")
        return "dry_run"

    ok = download(chosen)
    if ok:
        mark_seen(chosen.key())
        record(target, chosen, "downloaded")
        return "downloaded"
    record(target, chosen, "failed", "download did not start")
    return "failed"


# --- main --------------------------------------------------------------------

def read_find_lines() -> list[str]:
    if not config.FIND_TXT_FILE.exists():
        return []
    try:
        text = config.FIND_TXT_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def purge_titles(purged: list[str]) -> None:
    """Remove the first occurrence of each purged title from the current find.txt,
    without clobbering any titles the user added since the sweep began."""
    if not purged:
        return
    try:
        lines = config.FIND_TXT_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for pt in purged:
        for i, ln in enumerate(lines):
            if ln.strip() == pt:
                lines.pop(i)
                break
    config.FIND_TXT_FILE.write_text("\n".join(lines) + ("\n" if lines else ""),
                                    encoding="utf-8")


def clear_backoff(text: str) -> None:
    data = load_json(config.STATE_DIR / ".retry.json", {})
    if isinstance(data, dict) and _text_key(text) in data:
        data.pop(_text_key(text), None)
        save_json(config.STATE_DIR / ".retry.json", data)


def sweep(dry_run: bool) -> None:
    lines = read_find_lines()
    if not lines:
        return

    purged: list[str] = []
    remaining: list[str] = []
    for title in lines:
        if not retry_ok(title):
            remaining.append(title)
            continue
        log(f"find.txt entry: {title!r}")
        try:
            status = process_request(title, dry_run)
        except Exception as exc:  # noqa: BLE001 -- transient (the free-model chain/network); keep it
            log(f"  ! {title!r} failed ({exc}); keeping for retry")
            bump_backoff(title)
            remaining.append(title)
            continue
        if status in ("downloaded", "already_seen"):
            purged.append(title)
            clear_backoff(title)
        elif status == "dry_run":
            remaining.append(title)   # dry-run never modifies find.txt
        else:
            # unmatched / no_candidates / failed: not found, keep and retry later
            bump_backoff(title)
            remaining.append(title)

    if purged and not dry_run:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.FIND_TXT_BACKUP.write_text("\n".join(lines) + "\n", encoding="utf-8")
        purge_titles(purged)
        log(f"  purged {len(purged)} found title(s) from find.txt.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Title-Scout: find one title, download it.")
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--dry-run", action="store_true", help="report, do not download")
    ap.add_argument("--title", help="find this title directly (bypasses find.txt)")
    ap.add_argument("--no-annas", action="store_true",
                    help="skip the Anna's Archive headless-browser fallback")
    args = ap.parse_args()

    if args.no_annas:
        config.ANNAS_ENABLED = False

    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    config.TORRENT_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    log("Title-Scout starting.")

    if args.title:
        status = process_request(args.title.strip(), args.dry_run)
        log(f"done ({status}).")
        return

    while True:
        sweep(args.dry_run)
        reap_completed_torrents()
        if args.once:
            break
        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
