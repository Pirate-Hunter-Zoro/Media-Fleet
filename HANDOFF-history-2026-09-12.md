# HANDOFF — media fleet, written 2026-09-12

Read **this file plus `Torrent-Ingest/OPERATING.md`**. This one is the situation as of the
date above and how it got that way; `OPERATING.md` is the standing runbook and does not go
stale as fast.
`Torrent-Ingest/README.md` is the long-form history behind both.

Everything below was measured on this machine on 2026-09-12. Where a number is quoted it
was observed, not estimated. Where something is unproven it says so.

**This file assumes no prior conversation.** It is written for whoever is here next —
the owner, a human, or a different AI assistant. If you are an assistant, read §0a first;
it names the one mistake you are most likely to make.

| § | what is in it |
|---|---|
| **0a** | **Read this first if you are an AI.** What the project is, the five rules that cost real damage, where the prompts live, and the one trap with your name on it |
| 0 | The 60-second orientation table and the three commands worth knowing |
| 1 | Why the hardest pack failed three times, and what computes the answer now (`arcmap`) |
| 2 | Capacity: the six free providers, the free-only rule as the owner actually set it, and how model ids heal themselves |
| 3 | The worked example: the pack that failed three times, and what a purge really has to clean up (§3a) |
| 4 | The operating lessons that cost real time, and what survives a failed run |
| 5 | The library's own reports — what they were getting wrong and what is fixed |
| 6 | Honest limits. Read before trusting anything |
| 7 | What changed, file by file — and §7a, how the day closed out |

---

## 0a. If you are an AI assistant picking this up cold

This file is written to be read WITHOUT the conversation that produced it. If you are
DeepSeek, or any assistant other than the one that wrote this, start here.

**What this is.** A media fleet on one Mac: ~20 launchd daemons that take a `.torrent`
dropped into `iCloud Drive/Torrents/`, download it, ask a free AI model where each file
belongs, validate that answer, and file it into a Jellyfin library on a FUSE mount
(`~/MediaLibrary`) backed by an SSD (`~/Media`) and a pool of MEGA accounts. Five git
repos under `~/Developer`; `Torrent-Ingest` is the one that matters.

**The five rules that cost real damage. Read `OPERATING.md` §3 before touching anything.**

1. **A deletion is only real through the MOUNT.** `~/Media` is the SSD; a file missing
   there has been EVICTED to the pool, not lost. Never "clean up" `~/Media`.
2. **Never restart the reaper mid-drain.** `pgrep -f 'Torrent-Ingest/reap.py'` — any
   output means leave it alone. A drain has run for five days.
3. **`bash scripts/verify_fleet.sh` must print ALL CHECKS PASSED before any deploy.**
   It is the gate. `bash ~/Developer/ship-fleet.sh "what changed"` is the deploy.
4. **Never deploy while an identify run is in flight** (§4b) — `pgrep -f ai_runner.py`.
   The run is a subprocess of the daemon; a deploy kills it and the provider budget with it.
5. **The model PROPOSES, the harness DISPOSES.** `library.validate_plan` re-derives every
   destination and rejects a bad plan whatever wrote it. Do not weaken that seam to make a
   model's answer fit.

**The trap with your name on it.** `~/.config/api-keys/deepseek_key` exists on this machine
and the fleet **deliberately does not use it**. `scripts/audit_free_only.py` lists
`deepseek_key` in `PAID_CREDENTIALS` and `api.deepseek.com` in `PAID_ENDPOINTS`, and it is a
BLOCKING check. That is not an oversight to helpfully fix:

* the fleet's automated AI is free-only by the owner's standing instruction, and every
  provider in `config.AI_PROVIDERS` is a free tier (§2a);
* the owner using DeepSeek as *their own assistant to debug this project* is a completely
  different thing and is fine. Being that assistant does not make DeepSeek a fleet
  provider.

So: do not add DeepSeek (or any paid endpoint) to `config.AI_PROVIDERS`. If you do,
`verify_fleet.sh` fails and the deploy is refused — which is the check working.

**One distinction that looks like a contradiction and is not.** The chain DOES run
`deepseek-ai/deepseek-v4-flash-0731` — as an NVIDIA-hosted model, on NVIDIA's free tier,
with NVIDIA's key, through `integrate.api.nvidia.com`. That is a free request to a free
provider and the audit passes it. What is blocked is `api.deepseek.com` with a
`deepseek_key`, which is a billed account. **The rule is about who bills the request, not
about whose model it is.** Do not "fix" either half to match the other.

**Where the AI actually lives.** `prompts/identify.md` (~52 KB, the full placement prompt)
and `prompts/confirm_placement.md` (~5 KB, used when `arcmap` has already settled the
mapping). `identify.py` assembles a prompt from those plus computed blocks — the release
structure, the provider's season shape, the arc→season mapping, the specials' titles, and
which files must be `owned`. `ai_client.py` is the agent loop; `ai_runner.py` the CLI the
daemons spawn. Everything the model is told is assembled in `identify._runtime_prompt` and
`identify._confirm_prompt` — read those two functions and you know what the model sees.

**How to see what a run was actually told.** `state/tmp/<hash>-w<N>_identify.log` is the
turn-by-turn log; `state/tmp/<hash>-w<N>_plan.json` is the plan it wrote;
`state/decisions.log` is the human-readable audit trail and is never truncated.

---

---

## 0. The 60-second orientation

| | |
|---|---|
| `verify_fleet.sh` | **38 blocking checks, all pass** (was 35; +3 for the §6 fixes) |
| Repos | all five clean and pushed; Torrent-Ingest HEAD `40c92a2` |
| Monogatari | **DONE — 103/103 filed, `VERDICT: PASS`, then fully purged** (§3). History, not open work. §3a is the reusable part: a purge is five cleanups, not one |
| The arc→season bug | **root cause found and fixed in the harness** (§1). The mapping is now computed, not asked for |
| Capacity | **SOLVED — 6 providers, 14 attempts** (§2). Gemini leads and takes a whole 85K prompt |
| Library review | **11 items → 0**; the report itself was lying twice and is fixed (§5, §5a) |
| Open work | **none queued.** But read §6 before believing that — it lists what is KNOWN-BROKEN-AND-ACCEPTED, which is a different thing from a to-do list, and five of its seven entries turned out to be fixable |

```bash
bash ~/Developer/Torrent-Ingest/scripts/verify_fleet.sh            # must print ALL CHECKS PASSED
python3 ~/Developer/Torrent-Ingest/scripts/identify_capacity.py --probe
python3 ~/Developer/Torrent-Ingest/scripts/audit_arc_placement.py \
    --show "Monogatari Series (2009)" --hash ff13439e7e644541b0434527cb379b5bfadb27e8
```

---

---

## 1. Why Monogatari kept failing, and what now does the work instead

Three runs in a row filed this pack with seasons that mixed source arcs. The last census,
taken 2026-09-12 before the purge, is worth keeping because it is what the failure looks
like from the outside:

| season | files | source arcs |
|---|---|---|
| Season 03 | 23 | Owarimonogatari S1 (13), Owarimonogatari S2 (6), Tsukimonogatari (3), Nekomonogatari Black (1) |
| Season 05 | 17 | Koyomimonogatari (12), Hanamonogatari (5) |
| Season 06 | 5 | Otorimonogatari (4), Tsukimonogatari (1) |

Season 03 held exactly 23 files and the provider's Season 03 is exactly 23 episodes. The
count was perfect and every arc in it was wrong. **Nothing in the file, the sidecar or
Jellyfin can see that** — every episode scrapes a real title and a real plot, just another
arc's.

### The step that was never computed

`_release_structure_block` already told the run how the release was laid out.
`_provider_season_block` already told it what seasons the show has. The step between them
— *which arc belongs to which season* — was still the model's job, and it is the one that
failed. It is also genuinely hard for a model: the provider names Season 03 by its opening
arc-internal title ("Tsubasa Tiger"), the release names its folders by the `-monogatari`
arc names ("06 - Kabukimonogatari"). There is no string between them to match on.

**There is arithmetic.** `arcmap.py` (new) does this:

* A release **unit** is one filename LABEL whose episode numbers form a single run — **not
  a folder.** Monogatari's five folders `05 - Nekomonogatari (White)` through
  `10 - Koimonogatari` all name their files `Monogatari Series Second Season - 01..23`, so
  they are ONE unit of 23, which is precisely what the provider calls Season 03. Treating
  folders as the unit is how four arcs ended up in one season.
* Marrying units to the provider's season sizes is an exact-cover search under two
  constraints: a season is filled **exactly** or not at all, and the assignment is
  **monotone** (a later season never draws on an earlier unit than an earlier season did).
  Seasons may be left empty, because a release need not hold the whole show.
* On this pack there are 11 units and 6 provider seasons, and **exactly one** assignment
  covers the most files:

```
  S01 <- Bakemonogatari (15)                 S04 <- Owarimonogatari S1 (13)
  S02 <- Nisemonogatari (11)                 S05 <- Zoku Owarimonogatari (6)
  S03 <- Monogatari Series Second Season (23)
  left over: Kizumonogatari (films), Nekomonogatari (Black), Hanamonogatari,
             Tsukimonogatari, Koyomimonogatari, Owarimonogatari S2
```

That is the right answer, and no model had to find it. It goes into the prompt as a claim
the run is asked to confirm or reject — HANDOFF's own recommended shape: a wrong mapping
becomes visible instead of silent.

**It says nothing about the units it could not place.** Those are films, specials, or an
entry the provider carries separately — three different answers that need judgement. The
block says so outright rather than forcing them into the nearest season, which is exactly
how Nekomonogatari (Black) ended up in Season 03 in the first place.

### The bug underneath the bug — the structure block was DARK

`_release_structure_block` is the thing that names the split/numbering conflict. It reads
`parts[0]` of each release-relative path as the arc folder. On the **chunked** path those
paths come from qBittorrent and are named from the TORRENT ROOT:

```
[MTBB] Monogatari Series (BD 1080p)/01 - Bakemonogatari/[MTBB] Bakemonogatari - 01v2 [346DABB1].mkv
```

so every file shared one "folder" and the block returned `""` — it only speaks when a
release has two or more. **It was written for Monogatari and rendered nothing on
Monogatari, every single run**, because Monogatari is always chunked. Fixed by
`arcmap.strip_release_root`; frozen by `test_arc_mapping.py` part 5. This was invisible
from the code and only showed up when the real qBittorrent path list was fed through it.

### Two guards, so a wrong boundary fails LOUD

Both in `library.validate_plan`, both replayed over all 788 historical plans before
shipping, and **both reject zero of them**:

* `_reject_arc_split_across_seasons` — one release unit torn between two NUMBERED seasons.
  Catches Tsukimonogatari across Seasons 03 and 06. Season 00 is exempt on purpose:
  splitting an arc between a season and the show's specials is normal and often right.
* `_reject_season_over_provider_count` — a season filled past the provider's episode count,
  **counting what is already on disk** so cross-wave mixing is caught too. Catches
  Koyomimonogatari + Hanamonogatari stacked into a 6-episode Season 05.

**The obvious version of the first guard is wrong and it took a census to see it.**
"Refuse a season drawing on non-consecutive folders" rejects the CORRECT answer here:
Monogatari's Season 03 is folders 05, 06, 08, 09 and 10, and folder 07 is Hanamonogatari,
which aired a year later and belongs to no season at all. The release orders its folders by
the novels, not by broadcast. `audit_arc_placement.py` still prints non-consecutiveness as
a **hint**; do not promote it to a rule.

---

---

## 2. Capacity — SOLVED, 2026-09-12 afternoon

The fleet ran on three providers and spent most of the day at zero. **It now runs on six**,
and the owner made two decisions that changed the picture. Read §2a before believing any
older sentence about "free-only" in this repo.

| provider | models | verified |
|---|---|---|
| **gemini** (Google AI Studio) | `gemini-3.5-flash`, `gemini-3-flash-preview` | tools ✅, 85,000-char prompt ✅ |
| **openrouter** | 3 × `:free` | 1,000 req/day since the $10 (was 50) |
| groq | 2 | confirm-mode prompts only (see §2b) |
| **nvidia** (NIM) | nemotron-3-super, kimi-k3, deepseek-v4-flash, gpt-oss-20b | all four: tools ✅, 85K ✅ |
| **mistral** | `open-mistral-nemo` | tools ✅, 85K ✅ |
| cloudflare | 2 | 10,000 neurons/day |

The chain went from 7 attempts across 3 providers to **14 across 6**. Gemini is first
because it is the only free tier measured to take a WHOLE identify prompt.

**Every model id above was PROBED LIVE before it was configured, and that is not
ceremony.** Five ids that looked obviously right were already dead:
`meta/llama-3.3-70b-instruct`, `openai/gpt-oss-120b` and `qwen/qwen2.5-coder-32b-instruct`
answer HTTP 410 "reached its end of life" on NVIDIA; `gemini-2.0-flash` and
`gemini-2.5-flash` answer 404 "no longer available". Two more were excluded on the same
probe for 503 "experiencing high demand" (`gemini-3.8-flash`, `gemini-flash-latest`) and
two for answering 429 to everything (`mistral-small-latest`, `open-mixtral-8x22b`).
Re-probe with `scripts/identify_capacity.py --probe` when this rots; it will.

### 2a. The free-only rule, as the owner actually set it

It used to read *"no paid model, no API key with a balance."* **The owner put a one-off $10
on the OpenRouter account on 2026-09-12.** The second half is no longer true, by decision.

What it bought is a **gate, not usage**: OpenRouter's `:free` tier allows 50 requests a day
under 10 credits and 1,000 at or above it. The balance is never drawn down, because every
OpenRouter slug still ends in `:free` and a `:free` request is billed at zero whatever the
balance says. So the invariant is now **no request is BILLED** — which is what
`audit_free_only.py` checks 2 and 3 always actually enforced.

It is written into `config.py`, `scripts/audit_free_only.py` and `OPERATING.md` §4, each
with a "do not restore the old wording" note, because restoring it would make the audit
assert something the owner deliberately changed and undo a twentyfold capacity increase.

**Mistral is free and cannot bill.** No card, and an account with no billing configured
cannot be charged; the 429s are the free tier's rate limits doing their job.

**Cerebras is excluded on purpose**, despite the best headline numbers going (1M tokens/day):
its "free" plan is a 30-DAY TRIAL carrying $5 of EXPIRING credits, per its own rate-limit
docs. It would serve for a month and then quietly stop — the worst failure shape this fleet
has. It stays in `PAID_ENDPOINTS`.

### 2b. groq — what its limit really is

| provider | state | the real limit |
|---|---|---|
| groq | usable only for a CONFIRM-mode prompt | 8,000 tokens/MINUTE **and 200,000/DAY** |

### Groq was written off for the wrong reason, twice

`OPERATING.md` used to record groq as "permanently excluded — 26,367 char ceiling vs a
63,177 char smallest prompt". Two things about that were wrong:

1. **The ceiling was 21,892, not 26,367** — re-derived from a clean live 413
   (`Limit 8000, Requested 8763` against a 23,980-character prompt). The old number came
   from a refusal whose char/token ratio was different.
2. **A rate limit is not one thing.** Any 429 raised `AIUnavailable`, which made the caller
   abandon the provider. So a provider with a whole day's budget left was dropped over a
   *twelve-second* pause it had measured for us and named in its own reply. `ai_client`
   now sleeps a short stated wait and retries (`_retry_after_sec`), and still fails over
   on a long one or one the provider will not quantify. A request larger than the entire
   per-window allowance is never waited on — no amount of waiting shrinks it.

With pacing, groq ran a real agent loop for 19 turns. **It still produced nothing**, and
the reason is the useful part: it spent every turn web-searching for Season-0 episode
titles, hit the loop breaker, and burned **195,693 of its 200,000 daily tokens in twenty
minutes**. Which leads to:

### Two changes that came out of that

* **`prompts/confirm_placement.md`** — when `arcmap` has settled the mapping, the run is
  confirming an answer, not deriving one, and most of `identify.md` is rules about cases
  the mapping has ruled out. The confirm prompt states that smaller job honestly instead
  of truncating the larger one, which §6 has always warned against. Measured live on
  wave 1: **13,335 characters against a full prompt of 85,125** (74,673 with the narrowed
  digest) — a 5.6x reduction, and what puts groq on the board at all. It is offered to a provider **only** when the full prompt does not
  fit, and its plan goes through the identical `validate_plan`.
* **`epguide.specials()`** — the fleet already had the answer the run was searching for.
  TVMaze carries Nekomonogatari (Black), Hanamonogatari, Tsukimonogatari, Koyomimonogatari
  and Owarimonogatari's 2017 run as *specials of Monogatari Series*, each with a real title
  and synopsis. `arcmap.metadata_block` now puts them in the prompt, matched arc-to-arc.

  The matching is by size **and order**, and order is load-bearing: size alone pairs
  `Kizumonogatari` — three FILMS — with `Owarimonogatari`'s three 2017 specials, because
  that is the only group of three, and would have printed three confident wrong titles.
  With order it correctly matches four arcs; an arc the provider GROUPS where the release
  SPLITS (Owarimonogatari S2: 7 files, 3 provider entries) gets its titles offered
  **hedged and mapped to no file**, and Kizumonogatari gets nothing at all, which is the
  honest answer for three films.
* **The Season-00 NUMBERS are proposed too**, running across the special arcs in release
  order: `S00E01-E32`, distinct, no gaps, films excluded. This is the same reasoning as the
  season mapping — it is arithmetic, and two files at one destination is a hard error that
  fails the WHOLE plan rather than one file. Thirty-two files across five arcs is a lot of
  chances to collide.

* **A confirm-mode run is capped at 12 turns** (`config.IDENTIFY_CONFIRM_MAX_TURNS`), not
  120. A provider small enough to need this prompt has a small daily budget too, and a run
  that investigates instead of writing does not merely fail — it takes the provider off the
  board until its reset. Twelve is measured against what the job actually needs now that
  the mapping and the specials' metadata are handed over: a couple of TMDB lookups for any
  film in the pack, and the Write.
* **The confirm prompt shrinks to fit a stated ceiling**, and the ceiling is the whole
  REQUEST, not the prompt text. The tool schemas and the runtime's system prompt travel
  with every call and are not in the prompt — 3,205 + 1,348 characters with the deriving
  run's eight tools — so a prompt built right up to the stated ceiling is refused. Confirm
  mode therefore uses **four** tools (`Write,ListDir,WebSearch,WebFetch`: the job needs no
  `Read`/`Glob`/`Grep`/`Probe`, and every tool offered is a way to spend a turn not
  writing) and budgets `ceiling − 3,000` for the text. Over budget it shortens the
  specials' SYNOPSES first (220 → 140 → 90), then **drops them entirely** rather than
  emitting a stub: that text goes into a LOCKED sidecar, so a plot truncated to "..."
  would be frozen into the library permanently. Titles and the S00 numbers are never cut.
* **Measured live on the real wave, 14:29 CDT: 13,237 + 3,000 = 16,237 against groq's
  21,892 — it FITS, with 5,655 characters to spare.** The full prompt for the same wave is
  85,125, and the narrowed-digest fallback 74,673, so confirm mode is a 5.6x reduction and
  it is what puts groq on the board at all.

  *(An earlier estimate in this file said 23,211 and concluded groq could not serve it.
  That was measured against a synthetic listing of all 103 full paths, which `_list_files`
  never produces — it emits a folder summary plus inline byte-exact filenames for the
  wave, 2,918 characters here. Trust the live log line, not a reconstruction.)*

`identify_capacity.py` now has a third verdict, `PARTIAL`, for exactly groq's situation.
Read it; `NONE` and `PARTIAL` mean different things.

---

---

## 2c. `ai_models.py` — retired model ids heal themselves

Model ids rot fast and silently. Five of the eight a careful reader would have written down
on 2026-09-12 were already dead:

```
meta/llama-3.3-70b-instruct        HTTP 410  "reached its end of life on 2026-08-26"
openai/gpt-oss-120b       (nvidia) HTTP 410  "reached its end of life on 2026-09-03"
qwen/qwen2.5-coder-32b-instruct    HTTP 410  "reached its end of life on 2026-05-12"
gemini-2.0-flash                   HTTP 404  "no longer available"
gemini-2.5-flash                   HTTP 404  "no longer available to new users"
```

A retired slug is neither "unavailable" (no budget) nor "transient" (a retry fixes it), so
the chain spent one wasted attempt per record on it, forever, until a human noticed —
OpenRouter retired two `:free` slugs on 09-07 and the fleet 404'd for days.

Now, on a retirement signal: ask the provider's own `/v1/models`, rank the live ids for a
same-family drop-in, **probe the best candidates with a real tool-calling request**, and
record the winner in `state/ai_model_overrides.json`, which `config.enabled_ai_attempts()`
merges. Heals next cycle.

**Three decisions worth not re-litigating:**

* **No AI is involved.** Listing models is an HTTP GET; choosing is a policy. Spending a
  model call to find a working model is circular and burns the scarce budget.
* **It never edits `config.py` and never ships.** A daemon that rewrote its own source and
  ran `ship-fleet.sh` would restart every daemon including itself (killing in-flight runs,
  §4b), push a machine-chosen slug to git, and bypass the `verify_fleet.sh` gate. Config
  stays the human's DECLARED PREFERENCE; the overlay is runtime only.
* **The probe is load-bearing.** A models list says nothing about function calling and this
  fleet is an agent loop. Nothing is adopted until a real tool call comes back.

Two ranking bugs the tests caught before it shipped: a `-lite` sibling outranked full-size
ones (it shares every token plus one), and `z-ai/glm-5.3-flash` counted as the same family
as `nvidia/nemotron-3-super-120b-a12b` because both contain a `3`. Family now needs a shared
WORD; a smaller sibling is a last resort.

**Promote overrides into `config.py` when you see them.** The overlay is a runtime patch,
not a home. `cat state/ai_model_overrides.json` — anything in it is a model id the config
still names wrongly.

---

---

## 3. Monogatari — DONE. Filed 103/103, then purged. Kept as the worked example.

**This section is history, not open work.** The stress test finished and the content is
gone. It is preserved because the *shape* of the failure is the most instructive thing in
this repo: three runs filed the same show wrong, and the fix was to stop asking a model to
guess an arithmetic fact. Read it as a worked example, not as a to-do.

**Final result, 2026-09-12 ~19:00: `VERDICT: PASS`.** 103 of 103 filed, no drops, nothing
UNFILED; 32 Season 00 specials locked with 0 placeholder plots; the three Kizumonogatari
films in Movies with distinct TMDB ids. Then purged per `OPERATING.md` §6 — see §3a for
what a purge does and does not clean up on its own.

```
  S01  Bakemonogatari                    CORRECT
  S02  Nisemonogatari                    CORRECT
  S03  Monogatari Series Second Season   CORRECT     <- the arc that failed three times
  S04  Owarimonogatari S1                CORRECT
  S05  Zoku Owarimonogatari              CORRECT
  S00  specials (leftover arcs)          CORRECT
VERDICT: PASS
```

### Why Season 03 is the result that matters

It is the arc that broke three runs, and this is the before/after:

```
  three failed runs                    now
    13 <- Owarimonogatari S1              5 <- 05 - Nekomonogatari (White)
     6 <- Owarimonogatari S2              4 <- 06 - Kabukimonogatari
     3 <- Tsukimonogatari                 4 <- 08 - Otorimonogatari
     1 <- Nekomonogatari (Black)          4 <- 09 - Onimonogatari
                                          6 <- 10 - Koimonogatari
  four arcs, all wrong                  one arc, 23 files across 5 folders, correct
```

Waves 1 and 2 were filed by **nvidia/nemotron-3-super-120b-a12b**, confirming the harness's
computed mapping. No model derived it; the harness did, and the model agreed.

### READ THE VERDICT, NOT THE HEADLINE

The audit's non-consecutive-arcs line is a **hint that cries wolf on the correct answer
for this pack**: Season 03 is folders 05, 06, 08, 09 and 10, skipping 07 because
Hanamonogatari aired a year later and belongs in specials. As of 2026-09-12 the audit
computes a real PASS / CORRECT-SO-FAR / FAIL by rebuilding `arcmap`'s mapping and comparing
it to disk, and the hint is demoted to a parenthetical whenever the verdict is fine. Exit
code is 0 on PASS or CORRECT-SO-FAR, 1 otherwise.

### 3a. A purge is FIVE cleanups, not one — and only the first is automatic

This is the part that surprised us, and it will surprise the next person. `OPERATING.md` §6
deletes the **video files**. It does not finish the job. After any large purge, check all
five of these or you will be left staring at a show that looks like it is still there —
and #5 can silently bring the content BACK:

1. **The pool copies** — the real bytes on the MEGA remotes. Queued to
   `Media-Syncer/mediafs_deletions.jsonl`, drained by the reaper, which purges each file
   from every remote holding it and then prunes `remote_inventory.json`. **This is the one
   that is automatic.** Expect it to be slow: the reaper shells out to `rclone deletefile`
   serially, so ~60 files took about 20 minutes.

2. **The empty mount tree** — `~/MediaLibrary/Shows/<Show>/Season NN`, read-only,
   `dr-xr-xr-x`, 1969 mtime, zero files. These are *synthesized by MediaFS from whatever
   `remote_inventory.json` still lists*. They are not real directories and `rmdir` will not
   touch them. They vanish on their own the moment the reaper finishes step 1 — so if you
   see them, the answer is to wait and watch `grep -ic '<show>' remote_inventory.json`
   count down to 0, not to go hunting for a stuck directory.

3. **Orphan sidecars** — `.nfo`, `-thumb.jpg`, `-poster.jpg`, `._*` AppleDouble. The purge
   takes the videos; the metadata beside them stays. These are real files and they are what
   makes the tree look non-empty. Delete them **through the mount**:

   ```bash
   find ~/MediaLibrary/Shows/"<Show>" -type f -print0 | xargs -0 rm -f
   ```

4. **Jellyfin rows with nothing behind them.** Episode rows self-heal on the next library
   scan, but three kinds do **not**, because their "path" still exists or is internal:
   - a **Series** row pointing at the synthesized shell (clears once step 2 does, plus a scan),
   - an empty **Playlist** under `jellyfin/data/playlists/`,
   - an empty **BoxSet/Collection** under `jellyfin/data/collections/`.

   Find and remove them by API (`ChildCount`/children of 0 is the test for "empty"):

   ```bash
   curl -s -H "X-Emby-Token: $JELLYFIN_API_KEY" \
     "$JELLYFIN_URL/Items?recursive=true&searchTerm=<Show>&fields=Path&limit=300"
   curl -s -X DELETE -H "X-Emby-Token: $JELLYFIN_API_KEY" "$JELLYFIN_URL/Items/<id>"
   ```

   `JELLYFIN_URL` / `JELLYFIN_API_KEY` are not in a dotfile — they live in the launchd
   plists (`com.mikeyferguson.mediadoctor.plist`, `EnvironmentVariables`).

5. **The ingest journal record — chunked packs only, and this one can UNDO the purge.**
   A chunked record whose `status` is still `downloading` is re-adopted by
   `_readopt_chunked` on the next ingest cycle, and it re-adds the torrent **from the source
   `.torrent` under `finished/`** — so deleting it from qBittorrent does not stop it. This
   fired at 19:59 on Monogatari, twenty minutes after the purge finished:

   ```
   19:59:18  Re-adopted chunked torrent [MTBB] Monogatari Series (BD 1080p)
             (103/103 files already filed); waves resume next cycle.
   19:59:39  COMPLETED chunked [MTBB] Monogatari Series (BD 1080p): 103 filed, 0 declined;
             torrent removed.
   ```

   It was harmless here **only because `chunk_done` already covered all 103 files**, so the
   daemon retired the record 21 seconds later and nothing was re-fetched (library, staging
   dir, pool and qBittorrent all verified at zero afterwards). Purge a pack that is genuinely
   mid-download and it will resume fetching. Confirm the last journal line for the hash
   reads `completed` or `failed`:

   ```bash
   grep '<info_hash>' ~/Developer/Torrent-Ingest/state/journal.jsonl | tail -1
   ```

**The done test.** All five must read zero, for every alias of the title:

```bash
find ~/MediaLibrary ~/Media -maxdepth 4 -iname '*<alias>*'      # 0 hits
grep -ic '<Show>' ~/Developer/Media-Syncer/remote_inventory.json # 0
ls ~/Developer/Media-Syncer/mediafs_deletions.jsonl*             # no live queue
ls ~/Downloads | grep -i '<Show>'                                # nothing
# and 0 rows from the Jellyfin /Items query above, after a /Library/Refresh
```

For Monogatari the aliases were monogatari / bakemono / kizumono / nisemono / nekomono /
hanamono / tsukimono / owarimono / koyomimono. All five read zero at 19:46 on 2026-09-12.
Two things were deliberately **kept**: the unrelated show *Monogatari Series Off & Monster
Season (2024)*, and `state/torrent_sources/ff13439e….torrent`, which
`scripts/test_arc_mapping.py` uses as a fixture — do not "tidy" either away.

### If an ingest lands wrong

Purge the show and restart from zero; do not patch in place. But note the failure mode has
changed: with the two guards of §1 in place a wrong boundary should now be REFUSED rather
than filed, so a wrong landing is a different and more interesting failure than the one
that kept happening. Read the rejection first. Then stop the torrent, set `status=refused`
and drop every `chunk_*` key in the journal, and move the `.torrent` back to the TOP of the
watch folder.

---

## 4. What blocks an ingest now, and the lessons that cost real time

**Nothing structural — it is down to provider budgets on the day.** This is the normal
failure mode you should expect, and it is self-healing: the chain walks all six providers
in about three minutes (it used to take 48, §4a), and if every one is spent the pack defers
and retries later rather than failing.

Here is what a fully-exhausted evening actually looked like, measured at 16:21 CDT
2026-09-12 — keep it as the reference for recognising budget exhaustion versus a real bug:

```
gemini/gemini-3.5-flash        429  quota exhausted for the day
gemini/gemini-3-flash-preview  429  quota exhausted for the day
openrouter/...:free            429  free-models-per-day-high-balance (the 1,000/day bucket
                                    the $10 unlocked — spent during the day's retry storms)
groq/gpt-oss-120b, gpt-oss-20b 429  200,000-token day, spent
nvidia/nemotron-3-super-120b   OK   the only provider still with budget that evening;
                                    slow (10-20 min/run) but it answered
mistral/open-mistral-nemo      untried today
```

Nearly every line there is a `429`, not a crash. **That is a budget day, not a broken fleet, and
the correct response is to wait.** The pack defers safely between attempts: staged bytes are
kept, nothing is lost, and it retries on a backoff. **Do not "rescue" it by hand** — a
hand-run costs the same scarce budget the automatic retry needs.

To see where the budgets actually stand:

```bash
python3 ~/Developer/Torrent-Ingest/scripts/identify_capacity.py --probe   # NOT during a run
```

**Never `--probe` while a run is in flight** — it spends one live request per model, 14 of
them, against the same budgets the run needs (§4b). Check first with
`pgrep -f 'ai_runner.py'`, and see the self-match trap in §0a before you trust that.

---

---

## 4a. A BUSY provider is skipped, not retried (fixed 2026-09-12)

The chain grew to six providers and Monogatari still sat unfiled, because `503` was in
`IDENTIFY_TRANSIENT_SIGNATURES` — so a busy provider was RETRIED up to four times, and each
caller-level retry is a WHOLE FRESH AGENT RUN whose turns are all thrown away:

```
15:41:40  gemini-3.5-flash        -> 503 after 5m21s -> retry the SAME model
15:52:40  gemini-3.5-flash        -> 503 after 5m19s -> retry the SAME model
16:00:45  gemini-3.5-flash        -> 429             -> next model
16:08:24  gemini-3-flash-preview  -> 503 after 7m38s -> retry the SAME model
```

Two models x four attempts x ~6 minutes is about **48 minutes before the chain reached
OpenRouter** — while five funded providers idled behind one busy one. After the fix the same
walk took **three minutes** and reached NVIDIA.

`config.IDENTIFY_BUSY_SIGNATURES` / `identify_provider_busy()` now separate "this PROVIDER
is overloaded" (503/502/504/529/"high demand") from a genuine blip (reset connection, EOF,
timeout). Busy goes straight to the next provider; a blip keeps its retries, and
`ai_client._post` already retries the transport four times inside one run anyway.

**Keep the distinction narrow in both directions.** A 503 must never trigger a model swap
(that is `ai_models.is_retired`, §2c) and a 410 must never be read as load. Frozen in
`scripts/test_model_retirement.py` Part 4b, including the assertion that no string is ever
both BUSY and RETIRED.

---

---

## 4b. Two operational lessons, both learned the expensive way

**DO NOT DEPLOY WHILE AN IDENTIFY IS IN FLIGHT.** `ship-fleet.sh` restarts `torrentingest`,
and the identify run is its SUBPROCESS -- so a deploy kills a run mid-flight and the
provider budget it had already spent is simply gone. It happened at 15:47: the first
Gemini run on the real Monogatari wave was four turns in when a docs-only deploy killed it.
Check first:

```bash
pgrep -f 'ai_runner.py' && echo "a run is in flight -- do NOT ship yet"
```

**DO NOT `--probe` WHILE A RUN IS IN FLIGHT EITHER.** `identify_capacity.py --probe` spends
one live request PER MODEL -- fourteen of them now -- against the same free budgets the
real run is drawing on. Measured the same afternoon: the Gemini run sat on turn 1 for eight
minutes and advanced to turns 2, 3 and 4 within seconds of the probe being killed.

**A LONG IDENTIFY BLOCKS NEW DROPS, and that is not a bug.** `ingest.cycle()` is
synchronous: it registers new `.torrent` drops, then walks the filing path, then sleeps.
So while a 30-minute identify run is in flight, a `.torrent` dropped into the watch folder
is not registered and does not appear in qBittorrent at all. Observed 2026-09-12: two
torrents dropped at 17:12 were still unregistered at 17:50 because a Monogatari wave was
mid-identify. They pick up on the next cycle. **Do not re-drop them and do not add them to
qBittorrent by hand** — that creates a torrent with no journal record, which is worse.

**And a deploy has a second cost that is easy to miss.** It restarts the `mediafs` mount,
which takes Jellyfin down under it, and restarts `media_doctor` in the same breath -- so the
doctor's startup pass lands on a Jellyfin that is still coming up. See §5a.

---

---

## 4c. What SURVIVES a failure, and what used to be thrown away

Asked directly by the owner, answered from the code rather than from memory, because the
answer decides whether a re-run starts from zero.

**Material progress — saved, and defensively so.**

* The staged bytes. A failed identify leaves the wave intact; nothing is re-downloaded.
  (32 GB verified on disk while Monogatari sat at `0 filed / 32 staged`.)
* Anything already FILED lives in the journal as `chunk_done` / `chunk_filed`, and
  `chunk_done` is explicitly treated as *"a claim about the PAST"* — re-verified against the
  mount before it is trusted, so a file that was filed and later vanished is re-fetched
  rather than assumed done.
* So **progress compounds**: a wave that files 20 of 32 leaves 12, permanently, across
  provider switches, cycles, daemon restarts and deploys.

**Reasoning progress — mostly not saved, and now partly fixed.**

* The agent conversation is thrown away on every provider switch. Each provider starts
  from scratch. Not worth persisting (large, and stale the moment the library changes).
* A REJECTED plan plus the harness's reason IS handed to the next provider, which is what
  makes the chain a progressive repair rather than a blind retry — the block literally
  says "keep everything that was already right".
* **Until 2026-09-12 that died with the walk.** `rejections` was a local in `run_identify`,
  so when the chain ran out of providers the next cycle began knowing nothing. That is how
  the same mistake got made three times on this very pack. Now persisted to
  `state/identify_rejections.json` and seeded back in at the start of every walk.

**The loss that is bigger than it looks.** `validate_plan` rejects WHOLESALE: 41
`raise PlanError` sites, first one wins. A plan that placed 31 of 32 files correctly and got
one wrong is discarded in full. The fix is deliberately NOT to apply the good 31 — applying
a plan the harness distrusts is exactly how wave 2 once read mis-filed files as ground truth
and dropped 12 files. Persisting the rejection gives most of the benefit with none of that
risk.

Bounded three ways, because stale advice is worse than none: a **3-day TTL** (a rejection is
a fact about a plan judged by a SPECIFIC set of guards, and two guards were added that very
day), **deduplicated by reason** (the same error from three providers is one lesson), and
**capped at 2** (each costs prompt characters against providers with hard ceilings). Saved
at the MOMENT of rejection, not at the end of the walk, because the run is a subprocess of
the daemon and a deploy restarts the daemon.

---

---

## 5. The library-review items — all five resolved

`library_health.txt` listed five slots holding two files each. The owner decided on
2026-09-12 to clear all five; the redundant copy was deleted through the mount and 76+5
pool copies are queued for the reaper.

| slot | kept | deleted | why |
|---|---|---|---|
| One Pace S08E02 | `- Parallels.mkv` | `S08E02.mkv` | **byte-identical** (190,639,316 both). Kept the one with the locked, real title |
| Made in Abyss S00E05 | `-Papa to Issho.mkv` | `S00E05.mkv` | **byte-identical** (194,227,017 both). Same reason |
| One Pace S13E05 | `- Inherited Will.mkv` (1201 MB) | `- Quack Doctor.mkv` (889 MB) | Quack Doctor is the OLDER cut (ch. 141-145); Inherited Will is newer (ch. 140-145) |
| One Pace S29E01 | `S29E01.mkv` (939 MB) | `- Into the Depths!....mkv` (379 MB) | `decisions.log` records `[One Pace][603-604] Fishman Island 01` arriving as a **replacement** for this slot; the bare file is that newer cut |
| Powerpuff S03E09 | `S03E08-E09 - ... & Cop Out.mkv` (865 MB) | `S03E09 - Cop Out.mkv` (112 MB) | the pair file already covers E09; this was the last leftover hevc single from the set dropped in September |

**S29E01 needed a follow-up and got it.** The surviving file carried Jellyfin's placeholder
title "One Pace" and no synopsis at all — the real ones went with the sidecar of the older
cut. The fleet's own `decisions.log` still had them, from the run that filed the
replacement: it recorded reusing the existing `.nfo`'s title and plot, plot text and all.
Both halves were then set per `OPERATING.md` §5a — `Name` + `Overview` +
`LockedFields:["Name","Overview"]` + `LockData:true` through the API — and Jellyfin wrote
`<lockdata>true</lockdata>`, the real title and the 306-character plot back into the
sidecar. Verified on disk. **Nothing was invented**; it was recovered from the fleet's
own record, which is the only acceptable source for text that gets locked.

**`library_health.txt` went 11 review items → 0.** Jellyfin also held phantom rows for the
deleted files (474 One Pace episodes now, was 477); a `ValidationOnly` refresh cleared
them, and both series report zero blank synopses.

Clearing these three One Pace slots also **unblocks future re-cuts for them**: the
retarget path added on 2026-09-12 deliberately refuses to act on a slot holding two files.

---

---

## 5a. The health report was lying, in two different ways (fixed 2026-09-12)

`library_health.txt` said "20 shows with issues" and one movie was the only thing a human
could act on. Two distinct bugs, both now fixed and both worth understanding, because both
are the same species: **a report that lists what nobody can act on buries what they can.**

**1. A refusal nobody remembered.** 19 of 21 lines were "N episode image(s) are not real
stills", each followed by `-> fixed: left as-is (provider has no real still)`. The shape
check was right and the repair had already asked the provider and been told no -- and then
forgot, every cycle, forever. The refusal is now remembered per IMAGE in
`doctor_state.json` (`art_no_still`), with a 30-day TTL because stills do get added later.
Keyed per image, never per show, so one unfixable still cannot hide a fixable neighbour.
Measured across three real passes: 20 -> 19 -> 15 shows, 19 -> 18 -> 14 stills lines,
0 -> 40 -> 80 refusals learned. It converges at 40/cycle.

**2. A transient mount error reported as missing metadata.** `library._read_text` returns
None for ANY `OSError`, so "no sidecar" and "the FUSE mount would not give me the file just
then" were the same answer -- and `episode_is_blank` called both blank. The report said
*"The Powerpuff Girls: 66 of 78 episode(s) have no synopsis -- that ratio usually means the
series is matched to the WRONG provider entry"*, and sent a human to re-identify a series
that was perfectly fine: 78 of 79 sidecars had a real plot and all 79 were locked. The
mount had simply been restarted under the reader, by my own deploys.

`library.episode_nfo_state()` now returns `missing` / `unreadable` / `blank` / `ok`, an
unreadable sidecar is never counted as blank, and the doctor raises it as its OWN finding
("that is the MOUNT, not the metadata") so a genuinely flaky mount still surfaces instead of
being silently swallowed. **A pass that cannot reach Jellyfin now retries in 60s instead of
sleeping the full 1800s cycle** -- it used to lose half an hour every time a deploy restarted
it, and nothing marked the stale report as stale.

Result: `NEEDS REVIEW` went 2 -> 0, shows 20 -> 15 and still falling.

---

---

## 5b. A dead pinned TMDB id — the shape that hides for weeks (found 2026-09-12)

`Ghost in the Shell - Arise - Another Mission (2013)` sat in `library_health.txt` for weeks
as two `[auto]` lines — "movie has no Primary image; movie has no plot/overview" — meaning
the doctor believed it was handling them. It was not, and **could not**: the film's `.nfo`
pins `<tmdbid>573120</tmdbid>`, and TMDB answers **404** for that id. Every repair pass
searched against a dead id, found no candidate, filled nothing, and re-reported the same
two lines next cycle. Nothing ever said the automatic path was impossible.

Three things now stop that recurring:

* **`tmdbguide.movie_exists()`** — a pinned id that 404s is detected and reported by name,
  as `NEEDS REVIEW`, not `[auto]`: *"pinned TMDB id NNN does not exist (404) … this needs
  RE-IDENTIFYING, not re-fetching."* A dead id is a different problem from missing
  metadata and needs a different answer.
* **A title-search fallback** — with a dead id the search now DROPS it and searches TMDB by
  title, which is the one thing that can still work. It fills the plot and logs the
  candidate it used.
* **Stuck detection** — any `[auto]` problem unchanged for `STUCK_AFTER_PASSES` (4 passes,
  two hours at the 30-minute cycle) stops being called automatic and says *"unchanged for N
  passes — the automatic repair cannot fix this one."* This is the alarm that was missing,
  and it is general: it will catch the next stuck-but-automatic-looking thing too.

**RESOLVED 2026-09-12 by purging the file, and the evidence is worth keeping** because it
is how to judge the next one of these:

* **44.5 MB**, against 959 / 1446 / 1096 / 1737 MB for Arise Borders 1-4. About 3% of a
  real film — minutes of video, not an hour.
* The library already holds **Borders 1, 2, 3 and 4** complete, so nothing was lost. It was
  not the missing Border 5 either: that is *Pyrophoric Cult* and would be ~1 GB.
* TMDB has **no "Another Mission" title at all** — which is exactly why the pinned id 404s.
  There was never a film to point at.
* `decisions.log` shows that torrent carried commercials and scrapings
  (`Ghost in the Shell ARISE - CM 05 (1080p).mp4 - 18 seconds. Another commercial.`) — it
  came in during the Torrent-Searcher era, before that subsystem was deleted on 2026-09-10.

Deleted through the MOUNT; the pool copy was reaped. **The detection work above stays and
is the durable part** — the next dead pinned id will be named within two hours instead of
sitting for weeks.

**Postscript, 19:40 the same evening: the real Border 5 arrived and the collection closed
cleanly.** A `Border(s) [Movies 1-5]` torrent was ingested against a library that already
held 1-4. The run filed only what was missing and left the four existing films alone, which
is the behaviour the per-file ownership model of §1 is for. Final state:

```
  Border 1  Ghost Pain           993 MB   tmdbid 196750   plot 765 chars
  Border 2  Ghost Whispers      1869 MB   tmdbid 212168   plot 370 chars
  Border 3  Ghost Tears         1126 MB   tmdbid 240341   plot 248 chars
  Border 4  Ghost Stands Alone  2166 MB   tmdbid 279254   plot 691 chars
  Border 5  Pyrophoric Cult     1367 MB   tmdbid 196755   plot 399 chars
```

Five distinct TMDB ids, five real plots, no placeholders, and none of the torrent's
creditless OP/ED extras leaked into the library. Note Border 2 is now 1869 MB: the 44.5 MB
"Another Mission" junk is gone and the slot holds the real film.

---

---

## 5c. What happens when Claude is uninstalled — checked, 2026-09-12

**Nothing in the fleet depends on it.** `config.AI_BIN` is `[sys.executable,
ai_runner.py]` — the fleet's own agent loop over its own free providers. No daemon, no
script and no launchd job shells out to Claude Code. Grepped all five repos to confirm.

Re-verified the evening of 2026-09-12 by re-running the grep across all five repos. The
list below was INCOMPLETE when first written — it named three references and there are six
kinds. None of the new ones is a dependency either, but do not trust the old count:

* `drive_ingest.py` skips `.claude` in a directory-exclusion list. Inert.
* `scripts/audit_free_only.py` lists `api.anthropic.com` and `anthropic_key` in its
  BLOCKLISTS. That is the free-only check doing its job and must stay.
* `Media-Syncer/scripts/split_tunnel.sh` pins Anthropic's IP ranges OUTSIDE the Mullvad
  tunnel, so assistant API calls kept a stable home IP and exit-node rotation could not
  reset one mid-call. **This is the only real leftover.** After uninstalling it is dead
  weight but harmless — a few pinned routes to a service nobody is calling.

  If the owner debugs with a different assistant (DeepSeek was mentioned), the same
  treatment may be wanted for that API: the block is near line 196, pattern
  `pin "<label>" $RANGES`. Removing the Anthropic pins is optional and NOT urgent —
  editing routing to tidy up is a bigger risk than leaving four inert routes in place.

* **The four `.githooks/commit-msg` hooks** (Torrent-Ingest, Media-Syncer, Title-Scout,
  Open-Code-Doctor) strip AI attribution trailers. Their `AI=` pattern already covers
  `deepseek`, `copilot`, `chatgpt`, `gemini` and nine more alongside `claude` — so they are
  not Claude-specific and they KEEP WORKING for whatever assistant comes next. Leave them.
* **`.claude/` in three `.gitignore`s.** Inert.
* **`ship-fleet.sh`'s splittunnel comment.** Was the one reference that was actually BROKEN,
  and not because of Claude: it cited `scripts/split_tunnel_anthropic.sh`, which has never
  existed under that name. The file was `split_tunnel_deepseek.sh` until commit `a36a860`
  renamed it to the provider-agnostic `split_tunnel.sh`; the comment was never updated.
  That comment is the ONLY place recording how to reinstall the splittunnel LaunchDaemon by
  hand, and splittunnel is a root job in `/Library/LaunchDaemons` that `verify_fleet.sh`
  deliberately does not manage — so a broken pointer there is the kind that costs an hour
  on the day the daemon needs reinstalling. Now points at
  `Media-Syncer/scripts/split_tunnel.sh` lines 57-63, where the `sudo` steps actually live.

**Also cleaned up that evening: three ORPHANED shell loops** (pids 67843, 98158, 98499),
left behind by earlier assistant sessions that had already exited — parent `launchd`, running
3-4.5 hours. Two were `until ! pgrep -f "w64_plan"; do sleep 20; done` waiters that could
never exit: **the polling shell's own command line contains the pattern, so `pgrep` matched
the waiter itself, forever.** Harmless but immortal, and an uninstall would not have reaped
them. If you ever write a `pgrep`-until-gone waiter, match on something the waiter does not
itself contain.

---

---

## 6. Honest limits — do not let these surprise you

**Five of the seven entries here were defects, not facts of life, and were fixed on the
evening of 2026-09-12.** They are kept with their outcomes because the pattern matters: a
limit that has sat in a handoff long enough starts reading like physics. Three of these had
a working tool one import away; one was a tool that was confidently WRONG; one had been
misfiring in the logs for a month. Re-test a limit before inheriting it.

* ~~**TVMaze and TMDB disagree about this show, and Jellyfin uses TMDB**~~ — **the premise
  was wrong.** This said closing it "needs an API key the fleet does not have". The fleet
  has `~/.config/api-keys/tmdb_api_key` AND `tmdb_read_access_token`, and `tmdbguide`
  already speaks TMDB fluently — `season_shape` returns TMDB's own per-season counts and
  names. Nothing had to be taught.
  **`scripts/audit_provider_disagreement.py`** now prints every season where the two
  providers diverge (35 named seasons across 88 shows; 13 more have no pinned `<tmdbid>`).
  **It is a REPORT, and that part was tested rather than assumed.** It was first built as a
  blocking guard forcing `owned` on a disagreeing season and replayed over all 790
  historical plans the way §1's arc guards were: comparing counts rejected **47/790, all
  false positives**; requiring a name mismatch, 15, all false; requiring a *distinctive*
  TMDB name, 2 — and both of those were still false (TMDB calls Bakugan S04 `Mechtanium
  Surge` and the release IS Mechtanium Surge; TMDB calls Cells at Work S02 `Cells at
  Work!!` and the release is `Hataraku Saibou S2`). The only automatic signal available is
  token overlap between a provider's season name and a release's own words, and across
  romaji/English that means nothing. **So a person decides, and the fix for a real one is
  unchanged: file that season `owned`.**
* **Groq's day is 200,000 tokens, which is about thirty agent turns.** UNCHANGED and not
  fixable — it is the provider's free tier, not our code. Mitigated by the confirm prompt
  and the 12-turn cap (§2b); one run that investigates instead of writing can still spend
  all of it, and one did.
* **A season boundary that is off by one arc resolves perfectly** — the LIMIT STANDS: every
  episode still gets a real title and plot, they are simply another arc's, and only a
  census back to the source arc sees it. **But `verify_arc_mapping.py` is no longer part of
  the problem.** It sampled ONE file per season and on this very pack reported Season 01 as
  coming from `09 - Onimonogatari` when all 15 files came from `01 - Bakemonogatari`. It now
  censuses every file, prints the arc distribution per season, and raises a hint when a
  season draws on more than one arc. A tool that is confidently wrong about the exact
  failure it exists to detect is worse than no tool; this one was, for as long as it
  existed. `audit_arc_placement.py` remains the pass/fail census.
  Frozen by `scripts/test_arc_mapping_census.py`.
* ~~**`library.db` over-claims and its reconcile tool went with the searcher**~~ — **FIXED,
  and the diagnosis was never made.** The over-claim was mostly DUPLICATE ROWS:
  `librarydb.add_media` is a bare INSERT with no uniqueness on
  (series_id, mtype, season, number), so every re-file appended another owned row. Measured:
  **41,586 owned rows for 30,831 distinct items — 25.9% redundant**, with one episode of
  `That '70s Show` holding **59 rows**. Two fixes: `librarydb.upsert_media` (new, used by
  `dbhook`) makes a re-file idempotent and lets a better copy overwrite a worse one's
  record; `scripts/reconcile_library_db.py` collapses the existing duplicates and runs the
  stale pass. `reconcile_media` was never lost — the SEARCHER was its only caller, so
  deleting discovery on 2026-09-10 orphaned it. Applied: **41,586 → 28,397 owned rows.**
  The reconcile reads the MOUNT, never the SSD (eviction is not loss), refuses to act on an
  empty inventory, backs the DB up, and **declines to judge comics and light novels at all**
  — the inventory parser sees 8 comics in a 2,692-comic library, so superseding on it would
  have been the fifth false positive below. Frozen by `scripts/test_library_db_reconcile.py`.
* ~~**`library_supervisor` cannot tell a hung Jellyfin from a scanning one**~~ — **FIXED.**
  It had restarted Jellyfin **17 times** (the old count of 14 was stale). `/Items/Counts`
  runs a DB query that a real FUSE scan starves past its 10s timeout, so a scanning Jellyfin
  and a hung one were byte-for-byte identical through it. `/ScheduledTasks` is served from
  memory, keeps answering under scan load, and reports `RefreshLibrary`'s state and progress
  — so they ARE separable. A running scan now defers the restart, but only while its
  progress percentage is still MOVING and only up to `SUPERVISOR_SCAN_GRACE_SEC` (1h): a
  scan wedged at one percentage is restarted like any other hang, and a failing probe counts
  as "no scan" so an unreachable Jellyfin can never defer its own restart forever.
  Frozen by `scripts/test_supervisor_scan_grace.py`.
* **Four of my own checks have now shipped with false positives. Assume there is a fifth.**
  **There was, and it was caught before shipping — twice, in one evening, in the two checks
  written to close the entries above.** The provider-disagreement guard rejected 47/790
  historical plans in its first form and 2/790 in its third, and every one was a false
  positive; it was demoted to a report rather than narrowed further. The `library.db`
  reconcile would have superseded every manga row, because the inventory parser cannot
  enumerate comics. Neither was visible from the code — only from running them over what
  actually happened. **Keep assuming there is another one.** The replay corpus is
  `state/journal.jsonl` and the pattern to copy is `scripts/test_placement_guards.py`.
* **The §1 sidecar-lock fix closes the race for NEW filings only** — the REFUSAL stands,
  the blindness does not. "There is no sweep that finds them" is no longer true:
  **`scripts/audit_unlocked_specials.py`** finds the population in seconds and, more
  usefully, splits it. Of 456 Season-0 episode sidecars, **385 are locked and 71 are not**.
  Of those 71, three are ADJUDICABLE — the fleet authored a title for them in a journal
  plan, so the sidecar can simply be compared against it — and **two disagree** (Vinland
  Saga's specials, where Jellyfin prefixed `6.5 - ` and `18.5 - ` onto the fleet's titles).
  The other **68 are UNDECIDABLE**: no plan covers them, so nothing outside the sidecar
  knows what they should say. §6's original objection was about that 68, and it is correct
  about them — which is why the tool has **no `--apply`** and re-locks nothing. Re-locking a
  wrong title freezes the error permanently. The fix for a disagreeing row is to re-file
  that special `owned`.

---

---

## 7. What changed on 2026-09-12

Eleven commits, `6a9b6b1` .. `21a2fa7`, each named for the thing it establishes rather than
the files it touched:

```
6a9b6b1  The harness marries the release's arcs to the provider's seasons itself
3683f9f  A confirm-mode prompt shrinks to the provider's measured ceiling
05d7f06  A confirm-mode run gets a turn budget the provider can actually afford
c449797  An arc the provider groups and the release splits still gets its real titles
328a344  The confirm prompt budgets for the whole request, and never stubs a locked plot
c24b0c4  A guard that refuses the right answer is worse than none, so prove it does not
76f6cf1  A report that lists what nobody can act on buries what they can
1425e5f  Six free providers, all probed live, and the $10 that bought a gate not usage
f953b5b  The runbook states the rule the owner actually set
02e00fe  A busy provider is skipped, not retried -- the chain has five others
21a2fa7  Own only what Jellyfin's own provider cannot render
```

File by file:

* **`arcmap.py`** — new. Release units, the exact-cover arc→season search, the prompt
  block, the specials matcher, and `strip_release_root`.
* **`library.py`** — `_reject_arc_split_across_seasons` and
  `_reject_season_over_provider_count`, both wired into `validate_plan`.
* **`identify.py`** — the arc block and the specials-metadata block in the full prompt;
  `_confirm_prompt` and the confirm-mode fallback in the provider chain; the release-root
  fix in `_release_structure_block`.
* **`epguide.py`** — cache v3; fetches and exposes the show's unnumbered `specials()` with
  their titles, air dates and synopses.
* **`ai_client.py`** — rate-limit pacing (`_retry_after_sec`, `_request_exceeds_limit`) and
  a provider-aware transcript budget (`max_context_chars`), so a measured ceiling now bounds
  tool results and elision rather than only the opening prompt.
* **`ai_runner.py`** — passes each provider's measured ceiling into the agent loop.
* **`config.py`** — `CONFIRM_PROMPT_FILE`, `IDENTIFY_CONFIRM_MAX_TURNS`, and groq's real
  token limits recorded where the provider is declared.
* **`prompts/confirm_placement.md`** — new, the short prompt.
* **`scripts/test_arc_mapping.py`** — new, 6 parts, registered in `verify_fleet.sh`
  (33 → 34 blocking checks).
* **`scripts/test_agent_loop_guard.py`** — part 6, the rate-limit pacing.
* **`scripts/identify_capacity.py`** — measures the confirm-mode floor and reports
  `PARTIAL`.
* **`OPERATING.md`** — §4 rewritten for the three capacity verdicts and groq's real limits;
  §6's groq bullet replaced; check count 33 → 34. Later the same day, §4 rewritten again for
  the six-provider chain and the free-only rule the owner actually set (§2a).

Afternoon, after the owner added four keys (gemini, nvidia, mistral, tmdb) and $10 of
OpenRouter credit:

* **`config.py`** — three new providers, every model id probed live first; the free-only
  doctrine restated where the next reader will hit it.
* **`scripts/audit_free_only.py`** — `FREE_PROVIDERS` widened to six; Gemini and Mistral
  removed from `PAID_ENDPOINTS` with the reasoning attached; Cerebras kept there with ITS
  reasoning; the pass message now COUNTS providers instead of asserting "three" (it had
  already outlived the three-provider era).
* **`scripts/media_doctor.py`** — remembers a provider's "no still" refusal; reports an
  unreadable sidecar as a MOUNT fault rather than missing metadata; retries in 60s when
  Jellyfin is unreachable instead of losing a 30-minute cycle (§5a).
* **`library.py`** — `episode_nfo_state()`: `missing` / `unreadable` / `blank` / `ok`.

**The TMDB key is now wired in — NARROWLY, and the narrowness is the point.**
`tmdbguide.py` does NOT decide placement; `epguide` (TVMaze, key-less) still owns the
arc->season mapping. TMDB is asked exactly one question per file: *does Jellyfin's own
provider have this slot, and is its season the same show?* Where the answer is no, that
file — and only that file — gets `"owned": true` and the fleet's own locked metadata.

The owner's instruction was explicit: *"let TMDB still work for all the individual
episodes/files it will work for - only own what is necessary."* For Monogatari that is
**10 files of 103**:

```
  Season 01:  E13, E14, E15   TMDB's S01 stops at 12  -> would render BLANK
  Season 04:  E13             TMDB's S04 stops at 12  -> would render BLANK
  Season 05:  E01-E06 (all)   TMDB's S05 is the 2024 OFF & MONSTER Season
                              -> would render plausible but WRONG titles
```

That last case is the nasty one and `serves()` alone gets it backwards: TMDB *has* slots
S05E01-06, so a slot check says yes — and all six would wear another show's titles. A blank
is obviously wrong; a plausible wrong title is not. `season_identity_matches()` catches it
by comparing TMDB's season NAME against the arc labels.

`owned` is now per-FILE (`library._locks_episode_nfo`), because a whole-plan flag cannot
express "10 of 103". Owning a file is not free: a locked sidecar is the fleet's word forever
and Jellyfin can never improve on it.

**Still open (the ORACLE question, which is different):** switching `epguide` itself to
TMDB is NOT done and should not be attempted without first fixing the cover search.

**TMDB IS NOT WRONG. `arcmap` IS.** An earlier draft of this file said "TMDB's season shape
makes arcmap fill Season 05 wrongly", which blames the provider and would send the next
reader hunting for a bad oracle. It is the search that is too naive, in two specific ways,
and both are fixable:

TMDB's layout is coherent and arguably MORE accurate than TVMaze's. Its S01 is 12, not 15,
because Bakemonogatari 13-15 were web-only releases that never broadcast — TMDB files them
as Specials 2-4, which is correct. Zoku Owarimonogatari as a special is likewise defensible.
And TMDB is the layout that MATTERS, because Jellyfin scrapes it: TVMaze is not "right"
either, it is a different editorial convention that happens to suit the current algorithm.

What the search gets wrong, measured 2026-09-12:

  1. **It cannot split a unit.** A season is filled exactly, from WHOLE release units.
     Bakemonogatari is one 15-file unit and TMDB's S01 wants 12, so it cannot put 12 in S01
     and spill 3 into Season 00 — which is precisely what TMDB describes. S01 goes unfilled.
  2. **Arithmetic is its only anchor.** Hunting for a set summing to 15 for TMDB's S05, it
     found Hanamonogatari(5) + Tsukimonogatari(4) + Zoku(6) = 15 and reported it SETTLED.
     Those arcs aired 2014, 2014 and 2019. That season begins **2024-07-06**. Exact, and
     chronologically impossible.

The second is the deeper lesson and it generalises past this show: **a sum that matches is
not evidence.** The uniqueness argument in §1 assumed coincidental sums would be rare; with
six seasons and eleven units they are not.

**The fix, and the data for it is already on disk.** `epguide` now stores `airdate` per
episode (added the same day for the specials block). Give the cover search two more
constraints and TMDB becomes the BETTER oracle rather than a worse one:

  * **a season's air-date window must overlap the release's era** — a 2024 season cannot be
    filled by a release that ends in 2019, which kills the bad answer outright; and
  * **a unit's tail may spill into Season 00** — which is what makes TMDB's S01 = 12
    reachable from a 15-file arc.

Only then switch the oracle. Doing it in the other order means filing against TVMaze,
changing the oracle, and re-filing the whole pack. See §6's first bullet.


---

## 7a. Evening of 2026-09-12 — closing out

**Monogatari: filed 103/103 (`VERDICT: PASS`), then fully purged.** The purge is what taught
us §3a: deleting the videos is one of five cleanups, and four of them do not happen by
themselves. Final state verified at 19:46 — 0 hits on disk under `~/MediaLibrary` and
`~/Media` for any of the nine title aliases, 0 entries in `remote_inventory.json`, no live
reaper queue, nothing in `~/Downloads`, and 0 rows in Jellyfin after a `/Library/Refresh`.

What had to be removed by hand, and would have been missed:

* **78 orphan sidecars** (`.nfo`, `-thumb.jpg`, `-poster.jpg`, `._*`) left behind across six
  season folders after the videos were gone. These are what made the "empty" tree look full.
* **An empty Jellyfin Playlist**, `Monogatari Series - Watchable`.
* **An empty Jellyfin BoxSet**, `Kizumonogatari Collection`.
* **An orphan Video row** for `Monogatari (2009) - S04E1 Part 2` — note the folder name has
  no "Series" in it; it was a leftover from an earlier bad run pointing at a path that no
  longer existed. A `searchTerm` sweep found it; a path-prefix sweep would not have.
* One stray `Kizumonogatari Part 2: Nekketsu (2016).nfo` in `Movies` with no film beside it.

The 61 pool copies drained cleanly on their own and the synthesized mount tree vanished with
them at 19:44:32, exactly as §3a describes. **Do not fight that part; wait for it.**

**Then the purge tried to undo itself, which is the find of the evening.** At 19:59 — twenty
minutes after everything read clean — the ingest daemon **re-adopted the Monogatari torrent**,
because its journal record still said `downloading`. `_readopt_chunked` re-adds from the
source `.torrent` under `finished/`, so deleting the torrent from qBittorrent (OPERATING §6
step 5) had not stopped it. It self-corrected in 21 seconds, since `chunk_done` already held
all 103 files, and I re-verified everything at zero afterwards: library, `~/Downloads/.torrent-ingest`,
`remote_inventory.json`, the reaper queue, and qBittorrent (0 torrents). **Had the pack been
mid-download it would have resumed.** `OPERATING.md` §6 now carries steps 6 and 7 for this;
§3a carries the long version.

**Ghost in the Shell Arise Borders 1-5: complete.** See the postscript in §5b. The run filed
only the missing film into a library that already held four, which is the per-file ownership
model of §1 doing its job on a real case rather than a fixture.

**One Pace Drum Island 08: the re-cut REPLACED the old file, which is the whole point.**
Filed 19:57:55 by nvidia/nemotron after a 17-minute run. `S13E08 - Hiriluk's Cherry
Blossoms.mkv` went 771,284,789 -> 1,064,717,453 bytes, byte-exact to the torrent, same
filename, no duplicate left behind. It filed `owned=True`, and that is correct rather than
heavy-handed: a One Pace re-cut spans manga chapters 153-155 + 142 and anime episodes 90-91,
so no provider has an episode to map it to. The locked sidecar carries the run's own title
and a 300-character plot naming those chapters — a real plot, not the stub that
`_MIN_USEFUL_PLOT` exists to refuse. **Check that after any `owned=True` filing**: a locked
sidecar is the fleet's word forever.

**Two stale cross-references fixed.** `ai_models.py` and `identify.py` both cited
"HANDOFF §3b", which stopped existing when §3 became history; both now point at §4b, where
the deploy-kills-an-in-flight-identify lesson actually lives. `OPERATING.md`'s arc-mapping
pointer was rewritten for the same reason. **If you renumber a section in this file, grep
the repos for the old number** — the code cites it.

**Fleet state at close:** `verify_fleet.sh` ALL CHECKS PASSED (35 checks); `fleet_doctor`
0 findings for three consecutive passes; `fleet_health` all clear; `media_doctor` 0 shows
flagged and 0 pending human/AI review; all five repos clean and pushed. The live daemons
(`MediaFS`, `MediaSync`, `Predownload`) logged no tracebacks today.

**One caution that is NOT a bug.** `TorrentReap.err` carries a `CRITICAL - purge_batch
failed ... rclone.conf: No such file or directory` at 14:33. The config is rewritten
periodically by the MEGA supervisor, and the reaper caught it mid-write. This is the
designed behaviour — the batch is left in `.processing` and retried — and the same queue
drained successfully at 19:44. Do not "fix" it by removing the retry.

---

---

## 7b. Late 2026-09-12 — §6 worked through

The owner asked for the §6 limits to be fixed. **Five of seven were defects rather than
facts of life**; the detail is in §6 itself, which now carries each entry's outcome. What is
worth carrying forward is the *shape* of what was wrong, because it will repeat:

* **Three had the fix one import away.** The TMDB key that §6 said the fleet did not have
  sits in `~/.config/api-keys/`, next to a `tmdbguide.py` that already spoke TMDB.
  `reconcile_media` was not lost with the searcher, it was orphaned when its only caller
  went. Jellyfin's `/ScheduledTasks` was always there. **Nobody had re-checked the premise**
  — a limit written down once reads as settled, and these had all been restated across
  several handoffs without anyone testing them again.
* **One was a tool that was confidently wrong** (`verify_arc_mapping.py`, sampling one file
  per season). Worse than absent, because its output reads like a verdict.
* **One was misfiring in the logs the whole time** — 17 Jellyfin restarts, each one logged,
  none investigated.
* **Two were NOT fixable and are still not**, and both are now documented with the
  measurement that says why rather than an assertion: groq's 200K/day is the provider's,
  and the 68 undecidable Season-0 sidecars genuinely cannot be adjudicated from outside.

**The §6 false-positive warning earned its keep twice in one evening.** The
provider-disagreement check rejected 47 of 790 historical plans in its first form and 2 in
its third — all false positives — and was demoted from a blocking guard to a report rather
than narrowed a fourth time. The `library.db` reconcile would have superseded every manga
row, because the inventory parser sees 8 comics in a 2,692-comic library. **Neither was
visible from the code.** Replay anything that rejects work before you ship it; the corpus
is `state/journal.jsonl` and the pattern is `scripts/test_placement_guards.py`.

**Applied to live state:** `library.db` went 41,586 → 28,397 owned rows (10,755 duplicates
collapsed, 2,517 stale rows superseded, 83 restored, 825 series skipped as unverifiable),
backed up first to `state/library.db.bak-reconcile-*`.

**New, all read-only unless stated:** `scripts/reconcile_library_db.py` (`--apply` writes),
`scripts/audit_provider_disagreement.py`, `scripts/audit_unlocked_specials.py`. **New gate
checks:** `test_supervisor_scan_grace.py`, `test_arc_mapping_census.py`,
`test_library_db_reconcile.py` — 35 blocking checks became 38.

**One thing deliberately NOT done.** `_require_owned_where_providers_disagree` was written,
replayed, and then *deleted* rather than shipped behind a flag. A guard with a measured
false-positive rate does not become safe by being optional; it becomes a thing someone
turns on later without re-reading why it was off. `library._tmdb_id_for_show` survives
because the audit script uses it.

---

---
