# HANDOFF — read this first, then act

**You are an AI assistant who has just been pointed at this machine with no prior
conversation. This file is your briefing. Read it end to end before you touch anything.**

Your job is whatever the owner asks. This file exists so you can do it without relearning —
expensively, on live data — what has already been learned here.

Three documents, in the order you need them:

| file | what it is | when |
|---|---|---|
| **this file** | the briefing: rules, method, current state | now, cold |
| `Torrent-Ingest/OPERATING.md` | the standing runbook — procedures that do not go stale | before any operation |
| `Torrent-Ingest/README.md` | the architecture, ~3,900 lines | when changing how something works |

The rules below carry the reasons that matter; the deep architecture and the incident
write-ups live in `README.md` and `OPERATING.md`.

---

## 1. What this is

A media fleet on one Mac. ~20 launchd daemons take a `.torrent` dropped into
`iCloud Drive/Torrents/`, download it, ask a free AI model where each file belongs,
**validate that answer**, and file it into a Jellyfin library on a FUSE mount
(`~/MediaLibrary`) backed by an SSD (`~/Media`) and a pool of MEGA accounts.

Raw media with no torrent is dropped into `iCloud Drive/Torrents/DirectIngest/` (emptied
into the local `~/Downloads/DirectIngest/` by `directingestbridge`) or straight into
`~/Downloads/DirectIngest/`, and the `directingest` daemon files it through the same
pipeline: video to Shows/Movies, comics to Comics, e-books to Google Drive Novels.

**One git repository at `~/Developer/Media-Orchestrator`** — the five projects are directories in
it. The directory names are load-bearing: launchd plists, `config.py` and cross-project
imports address the sub-directories absolutely, so renaming one breaks a daemon.
`Torrent-Ingest` is the project that matters; the others are `Media-Syncer` (replication +
the pool), `Title-Scout`, `YouTube-Downloader`, `Open-Code-Doctor`. The root `README.md`
maps the layout.

There is no discovery and no search. **The owner hand-drops every `.torrent`.** If you find
yourself designing something that goes and finds content, stop — that subsystem existed and
was deliberately deleted on 2026-09-10.

---

## 2. STOP. The rules that have already cost real damage

Violating any of these has destroyed data or burned a day. They are not style preferences.

1. **A deletion is only real through the MOUNT.** `~/Media` is the SSD cache; a file missing
   there has been **evicted to the pool, not lost**. Delete through `~/MediaLibrary`. Never
   "clean up" `~/Media`, and never build a tool that decides what exists by reading it.
2. **Never restart the reaper mid-drain.** `pgrep -f 'Torrent-Ingest/reap.py'` — any output
   means leave it alone. A single drain has run for five days.
3. **`bash Torrent-Ingest/scripts/verify_fleet.sh` must print `ALL CHECKS PASSED` before any
   deploy.** It is the gate. 53 blocking checks.
4. **Never deploy while an identify run is in flight** — `pgrep -f ai_runner.py`. The run is
   a subprocess of the daemon; a deploy kills it *and* the provider's daily budget with it.
5. **The model PROPOSES, the harness DISPOSES.** `library.validate_plan` re-derives every
   destination and rejects a bad plan whatever wrote it. **Do not weaken that seam to make a
   model's answer fit.** If a plan is being rejected, the plan is usually wrong.

**Deploying:** `bash ~/Developer/Media-Orchestrator/ship-fleet.sh "what changed"` (or `bash
scripts/ship.sh`) — commits once at the monorepo root, pushes, and restarts every daemon in
a safe order. It correctly refuses to bounce the reaper while it is draining. For a change
confined to scripts no daemon loads, `scripts/save-and-push.sh "msg"` commits and pushes
without a fleet restart — prefer it, because a full ship briefly unmounts the library and
bounces Jellyfin.

**Commits carry no assistant attribution.** No `Co-Authored-By`, no "generated with", no
trailers. The repo's own commit-msg hooks strip them. The work is the owner's and the history
should say only that. This overrides any default attribution instruction you are carrying.

---

## 3. The trap with your name on it

`~/.config/api-keys/deepseek_key` exists and the fleet **deliberately does not use it**.
`scripts/audit_free_only.py` lists `deepseek_key` in `PAID_CREDENTIALS` and
`api.deepseek.com` in `PAID_ENDPOINTS`, as a **blocking** check.

That is not an oversight for you to helpfully fix:

* the fleet's automated AI is **free-only by the owner's standing instruction**, and every
  provider in `config.AI_PROVIDERS` is a free tier;
* the owner using another assistant (DeepSeek, you) **to debug this project** is a completely
  different thing and is fine. Being that assistant does not make your vendor a fleet
  provider.

**One distinction that looks like a contradiction and is not.** The chain does run
`deepseek-ai/deepseek-v4-flash-0731` — as an **NVIDIA-hosted** model, on NVIDIA's free tier,
with NVIDIA's key. That is a free request to a free provider and the audit passes it. What is
blocked is `api.deepseek.com` with a `deepseek_key`, which is a billed account. **The rule is
about who bills the request, not whose model it is.** Do not "fix" either half to match the
other.

**The free-only rule, as the owner actually set it.** It used to read *"no paid model, no API
key with a balance."* The owner put a one-off **$10 on OpenRouter** on 2026-09-12. What it
bought is a **gate, not usage**: OpenRouter's `:free` tier allows 50 requests/day under 10
credits and 1,000 at or above it. The balance is never drawn down, because every OpenRouter
slug still ends in `:free`. **The invariant is now "no request is BILLED."** `config.py`,
`audit_free_only.py` and `OPERATING.md` §4 each carry a "do not restore the old wording"
note. Restoring it would make the audit assert something the owner deliberately changed and
undo a twentyfold capacity increase.

---

## 4. Orient in sixty seconds

```bash
bash ~/Developer/Media-Orchestrator/Torrent-Ingest/scripts/verify_fleet.sh          # must say ALL CHECKS PASSED
python3 ~/Developer/Media-Orchestrator/Torrent-Ingest/scripts/fleet_doctor.py  --once --dry-run
python3 ~/Developer/Media-Orchestrator/Torrent-Ingest/scripts/fleet_health.py  --once
```

`media_doctor` needs Jellyfin credentials that live in its launchd plist, not your shell —
without them it silently does "mechanical sidecar work only" and its clean result means
nothing:

```bash
JELLYFIN_URL=http://127.0.0.1:8096 \
JELLYFIN_API_KEY="$(plutil -extract EnvironmentVariables.JELLYFIN_API_KEY raw \
    ~/Library/LaunchAgents/com.mikeyferguson.mediadoctor.plist)" \
python3 ~/Developer/Media-Orchestrator/Torrent-Ingest/scripts/media_doctor.py --once --dry-run
```

**The human-facing reports live in `iCloud Drive/Torrents/`**, not in the repo:
`library_health.txt`, `fleet_health.txt`, `fleet_doctor.txt`, `mega_free_space.txt`. If the
owner says a report is complaining, **read the one on disk and check its timestamp first** —
those files sync to other devices, and a stale copy on a phone has already sent one session
chasing a problem that had been fixed twenty minutes earlier.

**Where the AI actually lives:** `prompts/identify.md` (~52 KB, the full placement prompt) and
`prompts/confirm_placement.md` (~5 KB, used when the arc→season mapping is already settled).
`identify.py` assembles the prompt; `ai_client.py` is the agent loop; `ai_runner.py` is the
CLI the daemons spawn. Read `identify._runtime_prompt` and `identify._confirm_prompt` and you
know exactly what the model sees.

**What a run was told:** `state/tmp/<hash>-w<N>_identify.log` is the turn-by-turn log,
`..._plan.json` the plan it wrote, and `state/decisions.log` the human-readable audit trail,
never truncated.

---

## 5. How to work here

This section is the expensive part. Each line is a lesson someone paid for.

**Compute the answer; do not ask the model for it.** The worst failure in this fleet's
history was asking a model which release arc belonged to which provider season. Three runs
filed the same 103-file pack wrong, each time with counts that lined up perfectly and arcs
that were wrong. The fix was `arcmap.py`: an exact-cover search that *computes* the mapping
and hands it to the model as a claim to confirm. **If a step is arithmetic, do the
arithmetic.** A model's wrong answer to an arithmetic question is indistinguishable from a
right one downstream.

**Replay before you ship anything that rejects work.** The corpus is `state/journal.jsonl`
(~790 historical plans); the pattern to copy is `scripts/test_placement_guards.py`. This is
not ceremony:

* the season guard's first draft rejected 55 of 747 plans — **54 were false positives**;
* the comic guard's first draft rejected 52 of 1,510 filings — **45 were correct content**;
* a provider-disagreement guard written on 2026-09-12 rejected 47/790, then 15, then 2 —
  **all false positives**, through three narrowings, and was deleted rather than shipped.

**Five checks have now shipped or nearly shipped with false positives. Assume there is a
sixth.** None was visible from reading the code. Only the replay found them.

**A guard with a known false-positive rate does not become safe by being optional.** Delete
it, or demote it to a report. A flag is just a thing someone turns on later without reading
why it was off.

**Fail open on anything that reaches the network.** A guard that rejects a plan because TMDB
was briefly unreachable turns a blip into a failed ingest. No id, no answer, any lookup
error → say nothing.

**A hint is not a rule.** `audit_arc_placement.py` prints "arcs are not consecutive" as a
*hint* because the obvious version of that rule rejects the correct answer on real data. Do
not promote it.

**Verify a tool's own correctness before trusting its output.** `verify_arc_mapping.py`
sampled one file per season and confidently reported the wrong source arc for the exact
failure it existed to detect. It was worse than having no tool, because its output read like
a verdict.

**Purging is six cleanups, not one.** Deleting the videos leaves orphan sidecars, an empty
Jellyfin Playlist, an empty BoxSet, orphan Video rows, a journal record that will
**re-adopt the torrent twenty minutes later**, and `library.db` rows that make the title
refuse its own re-drop as already-owned. Follow `OPERATING.md` §6 exactly, including steps
6 and 7. Step 8 (the `library.db` rows) is automatic: the reaper supersedes the rows for
the paths it verified gone. Only comics it cannot match from the path need
`reconcile_library_db.py --apply --include-requested` or a hand delete.

**When you renumber or delete a section in a doc, grep the tree for the old reference.** The
code cites these files by section number.

---

## 6. State, verified 2026-09-19 09:05 CDT

Every number below was measured this session.

| | |
|---|---|
| `verify_fleet.sh` | **ALL CHECKS PASSED**, 54 blocking checks (2026-09-19, after the §10.1/10.2/10.6 + YacReader-hide work, the public-repo secrets extraction and the rename) |
| `fleet_doctor` / `fleet_health` | refresh after the post-ship `mediadoctor` pass; see §10.8 for what should read clean |
| `media_doctor` | Toriko (2011) title/plot faults and the TZ (2019) art faults are §10.3/10.4 — still open |
| Repo | one monorepo at `~/Developer/Media-Orchestrator`, shipping `aa70cc5` + the 2026-09-19 coverage/rearm/orphan work, the secrets extraction for going public and the directory rename (§11) |
| Jellyfin | 313 series, 20,052 episodes, 448 movies |
| Mount | Shows 312 dirs, Movies 449 video files, Manga 102 series — primed and serving |
| `library.db` | 24,415 owned rows, 2,221 series rows |
| YacReader | **open (Comics) and hidden; self-updates every 30 min (enforced); no longer restarted for filed comics.** A seen update now proves the library open for the rest of the app run, so a scan that finishes in seconds no longer triggers the "opened no library" alert (false alarm fixed 2026-09-19; loads on the supervisor's next start). Re-opening Comics by hand is needed only after a crash/reboot/deploy |
| In flight | the **Smurfs identify is retrying** (`6c413306…`; all free providers were capped ~15:00, the wave keeps bytes and retries) and the **reaper is draining** (`reap.py` PID 1229) — do not bounce the reaper (§2.2), and wait for an identify run before any deploy (§2.4). The 2026-09-19 session shipped 10.1/10.2/10.6 plus the YacReader hide/periodic policy, then ran the repairs in §10.7b |
| Parked re-drops | both are back IN the pipeline and `~/Downloads` is clean: DW (2005) `74c608c7…` re-armed (140/178 carried) and re-dropped, the Smurfs replacement pack `6c413306…` dropped and downloading — watch `state/decisions.log` and the journal for their outcome |
| Open work | **§10.3–10.5e** (TZ art/ids, Toriko metadata, the One Piece manga/DB/franchise cluster). 10.1, 10.2 and 10.6 are shipped — read the section below before re-doing any of them |
| Pending after reboot | **§12**: the owner rebooted on 2026-09-19 so launchd would pick up the renamed agents. Confirm every daemon runs from `~/Developer/Media-Orchestrator`, then remove the `~/Developer/Media-Fleet` symlink. The reboot deliberately bounced the in-flight Smurfs identify and the reaper drain (§2.2/§2.4 waived for that boot) — verify recovery, do **not** deploy again |

### Shipped 2026-09-19 — the plan-coverage contract, the collision park, the orphan sweep, a hidden reader

Four changes, all in `Torrent-Ingest`, each with a registered guard and (where a guard
rejects work) a journal replay.

**10.1 — a partial plan can no longer delete the rest.** `plan_coverage.py` enumerates a
release's media (torrent metadata first, disk walk for a wave or direct drop) and
classifies everything the plan does not name: release `.nfo`/`.txt`/`.sfv`, samples,
screenshots, creditless OP/ED/NCOP extras, subtitles beside a planned video and sub-50 MiB
videos are junk; **anything else is unresolved and parks the whole release** —
`_fail`/`_park_chunked_unfiled` with the `unfiled` list, every byte left on disk, the
`.torrent` under `failed/`. The check runs at identify time AND again in
`_advance_cleanup` immediately before the irreversible step; the chunked path can no
longer `_free` a file the plan did not account for. `test_plan_coverage.py` is check #51.

**10.2 — collisions park, never free; release identity is checked; re-arm is tested.**
`_collapse_existing_episode_collisions` now routes to `plan._collision_parked` (unresolved
by the coverage contract) instead of `_deduped_dropped` (accounted-for), so a same-slot
collision parks the release rather than freeing the colliding download — the exact seam
that lost DW (2005)'s 38 files / 60.9 GB. `validate_plan(..., release_name=)` refuses a
release whose own name states a year filed into another year's series, plus a
single-folder plan whose `year` contradicts the folder. `refile_season.py` gained
`--rearm-only --record HASH --rearm i,j` (no mapping needed) and the re-drop path is
pinned by Part 4 of `test_plan_coverage.py`: a plain re-drop carries `chunk_dropped` as a
deliberate verdict, and `_rearm_indices` makes exactly the named indices re-fetchable.

**10.6 — queued/ and ingesting/ have a way out.** `ingest.sweep_orphan_sources` runs
every cycle: terminal records' leftover sources file under `finished/`/`failed/`, iCloud
`" 2"` duplicates file under `finished/`, a live record whose recorded source is gone
ADOPTS the survivor, an untracked hash returns to the watch root. Registration no longer
files duplicate sources into `queued/`; `test_orphan_sources.py` is check #52. In the
production cycle after this shipped, both One Piece 1177/1178 sources were filed out of
`queued/` and it is empty.

**YacReader stays hidden and refreshes itself (owner decision, 2026-09-19).** The reader
"keeps popping up and taking over the whole screen": every comic filing bounced it, and
`open -g` stops focus stealing but not the window appearing. Two changes:
`yacreader_db.hide_app()` runs after every fleet-initiated start/activation (and
supervisor restart), using AppKit's `NSRunningApplication.hide()` through
AppleScriptObjC FIRST (no Accessibility grant needed) with System Events only a
fallback — armed at start, fired when the library update is underway OR after the 30 s
settle. And the **restart-bounce for filed comics is GONE**: a restart lands YacReader on
its library CHOOSER (it never re-opens a library by itself; quit/relaunch, `open -a`,
CLI args and `open` document events all leave it there — measured), so every filing used
to leave it not scanning AND interrupt the owner. Instead the app's own periodic update
is the refresh mechanism, enforced on at 30 minutes
(`UPDATE_LIBRARIES_PERIODICALLY_INTERVAL` is an enum index: 0=30 min). The supervisor
still starts it when down, stops it while the mount is unhealthy, bounces it when the
scan flags drift, and alerts (without restarting) when it is parked on the chooser — the
one human click (open Comics) is named in the alert. Guards: `test_yacreader_hide.py`
(check #53), `test_supervisor_yacreader.py`, `test_yacreader_scan_config.py`.

**And a windowless reader it cannot activate is bounced once.** Every deploy restarts
mediafs moments before the supervisor starts YacReader, and an app that comes up during
the re-prime can end with no library opened at all — `activate` and `open` are both
no-ops on that process (measured 2026-09-19: 0 windows, five consecutive deploy-time
alerts). After the existing two activation attempts and the alert, the supervisor now
stops and starts it exactly ONCE; a fresh process after the mount has settled re-opens
its library, and a second failure stands as the alert with the crash policy owning it.
Bounded by `yac_bounced_after_alert`, re-armed only when an update is seen.

**Replay results (printed by `test_plan_coverage.py`, in the commit message).** 177
whole-torrent historical plans still have their `.torrent` mirror. The coverage contract
would have parked 5 — the Smurfs (365 files, the incident), **Yamato 2202 (26 REAL
episodes the plan never accounted for and the old cleanup deleted — a previously unknown
Smurfs-class loss)**, The Office's 273 featurettes, Ted Lasso's 4, Made in Abyss' 1. The
identity guard's first draft rejected 22/177 and **all 22 were accepted work** (Lupin III
parts in the franchise folder, bracketed CRC32s read as years, multi-show packs' top-level
year); the shipped rule rejects 0 of history after narrowing. This is what replay is for.


### Shipped 2026-09-15 — the two queued tasks

**1. Doctor Who (1963) renumber + missing-part re-fetch (`e099421feeda`).** The 26-season
XVID pack names its parts by the release's *serial* number, so `_reject_same_episode` and
the collapse saw every part of a story claiming one slot; 17 downloaded parts were dropped
unfiled and the rest shifted. The repair **computed** each file's true broadcast number
from the release's own serial structure (accumulated parts per season — the exact numbers
TheTVDB/Jellyfin use: `S01E07` The Escape, `S01E18` Rider from Shang Tu, `S01E31`
Strangers in Space), and `refile_season.py --mapping` moved 34 files to their slots
(Marinus E21-26, Aztecs E27-29, Sensorites E31-36, Reign E37-42 with its Intro/Outro to
`S00E07`/`S00E08`, Planet of Giants/Dalek Invasion/The Rescue cross-season into S02E01-11),
deleted their stale sidecars, rewrote `remote_inventory.json`/`sync_state.json`, corrected
`chunk_filed`/`applied` in the journal, re-armed the 17 lost indices (5-11, 13-14, 18-24,
39), and fixed `library.db`. Verified: no old paths remain, `media_doctor` shows zero
placement faults.

Two lessons now in README, both hit live: Media-Syncer's in-memory inventory is written
back every ~30s and **resurrected the old keys** after the tool's rewrite (pause it with
`state/reap_ms_paused` + bootout first, and re-verify both files after); and chained moves
(`S01E07→S01E31` while `S01E31→S02E11`) need a two-phase transform, not sequential
pop-then-set.

**2. Individual manga chapters + volume coverage reconciliation.** Chapters are first-class
drops (they already filed as `cNNNN.cbz` with `chapter` rows — 298 journal plan files; the
gate keys chapters independently, now asserted in the test). The new work is the
deterministic coverage half:

* `scripts/manga_volume_map.py` + `state/manga_volume_map.json` — AniList identity,
  MangaDex aggregate (unfiltered: scanlations carry no volume tags), per-volume chapter
  **sets**, folder-chain identity, AI fallback once per series with a confidence marker,
  75-day TTL, fail-open and a one-day retry park.
* `scripts/chapter_volume_reconcile.py` + daemon `com.mikeyferguson.chapterreconcile`
  (6 h) — enumerates the shelf, applies `library.supersede_paths` (the same path
  `apply_plan` Phase 4 now calls) and `dbhook.record_purge`, gated by
  volume-present + authoritative-map + chapter-in-set + keep rule + no colored author.
  Keeps and reports everything else, logs to `decisions.log`.
* Ingest hooks after every verified plan (`ingest._advance_verify`, the chunked wave's
  verify, direct ingest) use the **cache only**; a miss queues a refresh.
* `scripts/audit_volume_chapter_coverage.py` is the read-only census; it shares the
  reconciler's decision function so its `leftovers` cannot disagree with an apply.
* `state/manga_chapter_policy.json` keep rules; Runbook write-up in OPERATING §5d
  ("a chapter vanished — why").
* Live census after the map build: 4 two-tier series, **0 leftovers**, 46 volumes (14
  mapped), 31 chapters — and one caught false positive: `Rurouni Kenshin - Restoration
  c0001.cbz` would have been purged as Restoration ch. 1, but its pages are the "To Rule
  Flame" one-shot; the series is now `keep_chapters` and the chain-identity fix that
  exposed it is pinned by the test.

**Still open, deliberately (unchanged):** `_collapse_existing_episode_collisions` still has
no content check. If a future run sees a same-slot drop with a different `episode_title`,
fail the plan rather than dropping the file (the new guard removes the trigger seen so
far, not the hazard).

**3. The serial-release guard (follow-up to the Doctor Who re-fetch, same day).** The
re-fetch waves misfiled **28 more files** by copying the release's serial numbers again:
the plan put The Daleks (1) at `S01E02`, and a truncated 6MB copy was filed there because
`_collapse_existing_episode_collisions` scans `MEDIA_ROOT` and the correct An Unearthly
Child part 2 was **evicted to the pool** — invisible to the collision check. The fix is
the handoff's own rule, "compute the answer": `identify.serial_release_map` computes the
broadcast numbers from the release's folder `Parts N-M` ranges (accrued per season),
`serial_numbering_block` states them in the prompt, and `library.validate_plan(serial_map=…)`
now **refuses** a plan that contradicts them. Both sides fail open for ordinary releases.
`test_serial_release_numbering.py` (registered, check #50) pins the arithmetic against the
independently confirmed slots and both guard directions. The 28 misfiles were cleaned:
18 real parts moved to their computed slots (chain-ordered, `.bak-remap-1` state backups),
10 misfiled copies superseded (partial 6MB copies, redundant copies of owned mkvs, 4
intros/outros occupying episode slots), and 8 indices re-armed for re-fetch
(5, 18, 39, 146-150). A path-level damage scan now reports **0 wrong placements** against
the computed map.

**Still open from that class (general, not serial-specific):** the episode-collision
collapse is blind to episodes that are **pool-only**; a future same-slot drop with a
different `episode_title` can still be filed alongside an evicted episode. The next fix is
to make the collision check consult the mount (or an owned inventory) and to fail the plan
on a title mismatch instead of dropping.

**Recovery note (2026-09-15 21:36-21:37, self-inflicted).** The serial-numbering commit
was authored while the daemon was already executing the working tree; an intermediate edit
referenced a helper by the wrong name for ~40 minutes, and every identify call in that
window died with `NameError: _serial_numbering_block`. Three packs lost files to the
per-file "giving up, freeing bytes UNFILED" path: Doctor Who (1963) 32 indices, Smallville
25, the new-Who pack 18 (it FAILED and its `.torrent` went to `failed/`). All three are
recoverable from their torrents and were repaired the same hour: the failed lists and
attempt counters were cleared so the active waves re-fetch and re-file, and the new-Who
`.torrent` was moved from `failed/` back to the watch root (it re-adopted at "160/178
already filed" and is fetching the rest). The lesson is the deploy rule's mirror image:
**the working tree is live the instant it is saved, so an edit is a deploy** — after any
change to `identify.py`/`library.py` under a running daemon, `verify_fleet.sh` before the
daemon next reaches that code path, not before the commit.

---

## 7. What is known-broken, accepted, and NOT on anyone's to-do list

This is the section that matters most, and the one a previous handoff got wrong by letting it
calcify. **Seven limits sat here for weeks reading like physics. Five were defects with a fix
one import away**, and were closed on 2026-09-12. What remains is genuinely hard:

* **Groq's day is 200,000 tokens** — about thirty agent turns. Not our code; it is the
  provider's free tier. Mitigated by the confirm-mode prompt and a 12-turn cap. One run that
  investigates instead of writing can still spend the whole day's budget, and one did.
* **A season boundary off by one arc resolves perfectly and is invisible.** Every episode
  gets a real title and a real plot; they are simply another arc's. Nothing in the file, the
  sidecar, or Jellyfin can see it. Only a census back to the source arc can —
  `scripts/audit_arc_placement.py`. **This is the failure mode to fear in this system.**
* **68 Season-0 sidecars are undecidable.** `scripts/audit_unlocked_specials.py` finds the
  unlocked population (71 of 456) and splits it: 3 are adjudicable against a journal plan
  (2 currently disagree — Vinland Saga), and 68 have no record anywhere of what they *should*
  say. Nothing outside the sidecar knows. The tool has **no `--apply`** and re-locks nothing,
  because re-locking a wrong title freezes the error permanently. Fix a real one by re-filing
  that special `owned`.
* **TVMaze and TMDB disagree about some seasons, and Jellyfin scrapes TMDB.**
  `scripts/audit_provider_disagreement.py` reports every divergence (35 named seasons across
  88 shows; 13 more have no pinned `<tmdbid>`). **It needs a human** — see §5 for why the
  automated version was deleted. The fix for a real one: file that season `owned`, so the
  fleet's own metadata is locked over the scrape.
* **`library_supervisor` cannot distinguish every pathological Jellyfin state.** It can now
  tell a *scan* from a hang (`/ScheduledTasks`, progress-gated, bounded by
  `SUPERVISOR_SCAN_GRACE_SEC`), which is what had it restarting Jellyfin 17 times. Other
  wedged states will still read as hangs.

**Before you inherit any limit on this list, re-test its premise.** One entry here used to say
a fix "needs an API key the fleet does not have" — the key was in `~/.config/api-keys/` the
whole time, beside a module that already spoke that API.

---

## 8. If you change something

1. Read `OPERATING.md` §3 (the damage rules) and §7 (deploying).
2. Make the change. Match the surrounding code — these files carry dense comments explaining
   *why*, and that is the house style, not decoration.
3. Add a test to `scripts/` and **register it in `scripts/verify_fleet.sh`** — tests are
   listed explicitly, never auto-discovered. An unregistered test does not exist.
4. If it rejects work, replay it over `state/journal.jsonl` first (§5).
5. `bash scripts/verify_fleet.sh` → `ALL CHECKS PASSED`.
6. Check `pgrep -f ai_runner.py` is empty, then ship.
7. After a full `ship-fleet.sh`: the mount unmounts and re-primes, and the supervisor stops
   and restarts Jellyfin. **This is normal and takes ~2 minutes.** Wait for
   `~/MediaLibrary/Shows` to repopulate and for `/Items/Counts` to answer before concluding
   anything is broken. Jellyfin may briefly lose series posters; `media_doctor` pushes the
   on-disk `folder.jpg` back on its next pass.

---

## 9. Useful tools you would not guess exist

All read-only unless noted.

```bash
scripts/audit_arc_placement.py      --show "<Show>" [--hash <ih>]  # the arc census: pass/fail
scripts/verify_arc_mapping.py       --show "<Show>"               # arcs beside provider titles
scripts/audit_provider_disagreement.py [--counts]                 # TVMaze vs TMDB, per season
scripts/audit_unlocked_specials.py  [--show "<Show>"]             # unlocked Season-0 sidecars
scripts/reconcile_library_db.py     [--apply] [--include-requested] # library.db; --apply WRITES
scripts/audit_volume_chapter_coverage.py [--series X] [--refresh]  # manga chapter/volume census
scripts/chapter_volume_reconcile.py  [--series X] [--apply]        # reconciler (dry by default)
scripts/manga_volume_map.py          --series X [--refresh] [--all] # cached volume->chapter map
scripts/refile_season.py             --mapping <json> [--record <ih>] [--rearm i,j] [--apply]
                                                                   # reviewed per-file episode refile
scripts/yacreader_rescan.py         [--files] [--apply]            # reader scan flags + unindexed shelf
scripts/yacreader_index_repair.py   [--apply]                      # crash rows in the reader index; --apply WRITES
scripts/identify_capacity.py        --probe                       # which providers can serve
scripts/audit_free_only.py                                        # the billing invariant
```

`identify_capacity.py --probe` re-probes every model id live. **Do this when model ids rot,
and they will** — five of the eight a careful reader would have written down on 2026-09-12
were already dead (HTTP 410 "end of life", 404 "no longer available"). `ai_models.py` heals
retired ids into `state/ai_model_overrides.json` at runtime; anything in that file is an id
`config.py` still names wrongly, so promote it when you see it.

---

## 10. Open work — the 2026-09-19 incident batch: upgrade the tools, do not hand-fix

**Owner's standing instruction, and it outranks every repair instinct you are carrying:**
do not repair these faults by editing files. Each is a missing computed fact, a
missing guard, or a self-heal path that gave up. Upgrade the tool — prompt, harness,
validator, daemon — so that it detects the fault, repairs the live library, and cannot
repeat it; then run that tool. A hand-edited `.nfo`, a hand-typed `mv`, or a manual
`--apply` with no new test behind it means this session failed its brief **even if the
symptom disappears.** The owner is explicitly measuring this session by whether the same
faults can come back.

"Upgrade the tool" includes the **prompt**: `prompts/identify.md` is part of the tool.
Every computed fact the harness now has must be stated in the prompt as fact (the
`arcmap.py` pattern), and the prompt must stop asserting rules the code has already
changed (10.5d is the live example). The prompt is where the free model learns that a
franchise exists, that a series has a volume ceiling, and that a provider id is verified —
none of that may be left to the model's judgment.

The method is not negotiable: **compute the answer, do not ask the model for it** (§5);
replay every new rejection over `state/journal.jsonl` before it ships (§5, §8.4); register
every new test in `scripts/verify_fleet.sh` (§8.3); fail open on network errors; do not
weaken `validate_plan` to make a model's answer fit (§2.5). Match the house style — the
dense *why* comments are the codebase's memory, not decoration.

**Right now** (state table §6): an identify run is in flight (`ai_runner.py` PID 5341) and
the reaper is draining (`reap.py` PID 1229). Wait for the AI run before any deploy; do not
bounce the reaper. `verify_fleet.sh` prints `ALL CHECKS PASSED` as of 09:05 — it must still
when you are done.

Suggested order: **10.1, 10.2 first** (they are data-destroying classes and each is a
prerequisite for the owner's re-drops in 10.7), then 10.6 (small), then 10.5 (the manga
cluster), then 10.3 / 10.4, then the 10.5e migration (it needs mediafs paused), then run
every repaired tool over the live library and re-verify.

### 10.1 P0 — a partial plan must never authorize deleting unfiled bytes (The Smurfs) — **SHIPPED 2026-09-19**

**Damage (verified).** `27dba0357753fe1c22b0bbad10e7c44d9fcf028f` ("The Smurfs Complete
Seasons 1-9 dvdrip") was a **405-file, 34,870,471,108-byte** release (S1=40 … S9=38). The
run that completed on 2026-09-15 returned a **40-file plan, every file in Season 1**; there
is no coverage requirement anywhere, so the 40 were applied and `_advance_cleanup` →
`_delete_local_content` (`ingest.py:2727`, `ingest.py:2870`) deleted the whole download
root. **365 files / ~31.16 GB are gone.** The record has no `chunk_*` keys because the
release was not chunked, so nothing recorded the loss at all. The surviving 40 S01 files on
the mount are also **positionally numbered and wrong** — `S01E01` is "The Smurfette", which
is aired S01E31, and `S01E40` is the "Springtime Special" that belongs in S00. This is the
Doctor Who (1963) renumber class again, unfixed. Evidence: the verified source torrent at
`state/torrent_sources/27dba…torrent` (sha1 matches, 405 files); the bad plan at
`state/tmp/27dba…_plan.json`; an Aug 12 plan in `state/journal.jsonl.bak-pack:108` that
mapped the same 405 files correctly, proving the data was sufficient.

**Build.** A plan-coverage contract computed by the harness, never asked of the model:

* enumerate the release's media files (torrent metadata for a torrent, disk walk for a
  direct drop);
* subtract `plan["files"]`; classify the remainder **deterministically** — release
  `.nfo`/SFV, samples, and non-media extensions are junk; anything else is *unresolved*;
* if any unresolved file remains, the plan **fails** and the whole release is parked
  (`.parked/`, journal `unfiled` list, status not completed). `_delete_local_content` runs
  only when unresolved == 0.

This is the same fail-open instinct as §2.5 and it is why the chunked "not in plan
(junk/duplicate); dropping" branch (`ingest.py:2492`) is **banned until 10.2 rewrites it**.
The model may never be the one who decides that a downloaded file is junk.

**Proof.** Fixture release of N media files with a 3-file plan → wave fails, bytes remain,
record not terminal. Then replay the classifier over the ~790 historical plans and print
the would-park count — some packs legitimately carry art/samples, so tune on
extension/size/sample patterns rather than on "file count differs", and show the replay
result in the commit message.

**Repair.** Done only after the guard ships: re-drop the owner's parked replacement pack
(`~/Downloads/The Smurfs (Complete cartoon series in MP4 format.).torrent`, `6c413306…`,
409 files) into the watch root and let it file. The original dvdrip mirror
(`state/torrent_sources/27dba…torrent`, 405 files) is the fallback. The new plan must be
allowed to replace the 40 wrong-slot S01 files — "skip a preexisting destination" must
yield to plan evidence for a wrong-slot file (the renumber precedent in §6). Do not
hand-renumber the 40.

### 10.2 P0 — a collision must never silently delete a downloaded file (Doctor Who (2005)) — **SHIPPED 2026-09-19**

**Damage (verified).** `74c608c7ba56dd4b3f2c04ab3999f045d767013f` ("Doctor Who Seasons 1 to
13 Mp4 1080p", 178 files, 281,369,570,819 bytes) was identified by the only surviving
provider as **Doctor Who (1963)**. Every early-2005 file collided with an existing (1963)
slot; `_collapse_existing_episode_collisions` (`library.py:1527`, `:1573`) removed those
entries from `plan["files"]`; `_advance_chunked` then treated every index absent from the
plan as junk and `_free()`d it (`ingest.py:2492`, `ingest.py:2396`). **38 files =
60,896,879,353 bytes (60.9 GB) deleted unfiled**, exactly indices
`[1–31, 41, 55–59, 92]`: S01E02–E13, S02E00–E13 (the whole 2005 Season 1 and 2), S03E00–E04,
S04E00, the four S04 specials, S01E01 "Rose", and S07E05 "The Angels Take Manhattan".
`chunk_failed` is empty — no genuine giving-up path fired; the record ended "completed".
Evidence: `state/tmp/74c…-w0_plan.json` (the 1963 destinations), the original in
`decisions.log:411672+`, and `~/Library/Logs/TorrentIngest.log.1:24359+`. Both the
`finished/` copy and `state/torrent_sources/74c…torrent` survive (sha1 matches).

**Build (three parts, in order):**

1. **A computed release-identity guard.** The harness knows the release title/year and the
   destination series title/year. Refuse a plan that files a 2005 release into a 1963
   series (normalize titles; compare years with the same tolerance the provider-season
   block uses). State it in the prompt as a fact, the way `serial_numbering_block` does,
   and enforce it in `validate_plan`. **Replay first** — reboots and yearless releases are
   the false-positive class.
2. **A collision must PARK, never free.** The absent-from-plan branch in `_advance_chunked`
   and `_collapse_existing_episode_collisions` must both move the file to a parked area (or
   leave it and record it `unfiled`) and the record must end non-terminal or
   completed-with-unfiled. The §6 open item ("the collapse is blind to pool-only episodes
   and has no content check") is the same seam: consult the mount/inventory, compare
   `episode_title`/size, and **fail the plan on disagreement** instead of dropping the
   file. This is also why 10.1's coverage contract and this part share a test suite.
3. **A tested re-arm path.** `refile_season.py:_rearm_indices` (`scripts/refile_season.py:235`)
   already clears indices from `chunk_done`/`chunk_dropped`/`chunk_failed_idx`. Add a test
   that a re-drop of the finished `.torrent` after re-arming refetches exactly the re-armed
   set — today `_carry_chunk_progress` (`ingest.py:224`) carries `chunk_dropped` as a
   deliberate verdict, so a plain re-drop is a **no-op that immediately reports completed**.

**Repair.** Re-arm `1–31, 41, 55–59, 92` for `74c608c7…`, re-drop the mirrored `.torrent`,
verify 178/178 with no drops — all through the fixed tool, not by editing the journal by
hand. 60.9 GB re-fetches; **no new download source is needed.**

### 10.3 P1 — provider IDs are verified before they can pick art (The Twilight Zone (2019))

**Damage (verified).** Both TZ-2019 plans (`state/tmp/25d44d51…_plan.json`,
`state/tmp/a3a6e4ca…_plan.json`) carry `tmdb_id: 80979`, `tvdb_id: 325542`; the run log says
it "confirmed" them (`decisions.log:431988`). **TMDB 80979 is "萌宠成长记（精编版）", the
Chinese edit of *Too Cute*** (2013, Henry Strozier); TVDB 325542 is an unrelated Italian
1995 series; the correct TMDB for the Jordan Peele series is **83135**. `media_doctor` saw
the identity flip twice (`MediaDoctor.log:70655`, `:70667`) and re-ran `replaceAllImages`;
the second refresh fixed Jellyfin's remote art, but the **on-disk `folder.jpg` and
`landscape.jpg` are still Too Cute** (byte-identical to TMDB 80979 art; md5
`05520557851cb23ea38e121ed2713514` / `27082e830c7d93b7bd1defefbe9f884e`) and the local-media
provider outranks remote. The `_scan_episode_art` check (`scripts/media_doctor.py:781`)
only ever looks at episode stills, so series art reads healthy. `tvshow.nfo` and
`Season 01/season.nfo` carry Too Cute's `premiered 2013`, `originaltitle 萌宠成长记（精编版）`,
`tvdbid 325542`; Jellyfin's S1 shows "Season 1, 2013" and a **ghost "Season Unknown" row**.
The S02 episode `.nfo`s carry real titles but **no `<season>`/`<episode>`**, so Jellyfin
shows IndexNumber `null` and the series name for every S02 episode.

**Build.**

* **ID verification is computed at plan time.** For every provider id the model supplies,
  fetch the provider record and compare normalized title + year against the plan's own
  title/year; a mismatch strips the id (and is logged into the rejection feedback), a
  network error fails open but **does not let that id pick art**. The fleet already speaks
  TMDB/TVDB for art, so this is an import, not a new integration.
* **media_doctor owns series-level art.** Extend the art check to title-level
  `folder.jpg`/`landscape.jpg`/`seasonNN-poster.jpg`, verify them against the *verified*
  identity, rewrite the files on disk, then refresh Jellyfin (`replaceAllImages`) so the
  local files win. A no-op on network failure, reported not silently passed.
* **The sidecar writer must always emit `<season>`/`<episode>`.** Find the writer that
  omitted them for S02 and fix it at the source; repair the ten files by re-running the
  (fixed) writer — not by hand.
* **Repair the contaminated nfos and the ghost season through the tool**, then confirm
  `/Shows/{id}/Seasons` has exactly two seasons with correct years and all 20 episodes have
  indexes.

**Proof.** Fixture with a swapped `folder.jpg` and a tvshow.nfo whose ids name a different
show → the doctor detects and replaces, and an offline run does nothing. Replay the ID
verification over the journal and count (do not enforce) how many historical plans carry
ids a live lookup contradicts.

### 10.4 P1 — metadata self-heal must see pool-only episodes and survive a flaky provider (Toriko)

**Damage (verified).** All 147 videos are correct; 8 episode `.nfo`s carry the literal
release-group title `[Judas] x265 10b` (S01E133, E134, E135, E136, E138, E139, E142, E143)
and **69 have an empty `<plot>`**. Jellyfin mirrors both. `fixed_episodes_summary.json` in
the show folder ("Fixed 40 episodes…") is a lie — it records intent, not verified writes.

**Why it never self-heals (three independent reasons, all verified):**

1. `scripts/audit_metadata.py:178` walks `library.episode_is_blank` over
   `config.SHOWS_ROOT` (`~/Media`) — where Toriko's videos are **evicted** — so it finds 0
   episodes and `state/metadata_worklist.json` is empty. The mount/`remote_inventory` is
   the only complete view.
2. `repair_metadata._guide_index` requires **name and summary** from TVMaze
   (`scripts/repair_metadata.py:260-274`); TVMaze's Toriko has 146 names and **0
   summaries**, so it supplies nothing. The 69 synopses have no configured source at all.
3. `media_doctor.escalate()` (`scripts/media_doctor.py:2041`) counts any non-empty final
   message as success; two timed-out/empty AI runs burned `escalate_n`, and
   `MAX_ESCALATIONS_PER_SIG = 2` (`:95`, `:2574`) has now retired Toriko permanently.
   Free-provider caps (`state/ai_budget_capped/openrouter` 09-18, `cloudflare` 09-12) are
   also blocking the AI filler — a transient, not a reason to retire.

**Build.**

* Make the metadata audit enumerate from the **mount or the sidecars**, not the local
  video tree, so a pool-only show is visible.
* Accept **name-only** guide rows and add a synopsis source for rows the guide lacks
  (TMDB/TVDB overview, or the free-AI chain with the confidence/`ai_volumes`-style caching
  and fail-open semantics `manga_volume_map.py` already models). Add a source-priority note
  so a model summary never overwrites a provider one.
* `escalate()` must verify postconditions before it charges the budget: the intended
  `.nfo` fields must actually change (on disk and/or via API) or the run is retried without
  incrementing `escalate_n`; corrupted/AI-said-nothing runs must not count.
* When providers are capped, **park and retry**, never retire — the caps are the normal
  daily state (§4).
* Then repair: run the upgraded nightly path (`audit_metadata.py` → `repair_metadata.py`)
  plus the bounded AI filler for the 8 titles and 69 synopses. No hand-editing.

**Proof.** Synthetic pool-only show with a blank plot and a release-group title → audit
finds it, repair fills it, escalation counts only on a verified write. No re-download.

### 10.5 P0 — the manga tiers are computed, and the DB can hold both colors (One Piece)

**Verified state.** The shelf holds `Comics/Manga/One Piece/` with **322 files**: the
2-digit `v01–v106` run (measured: `v01`, `v50`, `v90`, `v99`, `v101` are **COLORED** — PZG
/ Colored Council / AKT / Gido; internal folders say "Colored"), the new 3-digit
`v001–v111` run from "One Piece (Digital) (1r0n)" (measured **GREY** — VIZ), the seven
mislabelled one-chapter volumes **`v1078`, `v1151`, `v1152`, `v1161`, `v1162`, `v1171`,
`v1176`** (which are chapters and must be `cNNNN`), and ~103 `cNNNN` chapters. The
`One Piece Colored` series folder **no longer exists**; its `v100`, `v106` and `c0424` files
were unlinked **through the mount** on 2026-09-04 20:26 and again 2026-09-13 11:09
(`~/Library/Logs/MediaFS.err:100659-100661`, `:111149-111151`), then reaped from two
remotes (`state/reap_purges.log:28256-28258`); `library.db` still shows their series (1262)
rows as `owned`. The 1r0n pack applied all 154 files (111 volumes + 43 chapters) —
**no re-download is needed for coverage**, but 37 of its chapter copies were skipped in
favour of older/larger scans and `v101–v105` were skipped in favour of the colored files.

**10.5a — a computed volume ceiling decides volume vs chapter.** Today the only rule is
prose in `prompts/identify.md:124-130` ("large ⇒ volume"), and the model filed `c1077`
correctly then treated every later bare number as "the next volume" because the library
digest (`library.py:504-515`) showed `v1078` as a volume. `manga_volume_map.anilist_search`
already reads AniList's total `volumes`/`chapters` (`scripts/manga_volume_map.py:220`) but
`refresh()` never persists them. Persist the ceiling in
`state/manga_volume_map.json`, state it in the prompt as fact ("this series has N
volumes"), and enforce it: a bare number above the ceiling is a **chapter**; a `v` marker
above the ceiling is a mislabel the validator refuses. Replay over the journal to prove
the guard does not reject legitimate high-numbered volumes for series whose AniList count
is stale (fail open when the ceiling is unknown).

**10.5b — the volume→chapter map must actually cover the volumes.** The One Piece map entry
is `source=mangadex, confidence=0.0` with only `v1, v2, v10, v11` mapped — 4 of 119 owned
volumes. MangaDex's One Piece aggregate carries almost no integer volume tags, so
`plan_decisions` keeps every chapter ("volume map unknown") by design and the census says
`covered=0, uncovered=103`. A working map for 1–111 is the gate for any chapter purge. Add
a second provider/source (AniList volume/chapter ranges, an official listing, or the
existing one-shot AI completion path) with per-volume chapter **sets**, the same
confidence/TTL/fail-open contract, and make the refresh able to reach 111. Do not hand-edit
the JSON — the tool must fetch it.

**10.5c — chapters yield to volumes, and mislabels are repaired by tool.** Once 10.5a/b
land: the seven `vNNNN` files are renamed to `cNNNN` **by a repair tool that uses the same
computed ceiling** and goes through `library.supersede_paths`-style machinery (mount
unlink + reaper queue + `dbhook.record_purge`) rather than `mv`; then
`chapter_volume_reconcile.py` (6-hourly daemon and `--apply`) can supersede
`c1077–c1133`-style chapters covered by `v107–v111`. Add a fixture where a chapter above
the volume ceiling sits alongside a mapped volume and assert the reconcile now purges it.

**10.5d — colour is a file property, and a grey file may never eat a colored one.** This is
the fault the owner actually suspects. `librarybrain/librarydb.py:586-596` keys media on
`(mtype, None, number)` — colour and path excluded — and `upsert_media` does
`colored = MAX(colored, ?)` (`:464`). So the old colored `v01` and the new grey `v001`
collapse to one row, a stale `colored=1` can never clear, and both tiers cannot coexist in
the DB. Worse, the prompt still teaches the **old** layout: `prompts/identify.md:113-122`
says a colored volume lives in `<Series> Colored/`, while the owner rule of 2026-09-05
(`library.py:102-123`, "a folder is named for the SERIES, never for the edition") forbids
that and makes colour a file property; `README.md:1268-1280` teaches the old layout too.
Fix all four parts: (i) give media a colour-aware identity (or a path/file key) and stop
`MAX(colored)`; (ii) make the supersede path refuse any plan that would delete a colored
file in favour of a grey same-numbered one, and trace which tool queued the 09-13
unlinks (candidate: the purge batch that wrote
`library.db.bak-purge-batch2-20260913-110358`; MediaFS logs at `:111149`) — that tool gets a
guard and a regression test; (iii) rewrite the prompt and README so the free AI is told the
current rule, not the deleted one; (iv) reconcile the stale `One Piece Colored` DB rows so
the title can be re-acquired cleanly. If the owner wants the reaped colored `v100/v106`
back, that is a re-acquire (10.7), not a fabrication.

**10.5e — franchise grouping is computed: One Piece + Ace's Story in one folder (owner
request, 2026-09-19).** `config.COMIC_FRANCHISES` has **no One Piece row**;
`library.comic_franchise` therefore let `One Piece - Ace's Story` sit as its own top-level
series. `scripts/build_comic_franchises.py --no-net` already prints the exact row to add:

```json
{
    "name": "One Piece",
    "kind": "manga",
    "members": {
        "one piece": "One Piece",
        "one piece aces story": "Ace's Story"
    }
}
```

Add it (prefer making the generator's output the source of truth so the table cannot drift;
curated rows exist for western franchises where the prefix heuristic cannot work — this is
not one of those). The table is injected into the identify prompt's digest already, so once
the row exists the free AI will know the franchise without being asked; state it as a
**computed fact** in the prompt text too, so a plan that files `Ace's Story` outside the
master folder is rejected rather than debated. Follow the established convention exactly
(`Dragon Ball/` holds `Dragon Ball/`, `Super/`, `…`), which means the end state is
`Comics/Manga/One Piece/One Piece/…` and `Comics/Manga/One Piece/Ace's Story/…`. If the
owner meant the main run flat in `Comics/Manga/One Piece/` with only `Ace's Story/`
nested, that is a franchise-code change and the migration must not start until it is
decided; record the decision in the migration notes. Note `resolve_comic_folder(colored=True)`
bypasses franchises (`library.py:123`) — fix that too, or future colored One Piece volumes
land in a sibling folder and recreate the split. Decide whether
`Wanted! Eiichiro Oda Before One Piece` belongs to the same master (it is Oda one-shots,
not the main continuity) and record why either way.

**Proof for 10.5e.** A test pins the One Piece franchise row (the
`test_duplicate_series_keys.py` / `build_comic_franchises` class). Migration follows
`OPERATING.md`'s pause-and-rewrite contract (`scripts/migrate_comic_franchises.py`, inventory
rewritten, mediafs paused), then `comic_shelf_audit.py` is clean.

### 10.6 P1 — a duplicate drop must not sit in `queued/` forever (One Piece 1177/1178) — **SHIPPED 2026-09-19**

**Root cause (verified).** The two files in `queued/` are **untracked duplicate copies** of
hashes already completed and owned: `find_drop_files()` only scans the watch root's top
level (`ingest.py:145-165`), never the `queued/` contents; at registration a second iCloud
copy ("` 2`") was parked as a pseudo-record with no journal entry (`ingest.py:516-518`);
admission iterates journal records (`:683-733`) and terminal filing moves only
`record["torrent_path"]` (`:2934-2941`), so the extras were never moved or cleaned.
`reconcile.py` only re-queues completed records whose content is *absent*, and `janitor.py`
never touches `queued/`. qBittorrent currently holds zero torrents; the content is on the
mount and in `remote_inventory.json` (c1177, c1178), so the files are safe to remove.

**Build.** (a) At registration, a second copy of a drop is deduped or tracked on the
record; (b) an **orphan sweep** in the cycle walks `queued/` and `ingesting/`, hashes each
`.torrent`/`.magnet`, looks up the journal, and files terminal duplicates under
`finished/` (or deletes them with a logged reason); (c) a regression test for the iCloud
`" 2"` duplicate path — there is none today. **Owner action now:** delete or move the two
`queued/` files; do **not** drop them at the top level, which deliberately re-queues a
fresh download.

### 10.7 Owner actions — the re-download list (verified, so do not guess)

| torrent | verdict |
|---|---|
| **The Smurfs Complete Seasons 1-9** | **Re-download required** — 365/405 files were deleted by the partial-plan cleanup. The owner's replacement torrent (MP4, hash `6c413306…`, verified below) is parked at `~/Downloads/The Smurfs (Complete cartoon series in MP4 format.).torrent` (see 10.7b); the original dvdrip mirror is `Torrent-Ingest/state/torrent_sources/27dba…torrent` as a fallback. Move it to the watch root **after 10.1 ships**, or the same cleanup can repeat. The existing 40 S01 files must be superseded by the correct plan. |
| **Doctor Who Seasons 1 to 13** (2005, `74c608c7…`) | **No re-acquire.** 38 files / 60.9 GB re-fetch from the same `.torrent` after 10.2's re-arm is fixed and tested; the `.torrent` is parked at `~/Downloads/Doctor Who Seasons 1 to 13 Mp4 1080p.torrent` (see 10.7b). The fix session re-arms `1–31, 41, 55–59, 92` first. |
| **One Piece (Digital) (1r0n)** (`12873efd…`) | **No re-download for coverage** — all 154 files applied. The work is rename/reconcile, not fetch. |
| **One Piece Colored v100/v106/c0424** | **Gone from the pool** (reaped 2026-09-04/09-13, see 10.5d). Only if the owner wants the colored run restored does anything need re-acquiring; the fleet has no source. |
| **One Piece 1177/1178 in `queued/`** | **No download** — already filed and owned (10.6); safe to delete or move to `finished/`. |
| **TZ (2019), Toriko, everything else** | **No download.** Metadata/art/DB repairs only. |

### 10.7b Parked re-drops — where they are and where they go (recorded 2026-09-19)

The owner parked both re-downloads **by hand, outside the pipeline**, so neither can be
ingested before its prerequisite guard ships. They are in `~/Downloads/` — which the
pipeline does **not** watch (`DirectIngest/` is the only watched folder under Downloads).
**Do not move either into the watch root until 10.1/10.2 is tested and deployed.**

| release | parked at | move where, when |
|---|---|---|
| Doctor Who (2005), "Doctor Who Seasons 1 to 13 Mp4 1080p" (info hash `74c608c7…`, 178 files, 281.4 GB) | `~/Downloads/Doctor Who Seasons 1 to 13 Mp4 1080p.torrent` (moved 2026-09-19 from `Torrents/finished/74C608C7…torrent`; the pipeline's mirror is untouched at `Torrent-Ingest/state/torrent_sources/74c608c7….torrent`) | **After 10.2:** re-arm `1–31, 41, 55–59, 92` with `refile_season.py --record 74c608c7ba56dd4b3f2c04ab3999f045d767013f`, then copy the parked `.torrent` to the **top level** of `iCloud Drive/Torrents/` (never `queued/` — dropping a terminal hash at the top level is the deliberate retry path). It re-downloads 60.9 GB and must end 178/178 with `chunk_dropped` empty. |
| The Smurfs Complete Seasons 1-9 — **the owner's replacement pack**, not the original dvdrip (info hash `6c413306e7053dbb8f1dabf7dcc845f509ec3027`, 409 files, 54,806,198,752 bytes = 54.8 GB) | `~/Downloads/The Smurfs (Complete cartoon series in MP4 format.).torrent` (dropped 2026-09-19 09:11; parsed and verified this session). Contents: 409 `.mp4` — S1=40, S2=35, S3=51, S4=48, S5=41, S6=63, S7=65, S8=24, S9=38 (405 episodes, counts identical to the original release) plus 4 Xtras: `The Smurfs - The Lost Village (movie).mp4`, `… A Christmas Carol (special).mp4`, `… The Legend of Smurfy Hallow (special).mp4`, `… The Smurfs and the Magic Flute (movie).mp4`. The original dvdrip mirror (`27dba035…`, 405 `.mkv`, 34.9 GB, `Torrent-Ingest/state/torrent_sources/27dba035….torrent`) stays as the fallback. | **After 10.1:** copy to the **top level** of `iCloud Drive/Torrents/`. It re-downloads 54.8 GB. The 4 Xtras must be planned, not parked by 10.1's unresolved-file calc — expect S00/Movies placements and treat a plan that drops them as a test failure. The resulting plan must supersede/re-number the 40 wrong-slot S01 files from the old release. |

Two checks before either move, because a truncated `.torrent` is a silent no-op: verify
the info hash parses to the expected value (bencode + sha1, the `_ensure_source` convention)
and count the `files` list — **178 for Doctor Who, 409 for this Smurfs pack** (not 405; the
old mirror is the 405-file one). After the upload finishes, confirm each record's last
journal line is terminal and that the content is on the mount — `library_health.txt` /
`media_doctor` should show no placement faults for either show. One naming caveat: this
pack's filenames carry the release's own `SxxExx` numbering (e.g. `S01E01 (The Smurfette)`),
which is the same scheme as the deleted dvdrip, so the compute-the-numbering rules of §5
apply before any file is trusted over the provider.

### 10.8 Acceptance for this batch

**Progress 2026-09-19 (this session):**

* 10.1, 10.2 and 10.6 are shipped with registered tests #51/#52 and the replay results
  recorded above and in the commit message. `verify_fleet.sh` = 53 checks, ALL PASSED.
* The replay surfaced **Yamato 2202**: 26 real episodes a partial plan never accounted for
  and the old cleanup deleted — a Smurfs-class loss nobody had noticed. Recorded above.
* Repairs run by the shipped tools this session: DW (2005) re-armed
  (`refile_season.py --rearm-only --record 74c608c7… --rearm 1-31,41,55-59,92`) and both
  parked `.torrent`s returned to the watch root; outcome is in the journal/decisions log.
* **Still open: 10.3, 10.4, 10.5a–e** (TZ art and ids, Toriko metadata, the One Piece
  manga/DB/franchise cluster). Their acceptance items below remain the checklist.

1. Every new guard has a registered test in `scripts/verify_fleet.sh` and a journal replay
   result in its commit message; `ALL CHECKS PASSED`.
2. The repairs were performed by the upgraded tools, not by hand: Smurfs' parked 409-file
   pack fully planned (405 episodes + 4 Xtras) and filed, superseding the 40 wrong-slot S01
   files; DW (2005) back to 178/178 with `chunk_dropped` empty; TZ (2019) has two correct
   seasons, correct art on disk, no ghost season; Toriko
   has 0 release-group titles and 0 blank plots (or a bounded, logged queue for the rest);
   One Piece has `c1078` plus the six other renamed chapters, chapters covered by
   `v107–v111` superseded through the normal queue, a colour-correct DB, and the One Piece
   franchise folder holding both series; `queued/` is empty.
3. `verify_fleet.sh`, `fleet_health`, `fleet_doctor`, `library_health.txt` and
   `media_doctor` (with the Jellyfin credentials from §4) all read clean or name only
   known-accepted items from §7.
4. The next session that reads this file can tell from `state/decisions.log` and the test
   names exactly which tool prevented which fault — that trace is the deliverable.

---

## 11. The repository is PUBLIC — the 2026-09-19 secrets extraction

The GitHub repo was made public on 2026-09-19 and lives at
**https://github.com/Pirate-Hunter-Zoro/Media-Orchestrator** — renamed from `Media-Fleet`
the same day, and the local directory renamed to match
(`~/Developer/Media-Orchestrator`). It used to be private and tracked two credential
stores; both are now machine-local, untracked, and gone from history:

| what | where it lives now | tracked template |
|---|---|---|
| MEGA account pool (`user`/`pass`, ~822 remotes) | `Media-Syncer/rclone.conf` (root `.gitignore`) | `Media-Syncer/rclone.conf.example` |
| machine paths + `JELLYFIN_API_KEY` + provisioner email | `.env` (root `.gitignore`) | `.env.example` |

**What changed, and what a future session must not undo:**

* `fleet_env.py` (repo root) loads `.env` with `os.environ` taking precedence. The four
  configs (`Torrent-Ingest/config.py`, `Media-Syncer/scripts/config.py`,
  `Title-Scout/config.py`, `YouTube-Downloader/ytconfig.py`) read machine-specific roots
  through it with generic `Path.home()` defaults; the absolute paths that used to be
  hardcoded in code are gone (audit tools derive from `config`, plists keep the username
  in their absolute paths and that is accepted).
* The nine Torrent-Ingest plists that need a Jellyfin key carry `__JELLYFIN_API_KEY__`;
  `Torrent-Ingest/startup.sh` substitutes the real value at install time from `.env`,
  `~/.config/api-keys/jellyfin_key`, or the environment. The installed agents in
  `~/Library/LaunchAgents` still hold the real key; re-running startup.sh reproduces them.
* `Media-Syncer/scripts/mega_accounts.py` **no longer commits or pushes** the pool conf —
  the provisioner appends to the untracked file and publishes it atomically to
  `~/.config/rclone/rclone.conf`. Do not restore the commit path.
* Guard: `scripts/test_no_tracked_secrets.py` (verify check #54) scans `git ls-files`
  for secret stores and credential-shaped values; `.githooks/pre-commit` unstages
  `rclone.conf`/`.env` and rewrites a live Jellyfin key to the placeholder.
* History was rewritten with `git filter-branch` (rclone.conf removed from every commit;
  the Jellyfin key string replaced with the placeholder). A force-push alone was NOT
  enough and was measured not to be: after the rewrite the old commits were still
  fetchable by SHA (`raw=200`/`api=200`), and this file itself named old commits. So the
  GitHub repository was deleted, a clean one created, and the project renamed to
  `Media-Orchestrator` (GitHub would not release the old name immediately). The old git
  objects are gone: `git fetch origin <old-sha>` answers `upload-pack: not our ref` and the
  old SHAs 404 under the new name. One residue is outside the owner's control: the OLD-name
  raw URL (`raw.githubusercontent.com/.../Media-Fleet/<old-sha>/...`) kept answering 200
  from Fastly's cache (`x-cache: HIT`, `max-age=300`) while the same SHA under the NEW name
  404s — a CDN entry only GitHub Support or time can clear (it was observed to expire after
  the TTL once the old name was vacated). The pre-rewrite bundle
  (`~/Developer/Media-Fleet-backups/Media-Fleet-prepublic-20260919-151123.bundle`) was
  deleted after this migration was verified — it contained the old credentials by
  definition, hence the rotation note below.
* **The local directory was renamed the same day** (`~/Developer/Media-Fleet` →
  `~/Developer/Media-Orchestrator`), and every absolute-path reference in the tracked tree
  was rewritten: launchd plists, `config.py`/`ytconfig.py` fallbacks, audit tools,
  `ship-fleet.sh`, `verify_fleet.sh`, the `.env.example` template and the docs. The
  `.env` itself was repointed too. Because the move happened while an identify run and the
  reaper drain were live (§2.2/§2.4 forbid bouncing either), a **temporary compatibility
  symlink `~/Developer/Media-Fleet -> Media-Orchestrator`** was left in place so the
  in-flight processes' already-loaded absolute paths keep resolving; the installed launch
  agents were refreshed from the renamed tree (with the Jellyfin key substituted) but
  deliberately NOT reloaded, so nothing running was disturbed. **Remove the symlink only
  after a reboot has every daemon running from the new path** (`ship-fleet.sh` cannot do
  it: `kickstart -k` restarts the LOADED definition, which still names the old path). The
  owner scheduled that reboot; **§12 is the closing checklist** — until it runs, the
  symlink is load-bearing. `~/Developer/.megaignore` was repointed to
  `Media-Orchestrator/...` AND excludes the old name, so MEGA neither syncs the alias nor
  re-engages the old churn paths.
* **Rotation is recommended** even though the tree is clean and the remote object store
  is gone. The old commits were publicly fetchable by SHA for the window the rewritten
  repo sat public (~15 min) and until Fastly's 5-minute cache expired after the deletion —
  a real, measured exposure window, not a theoretical one. Changing the MEGA account
  password(s) and regenerating the Jellyfin API key is the only complete mitigation;
  nothing in the fleet breaks if the key is rotated and `.env` + the installed plists are
  updated with it.

This work was shipped without a fleet restart (`scripts/save-and-push.sh`): `.env` carries
the exact values the code previously hardcoded, so the running daemons see no change.
`verify_fleet.sh` printed `ALL CHECKS PASSED` (54 blocking checks) after the extraction.

---

## 12. After the reboot: close out the Media-Orchestrator rename (owner-scheduled)

The owner rebooted on 2026-09-19 so launchd would re-read the refreshed agents. The
rename itself is DONE and pushed (`aa70cc5`); `ship-fleet.sh` cannot put it into effect
because `kickstart -k` restarts the LOADED job definition, which still names the old path
(measured: `program = .../Developer/Media-Fleet/...`). The reboot also bounced the
in-flight Smurfs identify and the reaper drain — the owner knowingly waived §2.2/§2.4 for
this one boot, so this checklist is about proving recovery and finishing the rename, **not
deploying again**.

1. **Every daemon must be on the new path before anything is removed:**
   ```bash
   pgrep -fl 'Developer/Media-Fleet'                      # MUST be empty
   pgrep -fl 'Developer/Media-Orchestrator' | wc -l
   ```
   If a process still shows the old path, its loaded definition was not refreshed —
   reload just that agent (`launchctl bootout gui/$(id -u)/<label>`, then `launchctl
   bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`) rather than rebooting
   again.
2. **Remove the compatibility symlink** (only once step 1 is empty):
   ```bash
   rm /Users/mikeyferguson/Developer/Media-Fleet     # plain rm — no -rf, no trailing slash
   ```
   `rm -rf Media-Fleet/` with a trailing slash can follow the link and delete the real
   repo. Leave the `-p:Media-Fleet` line in `~/Developer/.megaignore`; it is now insurance.
3. **Prove the fleet healthy:** `bash Torrent-Ingest/scripts/verify_fleet.sh` must print
   `ALL CHECKS PASSED`; `fleet_doctor --once --dry-run` and `fleet_health --once` clean or
   naming only §7/§10 known items. After boot the mount and Jellyfin take ~2 minutes to
   re-prime — wait for `~/MediaLibrary/Shows` to repopulate and `/Items/Counts` to answer
   before calling anything broken (§8.7).
4. **Reaper recovery** (the reboot interrupted the drain): confirm it restarted
   (`pgrep -f 'Torrent-Ingest/reap.py'`) and that Media-Syncer was not left paused. A
   lingering `state/reap_ms_paused` marker is self-healing — the next reaper cycle
   resumes Media-Syncer and clears it; check `media_sync.log` for the resume rather than
   removing the marker by hand. The drain restarts its probe from the beginning; that is
   the reboot's cost, not damage.
5. **Smurfs/identify recovery:** `6c413306…` was mid-retry and the wave keeps its bytes;
   confirm the journal advances and `state/tmp/6c413306…_plan.json` eventually appears
   (decisions.log). A killed run costs provider budget (§2.4) — nothing to repair by hand.
6. **Rotation is still outstanding from §11:** regenerate the Jellyfin API key and change
   the shared MEGA password. For the key: update `.env`, then re-run
   `bash Torrent-Ingest/startup.sh` at a quiet moment (it re-substitutes the key into the
   installed plists and reloads the Torrent-Ingest agents). Check `pgrep -f ai_runner.py`
   is empty first.
