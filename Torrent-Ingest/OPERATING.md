# Operating this fleet without an assistant

This replaces `~/Developer/Media-Fleet/diagnosis.txt`, which was a hand-off note written for whatever
AI session came next. There is no next session, so this is written for **you**: what to
look at, what the reports mean, what is safe to do, and what is not.

Everything below is either a command you can run or a rule that cost a real failure.

---

## 1. The thirty-second check

Two files in `iCloud Drive/Torrents/` answer "is anything wrong?" from your phone.

| File | Written by | Read it when |
|---|---|---|
| `fleet_health.txt` | `fleethealth`, every 5 min | always — this is the one that matters |
| `fleet_doctor.txt` | `fleetdoctor` | when fleet_health has an ACTION, to see if it self-healed |
| `library_health.txt` | `mediadoctor` | when fleet_health mentions items needing review |
| `mega_free_space.txt` | Media-Syncer | when you want to know pool headroom |

`fleet_health.txt` says `ALL CLEAR` or lists `[ACTION]` (needs you) and `[warn]`
(informational) lines. **`[warn]` lines are not emergencies.** An `[ACTION]` line names the
thing to do.

On the Mac itself, the one command that answers "is the code sound?":

```bash
bash ~/Developer/Media-Fleet/Torrent-Ingest/scripts/verify_fleet.sh      # 52 blocking checks
```

It must print `ALL CHECKS PASSED`. If it does not, do not deploy anything.

---

## 2. How things get into the library

**You drop a `.torrent` into `iCloud Drive/Torrents/`.** That is the only way in for video
and comics. There is no search, no discovery, no automatic acquisition — that subsystem
(Torrent-Searcher) was deleted on 2026-09-10 because it was an unpredictable wild card.

The drop then moves itself through four subfolders that mirror its state, so you can watch
progress from Finder or your phone:

    queued/  ->  ingesting/  ->  finished/     (or failed/)

Re-dropping a `.torrent` from `finished/` or `failed/` back to the top level retries it.

**A `.torrent` that lands in `failed/` on its own is truncated** — the download or the
iCloud sync stopped short, and re-dropping it changes nothing. There is one exception
you do not have to work for: every drop is named with its 40-character info hash
(`04CFA…C2.torrent`), and when the bencode will not parse that filename is all a magnet
needs. Those are recovered **automatically** — the drop becomes a QUEUED magnet,
qBittorrent fetches the real metadata from the swarm, and you will see it in `queued/`
as a `.magnet`. The dead `.torrent` is then removed, so it will not keep reappearing in
`failed/` while the torrent it stands for is queued and downloading; re-dropping it
later just removes it again. No action is needed. A `.torrent` with a normal name in
`failed/` has no hash to recover from and must be replaced.

Two other inboxes still work:

* `~/Downloads/DirectIngest/` — drop a loose file OR folder here and it is filed: video
  (`.mkv`/`.mp4`/`.avi`/`.m4v`/`.mov`) into Shows or Movies, comics (`.cbz`/`.cbr`/`.pdf`)
  into Comics, e-books (`.epub`, or a `.pdf` planned as one) into the Google Drive Novels
  folder. A video's subtitle siblings (names beginning with the video's stem, e.g.
  `Movie.srt`, `Movie.en.srt`) go along automatically.
* **`iCloud Drive/Torrents/DirectIngest/`** — the same thing, droppable from any device.
  `directingestbridge` MOVES whatever lands there into the local folder above, then the
  local ingester files it. This folder empties itself by design; that is not a problem.
* `find.txt` in the Torrents folder — put one title on a line and **Title-Scout** goes and
  finds that one thing. This is the deliberate, one-off replacement for the searcher, and
  it is still running.

---

## 3. The four rules that cost real damage

**3.1 A deletion is only real through the mount.** `~/Media` is the SSD; a file missing
there has been EVICTED to the MEGA pool, not lost. `~/MediaLibrary` is the merged view and
the only place a deletion means anything. Never "clean up" `~/Media` because something
looks missing.

**3.2 Never restart the reaper while it is draining.** `torrentreap` is the only thing that
purges a MEGA copy. It probes hundreds of accounts per file, and a restart throws that work
away — a drain has run for five days. Check first:

```bash
pgrep -f 'Torrent-Ingest/reap.py'      # any output => it is working, leave it alone
```

Long silences are the probe, not a hang. `ship-fleet.sh` refuses to restart it for you.

**3.3 Believe the `.nfo`, once you understand what it claims.** When a filename and its
`.nfo` sidecar disagree about which episode a file is, the `.nfo` is usually right — it is
what the fleet recorded when it filed the file. **But a multi-episode file
(`S01E01-E02 - A & B.mkv`) carries one sidecar naming only its FIRST episode**, and reading
that as its whole claim is what made the library report dozens of correctly-placed files as
misfiled. If a report tells you to re-file something, check the file sizes too.

**And check WHO WROTE the sidecar before believing it.** "The fleet recorded it when it
filed the file" is only true of a sidecar the fleet still owns. An UNLOCKED sidecar has
almost certainly been rewritten by Jellyfin's scraper since (§5b) — a BOM, `<dateadded>`
or `<fileinfo>` in the file means it is Jellyfin's guess, not the fleet's record, and for
a Season-0 special that guess is routinely off by a slot or two. Against a Jellyfin-
authored unlocked sidecar, the FILENAME is the better witness: it is written once by
`apply_plan` and never touched again. A LOCKED sidecar is the fleet's record and §3.3
applies in full.

**3.4 Two files covering one episode is a question, not a bug.** Several shows hold two
complete parallel rips. Deleting one is a taste decision (bigger h264 vs smaller hevc), it
is irreversible, and nothing will make it for you. See §5.

**3.5 A "parked" release is a plan that did not account for the whole download.**
Since 2026-09-19, the harness enumerates every media file in a release and refuses to
delete anything unless the plan covers all of it. When a plan covers part — or a planned
file collides with a different file already at its episode slot — the record goes
**FAILED** with an `unfiled` / `chunk_unfiled` list, the download is left untouched, and
the `.torrent` is filed under `failed/`. Nothing was filed from that plan and nothing was
lost. To retry: re-drop the `.torrent` from `failed/`, or fix what the plan got wrong
(a model bug, a colliding episode) and re-drop. A parked release is the system working —
the old behavior deleted the unfiled remainder. Look for `unfiled` in `state/journal.jsonl`
or the `parked N unaccounted file(s)` line in `torrent_ingest.log` to see which files the
plan missed. Guard: `scripts/test_plan_coverage.py`.

---

## 4. The AI, and its one hard limit

Renaming and placement are done by free AI models only. **Six providers as of 2026-09-12**
— Gemini (Google AI Studio), OpenRouter, Groq, NVIDIA NIM, Mistral and Cloudflare — in that
chain order. Gemini is first because it is the only free tier measured to take a WHOLE
identify prompt: probed with real tool definitions at 85,000 characters and it answered
with a tool call.

**The rule, restated accurately — read this before you "restore" anything.** It used to say
*"no paid model, no API key with a balance."* The owner put a **one-off $10 on the
OpenRouter account on 2026-09-12**, so the second half is no longer true, by decision.

What it bought is a **gate, not usage**. OpenRouter's `:free` tier allows 50 requests a day
under 10 credits and 1,000 a day at or above it — so ten dollars raises the free allowance
twentyfold, and it is never drawn down, because every OpenRouter model id still ends in
`:free` and a `:free` request is billed at zero whatever the balance says.

So the invariant is now **no request is BILLED**, and `scripts/audit_free_only.py` enforces
exactly that (every OpenRouter slug `:free`; no paid endpoint or paid credential anywhere in
fleet code). Do not reinstate the old wording — it would make the audit assert something the
owner deliberately changed, and undo a twentyfold capacity increase.

**Cerebras is excluded on purpose**, despite looking like the biggest free tier going: its
"free" plan is a 30-day TRIAL carrying $5 of expiring credits (their own rate-limit docs,
checked 2026-09-12). It would serve for a month and then quietly stop.

**Every one of those has a DAILY cap, and the fleet routinely hits zero.** When it does,
downloads finish and sit UNFILED until a provider's cap resets. Nothing is lost and nothing
needs doing; filing resumes on its own. Check with:

```bash
python3 ~/Developer/Media-Fleet/Torrent-Ingest/scripts/identify_capacity.py --probe
```

`--probe` is not optional — a cached verdict is not a measurement.

**Read the capacity line carefully; there are now three answers, not two.** `OK` means a
provider can serve any identify prompt. `NONE` means nothing can. `PARTIAL` is new
(2026-09-12) and means a provider can serve a *confirm-mode* prompt but not a full one —
so a release whose arc→season mapping the harness has settled will file, and everything
else waits for a cap to reset. That is groq's permanent state: its measured ceiling is
~21,900 characters against a full identify prompt of 63,000, and a confirm prompt is
~10,000.

**Groq's real limit is TOKENS, not requests, and it is small.** Measured 2026-09-12 from
its own refusals: 8,000 tokens per MINUTE and **200,000 per DAY**. The per-minute half is
a pause the runtime now sleeps through rather than failing over
(`ai_client._retry_after_sec`), which is what makes groq usable at all; the per-day half is
about thirty agent turns, so it is a narrow reserve and not a big budget. One exploratory
run that spent every turn web-searching burned 195,693 of the 200,000 in twenty minutes.

**What went wrong with Monogatari, and what it turned out to be.** `[MTBB] Monogatari
Series (BD 1080p)` (103 files, 15 arcs) was run on 2026-09-10 as a deliberate stress test.
It was filed with one 26-episode arc spread across six season folders as absolute episodes
1–23 — Season 09 ending up with 18,19,20,21,23 and Season 10 with only 22. Every file had a
correct title and plot. The plan was simply incoherent.

**It was not the model's reasoning.** The identify prompt was handing it the searcher's old
stored file→item mapping under the words *"reuse this mapping by filename; do NOT re-derive
the season/episode numbering"* — and that stored mapping, made years of commits ago by a
searcher that no longer exists, contains exactly the broken layout. The most
authoritative-sounding line in a 90,000-character prompt was telling the model not to think
about the one thing it needed to think about.

Five things changed on 2026-09-10, in rough order of how much they mattered:

1. **A stored mapping is now evidence, not an instruction**, and one whose own numbering is
   internally inconsistent is withheld entirely. Nothing produces these maps any more, so
   nothing re-checks them.
2. **The harness computes the release's structure** and states it as fact — which folders
   share a filename label, and whether their episode numbers form one run across them. For
   Monogatari that is five folders carrying "Monogatari Series Second Season" as one run of
   01–23, with the two legal ways to resolve it spelled out.
3. **A chunked wave is shown the WHOLE release**, not just its own files. Only a wave exists
   on disk, so the analysis used to see a fifth of the pack; Monogatari's first wave hides
   every folder whose numbering conflicts.
4. **The rule itself is in the prompt now**: if you use season folders, every season
   restarts near 1; absolute numbering is legal only in one continuous season.
5. **`library._reject_absolute_run_split` refuses the shape outright** if all of that still
   fails, and the rejection is fed back to the next provider as a compact summary rather
   than 17,000 characters of re-quoted JSON.

**The standing expectation — CHANGED 2026-09-12.** It used to read: a complex multi-arc
franchise is the hardest thing this pipeline does, so if a pack comes back unplaced, place
it yourself, that is the honest limit.

**That is no longer the bar.** The owner's standing instruction is that the free AI system
is to be upgraded until it files Monogatari *perfectly*, and that the rest of the library
is not trusted to it until it does — a stress test that is allowed to fail is just a label.
Free-only still stands (no paid model, no API key with a balance); what has to improve is
the free system, not the budget.

**What changed on 2026-09-12.** The harness now computes the arc→season mapping itself
(`arcmap.py`) instead of asking the model to marry the release's arcs to the provider's
seasons — that marriage was the step that failed three runs running. It is an exact-cover
search over the release's own units, and on Monogatari the answer is unique. Two new
guards in `library.py` refuse a plan that contradicts it: an arc torn between two numbered
seasons, and a season filled past the provider's episode count. Both were replayed over
all 788 historical plans and reject none of them.

How this was proven, and the measurement tool, are written down in
`~/Developer/Media-Fleet/HANDOFF.md` §1 and §3. The short version: use
`scripts/audit_arc_placement.py` — not `verify_arc_mapping.py` — to decide pass or fail on
any multi-arc pack, because it censuses EVERY file back to its source arc instead of
sampling one per season. It prints a real `VERDICT:` line and exits non-zero on FAIL.

What is still true from before: a wrong answer now lands UNFILED and visible instead of
misfiled and silent, and the run is told what it got wrong. And if a wave does land wrong,
purge the show and restart from zero rather than patching in place.

---

## 5. Shows that were held twice — resolved 2026-09-10

`library_health.txt` lists items needing review. On 2026-09-10 it listed 87, and they were
not 87 problems: they were two shows held as two complete parallel rips, plus a reporting
bug that made half of them look like something they were not.

**What was resolved, and how:**

* **The Powerpuff Girls (1998)** — a h264 set of combined two-segment files
  (`S01E01-E02 - A & B.mkv`) and a hevc set of single-segment files covering the same
  content. You chose to keep the h264 set; the 75 hevc singles (11.8 GB) were removed after
  verifying every one had a surviving counterpart at the same slot.
* **Helluva Boss (2020)** — you chose to keep the small titled set. That answer needed
  adjusting before it was safe: **13 of the 22 bare-numbered files were the only copy**,
  including all of Season 1 and `S02E01–E04`. Only the 9 that genuinely duplicated a titled
  file were removed, so Season 1 is complete and Queen Bee sits correctly at `S01E08`.
* **Made in Abyss** (1) and **One Pace S08E02 / S29E01** (2) — the same shape, resolved the
  same way.
* **One Pace S13E05** — the interesting one. Two files, `Inherited Will.mkv` and
  `Quack Doctor.mkv`, and **both sidecars said "Quack Doctor"**. The fleet's own
  `decisions.log` had the answer: "Quack Doctor" is One Pace's older cut of that arc
  position (ch. 141-145) and "Inherited Will" is the newer one (ch. 140-145); the release
  filename `[One Pace][140-145] Drum Island 05` proves the slot. Filing the newer cut made
  it inherit the pre-seeded sidecar's title. The older cut was removed and the title
  corrected.

**And the fleet can now resolve that last class itself.** `media_doctor` detects a sidecar
whose `<title>` names a different episode than its own filename, and repairs it when the
ingest journal confirms the filename — a third witness, so §3.3's "believe the `.nfo`" still
stands. Where the journal does not confirm it, it is reported for review and never silently
rewritten.

**The reporting bug worth knowing about.** A multi-episode file carries one `.nfo` naming
only its FIRST episode. The duplicate check read that as the file's whole claim, so every
second slot of every pair file was reported as *"this is a placement fault, re-file it"* —
about 37 false items on Powerpuff alone, each one an instruction that would have moved a
correctly-placed file. Fixed, and guarded.

## 5b. Jellyfin owns the .nfo files — what that means for a lock

**Jellyfin rewrites almost every sidecar in the library.** Of 18,357 episode `.nfo`
under `~/Media/Shows` on 2026-09-11, 17,623 were Jellyfin-authored — a BOM,
`<dateadded>`, `<fileinfo><streamdetails>`, `<art>`, none of which this fleet emits.
The Shows library runs `SaveLocalMetadata=True` and `EnableRealtimeMonitor=True`, so
Jellyfin saves its own metadata back over the path whenever it refreshes an item.

So **the file is not the authority; Jellyfin's database is.** Two consequences that
have each cost real damage:

**1. A lock only takes if the sidecar is there BEFORE the video.** Jellyfin decides
whether an item is locked from the `.nfo` it finds at the moment it first indexes the
video. Find none and it creates the item unlocked, scrapes it, then writes its own
sidecar over the path — destroying a `<lockdata>true</lockdata>` written a moment
later, permanently, because nothing re-reads a sidecar Jellyfin has already replaced.
`apply_plan` used to move the video in Phase 2 and write sidecars in Phase 3, so every
video sat uncovered for the whole of a long Phase 2. Fixed on 2026-09-11: the locked
sidecar now goes down in Phase 2a, immediately before its own video. Guarded by
`scripts/test_specials_locked.py`, which instruments `os.replace` and asserts the
sidecar is already on disk and already locked at the instant each video lands.

**2. Repairing metadata by hand needs BOTH halves.** Writing a corrected
`<title>` + `<lockdata>true</lockdata>` into a sidecar is not enough for an item
Jellyfin already holds unlocked — it re-scrapes and reverts the file, usually within
seconds. Set the lock in Jellyfin too:

```
GET  /Items/<id>?userId=<uid>          # full DTO
     -> set Name / Overview / PremiereDate
     -> LockedFields: ["Name","Overview"]   # protects those fields
     -> LockData: true                      # the WHOLE-ITEM lock == <lockdata>true</lockdata>
POST /Items/<id>
```

`LockedFields` and `LockData` are different locks and you want both. `LockedFields`
alone leaves the item unlocked, so Jellyfin keeps refreshing it and keeps writing
`lockdata=false` into the sidecar. (Seen doing exactly that while repairing Mushi-Shi
on 2026-09-11: the titles took and the `lockdata` edit was reverted within the second.)

Beware one measurement trap: `GET /Items?ids=…&fields=LockedFields` does **not**
return `LockedFields` in that list projection, so it reads as `None` even when the
lock is set. Check a single item's full DTO instead, or you will "fix" it twice.

Verified durable: after `Refresh?metadataRefreshMode=FullRefresh&replaceAllMetadata=true`
— the most aggressive refresh Jellyfin offers — all three repaired Mushi-Shi specials
kept their titles, premiere dates and locks, on disk and in the DB.

## 5c. "I filed comics but YacReader doesn't show them"

The reader never notices the filesystem on its own. A comic exists to it only after the
APP runs a library update, and the only trigger the fleet relies on is
`UPDATE_LIBRARIES_AT_STARTUP` in YacReader's own ini. On 2026-09-14 every ElfQuest file
was filed, on the mount, in the pool — and invisible, because both auto-update flags read
`false` and the app had been up since before the files landed. The supervisor now owns
this contract (patches the flags before every start, bounces a drifted app, consumes the
refresh marker `record_plan` drops when comics are filed, activates a crash-restored app
that came up with no library window), so the symptom should not come back — but the
tools exist if it does:

```bash
python3 scripts/yacreader_rescan.py            # flags + drift report (exit 1 on drift)
python3 scripts/yacreader_rescan.py --files    # also list shelf files the index lacks
python3 scripts/yacreader_rescan.py --apply    # lock, patch flags, request a rescan
python3 scripts/comic_shelf_audit.py           # empty dirs, stale rows, and the same
                                               # missing-from-index report
```

**The fleet now watches this for you.** `fleet_health` reports the reader in the same phone
report as everything else — a crash row, a damaged index, drifted flags, and shelf files
the index does not know about — and `fleet_doctor` repairs the two it safely can
(`refresh_yacreader`, `repair_yacreader_index`). A running scan is never mistaken for
staleness: `update_in_progress()` reads the transaction journal or an open archive, not
CPU. A *damaged* index is deliberately an owner remedy: restoring a backup needs the
newest one that PASSES `integrity_check`, which is a human read
(`scripts/yacreader_index_health.py`).

**If the reader CRASHES instead of showing nothing**, it is almost always the folder
tree: `FolderModel::createModelData` dereferences the parent it looks up
`ORDER BY parentId,name` with no null check, so a dangling parent, a cycle, a missing
root, or a parent that sorts after its child is a SIGSEGV (`FolderModel::reload`; the
2026-09-13 crash left the app up for ten hours with a stale index). Check before it
does:

```bash
python3 scripts/yacreader_index_repair.py            # read-only: names the crash rows
python3 scripts/yacreader_index_repair.py --apply    # lock, back up, repair, verify
python3 scripts/yacreader_index_health.py            # integrity + the backup census
```

The repair backs the index up under a name that PASSES `integrity_check` and edits under
the index lock; the supervisor restarts the app when the lock is released.

**The reader is meant to be invisible.** The fleet starts and bounces YacReader on its own
schedule (every comic filing consumes the refresh marker), and since 2026-09-19 each of
those starts ends with the window HIDDEN — `open -g` alone stopped it stealing focus but
not covering the screen. Hiding does not stop the library update. If you want to read, open
YacReader from the Dock: the supervisor only re-hides through a bounded 60 s window after
a fleet start or a supervisor restart, so a reader you opened yourself stays up. A line in
the supervisor log saying the window "could not be hidden" means the AppKit route failed
and the System Events fallback was refused — check Accessibility for the daemon if the
popups return.

## 5d. "A chapter vanished — why?"

Because a volume that contains it was verified present. Chapters and volumes are both
shelved (`Comics/Manga/<Series>/<Series> cNNNN.cbz` and `... vNN.cbz`), and when a volume
lands, the chapters it covers are retired on purpose — files first, then the MEGA copy
(the reaper drains the deletion queue), then the `library.db` rows. Nothing else deletes
a chapter.

The retirement is gated, and every gate fails toward keeping the chapter:

* the volume's file has to be verified present in the shelf enumeration;
* the volume→chapter map has to be authoritative — from MangaDex, or an AI answer cached
  at high confidence; an unknown volume covers nothing;
* the chapter number has to be in that volume's exact chapter SET;
* a keep rule must not apply (`state/manga_chapter_policy.json`; `keep_chapters` preserves
  a series' chapters, `keep_all` both tiers);
* a colored volume never authors a chapter purge — it may supersede a same-numbered grey
  volume instead.

**What to do when a chapter disappears and you want it back:** check
`state/decisions.log` (the reconciler logs the series, the volume and the paths it
purged), then `library_health.txt`. The volume holds the content, so there is normally
nothing to restore. If the volume should NOT have covered it, put the series on
`keep_chapters`:

```json
{"version": 1, "series": {"<series norm>": {"mode": "keep_chapters"}}}
```

and re-fetch the chapter (re-drop the release or drop the file into DirectIngest). A
chapter the fleet never had is not re-downloaded automatically — there is no discovery.

**To inspect before anything is deleted:** `scripts/audit_volume_chapter_coverage.py` is
the read-only census. It uses the reconciler's own enumeration and decision function, so
its `leftovers` count is exactly what `--apply` would remove. The 6-hourly
`chapterreconcile` daemon runs the same pass. `state/manga_volume_map.json` is the cached
map; delete a series' entry to force a re-fetch.

The pool side of a supersede is the ordinary deletion queue. Do NOT add a series to
`state/blocklist.json` to stop a supersede — the blocklist is for owner purges and
refuses the series' future drops.

---

## 6. Purging something, correctly

```bash
# 1. Manifest FIRST, from Media-Syncer's remote_inventory.json at the REPO ROOT.
# 2. Delete through the MOUNT, never ~/Media:
rm -rf ~/MediaLibrary/Shows/"<Title>"
# 3. The queue should grow by exactly the manifest count:
wc -l < ~/Developer/Media-Fleet/Media-Syncer/mediafs_deletions.jsonl
# 4. Kick the reaper ONLY IF IT IS IDLE (see 3.2):
pgrep -f 'Torrent-Ingest/reap.py' || launchctl kickstart -k gui/501/com.mikeyferguson.torrentreap
# 5. Delete the matching torrent from qBittorrent, or ingest re-fetches it.
# 6. Delete the orphan SIDECARS. Step 2 takes the videos; the .nfo / -thumb.jpg /
#    -poster.jpg / ._* beside them stay, and are why a "purged" tree still looks full:
find ~/MediaLibrary/Shows/"<Title>" -type f -print0 | xargs -0 rm -f
# 7. CHUNKED packs only -- retire the journal record, or it is re-adopted (see below):
grep '<info_hash>' ~/Developer/Media-Fleet/Torrent-Ingest/state/journal.jsonl | tail -1
# 8. Nothing to do: the reaper now supersedes the purged rows in library.db itself as
#    the last step of a verified purge (shows, films and comics). For rows the reaper
#    could not match, or content that left the library without a purge:
python3 ~/Developer/Media-Fleet/Torrent-Ingest/scripts/reconcile_library_db.py --apply --include-requested
```

**Step 7 is not optional for a chunked pack, and step 5 alone does not cover it.** A
chunked record whose `status` is still `downloading` gets re-adopted on the next cycle by
`_readopt_chunked`, which re-adds the torrent **from its source `.torrent` under
`finished/` or `ingesting/`** — so removing it from qBittorrent does not stop it. Observed
2026-09-12 19:59 on Monogatari: the pack was re-added twenty minutes after a completed
purge. It was harmless *only* because `chunk_done` already covered all 103 files, so the
daemon retired the record 21 seconds later and removed the torrent again. **Purge a pack
that is genuinely mid-download and it resumes fetching, undoing the purge.** Check the last
journal line for the hash reads `"status": "completed"` (or `failed`) before you walk away.

**Step 6 matters for the same reason the rest do:** the mount also synthesizes read-only
`Season NN` directories (`dr-xr-xr-x`, 1969 mtime) from pool copies still queued for the
reaper. Those are NOT stuck directories and `rmdir` will not remove them — they disappear
on their own when the drain finishes. Watch
`grep -ic '<Title>' ~/Developer/Media-Fleet/Media-Syncer/remote_inventory.json` count down to 0 rather
than hunting for them. And Jellyfin keeps three kinds of row that no library scan reaps: a
Series row pointing at the synthesized shell, an empty Playlist, and an empty BoxSet.
Find and delete them by API (`ChildCount`/children of 0 is the test for "empty"):

```bash
curl -s -H "X-Emby-Token: $JELLYFIN_API_KEY" \
  "$JELLYFIN_URL/Items?recursive=true&searchTerm=<Show>&fields=Path&limit=300"
curl -s -X DELETE -H "X-Emby-Token: $JELLYFIN_API_KEY" "$JELLYFIN_URL/Items/<id>"
```

`JELLYFIN_URL` / `JELLYFIN_API_KEY` live in the launchd plists
(`com.mikeyferguson.mediadoctor.plist`, `EnvironmentVariables`).

**Step 8 is automatic.** The reaper marks the rows for the paths it VERIFIED gone
`superseded` (never a survivor's: its pool copy still exists) — episodes and films by
title/item, comics by folder chain then file stem, and a collection only when exactly one
row could be meant. The same paused window then folds comic/manga kind splits to the kind
the pool shelves the files under, and supersedes numbered comic rows the pool no longer
holds (`dbhook.reconcile_comics`); ambiguous norms are skipped, never guessed. It is
fail-open; a DB error is logged and cannot fail the purge. What still needs the reconcile
command above: a collection in a series with several, and content absent without a purge.

**Then, at the start of the next session, run the standing check.** A purge followed by a
re-download leaves queue lines pointing at live content, and the reaper cannot tell them
apart. This has happened five times.

```bash
python3 - <<'PY'
import json, os
root = os.path.expanduser('~/MediaLibrary')
for q in ('mediafs_deletions.jsonl', 'mediafs_deletions.jsonl.processing'):
    f = os.path.expanduser('~/Developer/Media-Fleet/Media-Syncer/' + q)
    if not os.path.exists(f): continue
    live = [p for p in {json.loads(l)['path'] for l in open(f) if l.strip()}
            if os.path.exists(os.path.join(root, p))]
    print(q, len(live), 'still on the mount')
    for p in live[:20]: print('   ', p)
PY
```

Anything printed is a **rescue** — remove that line from the queue — unless a same-size
sibling for the same episode is present with its `.nfo`, which makes it a real duplicate.

---

## 7. Deploying a change

```bash
bash ~/Developer/Media-Fleet/ship-fleet.sh "what changed"
```

Commits and pushes the monorepo once, then restarts every daemon. It refuses to restart a
draining reaper.

**The script itself lives at `~/Developer/Media-Fleet/ship-fleet.sh`, at the root of the one
repository** (since the 2026-09-13 merge everything is tracked there; before that it was
deliberately untracked, because owning it from any single repo was a risk). Being at the
top of the monorepo is the same protection without the untracked trade-off: it cannot be
mistaken for a single project's property, and it now has history.

`verify_fleet.sh` does not check it, so a syntax
error there surfaces only when you next try to deploy — run `bash -n ~/Developer/Media-Fleet/ship-fleet.sh`
after editing it.

Before you ship: `verify_fleet.sh` must pass all 52. After you ship: check the daemon got a
new PID and no NEW traceback appeared — check the log's modification time first, because
`DirectIngest.err` holds 2,176 stale ones from August.

---

## 8. Things that are known, unfixed, and not urgent

* **`library.db` ownership is maintained by two passes.** `reap.py` supersedes the purged
  paths' rows itself (step 8 of §6) as each purge completes, and
  `scripts/reconcile_library_db.py --apply --include-requested` sweeps anything else that
  holds owned rows for absent content. **What can still over-claim:** a purged comic whose
  folder chain and file stem match no series, a collection in a series that holds several,
  and any content absent without a purge. For those, delete that title's rows by hand
  (`sqlite3 state/library.db`) or re-run the reconcile command — the tool skips manga/comics
  it cannot enumerate, so hand-deletion is often the only route.
* **Jellyfin's DB grows orphaned rows.** `jellyfindbguardian` repairs them, and has done so
  94 times. The repair works; the cause has never been found.
* **A daemon that HANGS without exiting is still undetected.** `KeepAlive` restarts a
  process that dies and `fleetdoctor` re-bootstraps a job that vanishes, but neither
  notices one that is alive and stuck.
* **`library_supervisor` cannot tell "Jellyfin is hung" from "Jellyfin is busy scanning".**
  It restarts Jellyfin when the authenticated API has not answered for 90 seconds
  (`SUPERVISOR_UNRESPONSIVE_SEC`), and during a full scan of the FUSE-mounted library the
  API genuinely takes minutes to answer — a scan observed on 2026-09-10 left it slow enough
  that direct queries needed 120–240s. The signature the check fires on (authenticated
  requests time out while unauthenticated ones answer) is the SAME signature as a real hang
  it was written for, so it cannot simply be relaxed. It has restarted Jellyfin 14 times,
  most recently 2026-09-05, and did not fire during the 2026-09-10 scan because the API
  answered intermittently enough to keep resetting the timer. **If a big scan ever stops
  completing, this is the first thing to suspect** — a restart aborts the scan, and the
  next scan starts from the beginning.
* **Leaked MEGA session tokens.** Two `session_id`/`master_key` pairs reached the
  Media-Syncer repo's git history. They are out of HEAD and a pre-commit hook blocks
  recurrence, but history still holds them; invalidating them means logging those accounts
  out at MEGA's end.
