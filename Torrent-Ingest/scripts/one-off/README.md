# one-off/ — hand repairs, NOT fleet modules

Nothing in here is wired into a daemon, imported by fleet code, or run on a schedule. These
are the scripts a session wrote to perform a specific, dated repair by hand. They are kept
because the *record of exactly what was changed* is worth more than the script, and because
a couple of them are genuinely re-runnable.

| script | what it did | date |
|---|---|---|
| `fix_metadata.py` | 20 episode titles/plots the free-AI healer could not fix: Fate/Grand Order Babylonia S01E00-E12 (which carried Fate/kaleid liner Prisma Illya's metadata), Monogatari S15 (Zoku Owarimonogatari -> Koyomi Reverse Parts 1-6), Dr. STONE S04E38. | 2026-09-03 |
| `fill_synopses.py` | **DO NOT RE-RUN AS-IS.** Intended to fill blank synopses from TVMaze with the season mapping proven per season. Its blank test read `Overview` from `media_doctor.Jellyfin.episodes()`, which does not request that field, so the test was always true: it filled 0 blanks and replaced 221 correct plots. Fully reverted. The mapping logic is good and worth keeping; fix the blank test and key the episode map on the file PATH before using it again. | 2026-09-03 |
| `restore_plots.py` | reverted `fill_synopses.py` -- restored all 587 sidecars to their exact pre-run plot text and unlocked Overview on the 250 Jellyfin items. | 2026-09-03 |
| `babylonia.json` | the TVMaze titles/plots `fix_metadata.py` wrote, kept so the change is auditable without a network call. | 2026-09-03 |

Full write-up, including why the fleet's own AI healer failed and what to fix in it:
`../../METADATA-REPAIR-2026-09-03.md`.

**Read `fill_synopses.py`'s docstring before re-running it.** It talks to Jellyfin and
writes sidecars. It has one known sharp edge, documented in the write-up (§2.6b): it keys
its episode map on `(season, number)`, and `/Shows/{id}/Episodes` can return another
series' episodes when two series share a provider id. Key on the file path if you extend it.
