"""Durable state for the YouTube ingest: what we have seen, and what each playlist
became.

Two files, both under `state/` (gitignored, and mirrored to MEGA by Torrent-Ingest's
nightly `backup_metadata.py` along with the rest of that repo's `state/` tree -- so
this ledger survives losing the machine, exactly like the locked `.nfo` do).

`seen.json` -- video_id -> record
    The dedupe authority, keyed by YOUTUBE VIDEO ID rather than by playlist position.
    That key is the whole point: the SAME video routinely appears in several of your
    playlists, and it must be downloaded and filed ONCE. It also means a video is
    never re-fetched because a playlist was reordered, renamed, or unsaved and
    re-saved, and that an item you removed from a playlist is not re-downloaded on the
    next cycle. yt-dlp's own `--download-archive` is deliberately NOT used: it records
    a download as done the moment the bytes land, before the file is placed in the
    library, so a failed identify/apply would be permanently marked complete.

    Records carry a `state`:
      placed      -- in the library, at `dst_rel`
      soundtrack  -- filed as an audio track at `dst`
      skipped     -- deliberately not ingested, with `reason` (too big, junk, private)
      failed      -- attempted and failed `attempts` times, with `reason`. Retried
                     until FAIL_MAX_ATTEMPTS, then left alone so one poisoned video
                     cannot stall every cycle forever.

`shows.json` -- playlist_id -> the show that playlist became
    Pins the show folder the FIRST time a playlist places an episode, so a playlist
    renamed on YouTube keeps filing into the folder its existing episodes are in
    rather than forking a second show. Also the source of the next episode number.
"""
from __future__ import annotations

import json
from pathlib import Path

import ytconfig

# A video that keeps failing is retried this many times across cycles, then parked.
# Parked videos are reported by `--status` and released with `--retry-failed`.
FAIL_MAX_ATTEMPTS = 4


def _load(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _save(path: Path, obj) -> None:
    """Write via a temp file + rename, so a crash mid-write cannot truncate the
    ledger into unreadable JSON (which would re-download the whole library)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


class Ledger:
    def __init__(self):
        self.seen: dict = _load(ytconfig.LEDGER_FILE, {})
        self.shows: dict = _load(ytconfig.SHOWS_FILE, {})

    # --- persistence ---------------------------------------------------------

    def save(self) -> None:
        _save(ytconfig.LEDGER_FILE, self.seen)
        _save(ytconfig.SHOWS_FILE, self.shows)

    # --- video records -------------------------------------------------------

    def is_done(self, video_id: str) -> bool:
        """True if this video needs no further attention: it is in the library, is
        filed as a soundtrack, was deliberately skipped, or has exhausted its
        retries."""
        rec = self.seen.get(video_id)
        if not rec:
            return False
        state = rec.get("state")
        if state in ("placed", "soundtrack", "skipped"):
            return True
        return state == "failed" and int(rec.get("attempts", 0)) >= FAIL_MAX_ATTEMPTS

    def record(self, video_id: str, state: str, **fields) -> None:
        rec = self.seen.setdefault(video_id, {})
        rec.update(fields)
        rec["state"] = state
        rec["ts"] = ytconfig.log_stamp()
        if state == "failed":
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
        else:
            rec.pop("attempts", None)

    def mark_failed_batch(self, video_ids, reason: str) -> None:
        for vid in video_ids:
            self.record(vid, "failed", reason=reason[:400])

    def retry_failed(self) -> int:
        """Release every parked failure so the next cycle tries again. Returns the
        count released."""
        n = 0
        for vid, rec in list(self.seen.items()):
            if rec.get("state") == "failed":
                del self.seen[vid]
                n += 1
        return n

    def counts(self) -> dict:
        out = {}
        for rec in self.seen.values():
            out[rec.get("state", "?")] = out.get(rec.get("state", "?"), 0) + 1
        return out

    def parked(self) -> list:
        return [(vid, rec) for vid, rec in self.seen.items()
                if rec.get("state") == "failed"
                and int(rec.get("attempts", 0)) >= FAIL_MAX_ATTEMPTS]

    # --- playlist -> show registry -------------------------------------------

    def show_for(self, playlist_id: str) -> dict | None:
        return self.shows.get(playlist_id)

    def pin_show(self, playlist_id: str, playlist_title: str,
                 show_rel: str, year: int, title: str = "") -> dict:
        """Pin the show a playlist files into, the first time it places an episode.

        `title` is the show's DISPLAY title, kept alongside the folder because they can
        legitimately differ: a colon is illegal in a filename, so `Ancient Rome:
        Explained` lives in a folder called `Ancient Rome - Explained (2021)`. Storing
        both means a later cycle tells the identify run the real title rather than making
        it read the punctuation back out of a folder name.
        """
        rec = self.shows.get(playlist_id)
        if rec is None:
            rec = {"playlist_title": playlist_title, "show_rel": show_rel,
                   "title": title or Path(show_rel).name.rsplit(" (", 1)[0],
                   "year": year, "next_episode": 1, "pinned": ytconfig.log_stamp()}
            self.shows[playlist_id] = rec
        return rec

    def next_episode(self, playlist_id: str) -> int:
        """The next episode number to use in this playlist's show.

        Reconciled against what is actually ON DISK, not just the counter: the disk is
        ground truth, so a ledger restored from a backup (or a show whose folder was
        filled in by hand) continues after the real last episode instead of colliding
        with it. Numbering is monotonic append order -- NEVER the video's position in
        the playlist, because a video inserted at the top of a playlist would then
        renumber every episode below it and orphan the files already on disk.
        """
        rec = self.shows.get(playlist_id)
        if rec is None:
            return 1
        counter = int(rec.get("next_episode", 1))
        return max(counter, self.highest_on_disk(rec["show_rel"]) + 1)

    @staticmethod
    def highest_on_disk(show_rel: str) -> int:
        """Highest SEASON-`ytconfig.SEASON` episode number present in the show folder,
        or 0. Read through the mediafs mount when available so an EVICTED episode (bytes
        in the pool, not on the SSD) still counts -- reading the un-tiered local root
        would see a gap where an uploaded episode used to be and reuse its number."""
        import playlist as ti_playlist      # borrowed: it already resolves the mount
        root = getattr(ti_playlist, "LIBRARY_ROOT", ytconfig.MEDIA_ROOT)
        season_dir = Path(root) / show_rel / f"Season {ytconfig.SEASON:02d}"
        best = 0
        if not season_dir.is_dir():
            return 0
        for f in season_dir.iterdir():
            if f.suffix.lower() not in ytconfig.TI.VIDEO_EXTENSIONS:
                continue
            span = ti_playlist._parse_span(f.name)
            if span and span[0] == ytconfig.SEASON:
                best = max(best, span[2])
        return best

    def advance_episode(self, playlist_id: str, used_upto: int) -> None:
        rec = self.shows.get(playlist_id)
        if rec is not None:
            rec["next_episode"] = max(int(rec.get("next_episode", 1)), used_upto + 1)
