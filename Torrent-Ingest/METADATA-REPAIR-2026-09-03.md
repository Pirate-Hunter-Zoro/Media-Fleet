# Metadata repair, 2026-09-03 — what was fixed by hand, and why the free AI could not

The owner's instruction: *"go through all of the fucked up metadata noticed by the fleet
health scanner and one-off fix it. Try to use the free AI tools the fleet uses for
renaming, and if that doesn't work, make a note and do the renaming yourself — write down
where you were needed and why. Then later we'll isolate that issue and have you upgrade
the free tools to do their job."*

This is that note. **§1 is the list of places the free tools had to be replaced by hand and
the precise reason each one failed** — that is the input to the "upgrade the free tools"
session. §2 is what was actually repaired — including §2.4, an attempt that had to be reverted and
why. §3 is what was deliberately left alone.

---

## 1. WHERE THE FREE TOOLS HAD TO BE REPLACED, AND WHY

Four independent faults. Every one of them was measured this session, not inferred, and
each is fixable. **They compound**: any one of them alone stops the healer, and all four
are live at once, which is why `media_doctor` has been "running" for weeks while
`library_health.txt` never shrank.

### 1.1 ★★★ `ai_runner.py` has no provider fallback, so one capped model kills every judgment call

`discovery.complete()` (Torrent-Searcher) walks the whole nine-attempt chain in
`config.enabled_ai_attempts()` and skips a provider that cannot run. **`ai_runner.py`
— which is `config.AI_BIN`, the front end every daemon spawns for an agentic judgment
call — does not.** It takes a single `--provider`/`--model` and calls
`ai_client.run_agent` once. `media_doctor.escalate()` and `media_doctor.budget_available()`
both invoke it with *no* provider, so both fall back to `ai_client.DEFAULT_MODEL`
(`nvidia/nemotron-3-super-120b-a12b:free` on OpenRouter). The moment that one model hits
its daily cap, every escalation in the fleet dies.

Measured this morning, probing all nine attempts directly:

```
FAIL  openrouter/nvidia/nemotron-3-super-120b-a12b:free   http 429 free-models-per-day
OK    openrouter/minimax/minimax-m3:free
FAIL  openrouter/minimax/minimax-m2.7:free                (timeout on a second try)
FAIL  openrouter/dots-studio/dots-3-note-preview:free     http 429
FAIL  openrouter/cohere/north-mini-code:free              http 429
OK    groq/openai/gpt-oss-120b        <- works; only 413s on identify's LARGE prompts
OK    groq/openai/gpt-oss-20b
FAIL  cloudflare/@cf/openai/gpt-oss-20b                   http 429 daily neurons
FAIL  cloudflare/@cf/google/gemma-4-26b-a4b-it            http 429 daily neurons
```

**Three of the nine were answering the whole time.** `ai_runner.py -p --provider groq
--model openai/gpt-oss-120b` returns `OK` immediately; the agentic loop is fine. Nothing
was broken except that nobody asked the second provider.

> **The fix is small and it is the highest-value item here.** Give `ai_runner.py` the same
> walk `discovery.complete()` already has: iterate `config.enabled_ai_attempts()`, try each
> in turn, and only report unavailable when *every* attempt fails. Exit code 2 keeps its
> meaning; it just stops being reached after one 429.

Corollary worth stating plainly: **Groq is not structurally unavailable.** §5 item 6 of the
hand-off says it "can never serve identify" because it answers `413 Request too large`.
That is true *of identify's prompt size only* — a per-org TPM ceiling. Groq answers a small
prompt fine, which means it is a perfectly good provider for renaming, plot lookups and
every other small judgment call. Only `identify` needs the listing cap.

### 1.2 ★★ The budget gate stands the whole fleet down when ANY one account caps

`config.ai_budget_healthy()` reads one stamp, `state/ai_budget_capped_at`, written by
`note_ai_account_capped()` whenever the identify chain sees an account-level cap. All
auxiliary AI — media_doctor's escalation, the playlist curator, the searcher's audits —
then stands down for `AI_BUDGET_BACKOFF_SEC` (2 h). The stamp is **global, not
per-provider**. So OpenRouter and Cloudflare capping today disabled the healer even though
Groq was answering.

The gate's own rationale (config.py ~1515) is right about the constraint being a daily
request count rather than a clock. It is wrong to make that constraint account-wide when
the fleet deliberately runs three independent accounts. **Stamp per provider, and stand
down only when every enabled provider is capped.**

### 1.3 ★★ The escalation gives the model a COUNT, not a LIST — so it burns its budget rediscovering what the doctor already knew

`ESCALATION_PROMPT` says *"6 episode(s) with release-group/blank titles ... needs a
lookup"* and hands over a folder path. It does not say which six. The doctor knows exactly
which six — `_title_is_janky` just told it — and throws that away.

So the run opens with pure discovery. Instrumented (`--verbose`), against a working model:

```
turn 1: ListDir -> 515 chars
turn 2: Read    -> 2882 chars
turn 3: ListDir -> 15334 chars
turn 4: Read    -> 2726 chars
turn 5: Grep    -> 11345 chars
turn 6: Grep    -> 19094 chars
result: "Now I see clearly. There are many junk titles... Wait, the task says
         '6 episode(s)'. Let me look more carefully."
```

Six turns and ~52 KB of tool output to still be orienting, on a show with 80 episodes
across 15 seasons. At `--max-turns 80` the same run returned **empty stdout with exit 0**
after five turns — the context filled with directory listings and it produced nothing.

`escalate()` does not check the result, so a silent empty run is indistinguishable from
success. It is called twice, `escalate_n` reaches `MAX_ESCALATIONS_PER_SIG = 2`, and the
show is **never retried again**. Every show in §2 was sitting at `escalate_n: 2` — the
doctor had permanently given up on all of them.

> **Fix:** interpolate the actual episode list (season, number, current title, filename,
> whether the plot is blank) into the prompt. It is free, it is already computed, and it
> removes the entire discovery phase. Then check the run's `result`/`is_error` and do not
> burn an `escalate_n` on a run that returned nothing.

### 1.4 ★★ `ffprobe` is broken on this box, so the `Probe` tool cannot work

```
dyld: Library not loaded: /opt/homebrew/opt/x265/lib/libx265.216.dylib
  Referenced from: /opt/homebrew/Cellar/ffmpeg/9.0.1/bin/ffprobe
```

Every `ffprobe` invocation dies. `config.ai_env()` exists specifically so the agent's
`Probe` tool can find `ffprobe` — and `Probe` has been useless for some time. The damage is
visible in the sidecars: `.nfo` files written before ~2026-06-27 carry full
`<streamdetails>` (codec, bitrate, `durationinseconds`); ones written 2026-08-27 carry only
language tags. `brew reinstall ffmpeg` (or `brew link x265`) is the whole fix, and it should
be a `verify_fleet.sh` check — a silently-degraded probe is exactly the class of failure
this fleet keeps getting bitten by.

### 1.5 ★ `_title_junk_markers` false-positives on real titles containing "8-bit"

`JUNK_TITLE_STRONG_RE` includes `\b(10|8).?bit\b` for bit-depth tags. Young Sheldon
S02E08 is genuinely called **"An 8-Bit Princess and a Flat Tire Genius"** (confirmed on
TVMaze), and it matches. So it is reported as needing a lookup on every pass, forever, with
nothing to fix — the identical failure the comment right above that regex already
describes for `Magnum Opus` and `The Bad Batch`.

> **Fix:** demote the bare bit-depth marker to WEAK, or require it adjacent to a codec or
> resolution token (`10bit x265`, `1080p 8bit`) rather than free-standing in prose.

### 1.6 What the AI was NOT needed for

Worth recording, because it changes the design: **none of the renames in §2 needed a
language model.** Every one was settled by TVMaze — free, key-less, already in the repo as
`epguide.py` (§4.51) — plus the library's own data. A deterministic, anchored TVMaze pass
(§2.4) fixed 250 episodes with no AI at all. The AI's real job here is the residue: the
seasons where no anchor exists, and the identity calls. Spending the whole free budget on
work a keyless API answers exactly is why the budget is gone by 06:00.

---

## 2. WHAT WAS REPAIRED

All of it touched **only** `.nfo` sidecars and Jellyfin fields, both locked so a future
refresh cannot revert them. **No media file was created, moved, renamed or deleted.**
Sidecars live on the SSD (`~/Media/Shows/...`); the videos themselves are mostly evicted to
the MEGA pool and were never opened.

Backups, both restorable with `tar xzf` from `~/Media/Shows`:
`Torrent-Ingest/state/nfo-backup-preclaude-20260903.tgz` (Fate, Monogatari S15, Dr. STONE
E38) and `nfo-backup2-preclaude-20260903.tgz` (Fairy Tail, Dr. STONE, Monogatari).
`state/doctor_state.json.bak-preclaude-20260903` is the doctor's escalation state before
`Monogatari`'s counter was reset to give the free tool an honest third try.

### 2.1 ★★★ Fate/Grand Order — Babylonia: 10 episodes carried a DIFFERENT SHOW's titles and plots

The health report flagged **3** episodes here. The real number was **13**, and the fault is
structural.

Two series in this library have a `/` in their title: `Fate/Grand Order - Absolute Demonic
Front: Babylonia` and `Fate/kaleid liner Prisma Illya`. The slash was taken as a path
separator at filing time, so both collapsed into one phantom parent folder:

```
Shows/Fate/                                   <- phantom series folder, tvshow.nfo says Prisma Illya
├── tvshow.nfo                                   (tmdbid 63576 = Fate/kaleid liner Prisma Illya)
├── Grand Order - Absolute Demonic Front: Babylonia (2019)/
│   └── Season 01/Fate/                        <- and again inside the filename
│       └── Grand Order - ... - S01E01.mkv
└── kaleid liner Prisma Illya (2013)/
    └── Season 03/Fate/
```

Jellyfin saw **one** series, identified it as Prisma Illya, and applied Prisma Illya's
episode titles and plots to Babylonia's episodes. Babylonia S01E01 read *"Birth! A Magical
Girl!"* with the plot *"Illya has been chosen by the Kaleidostick Ruby..."*. E00/E11/E12
kept their raw fansub filenames only because Prisma Illya's season has no episode there —
**and those three were the only ones the health scanner ever reported.** The ten that were
confidently, plausibly wrong looked healthy to it.

Repaired all 13 from TVMaze (show id 43297), titles and plots, locked:

| | was (Prisma Illya's, or fansub junk) | now (Babylonia's) |
|---|---|---|
| S01E00 | `[μtw-Kaleido] Fate／Grand Order ... - 00 (BD 1080p)` | Episode 0 - Initium Iter |
| S01E01 | Birth! A Magical Girl! | Absolute Demonic Front: Babylonia |
| S01E02 | Who? | Fortress City: Uruk |
| S01E03 | Girl Meets Girl | The King and His People |
| S01E04 | We Lost | Welcome to the Jungle |
| S01E05 | There are two options? | Gilgamesh's Journey |
| S01E06 | A Blank, and the End of Night… | The Tablet of Destinies |
| S01E07 | Triumph and Escape | Diversionary Operation |
| S01E08 | The Normal Girl has Returned | The Mother of Demonic Beasts |
| S01E09 | End it Here | Good Morning, Goddess of Venus |
| S01E10 | Kaleidoscope | Hello, Goddess of the Sun |
| S01E11 | `[μtw-Kaleido] ... - 11 (BD 1080p)` | Temple of the Sun |
| S01E12 | `[μtw-Kaleido] ... - 12 (BD 1080p)` | Death of the King |

> **STILL BROKEN AND NOT FIXED HERE — the folder structure.** Two unrelated shows still
> live in one Jellyfin series whose poster, plot and identity are Prisma Illya's. Splitting
> them means **moving media**, MEGA-side, which §7 puts behind stopped daemons and the
> `refile_season.py`-class tooling — and the owner should pick the replacement names
> (`Fate - Grand Order - Absolute Demonic Front - Babylonia (2019)` and `Fate - kaleid liner
> Prisma Illya (2013)` are the obvious ones). It is out of scope for a metadata pass.
>
> **The generator needs the same fix.** Whatever writes a show folder from a title must
> sanitise `/` (and `:` is already being handled inconsistently — note `Babylonia (2019)`
> keeps its colon). Until then the next slashed title does this again. Candidates already
> in the library: anything `Fate/...`, `Steins;Gate` is fine, but `Yu-Gi-Oh!` and
> `Re:Zero`-style colons are worth auditing at the same time.

### 2.2 ★★ Monogatari — Zoku Owarimonogatari S15E01–06 had the filename as the title

All six read `Zoku Owarimonogatari - S15E01`…`06`. Zoku Owarimonogatari is the six-episode
*Koyomi Reverse* arc; TVMaze carries it as Monogatari season 5. Renamed to **Koyomi
Reverse - Part 1 … Part 6**, locked.

Independently corroborated afterwards: the anchored TVMaze pass (§2.4), which had no
knowledge of this edit, derived `library S15 -> provider S05, offset +0, 6/6 anchors
matched, 0 contradictions` on its own.

### 2.3 ★★ Dr. STONE S04E38 — not a missing title, a DUPLICATE, and the identify log says why

Title was `[Erai-raws]_HEVC_CR`. It is not renameable in the ordinary sense: **Dr. STONE
season 4 has exactly 37 episodes** (TVMaze), and the library holds 38 files.

The source is `[Erai-raws] Dr Stone - Science Future Part 3 - 12`. The season's own scraped
air dates split Science Future cleanly:

```
Part 1 = E01–E12   2025-01-09 .. 2025-03-27
Part 2 = E13–E24   2025-07-10 .. 2025-09-25
Part 3 = E25–E37   2026-04-02 .. 2026-06-25
```

So *Part 3 episode 12* is **E36, "Why-Man" (2026-06-18)** — and the scraper had already
stamped this very file `<aired>2026-06-18</aired>`, which is E36's date. Two independent
lines of evidence agree: **S04E38 is a second copy of S04E36 from a different release
group.**

`state/decisions.log` records exactly how it got there, in identify's own words:

> *"The searcher settled this file to **S4E38**, which is the next contiguous episode in
> that existing Season 04 — no new season, no season-number gap. … its seasoned numbering
> resolves at the provider, so Jellyfin scrapes the episode metadata itself. No
> `episode_title`/`plot` needed."*

It placed the file at the next free number rather than resolving `Part 3 - 12` against the
cour structure, then declined to write a title because it expected the provider to supply
one — for an episode that does not exist. **That is the metadata-corruption mechanism, in a
single record.** A guard worth adding: when a plan puts an episode *beyond* the provider's
known episode count for that season, do not accept "the provider will fill it in."

Action taken: title corrected to **Why-Man** with E36's plot, locked, so it stops reading as
release-group junk. **The duplicate file itself was left in place** — deleting library media
is the one thing the ground rules forbid, and which copy to keep is the owner's call. See §3.

### 2.4 ✗ REVERTED — the synopsis pass filled ZERO blanks and altered 221 good plots

`scratchpad/fill_synopses.py`. The hard part is that this library does not use provider
season numbers — Monogatari is 15 seasons where TVMaze has 6, Dr. STONE files a whole cour
as one season. A tool that assumes `library S/E == provider S/E` writes the wrong episode's
plot, which is the corruption being cleaned up. So nothing is written until the alignment is
proven:

> For each library season, search every (provider season, offset) pair. Accept one only
> when the episodes that **already** carry a real title agree with the provider under it —
> at least 2 of them, **at least 60% of the season's anchors landing on a real provider
> episode**, and **not one contradiction**. Otherwise skip the season and say so.

The 60% coverage rule was added after the first run: Pokémon Horizons S03 "matched" at
offset −45 on 5 of 28 anchors, because the offset slid the other 23 past the end of the
provider season where they could neither hit nor miss. Agreement without coverage is not
evidence.

**IT DID NOT WORK, AND IT HAS BEEN FULLY REVERTED.** The mapping logic was sound; the
"is this synopsis blank?" test was not. It read `Overview` off the objects returned by
`media_doctor.Jellyfin.episodes()` — and that method requests
`Fields="Path,ParentIndexNumber,IndexNumber"`, so `Overview` is **never present** and the
test was true for every episode. The sidecar guard held (the script itself never
overwrote a populated `<plot>`), but the script also POSTed the provider plot to Jellyfin,
and **Jellyfin's Nfo metadata saver wrote that straight back down into the sidecar** —
defeating the guard through a path that was not anticipated.

Measured against a pre-run backup of all 587 sidecars:

```
0    blank synopses filled          <- the entire point of the pass
221  already-correct plots replaced with TVMaze's wording for the same episode
29   sidecars that only gained <lockedfields>Overview</lockedfields>
```

Nothing was mis-described — the season mapping was anchor-proven, so every replacement
was the right episode's plot — but it was not a repair and it was not asked for.
`scripts/one-off/restore_plots.py` put all 587 sidecars back to their exact pre-run plot
text, reverted the Jellyfin `Overview` on all 250, and **unlocked `Overview`** so a future
scrape can still improve them. Verified: 0 plots differ from the pre-run state.

(One file needed a second pass: Jellyfin's `Overview` is plain text while the `.nfo`
stores it XML-escaped, so restoring the raw `<plot>` text turned `&amp;` into
`&amp;amp;` on Fairy Tail S02E03. Unescape before POSTing.)

**The blank counts in `library_health.txt` were therefore never going to move**, and did
not: Fairy Tail still reports 50, which is correct — 19 in S01 beyond TVMaze's episode
range and 31 in the unmapped S08. That is the honest state.

What the pass *did* establish, and what is worth keeping, is **the seasons it refused**:

```
Fairy Tail   S01 +0 (48/48)   S02 +0 (48/49)   S03 +0 (54/54)   S05 +0 (51/51)   S07 +0 (12/12)
             S00, S04, S06, S08  SKIPPED (no offset agrees with every anchor)
Dr. STONE    S01 +0 (24/25)   S02 +0 (11/13)
             S00, S03, S04       SKIPPED
Monogatari   S01 +0 (2/2)     S08 -> provider S03 (4/4)   S15 -> provider S05 (6/6)
             ten other seasons   SKIPPED
Gundam Build Divers Re:Rise, Cells at Work!, Yu-Gi-Oh! ARC-V / VRAINS / Go Rush!!,
Pokémon Horizons, Creature Commandos, Slow Start, That '90s Show, Digimon Adventure
                                 SKIPPED — no provable mapping
```

Those skips are the honest outcome, not a shortfall. A season where no offset satisfies
every anchor is a season whose numbering genuinely disagrees with the provider, and
guessing there is how `That '90s Show` (§5 item 5) got into its three-way disagreement.
**Each skipped season is a real, separable piece of work** — most of them want a different
provider (TVMaze is thin on long-running anime) or an absolute-vs-seasonal reconciliation.

### 2.5 Gundam Build Divers Re:Rise — the health report's own hypothesis was wrong

`library_health.txt` says of it: *"8 of 9 episode(s) have no synopsis — that ratio usually
means the series is matched to the WRONG provider entry."* It is not. E01 is correctly
`Wandering Core Gundam` with a real plot, matching TVMaze S1E01 exactly; the ids
(tmdb 299094 / tvdb 369139) are right. The scrape simply never completed for E02–E09, which
still carry the *series* name as their episode title.

It was skipped by §2.4 because one correct episode is below the two-anchor floor — correctly
so, on the general rule, though in this case the mapping is obvious. It is a small, safe
follow-up: E02–E09 are TVMaze S1E02–E09 (`Unknown Mission`, `A Place to Protect`, `Wounded
Wings`, `Now Spread Your Wings`, `Hero on the Brink`, `Battered Crown`, `Duty and Illusion`,
`Abyss of Isolation`). It was left undone deliberately rather than bypass the safety rule
for one show.

**And the report's heuristic should be softened**: "8 of 9 blank ⇒ wrong provider entry" is
wrong whenever one episode scraped cleanly, which is exactly the evidence that the identity
is right.


### 2.6 ★★★ TWO PAIRS OF UNRELATED SHOWS WERE MERGED IN JELLYFIN — found by accident, and fixed

This one was **not in `library_health.txt` at all**, and neither affected show appears in
it. It surfaced only because §2.4's tool asked Jellyfin for "Dr. STONE's episodes" and got
back 146 for a show with 96 files.

`/Shows/{id}/Episodes` does not filter on `SeriesId`. It filters on
`SeriesPresentationUniqueKey`, which is derived from the series' **provider id**. Two series
carrying the same provider id therefore serve each other's episodes. Reading the Jellyfin
DB directly:

```
key 355774-en-a656b907eb...  <- 2 series:  'Dr. STONE'                    'The Rising of the Shield Hero'
key 348545-en-a656b907eb...  <- 2 series:  'Demon Slayer: Kimetsu no Yaiba'  'Goblin Slayer'
```

Browsing **Dr. STONE** served 146 episodes: its own 96, plus all 50 of The Rising of the
Shield Hero. Browsing **Shield Hero** served 5. Same shape for Demon Slayer / Goblin Slayer.

The cause is in the sidecars, and it is worse than a single stray id:

| show | `tvshow.nfo` had | correct |
|---|---|---|
| Dr. STONE (2019) | tvdb 355774, tmdb 86031, imdb tt9679542 | **all correct** |
| Demon Slayer (2019) | tvdb 348545, tmdb 85937, imdb tt9335498 | **all correct** |
| The Rising of the Shield Hero (2019) | tvdb **355774**, tmdb **82684**, imdb **tt9054364** | tvdb 353712, tmdb 83095, imdb tt9529546 |
| Goblin Slayer (2018) | tvdb **348545**, tmdb **82684**, imdb **tt9054364** | tvdb 350166, tmdb 82591, imdb tt8690728 |

Note the signature: **Shield Hero and Goblin Slayer carry an IDENTICAL wrong `(tmdb, imdb)`
pair**, and each additionally carries a *different* neighbour's tvdb id. That is one bad
identify run stamping a single candidate onto more than one series, not two coincidences.
Worth hunting for more of the same in `state/decisions.log`.

Ids were cross-checked against two independent free sources before anything was written —
TVMaze `externals` for tvdb/imdb, and Jellyfin's own `RemoteSearch/Series` for tmdb/imdb.

**Repaired**, in this order:
1. Corrected `<tvdbid>`, `<tmdbid>`, `<imdb_id>` (and the matching `<uniqueid>` elements) in
   both `tvshow.nfo` files, `lockdata` true.
2. `apply_identification(..., replace_images=True)` on both Jellyfin series — **images
   replaced deliberately**, because every poster and backdrop had been fetched under the
   other show's identity and no ordinary refresh replaces a filled image slot. That is the
   documented five-day "wearing another show's face" failure.
3. The series keys then diverged, but the **child rows keep the key they were stamped with
   at creation** — 44 season/episode rows were still pointing at the old one. That is
   precisely what `db_guardian.reconcile_series_presentation_keys` exists for, so it was run
   rather than hand-patched: `python3 db_guardian.py --once` →
   *"repaired 44 orphaned season/episode row(s)"*. The daemon was stopped for the run and
   restarted after.

Verified — Jellyfin now matches the disk exactly:

```
Dr. STONE (2019)                       96 episodes / 96 files on disk, 0 foreign
The Rising of the Shield Hero (2019)   50 / 50, 0 foreign
Demon Slayer - Kimetsu no Yaiba (2019) 65 / 65, 0 foreign
Goblin Slayer (2018)                   24 / 24, 0 foreign
0 presentation keys shared by more than one series (was 2)
```

> **Two things the fleet should learn from this.**
>
> **(a) `media_doctor` cannot see it.** Its whole model is "reconcile disk truth against
> Jellyfin truth" per show, and both shows looked internally consistent — Dr. STONE simply
> had *more* episodes than files, which no check tests for. **A duplicate-provider-id check
> is one SQL query** and it belongs in `db_guardian` next to the reconciler it already has:
> `SELECT PresentationUniqueKey FROM BaseItems WHERE Type LIKE '%Series%' GROUP BY 1 HAVING
> COUNT(*) > 1`. Add "Jellyfin serves more episodes than the folder has files" to the doctor
> as well.
>
> **(b) Anything that reads `/Shows/{id}/Episodes` can be handed another show's episodes.**
> §2.4's tool keyed its map on `(season, number)`, so the two shows' episodes collided and
> one silently won. **No damage occurred** — verified afterwards: not one Shield Hero or
> Slime sidecar was modified, and 528 of 615 checked episodes have Jellyfin exactly matching
> their sidecar, with all 8 divergences pre-existing and in seasons the pass skipped. But it
> was luck, not design. Key on the file PATH, and cross-check `Path` against the series
> folder before trusting the endpoint's answer.

### 2.7 Pre-existing contamination found on the way, NOT repaired

* **`The Rising of the Shield Hero (2019) - S01E01.nfo`** carries **That Time I Got
  Reincarnated as a Slime** metadata — title `The Storm Dragon, Veldora`, plot *"Mikami
  Satoru, a businessman, is stabbed…"*. Its neighbours (E05 `Filo`, E12 `The Raven`,
  E20 `Battle of Good and Evil`) are genuine Shield Hero, so it is a single stray file, not
  the whole season. Its mtime (22:10) differs from the rest of the season (13:38), so it was
  written by a different pass. **Left alone: the video itself needs checking first** — if
  the *file* is a Slime episode, this is misplaced media, not bad metadata, and that is a
  different repair.
* **`Dr. STONE (2019) - S01E25`** existed only in Jellyfin, with Shield Hero metadata and
  no file or sidecar on disk. **Resolved by the key reconcile** — confirmed gone, and a
  library-wide check now finds 0 of 27,867 episode items without a `Path`.
* **Fairy Tail Season 00** (specials): seven episodes where the sidecar plot and the
  Jellyfin overview describe *different* episodes. Pre-existing, in a season §2.4 refused to
  map. Needs a specials-aware source; TVMaze's ordering of Fairy Tail OVAs does not match
  this library's.


### 2.8 The last 10, done by hand where the automatic rule was right to refuse

§2.4's two-anchor floor and exact-title match are the right general rule, but three shows
were individually verifiable and got done by hand rather than left in the report:

* **Gundam Build Divers Re:Rise S01E02-E09** (8) — `Unknown Mission`, `A Place to Protect`,
  `Wounded Wings`, `Now Spread Your Wings`, `Hero on the Brink`, `Battered Crown`,
  `Duty and Illusion`, `Abyss of Isolation`, with plots. Skipped automatically because a
  single correct episode (E01) is below the two-anchor floor; but E01 matches TVMaze S1E01
  exactly and the ids are right (§2.5), so the 1:1 mapping is not in doubt.
* **Creature Commandos S01E02, S01E04** (2) — `The Tourmaline Necklace`, `Chasing
  Squirrels`. These were skipped for a fixable reason: the library drops TVMaze's
  `Episode Two: ` prefix, so `norm()`'s exact match failed on titles that are otherwise
  identical. **A suffix/prefix-tolerant comparison would have anchored this automatically**
  — worth adding if that script is ever generalised.
* **Slow Start S01E01** — anchored fine, but TVMaze carries no summary for it. Left blank,
  correctly: there was nothing to write.

Still deliberately unfilled, each for a stated reason rather than for lack of trying:

| show | blank | why not |
|---|---|---|
| Yu-Gi-Oh! ARC-V | 85 | no offset satisfies all 49/23/29 anchors — library numbering genuinely disagrees with TVMaze |
| Cells at Work! | 13 | same, across all four library seasons |
| Yu-Gi-Oh! Go Rush!! / VRAINS | 9 / 7 | same |
| Pokémon Horizons | 8 | same; the one candidate offset was rejected by the coverage rule (§2.4) |
| Fairy Tail S00/S04/S06/S08 | ~40 | same; S00 in particular needs a specials-aware source |
| Digimon Adventure S01E57 | 1 | library holds 94 episodes where TVMaze has 67 — no anchor |
| That '90s Show S03E08 | 1 | **blocked on §5 item 5** — that series holds a three-way numbering disagreement (plan says S02E09-16, DB says season 3 numbers 1-8 twice, disk says Season 03). Fix the numbering first; writing a plot onto a row whose number is disputed just makes the dispute harder to see. |

These want a second provider (TVMaze is thin on long-running anime) or an
absolute-vs-seasonal reconciliation. Each is separable work, not a loose end.

---

## 3. WHAT WAS DELIBERATELY LEFT ALONE

### 3.1 The duplicate-coverage class — ~110 episodes, and it is a PURGE decision, not metadata

`The Powerpuff Girls` (~100 pairs), `Helluva Boss` (8), `One Pace` (2), `Fairy Tail` (1),
`Dr. STONE` (1, §2.3). In every case two files cover one `SxxExx`, and **the metadata on
both is already correct.**

Powerpuff is the clean example, and a previous healer run handled it exactly right: it
wrote `DUPLICATE_EPISODES_REPORT.md` into the show folder listing every pair, fixed the
`lockdata` on thirteen sidecars, and refused to delete anything. The library holds two
parallel rips — combined two-segment h264 files (~1–1.5 GB, real titles) and single-segment
hevc files (~100–350 MB). Both sets are complete.

Nothing here is a metadata fault to repair. It is one decision the owner has to make —
*keep the combined rip or the singles* — after which the loser is removed through the purge
runbook (through the mount, so mediafs propagates the unlink and queues the pool purge).
**Recommendation: keep the combined multi-episode set** — it is the higher-bitrate rip, it
carries real episode titles, and Jellyfin's `-E` span syntax already presents it correctly
as two episodes. But that is the owner's call and it is ~60 GB either way.

### 3.2 Everything `[auto]` in the report

`N episode image(s) are not real stills (portrait 300x450)` and similar are already
resolved by the doctor each pass with `-> fixed: left as-is (provider has no real still)`.
They are noise in the report, not open faults. **If they are to stop appearing, the fix is
in the reporter** — an `[auto]` line whose resolution is "left as-is" every single pass is a
permanent line item that trains the reader to skip the file.

### 3.3 Young Sheldon S02E08

Real title, detector bug. See §1.5. Nothing to repair.

---

## 4. THE SHORT LIST, IN ORDER

1. **`ai_runner.py`: walk the provider chain** (§1.1). Small, and it un-breaks every
   judgment call in the fleet. Three providers were answering while the healer reported
   "no budget".
2. **`brew reinstall ffmpeg`** (§1.4), and add an `ffprobe -version` check to
   `verify_fleet.sh`.
3. **Per-provider budget stamps** (§1.2).
4. **Put the episode LIST in the escalation prompt, and check the run's result** (§1.3).
5. **Sanitise `/` in show folder names, and split the `Shows/Fate` folder** (§2.1). This is
   the one that needs media moves and an owner decision.
6. **Guard against placing an episode beyond the provider's known count** (§2.3).
7. Demote the bare `8bit`/`10bit` junk marker (§1.5); soften the "8 of 9 blank ⇒ wrong
   identity" claim (§2.5); finish Gundam Build Divers Re:Rise E02–E09.
8. **The Powerpuff Girls purge decision** (§3.1) — owner's call, then the runbook.
9. **Add a duplicate-provider-id check to `db_guardian`** (§2.6a) — one SQL query, and it
   catches a whole class that `media_doctor` is structurally unable to see. Add
   "Jellyfin serves more episodes than the folder holds files" to the doctor at the same
   time.
10. **Audit `state/decisions.log` for other series stamped with tmdb 82684 / imdb
    tt9054364** (§2.6) — two shows shared that exact wrong pair, so there may be more.
