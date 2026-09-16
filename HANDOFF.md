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

**One git repository at `~/Developer/Media-Fleet`** — the five projects are directories in
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
   deploy.** It is the gate. 47 blocking checks.
4. **Never deploy while an identify run is in flight** — `pgrep -f ai_runner.py`. The run is
   a subprocess of the daemon; a deploy kills it *and* the provider's daily budget with it.
5. **The model PROPOSES, the harness DISPOSES.** `library.validate_plan` re-derives every
   destination and rejects a bad plan whatever wrote it. **Do not weaken that seam to make a
   model's answer fit.** If a plan is being rejected, the plan is usually wrong.

**Deploying:** `bash ~/Developer/Media-Fleet/ship-fleet.sh "what changed"` (or `bash
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
bash ~/Developer/Media-Fleet/Torrent-Ingest/scripts/verify_fleet.sh          # must say ALL CHECKS PASSED
python3 ~/Developer/Media-Fleet/Torrent-Ingest/scripts/fleet_doctor.py  --once --dry-run
python3 ~/Developer/Media-Fleet/Torrent-Ingest/scripts/fleet_health.py  --once
```

`media_doctor` needs Jellyfin credentials that live in its launchd plist, not your shell —
without them it silently does "mechanical sidecar work only" and its clean result means
nothing:

```bash
JELLYFIN_URL=http://127.0.0.1:8096 \
JELLYFIN_API_KEY="$(plutil -extract EnvironmentVariables.JELLYFIN_API_KEY raw \
    ~/Library/LaunchAgents/com.mikeyferguson.mediadoctor.plist)" \
python3 ~/Developer/Media-Fleet/Torrent-Ingest/scripts/media_doctor.py --once --dry-run
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

## 6. State, verified 2026-09-15 08:35 CDT

Every number below was measured, not estimated, except the `library.db` row (marked).

| | |
|---|---|
| `verify_fleet.sh` | **ALL CHECKS PASSED**, 50 blocking checks |
| `fleet_doctor` | the 7-item curation warning WAS the Doctor Who misfiles; the reports refresh on their own schedule |
| `fleet_health` | same warning, no action |
| `media_doctor` | Doctor Who (1963): **zero placement faults** after the repair; only title-quality items (bare names awaiting Jellyfin's scrape / the revert-guard) |
| `library_health.txt` | the 07:57 copy still lists the misfiles until the next `mediadoctor` pass; the faults it names are repaired |
| Repo | **one monorepo** at `~/Developer/Media-Fleet`, pushed to `Pirate-Hunter-Zoro/Media-Fleet`; clean @ `33bb287` + the 2026-09-15 changes being shipped |
| Jellyfin | 308 series, 18,852 episodes, 446 movies |
| Mount | Shows 307, Movies 2,673, Comics 9 — primed and serving |
| `library.db` | 23,153 owned rows at 09:50 (the repair superseded 15 wrong-slot rows and recorded 34 corrected files) |
| YacReader | scan-at-startup flags **on**, supervisor enforcing them; the NFC false positive was fixed 2026-09-15 (`test_yacreader_index_shape.py` / `test_supervisor_yacreader.py`) |
| In flight | chunked **Doctor Who (1963)** pack repaired twice: 34 wrong-slot files re-filed, the re-fetch misfiles cleaned (0 wrong placements now), 8 indices re-armed for re-fetch, and the serial-numbering guard now binding on every plan. Wave 184 re-identifies when `torrentingest` is next started. **Smallville (2001)** (`04cf0a35`) downloading its early waves. Reaper running (check idle before bouncing) |
| Open work | **none from the 2026-09-15 list — both queued tasks shipped** (below). Everything else is clear — read §7 before reading that as "nothing is wrong" |

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
