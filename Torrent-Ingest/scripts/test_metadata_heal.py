#!/usr/bin/env python3
"""Metadata self-heal sees pool-only shows and verifies its writes (HANDOFF 10.4).

THE TORIKO CASE, all three reasons it never self-healed:
  1. `audit_metadata` walked `~/Media`, where the 147 videos are EVICTED, so it found
     0 episodes and wrote an empty worklist beside 8 junk titles and 69 blank plots.
     The audit now enumerates the mediafs mount, which is complete.
  2. `_guide_index` required name AND summary from TVMaze; Toriko has 146 names and 0
     summaries, so the deterministic filler did nothing. Name-only rows are now a
     real partial fill, and a synopsis source (TMDB overviews) fills the plots.
  3. `media_doctor.escalate()` counted any non-empty closing sentence as success, so
     two empty AI runs retired Toriko permanently. The budget is charged only when
     the sidecars actually improve.

Both directions, fixtures only (no network, no live library):
  Part 1 -- a pool-only-looking show under a stubbed mount is FOUND by the audit.
  Part 2 -- a name-only guide row writes the title; TMDB overviews fill the plot; the
            provider title survives the synopsis pass.
  Part 3 -- the escalation postcondition: a run that changes nothing is NOT counted;
            one that changes the .nfo is.

    python3 scripts/test_metadata_heal.py

Exit 0 = all checks passed.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import library                                                       # noqa: E402
import media_doctor as md                                            # noqa: E402

failures: list[str] = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


def make_show(root, title="Fixture Show", n=3):
    show = root / title
    (show / "Season 01").mkdir(parents=True)
    for i in range(1, n + 1):
        v = show / "Season 01" / f"{title} - S01E{i:03d}.mkv"
        v.write_bytes(b"\0")
        nfo = v.with_suffix(".nfo")
        nfo.write_text(
            '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
            "<episodedetails>\n"
            f"  <title>{'[Judas] x265 10b' if i == 1 else f'Episode {i}'}</title>\n"
            f"  <season>1</season>\n  <episode>{i}</episode>\n"
            "  <showtitle>Fixture Show</showtitle>\n"
            "  <lockdata>true</lockdata>\n"
            "</episodedetails>", encoding="utf-8")
    (show / "tvshow.nfo").write_text(
        "<tvshow><title>Fixture Show</title><year>2011</year>"
        "<tmdbid>38251</tmdbid><tvdbid>247231</tvdbid></tvshow>", encoding="utf-8")
    return show


print("Part 1 -- the audit enumerates the mount, not the local SSD")
tmp = tempfile.TemporaryDirectory()
saved_mount, saved_shows = config.MEDIAFS_MOUNT, config.SHOWS_ROOT
try:
    mount = Path(tmp.name) / "MediaLibrary"
    local = Path(tmp.name) / "Media"
    config.MEDIAFS_MOUNT, config.SHOWS_ROOT = mount, local / "Shows"
    show = make_show(mount / "Shows")

    import audit_metadata as audit
    check("the audit root is the mount", audit.shows_root() == mount / "Shows")
    rec = audit.audit_show(show)
    check("the pool-only-looking show is found with its blanks", rec["blank"] == 3
          and len(rec["blanks"]) == 3)
    check("its tmdb id is carried for the synopsis source", rec.get("tmdb_id") == "38251")

    print("Part 2 -- name-only guide rows + TMDB synopses, provider title wins")
    import repair_metadata as repair
    import epguide
    import tmdbguide
    GUIDE = [{"season": 1, "number": i, "name": f"Real Title {i}", "summary": ""}
             for i in (1, 2, 3)]
    old_eps, old_ov = epguide.episodes, tmdbguide.episode_overviews
    epguide.episodes = lambda _t: GUIDE
    tmdbguide.episode_overviews = lambda _tid, _s: {
        (1, 1): "The real synopsis one.",
        (1, 2): "The real synopsis two.",
        (1, 3): "The real synopsis three.",
    }
    try:
        todo = [{"video": str(show / "Season 01" / f"Fixture Show - S01E{i:03d}.mkv"),
                 "season": 1, "episode": i, "abs": i} for i in (1, 2, 3)]
        fixed, residue = repair._fill_from_guide("Fixture Show", list(todo),
                                                 Path(tmp.name) / "backup")
        check("name-only rows are written (title)", fixed == 3)
        one = show / "Season 01" / "Fixture Show - S01E001.nfo"
        text = one.read_text("utf-8")
        check("the junk title was replaced with the guide's",
              "<title>Real Title 1</title>" in text and "[Judas]" not in text)
        check("a name-only row still needs its synopsis", len(residue) == 3)
        syn_fixed, residue2 = repair._fill_synopses(
            {"show": "Fixture Show", "tmdb_id": "38251"}, residue,
            Path(tmp.name) / "backup")
        check("TMDB overviews fill the plots", syn_fixed == 3 and not residue2)
        text = one.read_text("utf-8")
        check("the plot landed", "The real synopsis one." in text)
        check("the provider title SURVIVED the synopsis pass",
              "<title>Real Title 1</title>" in text)
        check("the sidecar is locked", "<lockdata>true</lockdata>" in text)
    finally:
        epguide.episodes, tmdbguide.episode_overviews = old_eps, old_ov

    print("Part 3 -- escalation charges its budget only on a verified write")
    stale = make_show(mount / "Shows", title="Stale Show", n=1)
    before = md._metadata_repair_state(str(stale))
    check("the postcondition snapshot sees a blank plot and a junk title",
          before == (1, 1))

    class _Proc:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout
            self.stderr = ""

    old_run, old_bin = md.subprocess.run, md.config.AI_BIN
    try:
        # A run that talks but writes nothing: not counted.
        md.subprocess.run = lambda *a, **k: _Proc('{"result": "All fixed!", "is_error": false}')
        probs = {"show": "Stale Show", "path": str(stale),
                 "problems": [{"kind": "plot_blank", "detail": "x", "auto": False, "sev": 1},
                              {"kind": "title_janky", "detail": "y", "auto": False, "sev": 2,
                               "items": [{"file": "Stale Show - S01E001.mkv", "season": 1,
                                          "episode": 1, "nfo_title": "[Judas] x265 10b",
                                          "jellyfin_title": "", "plot_blank": True}]}],
                 "sig": "1"}
        check("a run that changes nothing is NOT counted",
              md.escalate(probs, dry_run=False) is False)

        # A run whose work shows up in the sidecars: counted.
        def _fix(*_a, **_k):
            nfo = stale / "Season 01" / "Stale Show - S01E001.nfo"
            t = nfo.read_text("utf-8")
            t = t.replace("[Judas] x265 10b", "The Real Title")
            t = t.replace("</episodedetails>",
                          "  <plot>Fixed by the run.</plot>\n</episodedetails>")
            nfo.write_text(t, encoding="utf-8")
            return _Proc('{"result": "Fixed 1 episode.", "is_error": false}')

        md.subprocess.run = _fix
        check("a verified write IS counted",
              md.escalate(probs, dry_run=False) is True)

        # The postcondition is measured on the episodes the run was HANDED, never on
        # the whole show: a live pack keeps filing while the run works, and those new
        # sidecars must not move the verdict either way. Both directions, so neither
        # the old whole-show count nor a blanket "did anything change" can pass.
        print("Part 4 -- the postcondition names the work, not the whole show")
        race = make_show(mount / "Shows", title="Race Show", n=2)
        target = race / "Season 01" / "Race Show - S01E001.nfo"
        neighbour = race / "Season 01" / "Race Show - S01E002.nfo"
        junk = "[Judas] x265 10b"

        def race_probs():
            return {"show": "Race Show", "path": str(race), "sig": "2",
                    "problems": [{"kind": "title_janky", "detail": "x", "auto": False,
                                  "sev": 2,
                                  "items": [{"file": "Race Show - S01E001.mkv",
                                             "season": 1, "episode": 1,
                                             "nfo_title": junk,
                                             "jellyfin_title": "", "plot_blank": True}]}]}

        def _fix_unrelated(*_a, **_k):
            t = neighbour.read_text("utf-8")
            t = t.replace("<title>Episode 2</title>", "<title>The Real Neighbour</title>")
            t = t.replace("</episodedetails>", "  <plot>Present.</plot>\n</episodedetails>")
            neighbour.write_text(t, encoding="utf-8")
            return _Proc('{"result": "done", "is_error": false}')

        md.subprocess.run = _fix_unrelated
        check("an unrelated fix does NOT count as repairing the target",
              md.escalate(race_probs(), dry_run=False) is False)

        def _fix_target(*_a, **_k):
            t = target.read_text("utf-8")
            t = t.replace(junk, "The Real Target")
            t = t.replace("</episodedetails>", "  <plot>Fixed.</plot>\n</episodedetails>")
            target.write_text(t, encoding="utf-8")
            (race / "Season 01" / "Race Show - S01E003.mkv").write_bytes(b"\0")
            (race / "Season 01" / "Race Show - S01E003.nfo").write_text(
                '<?xml version="1.0"?><episodedetails>'
                f"<title>{junk}</title><season>1</season><episode>3</episode>"
                "</episodedetails>", encoding="utf-8")
            return _Proc('{"result": "fixed the target", "is_error": false}')

        md.subprocess.run = _fix_target
        check("a target fix DOES count even while the pack keeps filing",
              md.escalate(race_probs(), dry_run=False) is True)
    finally:
        md.subprocess.run, md.config.AI_BIN = old_run, old_bin
finally:
    config.MEDIAFS_MOUNT, config.SHOWS_ROOT = saved_mount, saved_shows
    tmp.cleanup()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
