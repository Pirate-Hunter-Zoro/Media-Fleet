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
   deploy.** It is the gate. 54 blocking checks.
4. **Never deploy while an identify run is in flight** — `pgrep -f ai_runner.py`. The run is
   a subprocess of the daemon; a deploy kills it *and* the provider's daily budget with it.
5. **The model PROPOSES, the harness DISPOSES.** `library.validate_plan` re-derives every
   destination and rejects a bad plan whatever wrote it. **Do not weaken that seam to make a
   model's answer fit.** If a plan is being rejected, the plan is usually wrong.
6. **A fix is real only where the owner can see it.** A log line, a worklist entry, a green
   test or a "shipped" commit is not acceptance. The artifact the owner uses — the YacReader
   shelf/index, the Jellyfin API, the bytes on disk (md5/counts) — must be checked after the
   repair, and the check output pasted into the commit message, before anything is called
   fixed. The five faults in §10.0 survived earlier sessions because "fixed" was claimed
   from logs. A session that cannot show the owner-visible before/after has not shipped.

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

## 6. State, verified 2026-09-20 08:00 CDT

Rows marked **measured** were verified this session (the owner's five, §10.0).

| | |
|---|---|
| `verify_fleet.sh` | **ALL CHECKS PASSED**, **60 blocking checks** (2026-09-20 evening, after 10.3/10.4/10.5/10.9, the owner report, the library-wide rewrite and the hard-coding guard; 6 checks added) |
| `fleet_doctor` / `fleet_health` | not re-run this session; §10.8 is the acceptance list |
| `media_doctor` | series-level identity + title art are now scanned (`series_identity_stale`/`series_art_stale`/`episode_slot_missing`); **TZ (2019) repaired live** — folder.jpg `965f20be…`, landscape.jpg `ec122588…` (neither Too Cute hash), tvshow.nfo `premiered 2019-04-01`, `tvdbid 358915`, `enddate 2020-06-25`, item locked, 2 seasons / 20 indexed episodes / no ghost season (§10.3) |
| Repo | one monorepo at `~/Developer/Media-Orchestrator`; this session's work on top of `a3104b0` + the doc-update HANDOFF, plus the 2026-09-21/22 commits through `81d07d7` |
| Stall policy | **one 24h deadline, partial bytes kept — shipped `c9fd3aa`, deployed 2026-09-23 20:08 CDT.** `_abandon_stalled` no longer reads `availability < 1` as "no complete copy in the swarm" (it is a connected-peers fact and reads < 1 during every stall); `STALL_ABANDON_NO_COMPLETE_SEC` is gone; an abandon calls `qbt.remove(delete_files=False)` so a re-drop resumes. The four Bob's Burgers packs the owner moved back are downloading again (S02 27%, S06 10.8%, S01 stalled with 4 complete peers known, S03 parked between waves) |
| Jellyfin | 313 series, 20,052 episodes, 448 movies (last counted 2026-09-19) |
| Mount | **One Piece, session 2 (2026-09-20 evening):** the franchise layout is live — `Manga/One Piece/One Piece/` (189 files) and `Manga/One Piece/Ace's Story/` (2), the old flat master and `One Piece - Ace's Story/` gone. 12 junk chapters purged (covered repeats c1080/1088/1098/1112/1133, the six bare `cNNNN.cbz` the old mislabel repair created, the nested `c1176` duplicate); 5 One Piece chapters misfiled into Jujutsu Kaisen purged as covered (JJK ends at 272 chapters, One Piece v108-v111 own them). Sessions' older rows (§10.0 rows 1–3) remain true. |
| `library.db` | colour-aware comic identity is live (`item_key` includes `colored`; no `MAX(colored)`). The One Piece renames recorded `cNNNN` chapter rows and superseded the old volume rows; every purge's DB mirror runs via `dbhook.record_purge` from the reconciler/reaper |
| YacReader | open (Comics), hidden, 30-min self-update. The migration moved 191 files, so the index is catching up; `yacreader_rescan.py --apply` was run and the supervisor refreshes it. Re-check `--files` after the next update |
| In flight | **The 70-file Smurfs re-fetch drop is in `DirectIngest/The Smurfs (1981)/` waiting on free-provider capacity** (Gemini 429, several OpenRouter models return empty text, nvidia calls take tens of minutes). One nvidia plan was rejected correctly (a real one-slot disagreement at S07E15) and the chain continues; the drop is local and safe and files when a provider serves. The queued One Piece chapter drops are filing through the same constrained chain; the Jujutsu Kaisen misfiles those produced were purged and the plan-time guard that refuses them is live (`library.validate_plan` chapter-ceiling). The reaper is draining (`reap.py` PID 1311) |
| Parked re-drops | **Smurfs row 5 repaired live 2026-09-20 (session 2), fetch completing.** The run had three real defects, all now fixed and guarded: (a) the title map was computed against **TVMaze while Jellyfin scrapes TMDB** — 77 files were placed one slot away from the name the owner sees; (b) the intra-torrent collapse silently dropped 32 mapped files when an unmatched file's release number collided with a mapped file's computed slot; (c) **media_doctor's duplicate rule deleted the planner's `S01E01.mp4`-class files at 38 S01 slots**, because a same-stem pair shares ONE `.nfo` and the rule never asked the journal. Live repair: all 375 surviving files refiled to their TMDB slots in one ordered 147-move pass (`refile_season.py --mapping`), 142 `library.db` rows superseded, inventory/sync_state rewritten; the 30 dropped episodes + the 40 deleted S01 pack files are re-fetching from the pack's own torrent into `DirectIngest/` (see the `In flight` row). The old dvdrip S01 files now sit at their correct slots (E31 = The Smurfette) as the fallback content. |
| Open work | The Smurfs re-fetch (70 files) is downloading from the swarm; when it lands, the 40 dvdrip S01 files are superseded through the mount and the drop files through the fixed pipeline. Doctor Who (2005)'s S00E04 two-file placement fault is the doctor's KNOWN NEEDS-REVIEW item (its locked nfos claim E16/E149 and both slots are occupied — needs a human decision, not an auto-move). Toriko's 8 blank plots were filled 2026-09-20 through `repair_metadata.py --no-ai` (0 blanks now). The free-AI upgrade (§10.10) is implemented. |
| Pending after reboot | §12: rename close-out verified done; **rotation (item 6) still outstanding** |

### Shipped 2026-09-23 — a stall is not a swarm verdict, and the partial bytes stay

**The owner's report.** Four recent failures — `Bob.s.Burgers.S01E01-13` (45% at failure),
`S02E01-09` (22%), `S03E01-23` (1%, chunked), `S06E01-19` (0%) — were moved out of
`failed/` and re-queued, and would "likely fail again". Each journal line reads
`stalled Nh with no progress (no seeders/peers); abandoned to release the download budget`
(4h/6h/7h/8h).

**Root cause (measured, not inferred).** `_abandon_stalled` selected a 4-hour deadline
whenever qBittorrent reported `availability < 1.0`, on the premise in `config.py` that this
means "NO complete copy anywhere in the swarm". It does not: availability is computed from
the peers this client is *currently connected to* plus its own pieces, so during any stall
it collapses to our own completion fraction and reads < 1 even in a swarm full of seeders
(live: a stalled S04 at 5.69% progress reported availability 0.056 — exactly its own
fraction). Every stalled torrent therefore took the 4h path. The four packs were paused by
an ordinary overnight seeder gap (S01 had gone 1% → 21% → 45% over the day) and were killed
the moment the daemon next reached them — after an identify run that blocks the sweep for
hours. Worse, the abandon called `qbt.remove(delete_files=True)`: the partial payload was
deleted with the torrent, so every re-drop restarted from zero and stalled again — the loop
the owner saw. `_fail`'s own message promises "local download left for inspection"; every
other failure path leaves the bytes; this one did not.

**Measured cost (journal + `~/Library/Logs/TorrentIngest.log`).** 48 stall-abandons in the
journal; 35 of them are still in the current log with a recoverable progress line; 6 had
partial data deleted, **7.40 GB** in total — S01 3.56 GB (45%), Croods S06 2.26 GB (83%),
S02 1.24 GB (22%), FMA Brotherhood E58 0.32 GB (36%), Dropkick E06/E10 0.02 GB. Not one of
those hashes ever completed (they were never automatically retried). The old code also took
one **more** victim in the gap between this fix being pushed and the daemon restart that
afternoon — `Bob.s.Burgers.S14` at 12:30 CDT (`stalled 5h`), its partial bytes deleted; it
is chunked now and re-fetching. That gap is exactly the standing rule's hazard, and the
owner approved the restart with a run still in flight rather than wait hours for a chain
that never leaves the identify phase.

**The fix.** One deadline, selected by peer activity alone: `STALL_ABANDON_SEC` (24h) from
the later of qBittorrent's `last_activity` and a chunked wave's `wave_started_at`.
`STALL_ABANDON_NO_COMPLETE_SEC` is deleted (an unused guard is a thing someone turns back
on later). The abandon now removes the torrent with `delete_files=False` — a re-drop of the
same source resumes from the bytes on disk, and `artifactjanitor` reclaims a directory that
is never retried after its own 7-day grace. The rationale lives on `_abandon_stalled` and
`config.STALL_ABANDON_SEC`, and `scripts/test_chunked_stall_clock.py` pins both directions
plus byte preservation: a fresh wave survives a stale clock; a wave idle past 24h is still
abandoned; a non-chunked 8h stall at 45% with `availability 0.45` and no seeder known is
**kept** (the exact 2026-09-23 shape); every abandon passes `delete_files=False`.

**Why this is not the Tailscale bind.** qBittorrent binding to the `100.79.20.10` CGNAT
address and riding the rotating Mullvad exit node is the documented design (`tailscale_up()`
gates downloads on it; Media-Syncer rotates the exit for MEGA throttling). These are
DHT-only magnets with no trackers (`.torrent` `announce` absent, `url-list` empty), so
seeder gaps of hours are normal — which is exactly what the 24h window and the kept bytes
are for. No VPN or qBittorrent configuration was changed.

**Acceptance (owner-visible).**
* `python3 scripts/test_chunked_stall_clock.py` → `ALL CHECKS PASSED.` (17 checks: the three
  chunked-clock parts, the non-chunked regression, byte preservation both paths, controls).
* `bash scripts/verify_fleet.sh` → `ALL CHECKS PASSED.`
* Pushed as `c9fd3aa`, then deployed **2026-09-23 20:08 CDT** with
  `Torrent-Ingest/scripts/ship.sh` per the owner's explicit "restart now" (the identify
  chain had no gap; see the S14 note above). New daemon PIDs: `torrentingest` 19764,
  `directingest` 19772, `driveingest` 19785; 0 tracebacks in `TorrentIngest.log`; the
  reaper (PID 6539) was not in the script's label set and was left draining.
* The four were admitted 11:53 CDT and survived the old code to the deploy (the identify
  chains blocking the sweep is what let them; nothing new was abandoned). Live state
  measured after the restart: **S02 27.0% with `availability 2.27`, `num_complete=2`,
  2 seeds connected; S06 10.8% with `num_complete=2`; S01 1.2% `stalledDL` with
  `num_complete=4`** — a stalled torrent with four complete peers known, which is exactly
  the shape the old 4h rule called "no seeders/peers" and would have destroyed again;
  S03 parked between chunked waves (0/23 filed, next wave enables when it fits). No
  `stalled`/`abandon` line appears in the log after the restart. The S01/S02 bytes they had
  before the first failure were already gone (deleted by the old code), so they re-fetch
  from zero; anything fetched from now on survives a retry.

### Shipped 2026-09-20 — the five computed facts, the verified self-heal, the scalable plan

Six registered tests (check #55–#60): `test_provider_id_verify.py`,
`test_series_identity_heal.py`, `test_manga_mislabels.py`,
`test_release_title_numbering.py`, `test_metadata_heal.py`, and
`test_no_incident_hardcoding.py` (no machine paths or digests in executable string
literals -- comments and docstrings may still name incidents). The report
`scripts/verify_owner_report.py` prints the owner-visible acceptance per §10.0 and is
advisory in `verify_fleet.sh` (a parked Smurfs or a draining purge must not gate).

**10.3 — a provider id is verified before it can pick art.** `tmdbguide.show_identity`
fetches the provider record (`/tv/{id}` + `/external_ids`); `library.verify_provider_ids`
(now called by `identify` on the model's plan, before `validate_plan` — deliberately not
inside `validate_plan`, which tests and offline tools call without network) strips a tmdb
id that answers 404 or names a different year (>1), and a tvdb id that disagrees with
TMDB's own mapping. A network error fails open. Live: TZ (2019)'s `tmdb_id 80979`
(*Too Cute*) / `tvdb_id 325542` would have been stripped at filing.

**10.3 — media_doctor owns series identity and title art.** The trigger is computed:
`premiered` vs TMDB `first_air_date` (a stale `<year>` with a matching premiere is
harmless — four live shows carry that shape), now plus `enddate` vs TMDB's
`last_air_date`: Jellyfin's saver re-stamps `enddate` from its DB and the field is not
lockable, so without that trigger the TZ `enddate` silently reverted to Too Cute's
2013-03-06 beside the corrected 2019 premiere (repaired live, and the item's `EndDate`
is now set too). The repair re-matches the Jellyfin item
FIRST (`RemoteSearch/Apply`, order measured: a `replaceAllMetadata` refresh after the nfo
write made Jellyfin rewrite tvshow.nfo and the S02 nfos from its stale DB rows), syncs
season items, locks the item (`LockData`, valid `LockedFields` only), then writes nfos
LAST and replaces `folder.jpg`/`landscape.jpg`/`seasonNN-poster.jpg` with the verified
identity's art. `_write_nfo_title` now always emits/repairs `<season>`/`<episode>`, and
null Jellyfin indexes are set on the item DTO.

**10.5a–e — the manga tiers are computed from the archives.** New `comicfacts.py` reads
volume associations (`(vNNN)`), chapter markers (`cNNNN`/`dNNNN`, bare `1176-001`),
edition (`[Digital CC] [PZG]` vs `[VIZ Media] [1r0n]`) and the mislabel shape from the
entries themselves, cached in `state/comic_archive_facts.json`. `manga_volume_map`
persists `total_volumes`/`total_chapters`/`shelf_volumes`/`shelf_ceiling` and merges the
shelf's exact sets over MangaDex; `comicfacts.ceiling_for` gives the plan-time ceiling
(the larger of AniList's total and the shelf's). `validate_plan` refuses a `vNNNN` above
the ceiling whose archive is chapter pages, and refuses a grey copy superseding a
coloured file. `librarydb.item_key` is colour-aware for volume/chapter (two rows for two
editions; `mark_superseded`/`_supersede` with unknown colour match grey only — a grey or
unknown file may never kill the coloured row). `chapter_volume_reconcile` reads colour
from `comicfacts` and lets a surviving coloured volume cover chapters; a coloured chapter
is still only superseded by a coloured volume. The One Piece franchise row is in
`config.COMIC_FRANCHISES` (Ace's Story nests; a flat master that already holds the
master's own files stays flat until the owner decides on the migration), and
`resolve_comic_folder(colored=True)` no longer bypasses the table. `identify.md` and
`README.md` teach the current layout.

**10.9 follow-up (2026-09-20 afternoon).** The generic owner report lists the Smurfs collapse first, then three
COMPLETED legacy releases whose plans collapsed destinations: `Pokemon Horizons 115-123
English Dub` (1), `[Deadmau-RAWS] Shokugeki.no.Souma.OVA.2016-2018` (5), `[MTBB]
Monogatari Series Off & Monster Season S1` (1). These predate the merge collision guard
and may be benign same-episode duplicates or the Smurfs disagreement shape; each needs a
content-verify pass (`probe` the survivors against the guide) before being dismissed.
The Smurfs pack itself is the 32-collapse case already recorded in §10.0 row 5.

**10.9 — release-order packs compute their broadcast numbering, and plans scale.**
`identify.release_title_map` matches each `SxxEyy (Title)` filename against TVMaze and
returns the broadcast slots only when they actually differ; the block is stated in the
prompt as fact and `validate_plan(..., title_map=)` refuses a copied release number. Above
`IDENTIFY_SKELETON_MIN_FILES` (150) the harness writes a deterministic skeleton of every
release file (all computed slots) and the prompt names it; `ai_client` now checks plan
coverage against a `--require-list` manifest before the run ends and asks for the exact
missing slice twice. The coverage guard still parks as the last line of defense. Measured
on the Smurfs pack: 362/405 titles matched, 347 differ, `S01E01 (The Smurfette)` ->
`S01E31`, 409-file skeleton (49.7 KB).

**10.4 — metadata self-heal sees the mount and verifies its writes.**
`audit_metadata.shows_root()` is the mediafs mount (Toriko's 147 videos are evicted from
`~/Media`, which is why the audit found 0 episodes); `repair_metadata._guide_index`
accepts name-only TVMaze rows (Toriko: 146 names, 0 summaries); `_fill_synopses` fills
plots from TMDB episode overviews (49 available) without overwriting the provider title;
`media_doctor.escalate()` snapshots `(blank_plots, junk_titles)` before the AI run and
charges `escalate_n` only when the sidecars actually improve.

**10.10F — `scripts/verify_owner_report.py` is library-wide, not incident-coded.** The
first cut named the five artifacts (a folder, two poster hashes, the Smurfs info hash);
that is evidence, not a check, and it cannot see the same fault anywhere else. It now
computes the invariants across the whole library: no `tvshow.nfo` whose `enddate`
precedes its premiere; no `vNNNN` mislabel, no chapter covered by an owned volume, and
no volume in both editions across EVERY two-tier manga series (the reconciler's own
functions, so it cannot disagree with an apply); and the journal's completed records
whose destinations were collapsed (content-verification review) and terminal records
parking unaccounted files. Queued purges report PENDING; already-purged paths report PASS while the inventory catches up; only bytes still visible through the mount or on the SSD fail. Read-only, exit 0
always, advisory in `verify_fleet.sh`. Its first run surfaced **the Smurfs collapse plus three legacy ones**
the incident-coded version could not see — see §10.9's note.

### Shipped 2026-09-20 (session 2) — the map follows Jellyfin, duplicates need proof, collisions see the mount

Three tool fixes, each with a registered test (`verify_fleet.sh`: check #61
`test_release_title_numbering.py` extended, #62 `test_duplicate_identity.py`, #63
`test_existing_collision_identity.py`; the direct-ingest release list is Part 6 of
`test_direct_ingest_media.py`). The full suite prints `ALL CHECKS PASSED`.

**The title map is computed against the provider Jellyfin SCRAPES.** `tmdbguide.episode_names`
(fetched, cached 7 days) now feeds `identify.release_title_map` whenever the library
folder pins a `tmdbid`; `epguide` (TVMaze) is the fallback. THE MEASURED DISAGREEMENT:
The Smurfs' *Locomotive Smurfs* is TVMaze S07E41 and TMDB/Jellyfin S07E43; TVMaze's S09
is one slot short of TMDB's three-part opener; the four specials live in TMDB's S00.
The July/September map placed 77 files one slot away from the title the owner sees, and
Jellyfin's own `.nfo` titles (rewritten from the scrape) sat beside the wrong episodes.
The matcher also gained a character-ratio pass with a part-digit rule
(`Wild Side - pt1` -> `(1)`, pt2 -> `(2)`; a digitless query against several parts gets
NO claim) and a collision-safe skeleton fallback: an unmatched file whose release number
equals a matched file's computed slot is marked needs-mapping, never filed there.
Replay: 15 titled-release plans in the journal, 0 contradictions. And
`library._reject_title_numbering` now checks the DESTINATION slot, not the model's
optional `season`/`episode` fields: on the re-fetch drop the model wrote the correct
`S01E06 -> S01E01.mp4` while leaving `(1, 6)` in those fields, and the guard rejected
its own computed slot (live, 2026-09-20). The fields are an annotation; the destination
is what gets filed.

**A same-stem duplicate is deleted only when the journal PROVES it is one.**
`media_doctor._classify_slot_collision` asks `journal.source_titles()` (the source title
each destination was filed from) when the two files share one `.nfo`: different
identities are a `misfiled_episode` (reported, never deleted), unknown identity is
reported, and only a proven same-episode pair may be auto-deleted. This is the fix for
the loss this session found: the old rule kept the higher-ranked container, deleted the
planner's `.mp4` at 38 Smurfs S01 slots, and left the older wrong-slot `.mkv`.

**A same-slot collision now sees the mount and checks identity.** `_collapse_existing_episode_collisions`
scans `MEDIAFS_MOUNT` as well as `MEDIA_ROOT` (an evicted episode was invisible, so the
replacement pack applied beside the pool-only dvdrip files), and when the journal says
the existing file's content is a different episode it raises `PlanError` — the release
parks, it is never a silent drop or an overwrite. Same-episode and unknown-identity cases
keep the historical collapse, so no new parks for ordinary work.

**The repair, through the tools.** `refile_season.py --mapping` (now accepting
comma-separated `--record` hashes and rewriting the plan's `dst_rel` alongside
`applied`/`chunk_filed`): 147 moves, 0 failures, 290 stale sidecars deleted, 142 db rows
superseded. `journal.source_titles()` is shared by the doctor and `library`, so a moved
file's identity follows it. Direct drops of 4+ videos are handed to `run_identify` as a
RELEASE (`direct_ingest._release_files_for`), so the same pack cannot file one way
through the torrent and another through `DirectIngest/`.

### Shipped 2026-09-20 (session 2) — the manga shelf: bare markers, wrong-series chapters, franchise layout

Owner report: odd One Piece chapters in Jujutsu Kaisen; covered repeats and bare
`cNNNN.cbz` files in One Piece; a nested One Piece folder with more repeats; and
`One Piece` + `One Piece - Ace's Story` should share a master. No hard-coded titles
anywhere: every rule is a computed fact.

* **`repair_manga_mislabels.py` created the bare markers.** Its rename wrote
  `c{ch:04d}{ext}` alone, so the seven `vNNNN` mislabels became `c1078.cbz`..`c1176.cbz`
  with no series. It now replaces the volume marker in the existing stem and prefixes
  the series label when nothing else names it (`One Piece v1176.cbz` -> `One Piece
  c1176.cbz`). `test_manga_mislabels.py` Part 5 pins it.
* **`library.validate_plan` refuses a destination that names only the marker**
  (`_BARE_MARKER_STEM`) and **a chapter above a FINISHED series' chapter total**
  (`comicfacts.chapter_ceiling_for`, from the persisted AniList total + status; ONGOING
  series have no bound). This is the guard that would have refused `Jujutsu Kaisen
  c1093.cbz` at plan time. `manga_volume_map` now persists `anilist_status`.
* **The reconciler learned duplicates, fractional chapters and wrong-series misfiles.**
  Same-number chapter copies collapse to the canonical, shallowest one (`c1151.cbz` +
  `c1151.5.cbz` no longer merge -- fractional chapters are distinct); a chapter above
  its folder-series' FINISHED total that another series' owned volume covers is purged
  as a redundant copy of that volume, otherwise reported. The coverage index is built
  from the WHOLE shelf even for a `--series` run. `series_label_for_rel` collapses a
  doubled master leaf, so `One Piece/One Piece/` is one series, not "One Piece One
  Piece". `test_manga_chapter_reconcile.py` covers all four directions.
* **The franchise layout is live**: `migrate_comics.sh --apply` moved 191 files
  (verified 191/191) into `Manga/One Piece/One Piece/` and `Manga/One Piece/Ace's
  Story/`, rewrote inventory/sync_state, and the empty old master/member folders were
  removed. The table row is the generator's evidence-based one (`build_comic_franchises`
  resolves it from the library; no title was typed in for this fix).

### Shipped 2026-09-21 — reconcile sees every witness: the One Piece re-queue storm

**Damage (measured live 2026-09-20 19:55–20:15).** The franchise migration moved 191 files
without rewriting the journal records that named them, and `reconcile.py` checked presence
against the inventory keys and `~/Media` (the SSD cache) only — the MOUNT and a moved key
both read as "gone". Every moved chapter completion was re-queued, re-downloaded and
re-filed on a loop, and the five chapters a volume already covered (**c1080, c1094, c1101,
c1120, c1122**) were re-fetched only for `chapter_volume_reconcile` to purge them again.
38 records carried the re-queue error; the re-identify runs those spawned were competing
with the Smurfs re-fetch for the same free-provider chain (`state/tmp` logs at 20:00–20:12
are re-queues, not new drops; openrouter was out of daily budget).

**The fix (three computed facts, plus a repair tool).**
* `reconcile._is_present_local` checks the MOUNT as well as the SSD — the module always
  said "local mount"; the code checked only the cache (HANDOFF §2.1).
* A moved file matches by CONTENT IDENTITY: basename + byte size, both carried by the
  inventory (`[account, timestamp, size]`). One unique candidate proves presence;
  ambiguous or different-size candidates prove nothing and are never guessed.
* `dbhook.purged_evidence` reads library.db's own supersede ledger (the read-only mirror
  of `_supersede_path`'s path→row mapping) and a completion whose files are gone from
  EVERY witness and deliberately superseded is CLOSED (`reconcile_closed`, plus the older
  `reconcile_dead` switch the running daemon honors) instead of re-acquired.
* `scripts/repair_journal_paths.py` applies the same facts to records that drifted before
  the fix: rewrites `applied`/plan/`chunk_filed` to the unique moved key and closes the
  all-missing-and-superseded ones. It never touches a record that still holds a file.

**Acceptance (owner-visible; pasted from the live run).**
* `bash scripts/verify_fleet.sh` → **`ALL CHECKS PASSED.`** — the new check is
  `test_reconcile_presence.py`, which proves both directions and replays the live journal:
  `completed records audited: 330 / present: 325 / deliberately superseded: 5 / genuinely
  missing: 0 / the path-only check would have re-queued: 5`.
* `python3 scripts/repair_journal_paths.py --apply`:
  `APPLIED: 0 record(s) rewritten, 5 closed as superseded, 17 stale error(s) cleared,
  312 already exact, 13 unresolved`.
* The five closed records are gone from the mount (`find
  ~/MediaLibrary/Comics/Manga/One Piece -name '*cNNNN*'` → nothing) and the reaper purged
  their pool copies (`state/reap_purges.log`/`Media-Syncer/torrent_reap.log` lines for
  flat and nested c1094 at 07:09/10:46/12:39/19:57); their library.db rows are
  `superseded`. The journal's last line for each is `completed` with
  `reconcile_closed: superseded` and no new re-queue after 2026-09-21T01:10:52Z.
* The 13 `unresolved` records are pre-existing partial completions (legacy collapsed
  plans, pool-only files whose keys moved before the inventory rewrite); each still holds
  at least one file, which is reconcile's "present" verdict, so they were deliberately
  left untouched.

**Deployed 2026-09-21 05:25 CDT** (`ship-fleet.sh`; it correctly skipped the reaper,
which is still mid-drain): the ingest daemon restarted onto the new code, 0 tracebacks in
`TorrentIngest.log`/`DirectIngest.log`/`MediaSync.log`, the mount re-primed (312 show
folders) and Jellyfin answered `/Items/Counts` (313 series / 20,386 episodes / 452 movies
/ 90 box sets). The old re-queue loop stopped at the restart — the last old-code re-queue
was Chapter 1133 at 04:53 (`TorrentIngest.log`), its re-download sits `present` on the
mount so the new audit leaves it, and when `chapter_volume_reconcile` purges it again the
completion will be CLOSED, not re-queued. `reconcile_dead` + `reconcile_closed` keep the
closed five out of every audit. Two of the tails from the old queue ended `failed`
(c1098/c1112 — the plan-time guards refused them); that is terminal and needs no action.

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
scripts/repair_manga_mislabels.py    --series X [--all] [--apply]   # vNNNN whose contents are chapters
scripts/verify_owner_report.py      (read-only)                    # the owner's five failures, PASS/FAIL per line
comicfacts.py                       (module)                       # archive-content facts: colour, volume, chapters, ceiling
scripts/yacreader_index_repair.py   [--apply]                      # crash rows in the reader index; --apply WRITES
scripts/identify_capacity.py        --probe                       # which providers can serve
scripts/audit_free_only.py                                        # the billing invariant
scripts/repair_journal_paths.py     [--apply] [--record <ih>]     # records left on moved/purged paths; --apply WRITES
```

`identify_capacity.py --probe` re-probes every model id live. **Do this when model ids rot,
and they will** — five of the eight a careful reader would have written down on 2026-09-12
were already dead (HTTP 410 "end of life", 404 "no longer available"). `ai_models.py` heals
retired ids into `state/ai_model_overrides.json` at runtime; anything in that file is an id
`config.py` still names wrongly, so promote it when you see it.

---

## 10. Open work — the owner's five verified failures, then the free-AI upgrade

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

**Right now** (state table §6, measured 2026-09-20 05:30): no identify run is in flight,
the reaper is draining (PID 1311), and the Smurfs replacement pack has already failed once
on a truncated plan (§10.9). Do not re-drop it, and do not deploy, until the tool work
below ships.

Suggested order: **§10.0 is the contract** — the owner's five faults, each tied to the
subsection that fixes it. Do 10.3 (TZ) and 10.5a–10.5e (One Piece) first, because both are
live-repair-and-guard pairs; then 10.9 (the Smurfs, which is the proving ground for the
plan-assembly upgrade); then 10.10 (the systemic free-AI upgrade those pieces are all
instances of); then 10.4 (Toriko) and the 10.5e migration. Run every repaired tool over
the live library, verify against the owner-visible artifacts (§2.6), then re-run the
checks and paste the output.

### 10.0 The owner's five verified failures — the work order, with the evidence

The owner reported these on 2026-09-20. Each was re-verified this session at the path
named; each has survived at least one prior session that treated its guard as the fix.
They are ordered as the owner listed them. **Do not mark any of these done in a later
edit of this file without pasting the acceptance command and its output beside it.**

| # | fault (owner's words) | verified state 2026-09-20 | fix |
|---|---|---|---|
| 1 | repetitive One Piece chapters already covered by volumes are still in YacReader | `~/MediaLibrary/Comics/Manga/One Piece/` = **322 files**: 100 two-digit volumes (v00–v99), 111 three-digit (v001–v111), 7 four-digit mislabels, **104 chapters**. `ChapterReconcile.log` last run 2026-09-20 04:35: "One Piece: 104 chapter(s) kept, nothing to purge". The map (`state/manga_volume_map.json`) covers only v1, v2, v10, v11 (partial sets) | 10.5b, 10.5c |
| 2 | a non-colored volume does not get replaced by a colored one | the shelf holds both overlapping runs and **mixed colour inside each** (measured by archive contents: v01/v02/v50/v51/v91/v98/v99/v101/v102 are COLORED; v89/v90 are grey fan scans; the 1r0n v001–v111 are grey). `library.db` has **one** `colored=1` volume row (number 100). `chapter_volume_reconcile.plan_decisions` *has* a "colored supersedes grey" rule (#5) but `_kind` detects colour **from the filename only** (`dbhook._COLOR`), and these filenames carry none — so the rule can never fire | 10.5d |
| 3 | `One Piece v1176` should be `c1176` — there are not that many volumes | all seven are live on the shelf and in `library.db` as `volume` rows: `v1078`, `v1151`, `v1152`, `v1161`, `v1162`, `v1171`, `v1176`. Sampled `v1078.cbz` contents are `d1078` chapter pages (15 pages). No volume ceiling is persisted anywhere, so nothing can reject them | 10.5a, 10.5c |
| 4 | TZ (2019) still shows *Too Cute* as its show cover | `folder.jpg` md5 = `05520557851cb23ea38e121ed2713514` — byte-identical to the TMDB 80979 (*Too Cute*) poster. `tvshow.nfo` still carries `<originaltitle>萌宠成长记（精编版）</originaltitle>`, `<tvdbid>325542</tvdbid>`, `<premiered>2013-01-30</premiered>` (its `<tmdbid>` is now the correct 83135). `Season 01/season.nfo` year 2013; S02 episode `.nfo`s have **no `<season>`/`<episode>`**; the doctor worklist has no TZ entry at all | 10.3 |
| 5 | the Smurfs torrent is failing and it should not be | failed 2026-09-19 22:36 CDT: `plan accounts for 24 file(s) but leaves 385 release file(s) unfiled`. The 24-file plan is S01E01–10, S02E01–10 and the 4 Xtras; the identify log shows a 36-turn investigation, then two tiny writes. The 24 are also **wrong numbering** (release order ≠ broadcast order; *The Smurfette* is broadcast S01E31). The full 54.8 GB pack is retained (51 GiB by `du`); the `.torrent` is in `failed/` | 10.9 |

**Status 2026-09-20 (verified live).** Rows 1–4 are **done** and `verify_owner_report.py`
prints them PASS (TZ art/identity; no `vNNNN`; zero covered chapters; one edition per
volume; Toriko 0 junk titles / 0 blank plots). Row 5: the tooling is shipped and the pack
filed (409 -> 377 applied, 32 accounted duplicates), **but the completed run exposed the
release-order-vs-guide-order collision above; the shelf needs a content-verified refile
before row 5 is done.** The evidence as measured:
  * TZ folder.jpg `965f20be…`, landscape.jpg `ec122588…`, premiered 2019-04-01,
    tvdbid 358915, enddate 2020-06-25; Jellyfin 2 seasons, 20 indexed episodes.
  * One Piece: 7/7 mislabels renamed; shelf 322 -> 197 (127 superseded), 0 `vNNNN`,
    112 volume numbers one edition each, 0 covered chapters.
  * Toriko: 69 blanks -> 0 (68 guide titles + 69 TMDB synopses), 8 junk titles -> 0.
  * Smurfs map: 362/405 titles matched, 347 differ, release `S01E01 (The Smurfette)` ->
    broadcast `S01E31`; 409-file skeleton written.

**The acceptance rule for all five (this is §2.6 in practice).** A repair is done when the
owner's artifact is checked and the output is pasted into the commit message:

* comics — the shelf listing and the YacReader-visible result (`scripts/yacreader_rescan.py
  --files`, `scripts/audit_volume_chapter_coverage.py`): zero `vNNNN`, zero chapters
  covered by an owned volume, exactly one copy per volume, colour correct;
* TZ — `md5 -q` of `folder.jpg`/`landscape.jpg` is neither Too Cute hash, `grep` of
  `tvshow.nfo`/`season.nfo` shows the correct title/year/ids, S02 episodes have
  `<season>`/`<episode>`, and `GET /Shows` via the Jellyfin API shows the corrected art and
  no ghost season;
* Smurfs — `state/tmp/6c413306…_plan.json` covers all 409 release files, all 405 episode
  slots are broadcast-correct, and the record ends terminal with the content on the mount.

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
mapped the same 405 files by title (this session measured that plan's provider ordering
as TVDB-ish, NOT the TMDB order Jellyfin actually renders — do not use it as the
authority; `tmdbguide.episode_names` is, see the session-2 section in §6).

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

### 10.3 P1 — provider IDs are verified before they can pick art (The Twilight Zone (2019)) — **SHIPPED 2026-09-20, repaired live**

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

**Re-verified 2026-09-20 (owner's report — this is still live).** The shelf `folder.jpg`
still md5s `05520557851cb23ea38e121ed2713514` (the Too Cute poster). `landscape.jpg` now
md5s `ace480b21fbaae4b9679aa8a158c7050` — not the Too Cute file recorded above, but it has
**not** been verified against TZ identity either; the tooling must decide that, not a human
eye. `tvshow.nfo` now carries the correct `<tmdbid>83135</tmdbid>` but still
`<tvdbid>325542</tvdbid>`, `<originaltitle>萌宠成长记（精编版）</originaltitle>` and
`<premiered>2013-01-30</premiered>`; `Season 01/season.nfo` is still year 2013; every S02
episode `.nfo` still lacks `<season>`/`<episode>`; and `state/doctor_worklist.json`
(2026-09-20 05:06) contains **no TZ entry at all**, which is why this cannot self-heal:
`_scan_episode_art` only looks at episode stills. Repairing the files by hand would leave
the self-heal blind and the fault would return on the next Jellyfin re-scrape. The fix is
the doctor's title-level check, then run it.

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

**Acceptance (owner-visible, §2.6).** After the fixed doctor runs: `md5 -q` of
`folder.jpg`/`landscape.jpg` differs from both Too Cute hashes recorded above;
`grep -E 'originaltitle|tvdbid|premiered'` on `tvshow.nfo` and `Season 01/season.nfo` shows
the TZ (2019)/83135 identity and no 2013 date; every `Season 02/*.nfo` carries
`<season>2</season>` and a real `<episode>`; and `GET /Shows?searchTerm=Twilight` answers
two seasons with the corrected image. Paste that output in the commit message.

### 10.4 P1 — metadata self-heal must see pool-only episodes and survive a flaky provider (Toriko) — **SHIPPED 2026-09-20**

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

### 10.5 P0 — the manga tiers are computed, and the DB can hold both colors (One Piece) — **SHIPPED 2026-09-20**

**Verified state 2026-09-20 (re-measured; the 09-19 description flattened this).** The
shelf `Comics/Manga/One Piece/` holds **322 files**, exactly: **100 two-digit volumes
`v00–v99`**, **111 three-digit volumes `v001–v111`**, the **7 four-digit mislabels**
(`v1078`, `v1151`, `v1152`, `v1161`, `v1162`, `v1171`, `v1176`), and **104 `cNNNN`
chapters**. The two volume runs overlap on v01–v99 (~99 duplicate numbers), and the
overlap is **mixed colour**, which the old description flattened:

* 2-digit run, measured by archive contents: `v01`, `v02`, `v50`, `v51`, `v91`, `v98`,
  `v99` are COLORED (PZG / "Digital Colored Comics" / Colored Council / Gido); `v89`, `v90`
  are grey fan scans ("Davy Jones Edition"); `v00` is unclassified (one image).
* 3-digit run: the 1r0n volumes are grey VIZ (`v001→c0001` … `v111→c1123`), but `v101`
  and `v102` are the colored PZG files, and the archives' own chapter markers show the
  volume→chapter boundaries (`v099→c0995`, `v100→c1005`, `v110→c1121`).
* `library.db` series 1261 has **119 volume rows and only one `colored=1`** (number 100);
  series 1262 `One Piece Colored` still exists and still owns v100, v106 and c0424 as
  `owned` even though those files were unlinked through the mount on 2026-09-04 and
  2026-09-13 (`~/Library/Logs/MediaFS.err:100659-100661`, `:111149-111151`) and reaped
  (`state/reap_purges.log:28256-28258`).

The 1r0n pack applied all 154 files (111 volumes + 43 chapters) — **no re-download is
needed for coverage** — but 37 of its chapter copies were skipped in favour of older/larger
scans and v101–v105 were skipped in favour of the colored files.

**The single most useful fact for all of 10.5:** the owned archives themselves carry the
chapter numbering (`One Piece - c0001 (v001) - …`, `One Piece - Digital Colored Comics -
c0471 (v049)` …), and their first entries name the edition. A volume→chapter map for One
Piece is therefore **computable offline from the shelf**, and colour is detectable from the
archive contents — neither needs a model or MangaDex. Build the map that way first
(10.5b), providers second, the AI only for what remains.

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

**Measured 2026-09-20:** all seven mislabels are still on the shelf and in `library.db` as
`volume` rows, and `state/manga_volume_map.json` persists no total for any series. The
ceiling is also derivable offline from the shelf: the highest real volume archive's chapter
marker (`v111→c1123`) plus the number of single chapters after it. Use AniList's total when
fresh, the shelf when it is not, and fail open when neither exists — never invent one. This
is the guard that makes the owner's "there are not that many One Piece volumes" a computed
fact instead of a judgment call.

**10.5b — the volume→chapter map must actually cover the volumes.** The One Piece map entry
is `source=mangadex, confidence=0.0` with only `v1, v2, v10, v11` mapped — 4 of 119 owned
volumes. MangaDex's One Piece aggregate carries almost no integer volume tags, so
`plan_decisions` keeps every chapter ("volume map unknown") by design and the census says
`covered=0, uncovered=103`. A working map for 1–111 is the gate for any chapter purge. Add
a second provider/source (AniList volume/chapter ranges, an official listing, or the
existing one-shot AI completion path) with per-volume chapter **sets**, the same
confidence/TTL/fail-open contract, and make the refresh able to reach 111. Do not hand-edit
the JSON — the tool must fetch it.

**Measured 2026-09-20:** the map still holds only v1, v2, v10, v11 (partial sets;
`source=mangadex`, `confidence=0.0`), so `ChapterReconcile` keeps all 104 chapters every
6 hours ("nothing to purge"). The build order is now fixed: **(1) compute from the owned
archives' embedded chapter markers** — offline, deterministic, covers all 111 volumes, and
it is the §5 "compute the answer" step; (2) fill gaps from AniList volume/chapter ranges or
an official listing; (3) only what remains may use the existing AI fallback with its
confidence marker. Record in the entry which volumes were shelf-derived so a better scan
landing later can invalidate exactly those, and keep the rule that a colored chapter may
only be superseded by a colored volume (10.5d) — the reconcile gate already carries a
colored-author keep, so do not drop it.

**10.5c — DONE live 2026-09-20 (session 2, see the session-2 shipped section).** Covered
chapters purged, the repair now keeps the series in renamed files, bare markers refused
at plan time and collapsed as duplicates by the reconciler, and the mismatch class is
guarded by the finished-series chapter ceiling. The text below is the original brief.

**10.5c — chapters yield to volumes, and mislabels are repaired by tool.** Once 10.5a/b
land: the seven `vNNNN` files are renamed to `cNNNN` **by a repair tool that uses the same
computed ceiling** and goes through `library.supersede_paths`-style machinery (mount
unlink + reaper queue + `dbhook.record_purge`) rather than `mv`; then
`chapter_volume_reconcile.py` (6-hourly daemon and `--apply`) can supersede
`c1077–c1133`-style chapters covered by `v107–v111`. Add a fixture where a chapter above
the volume ceiling sits alongside a mapped volume and assert the reconcile now purges it.

**Measured 2026-09-20:** the reconcile runs every 6 hours and says "One Piece: 104
chapter(s) kept, nothing to purge"; YacReader still lists them. The acceptance is the shelf
census (`audit_volume_chapter_coverage.py`) **and** the reader's own index via
`yacreader_rescan.py` — not the daemon's log line.

**10.5d — colour is a file property, the colored copy replaces the grey one, and a grey
file may never eat a colored one.** This is the fault the owner actually suspects, and the
2026-09-20 re-verification found the concrete mechanism. `librarybrain/librarydb.py` keys
media on `(mtype, None, number)` — colour and path excluded — and `upsert_media` does
`colored = MAX(colored, ?)` (`:468`). So the colored `v01` and the grey `v001` collapse to
one row, a stale `colored=1` can never clear, and the two tiers cannot coexist in the DB
(measured: 119 volume rows, exactly one `colored=1`). Worse, the prompt still teaches the
**old** layout: `prompts/identify.md:113-122` says a colored volume lives in
`<Series> Colored/`, while the owner rule of 2026-09-05 (`library.py:102-123`, "a folder is
named for the SERIES, never for the edition") forbids that and makes colour a file
property; `README.md:1268-1280` teaches the old layout too.

The reconcile daemon already contains the right rule — `plan_decisions` rule 5, "a colored
volume supersedes a same-numbered grey volume" — but **it can never fire on One Piece**
because `chapter_volume_reconcile._kind` reads colour from the **filename**
(`dbhook._COLOR = colored|full[- ]?color|colour`), and these filenames carry no colour
marker. The shelf's own archives do: the first entries name the edition
(`One Piece v002 (Colored) (Digital) (PZG)`, `One Piece - Digital Colored Comics - c0471
(v049)`, `One Piece - c0001 (v001) - … [VIZ Media] [Digital] [1r0n]`). That is the fix:
detect colour where the fact lives.

**Owner direction, 2026-09-20 (it overrides the older "colored is a separate series" design
in every direction):** when a colored and a non-colored copy of the same volume coexist,
**the colored copy is the one kept and the non-colored file is superseded**; a grey file may
never supersede a colored one; and no future plan may re-file a colored volume into a
`<Series> Colored/` sibling (the 2026-09-05 folder rule stands).

Fix all six parts: (i) give media a colour-aware identity (or a path/file key) and stop
`MAX(colored)`; (ii) make the supersede path refuse any plan that would delete a colored
file in favour of a grey same-numbered one, trace which tool queued the 09-13 unlinks
(candidate: the purge batch that wrote
`library.db.bak-purge-batch2-20260913-110358`; MediaFS logs at `:111149`), and pin that tool
with a guard and a regression test; (iii) detect colour from the **archive contents**
(first entries / ComicInfo) and use it in `_kind`, reconcile rule 5 and the DB — a
filename-based detector is the bug; (iv) rewrite the prompt and README so the free AI is
told the current rule, not the deleted one; (v) reconcile the stale `One Piece Colored` DB
rows so the title can be re-acquired cleanly; (vi) run the fixed reconcile and verify the
shelf keeps exactly the colored copy per volume (owner-visible acceptance in §10.0). If the
owner wants the reaped colored `v100/v106` back, that is a re-acquire (10.7), not a
fabrication.

**10.5e — DONE live 2026-09-20 (session 2).** `migrate_comics.sh --apply` moved 191
files into `Manga/One Piece/One Piece/` and `Manga/One Piece/Ace's Story/` (verified
191/191); the table row was already the generator's evidence-based one, and
`resolve_comic_folder` resolves both the master and the member's canonical name into the
nested layout. The text below is the original brief.

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
| **The Smurfs Complete Seasons 1-9** | **No new download; the bytes are already here and parked.** The replacement MP4 pack (`6c413306…`, 409 files, 54.8 GB) was dropped 2026-09-19 and **failed on the plan-coverage guard the same night** (24-file plan of 409, §10.9) — nothing was deleted, the full 54.8 GB pack (51 GiB by `du`) sits in `~/Downloads/.torrent-ingest/` and the `.torrent` is in `Torrents/failed/`. Fix the plan-assembly tool (10.9), then move the `.torrent` from `failed/` to the watch root's top level. The original dvdrip mirror (`Torrent-Ingest/state/torrent_sources/27dba…torrent`, 405 files) stays as the fallback. The existing 40 wrong-slot S01 files must be superseded by the correct plan. |
| **Doctor Who Seasons 1 to 13** (2005, `74c608c7…`) | **Done — no re-acquire.** Re-armed and re-dropped 2026-09-19; the journal's final line is `completed` with `chunk_done=178`, `chunk_dropped=0`, `chunk_failed=0` (verified 2026-09-20). The 38 re-fetched files are on the mount. |
| **One Piece (Digital) (1r0n)** (`12873efd…`) | **No re-download for coverage** — all 154 files applied. The work is rename/reconcile, not fetch. |
| **One Piece Colored v100/v106/c0424** | **Gone from the pool** (reaped 2026-09-04/09-13, see 10.5d). Only if the owner wants the colored run restored does anything need re-acquiring; the fleet has no source. |
| **One Piece 1177/1178 in `queued/`** | **No download** — already filed and owned (10.6); safe to delete or move to `finished/`. |
| **TZ (2019), Toriko, everything else** | **No download.** Metadata/art/DB repairs only. |

### 10.7b Parked re-drops — where they are and where they go (recorded 2026-09-19)

The owner parked both re-downloads **by hand, outside the pipeline**, so neither could be
ingested before its prerequisite guard shipped. Both have since been dropped: DW is
`completed` (10.7), and the Smurfs failed on plan assembly and now sits in
`Torrents/failed/`. `~/Downloads/` is no longer a parking spot — the only re-drop left is
the Smurfs, **after 10.9 ships** (below). `~/Downloads/` is not watched by the pipeline
(`DirectIngest/` is the only watched folder under Downloads).

| release | parked at | move where, when |
|---|---|---|
| Doctor Who (2005), "Doctor Who Seasons 1 to 13 Mp4 1080p" (info hash `74c608c7…`, 178 files, 281.4 GB) | **DONE.** Re-armed 2026-09-19 (`refile_season.py --record 74c608c7ba56dd4b3f2c04ab3999f045d767013f --rearm 1-31,41,55-59,92`), re-dropped to the watch root's **top level**, final journal line `completed` with `chunk_done=178`, `chunk_dropped=0` (verified 2026-09-20). The pipeline mirror stays at `Torrent-Ingest/state/torrent_sources/74c608c7….torrent`. | Nothing left to move. |
| The Smurfs Complete Seasons 1-9 — **the owner's replacement pack**, not the original dvdrip (info hash `6c413306e7053dbb8f1dabf7dcc845f509ec3027`, 409 files, 54,806,198,752 bytes = 54.8 GB) | **`Torrents/failed/The Smurfs (Complete cartoon series in MP4 format.).torrent`** since the 2026-09-19 22:36 failure; content retained at `~/Downloads/.torrent-ingest/The Smurfs (Complete cartoon series in MP4 format.)/` (51 GiB on disk by `du`; 54,806,198,752 bytes by the torrent). Contents: 409 `.mp4` — S1=40, S2=35, S3=51, S4=48, S5=41, S6=63, S7=65, S8=24, S9=38 (405 episodes, counts identical to the original release) plus 4 Xtras: `The Smurfs - The Lost Village (movie).mp4`, `… A Christmas Carol (special).mp4`, `… The Legend of Smurfy Hallow (special).mp4`, `… The Smurfs and the Magic Flute (movie).mp4`. The original dvdrip mirror (`27dba035…`, 405 `.mkv`, 34.9 GB, `Torrent-Ingest/state/torrent_sources/27dba035….torrent`) stays as the fallback. | **After 10.9 ships:** move the `.torrent` from `failed/` to the **top level** of `iCloud Drive/Torrents/` (never `queued/`). It must produce a 409/409 plan (405 episodes + 4 Xtras) and supersede the 40 wrong-slot S01 files. |

One check before the Smurfs re-drop, because a truncated `.torrent` is a silent no-op:
verify the info hash parses to the expected value (bencode + sha1, the `_ensure_source`
convention) and count the `files` list — **409 for this pack** (not 405; the old mirror is
the 405-file one). After it files, confirm the record's last journal line is terminal and
the content is on the mount. One naming caveat, unchanged and now load-bearing: this pack's
filenames carry the release's own `SxxExx` numbering (e.g. `S01E01 (The Smurfette)`), which
is the same scheme as the deleted dvdrip, so the compute-the-numbering rules of §5 and
§10.9 apply before any file is trusted over the provider.

### 10.8 Acceptance for this batch

**Progress 2026-09-19:** 10.1, 10.2 and 10.6 shipped with registered tests #51/#52; the
replay surfaced **Yamato 2202** (26 real episodes a partial plan never accounted for,
nobody had noticed). DW (2005) was re-armed and re-dropped; the outcome is in the journal.

**Progress 2026-09-20:** the tooling for all five is shipped with 60 registered checks.
§10.0's status block says exactly which live artifacts are verified and which await the
reaper drain / the Smurfs re-drop; do not claim more than it says.

1. Every new guard has a registered test in `scripts/verify_fleet.sh` and a journal replay
   result in its commit message; `ALL CHECKS PASSED`.
2. The five owner-visible faults in §10.0 are fixed **and each acceptance command from
   §10.0 is run and pasted, with its output, into the commit message.** Specifically:
   * Smurfs' 409-file pack fully planned (405 episodes + 4 Xtras) and filed, superseding
     the 40 wrong-slot S01 files, record terminal, content on the mount;
   * TZ (2019) has two seasons, correct art on disk (**md5 differs from both Too Cute
     hashes**), a clean `tvshow.nfo`/`season.nfo`, no ghost season, S02 episodes numbered;
   * One Piece has zero `vNNNN`, zero chapters covered by an owned volume, exactly one
     copy per volume with the coloured copy winning, a colour-correct DB, and the One
     Piece franchise folder holding both series; the YacReader shelf reflects it;
   * Toriko has 0 release-group titles and 0 blank plots (or a bounded, logged queue for
     the rest); `queued/` is empty.
3. `verify_fleet.sh`, `fleet_health`, `fleet_doctor`, `library_health.txt` and
   `media_doctor` (with the Jellyfin credentials from §4) all read clean or name only
   known-accepted items from §7.
4. The next session that reads this file can tell from `state/decisions.log` and the test
   names exactly which tool prevented which fault — that trace is the deliverable. A fix
   with no such trace and no owner-visible before/after is not done (§2.6).

### 10.9 P0 — the Smurfs: a plan the harness computes, and a plan the model can finish writing — **SHIPPED 2026-09-20, re-drop pending**

**Failure (verified 2026-09-20).** `6c413306…` (409 files, 54.8 GB) was dropped
2026-09-19 09:11 and failed the same night. `state/tmp/6c413306…_plan.json` is 8,388 bytes
and contains **24 files**: S01E01–E10, S02E01–E10 and the four Xtras. The journal's
terminal line (2026-09-19 22:36 CDT; `updated_at 2026-09-20T03:36:02Z`) is `failed` with
`plan accounts for 24 file(s) but leaves 385 release file(s) unfiled … the release is
parked intact` — §10.1's guard did its job. The identify log ends: turn 36 "finished
WITHOUT writing … asking for it (1/2)", turns 38/39 two tiny `Write`s, turn 40
"done (stop)". All bytes are still on disk; the `.torrent` is in `Torrents/failed/`.

**Two independent defects; fix both.**

1. **Nothing computes the mapping this pack needs.** The filenames carry SxxExx in the
   *release's* order, which is not broadcast order — the dvdrip mirror's own history proves
   it: release S01E01 "The Smurfette" is broadcast S01E31 (§10.1). The 24-file plan maps
   release S01E01 → broadcast S01E01, so even a complete plan written this way would file
   S01 wrong. This is the `serial_release_map`/`arcmap` class: the harness must compute
   release→broadcast episode numbers from the provider's episode list (match the release
   file's own episode title against the guide), state the block in the prompt as fact, and
   have `validate_plan` refuse a plan that contradicts it. Fail open when the guide is
   unavailable.
2. **A single `Write` cannot hold 409 entries.** The model spent 36 turns investigating,
   then wrote a truncated prefix; the harness accepted it because the JSON was valid, and
   only the POST-run coverage check saw the missing 385. Plan assembly must scale:
   * the prompt must say the plan may be written in parts (append via `Edit`, or a
     `files_part` schema), and `identify`/`ai_client` must merge parts, detect a
     truncated/partial plan **before the run ends**, and ask for the missing slice with the
     explicit unfiled list instead of finishing;
   * above a size floor (~150 files) the harness must hand the model a deterministic
     skeleton — every release file enumerated with its computed destination (or an
     explicit `needs_mapping` marker) — so it fills titles/ids/gaps instead of re-typing the
     listing;
   * a cut plan is a **retryable state, not a terminal `failed`**: bounded retries with the
     missing slice, then park with the reason. The coverage guard stays the last line of
     defense (§10.1), it is not the first.

**Repair.** After the tool ships: move the `.torrent` from `failed/` to the watch root's
top level. The full 409-file plan (405 episodes + 4 Xtras) files the pack; the 40
wrong-slot S01 files from the old dvdrip are superseded by plan evidence (the renumber
precedent in §6/§10.2); end state: every episode slot occupied with broadcast-correct
numbers, the 4 Xtras placed (S00/Movies), the record terminal.

**Proof.** Fixture release whose filenames are deliberately not in broadcast order → the
computed mapping block is in the prompt, the plan is rejected when it contradicts the block
and accepted when it follows it. Fixture release of >300 files → the skeleton is complete,
the model writes it in parts, coverage is 100% before the run ends. Replay both over
`state/journal.jsonl`; register the tests in `verify_fleet.sh`.

### 10.10 P0 — the free-AI upgrade: computed facts, completable plans, verified self-heal — **IMPLEMENTED 2026-09-20**

The five failures in §10.0 are one shape repeated: **the harness let the model do
arithmetic/enumeration it cannot do, and the self-heal that should have caught the result
was blind.** This section is the systemic fix the owner asked for and the umbrella over
10.3, 10.5 and 10.9. Do not treat the pieces as separate nice-to-haves; each is the
difference between a fault returning and not.

**A. Compute before the model runs, and state it as fact.** Every block follows the
`arcmap`/`serial_numbering_block` pattern — computed by the harness, injected into
`_runtime_prompt`, enforced by `validate_plan`, fail-open:

* manga volume ceiling (AniList total, shelf fallback) and the `vNNNN`-above-ceiling
  rejection (10.5a);
* volume→chapter sets from the owned archives, providers second, AI last (10.5b);
* colour per file from archive contents, and colored-supersedes-grey in both the reconcile
  and the plan validator (10.5d);
* franchise membership from `config.COMIC_FRANCHISES` (10.5e);
* release→broadcast numbering for season packs (10.9);
* provider ids verified (TMDB/TVDB title+year fetch) before any id may pick art; a mismatch
  strips the id and logs the rejection; a network error fails open but the unverified id
  cannot pick art (10.3).

**B. Plans the model can actually finish.** Enumerate the release in the harness (it does
this for coverage already); require the plan to cover it; support part-written plans with
merge + truncation detection; above a size floor hand the model a deterministic skeleton;
reject with the explicit missing list; bound retries; park — never terminal-fail — when
only assembly failed.

**C. The prompt tells the truth.** `prompts/identify.md` must lose the stale rules (the
`<Series> Colored/` layout at :113-122 is the live example) and gain every computed block.
Anything a human has had to say twice belongs in the prompt or the validator.

**D. Validation with evidence.** Every new rejection is replayed over
`state/journal.jsonl`, with the false-positive count printed in the commit message (§5);
tests are registered in `scripts/verify_fleet.sh`. No guard ships on a hunch.

**E. Self-heal that verifies itself.** `media_doctor` must see series/title art and nfo
identity (10.3); `audit_metadata` must enumerate pool-only media from the mount/inventory,
accept name-only guide rows and have a synopsis fallback (10.4); `escalate()` must verify
the postcondition (fields actually changed) before charging its budget; provider caps
park, never retire; `chapter_volume_reconcile` purges covered chapters once the map exists
and logs before/after; every repair writes a `decisions.log` entry with its evidence.

**F. The verification gate for "fixed".** Add `scripts/verify_owner_report.py` (name it
what it is): it computes the five §10.0 checks — Too Cute md5s absent, no `vNNNN` on the
shelf, zero covered One Piece chapters, colour-correct volume pairs, Smurfs plan coverage
once a plan exists — and prints PASS/FAIL per line. Read-only; registered in
`verify_fleet.sh` as a report check (it must not gate a deploy on the parked Smurfs). Run
it at the end of the session and paste the output into the commit message. Then add the
regression tests that fail if any of the five returns.

**G. Definition of done for this batch.** §10.8 item 2. The measure is not that the symptom
is gone today; it is that the tool that computes the fact, the guard that enforces it and
the self-heal that repairs it all exist and are tested, and that the owner-visible artifact
is clean.

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
  updated with it. **OWNER DECISION 2026-09-23: the rotation will NOT be done.** It is
  closed, not pending — do not raise it again or treat the exposure window as actionable.

This work was shipped without a fleet restart (`scripts/save-and-push.sh`): `.env` carries
the exact values the code previously hardcoded, so the running daemons see no change.
`verify_fleet.sh` printed `ALL CHECKS PASSED` (54 blocking checks) after the extraction.

---

## 12. After the reboot (2026-09-19) — close-out verified 2026-09-20

The owner rebooted on 2026-09-19 so launchd would re-read the refreshed agents. The
rename itself is DONE and pushed (`aa70cc5`); `ship-fleet.sh` could not put it into effect
because `kickstart -k` restarts the LOADED job definition, which still names the old path
(measured: `program = .../Developer/Media-Fleet/...`). The reboot also bounced the in-flight
Smurfs identify and the reaper drain — the owner knowingly waived §2.2/§2.4 for this one
boot, so this checklist is about proving recovery and finishing the rename, **not deploying
again**.

1. **Every daemon must be on the new path before anything is removed — DONE**, verified
   2026-09-20: `pgrep -fl 'Developer/Media-Fleet'` is empty and no live process resolves
   the old path.
2. **Remove the compatibility symlink — DONE.** `~/Developer/Media-Fleet` no longer exists
   (verified 2026-09-20). Leave the `-p:Media-Fleet` line in `~/Developer/.megaignore`; it
   is now insurance.
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
5. **Smurfs/identify recovery — it ran, and it failed for a new reason.** `6c413306…`
   produced a 24-file plan for a 409-file release and the coverage guard parked it intact
   at 2026-09-19 22:36. That is the §10.9 defect, not a transient: **do not re-drop the
   `.torrent` until the plan-assembly tool ships**, and do not hand-file any of the 54.8 GB pack (51 GiB by `du`)
   still in `~/Downloads/.torrent-ingest/`.
6. **Rotation (from §11) — CLOSED BY OWNER DECISION 2026-09-23: not doing it.** The Jellyfin
   API key and the shared MEGA password stay as they are; the exposure window recorded in
   §11 is accepted. Do not re-raise this, and do not re-run `startup.sh` for the key.

---

## 13. The FUSE layer: mediafs rides fuse-t, macFUSE removed (2026-09-21)

Symptom: after the 05:56 reboot the mount never came back. `com.mikeyferguson.mediafs`
crash-looped (13 × `mount_macfuse: the file system is not available (2)`), the
"macFUSE is too old" dialog kept popping, and `library_supervisor` held Jellyfin down
(`mount not ready -> stopping Jellyfin`) — the guard working as designed, not the fault.

Diagnosis: `fusepy` resolves its dylib by NAME (`ctypes.util.find_library('fuse')`),
which matched macFUSE's `/usr/local/lib/libfuse.dylib` → `libfuse.2.dylib` (macFUSE
5.0.6, hand-installed 2025-10-01, **no Homebrew receipt**), so the mount exec'd macFUSE's
`mount_macfuse` even though the fleet's FUSE layer is fuse-t. The nightly BrewUpgrade pass
could never help: macFUSE was not a cask it could see, and fuse-t 1.2.7 is already current
(`brew outdated` listed only jellyfin). The daemon was doing exactly what it was built to
do — hold the casks it cannot safely swap at 04:00 and leave them for a person; this
incident is the hand-upgrade path firing.

Fix: `run_mediafs.sh` pins `FUSE_LIBRARY_PATH=/usr/local/lib/libfuse-t.dylib` (fusepy's
supported override, `fuse.py:84`), read before `find_library`, and exits 1 with a clear
error if the library is missing instead of silently falling back. The LaunchAgent plist
comment and the mediafs README/docstring now say fuse-t.

macFUSE removal (by hand, admin): `macFUSE.framework`, both launch daemons + privileged
helpers (booted out first), the prefpane, `/usr/local/lib/libfuse.2.dylib`, the
`libfuse.dylib` symlink, `libfuse.la`, `/usr/local/include/fuse.h`, `pkgconfig/fuse.pc`.
**macFUSE's own uninstaller was deliberately NOT run**: it rm's
`/usr/local/lib/libfuse3.4.dylib` and `libfuse3.dylib`, which on this box are FUSE-T's
(timestamps + FUSE-T's own uninstaller agree; its wiki says the package never installs
`libfuse.dylib`). One remnant survives: `/Library/Filesystems/macfuse.fs` — SIP/`sunlnk`
on `/Library/Filesystems` refuses root `rm -rf` (`Operation not permitted` on every
entry). It is inert (no userspace lib and no launch job can invoke it); removing it needs
a SIP-disabled boot and was not judged worth it.

Verified after the change: a fresh `kickstart -k` of mediafs mounts
`fuse-t:/MediaLibrary (nfs)`, a read through the mount returns real bytes, and
`library_supervisor` restarted Jellyfin on its own; `/Items/Counts` = MovieCount 452,
SeriesCount 313, EpisodeCount 20386, BoxSetCount 90 (identical before and after). The
mount drop during the restart tripped the supervisor's "mount not ready" stop — expected,
and it recovered in ~70 s. Shipped with `Media-Syncer/scripts/ship.sh`.

---

## 14. WebDAV vs the mount for Jellyfin: considered, rejected (2026-09-21)

Owner asked whether WebDAV is faster than the FUSE mount and could improve streaming. Answer:
no, and the premise inverts the layering. Jellyfin cannot speak WebDAV (no remote-storage
support at all); the documented workaround is to mount the WebDAV endpoint locally and point
Jellyfin at that mount — i.e., WebDAV is a *backend protocol for a mount*, an extra hop, never
a replacement for the mount. The fleet already does the recommended thing, with a filesystem
that carries the inventory.

What an rclone mount / `rclone serve webdav` would forfeit here:

* **Inventory-local scans.** mediafs serves all 25,597 paths full-size from
  `remote_inventory.json`; `stat`/scans never touch MEGA. An rclone mount makes Jellyfin's
  first scan list directories over the API — precisely the load the account pool + VPN
  rotation exist to spread, but outside that machinery.
* **The tier engine.** Cold reads hydrate in 4 MB segments with 2 parallel workers
  (`STREAM_SEGMENT_BYTES` / `STREAM_WORKERS`, config.py:788), resume interrupted fills, serve
  un-hydrated ranges on demand, promote into the local cache, and prefetch the next episode
  (`PREFETCH_AHEAD`). rclone's VFS is a weaker, unmanaged re-implementation.
* **The deletion contract.** Deletes through the mount are tombstoned and reaped to every
  pool copy; an rclone mount knows nothing of `mediafs_deletions.jsonl` or the reaper.
* **Credential/rotation management.** Hydration rides the same rotating MEGA account pool as
  the syncer; a WebDAV endpoint needs its own access path and VPN story, unmanaged.

`.strm` files pointing at an HTTP/WebDAV endpoint were considered in the same pass: they would
bypass the local cache entirely (every play refetches from MEGA), sidestep the tier engine,
and add an unmanaged server — rejected on the same grounds.

Measurement that settles it: a 160 MB local file read through `~/MediaLibrary` runs at
**314 MB/s** (page-cached direct read: 17 GB/s). Playback bitrates sit orders of magnitude
below that, and a cold file is bound by MEGA's per-connection throughput, not the local
protocol — there is no streaming headroom for WebDAV to win back. Reconsider only if the pool
is replaced by a LAN-hosted store with no API metering; then a plain NFS/SMB/rclone mount
could be simpler. Shipped docs-only with `Media-Syncer/scripts/save-and-push.sh` (no daemon
runs this text, so no bounce).
