# Torrent-Ingest

An autonomous pipeline that turns "drop a `.torrent` into an iCloud folder" into
"the show is in my Jellyfin library, correctly named, correctly placed, and
watchable" — on a small local disk, and without ever risking a file that already
exists in the library (the lone exception is **One Pace**, whose re-releases are
meant to *replace* the older cut — § Already-present media is a success).

Drop a `.torrent` into the watched iCloud folder from any machine. The Mac Mini
— which has the SSD library root mounted and the download role — does everything
else: downloads it locally, has **an AI run** decide how the files map onto
the library — Jellyfin for video, YACReader for comics/manga — applies that
mapping onto the SSD, verifies it landed, and only then frees the local storage
and files the source `.torrent` away in a `finished/` folder. Nothing is marked
"done forever": drop the same `.torrent` back into the watch folder and it
redownloads from scratch.

It is the third daemon in the fleet, a sibling to **Media-Syncer** (which then
replicates the new files to the MEGA cloud pool) and **YouTube-Downloader**, and
follows the same `launchd`-daemon shape. It deliberately reuses two ideas proven
in those repos: a crash-resumable **state journal**, and a **dot-dir staging**
trick so a half-finished write is invisible to Media-Syncer's uploader.

---

## The core problem it solves, and the three hard sub-problems

Downloading directly onto the the SSD library root drive throttles everything — the syncer,
the downloader, the whole machine. So torrents must land on the fast local disk,
be organized to Jellyfin/TMDB conventions, and be moved onto the SSD. Doing that
by hand is the chore this repo eliminates. Three parts of it are genuinely hard,
and the design is shaped entirely around them:

1. **Disk exhaustion.** You cannot fit "all of Naruto + Dragon Ball + Bleach" on
   the local disk at once. Solved by **disk-budget admission** (§ Concurrency and
   the disk budget): the daemon runs as many torrents at once as fit the local
   free-space budget — reserving each one's still-to-fetch bytes so the disk can
   never fill even if every active download completes — and each local copy is
   deleted the moment it is safely copied onto the SSD, freeing room for the next.
   The one torrent that can't fit the local disk *even empty* (a single 400 GB+
   pack) is **refused** — it is never spilled outside the SSD, because a torrent's I/O
   on that drive wedges the library mount (§ Torrents too large for the SSD).
2. **Correct placement.** `guessit` reads a filename; TMDB reads its own
   database; **neither reads your library**, where the real numbering decisions
   already live (Jujutsu Kaisen as one continuous 47-episode season; DBZ Kai
   specials scattered into later arcs; Monogatari's movie/special ambiguity).
   Solved by making a headless **AI run** the identifier, with the existing
   library on disk as its ground truth (§ Identification).
3. **Irreversible deletes downstream of a fallible guess.** The pipeline's final
   act is deletion. An LLM is non-deterministic and occasionally confidently
   wrong. Solved by a hard split: **the model only proposes**; a deterministic
   harness validates, applies, verifies, and deletes — and never deletes before
   the files are proven present in the library (§ Safety invariants).

---

## Topology and role

**The three repos of the fleet, all on the Mini.** Each documents its own half of every
shared contract, so read the sibling README rather than inferring behaviour from this one:

| Repo | Role | Its README covers |
|---|---|---|
| **Torrent-Ingest** (this) | acquisition and placement: torrents, direct all-media ingest (+ the iCloud drop bridge), drive ingest, the plan API, Jellyfin health | the ingest state machine, the plan schema, the reaper, playlists, `media_doctor` |
| **Media-Syncer** (`~/Developer/Media-Fleet/Media-Syncer`) | replication to the MEGA pool, the `mediafs` virtual library, pre-download and eviction | the sync cycle, the purge/rename runbooks, the mount, the split tunnel |
| **YouTube-Downloader** (`~/Developer/Media-Fleet/YouTube-Downloader`) | YouTube discovery and download, placed through **this repo's** plan API | discovery, download waves, routing, the soundtrack split |


This daemon runs on the **Mini** only. That is not a config toggle, it is an
assumption baked into the design, and it lines up with the fleet:

- The Mini is the machine with the library mounted (`mediafs` at `~/MediaLibrary`, backed by the real media at `/Volumes/the SSD library root/MediaStore`).
- The Mini is the sole host that runs Media-Syncer (which replicates new media to
  the MEGA pool). Torrent-Ingest is the front half of that same pipeline — it puts
  the media on the SSD for Media-Syncer to replicate — so it belongs on the Mini too.
- The watch folder is in **iCloud Drive**, so a `.torrent` dropped from **any
  device** (a laptop, a phone) syncs to the Mini, where this daemon picks it up.
  The drop point is just iCloud; only the Mini runs any of the fleet daemons.

Downstream, the files this daemon writes to the SSD library root are seen by Media-Syncer as
new local files and uploaded to the MEGA pool on its next cycle. **Local Jellyfin
playback does not wait on that upload** — a title is watchable as soon as it is on
the SSD library root and Jellyfin has scanned it.

---

## Virtual library integration (MediaStore, the mount, the supervisor)

Media-Syncer now runs a **virtual library**: `mediafs` mounts at `~/MediaLibrary` (a local SSD path Jellyfin/YacReader read) with the real media at **`/Volumes/the SSD library root/MediaStore`** as its backing store; cold files are evicted and streamed back on demand (see Media-Syncer's README, *Virtual library*, incl. the offline path+GUID migration that moved the apps onto the local mount without losing watch-state). Three things follow for this repo:

* **`config.MEDIA_ROOT` is now `/Volumes/the SSD library root/MediaStore`** — this daemon reads/writes the *real* media dir directly (files it lands there appear in the mount via passthrough). Keep it in step with Media-Syncer's `EXTERNAL_DIR`.
* **The library app supervisor** (`library_supervisor.py`, agent `com.mikeyferguson.librarysupervisor`, a KeepAlive sibling of db_guardian) owns startup ordering. macOS's "reopen apps at login" relaunches Jellyfin + YacReader before the mount is up — pointed at an empty dir they show no media, and a Jellyfin scan of an empty library can gut its DB. So the supervisor **holds both apps down until the mount is healthy** (mounted + `Shows`/`Movies`/`Comics` non-empty, with a timeout so heavy scan load isn't misread as "down"), then keeps them running; if Jellyfin comes up with a collapsed item count it restores db_guardian's last-good backup (which `db_guardian`'s gut-guard, `DBG_MIN_ITEM_FRACTION`, protects from ever being overwritten by a gutted snapshot).
* **YacReader's index needs a lock the filesystem cannot give it** (`yacreader_db.py`). YacReader's registered library root is the *mount*, so the app opens and **writes** `~/MediaLibrary/Comics/.yacreaderlibrary/library.ydb` through FUSE, while every fleet tool opens the **same physical file** on the SSD at `~/Media/Comics/.yacreaderlibrary/library.ydb`. `mediafs` implements no `lock` operation, so byte-range locks taken on the two paths are in different domains and cannot see each other — SQLite believes it has the database exclusively in both processes at once, and the overlap corrupts it (a doubly-referenced btree page, rowids out of order, `comic_info` rows missing from their own autoindex). The exclusion is therefore built one level up: a tool takes `state/yacreader_db.lock` via `yacreader_db.db_lock()`, which stops the app, and **the supervisor refuses to start YacReader while that lock is held**, starting it again on the first tick after release — so no caller has to `bootout`/`bootstrap` launchd by hand (a runbook step that left Jellyfin unsupervised too whenever a session died half way through it). `scripts/yacreader_index_health.py` reports the live verdict plus a per-backup census, because the corruption is *partial* — `folder` answers while `comic` does not, so row counts read healthy and only `PRAGMA integrity_check` catches it. That census is why "restore the newest backup" is the wrong instruction and **"restore the newest backup that passes `integrity_check`"** is the right one: a backup taken on the way *into* a repair is a backup of the damage.
* **YacReader's index only moves when the APP updates it** (`yacreader_db.py`, `yacreader_index.py`). The reader never notices the filesystem on its own: a filed comic exists to it only after a library update, and the only trigger the fleet can rely on is `UPDATE_LIBRARIES_AT_STARTUP` in the app's own ini. On 2026-09-14 every ElfQuest file was filed, on the mount, in the pool — and invisible in the reader, because both auto-update flags read `false` and the app had been up since before the files landed. So the flags are now a supervised invariant: `library_supervisor` patches them before every start, bounces a running app whose flags drift, and **consumes `state/yacreader_refresh_request` WITHOUT restarting the app** — a marker `dbhook.record_plan` drops whenever a plan files comics. Restarting was the old refresh trigger, but a restart lands YacReader on its library CHOOSER (it never re-opens a library by itself; measured 2026-09-19: quit+relaunch, `open -a`, CLI args and `open` document events all leave it there), so every filing left the reader not scanning until a human clicked Comics — and took the owner's screen each time. The app's own periodic update is the refresh mechanism now, enforced on (30 minutes, the finest cadence it offers: `UPDATE_LIBRARIES_PERIODICALLY_INTERVAL` is an enum index where 0=30 min). Owner decision, 2026-09-19. Two failure modes get their own handling: an app that is **up but has no library open** (a crash restore leaves no window, `LibrariesUpdateCoordinator::init()` never runs, and nothing scans) is detected by the open index and activated; an app that dies and comes straight back is a **crash** and is backed off with an alert rather than restarted forever. When activation does not reach a chooser-parked app, the supervisor **alerts and leaves it alone** — a restart does not make the chooser open the library, it only interrupts whatever the owner was doing; the alert names the remedy (open the Comics library once). **Every fleet-initiated start/activate is hidden — but only once its library update is underway.** Owner report 2026-09-19: the reader "keeps popping up and taking over the whole screen" (every comic filing bounces it, and `open -g` stops focus-stealing but not the window appearing). `yacreader_db.hide_app()` uses AppKit's `NSRunningApplication.hide()` through AppleScriptObjC first — no Accessibility grant needed — with System Events only a fallback. The hide is ARMED at every fleet start/activate and on supervisor restart, and fires on the first tick where EITHER `update_in_progress()` is true (proof `LibrariesUpdateCoordinator::init()` has run, and a Cmd-H does not interrupt the SQLite transaction) OR the `SUPERVISOR_YAC_HIDE_SETTLE_SEC` window has passed — the window is created at launch, so after that it either exists or never will, and the settle path covers an app parked on the library CHOOSER that never starts an update at all (measured 2026-09-19; the reader's index had not moved since Sep 18). Once hidden, the supervisor stops touching it, so a reader the owner opens himself is never fought. `scripts/yacreader_rescan.py` is the human override, and `scripts/yacreader_index_repair.py` names and repairs the row shapes that crash the loader: `FolderModel::createModelData` dereferences the parent it looks up `ORDER BY parentId,name` with no null check, so a dangling parent, a cycle, a missing root, or a parent that sorts after its child is a SIGSEGV (`comic_shelf_audit` enables `PRAGMA foreign_keys` so its deletions cannot create that shape).
* **The Google Drive supervisor** (`gdrive_supervisor.py`, agent `com.mikeyferguson.gdrivesupervisor`, a KeepAlive sibling of library_supervisor) keeps the Google Drive macOS app running. Light novels are filed into the Google Drive `Novels` folder (not YACReader), so the Drive app must be up or every novel placement stalls. The supervisor starts it if it is down and alerts when the app is up but the `Novels` mount never becomes accessible. It never *stops* anything and never gates the media daemons — a dead Drive only delays novels, never Shows/Movies/Comics. Low Drive storage is a human problem reported by `fleet_health`, not fixed here.
* **The reaper is now queue-driven, not snapshot-driven** (see below) — a file vanishing from the SSD library root now means it was *evicted*, not deleted, so the delete signal moved to an explicit through-the-mount unlink.

---

## The pipeline (state machine)

Every `.torrent` is one record in the journal, keyed by its BitTorrent v1 info
hash, and advances through this state machine:

```text
QUEUED -> DOWNLOADING -> DOWNLOADED -> IDENTIFIED -> STAGED -> VERIFIED -> COMPLETED
                                                                   |
                                                    (any step can ->) FAILED
```

The states, with the precondition to enter, the action taken, and what happens
on a crash-and-resume from that state:

| State | Entered when | Action on entry | Resume behavior |
| --- | --- | --- | --- |
| **QUEUED** | a new `.torrent` is seen and its info hash is journaled | none; waits for a free pipeline slot and disk space | re-evaluated each cycle; idempotent |
| **DOWNLOADING** | disk budget check passed; torrent resumed in qBittorrent | poll qBittorrent for completion | re-queries qBittorrent; if still present, polls as usual; if **gone**, decides by disk — on-disk payload ≥ 99.9% of `total_size` → `DOWNLOADED` (qBittorrent auto-removed it at 100%), else → `FAILED` |
| **DOWNLOADED** | qBittorrent reports 100% | record the on-disk `content_path` | proceeds to identify |
| **IDENTIFIED** | the run wrote a plan and it passed validation | store the validated plan | proceeds to apply (see the partial-apply caveat under § Recovery) |
| **STAGED** | all planned files copied to staging and atomically moved into place | record the applied `{src,dst,size}` list | proceeds to verify |
| **VERIFIED** | every applied file confirmed present (moved files by size; already-present files by existence) | **journaled before any deletion** | re-runs cleanup (idempotent) |
| **COMPLETED** | local download deleted; source `.torrent` filed into `finished/` | terminal | no-op |
| **FAILED** | any step raised | record the error; **nothing pre-existing touched**; local download left for inspection; source `.torrent` **filed into `failed/`** so it leaves the watch folder | terminal; not auto-retried while it sits in `failed/`. **Move its `.torrent` back into the watch folder to retry** — the reappearance re-queues it from scratch (no journal edit needed) |

The transition functions live in `ingest.py` as `_advance_downloading`,
`_advance_identify`, `_advance_stage`, `_advance_verify`, `_advance_cleanup`.
A single `advance(record)` dispatches on `status`, and once a torrent reaches
`DOWNLOADED` the remaining transitions cascade within one cycle (identify →
stage → verify → cleanup) rather than waiting a poll interval between each.

**A partial plan is PARKED, not completed (2026-09-19).** When the plan-coverage
contract finds media the plan never accounted for, or a planned file collides with a
differently-named file already at its slot, nothing is applied and nothing is
deleted: the record goes FAILED with `unfiled` (whole-torrent) or `chunk_unfiled`
(chunked) naming the files, the local download stays where it is, and the `.torrent`
is filed under `failed/` for review. A re-drop after the cause is fixed resumes only
the chunked progress the record can still PROVE. See safety invariants 6 and 7.

**Orphaned sources in `queued/` and `ingesting/` have a way out (2026-09-19).**
`find_drop_files` scans only the watch root's top level, so a source filed into a
state folder is never seen again by registration. `ingest.sweep_orphan_sources` runs
every cycle: a terminal record's leftover source goes to `finished/` (or `failed/`),
an iCloud `" 2"` duplicate of a tracked source goes to `finished/`, a live record
whose recorded copy is gone ADOPTS the survivor, and a hash with no record at all
returns to the watch root for registration. It never deletes and never touches a
source it cannot parse. Guard: `scripts/test_orphan_sources.py`.

### The acceptance gate on the magnet path (§4.120)

A magnet leaving `QUEUED` passes one more check than a `.torrent` does, and it exists
because the fleet's authoritative acceptance gate was **dark for four days**.

That gate is `librarybrain/acceptance.py` and answers one question from a torrent's
**file list**: does this contain any library item we do not already own at equal-or-better
quality? It lived in Torrent-Searcher until 2026-09-10 and moved here with the rest of the
library brain when discovery was removed. It was only ever wired to the `.torrent` drop
path. When every
`.torrent` cache began serving truncated files, `write_magnet` became 100% of drops, the
gate stopped being *called*, and — because a guard that is never reached looks exactly
like a guard that is passing — nothing noticed while the queue filled with repeats.

A magnet has no file list at drop time. It has one **here**: qBittorrent pulls the
metadata from the swarm for a few kilobytes before any content is fetched, which is
precisely the input the gate wants and the last moment at which refusing is free. So
`_admit_magnet` calls `acceptance_gate.check()` the instant metadata resolves, before the
size is persisted and before any disk budget is committed.

**Both drop paths are gated here, since 2026-09-07 — and that is the point.** The fix
above put the gate on the magnet path and left the `.torrent` path's gate where it had
always been: in the searcher, at drop time. That is still a gate on ONE of two paths, and
the traffic shifted again, the other way. With the searcher quarantined, every `.torrent`
reaching the watch folder — hand-drops included, which is now the fleet's ONLY acquisition
route since discovery was deleted on 2026-09-10 — was admitted with nothing judging it.

**A hand-drop reaches no verdict, and that is expected.** The gate answers from the
searcher's torrent ledger, and a hand-dropped `.torrent` has no ledger row, so it returns
UNKNOWN and is admitted, logged and counted. With discovery gone that is now the normal
outcome for every drop. What still binds is the SAFETY half beside it,
`acceptance_gate.metadata_is_safe`, which refuses a hostile file list (path traversal,
absolute paths, executables) before qBittorrent is asked to add anything — that check used
to live in the searcher and was ported here when the searcher was removed, precisely
because hand-dropping became the only way in. Measured on 2026-09-07: 100% of
the day's drops were `.torrent`, and the gate had been silent for 50 hours while they
landed. A `.torrent` carries its own file list, so `admit_downloads` reads it with
`acceptance_gate.file_names_from_torrent()` and gates it **before `qbt.add`** — earlier
and cheaper than the magnet path, where nothing has been added and a refusal costs
nothing. Both halves run the same `_acceptance_gate` body, so they cannot answer
differently. Guard: `scripts/test_torrent_gate_path.py`, blocking in `verify_fleet.sh`.

The general lesson, paid for twice: **a gate that lives on one of several paths is one
traffic shift away from dark, whichever path it is on.** Ingest is the single point every
admission passes through, which is why both halves belong here rather than at each source.

| verdict | what it means | what happens |
| --- | --- | --- |
| **accept** | at least one file is an item we lack, or is a genuine upgrade | admitted, as before |
| **refuse** | every file is already owned **and** the mapping was corroborated | removed from qBittorrent, retired as `refused` (a deliberate decline, not a failure — see `journal.REFUSED`) |
| **unknown** | no verdict: no ledger row, an unreadable file list, or an "all owned" claim its own mapping could not corroborate | **admitted**, logged, and counted |

`unknown` is admitted on purpose. §7 says a fallback for "I cannot verify this" must not be
"accept it" — but here the alternative is not *defer*, it is *destroy*: the torrent is
already queued, no later sweep re-offers it, and an unreadable file list does not become
readable by waiting. What §4.120 actually lacked was not strictness but **visibility** — it
admitted on nothing at all, silently. An admitted `unknown` is a number in the heartbeat,
and the size of that number is the argument for fixing the parser gaps behind it.

**Series identity is looked up, never guessed.** The gate must know which series a torrent
is for, and a release title is not a reliable route to one. The searcher records the answer
in the shared `torrents` ledger against the infohash at drop time, so
`acceptance_gate.check` reads it back; every queued and downloading record in the live queue
had one. No ledger row is an `unknown`, not a guess.

**It cannot stop the daemon starting.** `acceptance_gate` is the one import here that
depends on another repository's working tree, so it is loaded defensively (§4.109: a pull
must never be able to stop a daemon starting). If it cannot load, ingest logs that at
startup, runs ungated, and `fleet_health` raises it as an ACTION — a silently ungated fleet
is the one outcome ruled out.

**Is it alive?** `python3 scripts/gate_status.py` (exits non-zero when dark) — or read
`fleet_health`'s report. Kill switch: `ACCEPTANCE_GATE=0` (the old `MAGNET_ACCEPTANCE_GATE`
is still honoured, and now governs both paths).

A note on reading the heartbeat: it goes stale only when an ADMISSION happens without a
verdict. A fleet with an empty queue reports its last verdict from whenever the last drop
was admitted, and that is correct — `fleet_health` only calls it dark when drops are
arriving too.

### The watch folder is a state mirror: four subfolders, three documents

The iCloud watch folder's **top level** is reserved for the human-facing
documents — `library_health.txt` (`media_doctor`), `mega_free_space.txt` (Media-Syncer's
`check_space`), `fleet_health.txt` (the `fleethealth` watchdog, below), `fleet_doctor.txt`
(what the doctor fixed and what needs you) and `find.txt` (Title-Scout's inbox) — plus the
`.torrent` files the owner drops, and nothing else. (`new.txt`, `compilations.txt`,
`acquisition_mode.txt` and `would_download.txt` were the searcher's inbox, mode switch and
drop ledger; they went with it on 2026-09-10.) Every `.torrent` lives in one of four subfolders that mirror its
journal status, so queue depth and in-flight work are visible from Finder/phone:

| Folder | Holds | Status |
| --- | --- | --- |
| `queued/` | a `.torrent` waiting for a free pipeline slot / disk space | `QUEUED` |
| `ingesting/` | a `.torrent` being worked | `DOWNLOADING` → `VERIFIED` |
| `finished/` | a completed torrent (re-droppable to redownload) | `COMPLETED` |
| `failed/` | a torrent that gave up (re-droppable to retry) | `FAILED` |

`register_new_torrents` moves a `.torrent` dropped at the top level (always by hand now)
into `queued/` as it journals it;
`admit_downloads` promotes it to `ingesting/` the moment it is admitted; cleanup or
failure files it to `finished/` / `failed/`. The move is just `os.replace` within
iCloud and the journal's `torrent_path` is updated at each hop, so a crash-and-resume
picks up the `.torrent` wherever it currently sits. Re-dropping still means moving a
`.torrent` back to the **top level** (see below).

**`DirectIngest/` is the one subfolder that is an input, not a state mirror.** It is the
cross-device drop point for RAW media — a loose `.mkv`/`.mp4`, a `.cbz`/`.epub`, or a
folder of them — for when the owner downloads something directly instead of handing over
a `.torrent`. `direct_ingest_bridge.py` watches it, materializes each drop (iCloud may
hand it over as a dataless placeholder), and MOVES it onto the local
`~/Downloads/DirectIngest/`, where `direct_ingest.py` files it. The folder is emptied by
design, and the iCloud census treats a falling count there as the fleet working rather
than as a §4.13 loss (it is still snapshotted).

Registration runs at **both the start and the end of each cycle**, not just once:
the per-torrent `advance()` sweep can take many minutes when the chunked backlog is
large, so a `.torrent` dropped while that sweep is still churning is filed into
`queued/` at the end of the *same* cycle rather than waiting a whole extra cycle.
The call is idempotent — the end-of-cycle pass only sees drops that landed after
the start-of-cycle pass.

### Completion is not retirement (the `finished/` folder)

`COMPLETED` does **not** delete the source `.torrent` or permanently retire it.
The `.torrent` is **moved into `finished/`** — a subfolder of the watch folder,
scanned only at its top level so a filed torrent is never re-ingested on its own.
This is deliberately not "mark it done in the journal and delete the `.torrent`
forever" behavior, which made a torrent that hit a weird state impossible to
redownload without hand-editing the journal. Now the rule is simple and file-based:

- **A `.torrent` in the watch folder is work; one in `finished/` is done; one in
  `failed/` gave up.** A FAILED torrent's source is filed into a `failed/` subfolder
  (a sibling of `finished/`, likewise not scanned for new work), so a dead torrent
  leaves the watch folder instead of sitting there indistinguishable from a queued
  drop. Both subfolders are re-droppable, and **moving a `.torrent` back up into
  the watch folder's top level is itself the retry signal** — no journal edit
  required. A failure is not *auto*-retried (it won't restart on its own while it
  sits in `failed/`), but the deliberate move-back is honored as a retry request.
- **Re-drop to redownload.** Drop a `.torrent` back into the watch folder's top
  level (or it reappears there) and it ingests again from scratch — even if the
  journal still has a `COMPLETED` **or `FAILED`** record for its hash.
  `register_new_torrents` detects the reappearance of a terminal hash at the top of
  the watch folder and re-queues it into `queued/`. This is exactly why the four
  state subfolders' contents are never scanned: a terminal source only re-enters the
  pipeline when *you* lift it back to the top. The journal is progress state, not a
  permanent block on re-downloading.

### A truncated `.torrent`: recovered as a magnet, or filed to `failed/`

A `.torrent` whose bencode will not parse has **no info hash**, so it can never get
a journal record — the journal is keyed by that hash. It is the one input the state
machine above cannot represent directly, and it gets one of two handlings, both on
the filesystem.

The cause is essentially always **truncation** — an interrupted download, or an
iCloud sync that stopped short — which shows up as a parse error at a byte offset
*past the end of the file* (`bad bencode at byte 471292` on a 471,040-byte file). A
truncated file is missing the tail of its `pieces` blob, so its name and file list
often still read fine while the torrent as a whole is unusable.

**Every drop here is named with its 40-hex info hash** (`04CFA…C2.torrent`), and
when the bencode will not parse that filename **is** the hash. So
`_recover_truncated_torrent` rebuilds the drop as a magnet: it synthesizes
`magnet:?xt=urn:btih:<name>` from the filename, lifts the display name and
announce-list out of the partial bencode (`qbt.salvage_from_truncated_file` — the
info dict's `name` and the top-level `announce-list` sit before the cut), registers
it through the normal QUEUED magnet path, and **removes the dead bytes** — a
recovered drop must not sit in `failed/` reading as a failure while its torrent is
queued and downloading. qBittorrent then pulls the **real** metadata from the swarm,
exactly as it would for a `.magnet` drop, and the pipeline proceeds. This is the
fallback the searcher used to provide — it wrote a sibling `.magnet` whenever a
`.torrent` cache served a truncated file — restored to ingest itself when the
searcher was deleted on 2026-09-10.

**A repeat drop of the dead bytes is discarded, not recovered again.** The bytes are
only a retry signal for a hash with no live record; when the hash is already
queued/downloading, `_recover_truncated_torrent` sees it and unlinks the duplicate,
logged `Removed dead truncated .torrent … is already <status> in the journal`. A
duplicate `.magnet` for a live hash is filed beside its record instead of stranding
at the top of the watch folder. Only a terminal record (COMPLETED/FAILED/REFUSED) is
re-queued from a re-drop, per the normal re-drop rule.

**Without a hash in the name there is nothing to recover.** That drop is moved into
`failed/` and logged `Filed unreadable .torrent under failed/`. Filing is
idempotent, and that is the diagnostic: nothing about moving the file changes its
bytes, so lifting it back into the watch folder re-runs the same parse and it lands
back in `failed/` within a cycle. A `.torrent` that keeps returning to `failed/` is
truncated and cannot be recovered: **replace the file, do not re-drop it.** Re-drop
is the retry signal for a torrent that failed *downstream* of parsing; there is
nothing to retry here. (A hash-named file that keeps returning is not this case —
its recovery discards the duplicate and says so in the log.)

`config.UNPARSEABLE_GRACE_SEC` (120 s) keeps that from catching a drop mid-flight.
A file iCloud is still materializing can be readable-but-incomplete for a few
seconds, so a parse failure is only treated as final once the file has been
untouched for that long; before then it is left alone and retried next cycle. A
dataless placeholder never reaches this path at all — `materialize` gates it first
(§ iCloud materialization).

### Already-present media is a success, not a failure

If, after all the identify/rename logic, a planned file **already exists** in the
library, that is a **success**, not an error. The pre-existing file is left
untouched (never overwritten — the two narrow exceptions are One Pace re-cuts and
anime quality upgrades, both § below) and its freshly-downloaded
local copy is simply dropped. A torrent whose files are *all* already present runs
straight through to `COMPLETED`: nothing is moved, the local download is deleted,
and the `.torrent` is filed under `finished/`. This is what makes a redownload of
something you already have harmless — it just cleans up after itself.

For this to work, the identify run must **still list the already-present files** in
its plan (the applier detects each existing destination and skips it). Returning an
**empty `files` list** because "it's all already there" is *not* a shortcut — the
validator still rejects an empty plan (`plan.files must be a non-empty list`), so the
torrent would FAIL instead of completing. The identify prompt is explicit about
this; an empty plan is correct only for a torrent that contains no library media at
all.

### An empty plan over media is a skip, not a failure — except a dual-audio upgrade

The identify prompt tells the run to list already-present files rather than return an
empty plan, but two "nothing to place" verdicts still slip through as empty plans over
a download that *does* contain media: a **repeat** of content already in the library
(the run took the "already present" shortcut anyway), and **extras with no library
home** (a creditless OP/ED, a TV commercial/CM, an NCOP placeholder, a bonus short the
provider doesn't carry). Both used to FAIL on `plan.files must be a non-empty list`.

`_advance_identify` now treats that shape as a **success**: nothing is moved, the local
download is dropped, and the `.torrent` is filed under `finished/` (`_skipped_repeated`).
The one exception is a **dual-audio upgrade** — `_looks_like_dual_audio` reads the
torrent name for a dual/multi-audio marker, because that is the searcher's explicit
upgrade signal. A dual-audio torrent that produces an empty plan would otherwise be
silently dropped by the write-once library, so it is FAILED for review instead of
quietly completing. This is what made `failed/` mean "something worth debugging" rather than "a
deterministic auto-fail the searcher should have filtered": the searcher declined the
structurally-unplaceable drops (single-file-too-large, `.rar`/`.7z` archives,
creditless/NCOP/CM extras) before they ever reached ingest.

**That pre-filter is gone with the searcher (2026-09-10), and the difference lands on
you.** Nothing screens a hand-drop for structural placeability any more, so a
single-file-too-large pack or an all-extras release now reaches ingest and fails HERE
instead of never being dropped. `failed/` is therefore a slightly noisier signal than it
was: it still means "something worth a look", but some of what lands in it is now simply a
release that was never placeable, rather than a bug. The safety screen that DID survive is
`acceptance_gate.metadata_is_safe` — hostile file lists are still refused before
qBittorrent sees them.

The **chunked** path mirrors this. `_identify_wave` and `_ingest_one_file` accept an
empty plan over media the same way `_advance_identify` does: a wave (or single file)
that is entirely extras or already-present media returns `(True, {}, set())` / `_NO_HOME`
and is dropped cleanly through the existing junk branch, instead of `validate_plan`
rejecting the empty list and the file burning its retries into `chunk_failed` — which
used to sink an otherwise complete pack (a chunked Hyouka release whose four NCED/NCOP
extras produced an empty plan ended the whole torrent FAILED).

A **creditless or standalone OP/ED, an NCOP/NCED, a TV CM, a placeholder, or a
trailer/preview/sample/teaser/menu/promo** is deleted like a declined manga chapter —
its bytes are freed, it is never renamed and never a failure
(`_looks_like_no_home_extra`). This holds even on a dual-audio torrent: the
dual-audio upgrade exception only fires for a file/wave that is *not* an extra, so a
dual-audio pack's creditless openings or `Trailers/*.mkv` files no longer sink the whole
torrent (the [FLE] Vivy, [Judas] Sangatsu no Lion, and dual-audio Cautious Hero trailer
cases).

### Loose page images are packaged; only truly-empty drops are skipped

A drop with **no library media at all** is a success (a skip), not a failure — and a
drop of **loose scanned page images** is not "no media", it is *packageable*. The two
cases split deterministically by extension, and the canonical case covers both: a
complete **Junji Ito manga collection** shipped as **loose page images** (`*.jpg`/
`*.png` + `Thumbs.db`) in per-story folders — thousands of files, zero
`.cbz`/`.cbr`/`.zip`/video. YACReader cannot shelve a bare pile of pages, but a `.cbz`
is literally a ZIP of page images, so a story folder *can* be shelved once packaged.

- **Loose page images → packaged into `.cbz`.** The identify run plans each story
  folder with a **directory `src`** and a `.cbz` `dst_rel`
  (`Comics/Manga/<Series>/<Series> - <Story> v01.cbz`); `apply_plan` zips the
  folder's pages into the `.cbz` deterministically — every page image in natural
  page order (digit-aware, so page 2 precedes page 10), non-page clutter
  (`Thumbs.db`, release `.txt`/`.nfo`, `.DS_Store`) excluded, written `STORED`
  since the images are already compressed. The run still applies its normal
  judgment: a folder whose material is already in a collected edition on the
  library is left out (redundant — deleted with the download), and a multi-folder
  work is filed as successive volumes. `validate_plan` accepts a directory `src`
  only for a `Comics` `.cbz`, and `_heal_missing_src`'s directory analogue
  (`_heal_missing_dir`) heals a retyped story-folder name before failing closed.
  The identify listing surfaces these folders as packageable work rather than
  marking them "no media — ignore", so the run plans them instead of skipping.

- **The raw pages are never uploaded — only the packaged `.cbz` is.** This is a
  hard structural guarantee, not a filter:
  * the loose `.jpg`/`.png` download into `INCOMING_DIR`
    (`~/Downloads/.torrent-ingest` on the local SSD), which is a *separate tree*
    from `MEDIA_ROOT` — Media-Syncer watches and replicates `MEDIA_ROOT` only, so
    it never sees the pages;
  * `apply_plan` never copies the pages themselves into the library — for a
    directory `src` it runs `_zip_loose_pages`, writing a single `.cbz` into the
    staging dot-dir (`MEDIA_ROOT/.ingest-staging/`, invisible to the uploader)
    and then `os.replace`ing *that* into `Comics/…` — the only thing that ever
    lands under `MEDIA_ROOT` is the finished archive;
  * cleanup then deletes the pages: `qbt.remove(delete_files=True)` plus
    `_delete_local_content()`, which is hard-confined to `INCOMING_DIR` and
    refuses to touch anything outside it. So the pages are transient scratch on
    the SSD, gone the moment the `.cbz` is verified in place, and Media-Syncer
    uploads the archive — never the unpacked pages.

- **Nothing ingestible or packageable → a skip.** If the identify run returns an
  empty `files` list *and* the download has no media extensions, the drop is a
  legitimate "nothing to shelve" verdict: it runs straight through to `COMPLETED`,
  the local download is deleted, and the `.torrent` is filed under `finished/` (a
  success, same as already-present media — not a failure). Over a download that
  *does* contain media, an empty plan is still rejected — that is the
  "already-present" shortcut the validator exists to stop, not a verdict.

This mirrors `direct_ingest.py`, which separates the same empty-plan verdict from real
errors (a loose single archive filed with no library media); the torrent path was the one
that still treated it as a failure. For direct-ingest VIDEO and directories the verdict is
stricter, not looser — nothing is deleted unless the library proves the content is already
present (§ Direct ingest). The empty-plan guard in `validate_plan` is untouched — it still
rejects empty plans — so the cross-repo plan contract (§ YouTube ingest) is unchanged.

### Duplicate variants collapse, not crash

The failure just above is about a repeat *across* torrents (a redownload of
something already in the library — a clean skip). A different repeat lives *inside a
single torrent*: a complete-series pack that ships the **same episode more than
once**. The canonical case is the `[Anime Time] One Piece (0001-1071+…)` pack, whose
episodes 1-206 appear **three times** — once in the real arc/season folders (the
BD/CR dual-audio 1080p line) and again in `Episode 001-206 Uncropped (480p)` and
`Episode 001-206 Uncropped (1080p Upscale)` (a colour-distorted upscale). The
filenames are byte-identical, so all three copies resolve to the **same**
`dst_rel` — e.g. `Shows/One Piece (1999)/Season 01/One Piece (1999) - S01E0001.mkv`.

There is no "keep the best of several in-torrent copies" answer the already-present
skip can give here, because *none* of the copies is on disk yet — they collide with
each other, in one plan, before apply ever runs. Historically that tripped
`validate_plan`'s duplicate-destination guard and **failed the entire torrent** (all
~1,300 files, not just the 206 duplicates). Two layers now fix it:

- **The prompt** tells the identify run to keep exactly one copy per episode — the
  highest-quality **main line**, never an `Uncropped`/`Upscale`/lower-resolution
  (480p/720p) alternate — and drop the rest, so the duplicates ideally never reach a
  plan.
- **The harness guarantees it** regardless of what the run emits. Before the
  per-file checks, `validate_plan` groups the planned files by resolved destination;
  for any destination with more than one source it **keeps a single survivor and
  drops the others** — the copy whose path carries **no** de-prioritization marker
  (`config.DUPLICATE_DEPRIORITIZE_MARKERS`: `uncropped`, `upscale`, `480p`, `360p`,
  `720p`), tie-broken by **largest file** (the higher-bitrate cut). So the untagged,
  larger BD/CR copy wins over both the tagged 480p and the tagged upscale. Dropped
  copies are left out of the plan entirely (recorded on the plan as
  `_deduped_dropped` for the audit trail, logged per file) and deleted with the
  local download. The duplicate-destination guard remains as the **post-dedup
  invariant assertion** — after the collapse no two survivors share a destination, so
  it never fires in normal operation, and a hit would signal a real bug.

This is deliberately **not** the One Pace churn class (below): nothing on disk is
overwritten. It only decides which of several *in-torrent* copies of a not-yet-present
episode survives into the plan. Tune the marker list to steer that choice; it only
ever breaks ties between duplicates that would otherwise collide, and never drops a
file that is the sole source for its destination.

#### The One Pace exception: re-releases and extended cuts replace, they don't skip

There is exactly one directory where "already present ⇒ skip" is the *wrong*
answer: **`Shows/One Pace (2013)/`** (`config.ONE_PACE_PREFIX`). One Pace
periodically re-cuts and re-releases episodes it has already shipped — a repeat
drop there is a **better cut** (higher quality, a re-edit) or an **extended cut**
of an episode already on disk, not a redundant duplicate. So for any planned file
whose `dst_rel` starts with that prefix, an existing destination is **replaced in
place** rather than left untouched: the new file is staged in the dot-dir and then
`os.replace`d over the old one, which is atomic within the volume, so the episode
is **never absent for an instant** (gapless) and the applied entry is flagged
`replaced: True` and size-verified like any moved file. Every *other* path in the
library remains strictly write-once.

An **extended version replaces the non-extended one in the same episode slot** — it
is the same episode, a longer cut, so the identify step files it at the base
episode's `season`/`episode` (never as a new trailing episode number) and the
replace machinery overwrites the shorter cut. This is the same rule as a newer
re-cut, extended to the length axis.

This mirrors **Media-Syncer**, which treats the same `ONE_PACE_PREFIX` as its lone
"churn class" for exactly this reason — a newer/longer cut must overwrite the older
version, gaplessly (see that repo's README, *One Pace as the lone churn class*).
Keep the two prefix strings in step: they name the same folder on the same drive.

#### Anime quality upgrades: a strictly-better copy replaces in place, a trade never does

Outside One Pace the library is write-once — with one new, deliberately narrow
exception: an **anime** file that is provably *better* than the one already on disk.
The identify plan carries an `anime` flag (set by the run: `true` for Japanese
animation, `false`/omitted for western TV/movies). When a planned file's destination
already exists **and** the plan is flagged anime, `apply_plan` probes **both** files
with `ffprobe` (`library._probe_quality` — resolution from the video stream's height,
dual-audio from the audio streams' tagged languages) and hands the result to
`library._upgrade_verdict`, a three-way decision:

- **`replace`** — the new file is a strict Pareto gain (higher resolution **or** dual
  audio, losing neither axis), *or* the existing file cannot be read at all (a corrupt
  file, which any readable new file beats).
- **`skip`** — the new file is provably equal-or-worse: a **higher-resolution but
  single-audio** copy over an existing **dual-audio** copy (would lose the dub), a
  **dual-audio but lower-resolution** copy over an existing **higher-res** copy (would
  lose definition), or a same-quality repeat.
- **`bad_source`** — the **new** file cannot be read (a corrupt/truncated download). It
  must not clobber a good file, so `apply_plan` raises and the torrent **FAILS** with a
  "re-search for a valid release" message rather than silently dropping the bad download
  as "already present".

The replacement itself reuses the One Pace machinery — stage in the dot-dir, then a
gapless atomic `os.replace`, flagged `replaced: True` and size-verified like any move —
and, for the pool, it is **queued, not left to drift**: `_queue_replacement` appends the
replaced path to Media-Syncer's `replacements.jsonl`, so Media-Syncer overwrites the
stale MEGA copy in place and empties that remote's rubbish bin (§ Media-Syncer README).
If ffprobe cannot read the *new* file the torrent fails; if it cannot read the *old* file
the new one replaces it. Untagged audio is read as single-audio (conservative), so a
provably-dual new file still replaces an untagged old one, while an untagged new file
never replaces a confirmed-dual one.

This is what makes the searcher's new "take anything, upgrade later" stance safe: a
sub-only or SD copy of *new* content is grabbed immediately, and the dual-audio /
higher-definition copy replaces it the moment it appears — but the fleet never trades
one quality axis for another, and never replaces a good file with an unreadable one. The
same Pareto rule is enforced on the searcher side (`searcher.is_upgrade()`) so a trade is
not even re-queued.

#### One Pace metadata is the AI's job, keyed by arc — never by anime episode number

One Pace is a **fan recut** of One Piece, re-edited arc by arc. No metadata provider
carries its arc-based episode list, so Jellyfin can never scrape it: **One Pace is
always an OWNED show**, and this pipeline (the AI run) authors its per-episode titles
and plots the same way it handles everything else — no external metadata repo. The
rules the identify and repair steps follow (`prompts/identify.md`, § *One Pace*):

- **Arc = season.** The show's `tvshow.nfo` `<namedseason>` list is ground truth
  (Romance Dawn = 1 … Wano = 35, Egghead = 36, `Specials` = Season 00); each episode
  is filed into the season for its arc and continues that season's numbering.
- **Titles come from the mkv container.** One Pace embeds `"<Arc> NN - <Title>"` in
  the file's container `title` tag (e.g. `Wano 60 - Conqueror's Haki`); that tag is
  authoritative, with the release filename and the One Pace guide as fallbacks.
- **Plots come from the adapted manga chapters — never from a global "absolute One
  Piece anime episode N."** A One Pace episode covers a manga chapter range; its
  plot describes *those chapters*. Mapping the recut onto a global anime episode
  number pulls a wrong-arc synopsis — that is precisely how a Wano episode once
  ended up with an Impel Down plot (stale metadata from an earlier tool, since
  purged). The repair tool has a dedicated One Pace path (below) that forbids this.

---

## Concurrency and the disk budget

`ACTIVE = {DOWNLOADING, DOWNLOADED, IDENTIFIED, STAGED, VERIFIED}`. Each cycle the
daemon advances **every** active torrent one step (oldest `created_at` first), then
calls `admit_downloads()` to start as many `QUEUED` torrents as still fit the local
disk budget. Downloads run **in parallel** — qBittorrent fetches them concurrently
(bounded further by its own `MaxActiveDownloads` queue setting), and each finishes
and is processed through identify → stage → verify → cleanup independently of the
others.

Why parallel and not one-at-a-time: with strict serialization a single slow or
stalled torrent (few seeders, throttled tracker) blocks the entire queue behind it.
Running everything that fits means a stalled download only holds its own slice of
the budget while the others download and ingest around it.

The safety property that replaces serialization is the **budget**, not a count:

- `_remaining_budget()` starts from current free space minus the `MIN_FREE_BYTES`
  floor, then subtracts, for every in-flight download, the bytes it has **still to
  fetch** (`total_size - completed`, read live from qBittorrent). What's left is
  admittable. Because each active download's remaining bytes are reserved up front,
  the disk cannot fill even if all of them run to completion.
- `admit_downloads()` walks `QUEUED` oldest-first and starts each torrent whose
  `need` (size × `SPACE_SAFETY_FACTOR`) fits the remaining budget, decrementing the
  budget as it admits. It is a **greedy fill**: a torrent too big for the current
  budget is skipped and *smaller* ones behind it can still start, so one giant
  torrent never starves the rest.
- `FAILED` and `QUEUED` are **not** in `ACTIVE`, so a failed torrent never consumes
  budget, and a torrent that doesn't fit right now stays `QUEUED` and is retried
  every cycle — starting automatically the moment completed ingests free space.
- This still lets a small working set ingest an unbounded library: fifty dropped
  `.torrent` files admit in waves as fast as the disk drains, not one at a time.

---

## Disk-space policy

> **Torrents download onto the Mac SSD, never the SSD library root.** `INCOMING_DIR` is
> `~/Downloads/.torrent-ingest` on the SSD. A torrent's heavy random I/O on the the SSD library root
> USB drive starves the directory reads mediafs serves to Jellyfin/YacReader and
> **wedges the library mount**: the FUSE mount throws `Device not configured` and Jellyfin
> serves an *empty* library to Infuse for as long as it lasts (the DB stays intact the whole
> time; only the mount is wedged). So
> torrents land on the fast SSD and `apply_plan` **copies** the finished files
> cross-device onto the SSD via the staging dir (`_copy_verified` falls back from a
> same-volume hardlink to a real `shutil.copy2` on `EXDEV`). Admission is a single **SSD
> budget** (`_remaining_budget` = SSD free − `MIN_FREE_BYTES` − in-flight remaining). A
> torrent too large to fit the SSD even empty is **REFUSED outright** — never spilled
> onto the SSD (§ Torrents too large for the SSD). (This reverts an earlier
> "download in place on the SSD for an instant hardlink" optimization; the I/O
> contention it caused wasn't worth the saved copy. `LIBRARY_INCOMING_DIR` /
> `LIBRARY_MIN_FREE_BYTES` are now vestigial.)

A torrent's `total_size` is read directly from the `.torrent` file at registration
(`qbt.total_size_from_file`), so the fit check needs no network transfer; adding it
to qBittorrent paused to read the size is only a fallback when the file can't be
parsed. **BEP 47 padding files are excluded from that sum** — they are piece-alignment
pads listed in the metadata that qBittorrent never writes to disk (detected by the
`attr` flag `p` or a `.pad/` path component). This matters twice: the budget doesn't
reserve space for bytes that never land, and — critically — the on-disk completeness
check (the `DOWNLOADING` resume row, § The pipeline) compares against a `total_size`
that now equals what actually hits the disk, so a fully-downloaded hybrid torrent is
not mistaken for a short one. The
admission math (§ Concurrency and the disk budget) then runs against the
`~/Downloads` volume:

- `need = total_size * SPACE_SAFETY_FACTOR` (1.15 — covers piece overhead and the
  transient window during the copy-to-the SSD library root).
- A torrent starts only if `need <= remaining budget`, where the budget already
  reserves what every in-flight download still has to fetch and holds the 20 GB
  `MIN_FREE_BYTES` floor free for the OS and everything else.
- If `need + MIN_FREE_BYTES` exceeds the SSD's **total** capacity, the torrent can
  never fit locally even on an empty disk, so it is downloaded in **chunked waves**
  instead (§ Torrents download in space-bounded waves) — never spilled outside the SSD,
  because a torrent writing to the library drive wedges the mount (§ Torrents too large
  for the SSD). With chunking disabled it is **REFUSED** (`FAILED`, `"too large to fit on
  the local SSD"`).
- Otherwise, if it merely does not fit **right now** (alongside everything already
  running), it stays `QUEUED` — never added to qBittorrent at all — and is retried
  every cycle, starting as soon as active ingests free enough space. **That wait is
  bounded, and must be:** "starting as soon as space frees" assumes something frees it,
  and this repo is not the only writer on that SSD. It emits a throttled `DEFERRED` line
  rather than waiting silently, and after `CHUNK_AFTER_DEFERRED_SEC` (2 h) it stops
  waiting and switches to chunked waves — see § *A torrent that doesn't fit right now
  waits*.

Downloads are written to `~/Downloads/.torrent-ingest/` — a dedicated,
dot-prefixed subdir. Isolating our downloads there means cleanup (`delete_files`)
never touches unrelated files the user keeps in `~/Downloads`.

### Torrents too large for the SSD: chunked or refused, never spilled outside the SSD

The disk budget above is about fitting *many* torrents onto the SSD at once. A
torrent that cannot fit the SSD **on its own, even if it were completely empty** —
a 400 GB+ complete-series pack against a 460 GB Mac Mini disk — has `need +
MIN_FREE_BYTES` exceeding the volume's *total* capacity, so no amount of waiting
for other ingests to finish will ever make room. Such a torrent is downloaded in
**chunked waves** (§ Torrents download in space-bounded waves), or, with
`CHUNKED_TORRENTS_ENABLED` off, **refused** (`FAILED`, `"too large to fit on the
local SSD"`).

Either way it is deliberately **not** spilled outside the SSD. An earlier design did exactly that
— a dedicated `LIBRARY_INCOMING_DIR` on the library drive, admitted against its own
the SSD library root budget — to avoid failing giant packs. But downloading onto the SSD is the
very thing that wedges the library mount: the torrent's heavy random I/O starves
the directory reads mediafs serves to Jellyfin/YacReader, the FUSE mount wedges
(`Device not configured`, uninterruptible-sleep hangs), and Jellyfin serves an empty
library to Infuse until it clears. The rule is now absolute — **nothing the torrent client
writes ever lands on the SSD.** The only path onto the library drive is
`apply_plan`'s controlled cross-device copy of a *finished*, verified file. If you
genuinely need a pack bigger than the SSD, fetch it some other way (a temporary
scratch disk) rather than through the ingest daemon.

### A torrent that doesn't fit *right now* waits — but only for two hours

Distinct from the refusal above. A torrent smaller than the SSD's total capacity but too big for
its *current* free space stays `QUEUED` and is retried next cycle, on the theory that it "starts as
the drive drains." **That theory has no owner.** Nothing in this repo drains the SSD;
Media-Syncer's `predownload.py` is the space manager for that disk, and its job is to *fill* it
with predicted content. When its floor settles below `MIN_FREE_BYTES + size × SPACE_SAFETY_FACTOR`,
whole-torrent admission never happens.

So waiting is bounded. A torrent that has been `QUEUED` for `config.CHUNK_AFTER_DEFERRED_SEC`
(2 h) without ever fitting stops waiting and **switches to chunked waves** (§ *Torrents download in
space-bounded waves*), which need only one wave's worth of headroom rather than the whole pack's.
The wait is measured from the record's `created_at` — a record that is still `QUEUED` has by
definition never been admitted, so registration time *is* the start of the wait, and the deadline
survives a daemon restart without a stamp of its own.

This is the branch that covers the **gap between the two size thresholds**, and that gap is where
drops go to die if nothing closes it: the refusal/chunk trigger above fires only for a torrent
bigger than the SSD's *total* capacity (460 GB), while admission needs it to fit the SSD's *free*
space (~70 GB admittable). A pack between those two numbers — an 85 GB BDRip season set — matches
neither branch and is re-deferred every cycle for as long as the disk stays full. The deadline is
what turns that into progress.

Until the deadline it emits a throttled (hourly, per torrent) line — **grep `DEFERRED` in
`torrent_ingest.log` first whenever a drop doesn't show up**:

```
DEFERRED <name> (26GB needed incl. safety factor, 9GB admittable, 31GB free on the SSD, 20GB floor):
waiting for space. It will start when the SSD drains -- if it never does, something else is holding
the disk.
```

**Fingerprint of a stuck drop:** the `.torrent` is still in the iCloud watch folder (not `finished/`,
not `failed/`), its journal record reads `"status": "queued"` with a `total_size` larger than the
SSD's headroom, and the show is absent from the library. Past two hours that state is itself a bug —
the record should read `"chunked": true` — so check `CHUNKED_TORRENTS_ENABLED` before anything else.

The two-hour wait applies only to a torrent whose fit has never been decided. A record carrying
`"chunk_intent": true` — set when a chunked torrent is re-dropped — skips the deferral entirely and
chunks on the next cycle (§ *A re-dropped chunked torrent resumes*).

**Fingerprint of a re-drop that silently did nothing:** `Re-queuing a finished torrent dropped
again: … resuming chunked waves at N file(s) already filed` followed within a minute by `COMPLETED
chunked …`, with no `chunked wave ingested` line between them, and the title absent from the mount.
That is a pack retiring on inherited progress whose content is gone (§ *What is carried is what can
be PROVEN*); it now FAILS instead, but the log signature is how the four lost titles were found.

Media-Syncer holds up the other half of the space contract: its pre-download cache honours an
absolute `SSD_MIN_FREE_BYTES` floor (80 GiB — this repo's 20 GiB `MIN_FREE_BYTES` plus 60 GiB of
torrent staging room) rather than a ratio that slides down with the disk. See Media-Syncer's README,
*Failure mode: the pre-download cache starves the torrent downloader*. **If you change
`MIN_FREE_BYTES` or `SPACE_SAFETY_FACTOR` here, re-check that floor** — the two numbers are a
contract between the repos, and nothing enforces it automatically.

### Tailscale dying idles this daemon indefinitely

`tailscale_up()` gates all downloading on a `100.64.0.0/10` CGNAT address being bound, because
qBittorrent is bound to that address — so when Tailscale dies the daemon correctly refuses to
download rather than leak, logging `Tailscale down; not starting/continuing downloads. Idling.`
every 61 s. But it has **no way to recover**: the Tailscale Mac app's network extension is not
launchd-supervised, so nothing restarts it. Media-Syncer now runs `com.mikeyferguson.tailscalewatchdog`
(`scripts/tailscale_watchdog.py`) to relaunch it automatically; see its README. **If you see a wall
of `Tailscale down` lines, check that watchdog is loaded** (`launchctl list | grep tailscalewatchdog`)
before touching anything here.

---

## Identification — the AI step

This is the part that does the judgment `guessit`/TMDB structurally cannot.
`identify.py` builds a prompt from `prompts/identify.md` plus runtime context (the
downloaded file listing and a digest of the existing library), then invokes:

```text
ai_runner.py -p --output-format json \
    --tools Read,Write,Glob,Grep,Probe,ListDir,WebSearch,WebFetch --max-turns 60
```

The prompt is fed on stdin; the run inspects files (`ListDir`, `Probe` for a runtime,
`Read` on existing `.nfo`) and does web lookups (TMDB, and the AniDB/TheTVDB anime
mapping lists), then **writes a JSON plan to a known path** —
`state/tmp/<hash>_plan.json`. The engine reads that file rather than parsing stdout,
which is far more robust. The run's final message is captured as a rationale.

### Reusing the searcher's settled file→item map (§ issues.txt 6.4)

The searcher already classifies every torrent it drops (which episodes/volumes/chapters/
movies the `.torrent` provides), and it persists that per-file map to the shared
`library.db` `torrent_plan` table keyed by infohash. Before spawning the identify run,
`identify.run_identify` loads any stored map for the infohash and injects it into the prompt
as *settled numbering* — so a re-download of the same infohash reuses the searcher's
classification instead of re-deriving it, and the run only fills in the placement details
(destinations, episode titles, plots, TMDB ids) rather than re-arguing the absolute-vs-
seasoned numbering. This is what makes "the AI does not re-derive it at ingest time" true:
the load-bearing numbering decision is made once, in the searcher, and carried forward.

### The deterministic fast-path (§ diagnosis 6.4)

For the *common* case the stored map is enough to place the torrent **without any model
call at all**. `fastpath.build_plan` (called by `identify.fast_path_plan` from
`_advance_identify`) derives the plan directly and skips the identify run, for:

* a show/volume/chapter whose file list is fully settled by the stored map,
* **not** an owned show, **not** a movie, **not** a Season-0 special.

It is gated so a fast-path hit is never riskier than the plan the harness validates
identically, and a miss is free (the AI still runs):

* **(a) folder resolution** — the stored plan's series name must resolve **unambiguously**
  to an existing library folder under the same normalization the searcher uses
  (`library.resolve_*_folder`: strip a trailing `(YYYY)`, fold punctuation/accent). A
  brand-new show (no folder), a Japanese/English name mismatch, or two folders sharing a
  key all fall back to the AI.
* **(b) numbering confirmation** — for every episode file, the release filename must carry
  a clean `SxxEyy` that **equals** the stored plan's `(season, number)` (so the release
  numbering *is* the library numbering), or a **loose seasoned** number — `S2 - 08`,
  `Part 3 - 12`, `Season 1 - 04`, `Episode 114`, or a lone `01` — whose episode equals
  the plan's number and whose season (from the filename, else from the plan) **equals the
  plan's season AND is already on disk**. The "already on disk" guard refuses the
  "Part 5 → Season 3" mis-number a bare-numbered map can infer (a season that is not on
  disk is not trusted), leaving `_reject_season_gap` as the deeper backstop. Either way
  the release filename must share an identifying token with the resolved folder (so it
  really is this series, not a sibling like "BanG Dream! Ave Mujica" filed under "BanG
  Dream!"). Volumes/chapters need only the token check — their numbers are deterministic
  (`vNN`/`cNNNN`).

The `(b)` gate is **stricter** than the diagnosis's original draft ("the library has
titles ⇒ the plan matched by title"). Re-running the diagnosis's Test B against the
journal found the searcher's `item_map` is sometimes wrong about show numbering **even
when the library has titles** — a bare-numbered release ("… - 01", "Episode 114") is
mapped by *filename inference*, not a title match, and a release of a different series in
the same family is matched against the wrong episode list. Trusting the map on titles
alone would reproduce those mis-placements. The clean `SxxEyy` gate eliminates the
wrong-series cases, and the widened bare-numbered seasoned gate (§ diagnosis 6.3.1)
recovers most of the show-identify savings while keeping the absolute-show, owned-show,
season-gap and token-overlap exclusions — so a bare "01" only ever fast-paths into a
season that already exists on disk.
The fast-path plan is still run through `library.validate_plan`, and any rejection falls
back to the AI rather than failing the torrent. `config.FAST_PATH_ENABLED`
(`TORRENT_INGEST_FAST_PATH=0`) turns it off without a redeploy.

### The tight "settled" run (comics/novels only; § diagnosis 6.3.3)

When a stored plan is present but the fast-path cannot fire, the AI identify may still run
with a **scoped library digest** (only the series the plan names, § diagnosis 6.3.2) and,
for **comics/novels only**, with web tools dropped and `--max-turns` capped at
`config.IDENTIFY_SETTLED_MAX_TURNS` (4). Comic/novel volume-and-chapter numbering is
deterministic, so the stored map genuinely settles placement and the run has nothing to
look up. Shows keep the full web-enabled run when they are not fast-pathed, because their
numbering judgment (absolute vs seasoned, wrong-series detection) is exactly what the web
lookups exist to resolve.

### The runtime

`ai_runner.py` is the fleet's own agent. It talks to a chat endpoint with function
calling; the judgment work here needs an agent — something that can look at the actual
files, look a fact up, and write a plan — so `ai_client.py` supplies the loop and the
tools and `ai_runner.py` wraps it in a CLI the daemons spawn.

### The free-provider fallback chain (§ diagnosis 6.5)

The identify step no longer runs ONE paid model. `identify.run_identify` walks a **chain
of free providers** (`config.AI_PROVIDERS`, keyed by `~/.config/api-keys/<name>_key`);
DeepSeek is deliberately **not** in the chain — the fleet's identify is free-only. A
provider is enabled simply by having its key file present, so dropping a key auto-adds it
as a fallback. The chain is ordered free-first; each provider lists its models in
preference order.

When a provider writes a plan the harness rejects, the **rejection reason + that plan are
handed to the next provider** as "fix exactly this" context (`_failure_context_block`), so
each successive free model corrects the last one's specific mistake instead of re-deriving
from scratch. A provider whose API cannot run (no key, quota, rate-limit) is skipped for
the next. The run succeeds as soon as one provider's plan passes `library.validate_plan`;
it raises `IdentifyUnavailable` only when *no* provider could run at all (defer), and
`RuntimeError` when providers ran but every plan was rejected.

The two tiers of failure were already there and are preserved: an **empty `files` plan is
a legitimate "nothing to place" verdict** (repeat/extras), returned as-is rather than
treated as a fixable mistake; a **validation rejection** is a real mistake, and it is what
escalates to the next provider. `TORRENT_INGEST_AI_PROVIDERS` overrides the whole chain
for testing/pinning (comma-separated `name:model`).

**`deepseek-v4-pro` (the reasoner) and its chain-of-thought bill** is the historical
failure mode this change retires. `deepseek-v4-pro` emits a `reasoning_content` field
alongside its answer, and that thinking counts toward the same `MAX_OUTPUT_TOKENS` ceiling
as the tool call it is reasoning toward. With a small ceiling a *large* identify prompt (a
61-file mixed pack like the Steins;Gate complete-series torrent — two shows, a movie, OVAs
and specials) spends the whole budget on reasoning and stops with `finish_reason: length`
before it ever emits the `Write` tool call: the run exits 0 with no plan, and the torrent
fails as "no plan file". `MAX_OUTPUT_TOKENS` is therefore sized for the *reasoning*, not
the reply — the reply is a short tool call, but the thinking ahead of it is not. This is
the failure mode that kept the Steins;Gate pack un-ingestable through days of retries, and
it is silent: the model is not "wrong", it is starved. The free providers are non-reasoning
flash models, so they do not carry this billing trap — the cost was the reason for the
switch.

### The three ways a free run produces nothing, and what is done about each (2026-09-10)

A run that returns a WRONG plan is cheap: `validate_plan` rejects it, the rejection is fed
to the next provider as "fix this", and the chain converges. A run that returns NOTHING is
expensive — rc 0, no plan file, no wrong answer to learn from — so the caller can only log
"transient, retrying" and walk into the same wall. The fleet carried exactly that for days
as *"6–10 minute runs ending in empty text, unexplained"*.

The runtime's own turn log (`state/tmp/<hash>_identify.log`, one line per tool call) had
the answer. Running `[MTBB] Monogatari Series (BD 1080p)` deliberately produced all three:

**1. The model loops on an identical tool call.**

    turn 13: Grep -> 375 chars      turn 22: Grep -> 375 chars
    turn 14: Grep -> 375 chars      turn 23: Grep -> 375 chars
    ... six identical Greps in a row, then more, then ListDir twice ...

An identical call returns an identical result, and a model cannot tell it is repeating
itself. From the third identical call (same tool, same arguments) the result now says so:
*you have made this call N times, it will not change, nothing is modifying it between your
calls, use what you have.* The tool result is the only channel that reaches the model
mid-run. Two identical calls are a legitimate re-read and pass through untouched.

**2. It runs out of turns mid-investigation.** A model does not track its own turn count.
At 55% of the budget it is told to converge; at 80% it is told to stop investigating and
write the file, because *an incomplete answer that is written is worth infinitely more than
a perfect one that never gets written.* This is not a limit change — it is telling the model
what its limit already was.

**3. It decides it is finished and answers in prose.** This is the one that actually bit
Monogatari, and it never came close to the turn ceiling — the model simply believed it was
done. `run_agent` now takes `require_file`: when the run stops and that file does not
exist, the model is told once more, plainly, that the caller reads that file and nothing
else, so nothing it has said is usable, and to write it now from what it already knows.
Bounded at two asks so a model that cannot produce the file never spins forever, and inert
when the caller names no file. It costs one turn.

All three live in the shared runtime, so every AI caller gets them — identify,
`media_doctor`'s escalation and the metadata repair. Guard:
`scripts/test_agent_loop_guard.py`, both directions.

### What the harness computes so the model does not have to (2026-09-10)

Monogatari was filed with one 26-episode arc spread over six season folders as absolute
episodes 1–23. The cause was not the model's reasoning: the prompt was handing it the
retired searcher's stored file→item mapping under the words *"reuse this mapping by
filename; do NOT re-derive the season/episode numbering"*, and that stored mapping contains
exactly the broken layout. The most authoritative-sounding line in a 90,000-character prompt
was the wrong one.

Four things changed, and the ordering matters — each is cheaper and more reliable than the
one after it:

* **A stored mapping is evidence, not an instruction.** Nothing has produced or re-checked
  these since the searcher was removed. One whose own numbering chains across seasons is
  withheld entirely, with the run told why.
* **The release's structure is computed, not inferred.** Which folders share a filename
  label, and whether their episode numbers form one run across them, is arithmetic. For
  Monogatari that is five folders carrying `Monogatari Series Second Season` as one run of
  01–23, stated as fact with the two legal resolutions spelled out.
* **A chunked wave is shown the WHOLE release.** Only a wave's files exist on disk, so the
  structure analysis used to see a fifth of the pack — and Monogatari's first wave hides
  every folder whose numbering conflicts.
* **The library digest is scoped by relevance.** ~32,000 characters of ~300 show folders
  went out on every call. Now: full detail for the shows that could be this one, every other
  folder still listed by name. It fails open — a hint matching nothing returns the full
  digest — so a bad guess costs tokens, never a placement.

### A provider's prompt ceiling is measured, not guessed (2026-09-05)

Two DIFFERENT things stop a provider serving an identify prompt, and conflating them cost
the fleet a day of unfiled downloads.

* **Out of daily budget.** OpenRouter's ~1000 free requests/day, Cloudflare's 10,000
  neurons/day. Transient — it clears at the provider's reset (00:00 UTC for OpenRouter).
  Recorded PER PROVIDER in `state/ai_budget_capped/<name>`; auxiliary AI work stands down
  only when **every** keyed provider is capped (`config.ai_budget_healthy`). It used to be
  one global stamp, so the first provider to cap stood the whole fleet down while the
  others answered normally.
* **Prompt too large.** A tokens-per-minute ceiling. **Permanent at this prompt size** —
  waiting does nothing. Groq's free tier enforces **8,000 TPM** against an identify prompt
  of ~**28,000 tokens**, so Groq can never run identify, while answering a small `cull` or
  `media_doctor` call in five seconds. *"Groq is up" and "Groq can run identify" are not
  the same claim.*
* **The model slug died** (added 2026-09-07). A provider retires a `:free` slug and every
  call to it answers `http 404 "This model is unavailable for free. The paid version is
  available now — use this slug instead: <paid slug>"`. Permanent, fixed only by editing
  `AI_PROVIDERS`, and **never** by taking the paid slug the error offers (§4.3). Two of
  OpenRouter's five models had been dead this way for days: the chain paid a timeout for
  each on every pass, and because the pass ended with a genuinely capped Cloudflare, its
  summary line read *"every free provider is out of budget"* — which was then read as a
  measurement and became this project's standing belief that identify was budget-capped
  everywhere. It was not. Three OpenRouter models answered a live probe throughout.

  Two guards came out of that. `identify` now names the providers that actually said
  "no budget" and says plainly that the others failed for other reasons, instead of
  asserting a fleet-wide fact from one provider's answer. And `identify_capacity.py
  --probe` probes **every** model rather than stopping at the first that answers — a dead
  model inside a live provider is invisible in a provider-level line, which is why this
  one was only ever findable by reading the daemon log by hand.

The second used to be handled by skipping the provider for 30 minutes, justified by "the
prompt is the same shape for every record". **It is not** — prompt size is dominated by
the library digest and the file listing, which differ by an order of magnitude between a
one-file comic and a 522-file season pack. So one oversized pack banned the only provider
with budget left from every small record too. On 2026-09-05 that skipped Groq 18 times in
a day at ~32 s a refusal, while fourteen Made in Abyss volumes sat downloaded and unfiled.

What replaces it: the refusal states its own boundary (`Limit 8000, Requested 30399`), so
`identify._note_too_large` converts it into a **ceiling in characters** measured against
the prompt we actually sent, and `config.save_prompt_ceiling` persists it (7-day TTL, so a
tier upgrade is still noticed). A prompt under the ceiling is tried; one over it is skipped
**without spending a request or a timeout**, and a smaller prompt re-tests the provider on
its own. Before writing a provider off, the run offers it a **narrowed library digest** —
`library.build_library_digest(sections=…)`, keeping only the sections the download's own
file extensions prove it could use, which takes a comic pack's prompt from 98.8K to 72.1K
characters and never withholds anything the run needed.

And when *no* provider can run, `identify` sets a fleet-wide backoff
(`IDENTIFY_UNAVAILABLE_BACKOFF_SEC`, 15 min) instead of re-proving it once per record per
27-second cycle. `direct_ingest` always had that sleep; the torrent daemon did not.

**`scripts/identify_capacity.py` answers "can identify run right now?" in one command** —
prompt sizes, each provider's cap stamp and measured ceiling, and a verdict. `--probe`
spends one tiny live request per provider to ask the provider itself rather than trusting
a stamp (§ diagnosis 4.178: the measurement costs one command). It runs as an advisory in
`verify_fleet.sh`, not a blocking check: an exhausted daily cap is a runtime condition, not
a code fault — but a ceiling below the smallest prompt we can build never clears on its
own and has no other symptom.

**The runs are subprocesses, not in-process calls.** A run lasts up to ninety minutes;
in-process, a wedged one would take the ingest daemon down with it, and
`subprocess.run(timeout=...)` — the only kill-backed time bound available — would not
apply. Out of process the worst a bad run can do is exit non-zero. It exits **2** for
the one failure class that must never be blamed on content (no credential, no balance),
which is what routes that case to `IdentifyUnavailable` and a deferral.

**`config.AI_BIN` is a list, and its first element is `sys.executable`.** The three
repos run under three different conda envs and launchd puts none of them on `PATH`, so
a runner invoked through a `#!/usr/bin/env python3` shebang would resolve to whichever
interpreter that minimal `PATH` found — typically one without this repo's dependencies.
Spawning the runner with the **caller's own interpreter** makes "the daemon can import
it" and "the runner can import it" the same question, permanently. Call sites splat it:
`[*config.AI_BIN, "-p", ...]`. `contract.py` asserts the shape, because a bare string
here would splat into single characters and fail hours later with an unreadable error.

**`config.ai_env()` supplies `PATH`** so the `Probe` tool finds `ffprobe` under launchd.
The credential is deliberately *not* passed through it: `ai_client.api_key()` reads
`~/.config/api-keys/deepseek_key` off disk, so the key never sits in a process
environment where `ps -E` or a crash dump would show it. `ytconfig` re-exports both so
the YouTube ingest cannot drift.

### There is no shell tool, and that is load-bearing

The agent gets `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Probe` (ffprobe), `ListDir`,
`Jellyfin` (authenticated API call), `WebSearch` and `WebFetch`. It does **not** get a
shell, and nothing can be added that grants one without re-opening the hole below.

The first version of this runtime did ship a `Bash` tool guarded by a blocklist that
refused `rm`/`mv`/`unlink` aimed at the library. A test run was told to delete a real
episode and to route around any refusal. It hit the guard on `rm`, reported the refusal,
and then deleted the file with **`find -delete`**, which the blocklist never matched. A
447 MB episode went off the SSD and off the mount, and was recoverable only because the
pool copy happened not to have been purged yet.

Enumerating the ways a shell can destroy a file is not a solvable problem —
`python3 -c "os.remove(...)"`, `perl -e unlink`, `install /dev/null f`, a bare `> f` —
and every miss is content. So deletion is not filtered, it is **inexpressible**: the
model is handed the three capabilities the prompts actually needed a shell for (probe a
file, list a directory, call Jellyfin) as narrow tools with fixed argument vectors and
no shell interpretation anywhere.

`Write`/`Edit` are the one remaining way to damage media — truncate an `.mkv` by writing
to its path — so they refuse a destination that is a media file under the library root
or the mediafs mount. That check reads the **resolved** path, so `..` traversal, a
symlink and an absolute path all normalise to the same answer. Sidecars stay writable:
`.nfo` and artwork are what these runs exist to fix, and are regenerable. Media is not.

**A tool call can never crash the run.** Every tool returns an `ERROR: ...` string for
the failures it anticipates; a safety net in the agent loop (`ai_client.run_agent`)
catches whatever a tool *raises* instead of returns and turns it into the same kind of
recoverable tool result, so a single bad tool call degrades to "the model sees an error
and tries something else" rather than aborting the whole run and failing the torrent.
Two concrete escapes this closes: `WebFetch` handing a hand-built URL with raw spaces or
unencoded CJK to urlopen (a Wikidata `wbsearchentities` call — `...&search=劇場版 STEINS;GATE
...` — raises `http.client.InvalidURL`, a `ValueError` the old `except OSError` missed),
and `Glob` passing urlopen an absolute pattern (`NotImplementedError`). Both are now
also handled at the tool: `WebFetch` percent-encodes anything urlopen would reject before
fetching (leaving well-formed URLs byte-for-byte unchanged), and `Glob` returns a clean
error for a non-relative pattern.

**Staying inside the time box on big/junk-laden packs.** A flat identify timeout was
a treadmill (900s failed on a Phineas & Ferb pack; 1800s failed on a ~110-file
complete-series Billy & Mandy pack that also bundled a large decoy folder of
unrelated shows). Two mechanisms keep a large pack finishing instead of timing out:
the timeout now **scales with the media-file count** (`IDENTIFY_TIMEOUT_BASE_SEC +
PER_MEDIA_FILE_SEC × count`, capped at `IDENTIFY_TIMEOUT_MAX_SEC`), and the file
listing handed to the run is **engineered to focus it** — media files first (with
sizes), non-media clutter after, each top-level entry annotated with its media
count so a "no media — ignore" decoy folder is skipped, and the whole listing capped
(`IDENTIFY_MAX_LISTING_FILES`). The prompt also tells the run to trust an
unambiguous in-filename `SxxExx` rather than `ffprobe` every episode.

**Mis-typed source filenames self-heal (`library._heal_missing_src`).** The run
occasionally retypes a `src` filename with one token wrong — most often an
audio-codec tag (`AAC2.0` where the real file is `DDP2.0`), because it reconstructs
the name instead of copying it. The path then points at a file that doesn't exist
and without healing the whole torrent fails the src-existence check.
`validate_plan` heals that specific, safe slip before failing: for a missing `src` it finds the one real file under the download
that is unmistakably the same file under a differently-typed name — **same episode
designator (`SxxExx[Eyy]`) exactly, same extension, inside the download, a
high name-similarity score that clearly beats any runner-up** — and substitutes it,
logging the heal. If there is no unambiguous match it still fails closed, so a
genuinely-missing file is never papered over. The prompt is also hardened to copy
`src` filenames byte-for-byte (never normalize codec/resolution/group tokens, which
routinely differ file-to-file within one pack), so the heal is a backstop, not the
first line of defense.

**The anchor is the episode designator *or* the volume marker.** Manga/comic files
carry no `SxxExx`, so a comic plan that retypes a volume name (a release token like
`(F)` hallucinated into the middle of the name — the Tokyo Revengers failure, where
the run copied the `(F)` off v01–v03 onto v04–v13) was unhealable: with no episode
anchor the heal demanded a stricter score over 31 near-identical `.cbz` volumes, saw
the runner-up within the margin, and failed the whole torrent. `_heal_missing_src`
now anchors on a manga `vNN` volume marker when no episode designator is present, so
a mis-typed volume heals to the *same* volume — a heal can still never cross volumes
or episodes. A video file's `v2` re-release marker never overrides its real `SxxExx`
(the episode anchor wins).

### The prime directive: the existing library is ground truth

The prompt's first instruction is to check whether the show already exists on
disk before consulting anything external. If it does, its layout is authoritative:

- **Numbering scheme.** If the existing show is one continuous `Season 01`, a
  torrent labelled `S03E13` is the next episode in that season (`S01E60`), not a
  new Season 03.
- **The season *number* is the library's, not the torrent's — read the per-season
  counts.** The digest now annotates every existing season with its episode count
  (`Season 01 (20 eps), Season 02 (20 eps)`), because that count is what
  distinguishes two numbering schemes a filename can't. The trap this closes:
  streaming shows (Netflix et al.) ship in **"Parts,"** and a release or TMDB
  routinely numbers each Part as its own season (Part 5 = `S05`), but **TheTVDB —
  which Jellyfin scrapes for these — bundles consecutive Parts into aired seasons**
  (Parts 1+2 = Season 1, Parts 3+4 = Season 2, Part 5 = Season 3). When the library's
  Seasons 01-02 hold ~20 episodes each (two Parts) and a ~10-episode "Part 5"/"Season
  5" drop arrives, its home is the **next aired season (Season 03)**, never Season 05.
  This is a real, shipped failure: **Disenchantment's** final 10 episodes were filed
  `Season 05` over a 20-per-season library, so TheTVDB (only 3 seasons) resolved
  nothing and every episode went blank. The run is now told to derive the number
  from the on-disk counts + the show's `tvshow.nfo` provider ids and to **never skip
  a season number**, and the harness enforces it: `library.validate_plan` rejects a
  plan that files an existing un-owned show into a season whose predecessor exists
  neither on disk nor in the plan (a `Season 05` over `01, 02` with no `03`/`04`) —
  fail-closed to `failed/` rather than a silent blank. The guard is scoped to
  *existing-folder, un-owned* plans, so a fresh single-season download or a
  deliberately-owned custom scheme never trips it. Without this the breakage is invisible
  because **nothing checked placement** — the plot-centric audit would have flagged
  the blanks only after 48h, by which point repair-by-lookup would have failed (there
  is no provider "Season 5") or written wrong-arc metadata; a misplacement is caught
  at plan time, not repair time.

  **A chunked pack is judged against the torrent, not just the library.** Waves are
  sized by disk headroom, not by season, so a multi-season pack files its seasons in
  whatever order they fit and the library is legitimately full of holes until the last
  wave lands — a `Season 04` wave over a library reading `[1, 6]` is mid-pack progress,
  not the Disenchantment mistake. So the chunked path passes `validate_plan` a
  `sibling_seasons` set: every season number the **torrent's own file paths** advertise
  (`SxxExx` designators and `Season NN` components, across all waves, filed or not),
  which counts as known alongside the on-disk and in-plan seasons. Protection survives
  because the evidence is the release's own numbering — a genuine "Part 5" drop names
  its files `S05Exx` and carries no `S04`, so the gap is still a gap and still fails.

  **Same-episode collision (write-once per episode, not per filename).** The
  single-residence invariant is per *episode*, not per filename — but the plan validator
  only ever skipped an EXACT destination path, so the same episode filed under two
  differently-named paths (a "title-with-title" `… - S02E05 - Unhappy Campers.mkv` vs a
  bare-number `… - S02E05.mkv`, or a `.mkv` vs an `.mp4` of one episode) used to land
  twice and accumulate. `validate_plan` now collapses a planned show episode that collides
  with an existing file carrying the same `SxxEyy` base number under a *different* name in
  the same `Season NN` folder — before any byte is written, exactly like the intra-torrent
  duplicate collapse. The existing copy is never touched; only the differently-named
  duplicate is dropped.
- **Filename format.** Match the exact `- SxxExx` padding/pattern already used.
- **Owned detection.** If the existing `.nfo` carry `<lockdata>true</lockdata>`,
  the show is OWNED — extend the hand-built scheme and set `"owned": true`.
- **Placement is inherited; the `owned` flag is not.** Matching an existing show
  means reusing its folder, season split, and filename format — but *not* copying
  a broken `owned: false` off disk. The `owned` flag is re-decided every drop by
  the resolve test (§ The hard calls). A long anime already sitting un-owned in one
  continuous absolute `Season 01`, whose later episodes already render blank, is a
  host that is *itself* the mistake (Gintama, Dragon Ball Z); extending it un-owned
  just breeds more blanks. The prompt now tells the run to skim a few of the host's
  later-episode `.nfo` — if they carry no `<plot>`, the scheme is failing and the
  new episodes must be filed `owned: true` with real titles/plots, while the
  nightly net backfills the pre-existing blanks.

Only when the show does **not** exist does the run establish a fresh layout from
TMDB + judgment. Whatever it establishes then becomes the anchor for future drops,
which is why the **first** drop of a new show is the highest-risk moment and later
drops are safe. Consistency within a show matters more than any single episode.

### The hard calls the prompt asks for

- **Absolute vs seasoned numbering** (anime absolute counts vs split seasons).
- **Movie vs special** — decided by one test: **does the provider carry it as a
  standalone film (its own TMDB `/movie/` entry)?** If yes it is a movie → `Movies/`
  with that film id pinned, *even when* it is deeply franchise-tied or bundled in a
  complete-series torrent (the My Hero Academia films, the One Piece films). Only
  something that exists solely as an entry in the show's **Season 0 special list** —
  a recap, a short OVA/ONA that ships as an episode, a non-theatrical episodic
  finale (an Attack on Titan finale, the Undead Unluck Winter Arc) — goes to
  `Season 00`. Filing a theatrical film as a `Season 00` special is a real past
  failure (the four MHA films landed as `S00Exx`, where Jellyfin then scraped them
  to the wrong Season-0 episode metadata); the prompt now forbids it explicitly.
- **Specials (`Season 00`) are always owned.** A file that genuinely belongs in
  `Season 00` (per the test above) is **never** left to Jellyfin's scraper: the
  identify plan must give every `Season 00` file a real `episode_title` **and**
  `plot`, `validate_plan` rejects any plan that doesn't, and `apply_plan` writes a
  **locked** episode `.nfo` for every special regardless of the plan's top-level
  `owned` flag. The reason is that a provider's Season-0 ordering almost never
  matches a release's own `S00Exx` numbering, so an un-owned special gets
  mis-scraped — wrong title/plot, or, worst of all, a *separate movie's* entry
  pulled onto it. This is a real, shipped failure: a Kim Possible pack's `S00E01–E04`
  specials were filed un-owned, and Jellyfin scraped the **"So the Drama" film we
  hold in `Movies/`** onto an *A Sitch in Time* special (and blanked the other
  parts). Owning specials pins only those `Season 00` episodes — the main series,
  if its numbering resolves, still stays un-owned and Jellyfin scrapes it normally.
- **Interleaved specials** — `airs_before_season`/`airs_before_episode` are written
  into the `.nfo` so a special slots into its correct watch position without a
  manual playlist.
- **Owned shows** — the cases where Jellyfin's scraper *can't* resolve the
  per-episode metadata for the numbering we filed under. The decision hinges on a
  single test: **will the exact `season`/`episode` coordinates resolve to a real
  episode at the provider (TMDB/TheTVDB)?** If yes, leave the show un-owned and
  let Jellyfin scrape. If no, the episode would render as a permanent blank
  "Episode N" — so `"owned": true` writes **locked episode `.nfo`** carrying the
  real title+plot and Jellyfin serves our layout instead of re-scraping. This is
  the required choice (not a rare exception) for long shows whose numbering can't
  resolve: absolute numbering continued in one `Season 01` past the provider's
  absolute-order coverage (newest Bleach arcs, Black Clover), a re-split into
  seasons that don't match the provider's boundaries (the Naruto/Shippuden trap),
  a multi-entry show like DBZ Kai's "Final Chapters", interleaved specials TMDB
  would misorder, or a Monogatari-class watch-order mess. Getting this call wrong
  — filing un-owned under numbering that doesn't resolve — is what silently blanks
  long shows; the audit/repair tools (§ Metadata integrity) catch and fix it.
  Two hard rules make owning safe:
  (1) **an owned plan must carry a real `episode_title` and `plot` for every
  locked episode** — the validator rejects the plan otherwise, because a locked
  episode with no metadata is one Jellyfin can *never* fill (it would show as a
  blank "Episode N" forever); and (2) **only episodes are locked, never the
  series** — the `tvshow.nfo` is written unlocked and seeded with the show's
  provider ids, so Jellyfin still scrapes a rich series page (plot/poster/cast)
  while being pinned to the right series. A standard-numbered show
  is left un-owned so Jellyfin scrapes everything itself; owning is not for shows
  that merely happen to bundle a movie or one ordinary special.

  The unlocked `tvshow.nfo` seed is written for **every** show, owned or not
  (`library._seed_tvshow_nfo`) — not just owned ones. This is what stops Jellyfin's
  scraper fuzzy-matching a **sequel/spin-off** folder onto its parent series and
  scraping the parent's episodes onto it: the classic failure was *Fairy Tail: 100
  Years Quest* landing un-owned with no seed, so Jellyfin title-matched it to the
  original *Fairy Tail* and duplicated the parent's first 25 episodes onto the
  sequel. The seed pins the plan's `tmdb_id` **and its `tvdb_id` when supplied** —
  the TVDB id matters because many anime libraries scrape TV via TheTVDB first,
  where the TMDB id alone does not stop the merge. The seed never **clobbers** an
  existing `tvshow.nfo`, so Jellyfin's own richer copy on a later drop is left
  alone — **but it now fills in a provider id that copy is missing.** An existing
  `tvshow.nfo` with no `<tmdbid>`/`<tvdbid>` is the same hole as no seed at all
  (Jellyfin's scraper is free to title-merge the sequel onto its parent), so a
  half-pinned file is topped up in place: an *absent* id is added, an id already
  present — even one that differs — is left untouched (a genuine id conflict is a
  placement bug, surfaced by the audit's merge detector below, not silently
  rewritten). This closes the case where a folder was seeded before its ids were
  known, or Jellyfin wrote an un-pinned `tvshow.nfo` first.
- **Comics/manga** — the easy case. A `comic` plan is title + designator number:
  `Comics/Manga/<Series>/<Series> vNN.cbz` (volume), `… cNNNN.cbz` (chapter),
  `Comics/Manga/<Series> Colored/<Series> Colored vNN.cbz` (colored volume), or
  `Comics/<Series>/...` (western). No seasons, no specials, no `.nfo`, no metadata —
  YACReader reads the folder and file names directly. The ground-truth rule still
  applies: match an existing series folder and continue its numbering. **Manga is
  shelved at three tiers — colored volume > black-and-white volume > chapter — and
  every tier is filed**, not just volumes: a loose chapter (a bare series name +
  number like `Sakamoto Days 217.cbz`, much smaller than a volume) is filed as
  `c0217.cbz`. Volume vs chapter is judged by both the name pattern and the size gap.
  **A higher tier supersedes the lower ones it covers**: the identify run lists the
  redundant files (chapters a new volume contains, or a B/W volume a colored volume
  covers) in the plan's `supersedes`, and `apply_plan` deletes each locally and
  queues it for remote purge (`mediafs_deletions.jsonl`, drained by the reaper) —
  § *Volumes supersede chapters; colored supersedes B/W*.
  **`.zip` → `.cbz`.** A `.cbz` is literally a ZIP of page images, so a comic that
  arrives as a plain `.zip` is filed as a `.cbz` — the plan gives it a `.cbz`
  `dst_rel` while its `src` stays `.zip`, and `apply_plan` renames it by copying to
  that destination (no repackaging). `validate_plan` accepts `.zip` under `Comics/`
  only when the destination ends in `.cbz`. Non-comic `.zip` files (samples,
  extras) are junk and left out of the plan as usual.
  **Loose page images → `.cbz`.** A release of bare scanned pages (no archive at
  all) is packaged, not dropped: each story folder is planned with a directory
  `src` and a `.cbz` `dst_rel`, and `apply_plan` zips the pages in natural order
  (§ Loose page images are packaged). Skip a folder whose material is already in a
  collected edition on the library, and file a multi-folder work as successive
  volumes.
- **Mixed torrents** — a single torrent can contain more than one kind of thing
  (e.g. a Steins;Gate release with the TV series *and* its movie). Placement is
  decided **per file** by its destination top-dir, so the episodes go to `Shows/`
  and the movie to `Movies/` from one plan; `media_type` is set to `"mixed"`.
- **Junk is scrubbed** — creditless openings/endings (NCOP/NCED/textless),
  previews, samples, and ad pages are simply left out of the plan. Anything not in
  the plan is deleted with the local download and never reaches the library.
- **Duplicate episode variants collapse to one copy** — a big pack often ships the
  *same* episode more than once (a complete-series One Piece pack that bundles
  `Episode 001-206 Uncropped (480p)` and a colour-distorted `(1080p Upscale)`
  alongside the real BD/CR season folders). Every copy maps to the identical
  destination, so a naive plan lists N sources for one `dst_rel`. The prompt is told
  to keep exactly one per episode — the highest-quality main line, not an
  `Uncropped`/`Upscale`/lower-resolution alternate — and the harness enforces it
  regardless (§ Duplicate variants collapse, not crash), so the pack no longer fails
  on its own duplicates.

### The plan schema (the interface contract)

```json
{
  "media_type": "show",              // "show" | "movie" | "comic" | "novel" | "mixed"
  "title": "Jujutsu Kaisen",
  "year": 2020,
  "owned": false,                    // true ONLY when Jellyfin can't scrape the
                                     // layout; then episode_title+plot are required
  "tmdb_id": 95479,                  // TMDB id (series for shows, film for movies).
                                     // Recommended for shows; REQUIRED for movies.
                                     // The engine writes an unlocked <movie> .nfo
                                     // with this id so Jellyfin pins the exact film
                                     // and can't mis-match it to a sibling in the
                                     // same collection. In a mixed/multi-movie plan,
                                     // set per-file tmdb_id (each film its own).
  "tvdb_id": 410031,                 // Optional TheTVDB series id. Strongly advised
                                     // for anime and any sequel/spin-off: the engine
                                     // pins it into tvshow.nfo alongside tmdb_id so a
                                     // TheTVDB-first library can't merge the show onto
                                     // its parent series.
  "existing_match": true,            // did it match a pre-existing library folder?
  "reasoning": "one-paragraph summary of the calls made",
  "files": [
    {
      "src": "/absolute/path/in/download/episode.mkv",
      "dst_rel": "Shows/Jujutsu Kaisen (2020)/Season 01/Jujutsu Kaisen (2020) - S01E48.mkv",
      "season": 1, "episode": 48,
      "episode_title": "required for specials and for every owned-show episode",
      "airs_before_season": null, "airs_before_episode": null, "airs_after_season": null,
      "plot": "required for every owned-show episode; optional otherwise"
    }
  ]
}
```

A `comic` plan drops all the video-only fields — just a destination per volume:

```json
{
  "media_type": "comic",
  "title": "Black Clover",
  "existing_match": true,
  "reasoning": "matched Comics/Manga/Black Clover; continued volume numbering",
  "files": [
    {"src": "/download/Black Clover v37.cbz",
     "dst_rel": "Comics/Manga/Black Clover/Black Clover v37.cbz"}
  ]
}
```

A `comic` plan may also point `src` at a **directory** instead of a file — the
loose-page packaging case (§ Loose page images are packaged). The directory is a
story/chapter folder of bare page images, and `apply_plan` zips it into the `.cbz`
at `dst_rel` (which must end in `.cbz`):

```json
{
  "media_type": "comic",
  "title": "Junji Ito Collection",
  "existing_match": false,
  "reasoning": "loose page images, one folder per story; packaged each as a .cbz",
  "files": [
    {"src": "/download/Junji Ito Collection/Alone With You",
     "dst_rel": "Comics/Manga/Junji Ito Collection/Junji Ito Collection - Alone With You v01.cbz"}
  ]
}
```

A `novel` plan (light novels / e-books) is the comic plan's sibling, but its
destination starts with `Novels/`, and `apply_plan` routes it to the **Google
Drive** `Novels` folder rather than the media library — `.epub` is served to an
e-reader, not to YACReader, so it must never land under `Comics/`:

```json
{
  "media_type": "novel",
  "title": "Overlord",
  "existing_match": true,
  "reasoning": "filed Overlord volume 1 as a light novel under Novels/",
  "files": [
    {"src": "/download/Overlord v01.epub",
     "dst_rel": "Novels/Overlord/Overlord v01.epub"}
  ]
}
```

`dst_rel` is relative to `MEDIA_ROOT` and must start with `Shows/`, `Movies/`,
`Comics/`, or `Novels/`. Placement is validated **per file** by that top-dir: for
a single-type plan every file must match `media_type`; for a `"mixed"` plan each
file picks its own top-dir. The validator enforces the right file family per
top-dir (`Comics/` takes `.cbz`/`.cbr`/… archives, plus a plain `.zip` that must
be filed as `.cbz`; `Shows/`/`Movies/` take video + subtitles; `Novels/` takes
`.epub` e-books), so a `.cbz` can never be filed as an episode nor a `.mkv` as a
comic. A **directory `src`** is accepted only when it
targets a `Comics` destination ending in `.cbz` — the loose-page packaging case —
so a folder can never be filed as a video episode, and a page-image folder can
never be filed as a non-`.cbz` archive. For movies and
comics, `season`/`episode` may be omitted; comics never use the

**Image-scan "light novels" re-route (§ diagnosis 4.2).** A `.zip` the identify
run plans into `Novels/` is sniffed before validation: if it holds no `.epub`/`.pdf`
at all (it is really a ZIP of page images, mis-titled "[Light Novel]"), the
validator re-routes it to `Comics/Manga/<Series>/` as a `.cbz` (the pipeline already
renames `.zip`→`.cbz` on apply) and fixes `media_type` to `comic`/`mixed`, instead of
rejecting it as `wrong file type for Novels/` and leaving it in `failed/`.
`owned`/`airs_*`/`.nfo` machinery. **Owned plans — and every `Season 00` special —
get one extra check:** every locked show episode must carry a `season`, `episode`,
a non-empty `episode_title`, and a non-empty `plot` — the validator rejects the
plan otherwise, so a lazy `"owned": true` can never lock a blank "Episode N" into
the library (leave a show un-owned and let Jellyfin scrape it if real per-episode
metadata isn't available). **The same title+plot check applies to any file placed
in `Season 00`, independent of the `owned` flag**, because specials are *always*
locked at apply time (§ Identification, *Specials are always owned*) — a
`Season 00` file with no metadata fails the plan outright, so an un-owned special
can never be shipped for Jellyfin to mis-scrape.
**Movies get the symmetric check:** every movie video must resolve to a TMDB id
(its own `tmdb_id`, or the top-level one when the plan places exactly one movie),
and no two distinct movies may share an id. The first stops an oddly-titled film
shipping blank because nothing identified it; the second stops the
collection-sibling mis-match (a trilogy's Part II filed under Part I's entry) at
plan time — the same failure the per-movie `.nfo` seeding prevents at apply time,
now refused before any file moves rather than only mitigated after.

**Shows get the parallel check:** a plan that places show video into a folder that does
*not* yet exist (a new show, not a match against the library) must pin a `tmdb_id` or
`tvdb_id`, or it is refused — Jellyfin cannot identify a series with no identity, and no
scan or refresh ever fixes a blank one. This is the validator backstop for the identify
prompt's "resolve to a real provider" instruction: the prompt declines an unmatchable
torrent (an empty plan), and this refuses a plan that places files into a fresh folder
but supplies no id. Two cases are exempt, mirroring the movie side: an `owned` show
(locked per-episode title+plot, so it needs no scrape — One Pace and the YouTube ingest),
and an *existing* folder (its `tvshow.nfo` already carries the identity, so the plan
need not repeat it).

**The one alternative to a TMDB id: an OWNED movie.** Some films are on *no* provider
at all — a fan edit, an original work, a long-form web video the YouTube source files as
a standalone film (§ *Non-torrent sources*). There is no id to pin, and demanding one
would simply make them un-ingestable. So a movie file may set `owned: true` (per file, or
plan-wide) and omit `tmdb_id` — but it must then carry a real `movie_title` **and**
`plot`, and `apply_plan` writes those into a **LOCKED** movie `.nfo`.

This is the exact trade the owned *episode* path already makes, and the symmetry is the
justification: the id requirement exists to stop a film shipping blank or fuzzy-matched,
and locked metadata prevents both **by construction** — Jellyfin serves what we wrote and
never title-searches, so it can never pull some unrelated real movie's poster and plot
onto an off-provider film. The guard is that missing title-or-plot on an owned movie
fails the whole plan, because a locked blank is permanent. A findable film must still pin
its id; this is not a way to skip the lookup.

---

## Placement guards: the season ceiling, the same-episode collision, and the franchise namespace

Three deterministic guards in `library.validate_plan`. All are free — no AI call, no paid
API — and all were narrowed against the whole history before shipping, because the first
draft of each rejected work the library had accepted correctly.

### The season ceiling (`_reject_season_gap`)

A plan may not file episodes into a season the show does not have. The check runs in three
layers, and only the first two bind an `owned` plan:

1. **The ceiling.** TVMaze (free, key-less, via `epguide.max_season`) says how many seasons
   have aired. A plan filing into Season 05 of a four-season show is refused.
2. **The source's own numbering.** The ceiling only fires when the RELEASE ITSELF does not
   claim that season either. This is what separates a wrong plan from a provider that is
   merely behind — very common for anime, where TVMaze often lists each cour as a separate
   show, and always true for a fan re-cut like One Pace with its own arc numbering.
3. **The gap heuristic**, unchanged, and still waived for an `owned` plan.

**`owned` no longer waives everything, and that was the bug.** Marking a plan `owned` means
it authors real titles and plots, so an unresolvable season *number* cannot silently blank
the episodes — a good reason to waive the heuristic in (3). It was never a reason to waive a
*fact*. `owned: True` is exactly how a pack whose own files are named
`Dawn.of.the.Croods.S04E01E02-…` was filed into `Season 05`, a season TVMaze says does not
exist, past a guard that would otherwise have contradicted it on its own filenames.

Verified by replaying all 748 historical plans (`scripts/test_placement_guards.py`): 746
accepted, 2 rejected, and both rejections are known-wrong plans. An earlier, broader version
of the same rule rejected 55 — 54 of them false positives — which is why the replay is a
checked-in test and should be run before any change here.

### The same-episode collision (`_reject_same_episode`)

**Two distinct video files may not claim one `SxxEyy` of one show in a regular season.**
Jellyfin resolves one episode per number, so the second file is either a duplicate or a
misnumbering. On 2026-09-15 it was the misnumbering: the release names every part of a
serial `Doctor Who - S01E05 (005) - The Keys of Marinus (1..6) - …`, and the model copied
the **serial** number onto all six parts instead of continuing the library's per-part run
(An Unearthly Child Parts 1-4 were already E01-E04). Every later wave then shifted to fit
around the collision, so the whole season was corrupt by the time `library_health` flagged
it.

The guard is keyed by SHOW, not season+episode: a multi-show pack (Steins;Gate + Steins;Gate
0) legitimately files the same episode number into two different show folders. Season 00 is
exempt — a special split across `- part1`/`- part2` is accepted content here (Kaguya-sama
S00E06), and every multi-part *regular* episode already in the library (The Office, The Bad
Batch, Star Wars Rebels) is numbered consecutively, which is exactly what the refusal asks
the next provider to do. Replayed over all 820 accepted journal plans: 0 rejected; the
Doctor Who wave that caused the repair is rejected. `scripts/test_placement_guards.py`
carries both directions.

**Why this guard had to come before the existing-episode collapse.** The wrong numbers were
not merely filed — they were *silently deleted*. `_collapse_existing_episode_collisions`
drops a planned file whose (season, episode) already holds a differently-named library
video, on the assumption that the two are the same episode. The serial's parts were planned
at the serial numbers (Daleks `E02`, Edge `E03`, Marco Polo `E04`), each colliding with the
already-filed An Unearthly Child parts at those slots, so seventeen downloaded parts were
dropped from the plan and the chunked wave then freed their bytes as "not in plan (junk)".
The plan-on-disk still named them; the validator had already pruned them. Rejecting the
collapsed plan up front is what stops the loss: a retried plan numbers the parts at
E05+ where nothing exists, so no part is dropped and no byte is freed.

### The serial-release numbering guard (`identify.serial_release_map`, `validate_plan`)

**A release whose `SxxEyy` is a serial number looks exactly like a correctly-named one.**
The classic Doctor Who pack names every part of a story `S01E05 (005) - The Keys of
Marinus (1) …`: six files advertise `S01E05`, the `(1)` is the part, and the folder says
`Parts 1-6`. Two different models copied the serial onto the destination — once on the
original ingest, and again on the 2026-09-15 re-fetch waves, which misfiled 28 files
(three as truncated copies) because the correct existing episode had been **evicted to
the pool**, so `_collapse_existing_episode_collisions` — which scans `MEDIA_ROOT` —
could not see it.

The numbering is arithmetic, so the harness computes it. `identify.serial_release_map`
parses the folder's `Parts N-M` range and accumulates ranges per season by serial; it
handles the shapes real later seasons use — `S04E01(028)` with no space, a story split
across folders (`Parts 5-8`), a whole season packed into one folder (`Parts 1-14`, the
files inside carrying different serials), and it refuses Bonus/Intro/Outro clips, which
are not episodes. The computed numbers are:

* **stated in the prompt** (`serial_numbering_block`, wave files only) as facts, and
* **binding in `validate_plan(serial_map=…)`** — a plan that files a mapped file anywhere
  but its computed slot is refused with the computed slot named. Both sides fail open:
  no map (an ordinary release) or an unmapped file (an extra) is simply not checked.

`test_serial_release_numbering.py` pins the arithmetic against the handoff's independently
confirmed slots (`S01E07` = The Escape, `S01E18` = Rider from Shang Tu, `S01E31` =
Strangers in Space) and both directions of the guard.

### The franchise namespace (`_reject_comic_at_franchise_root`)

**No comic file may be filed directly into a franchise master folder.** A master
(`Comics/ElfQuest/`, `Comics/Manga/Attack on Titan/`) holds sub-folders, one per series —
never files.

This is not tidiness. A master root that holds files becomes a shared `<Master> vNN`
namespace belonging to no series, and everything the table fails to recognise lands in it on
top of whatever is already there. Measured off `direct_ingest.log`:
`ElfQuest - The Final Quest (2026).cbr` was filed as `ElfQuest v04.cbz`, then
`ElfQuest v01.cbr`, then `ElfQuest v01.cbz` — three numbers for one work on three passes —
while `An_ElfQuest_Story_-_A_Gift_of_Her_Own.cbr`, a standalone story with no volume at all,
became `ElfQuest v06` and later `ElfQuest v10`. Two entirely different books ended up
sharing the slot `ElfQuest v01`. That is where the owner's "ElfQuest comics have repeats"
came from, and the repeats were collisions, not duplicate editions — a format-precedence
dedupe over those names would have deleted one of two different books.

**The rule is structural, not a numbering heuristic, and that is deliberate.** A numbering
rule was tried first — "a source with no volume number may not be filed as `vNN`" — and
measured against all 1510 historical comic filings it rejected 52, of which 45 were correct:
a one-volume standalone like `Uzumaki (Deluxe Edition)` is legitimately `Uzumaki v01` in its
own folder. What separates that from the ElfQuest mess is not the number, it is whose
namespace the number is in.

### The franchise table

`config.COMIC_FRANCHISES` maps a series to the sub-folder it lives in under its master.
**Every member names a real sub-folder now, the franchise's own main series included** —
`"battle angel alita": "Battle Angel Alita"` puts the original run beside Last Order and
Mars Chronicle instead of loose in the master root, which is what the owner asked for.

The table is **generated, not typed**:

```bash
python3 scripts/build_comic_franchises.py           # propose rows
python3 scripts/build_comic_franchises.py --no-net  # library evidence only
```

Its two inputs are the library's own sibling-folder layout and AniList (free, key-less), so
every row is either a folder that exists on disk or a series AniList lists under the same
franchise. It grew the table from 5 franchises to 24. Re-run it after the library grows and
paste what it prints; never add a franchise from memory.

**Adding a row changes placement for NEW arrivals only.** The already-flat folders must be
migrated in the same change, or a series is split across two paths:

```bash
bash scripts/migrate_comics.sh                      # plan only
bash scripts/migrate_comics.sh --apply
```

## Safety invariants

These are the load-bearing guarantees. Do not weaken them.

1. **The model proposes; the harness disposes.** The identify run has no authority to
   move or delete library files. It writes a plan. `library.apply_plan` and the
   cleanup step are deterministic code. A confident-but-wrong plan can misfile,
   but cannot delete or overwrite (see below), and every misfile is recorded and
   reversible (see § Decisions journal).
2. **Never overwrite a pre-existing file (except One Pace).** A `dst_rel` that
   already exists in the library is **never** clobbered. `library.validate_plan`
   still rejects a plan that escapes `MEDIA_ROOT` via `..` or points at a non-media
   file. Two *planned* files that map to one destination are no longer a rejection
   either: they are **collapsed to a single best copy** before validation
   (§ Duplicate variants collapse, not crash), and the duplicate-destination guard
   is now the post-dedup invariant assertion. An *existing* on-disk destination is
   likewise not a rejection: `apply_plan` checks existence live per
   file and **skips** it (leaves the on-disk file exactly as-is, records it
   `preexisting`, and drops the local copy). The check runs again right before each
   `os.replace` (TOCTOU guard), so a file that appears mid-apply is skipped, not
   overwritten. "Already there" is thus a clean success, and the overwrite invariant
   holds by construction. **The single deliberate exception is
   `config.ONE_PACE_PREFIX` (`Shows/One Pace (2013)/`)**, where a repeat is a newer
   re-cut that is *meant* to replace the old copy: there `apply_plan` overwrites via
   a gapless same-volume `os.replace` and flags the entry `replaced` (§ The One Pace
   exception). The invariant is therefore "never overwrite **outside** One Pace" —
   still fail-closed everywhere the library is your only copy of the media.
3. **Verify before delete.** The `VERIFIED` state — every applied file confirmed
   present (moved files by exact size, already-present files by existence) — is
   journaled **before** the cleanup step deletes anything. The only file ever
   *deleted* is the local download (re-downloadable); the source `.torrent` is
   *moved* to `finished/`, not deleted. The irreplaceable library is never in the
   deletion path.
4. **Invisible while in progress.** Files are copied into
   `MEDIA_ROOT/.ingest-staging/<hash>/`, a dot-dir, and then moved into place with
   `os.replace` (atomic within the volume). Media-Syncer prunes dot-dirs during
   its scan, so it never uploads a half-applied tree — the same principle as
   YouTube-Downloader's `.incoming`. Each final file appears atomically and
   complete, so even a mid-apply crash never exposes a partial file to the syncer.
5. **VPN gate.** A cycle does nothing (does not even connect to qBittorrent)
   unless a Tailscale CGNAT address (`100.64.0.0/10`) is bound to a local
   interface. We check the interface address rather than `tailscale status` on
   purpose: this machine runs the Tailscale **Mac app** (IPNExtension), whose
   socket the Homebrew `tailscale` CLI cannot reach under launchd's stripped
   environment — `tailscale status` works in a login shell but fails (exit 1) as
   the daemon runs it. The interface check is env-independent and tests the exact
   condition that matters: qBittorrent is bound to that `100.x` address, so if the
   address is gone no traffic can leak, and if it's present the VPN path is live.
6. **A partial plan may not delete the rest (2026-09-19).** Every plan is checked
   against the release's OWN file list before a byte is deleted — torrent metadata
   for a torrent, a disk walk for a wave or a direct drop. A file the plan names (or
   that sits under a planned loose-pages DIRECTORY, or that the harness itself
   collapsed as an intra-torrent duplicate) is accounted for; release `.nfo`/`.txt`,
   samples, screenshots, creditless OP/ED/NCOP extras, subtitles beside a planned
   video, and sub-50 MiB videos are JUNK; **anything else is UNRESOLVED and parks
   the whole release** — `_fail` with the `unfiled` list on the record, every byte
   left on disk, the `.torrent` under `failed/`, nothing applied. The check runs
   again in `_advance_cleanup` immediately before the irreversible step, and the
   chunked path parks the release rather than freeing an unaccounted file. This is
   the seam that cost the Smurfs 365 files / 31 GB to a 40-file plan and Doctor Who
   (2005) 38 files / 60.9 GB to a wave's "not in plan" branch; `plan_coverage.py`
   is the classifier, `scripts/test_plan_coverage.py` the guard, and its journal
   replay found both (plus a third, unnoticed Yamato 2202 partial plan).
7. **A release's own year decides its series (2026-09-19).** `validate_plan` refuses
   a plan that files a release whose name states a year into a series folder of a
   different year (`Doctor Who 2005` into `Doctor Who (1963)`), and refuses a
   single-folder plan whose own `year` contradicts the folder. Deliberately narrow,
   because the first replay showed the broad version rejecting 22 of 177 accepted
   plans (franchise parts like `Lupin III Part IV` in the (1971) folder, bracketed
   CRC32s read as years); the rule applies only when the release name says nothing
   more than the folder's title, and square-bracket groups/resolution pairs are
   stripped first. Fail-open in every other direction.

---

## Recovery and failure behavior

The journal (`state/journal.jsonl`) is append-only, last-writer-wins. On restart,
`journal.load_records()` replays it and the next cycle resumes **every** torrent
mid-pipeline from its recorded state — advancing each active one and re-admitting
queued ones against the disk budget. Idempotency per state is covered in the
state table above.

### Compaction — the journal is compacted, never rotated

Append-with-last-writer-wins is exactly what makes the journal crash-safe, and it is also
what makes it grow without limit: a torrent re-snapshotted every cycle — a `DEFERRED` one
waiting on disk space, say — writes a line per cycle forever. Left alone it reaches
tens of megabytes of snapshots describing a few hundred torrents — on the order of 15 lines
per torrent, and thousands for the worst single one.

`journal.compact_if_needed()` runs at the top of every cycle and rewrites the file as one
line per torrent once it is over `config.JOURNAL_COMPACT_MIN_BYTES` *and* snapshots
outnumber torrents by `JOURNAL_COMPACT_MIN_RATIO`. On the real file that was **83.6 MB →
16.8 MB, an 80% cut**. Under threshold it costs a single `stat()`, which is nearly every
cycle.

**Compaction is lossless for the state machine, and that is the only reason it is allowed
to touch this file.** `load_records()` already keeps just the last line per info hash, so
writing exactly what it computed preserves the resumable state bit for bit — verified by
asserting the replayed record set is identical before and after. What is discarded is the
intermediate transition history, which no code reads; the audit trail people actually read
is `decisions.log`, which is never touched.

**Compaction is not rotation, and must never be turned into it.** Rotation drops the
*oldest slice*, which for the journal means losing whole torrents. Compaction keeps **every
info hash** — including `completed` and `failed` — and only discards superseded snapshots
of the same torrent. Do not be tempted to prune terminal records by age: a `completed`
record is what tells a re-seen `.torrent` that its work is already done, and dropping it
invites a re-ingest of content already in the library.

Safety is **verify-then-swap**, not keep-a-backup. The rewrite goes to a `.compacting` temp
file in the same directory, is fsynced, re-parsed, and its replayed state compared against
the live file's before `os.replace()` swaps it in atomically. Any mismatch, any `OSError`,
leaves the original journal **completely untouched** — the worst case is a stale temp file
and a journal that keeps growing, never a journal that lost a record. A crash mid-write
leaves the temp behind and the real journal intact, and the next cycle overwrites the temp.

Two things to know about recovery:

- **`FAILED` is terminal and not *auto*-retried.** This is deliberate: an
  auto-retry loop on a torrent the model keeps misjudging would burn tokens and
  churn. A failed torrent leaves its **local download** in place for inspection and
  files its **source `.torrent` into `failed/`** (out of the watch folder, so it is
  never mistaken for queued work). To retry, **move its `.torrent` from `failed/`
  back into the watch folder** — `register_new_torrents` treats the reappearance of
  a `FAILED` hash at the top level as a retry request and re-queues it fresh (same
  path as a COMPLETED re-drop), so no journal edit is needed. The move-back is the
  retry gesture; leaving it in `failed/` leaves it retired. (Clearing its journal
  record still works too, but is no longer required.)
- **Partial-apply resumes cleanly now.** If the process dies *during* `_advance_stage`
  — after some files were `os.replace`d into the library but before `STAGED` was
  journaled — a resume re-runs `apply_plan`, which checks each destination's
  existence live: the already-moved files are detected as present and **skipped**
  (never overwritten), the remaining files are moved, and the torrent finishes
  normally. This is the one path that would otherwise raise and need a manual glance;
  making "already present" a skip-and-succeed rather than a failure removed it.

Every failure logs `nothing pre-existing was touched` precisely because that is
the contract being asserted.

---

## Library health daemon (`scripts/media_doctor.py`)

The nightly metadata net below (`audit_metadata` → `repair_metadata` →
`save_posters`) is **disk-centric**: it detects blank `.nfo` and missing on-disk
posters. It has a blind spot for the failure you actually hit browsing on a phone
— a title that is fine **on disk** but broken in Jellyfin's **presentation**:

* a Series present in the DB whose children Jellyfin never resolved (0, or fewer
  episodes than exist on disk) — *"Monogatari just doesn't show up"*;
* a Series with **no Primary image** even though a `folder.jpg` sits right next to
  it on disk — *"Berserk has no poster"* (`save_posters` can't see this: it checks
  the disk, where the poster is present, not Jellyfin, where it's absent);
* an episode whose stored title is release-group junk (`[Judas] x265 10b`) or a
  bare `Episode N` while the real title is right there in the filename — *"Sinbad
  has janky episode titles"*;
* a Series wearing **another show's artwork** — poster, backdrop and episode stills
  all from the entry it was mis-matched to — *"The Seven Deadly Sins' cover
  and episode images are completely out of whack"* (see *Failure mode: a show keeps
  the wrong show's artwork after being re-identified*, below);
* two files for the same `SxxExx` (the Fairy Tail repeat), a 0-byte stub, macOS
  `._AppleDouble` / `.DS_Store` litter.

> **`N flagged, 0 auto-fixed` pass after pass is not a media_doctor bug — look for a parked
> `ffprobe` first.** Every auto-fix on the ladder below heals via a Jellyfin refresh/scan (by
> design — it never deletes), so all of them queue into Jellyfin's library scheduler. If that
> scheduler is jammed, the doctor keeps *detecting* correctly and heals *nothing*, and the log line
> looks like a logic failure in the doctor. The usual jam is an `ffprobe` parked in uninterruptible
> sleep on a cold pool file, which pins the scheduler's worker slots and freezes `Scan Media Library`
> at `progress=0` while it still reports `Running`. Full diagnosis and the kill-by-PID
> procedure — including why `pkill -f ffprobe` is dangerous here — are in Media-Syncer's README,
> *Failure mode: one hung `ffprobe` holds the whole library scan hostage*. Check that **before**
> debugging the ladder.

`media_doctor.py` (launchd `com.mikeyferguson.mediadoctor`, KeepAlive, a reconcile
every **30 min** — `CYCLE_SEC` in `scripts/media_doctor.py`, which is the single
source of truth; the plist deliberately does **not** override it) walks every show
and **compares disk truth to Jellyfin truth**, healing the gap on a ladder that is
safe to re-run:

* **AppleDouble/.DS_Store junk** → swept from the SSD store and every drive (always
  safe; one pass cleared ~4,900).
* **Missing episodes** (Jellyfin resolved fewer than exist on disk) → a targeted
  per-item recursive refresh (up to twice), then — only for a fully-broken
  0-child orphan — a library scan. **Capped per cycle** (`MAX_REFRESH_PER_CYCLE`,
  default 4) so a big backlog heals over several cycles instead of hydrating a
  pile of pool-only files at once.
* **Missing poster in Jellyfin** → push the on-disk `folder.jpg`/`-poster.jpg`
  straight in via `POST /Items/{id}/Images/Primary`, else adopt a provider poster.
  A series Jellyfin presents is checked even when **every episode is pool-only**
  (not hydrated to the local mount): the `.nfo` sidecars are local but the videos
  live only in the pool, and the old `disk_count == 0` early-return skipped such a
  show entirely — which is how Father Ted, That '90s Show and the two Haunting
  series rendered blank covers in the app while the doctor reported them healthy.
  Before giving up on a poster for a series with **no provider ids**, the ladder
  first applies the ids from its own `tvshow.nfo` (the same first-rung heal the
  missing-episodes ladder uses), since `RemoteImages` searches by identity and an
  unidentified series has nothing to look up. If there is still no poster after
  that — no on-disk poster, and no provider poster (because the series has no
  identity to search against, or the provider simply has no Primary) — the ladder's
  **final rung generates a cover from the show's own artwork** (`_generate_series_poster`):
  it reuses an episode still sidecar (`Season */*-thumb.jpg`, always on the real
  disk) or, failing that, a frame from a genuinely-local episode video, centre-crops
  it to a 2:3 poster and scales to 680x1020, writes `folder.jpg`, and pushes it into
  Jellyfin as the Primary image. This is the same local-art route the episode stills
  already use (`onepace_thumbs`), extended to the series level, and it is the reason
  **no series can ever be left with a blank cover** — including the no-provider class
  (One Pace, the YouTube-playlist shows, fan re-cuts like *Initial D Kaï (2026)*) that
  has no TMDB/TVDB entry to scrape against.
* **Missing identity** (`identity_missing`) → a series with **no TMDB/TVDB id** in
  Jellyfin *or* its own `tvshow.nfo` is the *root cause* of a cluster of symptoms —
  blank poster, junk titles, missing plots — because there is nothing to scrape
  against, and the mechanical poster/title ladders cannot heal it (`RemoteImages`
  searches by identity). It is flagged **non-auto** and **escalated** to the AI fixer
  to (re-)identify the show (or report that it has no TMDB entry / is a duplicate, for
  human review). Youjo Shenki and Eureka Seven Hi-Evolution Zero were the canaries:
  they sat `poster_missing` every cycle, un-escalated, forever — the blank cover was
  only the visible symptom of a missing identity the doctor never named. Note the
  escalation and the cover heal are independent: a no-provider show gets a locally
  generated cover immediately (previous bullet), while the AI still gets its chance
  to find a real TMDB id and upgrade the cover to the provider poster.
* **Janky episode title with the real title in the filename** → deterministically
  rewrite the `.nfo` `<title>` and the Jellyfin item's `Name` (locked), no scrape.
* **Artwork that isn't this show's** → two independent checks, because they fail
  independently (see the failure-mode section below for the whole story):
  * `artwork_identity_stale` — the **cause**. A series' `Tmdb`/`Tvdb`/`Imdb` id that
    was *set* and then *changed* means the show was re-matched, so every image
    fetched under the old id is the other show's. Healed with an images-only
    recursive refresh at `replaceAllImages=true` — the only thing that replaces an
    image slot Jellyfin already considers filled. The last-seen identity per show
    lives in `state/doctor_state.json` under `pids`, and is **not** cleared when the
    episode-count ladder resets, or an episode drop would erase the evidence.
  * `artwork_bogus` — the **shape**, whatever the cause. An episode image that is
    portrait, or byte-identical to ≥`ART_DUP_MIN` (3) siblings, or byte-identical to
    the series/season poster, is not a still. Healed per episode via
    `RemoteImages/Download`, aspect-checked so a poster is *refused* rather than
    written (`MAX_ART_FIX_PER_CYCLE`, default 40). Verdicts are cached in
    `state/doctor_art_cache.json`, keyed by a signature over every image's
    (name, size, mtime), so an unchanged show costs one `stat` per file instead of a
    JPEG-header read across ~14k files every cycle.
* **Movies too** (a separate pass over `Movies/`, which the show pass never saw): a
  film whose **plot or poster** Jellyfin never scraped is filled from the TMDB
  **RemoteSearch candidate** — that candidate carries the `Overview` inline, which is
  reliable even when Jellyfin's own refresh *detail*-fetch fails (the case that left
  *Blue Exorcist – The Movie* blank). The plot is written into the `.nfo` + the item;
  the poster via `RemoteImages`. Films live directly under `Movies/`, so this pass
  scans the top level only (a recursive walk of the mount would be far too slow).
  Parts 2+ of a **stacked** film (`- part2`, `pt3`, …) are skipped, exactly as the
  episode walk skips them: Jellyfin stacks the parts into one movie item whose
  `Path` is part 1, so a continuation has no item of its own **by design**. Counting
  one reports `movie_missing` against a healthy film on every cycle forever, and
  since nothing can ever satisfy it, it also feeds a permanent escalation candidate.
* **Movie collections too** (a third pass, over Jellyfin **BoxSets**, which neither the
  show walk nor the loose-movie pass saw): a collection is its own item with its own
  Primary slot, so a film can have a poster while its collection cover is blank — the
  "some movie collection displays have no image" failure. The heal is always mechanical:
  adopt the TMDB collection poster via `RemoteImages`, and when the provider has none (the
  Billy & Mandy / Kids Next Door collections, whose TMDB entries carry no poster), adopt
  the first member film's poster bytes instead — so a collection never shows a blank tile.
  Because this is a per-cycle reconciliation like the other two passes, a collection that
  loses its image is re-healed within one cycle, so the blank-cover state cannot re-accumulate.

> **Failure mode: a show keeps the wrong show's artwork after being re-identified.** Correcting a
> mis-matched series fixes its plots and titles and **freezes its pictures** — Jellyfin only ever
> fills an image slot that is *empty*, and `RemoteSearch/Apply` was called with
> `replaceAllImages=false`. The result passes every mechanical check: every file is present,
> well-formed and the right resolution, and simply depicts another show.
> The two checks above now catch it; full diagnosis, the reasoning behind the absent→set vs
> set→changed distinction, and the AppleDouble false-positive trap are in Media-Syncer's README,
> *Failure mode: a show keeps the wrong show's artwork after being re-identified*.

Everything it can't fix mechanically — a blank **synopsis** to research, a **wrong
or missing TMDB identity** — it escalates the way the
rest of the fleet does judgment work: **one headless AI run per cycle**,
budget-gated, told to fix that one show's `.nfo`/metadata and nudge Jellyfin. A
**duplicate** pair and a **0-byte stub** are no longer escalated: they are resolved
mechanically — keep the higher-quality copy (resolution, then `.mkv` over `.mp4`, then
size) and delete the loser *through the mount*, so the reaper purges the MEGA copy too.
Only a same-stem duplicate (`.mkv` vs `.mp4` of one episode) is auto-deleted; a
different-stem duplicate (a mis-numbered file) still escalates as a judgment call.
Because
this is *non-ingestion* DeepSeek work, escalation only fires inside DeepSeek's off-peak
**billing** window (`config.is_off_peak()`); outside it the worklist is kept. It
writes a phone-glanceable `library_health.txt` next to the free-space report, and a
`state/doctor_worklist.json` of what still needs a human. Run it by hand with
`--once` / `--dry-run` / `--show <name>` / `--no-escalate`.

## Human-attention watchdog (`scripts/fleet_health.py`)

The daemons self-heal everything they can, but a handful of failures are human actions
no AI can take: **topping up the DeepSeek balance**, re-issuing a rejected key, adding a
MEGA account, freeing a full disk, or a dead drive. `fleet_health.py` (launchd
`com.mikeyferguson.fleethealth`, KeepAlive, a pass every **5 min**) detects exactly
those and writes a phone-glanceable `fleet_health.txt` to the iCloud Torrents folder.
It never fixes and never fails — it is a detector + notifier.

* **DeepSeek balance** — polls `GET /user/balance` each pass. A dead balance or a
  missing/rejected key is a loud `[ACTION]` line; **nothing fails** meanwhile, because
  every consumer already *defers* on `AIUnavailable` (identify leaves the torrent at
  `DOWNLOADED`, escalation skips, the searcher's calls are best-effort). The file flips
  back to `ALL CLEAR` the instant the balance is topped up, and the deferred work
  resumes on its own the next cycle.
* **Disk** — flags a volume below `FLEET_HEALTH_DISK_WARN_GB` (50) or a vanished mount.
* **Google Drive (light novels)** — flags the Google Drive `Novels` folder when it is
  unreachable (is the Google Drive app running?) or below `FLEET_HEALTH_GDRIVE_LOW_GB`
  (1 GB). Books are tiny, so a low Drive is an unexpected fill-up — a human must free
  space or buy more, hence an `[ACTION]`, never a `[warn]`.
* **MEGA** — flags a stale `mega_free_space.txt` (Media-Syncer's sync loop stopped),
  **minus the reaper's pause**. The reaper kills Media-Syncer for the whole of a purge and
  stamps `state/reap_ms_paused`; a drain has run five days at a stretch, so a stale cache
  during one is the expected consequence of a healthy purge, not a fault. Reporting it as
  one meant this line was permanently on, and a warning that is always on is one nobody
  reads. What the pause was hiding is now reported instead: a marker present with **no
  reaper running** is a LEAKED pause — `mediasync` carries no `KeepAlive` and its watchdog
  deliberately stands down while the marker exists, so nothing resumes replication and it
  stays stopped indefinitely. That is an `[ACTION]`, and it had no detector at all before.
  Guard: `scripts/test_mega_pause_check.py`, all four branches.
* **Library** — surfaces the count of `NEEDS REVIEW` items from `library_health.txt`, so
  the phone report shows them without opening the other file.
* **YacReader** — is the reader indexing what the shelf HOLDS? Reports a folder row that
  would crash the loader on its next reload, a damaged index, drifted auto-update flags,
  and shelf files the index does not know about. The freshness check is deliberately
  conservative: `update_in_progress()` (the update transaction's journal, the open index,
  or an open comic archive — never CPU, which sits at ~1.5% during pool reads) keeps a
  running scan from reading as staleness, and only a long-quiet index with files missing
  becomes an `[ACTION]`. That is the detector whose absence let the 2026-09-14 ElfQuest
  drop sit invisible with every other check reporting fine.

**Removed on 2026-09-10 with the searcher**: the GetComics check (files the sweeper could
not auto-download past a captcha) and the tracker-reachability probes with their
auto-discovery of replacement mirror domains. Both existed to keep *discovery* working.
Nothing discovers now — the owner hand-drops every `.torrent` — so both were noise about a
subsystem that no longer exists, and a check that cannot be acted on trains you to ignore
the file it is written in.
Run by hand with `python3 scripts/fleet_health.py --once`. Tunables:
`FLEET_HEALTH_CYCLE_SEC` (300), `FLEET_HEALTH_DISK_WARN_GB` (50),
`FLEET_HEALTH_MEGA_STALE_SEC` (21600), `FLEET_HEALTH_GDRIVE_LOW_GB` (1).

## Episode thumbnails for no-provider shows (`scripts/onepace_thumbs.py`)

One Pace has no TMDB/TVDB entry, so its episode stills come from the One Pace Jellyfin
plugin — which keys off a per-episode id that can't be recovered after a DB reset. When
those stills are missing (a fresh ingest, or the id lost), `onepace_thumbs.py` (launchd
`com.mikeyferguson.onepacethumbs`, KeepAlive, a pass every **30 min**) generates a
representative frame from each episode video (ffmpeg at ~15% in, past the OP), saves it
as the `-thumb.jpg` sidecar, and pushes it to Jellyfin. It is idempotent (skips episodes
that already have a still) and capped per run (`ONEPACE_THUMBS_MAX_PER_RUN`, default 20),
so a cold-library backfill drains gradually and **new episodes are picked up the same
way** — the whole point being that this isn't a one-time fix but a standing one.

Tunables: `ONEPACE_THUMBS_SHOWS` (comma list, default `One Pace`; matched by folder-name
prefix), `ONEPACE_THUMBS_MAX_PER_RUN` (20), `ONEPACE_THUMBS_CYCLE_SEC` (1800).
`python3 scripts/onepace_thumbs.py --once --dry-run` to preview.

> **Adding a diagnosis pass: honour the problem-dict contract.** Every pass — shows,
> movies, whatever comes next — returns dicts of one shape, because `run_once` pools
> them all into a single `worklist` and the escalation block picks from that pool
> **without knowing which pass produced an entry**. Required keys are `show` (also the
> `state` key), `path`, `problems`, and `sig`.
> `sig` is the ladder-reset signal: while it holds steady the fix ladder and the
> per-item escalation count keep climbing; when it moves, the item is treated as new
> and both reset. Each pass picks the cheapest value that actually moves when the
> content does — **episode count** for a show, **(size, mtime)** for a single film,
> which is why a film's ladder resets on a re-encode rather than on a count that is
> always 1. Never reach for another pass's private key from shared code.
> A pass that omits `sig` does not merely break its own entries: the `KeyError` is
> raised inside the escalation block, which sits **above** the state save, the art-cache
> save and the `library_health.txt` write, so the whole cycle unwinds and persists
> nothing. The daemon goes on scanning the library every 30 minutes and throwing the
> result away, and the only symptom is one `cycle error (non-fatal):` line — with a
> stale `state/doctor_state.json` mtime as the tell.

> **⚠️ It NEVER deletes a media file, and its escalated AI runs *cannot* — they have no shell.**
> This is not a style choice — see the callout below. Jellyfin file-management is
> ON, so `DELETE /Items/{id}` deletes the real files through the mediafs mount
> (removing them from every drive); for content not yet uploaded to the pool that
> is permanent loss. The doctor heals orphaned series with a **library scan** (adds
> only, never deletes) and reports duplicates/stubs for a human to purge deliberately.

### ⚠️ Landmine: a Jellyfin item DELETE is a **file** delete

On the Mini, Jellyfin has media deletion enabled, so `DELETE /Items/{id}` (and
Infuse's "delete") is **not** a DB-only forget — Jellyfin removes the files on
disk, and because those paths are the `mediafs` mount, mediafs propagates the
delete to **every attached drive**. Content still in the (weeks-long) upload
backlog is drive-only, with no pool copy, so deleting it is **irreversible**.
 To
make Jellyfin re-resolve a broken series, use a **per-item recursive refresh** or a
**library scan** (both only add/update), never a delete.

## Metadata integrity: audit and repair

The subtlest failure this pipeline can produce is **files in the right show, with
correct names, but no episode metadata** — Jellyfin shows a bare "Episode N" with
no title or description. It happens when a show is filed un-owned (§ Identification)
under `season`/`episode` coordinates that the metadata provider can't resolve: a
custom season split that doesn't match TMDB's boundaries (Naruto, Naruto Shippuden),
or absolute numbering that runs past the provider's absolute-order coverage or
across a multi-entry show (DBZ Kai's Final Chapters, the newest Bleach arcs). The
un-owned show trusts Jellyfin's scraper; the scraper finds nothing at those
coordinates; nothing writes a fallback; the blank is permanent. It goes unnoticed
because the ingest itself succeeded — the files *are* on disk.

Two deterministic tools close this gap, and the improved identify decision
(the resolve test) prevents new occurrences:

- **`scripts/audit_metadata.py`** — read-only, re-runnable detector. Walks every
  show and flags each episode whose sidecar `.nfo` is missing or carries no
  non-empty `<plot>` — i.e. has **no description**, which is exactly the bug
  ("generic label, no description"). Detection is deliberately **plot-centric, not
  title-centric**: a numbered-only season whose episodes have no distinct `<title>`
  but *do* carry a real synopsis is correctly described and is **not** flagged
  (flagging it would make the nightly repair rewrite it forever). Valid because
  the Nfo metadata saver is required, so the `.nfo` mirror what Jellyfin holds.
  Prints a per-show blank count and, with `--json`, writes a repair worklist. Each
  blank carries its on-disk `season`/`episode` **and** a computed absolute episode
  index (counting only main-series episodes, specials excluded) — the reliable key
  for anime per-episode lookups when the on-disk season split is custom. That
  index counts episode **slots, not files**: a merged multi-episode file
  (`SxxExx-Eyy`) advances the count by its whole span, so a show that ships two
  episodes in one file (Gintama's `S01E01-E02`) doesn't push every later episode's
  absolute number one behind the truth. This matters because the repair below
  trusts `abs` over the on-disk `SxxExx`; an off-by-one there would make it look up
  and lock the *wrong* episode's title/plot — a populated-but-wrong `.nfo` the
  plot-centric test can never re-detect, strictly worse than the blank it replaced.
  It also audits **`Movies/`** (easy to leave outside the walk, in which case movie
  failures were invisible to the net): a film is flagged when its `.nfo` is missing
  or has no `<plot>` — never identified, ships blank (the One Piece "3D2Y" case) —
  **or** when two distinct movie files share one `<tmdbid>`, meaning one was
  identified as the other (the collection-sibling collision, e.g. Madoka Part II
  under Part I). The duplicate-id check matters because a wrong-but-populated match
  *has* a plot, so the plot-centric test alone can't see it. Movies are scanned by
  default (skipped under `--show` or `--no-movies`) and flagged movies ride the same
  `--json` worklist under a `movies` key. Flagged movies are now **auto-repaired**
  by `repair_metadata.py` (below), not just surfaced — the nightly fixes them the
  same night it finds them.
  For **One Pace** only, the audit also cross-checks each episode's `.nfo` `<title>`
  against the title One Pace embeds in the mkv container tag and reports any genuine
  *conflict* (not a mere variant — the check ignores articles, plurals, romanization,
  and shorter-vs-fuller forms so it doesn't drown in noise). This catches the
  wrong-but-populated sidecar the plot-centric test can't — a Wano episode carrying
  an Impel Down title/plot — which is invisible as a "blank." Like flagged movies it
  is **detection-only** (surfaced under `one_pace_title_mismatches` in the worklist
  and printed for review, never auto-repaired, because most title differences are
  harmless alternates); to fix a confirmed-wrong one, delete its `.nfo` and re-run
  the repair below.
  It also flags a **`Season 00` special that shares a `<tmdbid>`/`<imdbid>` with a
  film in `Movies/`** — meaning Jellyfin scraped that separately-held movie's entry
  onto the special (the Kim Possible case: the *So the Drama* film's imdb id landed
  on an *A Sitch in Time* special). This is the special-axis twin of the movie
  duplicate-id check: a wrong-but-*populated* sidecar the plot-centric test can
  never see, caught here by an exact id match (high-signal, not a fuzzy title). Like
  the two above it is **detection-only** (surfaced under `special_movie_collisions`
  and printed for review) — new ingests can no longer produce it, since every
  ingested special is now locked at apply time (§ Identification); a hit is a legacy
  un-owned special, fixed by owning it (or moving it out) by hand.
  Finally it flags the **sequel/spin-off MERGE signature: two distinct show folders
  carrying the same series `<tmdbid>`/`<tvdbid>` in their `tvshow.nfo`** — Jellyfin
  scraped one series' id onto another, or a sequel was pinned to its parent (the
  *Fairy Tail: 100 Years Quest* → *Fairy Tail* class). Cheap (one small `tvshow.nfo`
  per show), so it runs on every audit. Like the others it is **detection-only**
  (surfaced under `series_id_collisions` and printed for review); the ingest-time
  seed now fills a missing id so a *fresh* drop should not create this, so a hit is a
  legacy folder or a manual mis-scrape — fixed by re-pinning the sequel's own id
  (correct the `tvshow.nfo`, refresh).
- **`scripts/repair_metadata.py`** — the backfill. For each blank episode it asks a
  headless AI run (same architecture as identify: the model proposes, the harness
  disposes) to look up the **real** title and plot by the absolute episode number,
  then writes a **locked** episode `.nfo` byte-identical to the owned-ingest path.
  It never invents: the run returns title+plot per exact video path (it can't choose
  placement — season/episode come from the filename), the harness rejects any entry
  with an empty title or plot, so a blank can never be locked in place. It is
  idempotent (an episode no longer blank is skipped, so a re-run only retries what
  failed), backs up every `.nfo` it touches to `state/nfo-backup-<ts>/` first, and
  honors `--min-age-hours` so it never owns an episode Jellyfin simply hasn't
  scraped yet. Run `--all`, `--show NAME`, or `--worklist PATH`; `--dry-run` reports
  without calling the AI or writing. **One Pace gets a dedicated repair path**
  (detected by its folder): because it is a recut whose episodes do *not* line up
  1:1 with the anime, the absolute-episode-number lookup above would pull a wrong-arc
  synopsis, so the One Pace prompt instead resolves each episode by its **arc (the
  season, from `<namedseason>`) + arc-relative number**, takes the title from the
  mkv's own embedded container tag (extracted by the harness, since repair runs
  without `Bash`), and writes the plot from the **manga chapters** the episode
  adapts — explicitly forbidding the absolute→anime mapping that caused the original
  breakage.

  **Movies get their own repair path** (the audit's other blank class — a film
  Jellyfin never identified, e.g. an oddly-titled TV-movie special, or one filed
  under a collection sibling's `<tmdbid>`). An owned episode is repaired by writing a
  locked `.nfo` directly; a movie is repaired by **pinning the right film so Jellyfin
  scrapes it**. The same proposes/harness-disposes split holds, narrowed to
  its safest form: the run returns **only the correct TMDB `/movie/` id** (plus IMDb
  id and canonical title/year) per exact video path — it never moves, deletes, or
  chooses placement. The harness guards every answer (the returned year must match
  the filename's ±1; no two films may share an id — the collection-sibling collision,
  refused not just mitigated), writes an unlocked `<movie>` seed `.nfo` with that id
  (`library._movie_nfo_xml`), and calls Jellyfin's **full-refresh** on the item so
  Jellyfin writes the rich `.nfo` + poster + backdrop to disk exactly as it does for
  every other movie. It is idempotent (a movie that already has a `<plot>` is
  skipped), backs up any `.nfo` it overwrites, and honors `--min-age-hours`. It
  **needs `JELLYFIN_URL`/`JELLYFIN_API_KEY`** (Jellyfin holds the TMDB access and is
  what scrapes) and skips non-fatally if they are unset; skip it entirely with
  `--no-movies`. This is why hand-fixing a blank movie is no longer a chore — the
  nightly identifies and pins it automatically.

**The nightly safety net.** `scripts/nightly_metadata.sh` (launch agent
`com.mikeyferguson.torrentmetadata.plist`, 04:30 daily) runs the audit and repairs
anything blank for **≥ 48h** — blank **episodes** (locked `.nfo` written) and blank
or mis-identified **movies** (correct TMDB id pinned, Jellyfin re-scrapes) alike —
long enough that Jellyfin's own scans have demonstrably failed on it, so the repair
is safe and never fights the scraper.
This makes "we never silently ship blank episodes again" a standing guarantee
rather than a thing to remember. The identify prompt steers the owned decision,
the digest now reports each show's numbering style + **per-season episode counts**
+ locked/blank counts so a future run sees which shows are already broken *and*
which numbering scheme each follows (§ The prime directive — the per-season counts
are what expose a Parts-vs-aired-seasons mismatch before it misfiles), and this net
sweeps up anything that still slips through.

---

## Metadata backup: the sidecars Media-Syncer doesn't replicate

> **Cross-repo contract: `METADATA_BACKUP_REMOTE` is excluded from Media-Syncer's media allocation.**
>
> Three daemons write to that account on their own schedules — this repo's `db_guardian` and `backup_metadata`, plus Media-Syncer's `backup_state` — and none of them can hold Media-Syncer's upload lock without either starving behind a days-long upload phase or blocking it. If the media uploader also allocated there, two independent writers would spend the same 20 GiB and push the account over quota.
>
> Media-Syncer therefore reads this value (`config.upload_excluded_remotes()`) and never places media on it, while still **indexing** it so media already stored there stays visible in `remote_inventory.json`.
>
> **Changing this value moves that exclusion.** Point it at an account you are content to remove from the media pool — ideally one holding little or no media. Do not pick "whichever account has the most room": that is precisely the account the media uploader most wants.
>
> The tree cannot collide with the media pool **by path** (Media-Syncer only looks at `Shows/`, `Movies/`, `Comics/` and ignores non-media extensions). Path safety was never the issue; quota was.

Media-Syncer replicates only true media to the MEGA pool — its scan is filtered
to video/subtitle/comic extensions and **deliberately ignores every Jellyfin
sidecar** (`.nfo`, posters, `-thumb.jpg`, `.trickplay`) and knows nothing about
this repo's `state/` dir. So the metadata this pipeline creates has, by default,
**no off-machine copy** — and the most valuable piece is exactly the one that
can't be regenerated: an **owned/locked episode `.nfo`** is owned *precisely
because* Jellyfin can't scrape it (§ Metadata integrity), so if the SSD library root is lost
the media restores from MEGA but every owned show comes back as bare "Episode N".
The `state/` audit trail (`journal.jsonl`, `decisions.log`, the plans, the
`nfo-backup-*` folders) likewise lives only on the Mini.

`scripts/backup_metadata.py` closes that gap. It runs two `rclone sync` jobs to a
dedicated MEGA path and is wired into `nightly_metadata.sh` as its final step (so
it captures the freshly-owned `.nfo` the repair just wrote):

- **The library sidecars** — `MEDIA_ROOT` → `<remote>:metadata-backup/media`,
  filtered (`METADATA_BACKUP_FILTERS`) to keep `.nfo` and show/season artwork
  (`folder.jpg`, `backdrop.jpg`, `logo.png`, `season*-poster.jpg`) while dropping
  what Jellyfin trivially rebuilds from the video — per-episode `-thumb.jpg` and
  the `.trickplay` scrub tiles — plus the `.ingest-staging` dot-dir.
- **The state dir** — `state/` → `<remote>:metadata-backup/state` (everything but
  the single-instance `.lock`).

Two design points make it safe and non-destructive:

- **Isolated from the media pool.** The backup lands under a `metadata-backup/`
  top-level prefix, never `Shows/`/`Movies/`/`Comics/`. Media-Syncer's
  purge/probe machinery only ever looks at those three prefixes and its sync
  ignores non-media extensions, so this tree is invisible to the pool and can
  never collide with it. The target remote is a pool account with room (default
  `vm_mega1`, empty when wired up; override with `TORRENT_INGEST_BACKUP_REMOTE`).
- **Mirror, but nothing is ever hard-deleted.** Each `sync` runs with
  `--backup-dir` pointed at a timestamped `metadata-backup/_versions/<ts>/`
  folder, so a `.nfo` that is overwritten or removed locally is *versioned aside*
  rather than destroyed — the same reversibility contract as `state/nfo-backup-*`.
  The backup is **non-fatal**: an rclone failure (dead MEGA session, throttle) is
  logged and retried on the next nightly run, exactly like the audit/repair steps.

**Version retention (do not remove it).** `--backup-dir` versions and the rubbish bin
would otherwise grow without bound and fill the shared `vm_mega1` account to "over
quota" — which is exactly what happened, breaking all three writers of that remote.
So `backup_metadata.py` now prunes `_versions/` down to the newest
`METADATA_BACKUP_KEEP_VERSIONS` (14) nightly dirs and runs `rclone cleanup` on the
remote each run, and `db_guardian.py` cleans up after pruning old DB snapshots, because
MEGA's `use_trash` keeps a deleted file consuming quota until the rubbish bin is
emptied.

**Restore** is a plain additive pull (never deletes): `rclone copy` from
`<remote>:metadata-backup/media` back onto `MEDIA_ROOT` and from
`metadata-backup/state` onto `state/`, then let Jellyfin scan — the locked `.nfo`
return byte-identical and Jellyfin serves your layout without re-scraping. The
`_versions/` folders are now bounded automatically, so no manual pruning is needed.

### Poster backstop (`scripts/save_posters.py`)

This pipeline never writes artwork — posters are Jellyfin's job. It fetches them
from its image providers (TMDB/TheTVDB) and writes `folder.jpg` into the show
folder only when the library's **"Save artwork into media folders"** option is on
and a scan runs. That works for freshly-ingested shows (a new show's `tvshow.nfo`
is seeded with its `tmdb_id`, § Identification, so Jellyfin matches and scrapes a
poster), but a **migrated** show can end up with a stale `tvshow.nfo` referencing
a `folder.jpg` Jellyfin never actually obtained — it then shows no cover forever.
This is exactly what happened to *Fist of the North Star (1984)*: the nfo pointed
at a `folder.jpg` that was never on disk and Jellyfin held no poster for the item
at all, even though TMDB had 16 available.

`save_posters.py` is the poster analogue of the blank-episode audit/repair: for
every Series folder with **no poster image on disk**, it pulls the best remote
poster (and backdrop) from Jellyfin's providers, tells Jellyfin to adopt it (so
its DB/UI match disk), and writes the file directly — so it lands regardless of
the "save to media folder" setting, and the backup below then captures it. When a
series has **no provider poster at all** (no identity to search against, or the
provider has no Primary), it falls back to the same local route `media_doctor`
uses — generating a `folder.jpg` from the show's own artwork — so the backstop
guarantees a cover for the no-provider class too. It is
idempotent (a show that already has a poster is skipped), non-fatal per show, and
scoped to Series (loose movies carry their own `<title>-poster.jpg`). The nightly
wrapper runs it just before the backup; run `--dry-run` any time to list shows
still missing a cover, or `--show NAME` to fix one.

This closes the loop with the backup: **artwork must be on disk to be backed up**,
and the backstop is what guarantees it gets there.

---

## Jellyfin DB guardian (`db_guardian.py`)

Jellyfin's entire library — watch state, playlists, collections, user data,
scraped metadata links — lives in one SQLite file,
`~/Library/Application Support/jellyfin/data/jellyfin.db` (~226 MB). If it
corrupts (Jellyfin crash mid-write, a bad shutdown, a disk hiccup), you lose all
of it. A once-a-day 4am cron is the wrong shape for this twice over: it captures
at most a day's granularity, and — the real flaw — a naive cron that backs up on
a fixed schedule will happily overwrite the last good copy with a *corrupt* one
if the DB went bad before it fired. `db_guardian` is the robust replacement: a
KeepAlive user-agent that continuously watches, backs up **only verified-good**
snapshots, and **auto-restores** on corruption.

The loop (`DBG_*` knobs in `config.py`):

1. **Watch, unobtrusively.** Poll the DB's `(mtime, size)` signature (plus its
   `-wal`/`-shm` sidecars). The DB churns constantly during a library scan, so it
   acts only when the signature has **settled** (`DBG_QUIESCENT_SEC`, default 5
   min of no change) or when `DBG_MAX_BACKUP_INTERVAL_SEC` (1 h) has passed while
   it keeps changing. No snapshot mid-scan storm.
2. **Snapshot without ever locking Jellyfin.** Uses SQLite's online-backup API
   (`Connection.backup`, page-batched with a `sleep` between batches), which folds
   committed WAL content into a consistent single-file copy while Jellyfin keeps
   writing. The live server is never blocked.
3. **Verify the COPY, never the live file.** Runs `quick_check` + `integrity_check`
   on the snapshot. It is promoted into the rotating store
   (`…/jellyfin/db-guardian-backups/`, keep `DBG_KEEP_LOCAL`=14) **only if it
   passes**. So "the most recent backup" is by construction the most recent
   *good* one — a corrupt DB can never clobber the last good copy.
4. **Classify failure as transient vs corrupt.** A lock/busy/timeout is
   `transient` → retried next cycle, no heal. A `malformed`/`not a database`
   signature, or an `integrity_check` that returns non-`ok`, is `corrupt` → heal.
   (Proven on synthetic page-level b-tree corruption, not just a truncated file.)
5. **Heal on corruption.** Stop Jellyfin (graceful `osascript quit`, `pkill`
   fallback), move the corrupt `jellyfin.db`/`-wal`/`-shm` aside into
   `db-guardian-backups/corrupt/corrupt-<ts>/` for forensics, restore the newest
   verified snapshot over the live path, restart Jellyfin, and alert (writes
   `state/db_guardian_ALERT.txt` + a macOS notification). **Guardrails:** it
   *refuses* to heal if no verified backup exists yet (never makes things worse),
   aborts if it can't stop Jellyfin (no half-restore), and backs off if corruption
   **recurs within `DBG_HEAL_COOLDOWN_SEC`** (30 min) rather than thrashing on a
   failing disk.
6. **Off-machine copy, throttled and detached.** The newest verified snapshot is
   pushed to the shared metadata-backup remote
   (`config.METADATA_BACKUP_REMOTE:metadata-backup/jellyfin-db/`, keep
   `DBG_KEEP_REMOTE`=7) at most every `DBG_REMOTE_PUSH_INTERVAL_SEC` (6 h), in a
   **background thread** so a slow MEGA upload never stalls the watch loop.

Runs as `com.mikeyferguson.jellyfindbguardian` — a **LaunchAgent** (user session,
not a LaunchDaemon) because it must drive the Jellyfin GUI app and post
notifications, which a system-daemon context can't do. Manual commands:
`python3 db_guardian.py --status` (state), `--once` (one verified-backup pass,
ignores the settle gate). The pre-existing `com.mikey.jellyfin-db-backup` 4am
cron is left in place as an independent, gzip-compressed daily archive — harmless
redundancy, a different backup dir; disable it if you want only the guardian.

---

## Remote deletion: mirroring an Infuse delete to the cloud (the reaper)

> **REWORKED for the virtual library — now QUEUE-driven, not snapshot-driven.** With
> the virtual library, a media file vanishing from the SSD library root almost always means it was
> **evicted** (its bytes moved to the cloud, still in the library) — NOT deleted — so
> a "diff the SSD library root snapshot and purge whatever disappeared" trigger would
> wrongly purge evicted files' only cloud copy. The delete signal moved: when you
> delete a title in Jellyfin/Infuse, the files are unlinked **through the mediafs
> mount**, and `mediafs.unlink` (for a media payload in the inventory) stops presenting
> it, drops any cached copy, and appends its path to a deletions queue
> (`config.MEDIAFS_DELETIONS_QUEUE` = Media-Syncer's `mediafs_deletions.jsonl`).
> `reap.py`'s `drain_deletions_queue()` (which replaces the snapshot `cycle()` in the
> main loop) claims that queue after it settles (`REAP_QUEUE_SETTLE_SEC`, so a whole
> franchise batches into one pass), then runs the **same `purge_batch()` machinery**
> documented below — discover remotes, delete, verify-gone, clean the metadata backup,
> prune state. No circuit breaker is needed: the signal is an intentional delete, not
> an ambiguous mass-vanish. The snapshot/breaker/approval machinery described below is
> retained as reference but is no longer the trigger.

Everything above is **additive and write-once** — the pipeline never deletes
library media, and Media-Syncer never deletes a remote copy (bar the One Pace
churn class). That is exactly right for a backup. But it leaves one gap: when you
decide you're *done* with a show and delete it in **Infuse** (which removes the
video off the connected the SSD library root drive), the backup on the MEGA pool lives on
forever — and worse, Media-Syncer's download phase re-fetches *any* remote file
missing locally, so the next sync cycle would **re-download the very thing you
just deleted**. Without it, deleting a title means running Media-Syncer's hand-driven
8-step purge ritual by hand.

The **reaper** (`reap.py`, launch agent `com.mikeyferguson.torrentreap`) closes
that gap. It is a fourth daemon in this repo, a sibling to the ingest daemon, and
it is the **one component in the whole fleet allowed to delete remote backups** —
and only ever to *mirror a deletion you already made on the connected drive*. It
constantly watches the SSD library root; when a file the fleet has backed up
suddenly disappears **locally** while the drive is provably healthy, it pauses
Media-Syncer, purges every remote copy of that file, clears the vanished video's
Jellyfin metadata (locally and in the backup), prunes Media-Syncer's state so
nothing resurrects, and restarts Media-Syncer. It encodes Media-Syncer's manual
*Purging a Title* ritual (that repo's README) as code, scoped to individual files.

### The load-bearing safety contract: a dead drive must delete NOTHING

This feature deliberately points a deletion gun at your only off-site backup, so
the entire design is shaped around one rule: **if the the SSD library root drive itself
vanishes — unmount, dead disk, flaky cable — we must not delete anything**, because
the remote backup is precisely what restores you then. "A file disappeared
locally" is only a delete signal when it means *you deleted it*, never when it
means *the drive is gone*. Three mechanisms enforce that distinction, and none of
them may be weakened:

1. **Health gate.** A cycle does nothing unless `MEDIA_ROOT` is mounted **and**
   `Shows/` exists and is non-empty. An unmounted or empty-presenting drive fails
   this, and the reaper leaves its snapshot untouched — so when the drive returns,
   the diff is empty and nothing is purged.
2. **Debounce.** A file must be observed missing across `REAP_DEBOUNCE_SCANS`
   consecutive *healthy* scans before it is eligible for purge. This rides out a
   transient FS race, a mid-scan Jellyfin rename, or a brief remount hiccup — the
   file has to be *persistently* gone, not gone-for-one-poll.
3. **Circuit breaker.** Even after debounce, the batch is **aborted** (logged to
   `state/reap_ALERT.txt`, nothing deleted, state left intact so it self-recovers)
   if the loss looks like a **drive-scale event** — a power-outage freak wipe, a
   half-mounted volume dropping a whole subtree — rather than a deliberate delete.
   The single signal for that is **fraction**: more than `REAP_MAX_MISSING_FRACTION`
   (30%) of the tracked library gone in one shot trips it. A catastrophic loss takes
   out a huge slice of the library at once, so the fraction guard catches it; a
   fully-unmounted drive is already caught upstream by `media_healthy()`. Deleting
   one show — even a 220-episode one — is a small fraction and passes untouched.
   (On the current library of ~14.9k tracked files, 30% is a threshold near 4,500
   files.)

   Earlier builds also capped the **absolute file count** (2,000) and **title span**
   (25 distinct titles). Both were removed: they only ever punished a big *legitimate*
   delete that sat below the drive-scale fraction (clearing a large manga run, or
   refreshing several shows at once), forcing an approval for something that was never
   fault-shaped. The fraction is the only drive-wipe signal that matters; when a
   deliberate delete genuinely crosses 30%, `--approve` (below) is the sanctioned
   release.

   **Approving a genuine mass delete (`reap.py --approve`).** The breaker cannot
   tell a deliberate large delete (you cleared out hundreds of manga volumes, or
   deleted several whole shows at once to re-ingest fresher packs) from a drive
   fault — both look like "thousands of files vanished." Historically that left the
   breaker stuck: it alarm-looped on `reap_ALERT.txt` forever with no sanctioned way
   to say "yes, I meant it" short of hand-purging. The fix is a **content-specific
   approval token**. Running `python3 reap.py --approve` stamps the *exact* set of
   currently-confirmed-vanished paths into `state/reap_APPROVE.json`; on the next
   cycle the breaker is overridden **only for the intersection of what's still
   confirmed-gone and what the token names**, and **only while the drive is provably
   healthy** (the real anti-fault guard is never relaxed). The covered paths are
   purged and then consumed from the token. Because the token names exact paths, a
   stale token can never green-light a *different* future vanish — a drive fault
   drops different paths, so it is never in the token. It is also robust to the
   vanished set micro-fluctuating between the stamp and the cycle (a directory still
   being rewritten flickers files in and out of the scan): the intersection only ever
   purges approved paths, and any newly-vanished, unapproved file is parked for the
   normal debounce-then-breaker path. This is the sanctioned replacement for
   hand-purging; the alert text points at it.

Only files with an extension Media-Syncer actually replicates
(`REAP_TRACKED_EXTENSIONS` — mirror it against that repo's
`VIDEO_EXTENSIONS | COMICS_EXTENSIONS`, **not** this repo's broader ingest sets) are
tracked, because only those have a remote copy to purge; a local-only file's
deletion is a no-op.

### Detection: a snapshot diff

The reaper keeps `state/reap_snapshot.json` — the set of tracked-media files it
last saw present, in the same remote-relative key space Media-Syncer uses. Each
healthy cycle it re-scans `MEDIA_ROOT`, diffs against the snapshot, and any path
that was present and is now gone becomes a *missing candidate*, counted in
`state/reap_pending.json`. A candidate that survives `REAP_DEBOUNCE_SCANS` counts
is **confirmed**. Newly-appeared files (a fresh ingest) just fold into the next
snapshot; a candidate that reappears is dropped from pending. The **first ever
run establishes the baseline and purges nothing.**

### The purge, once a batch is confirmed and the breaker passes

The ritual runs with Media-Syncer **paused** the whole time (`launchctl kill` to
stop — it has no `KeepAlive`, so it stays down — and `launchctl kickstart` to
resume), so it can't re-download a half-purged file mid-ritual:

1. **Discover every remote copy** as the union of three sources — because any one
   alone misses copies (Media-Syncer's README documents each blind spot):
   `remote_inventory.json` residence (single-residence map), the **Mini upload
   logs** (`Uploading '<path>' to <remote>...`, the orphan-from-failed-retry safety
   net), and a **targeted fleet probe** — one recursive `rclone lsf` per (remote,
   title) across all ~390 remotes, which is current ground truth and catches
   Air-uploaded copies the Mini's log never recorded. A remote that *errors* during
   the probe (dead session, throttle) is treated as **maybe-present** and gets a
   delete attempt anyway — a silenced error is never scored as clean.
2. **Delete** each file from every candidate remote (`rclone deletefile`, which is
   idempotent — an already-absent file is success), `rmdir` the parents it just
   emptied (never a top-level `Shows/`/`Movies/`/`Comics/`), and `rclone cleanup`
   each touched remote to empty its rubbish bin and actually reclaim space.
3. **Verify, never trust the exit code.** `rclone` can exit `0` while deleting
   nothing on a dead session, so every deleted `(remote, path)` is swept with
   `lsjson`; a survivor has its session stripped, the delete re-run, and is
   re-verified. A path that can't be proven gone is **left in the snapshot for a
   retry next cycle** rather than marked done.
4. **Clear the video's metadata** (video extensions only — subs/comics carry none):
   the local Jellyfin sidecars Infuse leaves behind (`.nfo`, `-thumb.jpg`,
   `*.trickplay`, a loose film's poster/backdrop artwork), the matching sidecars in
   the **metadata backup** on `METADATA_BACKUP_REMOTE` (default `vm_mega1`), and —
   when a title's *last* episode is gone — the whole show's backup footprint: the
   live `metadata-backup/media/<title>` mirror **and** every dated
   `metadata-backup/state/nfo-backup-*/Shows/<title>` snapshot, **and the local
   `state/nfo-backup-*` copies** (else the nightly `backup_metadata.py` resyncs the
   deleted remote copies straight back). The now-media-less local title folder is
   pruned too, taking `tvshow.nfo`/artwork with it.
5. **Prune Media-Syncer's state** — remove the purged paths from `sync_state.json`
   and `remote_inventory.json` — so its next cycle neither re-downloads the file
   (missing-locally) nor rides a stale baseline. This is what makes the deletion
   *stick* across the restart. The inventory prune also drops the **metadata-backup
   keys** for the sidecars just cleared from `METADATA_BACKUP_REMOTE` — the
   `metadata-backup/media/<stem>*` sidecars for each purged video and, for a fully
   deleted show, its `metadata-backup/media/<title>/` mirror and every
   `metadata-backup/state/nfo-backup-*/Shows/<title>/` snapshot key. Those keys are
   inert to Media-Syncer (it never acts on a `metadata-backup/`-prefixed
   non-media path) and the inventory is rebuilt on the next full scan regardless,
   so this is a *tidiness* step — it leaves `remote_inventory.json` spotless
   immediately instead of carrying stale keys until the (multi-hour) rescan.
6. **Stop `library.db` claiming them.** For every path VERIFIED gone (never a
   survivor — its pool copy still exists and stays owned), the ledger rows naming it
   are marked `superseded` by `dbhook.record_purge`: episodes and films by title and
   item number, comics by folder chain (the franchise layout nests a series at an
   unpredictable depth) and then by file stem, and a collection only when exactly one
   row could be meant. Without that pass a purge leaves an ownership claim behind, a
   re-drop the acceptance gate refuses as "already owned". The same paused window then
   runs `dbhook.reconcile_comics()`: same-norm `comic`/`manga` pairs are folded to the
   kind the pool shelves the files under (the old blanket "every comic is manga" mapping
   had left 25 duplicate series), and numbered comic rows the pool no longer holds are
   superseded. An ambiguous norm — files under both roots, or none — is skipped, never
   guessed. Fail-open: a DB error is logged and never fails the purge.
7. **Resume Media-Syncer** and advance the snapshot: successfully-purged files
   leave tracking entirely; survivors stay tracked for a retry.

The resume is **verified and retried** (up to 3 times, each confirming the
process is actually back up) — the whole point of the reaper is that the syncer
comes back, so a flaky `kickstart` can't silently strand it. While paused, the
reaper holds a `state/reap_ms_paused` marker; if a reaper is killed mid-purge
before its `finally`-guarded resume runs, the next cycle finds that marker and
resumes Media-Syncer, so it is **never left paused across a reaper crash/restart.**

**Deleting more while a purge is in flight is safe — the settle-gate.** The reaper
is single-threaded and one cycle purges only the batch it confirmed at that cycle's
scan. A title you delete *during* an in-flight purge was still present when that scan
ran, so it stays in the snapshot and is picked up on a following cycle after its own
debounce. The danger that creates: Media-Syncer, once resumed, **re-downloads files
missing locally** (the Mini is the downloader — Media-Syncer README, § Download
phase), so if it woke between the first purge and the second, it would *resurrect*
the not-yet-purged deletion (restore it locally, then re-upload it). The **settle-gate**
closes this: once the reaper has paused Media-Syncer to purge, it keeps it paused
**across cycles** and only wakes it when a cycle finds **nothing more missing or
debouncing** — i.e., it is sure no further deletions are in flight. A deletion that
lands mid-purge therefore keeps MS asleep, is drained on a later cycle, and MS is
woken **exactly once**, at the end. A hard cap (`REAP_SETTLE_MAX_HOLD_SEC`, 30 min)
resumes MS anyway if an unpurgeable survivor would otherwise strand it offline.
`recover_media_syncer()` runs **once at startup** (crash recovery), not per cycle, so
it can never prematurely wake an intentional mid-drain hold.

Every purge is recorded in `state/reap_purges.log` (per-file, which remotes),
mirroring the ingest pipeline's `decisions.log` audit ethos.

### Run modes and install

`reap.py` runs as the daemon under the launch agent, but is scriptable:

```bash
python3 reap.py --status          # health, tracked count, snapshot/pending, MS up? (read-only)
python3 reap.py --dry-run --once  # detect + plan a purge, delete NOTHING (network-free)
python3 reap.py --reset-baseline  # re-seed the snapshot from disk now (e.g. after a manual purge)
python3 reap.py --approve         # approve the CURRENT confirmed-vanished set as a deliberate
                                  #   delete: the next cycle purges exactly those despite the
                                  #   breaker (drive must be healthy), then discards the token
```

The reaper is **not** loaded by `startup.sh` — installing a daemon that can delete
your backups is a deliberate opt-in, like the metadata safety net. To enable it,
load its launch agent once (it runs under the same conda env and Full Disk Access
grant as the ingest daemon, so if that is installed this is covered):

```bash
cp com.mikeyferguson.torrentreap.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mikeyferguson.torrentreap.plist
```

The first cycle establishes the baseline and does nothing; from then on, deleting
a title in Infuse is all it takes — the reaper does the rest within a couple of
scan intervals. If the circuit breaker ever trips, it writes `state/reap_ALERT.txt`
explaining why and purges nothing until the situation clears. If the trip was a
**deliberate** mass delete (not a fault), approve it with `python3 reap.py --approve`
(§ Circuit breaker → *Approving a genuine mass delete*) rather than hand-purging —
the next cycle then mirrors exactly that delete to the fleet and empties the bins.

**This daemon runs on the Mini only** — it refuses to act unless Media-Syncer's
`.host_mode` is `mini` (the host with the SSD library root mounted and the download role). On
any other host it idles.

---

## qBittorrent integration notes

- **Control is via the Web API** (`qbittorrent-api`), not the CLI. `startup.sh`
  enables the WebUI on `127.0.0.1:8090` with **localhost auth bypassed**
  (`WebUI\LocalHostAuth=false`), so the daemon's calls from the Mini itself never
  authenticate. Note that qBittorrent v5 nonetheless **refuses to start the WebUI
  unless credentials exist**, even with localhost bypass on — so `enable_webui.py`
  also writes `WebUI\Username=admin` and a `WebUI\Password_PBKDF2` hash (of
  `torrent-ingest`, PBKDF2-HMAC-SHA512). Those credentials are effectively unused:
  the WebUI binds to loopback only (`WebUI\Address=127.0.0.1`) so nothing off-box
  can reach it, and loopback is bypassed anyway. The password is set only once
  (re-runs don't churn it), so a login you set by hand is preserved. Port 8090
  because **8080 is taken by YACReader** on this machine.
- **The WebUI serves only while the GUI app runs.** `qbt.connect()` launches
  qBittorrent (`open -b org.qbittorrent.qBittorrent`) if the API is unreachable,
  then waits for it.
- **Enabling the WebUI edits `qBittorrent.ini`.** qBittorrent rewrites that file
  on exit, so `scripts/enable_webui.py` must run while qBittorrent is **stopped**
  (`startup.sh` quits it first). The editor is idempotent and preserves every
  other key verbatim (it only writes `Key=Value` with no spaces, as QSettings
  expects).
- **Category tag.** Every torrent we add carries the `torrent-ingest` category, so
  the daemon only ever acts on its own torrents and never anything the user added
  by hand in the same qBittorrent instance.
- **Auto-remove on completion is the completion signal, not a preference.** qBittorrent is
  held at `max_seeding_time_enabled = true`, `max_seeding_time = 0`, `max_ratio_act = 1`:
  a torrent is removed the moment it finishes downloading, and **its files are kept**.
  `_advance_downloading` reads "gone from qBittorrent" as "finished" and then confirms
  against the bytes on disk, so the engine does not work without this. Nothing seeds.

  `max_ratio_act` is an ordered enum — `0 Stop, 1 Remove, 2 RemoveWithContent,
  3 EnableSuperSeeding`. **It must stay `1`.** At `2` qBittorrent deletes the payload the
  instant the download completes, before identify has run and long before anything is
  filed — total, silent loss, and one click in the WebUI away. At `0` or with the rule
  disabled, finished torrents seed forever and no completion is ever detected.

  `qbt.assert_share_limit_policy` re-asserts all three every cycle and repairs drift rather
  than trusting they were set once. Chunked packs escape this rule per-torrent, by design
  (above), and are removed by their own driver.
- **Added running; paused-add is a fallback.** Size is read from the `.torrent`
  file, so torrents are normally added **running** (`qbt.add(..., paused=False)`).
  The paused-add helper (`add_paused`, `is_paused=True`, which `qbittorrent-api`
  maps to v5's stop/start semantics) survives only as the fallback for a torrent
  whose size can't be parsed from the file.
- **Auto-remove on completion is supported.** If qBittorrent is set to delete a
  torrent from its list when it finishes (keeping the files), the daemon will often
  find the torrent already gone when it next polls. That is treated as *probably
  finished*, not failed: it confirms by disk, marking `DOWNLOADED` when the on-disk
  payload matches `total_size` and only `FAILED` when the data is genuinely short.
  This is why padding files must be excluded from `total_size` (§ Disk-space policy) —
  otherwise every completed hybrid torrent would read a fraction short and be wrongly
  failed on the auto-remove path.
- **Info hash is computed locally** from the `.torrent` (`qbt.info_hash_from_file`
  — SHA1 of the bencoded `info` dict) so we can track the torrent by hash
  regardless of what `torrents_add` returns. **Caveat: this is the BitTorrent v1
  hash.** v1 and hybrid torrents work (hybrids expose a v1 hash). A **v2-only**
  torrent has no v1 hash, so `get()` will not find it and the torrent will
  `FAILED` shortly after add. v2-only torrents are rare in the wild today; if this
  ever bites, compute the v2 (SHA256) infohash and match on `infohash_v2`.

---

## iCloud materialization

The watch folder is iCloud Drive, so a `.torrent` may be a **dataless
placeholder** (`.Name.torrent.icloud`) rather than a real file. `find_torrent_files`
resolves placeholders back to their intended path, and `materialize()` runs
`brctl download` and waits for the real bytes to arrive before the file is hashed
or added. A `.torrent` whose *release title* begins with a `.` (a few nyaa
releases do — e.g. `.Planetes.2003.TV...`) still registers: only the `.icloud`
placeholder dot is stripped, never the file's own leading dot, so a hidden torrent
is not stranded in the watch folder. The searcher now also strips leading/trailing
dots from the filenames it writes, so such titles drop as visible files in the
first place. A file that materializes only **partially** parses as broken bencode; it
is filed to `failed/` once it has been untouched for `UNPARSEABLE_GRACE_SEC`
(§ A truncated `.torrent`: recovered as a magnet, or filed to `failed/`). On completion the `.torrent` is **moved into the `finished/` subfolder**
(via `os.replace`, same iCloud volume) rather than deleted, so it disappears from
the watch folder on all devices — freeing the folder — but stays in iCloud as a
re-droppable record. Because the watch-folder scan reads only the top level,
`finished/` is invisible to it and its torrents never re-ingest on their own.

---

## Curated playlists (`playlist.py`) — turning a 367-episode beast into a watch

Some shows are too long or too uneven to watch straight through (Gintama, Naruto,
Bleach, the Dragon Balls, the original Sailor Moon, ...). The fix is a **curated,
ordered playlist**: only the episodes worth watching — the ones that are *super
important or super fun* — in watch order, appearing as a single pinnable item in
Infuse. This is a companion feature to the ingest pipeline, not part of the state
machine; it never moves, deletes, or writes a library file.

### What was figured out (so the next show goes faster)

The research that shaped this design, recorded here so it needn't be rediscovered:

- **Infuse *does* surface Jellyfin playlists.** Infuse 8.2.3+ added playlist
  browsing/management for Jellyfin; they appear under **Library → Playlists** and
  can be pinned to the home screen (pinning extended to all devices in 8.4). No
  new Jellyfin *library* is needed — a playlist is its own first-class construct.
- **A Jellyfin playlist alone is NOT durable, and that is the whole design
  problem.** It lives in Jellyfin's own data dir
  (`~/Library/Application Support/jellyfin/data/playlists/<name>/playlist.xml`),
  **not** on the SSD library root — so neither Media-Syncer nor this repo's
  metadata backup touches it. It references **volatile internal item ids**, and
  there is a live, unfixed Jellyfin bug where a library scan silently *deletes*
  UI/XML playlists (jellyfin#14589). Build hours of curation into Jellyfin alone
  and one bad scan or data-dir wipe erases all of it.
- **Therefore: manifest-as-truth.** The durable source of truth is a manifest
  file under `state/playlists/`. `scripts/backup_metadata.py` already mirrors the
  entire `state/` tree to the MEGA `metadata-backup/state` path (only `*.lock` is
  excluded) — the *same* backup that protects the irreplaceable locked `.nfo`. So
  a manifest is backed up exactly like the rest of this pipeline's metadata.
  Jellyfin's playlist is a **rebuildable projection**; lose it and the nightly
  rebuild restores it from the manifest, keyed to stable **library paths** rather
  than Jellyfin's internal ids. Same ethos as identification: a durable file is
  the truth; the serving layer is reconstructible from it.

### How it works

`playlist.py` reads each manifest, resolves its ordered tokens to on-disk files
(failing closed if any token has no file), maps each file to its Jellyfin item id
by **exact `Path` match** (series episodes via `/Shows/{id}/Episodes`, films via
an `IncludeItemTypes=Movie` query), and **creates or replaces** the playlist via
`POST /Playlists` using the same `JELLYFIN_URL`/`JELLYFIN_API_KEY` the ingest
daemon uses. It is **idempotent**: an existing playlist whose ordered item set
already matches the manifest is left untouched; a missing or drifted one is
deleted and recreated so the manifest always wins. A playlist is owned by
`JELLYFIN_USER_ID` (config; empty ⇒ the first/only user, since Infuse only sees
the playlists of the user it logs in as).

It is wired into `scripts/nightly_metadata.sh` (step 3b, after the Jellyfin
refresh so items are scraped, before the backup so manifests are captured). That
makes playlists **self-healing** — the scan-deletion bug or a wiped data dir is
repaired on the next nightly run. Non-fatal like the rest of that script.

### Manifest schema (`state/playlists/<slug>.json`)

```json
{
  "name": "Gintama - Watchable",             // the name shown in Infuse
  "description": "one-line note",             // optional
  "show": "Shows/Gintama (2006)",             // library-relative show folder
  "items": [                                   // ORDERED watch list
    {"ep": 18},                                // Season 01 episode 18
    {"ep": [43, 44]},                          // inclusive range 43..44
    {"ep": 90, "season": 2},                   // episode of another season (default 1)
    {"special": 1},                            // Season 00 special (int or [lo,hi])
    {"movie": "Gintama - The Movie (2010)"},   // a film in Movies/ by filename stem
    {"path": "Shows/X/Season 01/Y.mkv"}        // escape hatch: raw library-relative path
  ]
}
```

A merged multi-episode file (`S01E01-E02`) satisfies every number in its span;
duplicate resolutions collapse to one entry, preserving order. `--dry-run`
resolves and prints the full order while touching Jellyfin not at all — always
run it first to confirm every token lands on a real file.

### Adding a new show

1. Curate the watch order (see the standing decisions below), then write
   `state/playlists/<slug>.json` in the schema above.
2. `python3 playlist.py --dry-run` — confirm every token resolves and the film
   slots are right.
3. `python3 playlist.py` — build it (needs `JELLYFIN_URL`/`JELLYFIN_API_KEY` in
   the env; the nightly plist already sets them). Re-runs are idempotent.

### Standing curation decisions (apply to every show unless overridden)

- **Default cut depth = "cut the dead time" (description-driven).** The single
  shared philosophy lives in `playlist_curation.CUT_PHILOSOPHY` and is injected
  into *both* curation entry points (the from-scratch autobuild and the
  per-episode judge). The rule: **judge every episode from its own on-disk
  description** — the sidecar `.nfo` `<plot>` next to the video, which is the
  ground truth for whether anything actually *happens*. **Keep** an episode that
  advances the story or lands a real beat (a fight's actual turn/reveal/finish, a
  death, an emotional payoff, an arc opener/closer, a character/premise intro, a
  genuinely memorable or funny standalone). **Cut** the *dead time*: unresolved
  power-up / "five minutes to explode" countdown stalls, staring-contest
  stand-offs, crowd/bystander reaction-and-anticipation padding, recap/flashback
  of already-shown events, slow-closing-hazard tension crawls (the Dressrosa
  birdcage), and throwaway filler. Cutting *canon* is fine when the canon itself
  is padding (the manga / re-cut fills the gap), but never cut the cast/premise
  introductions. This deliberately supersedes a **"purely-enjoyable +
  keep-only-first-and-last-of-every-fight" rule**, which was content-blind and cut
  too hard — a multi-episode fight now keeps the episodes whose *descriptions*
  show a turn or a finish (that may be one of five or four of five), decided from
  the plot text, never a flat structural rule. Never delete files — curation is a
  playlist, the library stays whole.
- **Per-show keep band (a self-check, not a quota).** Every show has a target
  keep-rate in `playlist_curation.SHOW_CURATION`, set by how filler-/padding-heavy
  it is, and injected into the prompt so a run can sanity-check its own cut. The
  bands are tuned for a **fast-paced** watch: One Piece **35–45%**, Dragon Ball
  **40–50%**, Naruto/Shippuden/Boruto (Hidden Leaf) **45–55%**, Bleach **45–55%**,
  Sailor Moon **45–55%**, Fairy Tail **45–55%**, Gintama **45–55%** (arc-forward —
  keep every serious arc + cast intros + only the *elite* gags; the leisurely
  one-offs drag), Steven Universe **55–65%**, Adventure Time **55–65%**, Regular
  Show **60–70%**. Nothing enforces the band; it only tells a run when it has cut
  far too much or too little and should reconsider.
- **Real few-shot examples per show.** Each show in `SHOW_CURATION` carries a
  concrete `keep` and `remove` example — a real on-disk episode with its real
  description and the reason it stays or goes (e.g. DBZ *"A Final Attack"*, the
  Namek five-minute stall → REMOVE, vs *"Mighty Blast of Rage"*, the Kamehameha
  payoff → KEEP). Concrete pairs steer a judge far harder than abstract rules, and
  they teach the *keep the beat, cut the stall* distinction within a single arc.
- **Film vs equivalent TV:** when an arc exists as both a film and TV episodes,
  include only the **recommended/better** version, never both. This mainly bites
  **Dragon Ball Super** (the *Battle of Gods* / *Resurrection F* films vs the DBS
  arcs that re-adapt them). For Gintama it applied once (the 2010 Benizakura film
  replaces TV E58–61).
- **One Piece is already handled by One Pace** (`Shows/One Pace (2013)/`) — point
  its playlist there in arc order; do not hand-curate the 1999 anime.
- **Never skip the character/premise introductions.** A "watchable" cut is still
  a *watch* — it must establish the cast and the world before the arcs. Opening on
  a mid-series episode that assumes you already know everyone is a curation bug,
  not a lean cut. Always include the early episodes that introduce the core cast
  and the major recurring characters (for Gintama: absolute 1, 2, 3, 5, 6, 7, 8,
  11, 13, 15 — the Yorozuya trio + Sadaharu, the Shinsengumi, Katsura, Hasegawa,
  Elizabeth, and Takasugi's antagonist seed), *then* jump to the first real arc.
  This bites every long show (Naruto, Bleach, One Piece) the same way.
- **Watch out for the pilot / numbering offset.** Many guides count a show's pilot
  or a recap as episode 1, so their numbers run ahead of the library's. Gintama is
  the worked case: fan guides count the 2005 Jump Festa pilot as eps 1–2, a **+2
  offset** vs the pilot-excluded disk (disk `S01E01-E02` = "Gintama 001-002",
  `S01E202` container tag = "S02E01" = Gintama′ ep 1, both confirming pilot
  exclusion). Map by **episode title / container tag**, not by a guide's raw
  number, and confirm against the actual files before committing — a +2 slip
  silently sends every pick two episodes off.
- **Always ground the curation in disk truth and cross-checked guides**, and
  present the full proposed list for approval before building. Episode numbers are
  load-bearing: a wrong number sends you to the wrong episode. Anchor arc ranges
  against multiple sources and validate against any on-disk `.nfo`/container tags.

### Auto-maintained playlists (`playlist_watch.py`) — a playlist that grows itself

Some curated playlists can never be "finished": **One Piece** airs weekly forever
and this pipeline grabs each new episode, so its "watchable" cut has to keep
extending. `config.PLAYLIST_AUTO_SHOWS` maps such a show folder to its manifest
slug (currently `Shows/One Piece (1999)` → `one-piece-watchable`). After **every
successful ingest**, `ingest._advance_cleanup` calls
`playlist_watch.consider_new_episodes(...)` with the just-placed files. For each
newly-placed main-series episode of an auto-show, a **headless AI judge** (same
`ai_runner.py -p --output-format json` pattern as identify, writing a verdict JSON to a
known path) applies the **shared `playlist_curation.CUT_PHILOSOPHY`** — it reads
the episode's own sidecar `.nfo` `<plot>` first and decides *keep the beat / cut
the dead time*, calibrated by the same per-show keep band and KEEP/REMOVE examples
the from-scratch curator uses — and the keepers are appended to the manifest (as
`{"path": ...}` items, in ingest order) and the playlist is rebuilt in Jellyfin.

Same **model-proposes / harness-disposes** split as the rest of the pipeline: the
judge returns only a `{"keep": bool, "reason": str}` verdict per exact episode
path — it never moves, deletes, or reorders. The verdict is persisted as a
manifest entry (backed up to MEGA with `state/`), so the curation survives a
Jellyfin wipe. Everything is best-effort: any judge/Jellyfin failure is logged and
swallowed, and an episode whose judge failed is simply left unrecorded so a later
run retries it — it **never breaks an ingest**.

**Bulk guard.** If one ingest places more than `config.PLAYLIST_AUTO_MAX_INLINE`
(12) new episodes of an auto-show — a complete-series pack, not a weekly drop —
inline judging is skipped (judging hundreds synchronously would stall the daemon)
and a log line points you to the manual backfill:

```text
python3 playlist_watch.py --show "One Piece (1999)"            # judge every not-yet-considered episode
python3 playlist_watch.py --show "One Piece (1999)" --dry-run  # judge + report, write nothing
```

The judge is idempotent per episode (an episode already recorded in the manifest is
never re-judged), so the daemon and a manual backfill compose cleanly. The historical
One Piece back-catalogue is seeded by the curation batch (mixed `{"ep": N}` tokens);
new episodes accrue as `{"path": ...}` entries from the judge.

### Session-independent backfill (`scripts/playlist_autobuild.py`)

Curating a new show needs a headless AI research run, which fails while the
org is over its monthly API spend limit. `scripts/playlist_autobuild.py` — run by
launchd (`com.mikeyferguson.playlistautobuild`, every 3h) — makes that wait
**hands-off and fully unattended**: each cycle it probes
whether the API will answer (a tiny one-turn ping); if it still refuses it no-ops
and retries later; when budget returns it does **one** pending task — a headless
run researches the show and writes its manifest (the model proposes), then the
deterministic builder validates + pushes it to Jellyfin (harness disposes). One
task per cycle bounds spend and lets the budget recover between builds. It marks a
task done only if the manifest resolves to real files (else it retries), and
no-ops once every pending show is built. The pending set lives in
`CURATION_TASKS` (The Hidden Leaf — Naruto → Shippuden → Boruto as one combined
`hidden-leaf-watchable` playlist — plus Fairy Tail, Steven Universe, Sailor Moon,
One Piece, Dragon Ball, Bleach, Adventure Time, **Regular Show**, and Gintama).
Each task pulls its keep band and few-shot examples from
`playlist_curation.SHOW_CURATION` and the shared cut philosophy from
`playlist_curation.CUT_PHILOSOPHY`. Clearing `state/playlist_autobuild_done.json`
(or removing specific slugs from it) re-curates — that is how a philosophy change
is rolled out to every show. This is the "leave and it finishes on its own after
the limit resets" path.

**A curated cut can only drop whole episodes**, so it can't remove the *in-episode*
padding that One Pace / DBZ Kai re-edit away — the aggressive One Piece and DBZ
playlists hit a floor around their canon-episode count. For true leanness those
re-cut versions (already in the library) remain the answer.

### Universal playlist curator (`scripts/playlist_curator.py`) — de-hardcoded

`playlist_autobuild.py` above answered *"build the playlists on this fixed list."*
The list was the limit: drop a filler-heavy show into the library and it silently
never got a playlist until someone hand-edited `CURATION_TASKS`.
`scripts/playlist_curator.py` (launchd `com.mikeyferguson.playlistcurator`, every
3h; it **replaces** `playlistautobuild` in the fleet) removes the hard-coding by
splitting the problem in two:

Playlist curation is *non-ingestion* DeepSeek work, so both the curator and
`playlist_autobuild.py` (and `playlist_watch.py`'s inline episode judge) run only inside
DeepSeek's off-peak **billing** window (`config.is_off_peak()`). The identify step keeps
the critical path; playlist judgment/build is deferred to the cheap hours, and a new
auto-show episode that lands outside the window is picked up by the next cheap sweep instead.

* **DECIDE** — it walks **every** show folder and asks a headless AI run the one
  judgment the whole cut philosophy already turns on: *does this show carry enough
  DEAD TIME (filler arcs, padded canon, long stall/recap stretches) that a lean
  "watchable" cut is worth having, or is it already tight enough to watch straight
  through?* Toriko / One Piece / Bleach / the Dragon Balls → yes; Hunter × Hunter
  (2011) / Death Note / Monster / Cowboy Bebop / Frieren → no. One cheap AI run
  judges all still-undecided shows at once (it also groups franchise siblings into
  one combined playlist), and each verdict is persisted to
  `state/playlist_decisions.json`. Re-runnable; clearing that file re-judges the
  whole library after a philosophy change.
* **BUILD** — **every** decision marked *yes* that has no playlist yet is built
  **this cycle** (`build_all`), not one-at-a-time — deciding which shows warrant a
  playlist is a once-per-cycle scan, but actually building the ones we already know
  we want just goes to town. Each uses the **same validated builder**
  `playlist_autobuild` already provides (the model proposes a manifest to a temp path;
  only a manifest that resolves to real files is promoted and pushed — a bad run
  can never clobber a good playlist). It re-probes the API budget between builds, so
  a mid-run spend-limit just defers the rest to the next cycle. It **seeds** from
  the legacy `CURATION_TASKS` first, so the ten existing hand-tuned combined
  playlists (Hidden Leaf, the five Dragon Balls, …) are preserved verbatim and their
  member shows count as already-decided.
* **PROWL** — each cycle it also extends the **ongoing** playlists: `extend_ongoing`
  judges only episodes that landed *after* the last consideration (a per-decision
  `_considered` ledger, seeded to the whole disk set at build time) and appends the
  keepers — so a weekly drop grows its cut even if the ingest-time hook missed it,
  and an already-cut episode is **never** re-judged.

It also keeps the auto-extend set in sync: every **ongoing** show that got a
playlist is written to `state/playlist_auto_shows.json`, which `playlist_watch.py`
now merges with `config.PLAYLIST_AUTO_SHOWS` — so a newly-curated weekly show
auto-extends as new episodes ingest with **no code edit**. Inspect the ledger any
time with `python3 scripts/playlist_curator.py --status`.

### Film collections (`--movies-only`) — ordering, not cutting

The curator asks a **second, different question** of `Movies/`. Forcing films through
the filler lens would be meaningless (a film has no filler episodes to cut), so movies
get their own judgment:

> Do these films form a saga / franchise / thematic set whose **watch order** is not
> what the folder gives you, so an ordered playlist is genuinely useful?

`Movies/` is a **flat** folder, so Jellyfin lists it alphabetically — and that is the
problem it solves. "Avengers: Endgame" sorts above "Avengers: Infinity War"; the Star
Wars films scatter across three naming eras; a prequel or interquel (Rogue One, the
LOTR/Hobbit chronology inverting release order) has nowhere sensible to sit in an A–Z
list at all. The fix is an **ordered playlist**, and unlike the show side it is purely
**additive** — nothing is cut, the films are just put in the order you would watch them.

* **DECIDE** — one AI run groups still-undecided films into collections, keyed in
  the same `state/playlist_decisions.json` under `Movies/<flagship stem>` with
  `kind: "movies"`. A lone film is **never** a playlist (enforced in code, not just in
  the prompt — a single-film "collection" is rejected whatever the verdict claims), and
  the bar is deliberately high: sharing a studio, genre, or actor is not a collection.
* **BUILD** — its own curation runner (`_run_movie_curation`) with the same
  proposes / harness-disposes discipline: the candidate manifest is promoted only
  if it resolves cleanly to ≥2 real files, so a bad run can never clobber a good
  playlist. Manifests use the `{"movie": "<stem>"}` tokens `playlist.py` already
  supports, and may interleave `{"path": "Shows/…"}` episodes where a film series has to
  sit inside its show's run.
* **NEW FILMS SLOT IN AUTOMATICALLY** — no separate prowl is needed. Because the ledger
  is keyed by what is on disk, a newly-acquired film is *automatically undecided*, so the
  next cycle judges it; if it belongs to a collection that already exists, the verdict
  carries that collection's `existing_slug` plus an `after_movie`, and
  `playlist.insert_movie_item` slots it in at its **watch position** (not appended —
  a prequel belongs in the middle) and re-pushes the playlist.

```bash
python3 scripts/playlist_curator.py --movies-only   # judge/build film collections only
python3 scripts/playlist_curator.py --shows-only     # judge/build shows only
python3 scripts/playlist_curator.py --status         # both ledgers
```

> **A latent bug this surfaced and fixed.** `playlist._movie_index()` scanned
> `config.MOVIES_ROOT` — the SSD **lower** — while everything else in `playlist.py`
> resolves against the **mediafs mount**. Under the virtual library a film's payload is
> *evicted* from the lower once it is in the MEGA pool, leaving only its sidecars, so the
> lower showed **1** film where the mount shows **292**. Every `{"movie": ...}` token was
> therefore unresolvable, and because a manifest is promoted only when it resolves
> *cleanly*, that silently blocked any playlist that slots a film in — which is why no
> existing manifest contains a single movie token despite several briefs instructing the
> curator to include films. `_movie_index` and the curator's film inventory now both read
> the mount (`playlist.MOVIES_DIR`).

### Permissions

Nothing here needs a permission grant any more: the curation runs go through
`ai_runner.py`, which has no permission system to prompt from — its safety comes from
the tool set it is given (§ Security posture), not from an allow-list.

### The Gintama reference build

`state/playlists/gintama-watchable.json` is the first, worked example: ~170 items
(115 from the serious arcs, ~50 beloved gags, 2 Semi-Final specials, 3 films),
Balanced depth, films slotted in watch order, ending TV Silver Soul → *The
Semi-Final* → *The Very Final* (the true ending the TV run never reaches). Gintama
sits as one continuous **absolute** `Season 01` of 367 episodes (E01–E02 is one
merged file), which is why the manifest addresses episodes by absolute number.

---

## Security posture (be honest about this)

The trust boundary is **"the agent is not capable of it,"** not "the agent is
well-behaved." That distinction was bought the hard way (§ Identification — there is no
shell tool), and it is the property to preserve when changing anything here:

- **No shell.** The agent's tools are `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Probe`,
  `ListDir`, `Jellyfin`, `WebSearch`, `WebFetch`. Each has a fixed argument vector and
  none interprets a shell string. Adding a shell tool re-opens the whole class.
- **Media is unwritable.** `Write`/`Edit` refuse a media path under the library root or
  the mediafs mount, checked on the *resolved* path. Sidecars stay writable.
- **The harness does every irreversible thing.** The run only ever proposes a plan;
  `library.validate_plan` re-derives and confines every destination, and `apply_plan` /
  `verify_applied` do the moving and the deleting. A hallucinated path fails validation
  rather than landing somewhere.
- **Credentials stay out of the transcript.** The DeepSeek key is read from disk, not
  passed through the environment. The Jellyfin key is injected by the `Jellyfin` tool
  rather than interpolated into the prompt, so it never reaches the provider and cannot
  be quoted back into a log.
- The run still executes as the user, and `Write` outside the library is unrestricted —
  it has to be, since that is how a plan file gets written. A run that decided to write
  junk into the home directory could. That is the residual exposure.
- The WebUI runs with no auth, but only on loopback, on a single-user machine.

**Content now leaves the machine that did not before.** Filenames, the library digest,
and `.nfo` contents are sent to DeepSeek's API — a third-party provider — on every run.
That is a real change from the previous provider, not a like-for-like swap, and it is the
thing to revisit if the threat model changes.

---

## Requirements

- macOS with `launchd`; the the SSD library root drive present (`/Volumes/the SSD library root/MediaStore`) and the `mediafs` library mount at `~/MediaLibrary`.
- **qBittorrent** v5+. `startup.sh` configures its WebUI (see above).
- **Tailscale** running (the Mac app / IPNExtension) — the download VPN gate. The
  daemon detects it via the interface address, not the CLI, so the Homebrew
  `tailscale` binary is not required for the gate to work.
- **A DeepSeek API key** at `~/.config/api-keys/deepseek_key` (or `DEEPSEEK_API_KEY`
  in the environment) — the identify brain. If it is missing or the account has no
  balance, identify runs exit 2 and the work is **deferred untouched**, not failed: the
  torrent stays at `DOWNLOADED` and the next pass retries it. Nothing is mis-ingested and
  nothing is quarantined for a failure that was never about the content.
- **conda** (miniconda) — `startup.sh` builds env `torrent_ingest_env` (Python
  3.11) with `qbittorrent-api`, `guessit`, `requests`.
- **rclone** (`/opt/homebrew/bin/rclone`) with a MEGA-authed config — only for
  the nightly metadata backup (§ Metadata backup). It reuses the fleet's existing
  MEGA credentials (Media-Syncer's `rclone.conf`); if that repo isn't present the
  backup step just logs and skips, harmless to the rest of the pipeline.

---

## Install

```bash
bash startup.sh
```

Builds the conda env, enables the qBittorrent WebUI (quitting/reconfiguring/
relaunching qBittorrent), and installs and loads **all** of this repo's launch
agents (`RunAtLoad` + `KeepAlive` on the long-running ones; each daemon runs its own
loop, so `KeepAlive` only restarts it if it dies).

The agent set is the explicit `AGENTS` array at the top of the install section of
`startup.sh` — **it is the source of truth, so if you add a plist to this repo, add its
label there.** Otherwise you get an agent that exists in the repo, is described in this
README, and is never actually loaded — documented as "run by launchd every 3h" while
absent from the machine entirely. Load order matters in one place —
`librarysupervisor` is last, because it is the sole launcher of Jellyfin/YacReader and
gates them on the mediafs mount being ready.

It uses `bootout`/`bootstrap`, not the deprecated `load`/`unload` pair, which on current
macOS can report success while doing nothing — the same failure mode that hides an
uninstalled agent. It deliberately does **not** call `launchctl start`: `RunAtLoad`
already starts each job, and forcing `start` would additionally fire the
interval/calendar jobs (`torrentmetadata`, `playlistautobuild`) off-schedule.

### The launchers pull this repo, and that pull must be serialized

Every `run_*.sh` launcher pulls before exec'ing its daemon, so a config edit made on
another machine lands without a manual sync. Six of them do it — `run_torrent_ingest.sh`,
`run_torrent_reap.sh`, `run_db_guardian.sh`, `run_direct_ingest.sh`, `run_drive_ingest.sh`,
`run_library_supervisor.sh` — against **one shared working tree**, and `startup.sh`
bootstraps them about a second apart. Run raw, those pulls collide: each appends a
mergeable line to the shared `.git/FETCH_HEAD`, so the next launcher to read it sees two
candidates for `main` and dies with

    fatal: Cannot fast-forward to multiple branches.

The launcher then shrugs (`git pull failed; using local files`) and starts the daemon **on
stale code**. This is a silent failure by construction: the agent still comes up with a
PID and exit 0, so `launchctl list` looks perfectly healthy while the daemon runs whatever
the tree happened to hold. It can go unnoticed for weeks, and an interleave can get far
enough that git demands a `git reset --hard` to recover the working tree — the launcher
logs under `~/Library/Logs/` are where that shows up.

The pull now goes through `git_pull_locked` in `scripts/git_pull_locked.sh`, which
serializes it behind an atomic `mkdir` lock on `.git/pull.lock` — macOS ships no
`flock(1)`. A lock orphaned by a launcher killed mid-pull is reclaimed after 5 minutes; a
launcher that waits 120 s gives up and starts on local files rather than hang forever. The
lock is released explicitly rather than by an `EXIT` trap, because every launcher ends in
`exec`, which replaces the shell before any trap could fire.

**If you add a launcher that pulls, call `git_pull_locked` — never `git pull` directly.**
Verified against a scratch clone: six concurrent raw pulls fail 6 of 6 with
exactly the error above; the same six through the lock fail 0 of 6, fast-forward
correctly, release the lock, and leave exactly one mergeable line in `FETCH_HEAD`.

**The remote must be SSH, not HTTPS.** The launchers run headless under launchd, so git has
no TTY to prompt on: with an HTTPS origin and no credential helper, every pull dies with
`fatal: could not read Username for 'https://github.com': Device not configured` and the
launcher starts the daemon on stale code. Use an SSH remote —
`git@github.com:Pirate-Hunter-Zoro/Torrent-Ingest.git` — backed by `~/.ssh/id_ed25519`, which
authenticates non-interactively. If a clone ever arrives on HTTPS, fix it with
`git remote set-url origin git@github.com:Pirate-Hunter-Zoro/Torrent-Ingest.git`.

**One manual Jellyfin step, once:** in each library's settings, ensure the **Nfo**
metadata saver is enabled, and never run **"Replace all metadata"** on a locked
show (the safe scan modes and the nightly task respect `<lockdata>`; only that one
mode steamrolls it). This is what makes owned-show metadata stick — and the Nfo
saver is also what lets the metadata audit (§ Metadata integrity) read what
Jellyfin holds straight off disk.

**The metadata safety net loads itself now.** The nightly audit+repair
(§ Metadata integrity) is `com.mikeyferguson.torrentmetadata`, which is in `startup.sh`'s
`AGENTS` list — no separate manual step. (Do not document it as an optional
hand-load via `launchctl load`; that is what the `AGENTS` list replaced.)

It needs the same Full Disk Access grant as the daemon (it runs under the same
conda `python3.11` and spawns `ai_runner.py`), so if the ingest daemon is already
granted, this is covered. Run `python3 scripts/audit_metadata.py` any time for an
on-demand blank-metadata report.

**One manual macOS permission step, once:** the daemon touches TCC-protected
locations — the iCloud watch folder, `~/Downloads` (where qBittorrent writes), and
the the SSD library root volume — and the identify agent reads/writes files too.
macOS grants file access per *binary identity*, and a launchd daemon that hits a
protected folder without permission is **silently denied** (no dialog appears
in the background; the torrent just goes `FAILED`). Grant **Full Disk Access**
(System Settings → Privacy & Security → Full Disk Access → **+**, then ⌘⇧G to type
the path) to **one** binary — it covers every folder category at once:

- the daemon's interpreter, `…/miniconda/base/envs/torrent_ingest_env/bin/python3.11`
  (the resolved real binary, not the `python3` symlink).

One binary, not two, and that is a direct consequence of `config.AI_BIN` starting with
`sys.executable`: the agent runs under the *same* interpreter as the daemon that spawned
it, so it inherits the same TCC identity. There is no second, separately-versioned
executable whose grant silently lapses on every upgrade.

The remaining gotcha: the daemon reads its TCC grants only at launch, so **bounce it**
(unload/load the launch agent) after granting.

## Optional: Jellyfin auto-rescan

Set `JELLYFIN_URL` and `JELLYFIN_API_KEY` (e.g. in the plist's
`EnvironmentVariables`) to trigger a targeted library scan the instant an ingest
completes, so "on disk" and "watchable" are seconds apart instead of waiting for
the nightly scan. Left unset, ingests still land; Jellyfin just picks them up on
its own schedule. This nudge fires for video ingests only — comics are served by
YACReader, which scans its own folders, so a `comic` ingest never touches
Jellyfin.

## Usage

Drop a `.torrent` into:

```bash
~/Library/Mobile Documents/com~apple~CloudDocs/Torrents/
```

Then wait. For a normal-sized cour: drop it in the morning, watch it that evening
(download time dominates; local playback does not wait on the MEGA upload).

When it finishes, the `.torrent` is moved into `Torrents/finished/` — that folder
is your record of what's been ingested, and it is safe to leave alone. **To
redownload** anything (a botched ingest, a re-grab, a re-check against the
library), just drop its `.torrent` back into `Torrents/` — from `finished/` or
from anywhere. It ingests again from scratch; if the media is already in the
library it simply verifies that, deletes the local copy, and re-files the
`.torrent`, touching nothing.

## Stop / uninstall

```bash
bash cancel_ingest.sh          # stop the daemon (leaves the agent installed)
launchctl unload ~/Library/LaunchAgents/com.mikeyferguson.torrentingest.plist
rm ~/Library/LaunchAgents/com.mikeyferguson.torrentingest.plist
```

---

## Config knobs (`config.py`)

| Knob | Meaning |
| --- | --- |
| `TORRENTS_DIR` | iCloud watch folder for `.torrent` files. |
| `FINISHED_DIR` | Subfolder of the watch folder (`finished/`) where fully-ingested `.torrent` files are filed instead of deleted; not scanned for new work, but re-droppable. |
| `FAILED_DIR` | Subfolder of the watch folder (`failed/`) where a FAILED torrent's source `.torrent` is filed, so a dead torrent leaves the watch folder instead of looking queued; not scanned for new work, re-droppable. Also takes `.torrent` files that will not parse at all and carry no recoverable hash (§ A truncated `.torrent`: recovered as a magnet, or filed to `failed/`). |
| `UNPARSEABLE_GRACE_SEC` | How long a `.torrent` whose bencode will not parse must sit untouched (120 s) before it is filed to `failed/` as truncated. Covers the window where iCloud has materialized only part of a drop. |
| `DOWNLOADS_DIR` / `INCOMING_DIR` | Local download volume; the dedicated dot-subdir torrents land in. |
| `LIBRARY_INCOMING_DIR` | **Vestigial.** Was the overflow download dir on the library drive for oversized torrents; the torrent client now never writes to the SSD library root (oversized torrents are refused — § Torrents too large for the SSD). Kept only so cleanup can sweep any legacy leftovers. |
| `MEDIA_ROOT` / `SHOWS_ROOT` / `MOVIES_ROOT` | The the SSD library root video library (Jellyfin). |
| `ONE_PACE_PREFIX` | The one library path (`Shows/One Pace (2013)/`) where a repeat drop **overwrites** the existing file instead of skipping it, because a One Pace re-release is a better re-cut or an **extended cut** that replaces the version on disk in the same episode slot (§ The One Pace exception). This is also the always-**owned** show whose per-episode metadata the pipeline authors itself, by arc — never by anime-episode number. Mirrors Media-Syncer's churn-class prefix — keep the two strings in step. |
| `COMICS_ROOT` | The comics/manga library (YACReader); `Manga/` subtree for manga. |
| `COMIC_EXTENSIONS` | Archive types treated as comics (`.cbz`, `.cbr`, …). |
| `COMIC_CONVERT_EXTENSIONS` | Plain archive types (`.zip`) accepted as comics and filed as `.cbz` (rename-on-apply, no repackaging). |
| `LOOSE_PAGE_EXTENSIONS` | Bare image suffixes (`.jpg`/`.jpeg`/`.png`/`.gif`/`.webp`/`.bmp`) that count as page images when a loose-page folder is packaged into a `.cbz` (§ Loose page images are packaged). Anything else in such a folder (`Thumbs.db`, release `.txt`/`.nfo`, `.DS_Store`) is left out of the archive. |
| `DIRECT_INGEST_DIR` / `ICLOUD_DIRECT_INGEST_DIR` | Direct ingest's local watch folder (`~/Downloads/DirectIngest/`) and its iCloud drop mirror (`Torrents/DirectIngest/`, drained into the former by `direct_ingest_bridge.py`). |
| `DIRECT_INGEST_EXTENSIONS` | What the direct-ingest daemon picks up: `VIDEO_EXTENSIONS \| COMIC_EXTENSIONS \| COMIC_CONVERT_EXTENSIONS \| NOVEL_EXTENSIONS`. A dropped DIRECTORY holding any of these (or loose page images) is one identify run, like a torrent's download dir. |
| `STAGING_DIRNAME` | Dot-dir under `MEDIA_ROOT` for atomic assembly (`.ingest-staging`). |
| `QBT_HOST` / `QBT_PORT` | WebUI endpoint (`127.0.0.1:8090`). |
| `QBT_CATEGORY` | Tag isolating our torrents from the user's. |
| `MIN_FREE_BYTES` | Floor of free space always kept on the local volume (20 GB). |
| `LIBRARY_MIN_FREE_BYTES` | **Vestigial.** Was the free-space floor on the library drive for admitting overflow downloads; unused now that torrents never download onto the SSD. Admission uses the SSD's `MIN_FREE_BYTES` only. |
| `SPACE_SAFETY_FACTOR` | Multiplier on torrent size for the fit check (1.15). |
| `TAILSCALE_BIN` / `BRCTL_BIN` | Reference to the Tailscale CLI (gate uses an interface check, not this); iCloud materializer. |
| `AI_BIN` / `AI_MODEL` / `IDENTIFY_PROMPT_FILE` | Identify step: the agent runner (interpreter + `ai_runner.py`, spawned as `[*AI_BIN, ...]`), the DeepSeek model override, and the engineered base prompt. |
| `IDENTIFY_TIMEOUT_BASE_SEC` / `IDENTIFY_TIMEOUT_PER_MEDIA_FILE_SEC` / `IDENTIFY_TIMEOUT_MAX_SEC` | Identify timeout scales with media-file count: `base + per_file × count`, capped at max (replaces the old flat `IDENTIFY_TIMEOUT_SEC`, kept as the base alias). |
| `IDENTIFY_MAX_LISTING_FILES` | Cap on how many media files the identify prompt lists inline; beyond it, a capped sample + summary keeps a huge pack from bloating the prompt. |
| `DUPLICATE_DEPRIORITIZE_MARKERS` | Path substrings (`uncropped`, `upscale`, `480p`, …) that mark a *lower-preference* copy when a torrent ships the same episode more than once (§ Duplicate variants collapse, not crash). When several sources map to one destination, `validate_plan` keeps the copy with no marker, tie-broken by largest file, and drops the rest — so an in-torrent duplicate collapses to the best copy instead of failing the whole plan. |
| `CLAUDE_MODEL` | Optional model override for identify (env `TORRENT_INGEST_CLAUDE_MODEL`). |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` | Optional post-ingest rescan + the poster backstop's image lookups (env). |
| `RCLONE_BIN` / `RCLONE_CONFIG` | rclone binary and config for the metadata backup; config defaults to the machine-local `~/.config/rclone/rclone.conf` (seeded from Media-Syncer's committed conf if absent). |
| `MEDIA_SYNCER_RCLONE_CONF` | Fallback credential source copied into `RCLONE_CONFIG` when the machine-local one is missing (never used in place — keeps session tokens out of the git-tracked file). |
| `METADATA_BACKUP_REMOTE` / `METADATA_BACKUP_BASE` | MEGA pool account + top-level path for the metadata backup (`vm_mega1:metadata-backup`; override the remote via `TORRENT_INGEST_BACKUP_REMOTE`). |
| `METADATA_BACKUP_FILTERS` | rclone filter rules selecting which sidecars to back up (keep `.nfo` + artwork, drop regenerable `-thumb.jpg`/`.trickplay`). |
| `MEDIA_SYNCER_DIR` / `MEDIA_SYNCER_INVENTORY` / `MEDIA_SYNCER_SYNC_STATE` / `MEDIA_SYNCER_APP_LOG` / `MEDIA_SYNCER_LAUNCHD_LOGS` | Reaper (§ Remote deletion): where Media-Syncer lives + the state files it reads for remote discovery and prunes so a purge sticks. |
| `MEDIA_SYNCER_LABEL` / `MEDIA_SYNCER_PROC_PATTERN` / `LAUNCHCTL_BIN` | Reaper: how it pauses (`launchctl kill`) / resumes (`launchctl kickstart`) Media-Syncer and confirms it is really down before purging. |
| `REAP_TRACKED_EXTENSIONS` / `REAP_VIDEO_EXTENSIONS` | Reaper: the extensions it monitors (mirror Media-Syncer's replicated set — only these have a remote copy to purge) and the subset that also gets Jellyfin sidecar cleanup. `.m4v` is included in both: the library holds 89 `.m4v` files (Justice League Unlimited S01, The Dark Knight Rises, …) that a previous drift left invisible to the whole fleet — never uploaded, never reaped, never metadata-purged. Keep this set in step with Media-Syncer's `VIDEO_EXTENSIONS | COMICS_EXTENSIONS` and the searcher's `VIDEO_EXTENSIONS`. |
| `REAP_SCAN_INTERVAL_SEC` / `REAP_DEBOUNCE_SCANS` | Reaper: scan cadence and how many consecutive healthy scans a file must be missing before it is eligible to purge (debounce). |
| `REAP_MAX_MISSING_FRACTION` | Reaper **circuit breaker**: abort the whole purge (drive-scale-loss signal) if more than this fraction (30%) of the tracked library vanished in one shot. The sole breaker guard — the old absolute file-count / title-span caps were removed as they only punished big legitimate deletes below this fraction. A deliberate delete that trips it is released with `reap.py --approve`. |
| `REAP_SETTLE_MAX_HOLD_SEC` | Reaper **settle-gate**: hard cap (30 min) on how long Media-Syncer is held paused waiting for deletions to settle before it is resumed anyway, so an unpurgeable survivor can't strand it offline forever. |
| `REAP_PROBE_WORKERS` | Reaper: concurrency for the fleet probe (cheap per-account `lsf` calls). |
| `POLL_INTERVAL_SEC` / `IDLE_INTERVAL_SEC` | Loop cadence while active / idle. |
| `REGISTER_REFRESH_SEC` | How often (20 s) a fresh top-level `.torrent` is re-queued into `queued/` *mid-sweep*, so a drop is never left sitting in the base folder for the duration of a long chunked-backlog cycle. |
| `DEEPSEEK_PEAK_UTC` | DeepSeek's peak **billing** hours (01:00–04:00 and 06:00–10:00 UTC); `config.is_off_peak()` returns true outside them, at half price. The fleet's **non-ingestion** DeepSeek work (media_doctor escalation, playlist curator/autobuild, the searcher's discovery/completeness audits) defers to that cheap window. The identify step (file renaming/placement) runs any hour. Note this is a *billing* window, not the old "local night": DeepSeek's off-peak is most of the US daytime, so auxiliary work now runs then instead of 01:00–07:00 local. |

## Files

| Path | Purpose |
| --- | --- |
| `ingest.py` | The daemon: state machine, parallel disk-budget scheduler, cleanup, loop. |
| `qbt.py` | qBittorrent Web API wrapper + local v1 info-hash computation. |
| `identify.py` | Invokes the headless AI run; returns a validated plan + rationale. |
| `ai_client.py` | **The fleet's AI runtime.** DeepSeek tool-calling agent loop, the tool set (`Read`/`Write`/`Edit`/`Glob`/`Grep`/`Probe`/`ListDir`/`Jellyfin`/`WebSearch`/`WebFetch`), the media-write guard, and context trimming. Stdlib-only HTTP, so it imports under every one of the fleet's conda envs. |
| `ai_runner.py` | The CLI every daemon spawns (`config.AI_BIN`). Prompt on stdin, JSON envelope on stdout, exit 2 for "the run never happened". |
| `prompts/identify.md` | The engineered identification prompt (prime directive lives here). |
| `library.py` | Library digest (numbering/ownership/blank-metadata summary, cached), plan validation, atomic apply, verify, `.nfo` generation + repair helpers. |
| `direct_ingest.py` | Loose-file ingester for ALL media (§ Direct ingest): files what the owner drops into `~/Downloads/DirectIngest/` — files OR folders — through `identify → validate → apply → verify`. Video lands in `Shows/`/`Movies/`, comics under `Comics/`, novels under the Google Drive `Novels/`. |
| `direct_ingest_bridge.py` | iCloud bridge daemon (§ Direct ingest): materializes and MOVES raw-media drops out of `Torrents/DirectIngest/` into `~/Downloads/DirectIngest/`; copy → verify → rename → delete, collision-safe, dry-run capable. |
| `journal.py` | Append-only state journal + human-readable decisions log. |
| `reap.py` | The remote-deletion **reaper** daemon (§ Remote deletion): detects a locally-vanished file, runs the circuit-breaker-guarded purge across the MEGA fleet + metadata backup, controls Media-Syncer. |
| `mega.py` | Self-contained rclone/MEGA ops for the reaper: fleet enumeration, existence probe, delete/rmdir/cleanup/purge, dead-session detection + healing. |
| `yacreader_db.py` | Mutual exclusion for YacReader's SQLite index, which the app writes **through the FUSE mount** while fleet tools write the same physical file on the SSD — two lock domains that cannot see each other. `db_lock()` stops the app and holds `state/yacreader_db.lock`; `library_supervisor` refuses to start YacReader while it is held. Also owns the scan-at-startup invariant: `read_scan_settings()`/`scan_settings_ok()`/`ensure_scan_settings()` patch the app's ini under that lock, and `index_open()`/`activate_app()` detect the windowless-after-crash state in which no update ever runs. `hide_app()` puts a fleet-started reader out of the owner's way with AppKit's `NSRunningApplication.hide()` (AppleScriptObjC, no Accessibility grant needed), System Events only as fallback. |
| `yacreader_index.py` | Read-only index queries. `load_order_faults()` simulates `FolderModel::createModelData`'s exact walk and names the row that would SIGSEGV the app (dangling parent, cycle, missing root, parent-after-child); `unindexed_files()` reports shelf/pool comics the index does not know about — the signal that the reader has not scanned since they landed. |
| `scripts/yacreader_rescan.py` | Report the scan-at-startup flags and the unindexed shelf (`--files`), or `--apply` to patch them under the index lock and request a rescan. The human half of the freshness contract. |
| `scripts/yacreader_index_repair.py` | Report (and `--apply` under the lock) the tree shapes that crash YACReader's loader. Reconstructs parents from folder `path` when that keeps the loader's parent-sorts-first invariant, attaches to the root otherwise, recreates a missing root, backs up and verifies `integrity_check` around the edit. |
| `scripts/yacreader_index_health.py` | Read-only `integrity_check` of the live index **plus a per-backup census**, so "the newest backup that PASSES integrity_check" is a fact on screen. Advisory in `verify_fleet.sh`; the corruption is partial, so row counts alone read healthy. **Exits 2 — a distinct status from damage — when the index is INTACT but no backup passes `integrity_check`**, the one state from which the next corruption is permanent. |
| `scripts/audit_blocklist_orphans.py` | Blocklist rows that match **nothing** — the mirror of `audit_blocklist_collisions.py`. Classifies every row LIVE / DRAINED / ORPHAN against the mount, raw `wants.json`, `library.db`, `reap_purges.log` and the deletion queue. Read-only and report-only; `--selftest` (a blocking check) proves every verdict can fire against fixtures, including the real `'Saiki? no'` string. |
| `scripts/test_yacreader_backup_census.py` | The census, both directions, against databases the test builds and damages itself. Blocking check. The `NO CLEAN BACKUP` branch has never fired in production (6 of 7 backups are clean), which is exactly why it needs a fixture: its corrupt-database fixture reproduces the *partial* shape — `folder` and `comic_info` answer while `comic` fails — so a fixture that stopped corrupting anything is itself an assertion failure. |
| `scripts/test_yacreader_lock.py` | The index lock, both directions, cross-process, against a temp file. Blocking check — a lock that could never read "held" would let the supervisor start the app onto a tool's edit. |
| `scripts/test_yacreader_scan_config.py` | The ini surgery: flags set, every other line byte-preserved, idempotent, miss in the right section, fresh file usable. Blocking check. |
| `scripts/test_yacreader_index_shape.py` | Each crash shape is named (dangling/cycle/rootless/late-parent), a healthy tree stays clean, repair leaves a loadable tree, and freshness sees pool-only, local-only, non-comic and dotfile paths correctly. Blocking check. |
| `scripts/test_supervisor_yacreader.py` | The supervisor's YacReader contract: start with flags patched and marker consumed, bounce on drift, consume a filed-comics marker WITHOUT restarting, activate a chooser-parked app (alerting after two tries, never restarting it), back off after three rapid crashes, and arm/perform the hide only once the update proves the window exists. Blocking check. |
| `scripts/test_yacreader_hide.py` | `hide_app()`'s route order (AppKit `NSRunningApplication.hide()` first, System Events fallback), the retry on a not-yet-registered app, and the fail-soft contract — all against faked subprocesses. Blocking check. |
| `config.py` | All tunables. |
| `playlist.py` | Builds curated Jellyfin playlists from `state/playlists/*.json` manifests (§ Curated playlists): resolves ordered tokens to files, maps to Jellyfin item ids, idempotently creates/replaces the playlist. Read-only w.r.t. the library. |
| `playlist_watch.py` | Auto-maintains playlists for shows in `config.PLAYLIST_AUTO_SHOWS` (One Piece): judges each newly-ingested episode with a headless AI run and appends the keepers, then rebuilds. Called from `ingest._advance_cleanup`; also a CLI backfill. |
| `scripts/playlist_autobuild.py` | Session-independent (re)build daemon (launchd `com.mikeyferguson.playlistautobuild`): waits out the API spend limit, then curates each queued show via a headless run and builds it — one per cycle, temp-validated so a bad run can't clobber a good playlist. |
| `scripts/enable_webui.py` | Idempotent qBittorrent WebUI enabler (edits `qBittorrent.ini`). |
| `scripts/audit_metadata.py` | Read-only detector of episodes with missing/blank metadata (§ Metadata integrity). |
| `scripts/repair_metadata.py` | AI-driven backfill that owns+fills blank episodes with real title/plot. |
| `scripts/save_posters.py` | Poster backstop: fills any show missing an on-disk poster from Jellyfin's image providers (§ Metadata backup). |
| `scripts/backup_metadata.py` | Mirrors the Jellyfin sidecars (`.nfo` + artwork) and `state/` to a dedicated MEGA path Media-Syncer doesn't cover (§ Metadata backup). |
| `scripts/nightly_metadata.sh` | Nightly audit+repair+posters+backup wrapper run by the metadata launch agent. |
| `scripts/git_pull_locked.sh` | Sourced helper (not executed): serializes a launcher's `git pull` behind an atomic lock, so sibling daemons starting seconds apart can't corrupt the shared `FETCH_HEAD` and boot on stale code (§ Install). |
| `startup.sh` / `run_torrent_ingest.sh` | Installer / launchd launcher. |
| `run_torrent_reap.sh` | launchd launcher for the reaper (same conda env as ingest). |
| `run_db_guardian.sh` | launchd launcher for the Jellyfin DB guardian. |
| `run_direct_ingest.sh` | launchd launcher for the loose-file (comic/novel/video) ingester. |
| `run_direct_ingest_bridge.sh` | launchd launcher for the iCloud DirectIngest bridge. |
| `run_drive_ingest.sh` | launchd launcher for the external-drive auto-organizer. |
| `run_library_supervisor.sh` | launchd launcher for the mount-gated Jellyfin/YacReader supervisor. |
| `run_gdrive_supervisor.sh` | launchd launcher for the Google Drive app supervisor (light novels). |
| `com.mikeyferguson.torrentingest.plist` | Launch agent for the ingest daemon. |
| `com.mikeyferguson.torrentmetadata.plist` | Launch agent for the nightly metadata safety net. |
| `com.mikeyferguson.torrentreap.plist` | Launch agent for the remote-deletion reaper (in `startup.sh`'s `AGENTS`, so it installs and loads with the rest). |
| `com.mikeyferguson.directingest.plist` / `com.mikeyferguson.directingestbridge.plist` | Launch agents for the local ingester and the iCloud drop bridge; both in `startup.sh`'s `AGENTS` and `ship-fleet.sh`'s `LABELS`. |
| `cancel_ingest.sh` | Stop the daemon. |

## State files (all gitignored)

- `state/journal.jsonl` — per-torrent state machine, last-writer-wins. The source
  of truth for resume.
- `state/decisions.log` — human-readable record of **every call the AI makes**:
  the rationale and the full plan, per torrent, timestamped. This is the audit
  trail that makes every rename traceable and every misfile reversible.
- `state/tmp/<hash>_plan.json` — the raw plan the run wrote for each torrent.
- `state/library_summary.json` — cached per-show numbering/ownership/blank summary,
  keyed by directory mtime, so the digest doesn't re-read every `.nfo` off the
  spinning the SSD library root on every identify run (rebuilt only for changed shows).
- `state/metadata_worklist.json` — the most recent audit's list of blank episodes;
  consumed by `repair_metadata.py`.
- `state/playlists/<slug>.json` — curated playlist manifests, the **source of
  truth** for `playlist.py` (§ Curated playlists). Backed up to MEGA with the rest
  of `state/`; Jellyfin's own playlist store is a rebuildable projection of these.
- `state/nfo-backup-<ts>/` — every episode `.nfo` the repair overwrote, backed up
  before the write (mirror of the library path) so a repair is reversible.
- `state/reap_snapshot.json` — the reaper's baseline: the set of tracked-media
  files last seen present. A local disappearance is a diff against this.
- `state/reap_pending.json` — `{relpath: consecutive-missing count}` for the
  reaper's debounce; a path purges only once its count reaches `REAP_DEBOUNCE_SCANS`.
- `state/reap_purges.log` — human-readable audit of every reaper purge (per file,
  which remotes), the deletion analogue of `decisions.log`.
- `state/reap_ALERT.txt` — written when the reaper's circuit breaker trips (a
  drive-fault-shaped mass disappearance); its presence means a batch was refused.
- `state/reap_ms_paused` — crash-safe marker present only while the reaper has
  Media-Syncer paused for a purge; a lingering one at cycle start tells the reaper
  a prior run died mid-purge, so it resumes Media-Syncer.
- `state/manga_volume_map.json` — `{series_norm: {provider ids, fetched_at, source,
  confidence, volumes {n: [chapter ints]}, ai_volumes, unmapped_volumes}}`, the cached
  bibliographic volume→chapter map (`scripts/manga_volume_map.py`).
- `state/manga_chapter_policy.json` — per-series keep rules for the chapter reconciler
  (`keep_volumes` default, `keep_chapters`, `keep_all`).
- `state/manga_map_refresh_request.json` — series whose volume map an ingest hook found
  missing/stale; drained by the 6-hourly reconciler.
- `torrent_ingest.log` — engine log. launchd stdout/err at
  `~/Library/Logs/TorrentIngest.log` / `.err`. Metadata launch agent logs at
  `~/Library/Logs/TorrentMetadata.log` / `.err`.
- `torrent_reap.log` — reaper engine log. launchd stdout/err at
  `~/Library/Logs/TorrentReap.log` / `.err`.

> **Engine logs rotate; the journal and the audit trails never do.** Every **engine**
> log — `torrent_ingest.log`, `torrent_reap.log`, `db_guardian.log`,
> `library_supervisor.log`, and the direct-ingest/drive-ingest logs — is capped at
> `config.LOG_MAX_BYTES` × `config.LOG_BACKUP_COUNT` (5 MB × 3). These are working logs:
> you read them to see what a daemon is doing now, so dropping the oldest slice costs
> nothing.
>
> **Three files are deliberately excluded, and must stay that way:**
>
> * `state/journal.jsonl` — this is operational **state**, not output. It is the
>   crash-resumable per-torrent state machine; losing its oldest slice loses the record of
>   what was already applied and verified, which is exactly what stops a re-ingest from
>   re-running destructive cleanup. It is bounded by **compaction** instead (§ Recovery and
>   failure behavior), which keeps every torrent and drops only superseded snapshots.
> * `state/decisions.log` — the audit trail of every call the AI makes.
> * `state/reap_purges.log` — the audit of every remote purge, per file and per remote. It
>   is the only record of what was **deleted** from the fleet.
>
> The rule, shared with Media-Syncer (where `~/Library/Logs/MediaSync.err` is exempt for
> the same reason): **a file whose value is being COMPLETE cannot be rotated by dropping
> the oldest part of it.** Cap an audit trail only by *archiving* — gzip the old slice and
> keep it.
>
> Two mechanisms, because the writers differ. Daemons that append with `open("a")` per
> write call `config.rotate_log_if_large()`, where a rename is safe because the next write
> reopens by path. The reaper logs through Python's `logging`, whose handler holds the file
> open, so it uses `RotatingFileHandler` instead — an external rename there would leave it
> writing to an orphaned inode.
>
> **What makes bounding the repo copy safe is that it is not the only copy.** Every `log()`
> here also `print()`s, and launchd captures that into `~/Library/Logs/TorrentIngest.log`,
> which is **never rotated** and retains the full history back to install, spanning every
> line the daemon has ever written even when the repo's `torrent_ingest.log` has just
> rotated down to a single fresh line. So the repo file is
> the *working* copy (what the daemon is doing now) and the launchd file is the *archive*,
> exactly the split Media-Syncer has between `media_sync.log` and `MediaSync.err`. **When
> you need history — a `DEFERRED` hunt, an ingest post-mortem — grep the launchd file, not
> the repo one.** Corollary: the launchd captures grow forever by design; do not "clean
> them up" without reading the archive rules above.

> **Log timestamps are LOCAL time, via one helper — `config.log_stamp()`.** Every
> human-facing log line in this repo goes through it, so these logs interleave
> correctly with Media-Syncer's (whose `logging` handlers are local by default).
>
> This is written down because it drifted, expensively. Five daemons here
> (`ingest`, `direct_ingest`, `db_guardian`, `library_supervisor`, `drive_ingest`)
> formatted their lines as `datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")`
> — **UTC printed with no offset**, indistinguishable from local time by eye. So
> half the fleet's evidence sat 5 hours off, events read out of order when
> interleaved, and `db_guardian` contradicted *itself*: its log lines were UTC while
> the backup filenames it logged about were local, producing entries like
> `[<UTC time>] pushed jellyfin-db-<local-time stamp>.sqlite` — one instant,
> two clocks, five hours apart on the page. Diagnosing a fleet incident meant
> silently applying an offset to some lines and not others.
>
> **MACHINE records stay UTC on purpose** (`journal.py`, the reaper's marker files,
> `repair_metadata`'s dated dirs): those are data, and they carry full ISO-8601 with
> a `+00:00` offset, so they are self-describing. The bug was never "UTC" — it was a
> bare UTC clock time wearing local time's clothes. Use `config.log_stamp()` for
> prose and `config.log_stamp_iso()` (local, **with** offset) for audit trails that
> humans read but tooling may parse; never format a log timestamp by hand.

> **`--dry-run` writes nothing at all, including our own bookkeeping.** It must not save
> `doctor_state.json` / `doctor_art_cache.json` / `doctor_worklist.json` or overwrite
> `library_health.txt` — doing so advances the fix ladder, baselines show identities and
> caches artwork verdicts for repairs that never happened. Worse, `apply_auto_fixes` appends to its `acted` list *before* it
> checks `dry_run` (deliberately — that is how a dry run reports the whole ladder it
> would climb), so the list is a **plan, not a receipt**, and the report rendered
> every entry as `-> fixed:`. A scoped `--dry-run --show X` therefore replaced a
> whole-library report with a one-show view whose contents claimed to be repairs.
> Now: dry runs persist nothing and prefix every action `WOULD`, and
> `write_report(..., scoped=True)` refuses to write for any `--show`-scoped run at
> all — even a real one — because a one-title report reads as a whole-library
> all-clear.

---

## Known limits and gotchas

- **v2-only torrents are unsupported** (info-hash is v1). See § qBittorrent notes.
- **Never hand-trim an in-flight torrent — the completeness gate requires ~100%.**
  The `DOWNLOADING` resume gate only promotes a gone-from-qBittorrent torrent to
  `DOWNLOADED` when the on-disk payload is **≥ 99.9% of the full `total_size`**.
  Deleting files out of a torrent's download folder (e.g. dropping non-dual-audio or
  `Uncropped` episodes you don't want) makes it read as incomplete **forever**, so it
  `FAILED`s every retry — the missing bytes are exactly your deletion (the One Piece
  0001-1071 pack failed at 568/601 GB = the 33 GB of eps 1001-1071 that were removed).
  To keep only some of a pack: let it ingest **whole**, then delete the unwanted
  episodes from the **library** — the reaper mirrors that delete to the fleet cleanly
  (§ the reaper). If a trimmed pack is *already* on disk and you want to place just
  what's present, run the identify+apply steps directly on its `content_path`
  (bypassing the gate) rather than re-dropping the `.torrent`.
- **Transient identify stalls are retried, not fatal.** A complete-series pack makes
  the identify run stream one very large plan for many minutes; a network/server blip
  mid-stream (`Response stalled mid-stream` / `Connection closed mid-response`) used
  to fail the whole torrent on the first hiccup. It now retries with backoff
  (`IDENTIFY_MAX_ATTEMPTS` / `IDENTIFY_RETRY_BACKOFF_SEC`, matched against
  `IDENTIFY_TRANSIENT_SIGNATURES`); a deterministic failure (invalid/rejected plan)
  is still terminal and not retried.
- **First drop of a new show is the riskiest.** There is no on-disk ground truth
  yet, so placement leans on TMDB + the model's judgment; watch `decisions.log` on
  first drops until you trust a given show's layout.
- **`FAILED` is not *auto*-retried, but a move-back retries it.** By design it
  won't restart on its own while it sits in `failed/` — a torrent the model keeps
  misjudging shouldn't loop and burn tokens. To retry, **move its `.torrent` from
  `failed/` back into the watch folder**; `register_new_torrents` sees the `FAILED`
  hash reappear at the top level and re-queues it from scratch (no journal edit).
  A failed torrent's local download is left for inspection. A *completed* torrent
  likewise redownloads on a plain re-drop — completion is filed under `finished/`,
  not permanently retired (§ Completion is not retirement).
- **Both `FAILED` and `COMPLETED` re-drops work the same way now.** Moving/dropping
  the `.torrent` back into the watch folder re-queues it regardless of which
  terminal state its hash carries; the reappearance at the top level is the signal.
  (Clearing the record from `state/journal.jsonl` by hand still works, but is no
  longer necessary for a FAILED retry.)
- **Already-present media is a success.** A drop whose files are all already in the
  library completes cleanly (skips the existing files, deletes the local copy,
  files the `.torrent`) — it is no longer a plan-rejection failure (§ Already-present
  media is a success). **One Pace is the exception:** a repeat under
  `Shows/One Pace (2013)/` overwrites the older cut instead of skipping it, because
  its re-releases are meant to replace what's on disk (§ The One Pace exception).
- **Blank episode metadata on long shows.** A show filed un-owned under numbering
  the provider can't resolve renders as bare "Episode N" (§ Metadata integrity).
  The identify resolve test prevents new cases and the nightly audit+repair fixes
  any that slip through; run `scripts/audit_metadata.py` to check the current state.
  A repaired episode is **owned** (locked), so it won't re-scrape — if a plot ever
  looks wrong, delete that `.nfo` (or restore from `state/nfo-backup-*`) and let
  Jellyfin scrape, or re-run the repair.
- **Identify costs tokens and takes seconds-to-minutes.** Negligible against
  download time and at occasional-torrent volume; would be wrong at thousands/day.
- **The reaper deletes backups — it is opt-in and Mini-only** (§ Remote deletion).
  It is deliberately *not* loaded by `startup.sh`; load
  `com.mikeyferguson.torrentreap.plist` by hand to enable it. It acts only when the
  drive is provably healthy; a drive fault trips the circuit breaker and purges
  nothing. If the breaker trips (`state/reap_ALERT.txt` appears) it stays tripped —
  by design — until the missing files return or you purge by hand and
  `--reset-baseline`. A confirmed deletion is intentionally **irreversible on the
  remote** (that's the point), though MEGA's rubbish bin holds a copy until the
  reaper's `cleanup` empties it.
- **Reaper edits Media-Syncer's state and pauses its daemon.** It reads/prunes
  `remote_inventory.json` + `sync_state.json` and stops/starts the `mediasync`
  launch agent for the duration of a purge. Keep `REAP_TRACKED_EXTENSIONS` in step
  with Media-Syncer's replicated set, and its `ONE_PACE_PREFIX`/paths in step, or
  discovery/pruning drift. If a purged file is ever seen re-downloaded, the state
  prune didn't land — check `torrent_reap.log` for survivors.
- **Single-machine assumption.** This runs on the Mini. Do not also run it on the
  Air — the iCloud folder is shared, but only the machine with the SSD library root mounted
  should ingest.
- **qBittorrent must stay installed and its WebUI enabled.** A qBittorrent update
  that resets preferences would need `enable_webui.py` re-run (re-running
  `startup.sh` is safe and idempotent).
- **WebUI won't start without credentials (v5).** If you ever see the daemon log
  `qBittorrent WebUI unreachable`, check qBittorrent's own log for
  `WebUI: Credentials are not set` — that means the `WebUI\Username` /
  `WebUI\Password_PBKDF2` keys are missing. Re-run `enable_webui.py` (with
  qBittorrent stopped). Localhost bypass alone is not enough to satisfy v5.
- **Tailscale gate is an interface check, not the CLI.** If the daemon wrongly
  reports Tailscale down, remember it looks for a `100.64/10` address on a local
  interface — not `tailscale status`, which fails under launchd because the Mac
  app's socket is unreachable there. If you switch to Homebrew `tailscaled`, the
  interface check still works unchanged.

---

## Storage model: SSD library root, kept-and-uploaded, drive-aware

`config.MEDIA_ROOT` is the **Mac SSD** (`~/Media`); ingest lands renamed media there and **Media-Syncer uploads it to the MEGA pool and keeps the local copy** (the file may also live on an attached external drive — see Media-Syncer's README, *Storage model*). Everything is served through the `mediafs` mount at `~/MediaLibrary`. There is **no external-drive dependency**: `library_ready()` simply checks the SSD root exists, and no symbol in this repo names a specific drive.

### Torrents download in space-bounded waves sized as a **fraction** of the disk

Torrents download to and upload from the local Mac SSD. A torrent that can't be downloaded whole is
downloaded in **waves** via qBittorrent file priorities. `_admit_chunked()` adds it paused, exempts it
from qBittorrent's share limits (below — without this the pack is deleted after wave 1), and parks
every file at priority 0; `_advance_chunked()` enables one wave, waits for it, runs the whole wave
through the normal `identify → apply → verify` pipeline in a single pass, frees each file it proves
into the library, then enables the next wave until every file is done. Normal (fits-whole) torrents
are unaffected.

**Two triggers put a torrent into chunked mode**, and both are needed to cover the range:

1. **It cannot fit the SSD even empty** — `need + MIN_FREE_BYTES` exceeds the volume's *total*
   capacity (a 400 GB+ pack against a 460 GB disk). Chunked immediately at admission.
2. **It has been `QUEUED` for `CHUNK_AFTER_DEFERRED_SEC` (2 h) without ever fitting the disk's
   *free* space.** Trigger 1 alone leaves everything between the two thresholds — bigger than the
   headroom, smaller than the disk — waiting on a drain that never comes (§ *A torrent that doesn't
   fit right now waits*).

A torrent with a **single file** larger than `config.MAX_SINGLE_FILE_BYTES` (10 GiB — the
MEGA-account cap, not the disk) is the only un-chunkable case and is refused (and purged if
already queued/ingesting).

**Wave size is the smaller of two numbers**, and the distinction matters:

* `config.torrent_chunk_bytes()` = `TORRENT_CHUNK_FRACTION` (1/6) of the usable Downloads-volume
  capacity (total minus a 1/10 breathing reserve), ~69 GB here, computed live so it scales to any
  machine. This is the **ceiling** on how much disk torrent ingest may occupy at once.
* `_remaining_budget()` — the **live** headroom (free − `MIN_FREE_BYTES` − what in-flight downloads
  still owe). The ceiling is a policy, not a promise that the space is free; a wave sized past the
  live budget breaches `MIN_FREE_BYTES`, which is the exact failure chunking exists to prevent.

`CHUNK_MAX_FILES_PER_WAVE` (32) caps the wave by count as well. A wave is ingested in one blocking
pass, so an unbounded wave holds the daemon inside a single torrent while nothing else is admitted or
advanced. If the live budget can't hold even the smallest remaining file, the wave waits and logs
`DEFERRED (chunked)` hourly rather than stalling silently.

**A chunked torrent reserves only its active wave**, not its whole size (`_chunk_outstanding`).
Every file outside the active wave is parked at priority 0 and will not be written until a later
wave admits it, so charging the budget for the whole pack would drive it negative and block every
other torrent for the pack's entire lifetime.

**Already-owned files are skipped, never downloaded (§ diagnosis 6.3.2).** Before selecting each
wave, `_advance_chunked` folds the searcher's per-file verdict into `chunk_done`: a file the stored
`torrent_plan` marks `owned` (re-checked against the **live** library DB at ingest time — a stale
snapshot is never trusted) or `skip` (a non-media/delete file ingest would decline anyway) is treated
as already done, so a mostly-owned pack fetches, files, and identifies only its genuinely new and
upgrade files. The skip is fail-safe: a file whose `src` does not map, or whose owned re-check cannot
run, is left for the normal path — an optimisation, never a risk. Whole-torrent (fits-whole) packs do
not yet skip; the chunked path is where the savings are (huge, mostly-owned packs).

#### A chunked torrent must be exempt from qBittorrent's share limits

**`qbt.exempt_from_share_limits` is load-bearing, not hygiene.** qBittorrent here is configured to
delete a torrent the moment it finishes (`max_seeding_time = 0`, `max_seeding_time_enabled = true`,
`max_ratio_act = 1`), and the whole-torrent path depends on that: `_advance_downloading` reads "gone
from qBittorrent" as "finished," then confirms against the bytes on disk.

A chunked torrent is finished by that same rule at the end of **every wave** — qBittorrent counts a
torrent complete when its *selected* files are complete, and a chunked torrent selects one wave at a
time. Without the exemption the pack is reaped at the end of wave 1 with every later wave still
parked at priority 0 and never fetched.

So `_admit_chunked` pins the torrent's own share limits to **`-1` (no limit)** — not `-2`, which
means "use the global setting" and is precisely the rule being escaped — and `_advance_chunked`
re-asserts it before enabling each wave, so a torrent adopted from an older record or re-added by
hand cannot be running under the global rule. The chunked driver removes the torrent itself once the
last wave is filed. **Do not narrow this to admission-only**, and do not change it to `-2`.

#### A chunked torrent never seeds, and never sits in the list claiming to be finished

The exemption above buys survival across waves at the cost of qBittorrent's own reason to ever stop
uploading. It costs something subtler too: qBittorrent measures progress over *selected* files only,
so between waves a chunked pack reports **100% complete** no matter how much of it is still unfetched
— a 170-file pack with 164 files to go shows up as a finished torrent. Left alone it therefore both
seeds and lies, for the days the pack takes to drain.

Registration exists for exactly one purpose: to fetch the next wave. So the pack is registered when
it is doing that, and not otherwise.

* **A wave is downloaded but not yet filed.** The torrent must stay registered (`_free` sets file
  priorities through it) but has nothing to fetch, so it is **stopped** — filing a wave is an
  identify run, minutes. `sweep_chunked_idle` stops any pack whose active wave has no file still
  fetching.
* **No wave can be enabled because the disk is starved.** That wait is measured in other torrents
  completing, so hours or days. `_park_chunked` **removes the torrent from qBittorrent, keeping its
  files**, and records the size of its smallest remaining file. The row disappears entirely.
* **The disk frees.** `_unpark_chunked` re-adds it through `_readopt_chunked` once the budget covers
  that smallest file — never merely because it is missed, or the row would reappear within a cycle
  and sit at "completed" again. Since a budget that covers the smallest remaining file always admits
  at least one file, an unpark is always followed by a real wave; it cannot oscillate.

Parking is safe for the same reason re-adoption is: everything needed to resume is durable — the
source `.torrent` and `chunk_done` — and it happens only between waves, when nothing is in flight and
the wave's files have already been filed and freed. `_park_chunked` **refuses to park a pack whose
source `.torrent` is gone** and stops it instead; removal would be unrecoverable, and a stopped row
beats a stranded pack. A parked pack reserves no disk budget (`_chunk_outstanding` is 0 with no
active wave) and is not re-registered by `register_new_torrents`, which skips a `.torrent` whose
record is already in flight.

#### A finished chunked pack is retired before the slow work, not after

`advance()` walks the active torrents in one blocking pass and a single wave holds it for as long as
an identify run takes, so a pack that finished its last wave would otherwise stay registered — and
seeding — until the loop came back around, which across a full sweep is hours. `sweep_chunked_idle`
runs at the top of every cycle, before `admit_downloads` and the `advance()` loop, and costs one file
list per chunked torrent: no identify, no disk. It retires a pack with nothing left to fetch and
stops one that is merely between waves.

#### An active file parked at priority 0 is re-armed from qBittorrent, not trusted from the record

The wave completion gate waits for every active file to reach 100%. A file that is in `chunk_active`
but sits at **priority 0** is one qBittorrent will never fetch, so that gate can never pass: the wave
stalls forever and the pack — exempt from the reaper — seeds indefinitely, looking from outside
exactly like a torrent the engine abandoned.

The state is reachable whenever a wave is interrupted between `_free()` dropping a file to priority 0
and the journal write that would have recorded it done: a crash, a kill, a restart, an exception
mid-wave. The record and qBittorrent then disagree, and nothing reconciles them. qBittorrent's live
priority is the authority on what will actually be fetched, so `_advance_chunked` re-arms any such
file to priority 1 and resumes, rather than trusting the record to be self-consistent.

#### A wave file missing from disk is re-fetched, never marked done

A file in the wave that is not on disk has no bytes and is not in the library. Marking it done
retires it **silently** — absent from the library, absent from `chunk_failed`, and invisible to the
re-drop path that exists to recover exactly this — so the gap only surfaces when someone goes looking
for the episode. The torrent is still registered, so the bytes are recoverable: re-arm the file to
priority 1 and let a later wave fetch it, bounded by `CHUNK_FILE_MAX_ATTEMPTS`. Only once re-fetching
has also failed does it go into `chunk_failed`, where it is visible.

#### A chunked torrent that vanishes is re-adopted, never failed

A chunked pack is the one torrent whose lifetime is measured in days, so it outlives qBittorrent
restarts. And unlike a whole-torrent download, absence can never mean "finished," because a chunked
torrent is only ever removed by the chunked driver itself — either deliberately, to park it between
waves (above), or on the last wave. `_readopt_chunked` therefore re-adds it from the source
`.torrent`, re-parks every file at priority 0, re-applies the share-limit exemption, clears the
active wave, and lets the next cycle select a fresh wave from whatever is still unfiled.

Absence is read against `chunk_parked`: set, the pack is out of the list on purpose and waits for the
disk before coming back; unset, something else took it — a qBittorrent restart, a crash, a stray
manual removal — and it is re-adopted immediately.

**But re-adoption is now gated by the budget, not automatic.** A disk that cannot hold the pack's
smallest remaining file has no use for a re-added row: the pack would sit at priority 0, "completed",
for a cycle and then be parked again. With hundreds of starved packs that re-add-then-park loop was an
O(n²) qBittorrent churn that made a single cycle take hours — and, worse, held the watch-folder
registration at the end of that cycle, so fresh `.torrent` drops sat un-queued for the whole sweep.
When a vanished, un-parked pack's smallest remaining file does not fit the budget (read from the
`.torrent` itself, no round-trip), `_advance_chunked` parks it **in place** (`_park_out_of_qbt`)
instead of re-adding it, exactly as if `_park_chunked` had removed it.

Two supporting changes keep the cycle fast regardless of backlog depth:

* **One torrent snapshot per cycle.** The engine used to resolve a torrent with one `qbt.get()` per
  hash, and `_remaining_budget` (itself called once per chunked torrent) iterated every in-flight
  record — an O(n²) request storm. `qbt.torrent_map` fetches every torrent in a single
  `torrents_info()` call and the whole cycle consults that map instead of the API.
* **Registration runs on a timer mid-sweep, not just at cycle boundaries.** A large chunked backlog
  makes one `advance()` sweep long; `register_new_torrents` now also fires every
  `config.REGISTER_REFRESH_SEC` (20 s) during the sweep, so a `.torrent` dropped at the top of the
  watch folder is queued into `queued/` within seconds even while the queue is full and chewing —
  never left sitting in the base folder for the duration of a long cycle.

Everything needed to resume is already durable — the `.torrent` in the watch folder, and `chunk_done`
in the journal — so nothing is lost. Bytes on disk from a wave that was in flight are rechecked by
qBittorrent when those files are next selected. The one unrecoverable case is a re-adopt whose source
`.torrent` is gone: that FAILs, because there is nothing left to resume from.

#### A wave is identified as one unit, not one file at a time

`_identify_wave` runs **one** headless AI pass over the torrent's content root per wave. Only the
wave's files are on disk — qBittorrent does not preallocate a file parked at priority 0, and every
earlier wave's files were unlinked as they were filed — so the listing the run sees *is* the wave.
The content root is the torrent's own folder, never the shared `INCOMING_DIR`: that directory holds
every other torrent's download too, and an identify run pointed at it would plan files belonging to a
different torrent.

The wave, not the file, is the right unit for three separate reasons:

* **Cost.** A 175-file pack costs ~6 runs instead of 175; a 500 GB pack costs ~30 instead of ~1,000.
  Per file, the identify time alone runs to days and walks into a spend cap on every pack big enough
  to need chunking — the exact torrents this path exists to serve.
* **Correctness.** Every numbering call the prompt makes — absolute vs seasoned, which season an
  episode extends, movie vs special — is a judgment about a file *among its siblings*. A single
  episode handed over alone has none of that context.
* **Junk.** A creditless opening, a sample, or an in-pack duplicate the validator collapses is simply
  absent from a wave plan, which is how the whole-torrent path has always treated junk. Identified
  alone, such a file instead yields an empty plan — which the validator rejects as a *failure*, so
  every NCOP in a pack would burn its retries, land in `chunk_failed`, and end an otherwise complete
  torrent as `FAILED`.

A file on disk that a **successful** wave plan left out is therefore dropped as junk, not failed:
freed and marked done, exactly as the whole-torrent path deletes everything outside its plan.

**A wave holding no ingestable file at all never reaches identify.** The rule above only covers junk
that rides along *with* media, because it needs a plan that succeeded — and a wave whose every file
is junk produces an empty plan, which the validator rejects as a failure rather than reading it as
the verdict it is. That is not a corner case: releases put their `RARBG.txt`, tracker ad page, and
release `.nfo` at the end of the file list, so the *final* wave of a pack is routinely nothing else,
and the pack would end `FAILED` over its own release notes with every episode already correctly in
the library. So `_advance_chunked` filters the wave first: any file whose extension is outside
`config.MEDIA_EXTENSIONS` can never appear in a plan, and is freed and marked done without an
identify run. If nothing ingestable remains, the wave ends there.

**A file is freed only once it is proven to be in the library** (or explicitly declined as junk by a
plan that succeeded). The unlink is irreversible — for a chunked torrent the local copy is the only
one, since the pack does not fit the disk twice — so `_advance_chunked` frees and marks each file
done *individually*, against its own `verify_applied`, never as a blanket sweep over the wave. Three
failure modes are separated:

* A **usage limit** (`IdentifyUnavailable`) hits the whole wave at once, so it aborts the wave with
  every byte still on disk, nothing deleted and nothing marked done; the next cycle re-ingests it
  (see the table under § *Failure mode: the AI API being unavailable*).
* A **rejected wave plan** keeps every unfiled byte for another attempt. After
  `CHUNK_FILE_MAX_ATTEMPTS` (2) wave-level attempts, each remaining file gets a **per-file identify**
  as a last resort — a wave plan is rejected as a unit, so one pathological file must not take its
  whole wave down with it.
* A file that survives all of that unfiled is given up on: its name recorded in `chunk_failed` and
  its index in `chunk_failed_idx`, and the torrent ends `FAILED` naming the files that never landed —
  a pack with a hole in it must not read as a clean `COMPLETED`. (Unlike other failures there is no
  local download left to inspect: a chunked torrent's bytes are gone as they are consumed.)

Re-running a file is always safe, which is what makes every retry path above sound: `apply_plan`
detects the pre-existing destination and skips it (§ *Already-present media is a success*).

#### A re-dropped chunked torrent resumes; it does not start over

Re-dropping a `.torrent` restarts it from scratch (§ *Completion is not retirement*), which is right
for a whole-torrent ingest: it re-downloads, and `apply_plan` skips whatever is already in the
library. A chunked pack cannot afford that. Re-fetching hundreds of GB it has already proven into the
library, one disk-bounded wave at a time, costs days and an identify run per wave to conclude
"already present" — and every one of those waves is a window in which the pack can be interrupted
again.

So `_carry_chunk_progress` carries the previous record's per-file progress onto the re-queued record.
**File indices are stable for an info hash**, so a carried index names the same file, and files given
up on UNFILED (`chunk_failed_idx`) are deliberately **not** carried, because getting those is the
entire point of a retry.

##### What is carried is what can be PROVEN, not what was remembered

`chunk_done` is a claim about the past — §4.10's lesson one layer down — and **the commonest reason
to re-drop a pack by hand is that its content is gone**: purged, or never filed in the first place.
Carrying the claim then turns the owner's re-acquisition into a silent no-op that reports COMPLETED.

That is what happened to `[MTBB] Monogatari Series (BD 1080p)`. The title was purged on 2026-07-28;
the pack was re-dropped on 09-03, 09-07 and 09-08, and each time inherited `chunk_done` = all 103
files, found nothing left to fetch, and logged `COMPLETED chunked … all waves ingested` about 21
seconds later — over a library that had not held it since July. **Three other titles were lost the
same way** (Higurashi, Bakugan Armored Alliance, Log Horizon). Young Sheldon S04 inherited the same
way and its content really *was* still there, which is why the fix must prove **per file** rather
than refuse to carry at all.

An index is therefore carried only when:

* the record can name where the file landed **and** that destination is still in the library —
  `chunk_filed` (index → library-relative path), checked on the **mount**, because `~/Media` missing
  means EVICTED, not deleted (§4.1), so the SSD cannot answer the question; or
* the pack deliberately **declined** the file — `chunk_dropped` (non-media junk, a file the plan left
  out, an extra with no library home, an already-owned file folded in by `_skip_owned_indices`).
  Re-fetching a creditless opening to decline it again buys nothing, and it will never be "in the
  library" to find.

Everything else is re-fetched, **including every legacy record written before this bookkeeping
existed** — those can prove nothing, so they carry nothing. The asymmetry is deliberate: carrying
wrongly loses content silently and tells the owner it worked, while re-fetching wrongly costs
bandwidth, is visible in the log, and `apply_plan` drops what is already present anyway. Only one of
those is reversible. A mount that cannot answer narrows what is provable rather than inverting it,
and says so in the log, so a mount blip is never mistaken for content that is gone.

##### A pack that proved nothing must not read as success

`_finish_chunked` used to check only `chunk_failed`, so "every file is accounted for" passed straight
through as "all waves ingested" — the `"I cannot verify this, so accept it"` fallback §4.4 forbids. A
pack that can name **neither** a destination it filed nor a file it declined now **FAILS**, with a
message saying it retired on inherited progress it could not prove and to re-drop the `.torrent`.
Controls matter as much as the gate here (a gate that fires on everything is not a gate): a pack that
filed something, and a pack whose files were all legitimately declined, both still COMPLETE.

The chunked path also now writes **`record["applied"]`**, which it never did — `applied_all` was
built only to decide whether to poke Jellyfin, so a chunked pack that filed 103 files was
indistinguishable in the journal from one that filed none. That ambiguity is what the Monogatari
diagnosis had to reconstruct from the daemon log to resolve.

Guard: `scripts/test_chunk_progress_proof.py` (in `verify_fleet.sh`), both directions.

It also sets **`chunk_intent`**, which tells `admit_downloads` this pack is already known not to fit
and to chunk it immediately. Without it a retry re-serves the full `CHUNK_AFTER_DEFERRED_SEC` (2 h)
deferral it has already served — two hours in which a retry that looked instantaneous produces no
downloading at all.

#### Re-filing a misnumbered pack (`scripts/refile_season.py --mapping`)

The season mode moves a whole season folder; a pack like the classic Doctor Who collection needs **per-file** renumbering — its filenames carry the release's *serial* number (`S01E05 (005) - The Keys of Marinus (1)`), so all six parts of a story claim one slot, the model copied that number, and `_collapse_existing_episode_collisions` then dropped the colliding files the wave freed as junk. `--mapping state/<file>.json` takes a reviewed `[{old, new}]` list as the evidence and does the whole repair in step with the bytes:

* **Chained destinations are ordered, not raced.** The move set can contain `S01E07 -> S01E31` while `S01E31 -> S02E11`; a `moveto` onto a live destination would clobber it, so `_order_moves` runs a move only once nothing that is itself moving sits on its destination. The preflight allows an existing destination only when the occupant is part of the same mapping.
* **Remote first, then local**, per file; sidecars for moved files are deleted (Jellyfin regenerates), per-file so correctly-filed neighbours keep theirs; `remote_inventory.json` and `sync_state.json` are rewritten with `.bak-remap` backups; `--record` rewrites `chunk_filed`/`applied`; `--rearm` clears indices from `chunk_done`/`chunk_dropped` so the bytes that were freed unfiled are **re-fetched**; and `library.db` gets the old rows superseded plus the corrected files recorded.
* **Park the daemon first.** A wave's identify subprocess races the moves and can refile from a half-moved library. Stop `torrentingest`, kill the in-flight `ai_runner`, apply, then bootstrap.
* **Pause Media-Syncer too, and check its state after.** Media-Syncer's in-memory inventory is written back every ~30s, so it can resurrect the OLD keys and clobber the NEW ones after the tool rewrites them — measured live on 2026-09-15. Hold it down with the reaper's own pause marker (`state/reap_ms_paused`, which `mediasync_watchdog` respects), `launchctl bootout gui/501/com.mikeyferguson.mediasync`, apply, then verify the inventory has every `new` key and no old ones before removing the marker and bootstrapping it back. **Chained moves need a two-phase transform** (read all new values from the original map, then remove old keys, then set new ones) — a sequential pop-then-set corrupts any destination that is also a later source.

### Direct ingest (`direct_ingest.py`, daemon `com.mikeyferguson.directingest`)

Not everything comes from a torrent. Comics from GetComics.com (and anywhere else), light novels / e-books from LibGen / the Internet Archive / Anna's Archive, and — since 2026-09-13 — **raw video** (a movie or episode downloaded directly) arrive as loose files or folders. The owner drops them into **`~/Downloads/DirectIngest/`** (a *local* folder, not iCloud) or into the iCloud mirror **`Torrents/DirectIngest/`**, which `direct_ingest_bridge.py` moves onto the local folder first (below). This daemon files them with a headless AI run matching the existing library's conventions, landing them through the torrent pipeline wholesale (`identify.run_identify → library.validate_plan → apply_plan → verify_applied`). **Four destinations, one pipeline:** video (`.mkv`/`.mp4`/`.avi`/`.m4v`/`.mov`) lands in `Shows/` or `Movies/` exactly like a torrent's files (with the same `.nfo` handling, and a Jellyfin rescan); comics land under `Comics/` in `config.MEDIA_ROOT` (YACReader, uploaded to the MEGA pool); novels (`.epub`, and `.pdf` planned as a novel) land in the Google Drive `Novels` folder via the `Novels/` top-dir. A dropped **directory** is one identify run over the whole tree — a season folder or a loose-pages comic folder — just like a torrent's download directory, because that is the shape identify reads best. **No format conversion** (`.cbr`/`.cbz`/`.pdf`/`.epub` file as-is; a plain `.zip` comic archive is renamed to `.cbz`). Failures park in `DirectIngest/.failed/` with a `.error.txt`; processed sources are deleted once verified. Install via `startup.sh` (or `launchctl bootstrap` the plist).

A loose video's subtitle siblings are filed **deterministically**: the run only sees the video file, so `_attach_video_sidecars` appends every sibling whose name begins with the video's stem — `Movie.srt`, `Movie.en.srt`, `Show.S01E01-E02.srt` — to the plan at the video's own planned destination, keeping the name tail intact (the compute-don't-ask rule; a directory drop is listed whole, so there the run plans them itself).

#### The iCloud drop mirror (`direct_ingest_bridge.py`, daemon `com.mikeyferguson.directingestbridge`)

`Torrents/DirectIngest/` is the cross-device counterpart of the local watch folder. The bridge daemon polls it every 20 s and MOVES each drop to `~/Downloads/DirectIngest/`; the folder is emptied by design, so a drop made from a phone does not keep reappearing on other devices. Four properties make the crossing safe, because it crosses both an iCloud sync layer and a volume boundary:

* **Materialize, then settle.** iCloud may hand a drop over as a dataless placeholder (`.Name.mkv.icloud`); `brctl download` is invoked on every placeholder (the same primitive `ingest.materialize` uses for a `.torrent`), then the bridge waits for the placeholder to become real bytes AND for the tree's whole byte total to hold still for `STABLE_SEC`. A half-synced file is never copied and never deleted.
* **Copy → verify → rename → delete.** The local copy is staged in `DirectIngest/.icloud-bridge/` (dot-named, invisible to the ingester) and only `os.replace`d into the watch folder once its size is verified. A crash can never leave a half-file where `direct_ingest.py` would file it; a failed copy leaves the iCloud source untouched.
* **Collisions never clobber.** If a same-named entry is already local, identical content means the iCloud copy is a leftover from an earlier crashed pass and is removed; different content is uniquified (`name.1.ext`).
* **Only ingestible media is touched.** `config.DIRECT_INGEST_EXTENSIONS` files, and directories holding those or loose page images. Dot-files, AppleDouble junk, `.icloud` placeholders themselves and the fleet's state subfolders are left alone.

`--once` is one pass; `--once --dry-run` reports what it sees and touches nothing (not even the materialization). The iCloud census still snapshots the folder but exempts its falling count from the "drop" verdict — draining it is the fleet working, not a §4.13 loss.

#### Failure mode: the AI API being unavailable quarantines a whole batch of perfectly good content

**Fingerprint: a run of consecutive `identify/validate failed ... identify run exited 2: deepseek http 402 ... insufficient balance` lines a few seconds apart, and that many files sitting in `.failed/` with nothing wrong with them.**

A usage limit belongs to neither of the two obvious failure buckets. It is not **transient** (`IDENTIFY_TRANSIENT_SIGNATURES` — a mid-stream blip worth retrying three times over ~two minutes), because it clears on a *wall clock* tens of minutes out, so that retry budget can only burn itself and then report a hard failure. It is not **deterministic** either, because the plan was never attempted and the identical input succeeds later. Classified as deterministic, it reaches each caller's broad `except Exception`, which quarantines the content as though the content were at fault.

**A missing credential lands in the same bucket.** The CLI resolves its OAuth token out of the login Keychain, and when it cannot it exits 1 with `Not logged in · Please run /login` — a "could not run" that reads, to a two-bucket classifier, exactly like a bad plan. It is the worse of the two cases, because a usage limit self-heals on a clock and a missing credential does not: left misclassified it deletes content for as long as it takes a human to notice.

`config.identify_unavailable()` therefore raises `identify.IdentifyUnavailable` — its own exception type precisely so it is caught *before* those broad handlers. It matches two ways, and the split matters:

* **Structurally**, on `hit your … limit`. The CLI names whichever cap was hit — session, 5-hour, weekly, per-model — so a list that enumerates windows misses every window it does not happen to spell, and a miss is not a deferral but content deleted UNFILED.
* **Literally**, via `config.IDENTIFY_UNAVAILABLE_SIGNATURES`, for the phrasings that have no shared shape: the credential failures (`not logged in`, `please run /login`, expired/refresh token, `unauthorized`, HTTP 401) and the billing ones (quota exceeded, credit balance).

The predicate lives in `config` rather than beside either caller because **both** identify paths must agree — this repo's and the YouTube ingest's, which re-exports it through `ytconfig`. A signature the two spell differently is a silent, content-deleting divergence, so `contract.py` asserts the export.

Every caller **defers instead of failing**:

* **Direct ingest** leaves the file in the watch folder, aborts the rest of the pass (every remaining file would hit the same closed door — marching through them is exactly how fifteen got quarantined), and idles `config.IDENTIFY_UNAVAILABLE_BACKOFF_SEC` (15 min) instead of re-polling every 30 s.
* **Torrent ingest** leaves the record at `DOWNLOADED` and logs it. The next cycle re-enters `_advance_identify` with the download untouched.

> The general rule: **"could not run" is not "ran and was wrong."** A retry classifier with only two buckets will eventually put a closed door in the wrong one, and the cost is always paid by content that was fine.

Five call sites run the identify step, and "defer" means something different in each:

| Caller | Deferral behaviour |
|---|---|
| `direct_ingest.process` | file stays in the watch folder, pass aborts, 15 min backoff |
| `ingest._advance_identify` | record stays `DOWNLOADED`, retried next cycle |
| **`ingest._advance_chunked`** | wave aborted intact, re-ingested next cycle |
| `drive_ingest.organize` | volume's pass stops, drive re-organized next pass |
| `youtube_sync.process_batch` (other repo) | ledger untouched, cycle ends early |

Two of those could lose data if the exception is allowed to reach the generic handler, and **the chunked-torrent path is the severe one.** `_advance_chunked` unlinks each of a wave's files and adds it to `chunk_done` — permanent, because for a chunked torrent the local copy is the only one (the pack does not fit the disk twice). If the wave's identify swallowed a usage limit into an empty result, those bytes would be **deleted without ever being filed, and marked done so they are never retried.** Two things prevent it: both `_identify_wave` and the per-file fallback `_ingest_one_file` re-raise `IdentifyUnavailable` so the wave loop catches it *before* any free-and-mark, and the free-and-mark is per file, conditional on that file's own `verify_applied` having passed — a file that was not filed keeps its bytes whatever the reason. Re-running a partially-ingested wave is safe: `apply_plan` sees the pre-existing destination and skips it (§ *Already-present media is a success*).

The YouTube caller matters for a subtler reason: `mark_failed_batch()` spends one of a capped number of retries, and past that cap the videos are abandoned permanently — so a batch that was never routed must leave no mark on the ledger at all.

#### Processed files are deleted from the watch folder, not parked

The watch folder is a local scratch area, but parking processed media there still means it consumes disk forever. Once `verify_applied` confirms the planned files are in their destination the source bytes are a pure duplicate, so `direct_ingest.py` **deletes them** (an unlink failure leaves them for the next pass). For a directory drop, only the files the plan actually placed are removed — unplanned leftovers (release `.nfo`, a declined sample) are moved to `.skipped/` instead of being destroyed, and emptied subdirectories are pruned.

Only the success path deletes. `.failed/` keeps its entry (you want to look at it) and so does `.skipped/` — a skipped archive has **no** library copy by definition, so deleting it would be real loss.

#### An empty plan is read by drop type: a verdict for archives, a claim to PROVE for video

`validate_plan` rejects an empty `files` list, and rightly so — for a **torrent** it means the run gave up on a directory it was supposed to place. A single loose archive is different: a single issue already contained in a shelved collection, or a `(Variant Cover Only)` rip, genuinely contains no library media, and the correct plan really is empty. `direct_ingest.process()` catches that one `PlanError` specifically and, by default (`DELETE_SKIPPED = True`), **deletes** the archive. The trade is deliberate — a skipped file has no library copy, so deletion is final, which is acceptable precisely because "skipped" means the content is already shelved inside a collection or is not comic/novel content at all. Set `DELETE_SKIPPED = False` to park in `.skipped/` and review them instead.

**Video and directories do NOT get that verdict.** A free model can simply return nothing over content that is not in the library (the "That 90s Show" incident, § *An empty plan over media*), and a direct drop has no `.torrent` left for a retry — deletion would be the only copy gone. So `_empty_plan_is_proven` requires POSITIVE evidence: with an `SxxExx` tag, every episode key must already exist in the library (local tree + remote inventory); without one, the film's folded title must match an owned film. Anything else parks in `.failed/` with a `.error.txt` and is never deleted on a model's word. The torrent path's proof function cannot be reused here because it treats "could not check" as fine; the direct path's bar is inverted.

#### Volumes supersede chapters; colored supersedes B/W — the manga hierarchy

**Fingerprint: the library holds both a collected edition and the singles it already contains**, because the old rule was *"collected editions only, never the individual parts"* and each run re-derived which parts a collection covers from an arbitrary library snapshot.

The rule is now a **three-tier hierarchy** — **colored volume > black-and-white volume > chapter** — and *every* tier is filed, not just the top one: a chapter is shelved as `cNNNN.cbz`, and when its volume (or a colored volume over a B/W one) lands later, the identify run lists the now-redundant files in the plan's `supersedes` array. `apply_plan` deletes each superseded file locally and appends its library-relative path to `mediafs_deletions.jsonl`, which the reaper drains to purge the MEGA copy — so the pool never keeps both the volume and the chapters it contains. `validate_plan` confines `supersedes` to `Comics/`, refuses a path the plan itself is writing, and refuses an escape outside the media root.

`prompts/identify.md` states the tiers for **both** traditions (manga volume vs chapter; western TPB/`Compendium` vs issue), gives the vocabulary that marks each, and — the part that makes it reproducible — **anchors the supersede decision to the library**: only supersede files the run is confident the new file genuinely contains, and never anything outside `Comics/`. It also warns that a relaunch is a *separate* series sharing a name, each with its own #1.

Two traps that decide these calls, both settled by reading the archive rather than the filename: a collection runs several times the page count of a single issue (a 153-page `v01` against a 27-page issue), and the **internal page names** disambiguate a relaunch when the filename cannot (`Guarding the Globe v2 001-007.jpg` marks the second series).

#### The volume→chapter map is computed once and cached (`scripts/manga_volume_map.py`)

The supersede call above has one input no filename can supply: **which chapters a volume actually contains**. Asking the identify model per run made that a judgment about an arbitrary library snapshot instead of a bibliographic fact. `manga_volume_map.py` resolves each series **once** and caches the answer in `state/manga_volume_map.json`:

* **AniList** (same free, key-less API as `build_comic_franchises.py`) resolves the series identity and canonical title; **MangaDex's `/manga/{id}/aggregate`** supplies the chapters each volume contains. The aggregate is deliberately fetched **without** the English language filter — scanlation chapters routinely carry no volume tag, so the English view of a series is often `none`-only while the full aggregate has the tankoubon volumes, and chapter numbers are language-independent.
* Chapter numbers are stored as **SETS, never min/max**: manga numbering has gaps, and a range would claim a chapter the volume does not hold. Fractional keys (`12.5`) are dropped — a library chapter is filed as an integer `cNNNN`, so a fractional key can prove nothing.
* **The identity is the folder CHAIN, not the leaf** (`series_label_for_rel`). Measured on the live shelf: the leaf folder `Restoration` resolved on MangaDex to an unrelated manga named *Restoration* whose v01 contains chapter 1 — which would have purged `Rurouni Kenshin - Restoration c0001.cbz` on evidence about a different series.
* **The AI is the last resort, once per series** (`ai_models`' free chain via `ai_client.complete`), asked only for volumes the providers are silent about *and the shelf actually owns*, and cached with its own confidence marker. It runs in a **kill-bounded subprocess** — `ai_client._post` may pace a rolling rate-limit window for minutes, and an in-process call can wedge a scheduled daemon on a provider's clock.
* **Fail-open everywhere**: any network failure writes nothing and the reconciler purges nothing; a totally failed lookup parks itself for a day (`retry_at`) so an outage neither becomes per-tick AI spend nor freezes the series for the full TTL. TTL is 75 days.

#### Chapters yield to volumes on a schedule (`scripts/chapter_volume_reconcile.py`, daemon `com.mikeyferguson.chapterreconcile`)

The deterministic half of the hierarchy. It enumerates the manga shelf (pool inventory + mount — both unreadable means *nothing is judged*), intersects the cached chapter sets with owned volumes, and calls the **same** `library.supersede_paths` `apply_plan` Phase 4 uses: local unlink through `MEDIA_ROOT` plus a `mediafs_deletions.jsonl` line for the reaper, then `dbhook.record_purge` for the rows. A second deletion implementation is how one path ends up purging the pool and the other doesn't.

A chapter is purged **only when all five hold**:

1. the covering volume's file is verified present (both tiers come from the same enumeration);
2. that volume's map is authoritative — MangaDex directly, or an AI set at/above `AI_MIN_CONFIDENCE`;
3. the chapter number is in that volume's set;
4. no keep rule applies (`state/manga_chapter_policy.json`, per series: `keep_volumes` default, `keep_chapters`, `keep_all`);
5. a **colored volume never authors a chapter purge** (colored editions number differently); it may supersede a same-numbered grey volume instead.

Everything else keeps and reports why. `scripts/audit_volume_chapter_coverage.py` is the read-only census — same enumeration, same `plan_decisions`, so its numbers cannot disagree with an apply — and it prints `leftovers` (covered chapters still on the shelf), `unmapped` volumes, and `uncovered` chapters. **Run the census before any `--apply`.**

Two triggers, and neither blocks an ingest on the network:

* **After a plan files a manga volume**, `ingest._advance_verify` (and the chunked wave's per-entry verify, and direct ingest) calls `after_plan`, which reconciles that one series from the **cache only**. A miss/stale map queues a refresh in `state/manga_map_refresh_request.json` and returns.
* **The 6-hourly launchd one-shot** (`--scheduled`) drains queued refreshes, refreshes stale maps for series holding both tiers, then applies. That is the intermittent scan.

#### The watch folder is a local scratch area

The watch folder (`~/Downloads/DirectIngest/`) is on the local fast disk, not iCloud, so there are no Finder droppings to prune and no quota to leak. Sources are deleted as soon as they are filed; `.failed/` and `.skipped/` are the only things that can accumulate, and both are inspected by a human rather than hoarded.

### Curated playlists resolve against the **mount**, not the local store

`playlist.py` builds Jellyfin playlists from the manifests in `state/playlists/`. Because media now lives on drives or is pool-only (not necessarily under the local store), path resolution and Jellyfin item-ID mapping use `LIBRARY_ROOT` = the `mediafs` mount (`~/MediaLibrary`), where the whole library appears. If playlists ever come up empty, that's the tell they were resolving against the wrong root — rebuild with `python3 playlist.py` (needs `JELLYFIN_URL`/`JELLYFIN_API_KEY` in env). Manifests are the source of truth and are safe to re-push idempotently.

### The reaper is queue-only (safe with keep-and-upload)

`reap.py`'s main loop calls **only** `drain_deletions_queue()` — it purges a title from the MEGA pool **only** when it is deleted **through the mount** (an `unlink` that `mediafs` records in `MEDIAFS_DELETIONS_QUEUE`). It does **not** diff the drive for "vanished" files, so an unplugged/wedged drive or an uploaded-then-freed local copy never triggers a purge. This is what makes it compatible with keeping local copies transient.

### Drive ingest (`drive_ingest.py`, daemon `com.mikeyferguson.driveingest`)

Plug in a drive that already has media on it (a friend's flash drive, an old disk) and this daemon organizes those pre-existing files into the same `Media/{Shows,Movies,Comics}` layout and naming used everywhere else — via the same headless AI identify pipeline the torrent flow uses. Once organized under `<drive>/Media/`, the drive becomes a first-class library drive: mediafs serves it and Media-Syncer uploads its contents to the pool (so it's backed up and the drive is disposable).

Each cycle, for every attached external volume it finds top-level entries holding loose media **not** already under `<drive>/Media/`, runs `identify.run_identify` on each, and **moves** each planned file to `<drive>/Media/<dst_rel>` (same-volume rename — instant), then prunes emptied source dirs and drops a `.media-library` marker.

Safety: only **media** files are ever moved (never other data on the drive); files are moved, never deleted; the boot volume and Time Machine disks are skipped; and a drive carrying a **`.no-media-library`** marker is skipped entirely (drop that file on any drive you don't want auto-organized/uploaded). Installed by `startup.sh` alongside the direct-ingest daemon.

### YouTube ingest (`~/Developer/Media-Fleet/YouTube-Downloader`, daemon `com.mikeyferguson.youtubesync`)

<a name="non-torrent-sources"></a>The fourth source, and it lives in its **own repo** while running **this** pipeline. It discovers every playlist on the YouTube account — the ones created *and* the ones saved from other people, plus Liked videos, excluding Watch Later and History — downloads new videos in space-bounded waves onto the Downloads volume, and then hands them to `identify → validate_plan → apply_plan → verify_applied` exactly as direct-ingest and drive-ingest do. Nothing about placement is reimplemented there; it imports `library`, `identify`'s conventions, `playlist_watch` and this `config`, so a YouTube video is named, staged, published, verified and uploaded identically to a torrent. (Because both repos would otherwise define `config`, its own settings module is called **`ytconfig.py`** — this repo's modules must keep getting *their* `config` from a plain `import config`.)

Three things it needs that the torrent flow does not, and where they landed:

* **AI-authored metadata, always.** Nothing on YouTube is on TMDB or TheTVDB, so every plan is `owned: true` and its own prompt (`prompts/youtube_identify.md`, in that repo) writes per-episode titles and plots that `apply_plan` locks. Same mechanism as One Pace.
* **A standalone film needs no TMDB id.** A feature-length documentary from a playlist is a real `Movies/` title with no provider entry, which is what the **owned movie** branch in `validate_plan` exists for (§ *Identification*, above). That is the only change this made to Torrent-Ingest.
* **Short audio tracks are not library media.** An OST rip or character theme is routed *out* of the pipeline entirely, to the flat iCloud `Soundtracks/` folder, decided by a length gate plus a title prior plus one batched classify call. No plan, no `.nfo`, no pool upload.

Its routing is deliberately not "one playlist = one show, always": a video can extend a show **already** in this library, or become its own `Movies/` title, or be skipped as junk. It needs no credentials at all: every playlist it tracks is public, registered once with `--add-playlist`, so there is nothing to expire. See that repo's README for the wave/dedupe details.

**This is enforced, not just documented.** `contract.py` exercises the plan API for real — it asserts that an owned show episode and an owned movie *without* a `tmdb_id` both validate, that `validate_plan` still populates the `_src_abs`/`_dst_abs`/`_src_size` bookkeeping `apply_plan` needs, and that six guards still **reject** what they must (a plot-less owned movie or episode, an un-owned movie with no id, a destination outside `Shows/Movies/Comics`, one escaping the media root, a source outside the download). It also catches the `playlist.MOVIES_DIR == config.MOVIES_ROOT` eviction trap that once silently broke every movie playlist token.

It touches nothing: `validate_plan` never writes, so the fake destinations it uses are inert, and `apply_plan` is never called. It runs at `ingest.py` startup and in `startup.sh`, reported but **never fatal** — a broken cross-repo contract must not stop torrents from ingesting. Run it directly with `python3 contract.py` (exit 1 = broken). The YouTube ingest calls the same check in its own preflight and *does* refuse to run on failure, since its plans would all be rejected anyway.

**What this means for changes made HERE.** That repo is a live consumer of this one's internals, so three things in this repo are now load-bearing beyond it:

* **`library.validate_plan` / `apply_plan` / `verify_applied` are a public API.** The YouTube ingest builds plans and calls them directly, so a change to the plan schema, to `_dst_abs`/`_src_abs`/`_src_size` bookkeeping, or to the owned-episode/owned-movie validation rules breaks it silently — its plans simply start failing validation. The owned-movie branch in particular exists *for* it (§ *Identification*).
* **`config.MEDIA_ROOT` and friends are imported, not copied.** That is deliberate — a second definition of where the library is, or of how a show folder is named, is exactly how two ingest paths drift into filing the same show two different ways. Moving the library root only needs changing here.
* **`playlist_watch.consider_new_episodes` is called after a YouTube placement**, so a YouTube-sourced episode of an auto-extended show is judged like any other.

Conversely, that repo defines its own settings in **`ytconfig.py`, never `config.py`** — because every module here does a plain `import config` and must keep getting *this* one. Do not "tidy" that name.

Its most confusing failure mode is worth knowing so you do not debug it here by mistake: the YouTube cycle **refuses to run at all** while Google traffic egresses through the VPN exit node, so "it silently ingests nothing" is usually the root split-tunnel daemon in Media-Syncer being missing or stale, not anything in either ingest. `route -n get 142.250.72.14` returning a `utun*` interface is the tell.

## Purged titles stay purged (`scripts/purge_sweeper.py`)

Purging a title deletes its media, but the directory survives on both trees until the reaper
has purged every pool copy and pruned the inventory — hours, for a large purge. Jellyfin's
libraries point at the mediafs **mount** and run with `SaveLocalMetadata: True`, so every
scan in that window re-indexes the surviving directory as a series and writes `tvshow.nfo` +
`folder.jpg` back into it. The write lands in `~/Media`, the directory is non-empty again,
and the next scan finds it again. The owner sees purged shows in Infuse and reasonably
concludes the purge failed.

Turning `SaveLocalMetadata` off would break the loop and also break the fleet: the
per-episode `.nfo` sidecars are the metadata store, they survive eviction when the video does
not, and `media_doctor` depends on Jellyfin re-saving them after a POST. So the sidecars stay
and `purge_sweeper.py` removes the shells instead — deleting the Jellyfin row first, so a
scan racing it cannot re-save a sidecar into a directory that is about to go.

**It judges by the MOUNT, never the local tree.** A show whose video was evicted has no local
media and is perfectly healthy — 511 of 518 show folders were in that state when this was
written — so local emptiness proves nothing. The pool is the truth and the mount is the
pool's view. Anything holding a single media file on the mount is left alone, and an
unreadable directory counts as non-empty, because an unavailable mount and an empty one are
indistinguishable and "empty" invites destruction.

Runs every 15 minutes as `com.mikeyferguson.purgesweeper`; `--apply` to act, report-only by
default.

## Purged titles are retired from the queue too (`retire_blocked`)

`state/blocklist.json` stops a purged title being re-filed by ingest. (It also used to stop the searcher re-*searching* for it; with discovery removed on 2026-09-10 the ingest half is all that is left, and it is the half that matters when the owner re-drops something by hand.) It
says nothing about work already in this repo's queue, and **this queue is self-perpetuating**:
`_advance_chunked` re-adds any chunked pack that is missing from qBittorrent, because absence
normally means a crash or a restart rather than a deliberate deletion. So deleting a purged
title's files put it straight back — after one reboot qBittorrent went from 0 to 15 torrents
within minutes, every one re-added from a journal record, several of them titles that had
just been purged.

`cycle()` therefore calls `retire_blocked()` **before the advance sweep**, once per cycle: any
active record whose normalized title matches the blocklist is marked `REFUSED` (a deliberate
decline, not a failure) and its torrent and files are removed. `REFUSED` is terminal, so the
record is never re-adopted and never re-registered. The blocklist is re-read every cycle and
never cached — the owner edits it when they purge, and a stale copy is exactly the failure
this guard exists to prevent.

Matching is whole-phrase, the same rule the searcher uses, so a blocked `Nisekoi` catches
`Nisekoi S1+S2 [BDrip]` while a bare word can never swallow an unrelated title.

### `.ignore` for titles still draining

A blocked title whose pool copies have not drained yet is **not** a shell — it still has
media — so the sweeper cannot remove it, and Jellyfin re-indexes it on every scan for as long
as the drain takes (hours, for a large purge). `_mark_blocked_pending()` drops a `.ignore`
marker in those directories, which Jellyfin honours by skipping the folder outright. The
marker is a dotfile, so `_has_media` never counts it and the directory still reads as a shell
once the drain finishes and can then be removed normally. Idempotent, and it is what makes a
purge look finished to the owner on the same day it is run rather than the next morning.

### The sweeper only ever removes BLOCKLISTED titles

This is the load-bearing safety rule, and it was learned the expensive way. An earlier
version removed **any** directory that looked empty on the mount — and "looks empty" is not
the same fact as "was purged". A mount directory reads as empty whenever its
`remote_inventory.json` keys are momentarily absent (an inventory rewrite, a mediafs reload,
a partial read), and removing it through the mount does not merely tidy a shell: mediafs
turns that unlink into **deletion-queue entries** and the reaper then purges every pool copy.

That is how Boruto, Ghost in the Shell SAC, Dragon Ball, Dragon Ball Super, Legend of the
Galactic Heroes, Vinland Saga, Laid-Back Camp, One Pace and eleven other **kept** shows —
1,205 files — ended up queued for destruction by a script whose entire job was tidying up.
They were pulled back out of the queue before the reaper reached them, so nothing was lost,
but the margin was luck rather than design.

An empty directory that is **not** blocklisted is a SYMPTOM, never an instruction. The
sweeper prints it and leaves it completely alone.

### Auditing the blocklist itself, in both directions

`purge_sweeper` matches a blocklist row against a title by **normalized prefix**, and that
one rule fails in two opposite ways. Each has its own audit, because each is silent in a
different direction and neither can be seen by reading the file.

**Matching too MUCH** — `scripts/audit_blocklist_collisions.py`. A row that is a bare
franchise word matches the original series as well as the remake that was purged: `Ranma ½`
hides `Ranma ½ (1989)`, `Sailor Moon` hides `Sailor Moon (1992)`. The sweeper then writes a
`.ignore` into the original's folder and Jellyfin stops indexing 479 episodes the owner
owns. Every component is individually correct — the blocklist, the library, the reaper,
Jellyfin — and only the JOIN is wrong, which is why prose in a runbook never caught it.
Reports titles still HOLDING MEDIA that a row matches.

**Matching too LITTLE** — `scripts/audit_blocklist_orphans.py`. A row that matches *nothing*
is indistinguishable from no row at all, and both look exactly like a completed purge. The
row `'Saiki? no'` — a mangled fragment of the romaji — normalized to `saiki no`, matched no
title anywhere, and hid a stalled purge for weeks: 56 of 83 files queued, 0 purged, 27 manga
volumes untouched. It classifies every row as **LIVE** (matches the mount or `wants.json`),
**DRAINED** (matches only history — `library.db`, `reap_purges.log`, the deletion queue — so
the row is correctly guarding a finished purge), or **ORPHAN** (matches nothing, ever).

Two things about the orphan audit are deliberate and worth keeping:

* It reads `wants.json` **raw**, never the searcher's old `ingest.load_wants()`. That helper
  went with the searcher on 2026-09-10, and `wants.json` is now an inert file kept only so
  this audit has both sides of its comparison. `load_wants()`
  applies the blocklist, so asking it "does a want match this blocklist row" can only ever
  answer *no*, for every row, including the ones working perfectly — the §4.148/§4.153 bug
  in a new hat, a filter that structurally cannot be true reporting a clean result.
* **An orphan is not automatically a defect**, and the tool says so loudly. The purge runbook
  *requires* adding a purged title's romaji form, and a romaji row for content only ever
  filed in English matches nothing by construction; measured 2026-09-05, that is most of the
  31 orphans. A string near-miss pass meant to separate those from real typos was written,
  measured, and **deleted**: it paired `Rezero` with `Ga-Rei: Zero` and `Shingeki no Bahamut`
  with `Shokugeki no Soma` while missing every genuine pair, because romaji and English share
  almost no characters. It was wrong in both directions at once. The audit narrows 581 rows
  to 31 and states the one fact it can prove — which rows normalize identically — and leaves
  the reading to a person.

Both are **report-only**. Whether a held title should be un-blocked, and what a typo'd row
meant, are content decisions, and the sweeper section above is the standing reminder of what
happens when a cleanup tool makes that call itself.

## The fleet fixes what it safely can (`scripts/fleet_doctor.py`)

`fleet_health` detects; **`fleet_doctor` acts.** Before it existed, every finding waited
for a person to read a report and type the fix, which is the loop this closes.

**How it matches.** `fleet_health` writes `state/fleet_health.json` from the *same*
collection pass as `fleet_health.txt`, so the human report and the machine one cannot
disagree. Each finding carries the **name of the check** that produced it, and remedies in
`remedies.py` are registered against those names. That is the entire matcher for a known
finding — no prose parsing. The messages are written for a phone screen and get reworded
whenever they read badly; a matcher keyed on wording would turn every such edit into a
silent behaviour change, which is §4.26's fault exactly — reading the label instead of the
thing.

**What the AI is allowed to do.** Exactly one thing: for a finding whose check name no
remedy claims, a free model is shown the fault and the **closed list of remedy ids** and
asked to pick one or answer `NONE`. Its reply is then *looked up in the registry*, and
anything that is not a known id becomes `NONE`. So the worst a wrong, confused or hostile
answer can do is run a remedy that is already written, reviewed and tested — or nothing.
It cannot author an action, name a path or compose a command; there is nowhere in the
program to put one. The model proposes, the harness disposes, made structural rather than
promised.

It spends through `config.aux_ai_attempts()`, so it yields to identify. A self-healer that
drained the account the filing path needs would create more faults than it closed.

**Every remedy obeys four rules**, and `scripts/test_remedies.py` enforces them:

1. **Re-check before acting.** A finding is a snapshot up to five minutes old; `detect()`
   re-establishes it *now*.
2. **Prove it worked.** `verify()` re-reads the world. A repair tool once *reported* 250
   repairs it never made (§4.20), so "apply returned True" is not an outcome.
3. **Idempotent.** The doctor loops.
4. **Non-destructive, structurally.** The test reads the *source* of every remedy and
   fails the build on `rmtree`, `unlink(`, `apply_plan`, `mediafs_deletions`, `DELETE
   FROM`, a write to a media root, or a restart of `torrentreap`/`torrentsearcher` — with
   a control proving the scanner can still catch one. Source inspection rather than
   review, because this fleet's history is a list of plausible, well-reviewed actions that
   destroyed things.

**"Only you can do this" is a real answer.** Owner remedies carry the *specific* reason
automation is wrong — the acceptance gate must not be cleared by a faked heartbeat; the
library review list is curation, not a bug list, and 82 of its 86 items were misnumbered
sets whose `.nfo` names the right episode. Saying that plainly beats inventing an
automated action that half-works.

**The YacReader pair** is the newest auto pair, and the reason the reader's freshness is
now a closed loop even when the supervisor itself is the broken half:
`refresh_yacreader` runs `scripts/yacreader_rescan.py --apply` (flags under the index
lock, then the refresh marker) when the flags have drifted or a long-quiet index is
missing shelf files, and `repair_yacreader_index` runs
`scripts/yacreader_index_repair.py --apply` when a folder row would SIGSEGV the loader
(`FolderModel::createModelData` dereferences a missing parent). A *damaged* index has an
owner remedy instead, because restoring a backup is not a decision an unattended doctor
makes: it requires the newest backup that PASSES `integrity_check`, and that census is a
human read.

Its report is `fleet_doctor.txt`, beside `fleet_health.txt` in the iCloud Torrents folder.

```bash
python3 scripts/fleet_doctor.py --list            # the whole registry, and what it may restart
python3 scripts/fleet_doctor.py --once --dry-run  # decide, apply nothing
python3 scripts/fleet_doctor.py --once            # one real pass
```

## Every daemon comes back (supervision, and its two blind spots)

"It restarts if it dies" turned out to be three different questions, and the fleet only
had an answer to the first.

**1. The process exits.** `KeepAlive` in the plist. Every resident daemon has it; the
periodic ones (`StartInterval` / `StartCalendarInterval`) do not need it, because launchd
re-runs them on schedule and a death simply means the next tick starts a fresh one.

**Two daemons deliberately have no `KeepAlive`, and adding it would break them:**

| daemon | why not | what covers it instead |
|---|---|---|
| `mediasync` | the reaper *kills* it while purging deletions; `KeepAlive` would relaunch it mid-purge and fight the reaper | `mediasync_watchdog`, which relaunches only when the reaper's pause marker is absent |
| `torrentreap` | it has `KeepAlive` and keeps it, but nothing may `kickstart -k` it: a restart discards the in-flight MEGA account probe that is the purge's whole cost, and a drain has run for days | left alone, by name, in `remedies.RESTARTABLE_LABELS` |

**2. The job vanishes from launchd entirely.** This is the blind spot `KeepAlive` cannot
cover, because launchd only restarts jobs it still knows about. A label that was booted
out, whose plist was deleted from `~/Library/LaunchAgents`, or that was never installed
after a fresh checkout, is simply absent — and `launchctl list` stops mentioning it, so
nothing reads as wrong. Not hypothetical: the searcher was removed exactly this way and
nothing noticed for four days.

`check_daemons_loaded` closes it. The expected roster is **derived from the repos' own
plists** (`~/Developer/Media-Fleet/*/com.mikeyferguson.*.plist`), not hand-listed — a hand-maintained
roster is a second copy of the truth, and two records of one fact drifting apart while
both look healthy is this fleet's most expensive recurring failure. Adding a plist to a
repo supervises it automatically. `remedies.NOT_EXPECTED` carries the exceptions with
their reasons (currently `splittunnel`, an opt-in *root* LaunchDaemon outside the user
domain).

`fleet_doctor` then bootstraps anything missing — **except the reaper**, which is reported
as an owner ACTION instead. The asymmetry is deliberate and worth stating: a reaper that
is *not* running only means deletions queue up on disk, which is harmless and reversible.
A reaper started at the wrong moment drains `mediafs_deletions.jsonl`, the only path that
purges a MEGA copy, and that is not.

**3. The job hangs without exiting.** Still uncovered, and named here rather than
half-solved. launchd will not start a second instance of a periodic job while the first is
running, and it has no timeout — so a wedged `youtubesync` or `purgesweeper` silently
never runs again. Closing it needs per-job heartbeats that do not exist yet.

Two plists that were running but existed in **no repo** (`icloudcensus`, `purgesweeper`)
are now committed, so they can be reinstalled rather than only rediscovered.

```bash
python3 scripts/fleet_doctor.py --list     # the registry, and what it refuses to restart
launchctl list | grep com.mikeyferguson    # 29 agents; compare against the repos' plists
```
