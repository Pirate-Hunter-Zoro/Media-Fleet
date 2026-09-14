#!/usr/bin/env python3
"""Regression test: direct ingest covers ALL media, and never deletes video on trust
(2026-09-13).

THE CASE. `~/Downloads/DirectIngest/` was the loose COMIC/novel inbox; video only ever
arrived through a torrent. The owner downloads raw files directly too, so the watch set
now includes `VIDEO_EXTENSIONS` and dropped DIRECTORIES. Two properties have to hold,
and both are cheap to get wrong:

  1. Discovery: a loose `.mkv`/`.mp4`, an archive, an e-book, a dot-named release file
     and a directory of media are all picked up; `.srt` (identityless alone), `.txt`,
     AppleDouble junk, `.DS_Store` and the control dirs are not.
  2. An empty plan must NOT delete video or a directory on a model's word. For an
     archive the historical verdict stands (a redundant single is deleted); for video
     the library must POSITIVELY prove the episodes/film are already shelved, or the
     drop parks in `.failed/`.

BOTH DIRECTIONS (§4.5):
  Part 1 -- `_find_media` picks up exactly the ingestible entries.
  Part 2 -- a video's subtitle siblings are attached deterministically to its plan.
  Part 3 -- a real `process()` run (fake identify, real validate/apply/verify) files a
            movie + subtitle and removes BOTH sources.
  Part 4 -- empty plans: proven already-present is deleted; unproven video / directory
            is parked intact; an archive keeps the delete verdict.
  Part 5 -- a directory drop lands as one run, deletes only planned sources, and parks
            unplanned leftovers in `.skipped/` rather than destroying them.

    python3 scripts/test_direct_ingest_media.py

Writes only inside a temp dir. Exit 0 means every check passed.
"""

import shutil
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                          # noqa: E402
import direct_ingest                                                   # noqa: E402
import ingest                                                          # noqa: E402
import library                                                         # noqa: E402
import reconcile                                                       # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def fake_identify(plan, raises=None):
    def _run(cid, content_path, log_fn=None, **kw):
        if raises:
            raise raises
        return deepcopy(plan), "test rationale"
    return _run


tmp = Path(tempfile.mkdtemp(prefix="direct-ingest-media-")).resolve()
watch = tmp / "DirectIngest"
media = tmp / "Media"
saved = (direct_ingest.WATCH_DIR, direct_ingest.FAILED_DIR, direct_ingest.SKIPPED_DIR,
         direct_ingest.LOG_FILE, direct_ingest.identify.run_identify,
         config.MEDIA_ROOT, config.MOVIES_ROOT, config.SHOWS_ROOT, config.NOVELS_ROOT,
         library.dbhook.record_plan, ingest._jellyfin_rescan, reconcile._remote_keys,
         direct_ingest.STABLE_SEC)
rescans: list[int] = []


def _reset_watch():
    """Fresh watch folder AND fresh fake library, so each part's proof test sees only
    the evidence that part creates."""
    shutil.rmtree(watch, ignore_errors=True)
    shutil.rmtree(media, ignore_errors=True)
    shutil.rmtree(tmp / "Novels", ignore_errors=True)
    watch.mkdir(parents=True)
    (watch / ".failed").mkdir()
    (watch / ".skipped").mkdir()


try:
    config.MEDIA_ROOT = media
    config.MOVIES_ROOT = media / "Movies"
    config.SHOWS_ROOT = media / "Shows"
    config.NOVELS_ROOT = tmp / "Novels"
    library.dbhook.record_plan = lambda plan: None
    ingest._jellyfin_rescan = lambda: rescans.append(1)
    reconcile._remote_keys = lambda: set()
    direct_ingest.WATCH_DIR = watch
    direct_ingest.FAILED_DIR = watch / ".failed"
    direct_ingest.SKIPPED_DIR = watch / ".skipped"
    direct_ingest.LOG_FILE = tmp / "direct_ingest.log"
    direct_ingest.STABLE_SEC = 0

    # ----------------------------------------------------------------------
    print("Part 1 -- discovery: files, dot-releases, directories; junk excluded")
    _reset_watch()
    (watch / "a.cbz").write_bytes(b"comic")
    (watch / "b.epub").write_bytes(b"book")
    (watch / "c.mkv").write_bytes(b"video")
    (watch / "d.mp4").write_bytes(b"video")
    (watch / ".Planetes.2003.S01E01.mkv").write_bytes(b"video")
    (watch / "e.srt").write_bytes(b"subs")            # identityless alone
    (watch / "f.txt").write_bytes(b"readme")
    (watch / "._junk.mkv").write_bytes(b"appledouble")
    (watch / ".DS_Store").write_bytes(b"finder")
    (watch / "season01").mkdir()
    (watch / "season01" / "Some Show S01E01.mkv").write_bytes(b"video")
    (watch / "clutter").mkdir()
    (watch / "clutter" / "notes.txt").write_bytes(b"nope")
    got = sorted(p.name for p in direct_ingest._find_media())
    check("every ingestible entry is found",
          got, sorted(["a.cbz", "b.epub", "c.mkv", "d.mp4",
                       ".Planetes.2003.S01E01.mkv", "season01"]))
    check("no video is missed", any(n.endswith(".mkv") for n in got), True)

    # ----------------------------------------------------------------------
    print("\nPart 2 -- a loose video's subtitle siblings are attached to its plan")
    _reset_watch()
    video = watch / "Show.S01E01.mkv"
    video.write_bytes(b"video")
    sub = watch / "Show.S01E01.srt"
    sub.write_bytes(b"subs")
    tagged = watch / "Show.S01E01.en.srt"
    tagged.write_bytes(b"tagged subs")
    (watch / "Show.S01E02.srt").write_bytes(b"another episode")
    plan = {"media_type": "show", "files": [
        {"src": str(video),
         "dst_rel": "Shows/Show (2020)/Season 01/Show (2020) - S01E01.mkv"}]}
    direct_ingest._attach_video_sidecars(plan, video)
    added = [f for f in plan["files"] if f["src"] == str(sub)]
    check("the exact-stem subtitle is attached", len(added), 1)
    check("its destination mirrors the video's base name with .srt",
          added[0]["dst_rel"], "Shows/Show (2020)/Season 01/Show (2020) - S01E01.srt")
    tagged_added = [f for f in plan["files"] if f["src"] == str(tagged)]
    check("a language-tagged subtitle is attached too", len(tagged_added), 1)
    check("and keeps its language tag beside the video",
          tagged_added[0]["dst_rel"],
          "Shows/Show (2020)/Season 01/Show (2020) - S01E01.en.srt")
    check("a DIFFERENT episode's subtitle is not attached",
          any("S01E02.srt" in f["src"] for f in plan["files"]), False)
    dir_plan = {"media_type": "show", "files": [
        {"src": str(watch / "season01"),
         "dst_rel": "Shows/Some Show (2020)/Season 01/Some Show (2020) - S01E01.mkv"}]}
    before = len(dir_plan["files"])
    direct_ingest._attach_video_sidecars(dir_plan, watch / "season01")
    check("a directory drop is left entirely to the run", len(dir_plan["files"]), before)

    # ----------------------------------------------------------------------
    print("\nPart 3 -- process(): a movie + subtitle is filed, both sources removed")
    _reset_watch()
    (media / "Movies").mkdir(parents=True, exist_ok=True)
    movie = watch / "Some Film (2024).mkv"
    movie.write_bytes(b"m" * 4096)
    msub = watch / "Some Film (2024).srt"
    msub.write_bytes(b"s" * 100)
    direct_ingest.identify.run_identify = fake_identify({
        "media_type": "movie", "title": "Some Film", "year": 2024, "tmdb_id": "12345",
        "files": [{"src": str(movie), "dst_rel": "Movies/Some Film (2024).mkv"}],
    })
    rescans.clear()
    ok = direct_ingest.process(movie)
    check("process() reports success", ok, True)
    check("the movie is in the library",
          (media / "Movies" / "Some Film (2024).mkv").exists(), True)
    check("its subtitle landed beside it",
          (media / "Movies" / "Some Film (2024).srt").exists(), True)
    check("the video source was removed", movie.exists(), False)
    check("the subtitle source was removed", msub.exists(), False)
    check("a Jellyfin rescan was requested for a movie", len(rescans), 1)

    # ----------------------------------------------------------------------
    print("\nPart 4 -- an empty plan is read by drop type, never taken on trust")
    # 4a: unproven video -> parked intact in .failed/
    _reset_watch()
    ep = watch / "Some Show S01E01.mkv"
    ep.write_bytes(b"v" * 4096)
    direct_ingest.identify.run_identify = fake_identify(
        {"media_type": "show", "title": "Some Show", "files": []})
    check("process() reports the drop was not filed", direct_ingest.process(ep), False)
    check("an unproven empty plan does NOT delete the video", ep.exists(), False)
    check("it was parked in .failed/",
          (watch / ".failed" / "Some Show S01E01.mkv").exists(), True)
    check("with an error sidecar for the human",
          (watch / ".failed" / "Some Show S01E01.mkv.error.txt").exists(), True)

    # 4b: the SAME drop, but every episode is provably in the library -> deleted
    _reset_watch()
    ep = watch / "Some Show S01E01.mkv"
    ep.write_bytes(b"v" * 4096)
    libdir = config.SHOWS_ROOT / "Some Show (2020)" / "Season 01"
    libdir.mkdir(parents=True, exist_ok=True)
    (libdir / "Some Show (2020) - S01E01.mkv").write_bytes(b"already")
    direct_ingest.identify.run_identify = fake_identify(
        {"media_type": "show", "title": "Some Show", "files": []})
    check("a proven already-present episode is reported as a no-op",
          direct_ingest.process(ep), False)
    check("the proven duplicate source is deleted", ep.exists(), False)
    check("it is NOT parked in .failed/",
          (watch / ".failed" / "Some Show S01E01.mkv").exists(), False)

    # 4c: an unproven movie (no SxxExx, no matching film title) is parked
    _reset_watch()
    film = watch / "Unknown Film (2021).mkv"
    film.write_bytes(b"v" * 4096)
    direct_ingest.identify.run_identify = fake_identify(
        {"media_type": "movie", "title": "Unknown Film", "files": []})
    check("an unproven movie empty plan does not delete", direct_ingest.process(film), False)
    check("the unknown film is parked", (watch / ".failed" / film.name).exists(), True)

    # 4d: a movie whose title IS in the library -> deleted
    _reset_watch()
    film = watch / "Some.Film.2024.1080p.mkv"
    film.write_bytes(b"v" * 4096)
    (media / "Movies").mkdir(parents=True, exist_ok=True)
    (media / "Movies" / "Some Film (2024).mkv").write_bytes(b"already")
    direct_ingest.identify.run_identify = fake_identify(
        {"media_type": "movie", "title": "Some Film", "files": []})
    check("a proven already-present movie is a no-op", direct_ingest.process(film), False)
    check("the proven movie duplicate is deleted", film.exists(), False)

    # 4e: an ARCHIVE keeps the historical verdict: empty plan -> deleted
    _reset_watch()
    comic = watch / "Some Comic 001 (2020).cbz"
    comic.write_bytes(b"c" * 4096)
    direct_ingest.identify.run_identify = fake_identify(
        {"media_type": "comic", "title": "Some Comic", "files": []})
    check("an archive empty plan is still a verdict",
          direct_ingest.process(comic), False)
    check("the redundant archive is deleted", comic.exists(), False)

    # 4f: a DIRECTORY empty plan is never deleted on trust
    _reset_watch()
    season = watch / "Some Show S01"
    season.mkdir()
    (season / "Some Show S01E01.mkv").write_bytes(b"v" * 4096)
    direct_ingest.identify.run_identify = fake_identify(
        {"media_type": "show", "title": "Some Show", "files": []})
    check("a directory empty plan is not a delete verdict",
          direct_ingest.process(season), False)
    check("the directory is parked intact in .failed/",
          (watch / ".failed" / "Some Show S01").exists(), True)

    # ----------------------------------------------------------------------
    print("\nPart 5 -- a directory drop lands as one run, leftovers park in .skipped/")
    _reset_watch()
    season = watch / "Some Show Season 2"
    season.mkdir()
    ep = season / "Some Show S02E01.mkv"
    ep.write_bytes(b"v" * 4096)
    (season / "show.nfo").write_text("release clutter", encoding="utf-8")
    direct_ingest.identify.run_identify = fake_identify({
        "media_type": "show", "title": "Some Show", "year": 2020, "tmdb_id": "99",
        "files": [{"src": str(ep),
                   "dst_rel": "Shows/Some Show (2020)/Season 02/"
                              "Some Show (2020) - S02E01.mkv",
                   "season": 2, "episode": 1}],
    })
    check("process() reports success", direct_ingest.process(season), True)
    check("the episode landed",
          (media / "Shows" / "Some Show (2020)" / "Season 02" /
           "Some Show (2020) - S02E01.mkv").exists(), True)
    check("the planned source was removed",
          (season / "Some Show S02E01.mkv").exists(), False)
    check("the drop directory left the watch folder", (season / "show.nfo").exists(), False)
    check("the unplanned leftover was parked in .skipped/ (not destroyed)",
          (watch / ".skipped" / "Some Show Season 2" / "show.nfo").exists(), True)
    check("the show seed was written",
          (media / "Shows" / "Some Show (2020)" / "tvshow.nfo").exists(), True)

finally:
    (direct_ingest.WATCH_DIR, direct_ingest.FAILED_DIR, direct_ingest.SKIPPED_DIR,
     direct_ingest.LOG_FILE, direct_ingest.identify.run_identify,
     config.MEDIA_ROOT, config.MOVIES_ROOT, config.SHOWS_ROOT, config.NOVELS_ROOT,
     library.dbhook.record_plan, ingest._jellyfin_rescan, reconcile._remote_keys,
     direct_ingest.STABLE_SEC) = saved
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for x in failures:
        print(f"  - {x}")
    sys.exit(1)
print("direct ingest covers all media: all checks passed.")
