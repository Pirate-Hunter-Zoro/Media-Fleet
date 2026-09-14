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

## 6. State, verified 2026-09-14 06:35 CDT

Every number below was measured, not estimated.

| | |
|---|---|
| `verify_fleet.sh` | **ALL CHECKS PASSED**, 47 blocking checks |
| `fleet_doctor` | 0 findings |
| `fleet_health` | all clear (20:42 report) |
| `media_doctor` | 0 shows flagged, 0 pending human/AI review |
| `library_health.txt` | "All shows healthy. Nothing to fix." (19:33) |
| Repo | **one monorepo** at `~/Developer/Media-Fleet`, pushed to `Pirate-Hunter-Zoro/Media-Fleet`; clean @ `47ebd43` |
| Jellyfin | 301 series, 18,395 episodes, 444 movies |
| Mount | Shows 300, Movies 2,663, Comics 9 — primed and serving |
| `library.db` | 22,957 owned rows, 22,957 distinct, **0 redundant**. 25 comic/manga norm pairs → 10, all folded to the kind the pool shelves them under; the 10 that remain have no pool files to judge (the reaper now runs this after every purge) |
| YacReader | scan-at-startup flags **on**, supervisor enforcing them, and `fleet_health` now reports the reader's own state (crash rows, damage, flags, shelf files the index lacks) with `fleet_doctor` remedies for the two safe repairs. The ElfQuest reindex was scanning at hand-off (pool reads ~3 MB/s, 166 shelf files still behind); the 2026-09-13 `FolderModel::reload` crash was a malformed folder tree it had written itself -- `load_order_faults` names that shape, the repair fixes it, and `comic_shelf_audit` now deletes with `PRAGMA foreign_keys=ON` so it cannot be created again |
| In flight | no identify run at deploy time; reaper running (queue empty); YacReader's startup update scanning. The held full-fleet restart from the 2026-09-13 direct-ingest ship went out with this deploy |
| Open work | **none queued.** Read §7 before reading that as "nothing is wrong" |

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
