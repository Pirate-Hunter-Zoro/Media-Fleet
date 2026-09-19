# YouTube Ingest

Everything you have saved on YouTube, folded into the Jellyfin library the same way
torrents are.

This repo is no longer a standalone downloader. It is a **source** for Torrent-Ingest's
pipeline: it discovers what is on your YouTube account, downloads it in space-bounded
waves, and then hands the files to that repo's own placement machinery. A YouTube video
reaches the library through the identical door a torrent does, with the same naming,
the same write-once safety, the same locked `.nfo`, and the same MEGA upload afterwards.

## Read this first: it takes THREE repos

Nothing here runs alone, and that is the single most important thing to know before
changing anything. This repo is one of three that have to be reasoned about together, and
a change in any one can break the other two:

| Repo | What it provides here | Break it and… |
|---|---|---|
| **YouTube-Downloader** (this) | discovery, download waves, routing, the audio split | — |
| **Torrent-Ingest** | `library` (validate/apply/verify + the locked-`.nfo` writers), `playlist_watch`, and `config.MEDIA_ROOT` | nothing can be placed; the ingest fails at validation |
| **Media-Syncer** | `scripts/split_tunnel_anthropic.sh`, a **root LaunchDaemon** pinning Google around the VPN exit | every cycle defers, and the ingest does *nothing* — silently, by design |

Full detail for each sibling lives in its own README — `~/Developer/Media-Orchestrator/Torrent-Ingest/README.md`
for the plan API, the ingest state machine and playlists, and `~/Developer/Media-Orchestrator/Media-Syncer/README.md`
for the split tunnel, the MEGA pool and the `mediafs` mount.

**These couplings are asserted, not just documented.** `preflight.py` runs at the start of
every cycle, before any network call, and refuses to place anything if a dependency is
broken — because a broken plan API means every plan would be rejected anyway, so failing up
front avoids a ledger full of failures that blame the wrong layer. Check it any time:

```bash
./run_youtube_sync.sh --preflight
```

It catches a `config.py` appearing here (including a *copied-in* one that would pass a
naive attribute check, and stale `__pycache__` bytecode), `import config` resolving outside
Torrent-Ingest, any borrowed function being renamed or deleted, the plan API changing
behaviour (delegated to Torrent-Ingest's own `contract.py`, which exercises it for real),
and — as a warning rather than a failure — the installed split-tunnel being a stale copy.
That last one is a warning on purpose: stale routes that are currently up still work, so
it breaks nothing today, but the installed copy is what runs at the next boot.

Two couplings that are easy to break by accident:

* **This repo's settings module is `ytconfig.py`, never `config.py`.** Torrent-Ingest's
  modules all do a plain `import config` and must keep getting *theirs*. A `config.py`
  here would win that import — the entry script's own directory leads `sys.path` — and
  `library` would come up holding a module with none of the attributes it needs.
* **The cycle refuses to run while Google egresses through the VPN** (`REQUIRE_DIRECT_EGRESS`).
  That is deliberate, and it means the most likely cause of "the ingest silently does
  nothing" is *not in this repo at all* — see Troubleshooting.

---

## What you need to do

**Nothing, ongoing.** This daemon never signs in to YouTube, so there is no credential to
export, expire, or refresh.

**Never reintroduce a credential here.** Authenticating with exported browser cookies puts
every call under the account, so a backlog becomes hundreds of authenticated requests per
cycle — which is what a scraped account looks like, and Google revokes the session for it
(twice in one afternoon, at 40 minutes and 2 hours, each needing a manual re-export). The
constraint is volume, so it recurs at any scale this daemon actually runs at.

Almost nothing here needs an account. Verified signed-out: a public playlist enumerates
fine, and public videos download fine. Cookies bought exactly two things — reading *which*
playlists had been saved, and Liked videos. Moving the soundtracks out of Liked videos into
a **public** `Soundtracks` playlist removed the last reason to hold a credential, so they
are gone: no cookie file on disk, no expiry, no alert, no chore.

### The trade

A playlist you newly save on YouTube is **not** discovered automatically any more — that
discovery was the one thing that needed the account. Register it once:

```bash
./run_youtube_sync.sh --add-playlist https://www.youtube.com/playlist?list=PLxxxx
./run_youtube_sync.sh --forget-playlist PLxxxx      # to stop ingesting one
```

`--add-playlist` verifies the playlist is readable **signed-out before** accepting it, so a
private one is refused with an explanation rather than accepted and silently broken every
cycle forever.

So `state/playlists.json` is the source of truth for *which* playlists to ingest, and
YouTube remains the source of truth for what is *in* them. **Add a video to any registered
playlist and the next cycle picks it up** — no credentials, no action.

### Requirement: the playlists must be public

Anything you want ingested has to be readable without an account. In practice that means
using a public playlist instead of Liked videos — which is also strictly better, because a
public playlist is something you can see, share, and reorder, and Liked videos is not.

### Install

```bash
bash startup.sh
```

Checks the tools, checks the Torrent-Ingest pipeline is present, installs the launch agent,
runs once, then every **5 minutes** (`StartInterval` in the plist == `ytconfig.POLL_INTERVAL_SEC`).
The short sweep keeps a newly-added playlist item from sitting for up to an hour; each cycle is
a cheap `yt-dlp --flat-playlist` enumeration of every registered playlist, so it stays gentle
on YouTube. It does **not** install the split-tunnel daemon — that one is root
and stays deliberately manual.

The launcher (`run_youtube_sync.sh`) runs the daemon under **Torrent-Ingest's conda env**
(`…/envs/torrent_ingest_env/bin/python3`), not `command -v python3`. Two reasons: this repo
already imports Torrent-Ingest's `config`/`library`/`classify`, so that env has the
dependencies; and it is the interpreter that already carries the **Full Disk Access** grant
(iCloud Drive + Google Drive + Downloads + volumes). `command -v python3` resolves to
Homebrew's `python@3.14`, whose TCC grants silently lapse on every `brew upgrade python` —
which is how the iCloud-Drive prompt that blocks the `Soundtracks/` filing kept coming back.
Both music folders are inside TCC-protected cloud trees, so the same grant is what lets a
track land in Drive's `Music/` too. One stable interpreter, one permanent grant.

`startup.sh` and `run_youtube_sync.sh` pull this repo before running, and they run headless
under launchd — so git has no TTY to prompt on. Use an SSH remote
(`git@github.com:Pirate-Hunter-Zoro/YouTube-Downloader.git`, backed by `~/.ssh/id_ed25519`),
not HTTPS: an HTTPS origin with no credential helper fails every pull with
`fatal: could not read Username for 'https://github.com': Device not configured`. If a clone
ever arrives on HTTPS, fix it with
`git remote set-url origin git@github.com:Pirate-Hunter-Zoro/YouTube-Downloader.git`.

## The VPN: leave it up, route Google around it

**Still worth having, for a different reason than before.** With no session to lose, this
is now about download reliability: a rotating commercial-VPN **datacenter** IP gets
rate-limited and `403`-ed by YouTube far more than a residential one, and each failure
wastes a wave. (It originally existed to stop the authenticated session being *revoked* —
that hazard is gone with the credentials, but the throttling one is not.)

The fleet rotates Tailscale **Mullvad** exit nodes for MEGA throttle avoidance, so the
machine's public IP is a datacenter address in a different country every few minutes.

The check is `route -n get 142.250.72.14`: a `utun*` interface means Google traffic is
egressing through the tunnel; `en0` means the split tunnel is in force.

The fix is not to take the VPN down. It is to pin Google to the physical gateway, exactly
as `160.79.104.0/21` is already pinned for Anthropic — same daemon, same reasoning, same
one-time `sudo`:

> **Media-Syncer → `scripts/split_tunnel_anthropic.sh`.** It now pins Google's published
> netblocks (`gstatic.com/ipranges/goog.json` — 99 IPv4 prefixes, regenerated daily,
> cached locally so a failed fetch never drops the routes) alongside Anthropic's. Install
> with the `sudo` steps in that script's header. Preview first with `--dry-run`; it needs
> no root and changes nothing.

Then: VPN stays up permanently, Mullvad rotation continues untouched, torrents keep
exiting through Mullvad, MEGA avoidance is unaffected (it is on its own infra), and
YouTube only ever sees one stable residential IP.

**The tradeoff, plainly:** your ISP sees YouTube traffic as YouTube traffic. Same
concession already made for Anthropic, and a different risk class from torrenting.

**IPv6** is deliberately not pinned — it needs a physical-interface v6 router whose
discovery Tailscale owns, and a half-working v6 route is worse than none (v4 direct while
v6 still tunnels would silently defeat the whole thing). The leak is closed at this end
instead: every `yt-dlp` call passes `--force-ipv4` (`ytconfig.FORCE_IPV4`), so this daemon
can never reach Google over v6 and slip back out through the exit node.

There is no no-root alternative that actually holds. `--source-address` binds a local
address but does not change the route lookup, so packets would leave through the tunnel
carrying a physical-interface source and be dropped; a local proxy follows the same
routing table; per-process routing on macOS means PF rules, which is also root and far
more fragile than 99 static routes.


---

## What gets ingested

| Source | Ingested |
|---|---|
| Any **public** playlist you register | **yes** — as library media, or as audio if it is in `AUDIO_PLAYLIST_DIRS` |
| Liked videos (`LL`) | **no** — account-only; use a public playlist instead |
| Watch Later (`WL`) | **no** — excluded by id and by name |
| History (`HL`) | **no** — excluded by id and by name |
| A local URL list | **no such thing** — the account is the only source of truth |

Playlists come from `state/playlists.json`, populated by `--add-playlist`. There is no
account feed to read and no local URL list to edit — the registry *is* the operational
list, and it is the only one.

## Where things land

### Videos → the library, routed by judgment

Each new video is routed by a headless AI run, which also **writes its metadata**.
Nothing here is on TMDB or TheTVDB — a video essay has no provider entry — so there is
no scraper to fall back on and whatever it writes *is* the metadata, locked into the
`.nfo` (`lockdata=true`) so Jellyfin serves it and never guesses over it. This is the
same "owned" mechanism One Pace uses in Torrent-Ingest, for the same reason.

Four routes:

* **`series`** — the playlist becomes one show. Everything in `Season 01`, named
  `Show (Year) - S01E07 - Episode Title.mkv`.
* **`existing_show`** — the video belongs to a show *already in the library* (a fan
  episode, an official companion piece). It is filed there, at a season/episode number
  the run has to read off disk and the engine then confirms is genuinely free.
* **`movie`** — the video stands on its own as a work: a feature-length documentary, a
  standalone short film, a concert film. It goes to `Movies/Title (Year).mkv` and gets
  a locked movie `.nfo`.
* **`skip`** — genuine junk (a channel trailer, an advert, a duplicate re-upload),
  recorded with a reason so it is never fetched again.

**A playlist is a hint, not a cage.** That was the explicit design ask, and the third
route is what delivers it: a 90-minute documentary sitting in a playlist of 8-minute
explainers is a *film*, and filing it as "episode 12" would bury it. Equally, not every
long video is promoted — a livestream VOD or a two-hour tutorial is still an episode.

### Audio → a music folder, chosen by playlist

An OST rip, a character theme or a song is not library media. It is filed as
`<Clean Track Title>.mp3` with cover art embedded, into a flat cloud-synced folder that
already exists — no placement plan, no `.nfo`, no episode number, no MEGA upload (the
cloud tree it lands in is its durability).

There are two such folders, and **which playlist a track came from decides which one it
lands in** — `ytconfig.AUDIO_PLAYLIST_DIRS`:

| Playlist (by id) | Files into |
|---|---|
| `Soundtracks` — `PLJtTPjwghzms` | `~/Library/Mobile Documents/com~apple~CloudDocs/Soundtracks/` (iCloud) |
| `Download` — `PLSLJ9WOPSCSU` | `MUSIC_DIR` (Google Drive `My Drive/Music/`; set in `.env`) |

A map rather than a list because the destination is genuinely per-playlist: the OST
folder and the Drive folder belong to different people, so "audio goes here" cannot be
one constant. Add another pair to that map to add another music playlist, then register it
with `--add-playlist` — those are two separate steps, and the routing one is the one
easy to forget: an unmapped playlist ingests as **library media**, not as audio.

The routing is a name, not a classifier. Every playlist **not** in that map goes through
the library pipeline — a short OST-looking clip inside a show playlist is an
episode/short to place, not a track. The retired length/title/AI classifier used to split
a "track" out of any playlist, which was filing soundtrack-looking shorts from everywhere
into `Soundtracks/`.

**Dedupe is per-destination, and it recurses.** A track whose cleaned title already
exists in *its own* destination is recorded and never downloaded — but the two folders
never suppress each other, so the same piece can legitimately sit in both. The scan walks
subfolders, because the Drive folder sorts part of itself into them by hand (`Church/`,
`Folk music/`, …) and a song already filed in one of those is still a song we have; a
top-level-only scan would drop a second copy at the root every time.

**A destination is never created, only used.** Both folders live in a File Provider tree
(iCloud Drive, Google Drive), and an unmounted tree is indistinguishable from a path that
does not exist — so `mkdir -p` would cheerfully build the whole chain locally and file
every track into a plain directory that nothing ever syncs, silently, forever. Instead
the track fails, is retried next cycle, and the condition is named up front: `preflight`
warns (not fails — it stops one playlist, not the cycle) and `--status` shows each folder
with `[ok]` or `[MISSING — not mounted?]`.

---

## Storage discipline

Being careless here would break the whole fleet, so three rules are enforced:

* **Downloads never touch the library root.** They go to `~/Downloads/.youtube-ingest/`.
  This is the same rule Torrent-Ingest enforces, for the same reason (see its README,
  *Torrents too large for the SSD*): heavy write I/O inside the library root starves the
  directory reads mediafs serves to Jellyfin and wedges the mount. The dot prefix also
  keeps an in-flight file invisible to Media-Syncer's uploader scan and to the reaper's
  delete detection.

* **Work happens in waves with a live disk budget.** Each cycle downloads a wave whose
  estimated sizes fit inside `WAVE_FRACTION` of the disk (and never breaches the free-space
  floor), places it, frees the scratch copy, then takes the next wave. A 900-video backlog
  flows through a few GB of transient disk instead of demanding room for all of it. Waves
  spill across cycles; there is also a per-cycle video cap and a per-video size ceiling.

* **The floor is defended live, not just in the plan.** A wave's budget is an *estimate*,
  and an estimate committed to before the first byte lands cannot notice it was wrong.
  So `download_videos` fetches one video at a time, re-reading real free space between
  each and stopping the moment the next would cross the floor — the same pattern
  Media-Syncer's `predownload.py` uses on the same disk. Each fetch also carries
  `--max-filesize`, set to half the live headroom, so a video whose true size overruns its
  estimate is aborted by yt-dlp mid-download rather than discovered afterwards from the
  wrong side of the floor. Half, because that flag applies **per format** — a `bv*+ba`
  selection checks video and audio separately — and the parts coexist with the merged
  output during the remux, so peak usage is about twice the final file. An aborted video
  is simply not recorded, so it returns next cycle when there is room.

  This is worth the extra process spawns because the batched version failed in production:
  on 2026-08-07 a 12-video wave estimated at 10.5 GB actually consumed 20.4 GiB and took
  the disk 8 GiB *through* the floor, because `ASSUMED_VIDEO_BYTES` was 900 MB against a
  measured mean of 1744 MB. Estimates are now padded by `SPACE_SAFETY_FACTOR` as well, but
  padding narrows the error and only the live re-check bounds it.

* **The floor sits deliberately *below* Media-Syncer's, not level with it.** `MIN_FREE_BYTES`
  is its 80 GiB floor minus `FLOOR_DIP_BYTES`, and that gap is what makes this ingest move at
  all. Matching it exactly looks like the conservative choice and is actually a deadlock:
  Media-Syncer's tiering drives the disk *down toward* its own floor by design, so the
  headroom left here settles at roughly zero and stays there. The dip is safe where a
  genuinely lower floor would not be, because it is bounded and transient — one wave, then
  the scratch copy is freed, leaving room for a ~43 GiB torrent throughout. `preflight.py`
  enforces the band in both directions, because a floor that is too *high* fails silently:
  every check reports healthy while nothing downloads.

* **Publishing costs no second copy.** The Downloads volume and the library root are the
  same physical volume, so `apply_plan` hardlinks the scratch file into its staging dir
  rather than copying it. Once it is placed, Media-Syncer uploads it to the MEGA pool and
  the tiering evicts the local bytes as usual — the pool copy is the durable one.

Height is capped at 1080p on purpose: a 4K YouTube re-encode costs several times a 1080p
copy, and every byte is a byte the pool holds and the predictive cache moves.

---

## Dedupe: the ledger

`state/seen.json` is keyed by **YouTube video id**, and that key is the point: the same
video routinely sits in several of your playlists and must be downloaded and filed
**once**. It also means a video is never re-fetched because a playlist was reordered,
renamed, or unsaved and re-saved, and an item you removed from a playlist is not
re-downloaded.

yt-dlp's own `--download-archive` is deliberately **not** used: it marks a download done
the moment the bytes land — *before* the file is placed in the library — so a failed
identify would be permanently recorded as complete.

Each video ends as `placed`, `soundtrack`, `skipped`, or `failed`. A failure retries
across cycles up to four times and is then parked, so one poisoned video cannot stall
every cycle forever. Parked failures are listed by `--status` and released with
`--retry-failed`.

### Episode numbering

Numbers are handed out in **append order**, continuing from the highest episode actually
on disk (read through the mediafs mount, so an evicted episode still counts).

They are emphatically **not** the video's position in the playlist. That was the old
behaviour and it was a real bug: inserting a video at the top of a playlist renumbered
every episode below it and orphaned every file already on disk. The playlist → show
mapping is pinned in `state/shows.json` the first time a playlist places an episode, so
renaming the playlist on YouTube keeps filing into the folder its episodes are in
instead of forking a second show.

A destination that already exists is **refused**, never overwritten — the library is
write-once, and a "pre-existing" skip would otherwise mark a video as placed while
pointing at somebody else's episode.

---

## What this repo borrows

Placement is imported from Torrent-Ingest, never reimplemented — a second copy of "how a
show folder is named" is exactly how two ingest paths drift into filing the same show two
different ways:

| Borrowed | For |
|---|---|
| `library.build_library_digest` | the existing library as ground truth in the prompt |
| `library.validate_plan` | the single safety gate in front of any write |
| `library.apply_plan` | dot-staging dir, atomic publish, locked `.nfo` writers |
| `library.verify_applied` | confirm every file landed at its size |
| `playlist_watch.consider_new_episodes` | curated-playlist auto-extend |
| `config.MEDIA_ROOT`, paths, log format | one definition of where the library is |
| `config.AI_BIN` / `config.AI_MODEL` | one definition of which runtime the fleet talks to |
| `config.ai_env` | the headless-run environment (`PATH`, so `Probe` finds `ffprobe`) |
| `config.identify_unavailable` | one ruling on "the run never happened" |

The last three are borrowed for the same reason as placement. `AI_BIN` is a list —
interpreter plus `ai_runner.py` — so call sites splat it (`[*ytconfig.AI_BIN, "-p", ...]`);
pointing the two repos at different runtimes is exactly the drift this prevents.
`identify_unavailable` decides whether a failed run was the API being unable to answer
(no balance, no credential) or a genuinely bad plan. Both ingests must rule identically: a signature one spells and the other does not
is a silent, content-deleting divergence, so Torrent-Ingest's `contract.py` asserts both exports and
`ytconfig` re-exports them rather than restating them.

Because of the module-name collision this creates, **this repo's settings live in
`ytconfig.py`, not `config.py`** — Torrent-Ingest's modules all do a plain
`import config` and must get *theirs*.

### The one change made to Torrent-Ingest for this

`library.validate_plan` accepts an **owned movie** alongside the id-pinned kind, because no
YouTube video can pin a TMDB id: no id, but a real `movie_title` and
`plot`, which `apply_plan` writes into a *locked* movie `.nfo`. Same trade as an owned
episode — the id requirement exists to stop a film shipping blank or fuzzy-matched, and
locked metadata prevents that by construction instead. A findable film must still pin
its id; the escape hatch is only for works that are genuinely on no provider.

---

## Layout produced

```text
/Users/mikeyferguson/Media/
├── Shows/
│   └── <Series Title> (<year>)/
│       ├── tvshow.nfo              (LOCKED — our plot, no provider to scrape)
│       ├── poster.jpg  folder.jpg  backdrop.jpg  season01-poster.jpg
│       └── Season 01/
│           ├── <Series> (<year>) - S01E01 - <Episode Title>.mkv
│           ├── <Series> (<year>) - S01E01 - <Episode Title>.nfo   (LOCKED)
│           └── <Series> (<year>) - S01E01 - <Episode Title>-thumb.jpg
├── Movies/
│   ├── <Film Title> (<year>).mkv
│   ├── <Film Title> (<year>).nfo   (LOCKED — owned movie, no TMDB id)
│   └── <Film Title> (<year>)-poster.jpg
└── ...

~/Library/Mobile Documents/com~apple~CloudDocs/Soundtracks/     (iCloud — "Soundtracks")
└── <Clean Track Title>.mp3

~/Library/CloudStorage/GoogleDrive-<account>/My Drive/Music/     (MUSIC_DIR in .env)
├── <Clean Track Title>.mp3                                     (Google Drive — "Download")
└── Church/  Folk music/  Anime music/  …                       (hand-sorted; scanned for dupes)
```

Artwork is written locally but is deliberately **not** part of the placement plan: the
plan carries only true media (what Media-Syncer replicates), and Torrent-Ingest's
metadata backup already drops `-thumb.jpg` as trivially regenerable. A YouTube
thumbnail is the only poster these titles have, so it is worth writing — just not worth
uploading.

---

## Commands

```bash
./run_youtube_sync.sh                  # one cycle (what launchd runs hourly)
./run_youtube_sync.sh --status         # discovered playlists, ledger counts, parked failures
./run_youtube_sync.sh --preflight      # check the cross-repo dependencies and exit
./run_youtube_sync.sh --dry-run        # discover + plan waves; download and place nothing
./run_youtube_sync.sh --retry-failed   # release parked failures
./run_youtube_sync.sh --playlist LL    # restrict a run to one playlist id
./run_youtube_sync.sh --daemon         # loop in the foreground instead of one cycle
```

## Requirements

```bash
brew install yt-dlp ffmpeg
```

**Keep yt-dlp current** — `brew upgrade yt-dlp` — and it is not housekeeping. A build more
than a month old 403s the media fetch while metadata still works, which reads in the log as
YouTube refusing the video (see Troubleshooting). `preflight` warns once it drifts past
`YT_DLP_STALE_DAYS`.

Plus an **OpenRouter API key** at `~/.config/api-keys/openrouter_key` (the identify step,
which `startup.sh` checks for),
and the Torrent-Ingest repo at `~/Developer/Media-Orchestrator/Torrent-Ingest` (override
with `TORRENT_INGEST_DIR`) — which is also where the agent runtime itself lives
(`ai_client.py` / `ai_runner.py`).

## Logs

* Engine log: `./youtube_sync.log`
* launchd: `~/Library/Logs/YouTubeSync.log` / `.err`
* Alerts needing you: `state/youtube_ALERT.txt`

## Stop / uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.mikeyferguson.youtubesync.plist
rm ~/Library/LaunchAgents/com.mikeyferguson.youtubesync.plist
```

## Config knobs

All in `ytconfig.py`: `AUDIO_PLAYLIST_DIRS` (which playlists are audio and where each
one files), `SOUNDTRACKS_DIR` / `MUSIC_DIR`, `MAX_HEIGHT` / `FORMAT`,
`WAVE_FRACTION`, `MAX_VIDEOS_PER_CYCLE`, `MAX_VIDEO_BYTES`, `IDENTIFY_BATCH_SIZE`,
`SKIP_PLAYLIST_IDS`, `FORCE_IPV4`, `REQUIRE_DIRECT_EGRESS`,
`PLAYER_CLIENTS` / `PLAYER_CLIENTS_FALLBACK`, `MIN_FREE_BYTES` / `FLOOR_DIP_BYTES`,
`ASSUMED_VIDEO_BYTES` / `SPACE_SAFETY_FACTOR`. Cadence is `StartInterval`
in the plist.

## Troubleshooting

`./run_youtube_sync.sh --status` answers most of this in one screen: jar health and when
`yt-dlp` last refreshed it, whether the split-tunnel is genuinely in force, the playlist
registry, ledger counts, and any parked failures. Start there.

**"It runs but ingests nothing, and the log looks calm."** Check egress *before*
suspecting anything in this repo. The cycle deliberately defers while Google would leave
via the VPN, and it says so in the log. `route -n get 142.250.72.14` reporting a `utun*`
interface means the split-tunnel is not in force — the usual causes are that the daemon
was never installed, or `/usr/local/bin/split_tunnel_anthropic.sh` is a **stale copy**
from before a change in Media-Syncer's repo. Editing the installed copy needs `sudo`, so
it cannot be done unattended, and it is the step most likely to have been skipped.

**"Every wave defers on disk budget, but `--status` says there is plenty free."** Both are
true, and the gap between them is the answer. `--status` reports raw free space; a wave is
planned against `free - MIN_FREE_BYTES`. If this repo's floor has been set level with (or
above) Media-Syncer's `SSD_MIN_FREE_BYTES`, that subtraction lands at approximately zero
forever, because Media-Syncer's tiering deliberately holds the disk near its own floor —
so the log reads `900 MB needed, 485 MB usable` while `--status` cheerfully reports 80 GB
free. `--preflight` now names this in both directions; see *Storage discipline*.

**"The log says `holding the 70 GiB floor` and the wave stopped early."** Working as
intended, not a fault. The wave was planned on estimates; that line means real free space
was re-read between videos and the next one would have crossed the floor. The remainder is
unrecorded and returns next cycle. The same is true of `at or below the 70 GiB floor` —
that one means the disk was already at the floor when the cycle started, usually because a
previous wave's files are still waiting on Media-Syncer to upload and evict them. It clears
itself as the uploader drains; nothing to do.

**"A playlist isn't being ingested."** Check it is registered (`--status` lists them) and
that it is **public** — `--add-playlist` refuses a private one, so if it never registered,
that is why. Adding *videos* to an already-registered playlist needs nothing.

**"A show's episode numbering looks wrong."** Numbers are append-order from what is on
disk, never playlist position, and a destination that already exists is refused rather
than overwritten. If the ledger and disk disagree, disk wins on the next cycle; the
playlist → folder pin lives in `state/shows.json`.

**"An episode failed and won't retry."** Four failures parks a video so one bad item
cannot stall every cycle. `--status` lists parked ones; `--retry-failed` releases them.

**`HTTP Error 403: Forbidden` on the download.** This one is worth understanding, because
it lies about what it is — and it has **two** causes that produce a byte-identical log line.
Check the cheap one first:

> **Is yt-dlp current?** `yt-dlp --version` against `brew outdated yt-dlp`. A build more
> than a month or so old extracts metadata fine and then 403s the media fetch, because
> YouTube has moved its player protocol out from under it. That is the *same* symptom as
> the PO-token gating below, so the fallback chain dutifully retries every client and every
> one of them 403s for a reason no client can fix.
>
> Measured 2026-09-05: yt-dlp 2026.07.04 403'd two tracks on `default`, `web_embedded` and
> `web_music` alike, on every attempt across three cycles. `brew upgrade yt-dlp` to
> 2026.08.19 and both downloaded immediately — same machine, same route, same client, no
> code change. Six weeks of drift was enough to do it.
>
> `preflight.py` now says so up front (`YT_DLP_STALE_DAYS`, a warning rather than a failure
> — an old build still places most things). It is the dependency here that rots fastest,
> and it was the only one with no assertion of its own.

The second cause is real gating, and it is what the fallback chain exists for. YouTube's
default `web` client issues media URLs that require a **PO Token** (proof-of-origin).
Without one, metadata extraction succeeds and then the media fetch 403s — so it presents as
a transient network failure, gets recorded as one, and is retried forever with the
identical result.

It is fixed by `ytconfig.PLAYER_CLIENTS_FALLBACK`: the primary pass runs yt-dlp's default
client set (best coverage), and anything it cannot fetch is retried with
`player_client=web_embedded`, the embedded-player client. Embedded players are not under
yt-dlp's `WEB_PO_TOKEN_POLICIES`, so their media URLs need no proof-of-origin token and
download where `web`/`web_safari` 403 — while still exposing the full format set (the same
audio ladder and 144p–1080p video ladder as `web`).

This replaced `tv`, the previous fallback, which had two strikes against it: it returns only
storyboards for much long-form content, and YouTube now answers its player request with
"The page needs to be reloaded", so it rescues nothing. Measured 2026-08-19: the default
client 403'd the audio track `mIHAyQ9wa6o` on every retry; `web_embedded` downloaded it
immediately (opus 123k → mp3).

Note `default` is deliberately **not** in either list, not even last. yt-dlp merges the
format lists from every client given and the selector picks the best across all of them, so
including a PO-token-gated client lets a gated format win selection and 403 again.

The fallback is a **chain**, not a single client: `web_embedded` first, then `web_music`,
each tried for whatever is still missing — so one client breaking does not need a human to
fix it. If a future YouTube change gated both, the chain is an env override
(`YOUTUBE_PLAYER_CLIENTS_FALLBACK`, comma-separated), so widening it needs no code change.

Two things it is **not**, both ruled out by measurement here: it is not the VPN
split-tunnel (successes and failures both fetched media over the same interface — see
below), and it is not throttling (a retry reproduces it exactly rather than sometimes
succeeding).

### What the split tunnel does *not* cover: the media CDN

Worth knowing before blaming it for a 403, because the obvious hypothesis is wrong and
takes a while to rule out by hand.

The split tunnel pins Google's **published** netblocks (`gstatic.com/ipranges/goog.json`).
Video *bytes* do not come from those. They come from a `rr8---sn-….googlevideo.com` edge,
and on this connection that resolves into **`12.12.38.0/24` — AT&T space**, a Google Global
Cache node hosted inside the ISP. Those addresses are not Google's, are not in `goog.json`,
and are therefore **not pinned**: measured 2026-09-05, every media fetch left via `utun4`
while the API calls went out `en0`.

So the claim "YouTube only ever sees one stable residential IP" is true of the API
conversation and **not** of the download. It is left as-is deliberately: the GGC hostname
rotates per request and its ISP-owned prefixes cannot be enumerated into static routes the
way `goog.json` can, and it is measurably not causing trouble — the working and failing
tracks in the 403 investigation above took the *same* tunnelled path, and the fix was the
yt-dlp build.

The practical consequence is for *diagnosis*: `--status` reporting the split-tunnel as
`active` means the API path is pinned. It says nothing about where the bytes came from, so
it is not evidence either way when a download fails.

## A design principle worth keeping

Failures here are built to **self-heal rather than alert**, because this runs on a machine
nobody sits in front of. An alert that needs a human is a task that will eventually stop
getting done, and the automation rots quietly.

So: a
cycle that would waste a wave on a throttled VPN exit defers instead of burning it; a
download the default player client cannot fetch is retried with another rather than failed;
a video that keeps failing retries on a schedule and is then parked rather than blocking
everything behind it.

The strongest version of that principle is what removed the credentials entirely: the most
reliable way to stop a recurring chore is to delete the thing that generates it.

When adding to this, prefer a harder one-time setup over anything that introduces a
recurring chore, and make the safe direction the automatic one.

## Notes / limits

* **There are no credentials.** Nothing to export, expire, or refresh. The cost is that a
  newly-saved playlist must be registered once with `--add-playlist`.
* A **large saved playlist** (hundreds of videos) syncs across several cycles by design.
  It is called out in the log when discovered so it is an obvious event, not a silent
  month of downloading.
* `existing_show` routing is restricted to season 1 and above — specials are not routed
  from YouTube, because provider Season-0 ordering is unreliable enough that
  Torrent-Ingest holds specials to a stricter bar than this source can meet.
* If Jellyfin is down, placement still succeeds; only the rescan is skipped.
