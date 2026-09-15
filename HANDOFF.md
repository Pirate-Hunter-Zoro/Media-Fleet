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
| `verify_fleet.sh` | **ALL CHECKS PASSED**, 48 blocking checks |
| `fleet_doctor` | 1 finding: the 7-item curation warning below; **no YacReader finding** (08:21 report) |
| `fleet_health` | 1 warning (7 library items need review; no action) (08:26 report) |
| `media_doctor` | 1 show flagged: Doctor Who (1963), 7 misfiled items; 0 pending human/AI review (07:57) |
| `library_health.txt` | 7 placement faults in Doctor Who (1963) -- the misfiled Marinus/Aztecs sets. The check now says "believe the `.nfo`", and each fault names the file's true slot (07:57) |
| Repo | **one monorepo** at `~/Developer/Media-Fleet`, pushed to `Pirate-Hunter-Zoro/Media-Fleet`; clean @ `ca45307` + the commit that carries this section |
| Jellyfin | 308 series, 18,852 episodes, 446 movies |
| Mount | Shows 307, Movies 2,673, Comics 9 — primed and serving |
| `library.db` | 23,146 owned rows at 08:35 (not re-audited this session; the 2026-09-14 redundant-norm audit stands) |
| YacReader | scan-at-startup flags **on**, supervisor enforcing them. The 2026-09-15 pop-up loop was a **false positive in our comparison, not the app**: the index stores NFC (`Nausicaä` = U+00E4) while APFS/FUSE and the pool inventory hand out NFD, so `unindexed_files` reported two already-indexed volumes forever; `fleet_doctor` then bounced the app every 15 min and the supervisor activated its window every 60 s (attempt 89). Both sides now compare in NFC, activation is capped at 2 per start, and the app is launched with `open -g` (no focus theft) -- both-ways tests are in `test_yacreader_index_shape.py` / `test_supervisor_yacreader.py` |
| In flight | chunked **Doctor Who (1963)** pack (`e099421feeda`) **stuck on wave 104** in a provider walk (empty text / rate limit / loop breaker) -- the model cannot reconcile the release's serial numbers with the corrupted library; chunk_done 106, active [104-135]. **Smallville (2001)** (`04cf0a35`) downloading its early waves; the recovered magnet is working. Reaper running (check idle before bouncing). No identify run should be killed except deliberately (see below) |
| Open work | **the Doctor Who (1963) renumber + missing-part re-fetch — see the subsection below.** Everything else is clear -- read §7 before reading that as "nothing is wrong" |

### Open work — renumber Doctor Who (1963) and re-fetch the dropped parts (`e099421feeda`)

**What went wrong.** The 26-seasons XVID pack names its parts `Doctor Who - S01E05 (005) - The Keys of Marinus (1) …`, where `S01E05` is the release's **serial** number. The identify runs copied those numbers instead of continuing the library's per-part run (An Unearthly Child Parts 1-4 were already E01-E04), so all six Marinus parts were planned at `S01E05`, all seven Daleks at `S01E02`, and so on. Worse, `library._collapse_existing_episode_collisions` then dropped every planned part whose wrong number collided with an already-filed episode, and `_advance_chunked` freed their bytes as "not in plan (junk)": **17 downloaded parts (Daleks 7, Edge of Destruction 2, Marco Polo 7, plus the Marinus bonus) were deleted unfiled.** Later waves then numbered around the collision, shifting the rest of the run.

**Already fixed — do not re-fix.** `library._reject_same_episode` now refuses a plan that puts two distinct video files on one episode of one show in a regular season (keyed by show, Season 00 exempt); the identify prompt gained the "a repeated `SxxEyy` with `(1)/(2)/Part N` is a STORY number" rule; the replay over 820 accepted journal plans rejects zero of them. Shipped `355ce43`/`ca45307`, with the mechanism written up in README § Placement guards.

**The target state (TheTVDB's part-sequential numbering, confirmed by Jellyfin's own scrape: `S01E07` = The Escape, `S01E18` = Rider from Shang Tu, `S01E31` = Strangers in Space):**

| now on disk | really | action |
|---|---|---|
| `S01E01-E04` An Unearthly Child | S01E01-E04 | correct, leave |
| `S01E05` x6 The Keys of Marinus | S01E21-E26 | re-file to the sidecar's `<episode>` (each `.nfo` already says 21-26) |
| `S01E06` x3 The Aztecs | S01E27-E29 | re-file; check whether part 4 (`The Day of Darkness`) was dropped too |
| `S01E07-E12` The Sensorites | S01E31-E36 | re-file |
| `S01E13-E20` The Reign of Terror (incl. intro/outro) | S01E37-E42 (intro/outro are extras) | re-file; decide the two extras |
| `S01E21-E23` Planet of Giants | **S02E01-E03** | cross-season re-file |
| `S01E24-E29` Dalek Invasion of Earth | **S02E04-E09** | cross-season re-file |
| `S01E30-E31` The Rescue | **S02E10-E11** | cross-season re-file |
| `S02E12-E30` (The Romans onward) | S02E12+ | correct (verified `S02E12` = The Slave Traders), leave |
| missing: Daleks E05-E11, Edge E12-E13, Marco Polo E14-E20 | | clear their indices and re-fetch from the still-registered torrent |

**Why this is a reviewed operation, not a shell loop.**
* The files are replicated to MEGA (`remote_inventory.json` has 188 Doctor Who lines), so a local rename desyncs the pool. `scripts/refile_season.py` is the reviewed precedent: the move set comes from the record/plan evidence, sidecars are deleted (Jellyfin regenerates them), and it is resumable and verifiable. Per-file renumbering has no tool yet; extending that one is the intended path.
* The record's bookkeeping must be edited in step with the bytes: `chunk_filed` maps 64 indices to library paths, `chunk_done` is 106, `chunk_dropped` is 42. A missing part will never be re-fetched while its index sits in `chunk_done`/`chunk_dropped` — `_carry_chunk_progress` carries those as proven. Clear and re-arm exactly the indices being re-fetched.
* **Park the torrent before touching the library.** Wave 104's identify is currently spinning; repairing underneath it races the next wave. Stop the daemon or use the chunked park path, and never bounce the reaper mid-drain (`pgrep -f 'Torrent-Ingest/reap.py'`).
* After: `media_doctor`/`library_health.txt` must show zero placement faults, then unpark and let the remaining waves file under the new guard + prompt.

**Related hazard, not yet actioned:** `_collapse_existing_episode_collisions` drops silently and the wave then deletes the bytes; that is only safe when the existing file really is the same episode. The new guard removes the observed trigger, but the drop path still has no content check. If a future run sees a same-slot drop with a different `episode_title`, treat it as a placement fault and fail the plan rather than dropping it.

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
