# Media Syncer

An autonomous sync system that replicates a media library across a pool of MEGA cloud storage accounts.

It runs on the **Mac Mini only** — a single host. New media lands on the Mini's SSD library root (~/Media) (put there by the sibling **Torrent-Ingest** daemon), and this daemon uploads any file not yet on any remote and downloads any file missing locally, keeping the local library and the MEGA pool in agreement. It never overwrites or deletes an existing remote copy — **except the two churn classes**: **One Pace**, whose newer re-cuts must replace older versions, and **anime quality upgrades** (a strictly-better copy Torrent-Ingest replaced in place), both replaced via a gapless route. The correctness property is **write-once**: since no ordinary remote file is ever deleted, there is no gap for a stale copy to slip into, so nothing can race a stale version into the fleet.

> **Single-host by assumption, not by configuration.** There is no host-mode toggle and no staging role: every path assumes the Mini and its SSD library root. The MEGA pool credentials (`rclone.conf`) are machine-local and untracked — this repository is public — and **only the Mini runs the daemon.**

Designed to run persistently as a macOS background daemon via `launchd`, the system maintains stateful synchronization with the shared MEGA storage pool.

---

## The fleet: fed by Torrent-Ingest

**The three repos of the fleet, all on the Mini.** Each documents its own half of every
shared contract, so read the sibling README rather than inferring behaviour from this one:

| Repo | Role | Its README covers |
|---|---|---|
| **Media-Syncer** (this) | replication to the MEGA pool, the `mediafs` virtual library, pre-download and eviction | the sync cycle, the purge/rename runbooks, the mount |
| **Torrent-Ingest** (`~/Developer/Media-Orchestrator/Torrent-Ingest`) | acquisition and placement: torrents, GetComics, drive ingest, the plan API, Jellyfin health | the ingest state machine, `library.validate_plan`/`apply_plan`, the reaper, playlists |
| **YouTube-Downloader** (`~/Developer/Media-Orchestrator/YouTube-Downloader`) | YouTube discovery and download, placed through Torrent-Ingest's plan API | discovery, download waves, routing, the soundtrack split |


Media-Syncer is the **replication layer** of a small multi-daemon fleet on the Mini. Its upstream is **Torrent-Ingest** (`~/Developer/Media-Orchestrator/Torrent-Ingest`), the sibling daemon that turns "drop a `.torrent` into an iCloud folder" into "the title is correctly named, correctly placed, and watchable" — video organized to Jellyfin/TMDB conventions, comics/manga to YACReader conventions — landing every file on the SSD library root (~/Media). Everything Torrent-Ingest writes to the SSD library root this daemon then sees, on its next cycle, as a new local file absent from every remote and **replicates out to the MEGA pool**. Local Jellyfin playback never waits on that upload; the MEGA copy is durability, not a serving path. **YouTube-Downloader** (`~/Developer/Media-Orchestrator/YouTube-Downloader`) is the fleet's third daemon: it discovers and downloads YouTube content and places it through Torrent-Ingest's plan API, so a YouTube video reaches the pool through the same door a torrent does.

The two repos are deliberately kept in step:

* **Shared layout** — both mirror the same `Shows/` / `Movies/` / `Comics/` tree beneath their media root.
* **Shared churn classes** — both treat `config.ONE_PACE_PREFIX` (`Shows/One Pace (2013)/`) as a content class that overwrites in place, and both honour a `replacements.jsonl` queue (written by Torrent-Ingest on an anime quality upgrade, drained by this daemon) for the same gapless replace. A One Pace re-cut must replace the older version gaplessly on **both** the local SSD copy (Torrent-Ingest's atomic `os.replace`) and the MEGA copy (this daemon's fitting-aware replace/relocate). Keep the two prefix strings identical — they name the same folder in the same SSD library root.
* **Shared safety idiom** — Torrent-Ingest stages into dot-dirs (`.ingest-staging/`, `.torrent-ingest-incoming/`) precisely because *this* daemon's local scan prunes dot-dirs, so a half-applied ingest is never uploaded as a partial.
* **The Jellyfin metadata backup lives on the Torrent-Ingest side** — its nightly `scripts/backup_metadata.py` mirrors the library's `.nfo`/artwork plus its own state to `config.METADATA_BACKUP_REMOTE` (default `vm_mega1`) under a `metadata-backup/` prefix this pool never syncs. That second, invisible footprint is why purging a title has an extra step to clear it — see *Purging a Title*.

---

## What It Syncs

* Video (`.mkv`, `.mp4`, `.avi`, `.m4v`)
* Subtitles (`.srt`, `.ass`)
* Comics (`.cbz`, `.cbr`)

**Local destination:**

* Mac Mini: the real media lives on the SSD library root at `/Users/mikeyferguson/Media` (the daemons' read/write dir), presented to the apps through the `mediafs` virtual mount at `~/MediaLibrary`; full remote path preserved (e.g. `Shows/One Pace (2013)/...` → `/Users/mikeyferguson/Media/Shows/One Pace (2013)/...`). One Pace lives here among the ordinary Shows; only its *overwrite* behavior is special, not its residence

There is a single local media root (`config.SSD_LIBRARY_ROOT` = the SSD library root ~/Media). The remote relative path is mirrored verbatim beneath it, and `config.local_root_for()` is the single source of truth both the remote→local mapper and the local→remote path-strip resolve through, so the two directions can never disagree about where a file lives. (The join is uniform `root / relative_path`.)

**Key behaviors:**

* Streaming chunked downloads (1 GB) via `rclone cat`, applied unconditionally regardless of file size
* MEGA throttling bypass via Tailscale + Mullvad exit-node rotation
* **Write-once, with One Pace as the lone churn class:** the Mini **uploads** any new local file (any file not yet on any remote) and **downloads** any file missing locally; it never replaces an existing copy — a fresher mtime is treated as noise and the baseline is silently advanced. **One Pace** (any remote path under `config.ONE_PACE_PREFIX`) is the only content class that churns, because newer re-cut versions ship and must replace older ones: the Mini re-uploads a newer local re-cut over the existing remote copy, and downloads a newer remote version over its stale local copy
* The Mini **retains everything it uploads or downloads** — it is the library. (Reclaiming space by evicting cold local files whose bytes are safely on a remote is a separate, inventory-verified concern of the planned tier engine, never a blind post-upload delete.)
* **Replace newer versions gaplessly, fitting-aware:** a newer One Pace version replaces the existing copy by the cheapest gapless route available. When the new version still fits on its current remote, it is **replaced in place** — `copyto` over the same remote path (then the remote's MEGA rubbish bin is emptied to reclaim the old version's space), or a chunked overwrite-download on the Mini. When a larger re-cut **no longer fits** on that remote (an in-place overwrite transiently holds *both* the rubbish-binned old copy and the new one before cleanup runs, so the remote needs free space ≥ the **full new size**, not just the delta), the file is instead **relocated**: the new version is uploaded to a *different* remote that has room **first**, and only then is the old copy deleted from the original remote (and that remote's rubbish bin emptied). Both routes are **gapless** — at no instant is the episode absent from the fleet — so there is never a window in which a stale version could slip into a gap. Note this is *not* delete-then-reupload (which deletes first, opening that gap); the relocation path uploads first and deletes last
* **Anime quality upgrades are a second, queue-driven churn class.** Torrent-Ingest replaces an *anime* file in place when ffprobe proves the new copy is strictly better (higher definition or dual audio, losing neither). A replaced file is otherwise invisible to write-once — its mtime drift would be absorbed and the pool would keep the old version forever — so Torrent-Ingest appends the replaced path to `replacements.jsonl`, and `drain_replacements_queue()` (run at the top of the update phase) re-uploads it over the stale MEGA copy with the exact same gapless replace/relocate route, then empties that remote's rubbish bin. The queue is the only signal; ordinary mtime drift is still absorbed, so nothing outside Torrent-Ingest's deliberate replacements ever churns.

---

## Role (single host)

The daemon runs on the **Mini** only. It does all of:

* Uploads new local files (all content classes) that aren't on any remote yet
* Downloads files missing locally, and downloads a newer remote One Pace version to replace a stale local copy
* Re-uploads an existing remote file only when the local copy is newer **and** the path is under `config.ONE_PACE_PREFIX` — the One-Pace-only overwrite rule. For any non-One-Pace file, both a local-newer and a remote-newer drift are treated as noise and silently absorbed into the baseline — never re-uploaded, never overwritten locally
* **Retains everything it uploads or downloads** — it is the library

### Why write-once

**Write-once is what makes the sync safe, independent of host count.** A file is uploaded once and is **never deleted or overwritten** on any remote thereafter — a fresher mtime is absorbed into the baseline, not acted on. With no delete step there is no window in which a file is briefly absent, so a stale copy can never race into a gap (a hazard a multi-writer design has to dodge with a single writer, and which write-once makes structurally impossible). One Pace is the one content class that *does* delete/overwrite, and there the gap is closed by never deleting before the replacement exists: when the new version fits on the current remote it is **replaced in place** (copyto over the same path, then the rubbish bin is emptied); when it is too large to fit, it is **uploaded to a remote that has room first and the old copy deleted only afterward**. Either way the file is never absent.

### Single-residence invariant (one file, one remote)

A direct consequence of write-once: because an ordinary file is uploaded exactly once, to whichever remote has room, and is thereafter never copied, moved, or re-uploaded, **every file resides on exactly one remote — there are no duplicate copies of a path across accounts.** The only thing that ever moves a file between remotes is One Pace relocation, and that uploads to the new remote *before* deleting the old, so a file is on two remotes only transiently and returns to single residence the moment cleanup runs.

This makes `remote_inventory.json` a **complete** path→remote map rather than a lossy one. `build_remote_index` collapses each path to a single remote (alphabetically-last writer wins), which would lose information *if* duplicates existed — but they do not, so the collapse is lossless. Any operation that must locate every copy of a title — most importantly a purge — can therefore be driven entirely off the inventory, with no need to scan all remotes to hunt for hidden duplicates.

---

## rclone Configuration

The pool's account list is a **credential store and is NOT tracked**: this repository is public, so `Media-Syncer/rclone.conf` is machine-local and covered by the root `.gitignore` (copy `rclone.conf.example` there on a fresh host). All remotes with `type = mega` are treated as part of the storage pool — there is no namespacing or filtering.

**No `session_id` or `master_key` line may be committed either** — and since the file is untracked, none can be. Only the durable credentials (`user`, `pass`, `use_trash`) belong in the repo copy. `session_id`/`master_key` are ephemeral, machine-local MEGA auth tokens that rclone regenerates on demand from `user`/`pass`; a cached token is tied to the machine that minted it, so it is stale-on-arrival anywhere else, surfacing as the `go-mega` SIGSEGV panic / `couldn't login` failures the daemon then has to self-heal from (see *Stale-session recovery*). It also silently defeats a hand-run maintenance delete: rclone can exit `0` while the operation actually failed on the dead session, so a delete looks like it succeeded when the file is still there. The daemon's own `purge_mega_session()` only ever strips these tokens from the live `~/.config/rclone/rclone.conf`. The `.githooks/pre-commit` hook is the belt to that pair of braces: it unstages any staged `rclone.conf`/`.env` outright, and rewrites a live credential that reaches another staged file to a placeholder (see `Torrent-Ingest/scripts/test_no_tracked_secrets.py`, verify check #54).

---

## Deployment

Deployment is handled via:

```bash
bash startup.sh
```

### What `startup.sh` does

* Creates the conda environment (`media_sync_env`, Python 3.11)
* Installs dependencies
* Copies configuration files
* Installs the launchd service definition
* Starts the daemon

### Key Design Detail

* One service, installed and loaded on the Mini, operating on its SSD library root (~/Media)

---

## State Files

All state is stored at the repository root and gitignored via:

```bash
*.json
```

* `sync_state.json`
* `remote_inventory.json`
* `free_space.json`
* `exit_node_speeds.json` — the last full exit-node throughput sweep, ranked
* `fast_exit_nodes.json` — the allowlist `get_exit_nodes()` restricts rotation to

The two exit-node files are gitignored for a reason beyond convention: the fast set depends on where *this* machine sits on the network, so propagating one host's measurement to another would hand it an allowlist it never earned. Each host measures its own.

After a successful upload, the Mini records the file's actual mtime as both the local and remote baseline in `sync_state.json`, for every file (it retains all of them). The update phase then stays inert until a genuine drift: for One Pace, a genuinely newer version dropping, at which point the standard mtime-drift logic schedules an overwrite; for any ordinary file, a later drift is silently absorbed into the baseline and never acted on. (The old `[0, 0]` sentinel that ordinary files got under the two-machine staging design — where the Air deleted its local copy after upload — is gone along with that design; the baseline-init handshake still tolerates a `0` on either side, so a legacy sentinel already in `sync_state.json` remains harmless.)

---

## Sync Behavior Overview

The pipeline executes in a tight loop with **no idle waits** between or within cycles. Several phases are role-gated and only execute on the host whose role they apply to.

1. **Quarantine reset** — `clear_quarantine()` empties the in-memory set of remotes that were benched during the previous cycle, and `clear_session_heals()` restores every remote's stale-session heal budget, giving each one a fresh attempt this iteration. See the Per-cycle remote quarantine and Stale-session recovery entries under Shared Mechanisms for what produces entries in those sets
2. **Config refresh** — `git pull` at the top of every loop iteration, then the machine-local `rclone.conf` (untracked; it holds the pool's credentials) is published into `~/.config/rclone/rclone.conf` so rclone actually sees any new remote appended by the provisioner — without this deploy step the append is cosmetic to every rclone process
3. **Remote discovery + free space calculation** — re-enumerated each cycle so newly pulled remotes become immediately active
4. **Sync state load + baseline init** — load `sync_state.json` and populate baselines for any files that exist on both sides without an existing baseline
5. **Update phase** — drift detection, gated on the One Pace prefix:

    * **Local-newer branch**: when the local copy has drifted newer than the baseline, a One Pace file is replaced by whichever gapless route fits. If the current remote's free space is ≥ the new file size, it is **replaced in place on the same remote** via `copyto`, after which the remote's MEGA rubbish bin is emptied to reclaim the old version's space. If it does **not** fit, `relocate_newer_one_pace()` uploads the new version to a different remote with room and then deletes the stale copy from the original (emptying that remote's rubbish bin), updating the in-memory `remote_index` so the rest of the cycle knows the path's new home. For any non-One-Pace file the local copy is not re-uploaded and `sync_state[relative_path]` has its local-side baseline bumped to the live local mtime, so the same drift does not retrigger
    * **Remote-newer branch**: when the remote copy has drifted newer, a One Pace file is overwritten locally via chunked download. For any other case the local file is left in place and the remote-side baseline is bumped to the live remote mtime
6. **Download phase** *(downloader only)* — fetches files missing locally. A failed download removes its own partial: `chunked_download` opens the local file before fetching the first chunk, so a fetch that dies mid-stream would otherwise leave a stub (often zero-byte). That stub is deleted on failure — otherwise the upload phase would later see it as a new local file absent from every remote and propagate it to the cloud. The directories created for the download are pruned too: the phase walks upward from the file's folder removing every directory that holds **no tracked media file** (`VIDEO_EXTENSIONS | COMICS_EXTENSIONS` — the same set the local scan uses), stopping at the first directory that still holds a real video (a sibling episode) and never touching the media root. The test is "media-less," not "empty," because Jellyfin litters `.nfo`/`.jpg`/`.png` metadata into show and season folders — a folder holding only that metadata is removed wholesale (taking the metadata with it) so a failed fetch leaves no orphan show or season skeleton behind
7. **Upload phase** — places new local files (not yet on any remote) on a remote with available space; the Mini uploads any new file of any class. After success the file is **retained locally with a real-mtime baseline** (the Mini is the library), per the State Files section

If the config refresh yields zero usable remotes (missing or empty `rclone.conf`), the loop skips the cycle and `continue`s — the next iteration retries the publish, then the next cycle re-reads it. The daemon is therefore **self-healing** against transient config loss (a host that never created `rclone.conf` must copy `rclone.conf.example` and fill it in; nothing can regenerate credentials from nothing).

### Shared Mechanisms

* **Sleepless loop:** no `time.sleep` calls anywhere in the sync pipeline. Throttle avoidance is split between two non-timer mechanisms — VPN exit-node rotation defeats MEGA's per-IP bandwidth caps, and per-cycle remote quarantine (see below) defeats MEGA's per-account auth-rate caps. Cooldown between attempts against any one account is provided organically by the cycle interval rather than by a `sleep` call
* **The config-pull remote must be SSH, not HTTPS.** The daemon runs headless under launchd, so git has no TTY to prompt on: with an HTTPS origin and no credential helper, every top-of-cycle `git pull` dies with `fatal: could not read Username for 'https://github.com': Device not configured` and config propagation silently stalls. Use an SSH remote — `git@github.com:Pirate-Hunter-Zoro/Media-Orchestrator.git` — backed by `~/.ssh/id_ed25519`, which authenticates non-interactively. If a clone ever arrives on HTTPS, fix it with `git remote set-url origin git@github.com:Pirate-Hunter-Zoro/Media-Orchestrator.git`.

* **Machine-local config propagation:** `rclone.conf` is NOT versioned (it holds credentials and the repo is public). It lives at `Media-Syncer/rclone.conf`, covered by the root `.gitignore`; copy `rclone.conf.example` there and fill it in. Each cycle's top-of-loop `git pull` is followed by the publish step that copies that file into `~/.config/rclone/` (atomically, under the shared lock), so an account the provisioner appended is visible to every rclone process on the next cycle. **That pull is serialized behind an `flock` on `.git/pull.lock`, and naming the refspec is not a substitute for it.** Two pulls against one working tree each append a mergeable line to the shared `.git/FETCH_HEAD`, so the second to read it sees two candidates for `main` and dies with `fatal: Cannot fast-forward to multiple branches` however precisely it named `origin main` — measured at 5 of 6 concurrent raw pulls failing against a scratch clone, and 0 of 6 through the lock. There is only one pull site here, so the overlap takes two *processes*, which is exactly what a restart produces: launchd SIGTERMs the outgoing daemon and starts the incoming one while it is still finishing its iteration. On contention the pull is **skipped, not queued** — the process holding the lock is pulling the same tree, so the update lands regardless, and a cycle must never block on config propagation.

* **The provisioner no longer commits.** Serializing pull-against-pull was once only half the job, because `mega_accounts.create_accounts()` appended a remote to the tracked `rclone.conf` and then `git add`/`commit`/`push`ed it, so a pull landing inside that sequence aborted (measured: **4 aborted pulls in 12 minutes** while provisioning 31 accounts). Since 2026-09-19 the conf is untracked and the provisioner only appends and publishes it atomically; there is no local commit left for a pull to collide with, and the account list never enters the public history. The pull still takes the shared `flock` on `.git/pull.lock` via `utils.git_tree_lock(block=False)` and skips on contention.

* **The provisioner publishes `rclone.conf` through `install_rclone_conf`, never `shutil.copy2`.** `_append_remote` used a plain `copy2` to push the freshly-appended conf to `~/.config/rclone/`, which reintroduced through a second code path the precise bug `install_rclone_conf` exists to prevent: `copy2` opens the destination and **truncates it before copying a byte**, so every rclone process on the machine — including the 16 parallel scan workers — has a window in which the active config is empty or half-written. A reader inside that window dies with `didn't find section in config file`, and because that message is *also* a stale-session signature it triggers `purge_mega_session`, putting a second writer on the same file. Atomic `os.replace` under the shared `_conf_lock` is the only version that holds, and there must be exactly one publish path so a fix in one place cannot be silently undone in another.
* **Chunked transfers:** 1 GB streaming segments applied to every download, sized to fit within a single exit node's MEGA bandwidth budget before throttling
* **VPN rotation:**

  * Reactive (on failure during downloads, uploads, and remote scanning)
  * Proactive (between chunks)
  * `rotate_exit_node()` queries live Tailscale state each time to avoid stale IP tracking
  * Exit node list is deduplicated to prevent rotation traps from duplicate IPs
  * Restricted to nodes **measured fast** — see *The exit node is the throughput ceiling* below
  * Returns `True` only if the node actually changed, so a caller counting failures toward a rotation threshold does not discard its evidence on a throttled call

* **The exit node is the throughput ceiling, and a slow one takes throughput to zero.** The selected Mullvad node — not MEGA, not the ethernet — caps the fleet's aggregate upload, and the spread across Mullvad's fleet is more than two orders of magnitude: **26.72 MB/s measured on `us-den-wg-101` against 0.060 MB/s on `za-jnb-wg-001`.** The failure this produces is worth stating precisely, because "slower" is not what happens. Every transfer carries a wall-clock budget from `config.TIMEOUT()`, so once per-stream throughput falls below what that budget implies, **nothing completes at all** — and nothing completing means nothing is recorded as uploaded, so the identical file set is retried next cycle at the identical doomed rate. Measured on `za-jnb-wg-001`: **0.86 MB/s aggregate across all 16 workers, 0 uploads in 1 h 43 m, 56 timeouts an hour, 69 files abandoned** — against 150–280 files an hour on a healthy node in the same phase.

  Two mechanisms close this, and they are complementary — the allowlist keeps the daemon off slow nodes, the trigger gets it off one that goes bad while selected:

  * **A measured allowlist.** `scripts/benchmark_exit_nodes.py` probes every non-blocked node and writes `fast_exit_nodes.json`; `get_exit_nodes()` intersects that with what Tailscale currently offers, so rotation only ever lands on a node clocked at or above `config.FAST_EXIT_MIN_UPLOAD_BPS` (8 MB/s aggregate — double the 0.25 MB/s `TIMEOUT_ASSUMED_RATE_BPS` per worker, so an admitted node never puts its transfers on the edge of their own timeout). The list is **gitignored, not committed**, because the fast set is a property of where this machine sits on the network and does not survive being copied to another host; it expires after `FAST_EXIT_MAX_AGE_SEC` (30 days) because Mullvad retires nodes. Every failure path — missing file, corrupt file, expired file, none of the fast IPs currently offered — falls back to the country-filtered list and logs. **An empty allowlist must never propagate**, since `rotate_exit_node()` would divide by zero on it and rotation would be gone entirely: a slow node is a bad day, no rotation is a dead daemon.
  * **Rotation on systemic upload timeouts.** `config.UPLOAD_TIMEOUT_ROTATE_THRESHOLD` (3) consecutive upload timeouts *with no success in between* rotate the node. A timeout is the only failure shape that indicts the node rather than the account, and it was also the only one nothing handled — `quota` zeroes the ledger entry and a stale session heals and benches the remote, but a transfer that simply never finished was re-queued against a different account, which changes nothing when the link underneath all of them is the problem. **The counter, rather than an immediate rotation, is the load-bearing part:** a switch resets every TCP connection on the machine, so rotating on a single timeout destroys the other 15 in-flight transfers to fix one file. Requiring consecutive failures means the trade is only made once the node is provably bad for everyone, and any successful upload clears the count.

* **A timeout has its own per-file budget, and it goes to the back of the queue.** Two details of the timeout path that look like bookkeeping and are the difference between losing the backlog and keeping it:

  * **`MAX_UPLOAD_TIMEOUTS_PER_FILE` (6) is separate from `MAX_DOWNLOAD_TRIES` (3), because the two bound different hazards.** `MAX_DOWNLOAD_TRIES` exists to stop a *poison* file — a bad read off a dying drive, a path MEGA refuses — from spinning a worker; those fail in seconds, so a small budget is right. A timeout is the opposite shape: it burned the file's entire transfer budget and returned no verdict about the file at all, only about the link. Charging one against the other means three slow minutes on a bad node abandon a healthy file for a cycle that runs for **days** — which is precisely what produced 69 abandoned files in a single morning. So a timeout never spends an attempt; it spends this instead.
  * **A timed-out file is re-queued with `insert(0)`, not `append`.** Workers pop from the end, so appending hands the same file straight back to the next free worker — and it then spends its whole timeout budget in consecutive tries against the very link that is failing it, exhausting the budget before any rotation has had time to help. Going to the back of the queue lets the rest of the backlog absorb the failures while rotation works. Measured against a node that recovered after 15 attempts: **2 of 6 files still abandoned with `append`, 0 with `insert(0)`.**

  **Probe upload, not download, and not MEGA.** The daemon's job is pushing bytes, so upload is the direction that matters; the probe runs against `speed.cloudflare.com`, which is deliberately not one of the split-tunnelled destinations, so it rides the node exactly as a MEGA transfer does. It measures the *tunnel* ceiling rather than a MEGA transfer on purpose: rclone's mega backend uploads over a single connection capped around 2.4 MB/s, so a single-stream MEGA probe is MEGA-bound and cannot separate a 24 MB/s node from a 42 MB/s one. It would only catch the catastrophic case — which the tunnel probe catches too, in seconds instead of minutes. Verified equivalent against production: the probe read 0.060 MB/s on `za-jnb-wg-001` while the live upload phase was managing 0.054 MB/s per worker through it.
* **Retry logic:** configurable attempt limits via `repeat_command` wrapper, which handles retry-and-rotate (and, on exhaustion, quarantine and session-purge) automatically. It judges success by an **empty stderr**, so it wraps only the MEGA operations that are silent on success — `about`, `lsjson`, and the `deletefile`/`cleanup` calls in the One Pace replace/relocate paths. (**The config-propagation `git pull` is deliberately not wrapped** — it is chatty on success, so the empty-stderr rule misreads it; see *The config pull must never go through `repeat_command`*.) The transfer commands (`copyto`) are deliberately **not** wrapped: rclone writes benign notices and stats to stderr even on a successful copy, which the empty-stderr test would misread as failure and needlessly retry/rotate/quarantine. Those use `run_command` with a content-based error check instead (failure only when stderr actually contains `error`/`failed to`, with `file exists` forgiven), and call `heal_stale_session` themselves for the one class of failure that check cannot fix by retrying — see *Stale-session recovery*. A `repeat_command` op that was quarantined or ran out of attempts returns a `None` result, which the destructive delete path treats as "did not happen" — the old copy is left intact and no cleanup is attempted
* **Stale-session recovery:** a stale cached MEGA session manifests in four distinct stderr shapes, all with one cause and one cure. A `go-mega` segfault (`panic: runtime error` / `SIGSEGV`) when MEGA returns a nil root Node; a login failure (`couldn't login`) when MEGA hands rclone an empty auth response; `Invalid arguments` (MEGA's EARGS), which reads like a caller mistake and is not — the same command worked minutes earlier, and rclone reports real flag mistakes as `unknown flag`; and `failed to create file system`, meaning the backend could not be constructed at all. `is_stale_session_error` matches any of these, case-insensitively. **`didn't find section in config file` is deliberately NOT a signature** — that is a torn *read* of `rclone.conf` mid-rewrite, and treating it as a dead session makes each purge trigger the next one; `purge_mega_session`'s atomic `os.replace` is what prevents the torn read, and matching the message here would reintroduce the same cascade from the other side.

  Every path routes the cure through **`heal_stale_session(remote)`**, which strips that remote's `session_id`/`master_key` from the active `rclone.conf` and returns whether the remote is worth retrying. Three properties make it safe to call from anywhere, including from 16 upload workers at once:

  * **Deduplicated.** Concurrent callers hitting the same dead remote produce exactly one purge; within `SESSION_HEAL_COOLDOWN_SEC` (120 s) the rest get `True` without touching the config. This matters because rclone writes a *fresh* `session_id` back on every successful auth, so a second purge races the good session the first one just minted — and a burst of simultaneous logins is itself rejected as EARGS, a login storm indistinguishable from the failure it is trying to fix.
  * **Budgeted.** `SESSION_HEAL_MAX_PER_CYCLE` (3) purges per remote, after which it returns `False`: a purge that does not fix the remote means the fault was never the session (a suspended or quota-dead account), and repeating it is how a spin starts. `clear_session_heals()` resets budgets at each cycle boundary, and `SESSION_HEAL_WINDOW_SEC` (1 h) resets them on idle — the resident daemons (`mediafs`, `predownload`) never reach a cycle boundary and would otherwise retire a remote on three unlucky heals spread over weeks.
  * **Off the happy path.** Nothing runs unless a command already failed with a matching signature, so a healthy fleet pays nothing.

  **Mass session events are MEGA-side, not a config bug.** On 2026-08-24 the free-space
  rescan fired `rclone about` at every remote at once; MEGA invalidated ~242 of the 739
  cached sessions in that burst (a rate-limit/security sweep on its side), each dead
  session surfaced as a `go-mega` nil-root SIGSEGV, and the fleet healed them one-by-one —
  282 heals in the window. The cost is *duty cycle*, not correctness: each heal is a
  purge → re-login → retry → bench cycle, which is why a ~24 MB/s fleet averaged ~1.1 MB/s
  that day while every healthy account was still uploading normally. The recovery is
  bounded and self-limiting by design (dedup + per-cycle budget + bench), so the fleet
  drains the backlog on its own once the sessions are re-minted; the fix is to *not*
  treat each heal as an isolated event — a burst of simultaneous heals is one MEGA-side
  event, not hundreds of failing accounts.

  `repeat_command` calls it after exhausting its rotation budget, then retries once (guarded by `just_tried` against unbounded recursion), quarantining the remote if the budget is spent. The transfer paths — the upload worker, both One Pace `copyto` routes, `chunked_download`, and `tier`'s stream/ranged fetches — call it directly, since they are on `run_command` and get none of `repeat_command`'s handling. **On the download and streaming paths a heal replaces the VPN rotation rather than following it:** a dead token is dead from every exit node, so rotating first spends 10–40 s of tunnel re-auth to learn nothing, and on `tier`'s ranged fetch there is a player blocked on the read.

* **Upload bench (a dead account must not be re-offered):** a stale-session failure returns in about a second, while a real upload runs for minutes — so on the upload path the *speed* of the failure is the hazard, not the failure. `_release` refunds the reservation of a failed claim, so a dead account's ledger balance never drops and the allocator immediately re-offers it as the best candidate; it then absorbs an unbounded share of worker turns while healthy accounts are still moving bytes. Left unguarded this reaches **5,949 of ~7,750 upload attempts in a day against a single dead account**, and a simulation of that asymmetry places 2 of 40 files instead of 40. So a stale-session upload failure heals the remote and then **benches** it: out of the allocator for `UPLOAD_BENCH_SEC` (180 s, long enough for the next login to land, and short enough that a healed account rejoins the same phase), or for the rest of the phase once its heal budget is spent. The file is re-queued **without** consuming an attempt from its `MAX_DOWNLOAD_TRIES` budget — it never got a real chance, and charging it for the account's fault is how a perfectly good file ends up abandoned for the cycle. A bench preserves the remote's ledger balance and restores it on expiry rather than zeroing it, because a zeroed balance is indistinguishable from a full account and would strand that capacity until the next rescan; re-benching an already-benched remote is a no-op for the same reason
* **Per-cycle remote quarantine:** MEGA enforces two distinct throttle classes — one per IP, one per account. VPN rotation handles the IP variant; nothing rotation-based touches the account variant, because MEGA's anti-abuse tracks the account regardless of which exit node the request originates from. After `repeat_command` exhausts its full retry-and-rotate budget against a remote — including any session-purge follow-up — the remote name is added to an in-memory quarantine set via `quarantine_remote()`. For the remainder of that cycle, any subsequent operation against the same remote returns immediately at the top of `repeat_command` without spawning rclone, preserving cycle time for healthy remotes and giving MEGA's per-account throttle counter the silence it needs to decay. The set is cleared at the top of every cycle by `clear_quarantine()`, so a remote that quarantined in cycle N gets a fresh attempt in cycle N+1. The quarantine is in-memory only — no state file — because cross-cycle persistence would defeat the cooldown semantic
* **Transfer budget (`config.TIMEOUT`) is a dead-transfer detector, not a throughput target.** Every `copyto`/`cat` gets a wall-clock budget sized from the file, so it must sit *below* the slowest transfer that still legitimately completes — otherwise it cuts off work that was still moving, and the whole file re-uploads from zero next cycle. It was doing exactly that: **measuring 396 completed uploads out of `media_sync.log` gave median 2.06 MB/s, p10 0.66 MB/s, min 0.07 MB/s**, against an assumed **0.5 MB/s** — a budget inside the normal slow tail rather than below it, which is where **189 logged timeouts** came from. Two separate shapes: the **floor** (`Bleach v26.cbz`, 135 MB, formula said 270 s, got the 300 s floor, died at exactly 300 s — killed by the floor, not the rate) and the **rate** (the 1 GB streaming chunk sized to 2048 s and timed out there **34 times**, the largest single cluster). Now `max(900 s, min(size / 0.25 MBps, 6 h))`: the rate sits below p10 so an ordinary slow transfer is never cut off, the floor covers a small-but-slow file, and the ceiling exists because the library holds **12 GB** movies — a pure rate budget would hand one a 17-hour timeout, and with `--low-level-retries 20` a stalling transfer can burn that without moving a byte. **Files above ~5.3 GB get a slightly *shorter* budget than before** (12 GB: 6 h vs 6.7 h); that is the ceiling doing its job, and 6 h still implies only 0.57 MB/s, under measured p10
* **Stateful diffing:** avoids redundant transfers

---

## Shell Scripts

| Script              | Purpose                                         |
| ------------------- | ----------------------------------------------- |
Every launcher an installed agent runs is listed here — `startup.sh`'s `AGENTS` array is the source of truth for *which* agents exist, and each one is started through its `run_*.sh`.

| Script              | Purpose                                         |
| ------------------- | ----------------------------------------------- |
| `run_media_sync.sh` | Launches the media sync loop                    |
| `run_mediafs.sh`    | Launches the `mediafs` virtual filesystem — waits for the SSD library root to be populated, force-cleans a stale mount, then mounts `~/MediaLibrary` |
| `run_predownload.sh` | Launches the predictive pre-download daemon    |
| `run_tailscale_watchdog.sh` | Launches the Tailscale watchdog (relaunches Tailscale when the 100.x address disappears; rotates away from an exit node that is selected but carrying no traffic) |
| `run_backup_state.sh` | Runs the hourly `remote_inventory.json` + `sync_state.json` backup (`com.mikeyferguson.mediasyncstatebackup`, interval-driven — no resident process). Retains the newest `STATE_BACKUP_KEEP_VERSIONS` version dirs and empties the remote's rubbish bin each run, so the metadata-backup account cannot be filled by its own version history. |
| `cancel_sync.sh`    | Stops the sync process                          |
| `startup.sh`        | Full setup + deploy + service start             |
| `purge.sh`          | Stops the service and removes conda environment |

---

## Utility Scripts

Hand-runnable helpers that are not the sync daemon itself.

| Script                | Purpose                                                                                                                                                                                                                                                                                    |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `scripts/check_space.py` | Writes the phone-glanceable fleet free-space report to `config.FREE_SPACE_REPORT_PATH` and prints where it went. Reads the `free_space.json` the cycle already refreshed — **no rclone calls**, so it is safe and instant. Run it by hand to refresh the report without waiting for a cycle to end (see the freshness caveat under *Fleet free-space report*). |
| `scripts/tier.py`     | Cache/eviction CLI: `--status`, `--hydrate <relpath>`, `--cache-gc`, `--evict-plan [--floor-gb N] [--execute]`. Dry-run by default. See *`tier.py` — hydration, streaming, eviction*.                                                                                                       |
| `scripts/rebalance_overfull.py` | Moves files off any remote whose live usage exceeds `REMOTE_CAP_BYTES`, until it is back under cap minus the fill margin. Dry-run by default; `--execute` to move, `--remote <name>` for one account. Takes the uploader lock, so the daemon skips its upload phase for a cycle rather than competing — it does not need to be stopped. Each move is gapless (verify the destination, then delete the source and empty its rubbish bin). See *One snapshot, many upload phases*. |
| `scripts/benchmark_exit_nodes.py` | Measures every exit node's upload/download throughput and publishes the fast allowlist rotation is restricted to. **Requires the daemon stopped** — it refuses to run otherwise, since 16 concurrent uploads would poison every reading and the node switching would kill them. ~25 min for the full fleet; `--countries us,ca` to narrow, `--min-upload N` for a stricter threshold, `--report` to re-derive the allowlist from the last sweep without probing. Restores the original node on exit, including on Ctrl-C. Re-run it whenever throughput looks wrong, and after the allowlist expires. |

> **There is no bulk episode-renumbering helper in this repo.** If you need to renumber episode files, it is a manual job — and see *Renaming or Restructuring a Title*, because a local rename alone duplicates and resurrects files.
>
> Treat any script named in this table as a claim to verify against the working tree rather than a fact. Entries here have outlived the files they describe.

---

## Cancel / Purge

### Stop sync activity

```bash
bash cancel_sync.sh
```

### Full teardown

```bash
bash purge.sh
```

* Stops the service
* Removes the conda environment

---

## Purging a Title

Distinct from the teardown above (`purge.sh` only removes the conda environment): this is how to fully and permanently remove a *title* — every episode, volume, and loose file — from both local storage and the entire remote fleet, without it resurrecting on the next cycle. The procedure leans on the **single-residence invariant** (see *Why write-once*): since each file lives on exactly one remote, `remote_inventory.json` names every copy, so the remote purge is inventory-driven and needs **no full-fleet scan**.

> **Default (settled library): trust `remote_inventory.json` ∪ `media_sync.log` — do not scan the fleet.** Once the library is fully uploaded and the fleet is at rest (no uploads in flight), these two sources are authoritative and a full-fleet probe is unnecessary. A *completed* inventory scan visits every remote, so by the single-residence invariant `remote_inventory.json` is a complete path→remote map of everything that has been uploaded; `media_sync.log` covers only the brief just-uploaded-but-not-yet-rescanned window. **If a title appears in *neither*, it was never uploaded — it is local-only, so the entire remote purge is a no-op: `rm -rf` it locally, prune nothing (it is absent from both state files), and stop.** For a title that *is* uploaded, drive the remote purge off its inventory residence set (∪ any `media_sync.log` upload lines for very recent drops). This is the standing assumption for all future purges, because deletion requests are only made against a settled library — never mid-upload.
>
> **The full-fleet `rclone lsf` probe below is a fallback for one condition only: uploads still in flight.** A partial inventory scan undercounts wildly, and while that holds you *cannot* trust inventory ∪ log and must probe. Against a settled library — the normal case — the probe is redundant; use the inventory-driven path above.

> **Enumerate from disk, not from the inventory.** The inventory-driven premise above only holds for the *remote* footprint of content that has **already been uploaded**. `remote_inventory.json` reflects nothing else — a title dropped locally on the Mini but not yet pushed to a remote does **not** appear in it, and appears only partially if its upload is still in flight. So when deciding *what exists* — especially for a category-wide cull ("purge some of the manga") — enumerate the **local library on the Mini** (`ls ~/MediaLibrary/...`, the full mount view — or the real `ls /Users/mikeyferguson/Media/...` for non-evicted files), never the inventory, which silently undercounts freshly-dropped content. A local-only title needs only a local `rm -rf` — there is nothing to purge on any remote and nothing to prune from `sync_state.json`/`remote_inventory.json` (it is absent from both until its first successful upload). For a title that *is* on the fleet, the inventory-driven remote purge below still applies.
> **For the remote purge, discover remotes from the upload logs, not the inventory alone.** `remote_inventory.json` is a *snapshot* — it names each path's **current, single residence as of the last completed scan** — and a purge that trusts it exclusively misses remote copies in **two** ways. (1) **Staleness:** a sync cycle runs many hours, so the on-disk inventory can be *days* old (its file mtime is not proof of freshness — a hand-prune rewrites the mtime without rescanning). Anything uploaded *after* that snapshot is absent from it. (2) **Orphans from failed retries:** a single file is uploaded via `run_command` (not `repeat_command`), and a transfer that partially lands, then rotates/retries onto a *different* remote, can leave a copy on the first — write-once never deletes it, so it lingers off-inventory. The inventory collapses each path to one residence and never sees these. Net effect can be large: the inventory routinely names a fraction of the remotes a title actually occupies — a third of them is typical for long-resident content — so an inventory-only purge leaves dozens of remotes dirty, and a later scan re-downloads from them. **Therefore drive the remote purge off the union of (a) the inventory residence set and (b) every remote that path was *ever* an upload target for**, harvested from the logs: grep `Uploading '<path>' to <remote>...` in **`~/Library/Logs/MediaSync.err`** (the launchd stderr log retains the *full* history back to install; the repo's `media_sync.log` may start mid-window) plus the app log. **`MediaSync.err`, not `MediaSync.log`** — the daemon logs only to stderr, so the stdout file holds no upload lines at all; see the note under *Logging*. Purge each such remote — it is idempotent, so remotes that never actually held the folder just return `directory not found`. **Regex trap:** titles contain apostrophes (*World's*, *God's*), and the log delimits the path with single quotes, so a naive `'([^']+)'` truncates at the first apostrophe and silently drops those titles — use a greedy capture anchored on the `' to <remote>...` suffix instead.
> **The log-union is blind to uploads this host did not perform — probe the fleet when the inventory looks thin.** The log-discovery note above has a blind spot that can silently defeat it for older content: **a host's launchd logs record only that host's own uploads.** `MediaSync.err` names every remote this host ever pushed a path to, and nothing else. Older content that reached the pool by any other route leaves *zero* upload lines here, so "inventory ∪ log" can miss such a title's remote footprint entirely — and those orphans still sit on remotes today even though the Air no longer runs. Two more signals fail alongside it: (1) `remote_inventory.json` is only complete if the last scan *finished* — a partial/in-flight scan undercounts wildly, and the single-residence invariant makes it *potentially* complete, not *reliably* so; (2) `sync_state.json` is no help — it held **0** keys for every target that run, including titles unquestionably on remotes (*Death Note*, *InuYasha*, *Code Geass*). **So for an old title, or any time the inventory looks suspiciously thin, do not trust inventory ∪ log — establish ground truth by probing the whole fleet.** For each of all ~390 remotes, run a cheap **top-level, non-recursive** `rclone lsf <remote>:"Shows"`, `rclone lsf <remote>:"Comics/Manga"`, and `rclone lsf <remote>:"Movies"`, and match the returned folder/file names against your target set (run it at `xargs -P 6`; ~10–15 min for the fleet). A title's own folder appears in the *top-level* listing even when the only thing under it is an orphan buried deep, so top-level `lsf` catches orphans without the cost of a recursive walk. Drive the purge off **probe (current ground truth) ∪ Mini-log (orphan safety net)**. The probe also cleanly separates *local-only* titles (present on **zero** remotes → a local `rm -rf` is the whole job, nothing to purge or prune) from Air-uploaded ones. An `lsf` that returns `Invalid arguments` is the usual dead session — strip that remote's `session_id`/`master_key` and re-probe. **Capture per-remote stderr in the probe — a silenced error reads as a clean empty.** A probe that pipes each `lsf`'s stderr to `/dev/null` and only greps stdout for matches cannot tell "remote genuinely lacks the folder" from "remote errored on a dead session," so every dead-session remote is silently scored as clean and its targets survive. Record an `OK`/`ERR` status per remote (match `invalid arguments`/`couldn't login`/`unexpected end of json`/`SIGSEGV`/`panic:`/`failed to create file system` in stderr), then strip + re-probe only the `ERR` set. **The strip only sticks if the next login lands before you re-read — rclone writes a *fresh* `session_id` back into the active conf on every successful auth.** So a strip-then-immediately-re-probe can race: the strip clears the token, but a concurrent/next `lsf` re-authenticates and the read still rides a bad path, or you re-strip a token rclone just regenerated. If a remote keeps throwing `Invalid arguments` *after* a plain strip, force a clean re-auth with a single lightweight serial call — `rclone about <remote>:` — which logs in fresh and mints a good session, *then* probe. **Two shell traps this run:** do **not** wrap the `rclone` call in a loop that has set `IFS=$'\t'` (for reading tab-delimited targets) while relying on an unquoted `$FLAGS` expansion — tab-`IFS` disables space word-splitting, so all the flags fuse into one bogus argument; and `rclone deletefile` accepts only `--timeout` (plus `-v`/`-n`/`-i`), **not** `--low-level-retries`/`--retries` like `purge` does — passing them aborts the delete.
> **Verify every target is actually gone — never trust the purge's exit code.** `rclone purge`/`deletefile` can exit `0` while deleting **nothing** when the remote's cached MEGA session is dead: the delete silently no-ops on the dead session (the same rot described under *rclone Configuration* — a committed/stale session token is dead-on-arrival and rclone can report success against it anyway). So after purging, run an `rclone lsjson <remote>:<path>` sweep over **every** (remote, target) pair and treat only `directory not found` / `doesn't exist` / an empty `[]` as truly gone; a `Failed to create file system … login` error counts as *unverified*, not clean. For each survivor, strip its `session_id`/`master_key` from the active conf, re-run the delete, then re-`lsjson` to confirm it is really absent. This sweep **must precede the daemon restart**: a daemon brought back up before the survivors are refixed can re-download one — its scan phase takes hours, so the window is wide, but the risk is real. Correct order is stop → purge → **verify + refix** → prune → restart.
> **Budget for scale on old titles.** Long-resident content scatters across the fleet far more widely than a fresh drop, because months of orphan/retry uploads accumulate (see the log-union note). A category cull of aged titles is 5–10× slower than a recent-drop purge and touches *hundreds* of accounts. Loose **films** live directly under `Movies/` as a video plus Jellyfin sidecars (`.jpg`/`.nfo`/`.trickplay/`); only the media extensions (`.mkv`/`.mp4`/`.avi`/`.srt`/`.ass`) ever reach a remote, so the sidecars are a **local-only** delete and never appear as inventory keys. Match a film by its `Movies/<Title (YEAR)>` prefix — the trailing `(YEAR)` makes each stem a safe, non-overlapping prefix (so *Iron Man (2008)* never catches *Iron Man 2 (2010)*) — and note a title may carry both a `.mkv` and an `.mp4` copy, so delete every media file, not just the first.
>
> **Check for the Jellyfin metadata backup — a second remote footprint this daemon's purge/probe is blind to.** Media-Syncer only ever touches the `Shows/`, `Comics/Manga/`, and `Movies/` prefixes and ignores non-media extensions, so a purge run entirely inside this repo leaves the purged title's **Jellyfin sidecars** (`.nfo`, `folder.jpg`/`backdrop.jpg`/`logo.png`/`-poster.jpg`, season posters) sitting in the sibling **Torrent-Ingest metadata backup**. That backup (`~/Developer/Media-Orchestrator/Torrent-Ingest/scripts/backup_metadata.py`, run nightly) mirrors `MEDIA_ROOT` to `<remote>:metadata-backup/media/` — same `Shows/`/`Movies/`/`Comics/` layout but under a `metadata-backup/` top-level prefix the pool never sees — plus `state/` to `metadata-backup/state`. The remote is `config.METADATA_BACKUP_REMOTE` (default **`vm_mega1`**, itself a pool account in `rclone.conf`; override env `TORRENT_INGEST_BACKUP_REMOTE`). **So a full purge has a step-4½: also clear the backup.** For each purged **show**, `rclone purge <remote>:metadata-backup/media/Shows/<title>`; for each purged **film**, `rclone deletefile` every sidecar under `metadata-backup/media/Movies/` whose name starts with the film stem (`lsf … --files-only | grep -Ff <stems>` to list them — ~5 sidecars per film: `.nfo` + `-poster`/`-backdrop`/`-landscape`/`-logo`); **manga has no sidecars** (`.cbz` carries none) so its backup tree is empty — skip it. Then `rclone cleanup <remote>:`. **Locally** the inline sidecars ride along with the media: the show/season `rm -rf` takes the whole folder, and the loose-film `Movies/<stem>*` glob takes the film's `.nfo`/artwork/`.trickplay/`, so no separate local step is needed — but *verify* it, don't assume. (If you skip the backup, the removed sidecars aren't lost — the next nightly `sync --backup-dir` versions them aside into `metadata-backup/_versions/<ts>/` rather than deleting them — but they linger there and clutter restores; purge them now.)
>
> **Delete *every* backup footprint of a purged title — the `state/` nfo-backup snapshots too, not just `media/`.** Standing rule: whenever a title is purged, its entire footprint under `metadata-backup/` on `config.METADATA_BACKUP_REMOTE` must go — **all three** of (a) the live mirror `metadata-backup/media/{Shows,Movies}/<title>…`, (b) any `metadata-backup/_versions/<ts>/…` copies the nightly `sync --backup-dir` versioned aside, and (c) the dated **`metadata-backup/state/nfo-backup-<ts>/Shows/<title>/…`** snapshots. The `state/` tree is a *second, independent* backup the `media/` purge never touches: it holds one dated `nfo-backup-<ts>/` snapshot per nightly run (a point-in-time copy of the library's `.nfo` files, **Shows only** — loose films carry no `state/` nfos), so a title appears in *every* snapshot taken while it still lived in the library, across many `nfo-backup-*` dirs. To clear it: `rclone lsf <remote>:metadata-backup/state -R --files-only | grep -F '<title>'` to find which snapshots hold it, then `rclone purge <remote>:metadata-backup/state/nfo-backup-<ts>/Shows/<title>` for each, then `cleanup`, then prune the matching `metadata-backup/state/…` keys from `remote_inventory.json`. **Durability — you MUST also clear the *local* Torrent-Ingest snapshots, or they resync tonight.** The remote `state/` tree is a `sync` mirror of Torrent-Ingest's local `config.STATE_DIR` (= `~/Developer/Media-Orchestrator/Torrent-Ingest/state`), so deleting only the remote copy is undone by the next nightly `backup_metadata.py`, which re-uploads whatever is still under the local `state/nfo-backup-*` dirs. The local tree mirrors the remote 1:1 (same `nfo-backup-<ts>/Shows/<title>` layout, same snapshots). So after clearing the remote, also delete locally: `find ~/Developer/Media-Orchestrator/Torrent-Ingest/state/nfo-backup-* -type f | grep -F '<title>'` to find which snapshots hold it, then `rm -rf ~/Developer/Media-Orchestrator/Torrent-Ingest/state/nfo-backup-<ts>/Shows/<title>` for each. (Snapshots are per-ingest-event, not nightly-full, so a title appears only in the one or two snapshots taken around when it was ingested.)

1. **Stop the daemon first.** It snapshots the remote index once at cycle start and holds it for the whole (often multi-hour) cycle, so deletions made mid-cycle are invisible until the next one. There is no `KeepAlive`, so unload the launchd agent rather than merely killing the process. Nothing syncs while it is down — which is exactly what you want mid-purge.

2. **Pin the exact targets.** Resolve the precise title folders and any loose files (a movie often lives directly under `Movies/` rather than in its own folder). Watch for lookalikes that must be spared — e.g. *The Dragon Prince*, *DreamWorks Dragons*, and *How to Train Your Dragon* are not *Dragon Ball*; match on the full title, not a substring.

3. **Delete local copies.** Remove the title's folders and loose files from the Mini's library root (the SSD library root, `~/Media`). With the virtual library, this is usually just the title's metadata sidecars plus any transient in-flight media; the authoritative copy is on the MEGA pool and is cleared by the remote purge steps.

4. **Purge the remotes, in parallel.** Build each target path's remote set as the **union of its inventory residence and every remote it was ever uploaded to** (see the log-discovery note above) — not the inventory alone. **If the title is old or the inventory looks thin, do not stop at inventory ∪ log — probe the whole fleet with top-level `rclone lsf` first (see the fleet-probe note above) and drive the purge off probe ∪ Mini-log.** For each remote in that union: `rclone purge` each target directory, `rclone deletefile` each loose-file target, `rclone rmdir` the parent directories the inventory shows are now empty, then `rclone cleanup` the remote to empty its MEGA rubbish bin and actually reclaim the space. These are per-account metadata calls, not data transfers, so running many remotes concurrently overlaps login latency without tripping MEGA's per-IP bandwidth cap. Treat a `directory not found` result as success so the pass is **idempotent** and safely re-runnable. **Then clear the Jellyfin metadata backup** on `config.METADATA_BACKUP_REMOTE` (default `vm_mega1`) — **all** of its footprint per the metadata-backup callout above: `rclone purge metadata-backup/media/Shows/<title>` per show, `deletefile` the per-film sidecars under `metadata-backup/media/Movies/`, **and** `rclone purge metadata-backup/state/nfo-backup-<ts>/Shows/<title>` for every dated `state/` snapshot that holds the title (find them with `lsf metadata-backup/state -R --files-only | grep -F '<title>'`), then `cleanup`. The `media/` purge alone is *not* enough — the `state/` nfo-backup snapshots are a separate tree it never touches. **And clear the *local* source too:** `rm -rf ~/Developer/Media-Orchestrator/Torrent-Ingest/state/nfo-backup-<ts>/Shows/<title>` for each snapshot that holds it — otherwise the next nightly `backup_metadata.py` resyncs the deleted remote `state/` copies straight back (see the durability note in the callout). The media purge/probe machinery never sees any of that.

5. **Clear stale-session stragglers.** A remote whose cached session has died surfaces as a `go-mega` SIGSEGV panic or an `Invalid arguments` / `couldn't login` failure. Discard its session with `purge_mega_session()` (strips `session_id`/`master_key` from the active conf) and retry on the fresh login. Note this catches only the *hard-failing* stragglers — the **silent exit-`0` no-ops** are invisible here and are caught only by the verification sweep in the next step.

6. **Verify every target is actually gone, then refix survivors.** Sweep `rclone lsjson <remote>:<path>` over every (remote, target) pair in step 4's union and confirm each returns `directory not found` / `doesn't exist` / `[]`. Any still present — or erroring on login — was silently no-op'd on a dead session (see the verification callout above): strip that remote's `session_id`/`master_key`, re-run the delete, and re-`lsjson` until it confirms gone. Do this **before** restarting the daemon, or a survivor can be re-downloaded.

7. **Prune state and sweep stubs.** Remove the target keys from `sync_state.json` and `remote_inventory.json` (the latter is rebuilt next cycle, but pruning keeps it consistent in the meantime), and delete any 0-byte media stubs a prior failed download may have left, so the upload phase does not propagate them. Clean up any `rclone.conf.*` backups the session-strip retries left in `~/.config/rclone/` — they hold stale tokens and only add clutter.

8. **Restart the daemon.** Its next cycle rebuilds the remote index from a fresh scan; with the title gone from every remote and from local storage, nothing is re-downloaded or re-uploaded.

> **Running the `cleanup` pass — keep it dumb and sequential.** Emptying the rubbish bin is a cheap per-account call (`rclone cleanup <remote>:`, ~1–2 s each), so a plain sequential shell loop over the touched remotes clears ~70 accounts in a couple of minutes and *always works*. Do **not** reach for a fancy `tr … | xargs -P … &`-style piped-parallel one-liner for it: in this harness those long pipelines get auto-backgrounded and hang with the parent shell blocked and **zero** child processes spawned — it looks like MEGA is throttling when nothing has actually started. If you want parallelism, put the body in a standalone `.sh` and call it with `xargs -P 4` on the **remote names only** (they are `[a-z0-9]+`, no apostrophes) — never pipe title *paths* through `xargs`, which chokes on the apostrophes in *World's*/*God's*. A lone straggler is almost always the `go-mega` SIGSEGV stale-session panic (step 5): strip its `session_id`/`master_key` from the active `~/.config/rclone/rclone.conf` and the retry logs in fresh and succeeds.

**Two traps worth calling out.** Do **not** use `rclone rmdirs … --leave-root` to clean empty parents — it walks the *entire* remote (effectively the recursive scan you are trying to avoid) and is needlessly slow on a fleet of hundreds of accounts; instead compute which parents went empty from the inventory and `rclone rmdir` just those paths. And do **not** route these deletes through `repeat_command` — it judges failure by non-empty stderr, so a benign `directory not found` trips its rotate-and-quarantine path; use a plain command runner with an "already gone" allowlist, reserving `repeat_command` (and its session-purge follow-up) for the genuine stale-session stragglers in step 5.

---

## Tooling that implements these procedures

The three procedures below (purge, rename/restructure) are correct and hard-won, and every
one of them had been executed by hand each time it was needed — which is how a step gets
skipped under time pressure. Three scripts in **Torrent-Ingest** now implement them, each
carrying the traps from this document in its own docstring so the reasoning travels with
the code:

| script | what it does | procedure it follows |
|---|---|---|
| `scripts/migrate_comic_franchises.py` (`scripts/migrate_comics.sh`) | moves comics into the `COMIC_FRANCHISES` layout | *Renaming or Restructuring a Title* |
| `scripts/refile_season.py` | moves a show's episodes out of a wrong season folder | *Renaming or Restructuring a Title* |
| `scripts/dedupe_video_formats.py` | deletes a same-episode duplicate that differs only by container | *Purging a Title* |

All three share the safeguards this document insists on, and none of them will proceed
without them:

* **log-union remote discovery** — inventory residence ∪ every `Uploading '<path>' to
  <remote>` line in `~/Library/Logs/MediaSync.err`, with the greedy capture anchored on the
  `' to <remote>` suffix so titles containing apostrophes (*World's*, *God's*) are not
  truncated and silently dropped;
* **file-level `rclone moveto`**, never directory-level `move` — which returns
  `Invalid arguments` rather than a clean "not found" on a remote that lacks the source, and
  refuses to move a same-named child into its own parent (exactly the shape of the
  main-series move);
* **moves grouped by destination directory, groups run serially** — MEGA permits two
  sibling directories with the same name and `rclone lsf` resolves the name to only one of
  them, so parallel `moveto` calls into a not-yet-existing destination create duplicates and
  whatever lands in the shadowed node reads as missing;
* **dead-session healing** — a bare `Invalid arguments` strips that remote's
  `session_id`/`master_key` and forces a clean re-auth with `rclone about` before one retry;
* **verification before state rewrite** — exit code 0 is not proof, so every path is
  re-listed, and the state keys are left ALONE while any residual remains. Every pass is
  idempotent, so the fix for a partial run is to run it again.

They also refuse rather than guess. `refile_season.py` will not move anything unless the
plan's own SOURCE FILENAMES state the season being moved to; `dedupe_video_formats.py` will
not delete a copy unless format precedence and file size agree AND the two `.nfo` sidecars
carry the same episode title. Both refusals print the reason.

Neither `rclone dedupe` nor `rclone rmdirs --leave-root` appears anywhere in them, for the
reasons this document gives.

## Renaming or Restructuring a Title

Distinct from a purge: here you **keep** the files but change their *path* — renaming a title, or flattening a nested layout (e.g. `Comics/Manga/<Parent>/<Child>/…` folders-within-folders into sibling base folders `Comics/Manga/<Child>/…`). The move must land on **both** local storage and every remote copy, and it follows the same stop → mutate → verify → prune → restart discipline as the purge above.

> **A local-only move duplicates and resurrects — you must move the remotes too.** Move a file locally and nothing else, and two write-once rules turn against you. (1) The new local path is "not on any remote," so the Mini **re-uploads** it as a fresh file while write-once keeps the old remote copy — now two copies. (2) The old path is suddenly **missing locally**, so the download phase **re-downloads** it, resurrecting the file at the old path. So every local rename must be mirrored by a remote move of the same file.
> **Drive the remote move off the log-union, not the inventory** — exactly as in the purge (see the log-discovery note there). A file orphaned at the old path on an un-inventoried remote (from a past failed retry) is invisible to `remote_inventory.json`; miss it and the next scan re-downloads it at the old path. Build each path's remote set as inventory-residence ∪ every `Uploading '<path>' to <remote>...` target in the launchd logs.
> **Move at the file level with `rclone moveto`, never the directory level with `rclone move`.** Directory `move` has two failure modes here. (1) On a remote that doesn't hold the source dir, mega returns `Server side directory move failed: Invalid arguments` (EARGS) rather than a clean `directory not found` — trivially misread as a real error, and it aborts the batch. (2) It **refuses** to move a same-named child into its own parent (`<Parent>/<Parent>` → `<Parent>`) because the destination is an ancestor of the source (overlapping paths). File-level `rclone moveto R:"…/<Parent>/<child>/<file>" R:"…/<child>/<file>"` sidesteps both: it handles the same-named overlap, it is a server-side metadata rename on the same remote (fast, no data transfer), and — because you only move files you actually discovered via `rclone lsf -R --files-only` — there is never an absent-source error. Files already at the target depth are simply skipped, so the pass is **idempotent** and safely re-runnable.
> **A bare `Invalid arguments` on `moveto`/`lsf` is usually a dead session.** Same rot as everywhere else: strip that remote's `session_id`/`master_key` from the active `~/.config/rclone/rclone.conf` and retry once. Stripping *all* tokens up front to force fresh logins is fine, but at high concurrency it triggers a login storm that itself surfaces as EARGS — keep the worker pool to ~5–6. A run killed mid-flight leaves partial state (a parent renamed to a temp dir, children half-moved, empty dirs); the discover-then-`moveto` pass converges any such state on re-run. Note temp-dir names generated with Python's `hash()` are **not** reproducible across processes (`PYTHONHASHSEED`) — discover leftover temp dirs by listing, never by recomputing the name.
> **Verify like a purge — never trust exit codes.** After moving, `rclone lsf -R`/`lsjson`-sweep every union remote and assert **zero** files remain at any old-path depth (`Comics/Manga/<Parent>/<child>/…`) or under any temp dir. Then tally the distinct filenames per destination across all remotes and diff against the local file set to prove nothing was lost in a partial run. Do this **before** restarting the daemon.

1. **Stop the daemon first** (unload the launchd agent — same reasoning as the purge: it snapshots the remote index at cycle start).
2. **Move local copies** on the Mini's library root. For a rename that collides with itself (a same-named child), rename the parent to a temp name first, move each child up to the base, then remove the emptied temp.
3. **Move the remotes, log-union-driven.** For each file under each source path, on the union of remotes, `rclone moveto` it from the old path to the new path (file-level, idempotent, `directory not found`/absent = skip). Heal-and-retry the genuine stale-session stragglers.
4. **Verify** the sweep above — no residual old-path or temp-dir files anywhere, and every local file present at its new remote path — then refix any survivors on a fresh session.
5. **Clean up empty dirs.** Remove the now-empty temp dirs and old container/child folders from the remotes (`rclone rmdir` the specific paths — do not `rmdirs --leave-root` the whole remote, per the purge trap). Empty remote dirs are cosmetic (they produce no inventory keys) but leaving them is untidy.
6. **Rewrite the state keys.** In **both** `sync_state.json` and `remote_inventory.json`, rewrite each moved key from its old path to its new path (for a flatten, drop the parent segment). The inventory is rebuilt next scan regardless, but rewriting keeps the baselines inert so nothing re-uploads or re-downloads in the meantime.
7. **Restart the daemon.** Its next scan rebuilds the index from the new paths; with local and remote agreeing, nothing churns.

---

## Logging

Logging is intentionally sparse — only events that reflect real work or real failure are recorded. Cycle banners, scan announcements, and per-cycle remote counts are deliberately suppressed to keep the log signal-dense.

**What gets logged:**

* Errors, warnings, and unhandled exceptions (always)
* File transfers — pre-transfer announcements and success lines for downloads, uploads, and overwrites
* Chunk-level transfer events (per-chunk failure, success, and retry exhaustion)
* In-place overwrite-uploads of a newer One Pace version onto its existing remote
* Relocations of a newer One Pace version that no longer fits its current remote — the upload to the new remote, the delete from the old, and a warning if no remote had room
* VPN exit-node rotations
* MEGA session purges triggered by stale-session signatures — `go-mega` panics or `couldn't login: unexpected end of JSON input` (entry and exit lines)
* Per-cycle remote quarantines (one entry per remote at the moment it is benched for the rest of the cycle)

**Log files:**

* **Application log:** `media_sync.log`
* **System log (launchd):** `~/Library/Logs/MediaSync.err`

> **`MediaSync.err` is the whole launchd log — there is no useful `MediaSync.log`.** The plist declares both (`StandardOutPath` → `MediaSync.log`, `StandardErrorPath` → `MediaSync.err`), but the daemon logs exclusively through Python's `logging`, which writes to **stderr**. Nothing is ever written to stdout, so `MediaSync.log` only ever collects stray subprocess output that escaped a capture. The one that existed Anything that ever appears there is stray subprocess output; launchd recreates the file empty on the next load, and it stays empty. **Timestamps are local time**, matching the sibling Torrent-Ingest daemons (see that repo's README on `config.log_stamp()`).
>
> This matters beyond tidiness: the purge runbook tells you to harvest a title's upload history from these logs, and for months it named `MediaSync.log` alongside `MediaSync.err`. An operator splitting the search across both would have found **zero** upload lines in half of it — under-discovering the remotes a title lives on, which is exactly the failure that step exists to prevent. Grep `MediaSync.err`.

### Rotation: the repo's logs are bounded, `MediaSync.err` deliberately is not

The repo's own logs (`media_sync.log`, `predownload.log`, `tailscale_watchdog.log`) are
**working** logs — you read them to see what a daemon is doing now — so they use
`RotatingFileHandler` at `config.LOG_MAX_BYTES` × `config.LOG_BACKUP_COUNT` (5 MB × 3, so
20 MB each). `media_sync.log` sat at 2.3 MB, so a normal window still fits in the live file
and rotation is a ceiling rather than a routine event.

`RotatingFileHandler` specifically, **not** an external rotator: the logging handler holds
the file open, so renaming it from outside would leave the daemon writing forever to an
orphaned inode — a log that looks rotated and silently receives nothing.

**`~/Library/Logs/MediaSync.err` is exempt, and that is not an oversight.** It is the
daemon's *archive*, not a working log: it retains upload history back to install, while
`media_sync.log` may start mid-window. The remote-purge runbook above harvests a title's
full `Uploading '<path>' to <remote>...` history out of it, and that history is what catches
remote copies the inventory alone misses — an inventory-only purge once left **49 remotes
dirty**. Rotating it would delete the evidence that procedure depends on.

That exemption is why the `StreamHandler` stays in `setup_logging()` even though it looks
like pure duplication of the file handler: **it is what writes the archive.** Removing it to
save disk would look like a tidy-up and would quietly destroy the purge runbook's only
complete data source.

**The general rule, which Torrent-Ingest's `config.rotate_log_if_large` repeats: a file
whose value is being COMPLETE cannot be rotated by dropping the oldest part of it.** If
`MediaSync.err` ever must be capped, *archive* it — gzip the old slice and keep it.

---

## Key Design Principles

* **Stateless deployment, stateful execution**
* **Single host (the Mini)** — the Mini uploads new files, downloads missing ones, and retains everything as the library. There is no second machine, no role split, and no cross-machine locking; the correctness property is write-once (below), not a single-writer lock
* **Write-once, with One Pace as the lone churn class** — once an ordinary file lands on the cloud, no host ever replaces or deletes it on subsequent cycles. A fresher mtime on an ordinary file is silently absorbed into the baseline by both the local-newer and remote-newer branches — no re-upload, no overwrite. Because nothing is ever deleted, the delete-then-reupload race is impossible no matter how many hosts upload. One Pace is the only content class that actively churns, because newer re-cuts ship and must replace older versions. The churn class is one path test against `config.ONE_PACE_PREFIX`; widening or replacing it is a one-line change. Because each ordinary file is placed on a single remote and never replicated, **every file resides on exactly one remote** (the single-residence invariant), so `remote_inventory.json` is a complete path→remote map — see *Why write-once* above
* **Gapless, fitting-aware replacement for newer versions** — a newer One Pace copy replaces the existing one in place when it still fits on its current remote (`copyto` over the same remote path followed by emptying the rubbish bin, or a chunked overwrite-download), and is otherwise **relocated** — uploaded to a remote with room first, with the old copy deleted only afterward. Neither route ever deletes before the replacement exists, so there is never a missing-file window in which a stale version could win a race
* **Resilience against MEGA throttling via IP rotation**
* **No idle waits:** the loop never sleeps; throttle avoidance is mechanism-driven, not timer-driven — VPN rotation handles MEGA's per-IP bandwidth caps, per-cycle remote quarantine handles MEGA's per-account auth-rate caps
* **Machine-local control plane:** the pool conf is untracked (credentials in a public repo); the provisioner appends to it and each cycle publishes it atomically to `~/.config/rclone/rclone.conf`, so new remotes become usable on the very next iteration
* **Self-healing discovery:** an empty remote list does not kill the daemon — the loop `continue`s and the next `git pull` has another chance to restore the configuration
* **Live VPN state:** rotation queries Tailscale directly rather than relying on cached state
* **Scan-phase resilience:** remote inventory scans retry with IP rotation on MEGA login failures, ensuring complete remote coverage
* **Session-rot recovery:** stale MEGA session IDs that surface as either `go-mega` panics or `couldn't login: unexpected end of JSON input` failures are detected at the wrapper layer and purged from the active `rclone.conf`, forcing a fresh login on the next attempt — the daemon self-heals without operator intervention
* **Per-cycle remote quarantine:** a remote that exhausts its full retry-and-purge budget within a cycle is benched in memory for the remainder of that cycle and gets a fresh attempt at the top of the next one. This rate-limits the daemon's own access pattern against any one MEGA account, preventing the wrapper from pinning MEGA's per-account anti-abuse throttle through rapid repeat attempts. In-memory only — no state file — and cleared at the top of every loop iteration via `clear_quarantine()`

---

## Virtual library: `mediafs` + the tier engine (`tier.py`) — LIVE

The library no longer needs to fit on the local SSD. The MEGA pool **already** holds a complete copy of everything (the single-residence invariant makes `remote_inventory.json` a full path→`[remote, mtime, size]` map), so the SSD library root is treated as a **cache**: recently-used files stay local, cold files are evicted (their bytes are provably on a remote), and anything you open is streamed back on demand. Jellyfin and YacReader see the whole library at full size and never know the difference.

### The local-mount design (why it's robust)

`mediafs` mounts at **`/Users/mikeyferguson/MediaLibrary`** — a local path on the Mac SSD — with the real media dir at **`/Users/mikeyferguson/Media`** (`config.SSD_LIBRARY_ROOT`, the SSD library root) as its `lower`. The library *presentation* therefore lives on the Mac: cold files that have been evicted from the SSD just stream back from MEGA. Jellyfin and YacReader read the local mount.

> **The path-change trap, and how it was crossed safely.** Jellyfin derives each item's id from `hash(type.FullName + path)` (UTF-16LE → MD5 → GUID), and `UserData` (watch-state) is a cascade FK to that id. So naïvely repointing the apps at a new path re-hashes every id → the library doubles and 8k+ watch-states orphan. That is exactly what a first attempt hit. The migration onto `~/MediaLibrary` was therefore done **offline, not by rescan**: every media path in `jellyfin.db` was rewritten and **every path-derived GUID recomputed with the verified hash** (proven byte-for-byte against all 14,938 stored ids first), then the old→new id map was cascaded through every id-bearing column in the DB (BaseItems parent/series/season links, AncestorIds, UserData, MediaStreamInfos, image infos, …). The `.mblink`/`options.xml` library roots and YacReader's registered root were repointed to match. Result: 31,501 items and 8,237 plays preserved intact, and because every id now equals `hash(type, newpath)`, Jellyfin's next scan matches every item and creates zero duplicates. **If you ever move the library path again, you must run the same offline path+GUID migration — never just repoint the apps.**

The daemons (this one, and Torrent-Ingest) read/write the real dir **`Media`** (on the SSD) directly; the apps read the **local mount**. New media Torrent-Ingest lands in `Media` appears in the mount via passthrough and is uploaded by this daemon as usual.

### `mediafs` (`scripts/mediafs.py`) — the virtual filesystem

A multi-threaded fuse-t filesystem presenting a **merged** view of `lower` (`Media` — real files: metadata sidecars + hot media, **read-write passthrough** so Jellyfin's `.nfo`/artwork writes land on the real drive) and the inventory (every media payload, presented **full-size** so `stat`/scans are instant and never touch a remote). Scoped to `config.MEDIAFS_PREFIXES` (`Shows/`/`Movies/`/`Comics/`). Bytes are fetched **only on an actual read**. Runs as `com.mikeyferguson.mediafs`; `run_mediafs.sh` waits for `Media` (the SSD library root populated) before mounting, force-cleans a stale mount, and `KeepAlive` remounts on any death.

#### The mount rides fuse-t, never macFUSE

`fusepy` resolves its dylib with `ctypes.util.find_library('fuse')`, which matches macFUSE's `/usr/local/lib/libfuse.dylib` whenever that antique is installed — and on macOS 27 macFUSE 5.0.6's `mount_macfuse` refuses to serve (`mount_macfuse: the file system is not available (2)`), so every `KeepAlive` respawn crash-looped and `library_supervisor` held Jellyfin down. `run_mediafs.sh` therefore pins `FUSE_LIBRARY_PATH=/usr/local/lib/libfuse-t.dylib` (fusepy's own override, read before `find_library`) and **refuses to start** if that library is missing. macFUSE 5.0.6 — a hand-installed relic with no Homebrew receipt, hence invisible to Open-Code-Doctor's nightly pass — was uninstalled on 2026-09-21; the one SIP-protected remnant (`/Library/Filesystems/macfuse.fs`) is inert and cannot be deleted without a SIP-disabled boot. Two hard rules follow: **never run macFUSE's own uninstaller on this box** (it deletes FUSE-T's `/usr/local/lib/libfuse3*` libraries, not just its own files), and **never reinstall macFUSE** — the mount must ride fuse-t, the cask Open-Code-Doctor holds current.

#### Why not WebDAV (or an rclone mount)? — asked 2026-09-21, rejected

Jellyfin has no native WebDAV support; it reads local paths only, so every WebDAV recipe ends in "mount the WebDAV storage locally and point Jellyfin at the mount" — which is what the fleet already does, with a filesystem that knows the inventory. Swapping mediafs for an rclone/WebDAV mount would trade all of mediafs's value away: `stat`/scans stop being inventory-local and go back to hitting MEGA's API per directory (outside the account pool/VPN rotation that exists to absorb that), reads lose the tier engine's 4 MB × 2-worker segment hydration, resume, on-demand ranged serving and prefetch (`config.STREAM_SEGMENT_BYTES`/`STREAM_WORKERS`/`PREFETCH_AHEAD`), and deletes through the mount stop being tombstoned and reaped. Measured on 2026-09-21: a 160 MB file read through `~/MediaLibrary` moves at **314 MB/s** (direct page-cached read: 17 GB/s) — any playback bitrate sits orders of magnitude below that, and a cold file is network-bound anyway, so the local protocol is not the streaming bottleneck and WebDAV has nothing to recover. Revisit only if the pool becomes a LAN-hosted server with high throughput and no API metering. See HANDOFF §14.

#### The inventory is reloaded on change — a mount-time snapshot loses titles

`remote_inventory.json` used to be read exactly once, at mount, and the resulting view treated as immutable for the life of the process. That silently drops files from the library, because three daemons race across one library and each is individually correct:

1. `media_sync` uploads a title and adds it to the inventory.
2. `predownload` sees it is now pool-resident, evicts the local bytes to reclaim space, and logs `safe on <remote>`.
3. `mediafs` never learned about step 1, so it will not present the pool copy.

The bytes are on the pool, the local copy is gone, and the title is in neither half of the merged view — so it **disappears from Jellyfin/YacReader until the process happens to restart**. Caught on `Kim Possible Movie - So the Drama (2005).mp4`: uploaded 20:15, evicted 20:16, absent from the mount at 20:19 while sitting safely on `automega15` the whole time. Eviction runs continuously (15 files / 19.7 GB in two consecutive cycles during one observed window), so **any** title ingested during a single `mediafs` lifetime is exposed to this.

`_maybe_reload_inventory()` re-reads the file when it changes, hooked on `getattr`/`readdir` because that is what discovery actually goes through — a Jellyfin scan, a Finder listing, an Infuse browse. Four properties, each load-bearing:

* **Tombstones, or a reload undoes your deletions.** A delete *through the mount* pops the key from the in-memory view and queues it for the reaper — but `remote_inventory.json` still lists that path until the reaper purges the pool copy and a later scan rewrites the file. Without a tombstone set, the very next reload reads the stale entry straight back and **resurrects a title the user just deleted**. Deleted keys are recorded in `self._tombstones` and subtracted on every rebuild, so the fix cannot become a worse bug than the one it replaces.
* **The tombstone set is rebuilt at startup, so a restart does not undo them either.** An in-memory-only set covers reloads but not the other half of the window: `mediafs` restarting (a crash, a `KeepAlive` remount, a deploy) between the delete and the reaper's purge. The on-disk inventory still lists that path for the whole interval — the reaper prunes `remote_inventory.json` only *after* a successful purge — so a fresh process would read it back and re-present it. Restarts here are routine, not exotic. `_pending_deletions()` seeds the set from **both** queue files: `mediafs_deletions.jsonl` (what mediafs is appending to now) and `mediafs_deletions.jsonl.processing` (a batch the reaper has claimed by atomic rename but not yet purged, or one left behind by a reaper crash, which it re-adopts). A torn final line from a crash mid-append is skipped rather than fatal. Nothing needs pruning: once the reaper finishes it deletes the `.processing` file *and* prunes the inventory, so the entry leaves disk and the tombstone stops mattering — the set is naturally bounded by what is genuinely in flight.
* **Change is detected by `(mtime_ns, size)`, not content.** Re-reading 12 MB of JSON to decide whether to re-read it defeats the purpose.
* **A bad read is survivable.** A torn or unparseable file, or one that parses to nothing, keeps the current view and retries at the next poll. The old view is always serviceable; a half-read one is not.
* **The stat is throttled** to `config.MEDIAFS_INVENTORY_POLL_SEC` (5 s), so a directory walk over thousands of entries costs one stat rather than thousands, and a quiet mount costs nothing.

#### Every shared state file is written atomically (`utils.write_json_atomic`)

The reload above is only sound if a reader can never observe a half-written file, so all of them go through one helper. A plain `open(path,'w')` **truncates before writing**, so a multi-megabyte dump leaves a real window in which a reader gets an empty or partial file — and it makes the mtime *lie*, stamping a new time while the content is still incomplete, which is precisely how a poller latches a torn read as the live view. `os.replace` swaps the name to a fully written inode in one step.

Four sites were truncating, all read live by another process:

| File | Writer | Read live by |
|---|---|---|
| `remote_inventory.json` | `media_sync._persist_inventory` (every 30 s during uploads) | `mediafs`, `predownload` |
| `remote_inventory.json` | `operations.build_remote_index` — a **second** writer | same |
| `free_space.json` | `operations` (×2) | `predownload`, `mega_accounts`, `check_space` |
| `sync_state.json` | `media_sync` at cycle end | `backup_state`, and itself on restart |

`sync_state.json` is the sharpest of these: it is the drift **baseline**, not a cache. An interrupted truncating write — launchd `SIGTERM` on a restart, a panic, a full disk — leaves it empty, and a lost baseline makes every file look drifted on the next cycle.

**The staging name is unique per call (`tempfile.mkstemp` in the destination directory), not a fixed `<name>.tmp`.** A fixed name is only safe with exactly one writer, and there are routinely several: across processes, launchd overlaps the outgoing and incoming daemon (the same overlap the config-pull lock exists for); across threads, every upload worker reaches `_persist_inventory`. Two writers sharing one staging path interleave into the same file, and whoever renames second either publishes a spliced document or fails outright because the first already renamed the inode away — **an update silently lost, with only an error log to show for it.** The temp must also live in the destination directory: same filesystem, so the rename stays atomic rather than becoming a cross-device copy.

`.gitignore` matches `*.json.tmp` and `*.json.*.tmp` — the `*.json` rule does **not** cover a `.tmp` suffix, so without them a crash between write and rename leaves a multi-megabyte state dump sitting committable.

#### `operations.py` addressed its state files by relative path

`free_space.json` and `remote_inventory.json` were opened as bare relative paths while every *reader* used the absolute `config.FREE_SPACE_PATH` / `config.REMOTE_INVENTORY_PATH`. That agreed only because `run_media_sync.sh` and `run_predownload.sh` happen to `cd "$PROJECT_DIR"` first — the launchd plists set **no** `WorkingDirectory`. Run the module from anywhere else and the daemon silently writes a second, orphaned copy of the library state next to wherever it was started. All sites now use the config constants.

### Progressive streaming — playback starts in seconds, not minutes

Opening a cold file does **not** download it whole first. `STREAM_WORKERS` (=2) parallel workers fill a sparse `.streaming` cache file **segment by segment** (`STREAM_SEGMENT_BYTES`, front-to-back for playback locality); a per-segment `done` map records what is ready. A `read()` whose covering segments are done is served from the partial file; a read on a not-yet-filled region — a seek, an MP4 `moov` atom at the end, or a **comic's trailing central directory** — gets an **on-demand ranged fetch**. Verified byte-identical against the source.

Four hard-won design points (see the streaming commits for the measurements behind them):

* **No proactive VPN rotation mid-stream.** Rotation is *reactive only* — inside `_fetch_into`/`_ranged_fetch` on an actual failure. The old code rotated the exit node after **every** chunk; each rotation is 10–40 s of dead air (drop + re-auth), which is what made cold playback time out. Measured throughput: one connection to one MEGA account ≈ 1.9 MB/s, **two** parallel ≈ 3.3 MB/s, **four** is *slower* (per-IP throttle) — so 2 workers is the sweet spot, and we ride a node instead of hopping it.
* **Bounded per-fetch timeout, floored below the SLOWEST account.** Streaming fetches use `STREAM_TIMEOUT()` (≈ `max(20 s, size/0.1 MBps)` → 40 s for a 4 MB segment), **not** the bulk `TIMEOUT()` whose 900 s floor would hang a 1 MB interactive read for minutes. A stuck node fails fast and rotates; a healthy node returns long before the ceiling. **The rate in that formula is the line between "slow" and "dead", and it must sit below the slowest account in the pool, not below the average** — accounts vary by nearly an order of magnitude (measured 2026-08-12: `showsmega41` sustained ~245 KB/s while `showsmega37` ran ~1.5 MB/s). Set too high, a slow-but-working account fails *every* fetch deterministically — a 4 MB segment it serves in 17 s killed at a 15 s ceiling, every attempt — and the file becomes unfetchable from it while the account looks unreachable rather than slow.
* **Interactive reads get priority.** While an on-demand ranged fetch (a seek / comic index) is in flight, the background fill-workers stop claiming new segments (`readers_waiting`), so the interactive read isn't fighting two bulk connections for the throttled per-IP budget.
* **A failed segment is not a failed file, and a failed fill is not permanent.** A segment fetch fails for reasons that have nothing to do with the file — an exit-node rotation landing mid-transfer, an account throttling for a minute, a reset connection — so a failure parks that segment in `bad`, waits `STREAM_FILL_BACKOFF_SEC` (a just-rotated node needs a moment before it serves), and moves on. Skipped segments are swept again (`STREAM_FILL_RETRY_PASSES`) before the end of the fill. Only `STREAM_FILL_MAX_CONSECUTIVE_FAILS` failures **back to back** — the signature of an unreachable remote rather than a bad moment on the wire — end the fill, and even then the stream is dropped from the registry so the next open rebuilds it after `STREAM_RETRY_COOLDOWN_SEC`. **A stream must never be able to fail permanently in-process:** `_Stream` is cached per path, so a `failed` flag that is never cleared is a file that cannot play again until `mediafs` restarts (see the failure mode below).
* **The daemons do NOT pause for playback.** Earlier the sync loop and the pre-downloader yielded all MEGA I/O while `STREAM_ACTIVE_FLAG` was fresh, on the theory that the ~390-remote scan's VPN churn + per-IP budget use would stutter cold playback. That was dropped as both unnecessary and counter-productive: **(a)** playback of a *physically-present* file is a `mediafs` passthrough read (`os.pread`) that never touches MEGA, so uploads/scans can't disturb it; **(b)** for a *cold* (pool-only) show the pre-downloader's entire job is to fetch the **next** episodes while you watch the first — pausing defeats that; and **(c)** the flag is stamped by *any* cold-pool read, so a Jellyfin background scan (below) would masquerade as playback and starve sync for hours. `wait_while_streaming()` is now a **no-op**, the sync cycle-pause and both predownload `streaming_active()` defers are gone, and VPN churn stays bounded by the rotation throttle (`ROTATE_MIN_INTERVAL_SEC`) + the Anthropic split-tunnel. `mediafs` still stamps the flag and still prioritizes interactive reads over its background fill workers **internally** — only the daemon-wide pause was removed.

  > **The failure mode that forced this (the 11-hour stall).** Because the flag was stamped by *any* cold-pool read — including Jellyfin's own file-analysis tasks (**Media Segment Scan**, trickplay/chapter-image extraction) and a metadata **refresh's `ffprobe`**, none of which is a human watching — an overnight *Media Segment Scan* ffprobing pool files held the flag perpetually fresh and paused **all** upload/download for ~11 h with `/Sessions` = 0 (`media_sync.log` sat at `client streaming; daemon yielding…`, zero transfers). Beyond removing the pause, those file-reading Jellyfin tasks are also **disabled** on the virtual library — same rationale as trickplay already being off — by clearing their scheduled-task triggers (`POST /ScheduledTasks/{id}/Triggers` with `[]`, persists across restart), since they only hydrate the pool. **Operational trap that bit the recovery:** do **not** `pkill -f "Jellyfin.app/Contents/MacOS/ffmpeg"` to kill stray analysis children — the **main** Jellyfin backend's command line contains `--ffmpeg /…/ffmpeg`, so that pattern kills Jellyfin itself; and its menu-bar wrapper (`Jellyfin Server`) stays alive, fooling the library-supervisor's `pgrep` liveness check so it never relaunches the backend (it only starts Jellyfin on a mount-unready→ready transition, not on backend death). Recover with a full `osascript -e 'quit app "Jellyfin"'` + `open -a Jellyfin`.
  >
  > **Related: new media not appearing in Jellyfin = a wedged library scan.** Torrent-Ingest nudges Jellyfin (`POST /Library/Refresh`) after each ingest, but if the *Scan Media Library* task is stuck in a `Cancelling` state (e.g. left there by manually stopping a running scan — a stop can even 500), no scan runs and new titles never appear even though the files and their `.nfo` are on disk. Clear it with a clean Jellyfin restart (resets task state) → `POST /Library/Refresh` → a per-item recursive refresh on the new series to resolve its episodes. The **`media_doctor`** daemon is the standing backstop: a series with more on-disk episodes than Jellyfin resolved is its `episodes_missing` auto-fix.

Concurrent streams are bounded by `TIER_MAX_CONCURRENT_HYDRATIONS` (shared VPN/bandwidth, counted per *file*, not per connection) but not serialized, so a play never waits behind a background prefetch.

> **Don't self-throttle when testing.** MEGA throttles hard per-IP/per-account; a dozen cold-file downloads in a few minutes (e.g. a test loop hammering `rclone cat`) will crater throughput for a while and make streaming latencies look far worse than a real single-viewer session. Measure sparingly and let it decay.

### Resume: the `.segmap` sidecar

A `.streaming` partial is **sparse and pre-truncated to full size**, so its length says nothing about its contents — holes and downloaded bytes are indistinguishable on disk. `<name>.segmap` (`tier.STREAM_MAP_SUFFIX`) is the record of which segments are actually present: a JSON `{size, seg, done}` written next to the partial, checkpointed every `_SEGMAP_SAVE_EVERY` segments and whenever a fill gives up. A rebuilt stream adopts it and resumes; without it every retry re-pays for bytes MEGA already delivered.

Two invariants keep it from serving holes as content, and both are load-bearing:

* **A map is only ever valid for the partial it was written for.** `_load_segmap` adopts it only when the partial exists *at the expected size*, and the recorded `size`/`seg` match. A partial recreated from scratch drops the map **before** truncating — a sparse file is full-size the instant it is created, so a surviving map would otherwise pass every adoption check and describe holes.
* **The map dies with its partial.** `predownload._clean_stale_partials` unlinks the sidecar alongside the `.streaming` file it reaps, and a completed fill drops it at promote time.

Download scratch (`.streaming`, `.hydrating`, `.segmap`) is invisible to cache accounting and eviction alike — `tier._is_scratch` gates both `cache_total_bytes` and `_cache_files_by_atime`. **An LRU trim must never unlink a partial:** that deletes the transfer rather than reclaiming a spare copy, and leaves the still-running workers' `done` map pointing at bytes that no longer exist.

### Failure mode: an episode that plays for a minute, then stalls forever

**Fingerprint.** A cold episode starts, plays for a minute or two, and dies; restarting playback or forcing a download hangs indefinitely. Other episodes of the same season are fine. `MediaFS.err` shows a `stream fill … stopped` for exactly that file, and `predownload.log` then logs `pre-downloading <that file>` every cycle with `downloaded=0` forever. Only restarting `mediafs` clears it.

**Cause — two layers.** The segments were failing because `STREAM_TIMEOUT` for a 4 MB segment (15 s) sat *below* what a slow account needs to deliver one (~17 s on a ~245 KB/s remote), so every fetch from it timed out deterministically. On top of that, a single failing segment made `_seg_worker` set `s.failed` for the whole file, and `_Stream` is cached per path with nothing ever clearing that flag. Every later reader got the same dead object: `failed` gated *both* the segment wait and the 4 MB coalescing, so each 128 KB read fell back to its own `rclone cat` — one MEGA login per 128 KB, which is slower than playback and never recovers. The distance played before the stall is just how far the fill got before the bad segment (offset 46 MB ≈ 90 s of video).

**Now.** Transient segment failures are skipped and retried; a genuinely-failed fill is dropped from the registry and rebuilt on the next open after a cooldown; coalescing runs *regardless* of `failed` (a stalled fill is precisely when the reader is the only thing still fetching, so 4 MB per login instead of 128 KB matters most); and a `done` map whose partial has vanished is cleared rather than trusted.

**The pre-downloader must say when it fails.** `ensure_cached` returning a bare `False` is indistinguishable from "nothing to do" — the daemon re-queued the same file every cycle and reported an idle-looking `downloaded=0` while a file had been failing for hours. It now logs the incomplete segment count.

### Predictive pre-download (`predownload.py`) — the "smart downloader"

On-demand streaming from MEGA (~2–3 MB/s per account) can never feel like local disk, so the winning strategy is to **have what you'll want next already downloaded**. `predownload.py` is a KeepAlive daemon (`com.mikeyferguson.predownload`) that keeps the local cache full of predicted content; reads then come from disk (instant, seek-anywhere) and on-demand streaming is only the rare cold-miss path. The only cost is a possibly-spotty *first* play of something brand new.

* **Budget scales to any host** (`config.storage_plan()`): of the space the cache system can use (current free + what the cache already holds), keep **1/10** as breathing room; of the remaining 9/10, reserve **1/6** as a download "chunk" (also the torrent download batch size) and dedicate the other **5/6 (≈3/4 of the disk)** to pre-downloads. On this Mini: ~374 GB → 37 breathing / 56 chunk / **280 GB pre-download budget**.
* **Prediction sources.** *Shows*: Jellyfin watch-state → the next `PREDOWNLOAD_EPISODES_AHEAD` (50) **unwatched** episodes of each recently-active series (a rolling window). *Comics*: Jellyfin doesn't track them, so mediafs appends every interactive open to `PREDOWNLOAD_ACCESS_LOG` — opening a volume caches the next `PREDOWNLOAD_VOLUMES_AHEAD` in its folder. *Movies*: a recently-watched movie is sent to **DeepSeek** (`scripts/ai.py` — one plain completion, no agent loop; everything the judgment needs is already in the prompt), which picks related library titles (sequels/franchise) to cache; a true one-off predicts none. Every pick is intersected with the real inventory, so a hallucinated title predicts nothing rather than queueing a phantom download.
* **Playlist-aware prediction (highest priority).** When a curated "watchable" playlist is being played, predicting the show's *natural* next episode is wrong — the cut deliberately **skips** episodes, so the natural look-ahead would pre-download exactly the ones the playlist drops. So before every other source, `build_desired` checks Jellyfin `/Sessions` for the now-playing item (falling back to the most-recently-opened item) and, if it belongs to a curated playlist, pulls the next `PREDOWNLOAD_EPISODES_AHEAD` items **in PLAYLIST order** (resolved from `/Playlists/{id}/Items`, skipping watched). These win the budget and eviction protection, so watching Toriko-Watchable pre-fetches the playlist's next entries, not Toriko's next raw episodes. When nothing playlist-bound is playing it's inert and the ordinary show/comic/movie prediction runs unchanged.
* **Rolling eviction.** Each cycle it evicts cached media that is watched / no-longer-predicted / cold — **never** the file being read right now (guarded by a recent-atime window) nor anything still in the desired set — to hold the budget, then downloads the rest in priority order. It does **not** pause for playback: while you watch the first cold episode it keeps fetching the next ones — that is the whole point. (`mediafs` still serves an interactive read ahead of its own background fill workers internally, so a cold miss isn't fighting the prefetch for the per-IP budget.)

### `tier.py` — hydration, streaming, eviction

* **`get_stream` / `read_stream`** — the progressive-playback engine above; shared per-path, reference-counted, reused by `mediafs`.
* **`hydrate(relpath)`** — whole-file fetch into the cache (`config.TIER_CACHE_DIR`, on the SSD) for prefetch/CLI; LRU-trimmed by `enforce_cache_limit()` to `TIER_CACHE_MAX_BYTES`.
* **`evict_plan(root, floor_bytes, execute=False, exclude=None, protect_sec=0)`** — evict the coldest media (by access time) **whose bytes are proven on a remote** (inventory hit with matching size) until the drive holds `floor_bytes` free. Anything not proven on a remote is **skipped** — un-uploaded files are never evicted. `exclude` is a set of relpaths to spare (`predownload` passes its desired set, so an eviction never becomes a re-download), and `protect_sec` spares anything read that recently. Restricted to `config.TIER_EVICT_PREFIXES`. Dry-run by default.
* CLI: `python3 -m scripts.tier --status | --hydrate <relpath> | --cache-gc | --evict-plan [--floor-gb N] [--execute]`.

### Automatic eviction — ON, and owned by `predownload.py` alone

**`predownload.py` is the single space manager for the SSD, and it now manages the whole disk rather than only the cache.** Each cycle it evicts cold cached files to hold the cache to its budget; then, if real free space is still under `SSD_MIN_FREE_BYTES` (80.6 GiB on this Mini — see *The floor scales to the host* below), it evicts the coldest **library-root** media down to `SSD_LIBRARY_EVICT_TARGET_BYTES` (1.25 × the floor) via `tier.evict_plan`.

**`media_sync`'s own post-cycle eviction stays disabled** — `config.TIER_AUTO_EVICT = False`, `TIER_EVICT_FLOOR_BYTES = 0` — and that is still correct. A *second* evictor racing the space manager for the same bytes is how the two-floor deadlock in *Failure mode: the pre-download cache starves the torrent downloader* got built. One owner, one floor.

Three things make library eviction safe to run unattended:

* **The inventory guard.** A file is deleted only if `remote_inventory.json` holds it at a matching size, so the local delete can never remove the last copy. Un-uploaded media is skipped, always.
* **The desired set is excluded.** Deleting what the predictor is about to fetch would turn one eviction into a re-download over MEGA at 2–3 MB/s.
* **`PREDOWNLOAD_PROTECT_SEC` (30 min) spares anything just read,** so the episode playing right now is never evicted out from under the mount.

Eviction **triggers** at the floor but **runs down to** the higher target. That hysteresis is deliberate: without it the daemon shaves a few hundred MB every 20-minute cycle and walks the disk along its own floor forever.

#### The floor scales to the host

`SSD_MIN_FREE_BYTES` is **a fraction of total disk capacity**, not a hardcoded byte count:

```python
SSD_MIN_FREE_FRACTION    = 0.175
SSD_MIN_FREE_LOWER_BYTES = 25 * 1024**3    # never below Torrent-Ingest's 20 GiB headroom
SSD_MIN_FREE_UPPER_BYTES = 200 * 1024**3   # past this, more reserved space buys nothing
```

**The reference is the whole point, and it is what separates this from the sliding-floor bug below.** `storage_plan()`'s `breathing` is a fraction of *free* space, which shrinks as the disk fills — so the floor it implies slides down with the disk and deadlocks. Total capacity is a property of the hardware and never moves, so a fraction of it is as stable as a constant while still sizing itself to the machine. Resolved once at import; there is nothing to re-measure.

The clamps handle both extremes. Below ~143 GiB of total disk the raw fraction falls under Torrent-Ingest's own `MIN_FREE_BYTES` (20 GiB) and admission deadlocks at *any* torrent size, so `LOWER` holds it at 25 GiB. On a multi-TB SSD the fraction reserves hundreds of GB that buy nothing once the largest admissible torrent already fits, so `UPPER` caps it at 200 GiB.

| total disk | floor | evict target | largest admissible torrent |
|---|---|---|---|
| 128 GiB | 25.0 GiB *(clamped)* | 31.2 GiB | 4.3 GiB |
| 256 GiB | 44.8 GiB | 56.0 GiB | 21.6 GiB |
| **460 GiB (this Mini)** | **80.6 GiB** | **100.7 GiB** | **52.7 GiB** |
| 1 TiB | 179.2 GiB | 224.0 GiB | 138.4 GiB |
| 4 TiB | 200.0 GiB *(clamped)* | 250.0 GiB | 156.5 GiB |

0.175 reproduces the measured-good 80 GiB on this Mini, so the change is behaviour-neutral here. `SSD_LIBRARY_EVICT_TARGET_BYTES` rides the floor via `SSD_LIBRARY_EVICT_TARGET_RATIO` (1.25, the measured-good 100/80) rather than carrying its own byte count: the two numbers are only meaningful *relative* to each other — the gap between them **is** the hysteresis band — so a floor that scales while the target stayed pinned at 100 GiB would silently widen or invert the band on another machine.

> **Why the library root itself must be evictable.** Media is *kept* locally after upload (`GRADUATED_UPLOAD_THEN_REMOVE = False`), so uploading frees nothing and the library root grows monotonically. If the tier cache is the only automatically evictable thing, then once it is empty there is nothing left to give: the SSD sits under its floor logging `eviction cannot reach it` indefinitely, while a 2 GiB cache faces a 20 GB shortfall. Torrent admission is squeezed and the YouTube ingest stops placing entirely.
>
> `evict_plan` can clear that in seconds, which is exactly the trap — a fix that only exists as a manual command does not run when it is needed. **A safety mechanism that requires a human is a safety mechanism that eventually does not run.**

If you are diagnosing SSD space, `predownload.py` is still the daemon that owns it. The cycle line reports both evictors: `evicted=N` is the cache, `lib_evicted=N` is the library root, and `free=` is the live figure.

### Fleet free-space report (`scripts/check_space.py`)

A compact free-space summary is written to `iCloud/Torrents/mega_free_space.txt` (`config.FREE_SPACE_REPORT_PATH`) for glancing at on a phone — total free, per-remote room, count of full accounts, and an "add more accounts" warning below `FREE_SPACE_LOW_WARN_BYTES` (40 GB). It is **silent** (no logging, no rclone calls — it reads the `free_space.json` the cycle already refreshed), so `media_sync.log` stays clean.

> **It is written once per cycle, *after* `sync_cycle()` returns — so it goes stale for as long as a cycle runs, and nothing on the page says so.** A cycle that is working through a large upload backlog runs for **days**, so the report can show numbers from several days earlier while the daemon is actively uploading, because `free_space.json` is rewritten by `get_remote_free_space` after *every* remote query (continuously, mid-cycle) while the report is not. Only the `updated` line distinguishes them, and it is easy to read as current. Run `python3 -m scripts.check_space` by hand for live numbers — it re-reads the same `free_space.json` and costs nothing.

### 20 GB-per-remote cap

`operations.usable_free()` caps each account's usable capacity at `config.REMOTE_CAP_BYTES` (20 GB), so a temporary bonus (a 25 GB account) is never mistaken for durable space — `used + free` is always ≤ 20 GB, in both upload decisions and the report. It is the **single** definition of how much room an account has: `free_space_ledger()`, `get_remote_free_space()`, `mega_accounts.pool_free_bytes()` and `check_space.py` all resolve through it. Keep it that way — a second copy of the arithmetic is how the report and the allocator come to disagree about which accounts are full, and it answers to two signals rather than one (§ *One snapshot, many upload phases*), so a reimplementation from `about` alone silently loses the guard that stops accounts going over quota. `operations.placeable_free()` is the one derived quantity layered on top — `max(0, usable_free − REMOTE_FILL_MARGIN_BYTES)` — and is what **provisioning** budgets against, since a remote at the margin can't accept a file (§ *Pool capacity provisioning*). It is a thin wrapper over `usable_free`, not a second arithmetic.

`check_space.py` reports an **over cap** count. It should always be zero; a non-zero figure is the fingerprint of an allocator that has lost track, and the repair is `scripts/rebalance_overfull.py`.

### A file too big for any account is quarantined, not re-logged

A single file whose size exceeds one account's usable cap (`REMOTE_CAP_BYTES − REMOTE_FILL_MARGIN_BYTES`) **can never be placed**: a file cannot span accounts, and auto-provisioning mints the same 20 GB, so no remote will ever hold it. The uploader does not warn-and-skip it forever (the old behaviour left the "poison" file squatting on the SSD library root, un-evictable because it is "on no remote", blocking the disk-budget admission for days). Instead it **moves the file out of the media root** to `config.UNPLACEABLE_DIR` (`~/unplaceable_media`, same volume so the move is an instant rename), preserving its library-relative path, and appends it to `unplaceable/unplaceable.jsonl` for reporting. Re-encode it smaller (or split it) and drop it back under `~/Media` to have it uploaded; otherwise delete it. This is what turned the 20.11 GiB *Love Live! Nijigasaki Final Chapter Part 2* deadlock into a one-line quarantine instead of 878 warnings a cycle.

### Boot ordering (why a restart just works)

Every daemon is a `RunAtLoad` LaunchAgent. On boot: `mediafs` (which waited for `Media` to be populated) mounts at `~/MediaLibrary` → the **library supervisor** (in Torrent-Ingest) is the **sole launcher** of Jellyfin/YacReader: it **stops them immediately** if the mount is not ready and **starts them only after the mount has been confirmed primed for `SUPERVISOR_READY_DEBOUNCE` consecutive polls** (no premature launch over a half-ready mount). The sync/ingest/reaper daemons tolerate a not-yet-ready mount and simply retry. So a reboot takes a minute to settle and then runs unattended.

> **macOS auto-reopen is disabled on the Mini** (`defaults write com.apple.loginwindow TALLogoutSavesState -bool false`) so the OS never relaunches Jellyfin/YacReader at login ahead of the mount — the supervisor is the only thing that starts them. Without this, YacReader would auto-reopen before `mediafs` mounts and pop "library folder doesn't exist." (See Torrent-Ingest's `library_supervisor.py` for the immediate-stop / confirmed-primed gating.)
>
> **Tailscale is the one thing in the boot chain that is NOT a LaunchAgent.** Its Mac app (IPNExtension) is started by the GUI/login item, so it is outside this ordering and outside launchd's supervision entirely — which is why `com.mikeyferguson.tailscalewatchdog` exists to restart it (see *Tailscale is an unsupervised single point of failure*).

### `startup.sh` installs every agent — the `AGENTS` list is the source of truth

`startup.sh` carries an explicit `AGENTS` array and installs **all** of it: `mediafs`, `mediasync`, `predownload`, `tailscalewatchdog`, `mediasyncstatebackup`, in that load order (`mediafs` first so the mount is coming up while the rest load — the others all tolerate an unready mount and retry, so it is a preference, not a hard dependency). **If you add a plist to this repo, add its label to that array**, or it becomes an agent that exists, is documented, and is never actually loaded. That is not hypothetical: `startup.sh` previously installed only `mediasync` and `predownload` while four other plists sat in the repo waiting to be loaded by hand, and on the Torrent-Ingest side the same gap left `playlistautobuild` documented as "run by launchd every 3h" while not being loaded on the box at all.

Two deliberate details:

* **`com.mikeyferguson.splittunnel` is excluded on purpose.** It rewrites the routing table, so it is a **root LaunchDaemon** in `/Library/LaunchDaemons` (system domain) — not a user LaunchAgent, and it must never be bootstrapped into the `gui` domain. Install it by hand with the `sudo` steps at the top of `scripts/split_tunnel_anthropic.sh`; `startup.sh` finishes by reporting whether it is currently active.
* **`bootout`/`bootstrap`, never `load`/`unload`, and no `launchctl start`.** The `load`/`unload` pair is deprecated and on current macOS can report success while doing nothing — precisely the failure that leaves an agent silently uninstalled. And since every plist here sets `RunAtLoad`, `bootstrap` already starts the job; adding `start`/`kickstart` would *also* fire the interval-driven ones (`mediasyncstatebackup`) off-schedule.

### Failure mode: a wedged mediafs mount

`mediafs`'s `getattr`/`readdir` **stat and `os.listdir` the real SSD library root** for every path (that is how the passthrough layer is merged). So mediafs is only ever as responsive as that disk itself — it does no network I/O on a directory read, but it does block on the disk. The `lower` is now the internal SSD, so those stats are fast; the historical outage came when the `lower` lived on a USB drive that a heavy writer saturated. The lesson generalizes: if something floods the disk the mount reads from with heavy random I/O — most concretely **a large torrent downloading directly onto it** — those directory reads stall and the FUSE mount **wedges**: reads hang in uninterruptible sleep and any process touching the mount (Jellyfin, a bare `ls`) blocks unkillably (survives `kill -9` until the mount is torn down). A USB-attached drive can even drop off the bus entirely (`Device not configured` / ENXIO) under that load.

The visible symptom is downstream and misleading: **Jellyfin serves an empty library** — 0 items to Infuse and every client — even though its database is completely intact. Jellyfin resolves each library's physical folders by reading the mount at startup; brought up over a wedged mount, it resolves them to nothing and every *user-scoped* query (`/Items?userId=…&Recursive=true`, what Infuse uses) returns 0. Meanwhile `/Items/Counts` (a raw DB aggregate) still reports the true episode/movie counts. **That split — raw counts healthy, every view empty — is the fingerprint of this failure**, and it means the DB is fine and must not be "restored." (The library-supervisor's health check is **shallow**: it only checks the mount's top level is non-empty, so it cannot tell a wedged mount from a healthy one and will happily keep Jellyfin up over a broken one. It also reads `/Items/Counts`, which stays healthy here, so its gutted-DB restore correctly does *not* fire.)

**Recovery (in order):**

1. **Remove the load.** Stop/cancel whatever is hammering the disk (the torrent). The disk recovers once idle; a **full reboot is the surest way** to clear a wedged mount and release unkillable blocked processes.
2. **Confirm the raw disk is healthy** — a readdir sweep of `/Users/mikeyferguson/Media/Shows` completes without hanging (bypass mediafs to prove it is the disk, not the code).
3. **Remount mediafs cleanly** if it is still wedged: `launchctl bootout gui/$(id -u)/com.mikeyferguson.mediafs`, force-unmount `~/MediaLibrary` (`umount -f`, then `diskutil unmount force`), then `launchctl bootstrap …` the agent.
4. **Trigger a Jellyfin library scan** (`POST /Library/Refresh`). This rebuilds the CollectionFolder → physical-folder → item linkage and repopulates the views. The DB was never damaged — the scan only reconciles the runtime structure a wedged-mount startup left unresolved.

**Prevention:** never let a heavy writer saturate the disk the mount reads from. Torrent-Ingest downloads torrents into `~/Downloads` and only the *finished* copy is landed in the SSD library root; a torrent too large for the SSD is **refused**, never spilled straight into the library (see Torrent-Ingest's README, *Torrents too large for the SSD*). Diagnosing a repeat: the tell is a hang on directory reads — historically `Device not configured` in `~/Library/Logs/MediaFS.err` / the Jellyfin log (the USB-era fingerprint), and an `ls` of a library subdirectory hanging.

### Failure mode: fusepy's own crash handler is broken, and it corrupts the shutdown

Repaired at import by `mediafs._patch_fusepy_critical_handler()`.

**fusepy 3.0.1 — the newest release, bug unfixed upstream — declares `FUSE._wrapper` as a
`@staticmethod`, so it has no `self`.** Its last-resort handler then does:

```python
except BaseException as e:
    self.__critical_exception = e     # NameError: name 'self' is not defined
    log.critical(...)
    fuse_exit()
    return -errno.EFAULT
```

Every line after the first is dead code. The consequences compound, worst last:

1. **The real exception is destroyed.** It survives only as the `NameError`'s `__context__`
   and is never recorded, so `FUSE.__init__` cannot re-raise it when the mount ends
   (`fuse.py:708`). The actual fault is never reported anywhere.
2. **`fuse_exit()` never runs**, so FUSE is never told to stop.
3. **`return -errno.EFAULT` never runs**, so the ctypes callback returns `None` where the C
   side requires an int. That is what emits `fuse: read too many bytes` and
   `fuse: writing device: Invalid argument` — and it can take the mount down by SIGSEGV.

So the handler that exists to shut the filesystem down *cleanly* is itself what corrupts the
shutdown, and it erases the evidence on the way out. Reproduced directly against the
installed library: the wrapper raises `NameError`, records nothing, and returns no value at
all. Its signature in `~/Library/Logs/MediaFS.err` is that `NameError` followed by the
read-too-many-bytes / `Invalid argument` pair.

**Read that as a sibling of the wedged mount above, not a duplicate of it.** The section
above is the mount going unresponsive because the *disk* is saturated; this is the mount
being torn down incorrectly when *any* critical exception occurs. They present similarly
from Jellyfin's side, and this one additionally guarantees you cannot see the cause.

The patch rebinds `_wrapper` as a normal method so `self` exists — the call site
(`fuse.py:688`) is `partial(self._wrapper, ...)`, which binds correctly either way — and
delegates to the original for everything that works, intercepting only the path the library
gets wrong. Recovering `__context__` is what gets the true exception back. Verified: a
`BaseException` now returns `-EFAULT`, records the original exception for re-raise, and
actually calls `fuse_exit()`; the ordinary `FuseOSError → -ENOENT` and `Exception → -EINVAL`
paths are untouched and do not trip the critical handler.

**It is patched in `mediafs.py`, not in `site-packages`, deliberately.** An edit to the
conda env is invisible to review and is silently lost the next time the environment is
rebuilt — which `startup.sh` does. The patch is idempotent and runs at import, because the
operations table is wired up in `FUSE.__init__` and a patch applied later would never be
seen.

### Failure mode: Jellyfin 500s on Next Up / library views collapse on scroll (the `ExtraIds` delimiter bug)

A second "Jellyfin looks broken but the DB is fine" fingerprint, distinct from the wedged mount above. Symptoms: **Next Up shows "an error occurred," and a library view (e.g. Infuse's Shows) lists items but collapses to an empty folder as you scroll**, while a *direct search* for a title still plays it. `GET /Items/Counts` (a raw aggregate) reports the true, healthy counts.

Cause: the offline path+GUID migration (see *The path-change trap*) rewrote the `BaseItems.ExtraIds` column joining the GUID list with **`;`**, but Jellyfin 10.11 deserializes it with `entity.ExtraIds.Split('|')` → `Guid.Parse(...)`. A single value with more than one ExtraId therefore feeds `Guid.Parse("guid;guid;...")` → `System.FormatException: Guid should contain 32 digits with 4 dashes` (visible in `~/Library/Application Support/jellyfin/log/log_*.log`). It fires whenever that item is deserialized as a parent/ancestor during a recursive or Next Up query, 500-ing the whole request — but items with a single (or no) ExtraId deserialize fine, which is why only *some* views break.

**Fix (forward-only — every db-guardian backup carries the same corruption, so do NOT "restore"):** stop the library supervisor so it can't relaunch Jellyfin mid-edit, quit Jellyfin, then in `jellyfin.db` run `UPDATE BaseItems SET ExtraIds = REPLACE(ExtraIds, ';', '|') WHERE ExtraIds LIKE '%;%'` (every `;`-split token is a valid GUID, so the swap is safe), confirm `PRAGMA integrity_check` = `ok`, restart Jellyfin, re-bootstrap the supervisor. **Standing rule for any future GUID-list migration: join with `|`, never `;`.**

---

## Upload throughput: the parallel upload phase

Uploads run `config.UPLOAD_WORKERS` (16) at a time in `_upload_phase()`, each worker pinned to a **different** MEGA account, largest file first.

The design follows from where the limits actually are. MEGA throttles per account and rclone's mega backend pushes a file over a single connection, so one transfer measures ~1.4–2.4 MB/s no matter what; concurrency across distinct accounts is the only thing that scales. It scales close to linearly until the exit node saturates (§ *The throughput ceiling*).

Four properties hold the design together:

* **A free-space ledger, not live `about` calls.** `operations.free_space_ledger()` seeds `{remote: free}` from one read of `free_space.json`; `_claim()` debits it under a lock at claim time and `_release()` refunds a failed reservation. This is what makes "one remote, one in-flight upload" safe — two workers can never be admitted against the same account's bytes.
* **No network call on the hot path.** A live `get_remote_free_space()` goes through `repeat_command`, which may **rotate the exit node** on failure, and a rotation resets every TCP connection on the machine — killing every other in-flight upload to re-check one account's quota. A `quota` error instead zeros that remote in the ledger for the rest of the phase; the periodic rescan restores a real number.
* **Never `--progress`** (`config.UPLOAD_RCLONE_FLAGS`). Under launchd stdout is a pipe rather than a terminal, so rclone's progress redraw emits megabytes of ANSI noise per transfer into the same stream `run_command` substring-matches for `"error"` to decide whether the upload failed. `--stats 0` instead, plus `--buffer-size 64M` and `--use-mmap` for the read-ahead a long-haul link needs.
* **Bounded re-queue on failure.** A failed upload goes back on the queue so a different account can take it, capped at `MAX_DOWNLOAD_TRIES` attempts per cycle. Without the cap, one poison file (a bad read off a dying drive, a path MEGA refuses) holds every worker in a permanent retry spin.

Largest-first ordering keeps a big file from straddling the next rescan; smaller files fill the remaining slots around it.

### Measured throughput

Real backlog files (250–450 MB each), sampled at the `en0` interface:

| Workers | Aggregate | Per stream |
|---|---|---|
| 1 | 2.4 MB/s | 2.4 MB/s |
| 6 | 9.3 MB/s | ~1.6 MB/s |
| 12 | 18.9 MB/s | ~1.6 MB/s |
| 16 | ~24 MB/s | ~1.4 MB/s |

Account availability is never the constraint — ~200 accounts carry free space. The worker count is, and it is bounded by the exit node rather than by bandwidth (§ *The throughput ceiling*).

> **Measure at the interface, not in the log.** Two reads of `netstat -ib -I en0` a minute apart (**field 10 `Obytes`** for upload, **field 7 `Ibytes`** for download — field 8 is a packet count and reads as a flat zero, which looks exactly like a stall). A per-file rate in `media_sync.log` is a **per-stream** rate: it reads *lower* than a serial transfer would while aggregate throughput is many times higher. That is expected, not a regression.

---

## Storage model (2026-08): the SSD + disposable external-drive caches

The library is served from the MEGA pool through the `mediafs` mount, with **local storage as a cache in front of it**. There are two kinds of local storage, and both are optional/disposable:

* **The Mac SSD library root** (`config.SSD_LIBRARY_ROOT` = `~/Media`) — holds metadata sidecars permanently plus transient in-flight uploads, and is the `lower` beneath the mount.
* **Any number of external drives** — large bulk caches. A drive is auto-discovered (`config.discover_library_drives()`) if it carries a `.media-library` marker **or** a top-level `Shows/` folder, at the drive root **or** a legacy `MediaStore/` subfolder. Same `Shows/`/`Movies/`/`Comics/` hierarchy as the mount — **no per-drive naming**; drives are interchangeable and there can be zero, one, or many.

**Keep-and-back-up, not move.** Every media file is uploaded to the pool **and kept on its drive** (`GRADUATED_UPLOAD_THEN_REMOVE = False`). A drive is therefore disposable: while it's attached, its files serve locally (fast, no MEGA); if it's ever lost or wedged, `mediafs` serves those same paths from the pool instead (this is why everything keeps working when you unplug a drive — provided it finished uploading first). `mediafs` merges **all** attached drives into the view, re-discovering them every 60 s and skipping any that wedge, so plugging/unplugging is transparent.

**A drive that has content not yet on the pool** is uploaded from (Media-Syncer scans `config.scan_dirs()` = the SSD root + every *currently attached* drive), so the physical space is used *and* backed up. A drive with **free space** is filled with predicted content by the pre-downloader (below), keeping a **1/10 buffer** free (`DRIVE_BUFFER_FRACTION`).

**The drive set is resolved live, never frozen at import.** `config.scan_dirs()` and `config.local_media_roots()` are *functions*, memoised on a 5-second TTL — long enough that an `os.walk` driver doesn't re-stat `/Volumes` per file, short enough that plugging a drive in takes effect within one daemon cycle. This is load-bearing: `media_sync` runs for days at a stretch, so anything captured at import is a snapshot of whatever happened to be mounted at launch. See *Failure mode: a hot-plugged drive is never backed up* below for what that cost.

> **Deletes still go only through the mount.** Removing a title in Jellyfin/Infuse (an `unlink` through `mediafs`) removes it from every attached drive **and** queues a pool purge (the queue-only reaper). A file merely *vanishing* from a drive is treated as the drive being absent, never as a delete — so an unplugged drive never triggers a purge.
>
> **⚠️ Landmine: a Jellyfin item DELETE is a real FILE delete — and for backlog content it is irreversible.** Because that unlink path exists, `DELETE /Items/{id}` against the local Jellyfin (file-management is ON) is **not** a DB-only "forget the row" — Jellyfin deletes the underlying files through the mount, which `mediafs` then removes from every attached drive. For content still in the (weeks-long) **upload backlog** — drive-only, no pool copy yet — that is permanent loss, because there is nothing to re-download. **Never** delete a Jellyfin item to fix metadata or an orphaned entry: use a per-item recursive refresh or a `POST /Library/Refresh` (both only add/update, never delete a media file). The sibling **`media_doctor`** health daemon (in Torrent-Ingest) that auto-heals unresolved/poster-less series is built on exactly this rule — it heals via refresh/scan and never deletes.

### Drives DRAIN: uploaded content is deleted from them

`DELETE_DRIVE_COPY_AFTER_UPLOAD = True`. A file on an external drive is removed once the pool provably holds it, so the drive empties as the backlog uploads and **"the drive holds no media" is the signal that it can be unplugged for good.** Emptied directories are pruned behind it, and each cycle logs what remains:

```
drive /Volumes/Seagate/Media: 6572 media file(s), 2688 GB remaining
drive /Volumes/Seagate/Media holds NO media -- it can be unplugged.
```

This is the exact opposite of the policy for `SSD_LIBRARY_ROOT`, and deliberately so: the SSD is the **serving cache** and keeps its copy (`GRADUATED_UPLOAD_THEN_REMOVE = False`, with `predownload.py` managing that space by evicting cold, inventory-proven media). The drive is the tier being retired. The two settings are separate for that reason — do not collapse them.

#### The exact-size guard, and the residue it protects

A drive copy is deleted **only when its size matches the inventory's recorded size exactly** (`DRIVE_DELETE_REQUIRE_EXACT_SIZE`). An exact match is the only cheap proof that the pool holds *this* file rather than a different version of the same path, and two real populations make a looser test dangerous:

| Population | Count on Seagate | Why it is not deleted |
|---|---|---|
| exact match | 3514 files, 1425 GB | *(deleted — the pool holds these byte-for-byte)* |
| not on the pool | 6572 files, 2688 GB | must upload first |
| **larger** than the pool copy | 57 files, 35 GB | a better local encode; the pool copy is a different, worse file, and write-once means re-uploading will not correct it |
| **smaller** than the pool copy | 2 files, 0.8 GB | truncated, typically a download killed mid-flight, sitting at a real library path where `mediafs` serves it as valid media |

**A drive will therefore not reach empty on its own if it carries any mismatched files**, which is why the residue is logged with its cause rather than silently skipped. The larger ones need a decision (keep the better encode and accept the drive never empties, or overwrite the pool copy and lose write-once for those paths); the smaller ones are corrupt and should be deleted so the pool copy becomes authoritative.

### Nothing is downloaded ONTO a drive: `PREDOWNLOAD_FILL_DRIVES = False`

**Nothing is downloaded onto an external drive any more.** `fill_drives()` is gated off at its one call site in `reconcile()`. Every *other* drive role is untouched and deliberately so — this is the "scan drives, upload from them, never write to them" model:

| Drive role | Status |
|---|---|
| Discovered live (`discover_library_drives()`, 5 s TTL) | **kept** |
| Merged into the `mediafs` view, files serve locally | **kept** |
| Scanned by `find_local_files()` → uploaded to the pool | **kept** |
| Organized/renamed in place by `drive_ingest` (Torrent-Ingest) | **kept** |
| Filled with predicted pool content as bulk cache | **OFF** |

Filling a drive is right while that drive **is** the library, and wrong while it is being retired, for three compounding reasons: it pulls pool content **down** at ~4.3 GiB/h over the same exit node the uploader is pushing a multi-TB backlog **up**; every byte it writes lands on the disk being made disposable and must later be re-verified as pool-resident; and it is the phase most prone to starving `reconcile()`'s self-healing (see the failure mode two sections down). Set the flag `True` to re-enable it — the code path is unchanged.

> **The section below argues drive-fill and upload should run concurrently and neither should yield.** That holds for a drive being **kept**, not one being **emptied**: when the goal is to get every byte off a disk and onto the pool, a phase whose job is putting more bytes onto that disk is not a neutral concurrent workload. The reasoning there about *why pausing one does not speed the other* is still the right way to think about the general case.

### Predictive pre-download uses the SSD **and** the drives

`predownload.py` keeps the likely-next content local so playback is instant. Its ephemeral hot-set lives in the SSD tier cache (evict-on-watched); additionally, `fill_drives()` uses **leftover drive space** as bulk predictive capacity — it downloads pool content a drive doesn't already hold onto the drive at its real library path (served locally, and Media-Syncer sees it's already on a remote so never re-uploads), predicted content first then the rest of the pool, always leaving the 1/10 buffer. **Drive-fill and upload run concurrently, and deliberately so.** `predownload`'s `fill_drives()` and `media_sync`'s upload phase are independent daemons pulling in opposite directions over the same MEGA link, and neither yields to the other: a drive's free space gets used even while the upload backlog is still draining. Gating downloads on a drained backlog would idle the drives for weeks and would not make uploads any faster, because the two directions do not contend for the same bottleneck in a way that pausing one relieves.

> **There is no upload-backlog gate on drive-fill**, and no other condition that holds it inert waiting for the fleet to catch up. If a drive looks like it is not filling, see *Drive-fill is slow by design* below before looking for a gate.

#### Drive-fill is slow by design, and `df -h` cannot show it

**A drive that is filling correctly looks identical to a drive that is doing nothing, and it will look that way for a week.** Measured 2026-08-08 over 119 active hours: **4.29 GiB/hour**, ~103 GiB/day, no hour at zero. Against a 7.3 TiB drive that is **1.4% of capacity per day** — `df -h` prints `3.5Ti` for about a week straight and the percentage column moves one point per day. This is the single most confusing thing about the subsystem and it has cost multiple investigations.

The rate is bounded by three things, none of which is a fault:

* **MEGA download bandwidth**, 2–3 MB/s, which is the floor under everything.
* **`PREDOWNLOAD_DRIVE_FILL_MAX_SEC` (15 min)**, which deliberately hands control back to `reconcile()` each cycle so stale-partial reaping and SSD eviction keep running — the alternative is the 20-hour stall documented under *the pre-download cache starves the torrent downloader*.
* **IP rotation after every chunk**, which costs a reconnect per chunk.

**Do not diagnose this with `df -h`.** To tell a filling drive from a stalled one, in increasing order of effort:

1. `grep 'drive-fill: ' predownload.log | tail` — targets should be recent and changing.
2. Aggregate MB placed per hour from those same lines. A healthy drive shows a non-zero figure every hour; a stalled one shows a hard stop at some timestamp.
3. Take the last few drive-fill targets and check they exist on the drive at exactly their `remote_inventory.json` size. All but the in-flight one should match.
4. Sample `df -k` (**not** `-h`) twice, several minutes apart, and compare the raw used-KB figures. At 4 GiB/h a three-minute window moves ~200 MB — visible in kilobytes, invisible in the rounded human-readable output.

## The throughput ceiling is the exit node, and it moves

Read this before optimising transfer throughput, because it says where the remaining headroom is **not**.

Three limits stack. Two are handled by concurrency across accounts; the third is the wall — and **the wall is wherever the currently-selected Mullvad exit node puts it**, which varies by a factor of two:

| Exit node | Aggregate upload |
|---|---|
| `se-mma` (Stockholm) | **~24 MB/s** |
| `us-den` (Denver) | **~42 MB/s** |

Rotation picks from the whole allowed set (`config.BLOCKED_EXIT_COUNTRIES` only excludes countries where the AI APIs are unavailable), so throughput swings with geography as the node cycles. **Do not read a single throughput measurement as the system's ceiling** — record which exit node it was taken on, or the number means nothing.

| Layer | Measured | Status |
|---|---|---|
| MEGA per-**account** throttle | ~1.6–2.6 MB/s per stream | handled — N streams on N distinct accounts (uploads *and* downloads) |
| **Tailscale/Mullvad exit node** | **~24–42 MB/s aggregate, node-dependent** | **the wall** |
| Local ethernet | ~50 MB/s (measured outside the tunnel) | not the limit |

Upload aggregate at `en0` as concurrency rises:

```
 6 streams    9.3 MB/s
14 streams   23.4 MB/s
18 streams   24.4 MB/s     <- +4% for 29% more streams
```

Per-stream throughput falls from 1.67 to 1.35 MB/s across that last step — the signature of a **shared** cap rather than a per-account one. Confirmed with MEGA removed from the experiment entirely: an 8-connection download from a public CDN through the same tunnel measures **21.3 MB/s** on that node.

`UPLOAD_WORKERS = 16` saturates a slow node and still leaves per-stream headroom on a fast one (2.6 MB/s each at 42 MB/s aggregate). **More workers, more accounts, and bigger buffers do not move the shared ceiling** — only a better exit node does.

> **Selecting exit nodes by proximity rather than at random is unexplored headroom**, worth roughly +75% on this evidence. It trades against the IP diversity rotation exists for, so it is a real decision rather than a free win.

### Breaking past it

Two options exist, neither free, which is why they are documented rather than implemented:

* **Multiple exit nodes.** Tailscale's exit node is machine-global, so per-process egress IPs mean running several userspace `tailscaled` instances (`--tun=userspace-networking --socks5-server=…`), each authed as its own tailnet device with its own state dir, each pinned to a different exit, with rclone pointed at them via `ALL_PROXY`. That is N more supervised processes in a fleet whose README already flags *"Tailscale is an unsupervised single point of failure"* and keeps a watchdog for the one tunnel it has.
* **Route MEGA outside the tunnel.** By far the simpler change — the split-tunnel machinery already exists for Anthropic (`split_tunnel_anthropic.sh`). But the VPN in front of MEGA is deliberate: it is what keeps ~390 accounts from all transacting from one residential IP. Removing it trades throughput for a real account-linkage/ban risk across the whole pool, which is a policy decision about the pool's survival, not a tuning knob.

**Pool capacity, not bandwidth, is what gates graduation** (see *Pool capacity provisioning*) — doubling throughput would halve a wait that is not the binding constraint.

---

## The remote rescan runs in parallel

The rescan is `rclone lsjson --recursive` plus `rclone about` on every account. Each costs ~1.56 s and ~1.77 s of almost pure latency, so run serially across ~390 accounts that is **~22 minutes** with the uploader completely idle. Both sweeps run at `SCAN_WORKERS` (8) instead:

| Sweep | Serial | Parallel |
|---|---|---|
| `lsjson` × 391 | 10.2 min | **38 s** |
| `about` × 391 | 11.5 min | **41 s** |

`refresh_free_space()` performs the free-space sweep and writes `free_space.json` **once**. Writing once is required, not tidiness: the per-remote accessor rewrites the entire file on every call, so concurrent use would have threads serialising different snapshots over each other and eventually truncate it.

#### Every write to `rclone.conf` must be atomic

**Fingerprint: a burst of `CRITICAL: Failed to create file system for "<remote>:": didn't find section in config file`, on remotes that are perfectly fine when tested by hand.**

`rclone.conf` is a single file that **every** rclone process on the machine reads, and two code paths write it: `purge_mega_session()` (a read-modify-write, triggered by MEGA stale-session panics, which are routine under a parallel sweep) and `install_rclone_conf()` (publishing the repo copy each cycle). Concurrency turns two distinct hazards live:

* **Lost updates** — two threads read the same original and each writes back its own edit, so one remote's purge vanishes.
* **Torn reads**, the damaging one. A non-atomic rewrite leaves a window where the file is partial or empty; an rclone subprocess reading inside it dies with the message above. That *looks* like a broken remote and is really a broken read — and it cascades, because that message is also a stale-session signature, which triggers another purge.

Both writers take `_conf_lock` and both swap the file in with `os.replace`. Both mechanisms are required and fix different things: the lock handles writer-vs-writer, and only the atomic swap handles writer-vs-**reader**, because rclone is a separate process. Each writer also refuses to publish a config with no sections, so a truncated read cannot be laundered into a truncated write.

> **The rule this enforces:** making anything in this repo parallel promotes every unguarded read-modify-write from latent to live. `free_space.json` and `rclone.conf` are the two shared files; both are now written exactly once per operation, atomically.

#### The parallel sweeps must not rotate the exit node (`rotate=False`)

A rotation resets **every** TCP connection on the machine, so under a parallel sweep one rotation fails all the other in-flight listings, each of which then asks to rotate again — while also killing any uploads running alongside. `ROTATE_MIN_INTERVAL_SEC` (180 s) caps how much of that actually happens, but the failed calls are still wasted work.

Rotation is pointless for a listing regardless: exit-node churn exists to dodge MEGA's **transfer** throttling, and a listing moves no payload, so a failure there is almost always a blip that a plain retry clears. Both parallel sweeps pass `repeat_command(..., rotate=False)`. Transfer paths still rotate.

> **`build_remote_index()` persists `remote_inventory.json` as a side effect.** Calling it with a subset of remotes silently truncates the inventory to that subset. Always pass the full `find_mega_remotes()` list; a complete rebuild takes ~38 s.

---

## Download throughput: the throttle is per-ACCOUNT, not per-IP

The streaming section states that **four workers are slower than two**, which reads like a per-IP wall. That measurement is real but narrow: it is four workers on **one file from one account**, where the per-account throttle is exactly what pushes back. It says nothing about concurrency across *different* accounts. Measured on real pool files:

| Streams | Accounts | Aggregate | Per stream |
|---|---|---|---|
| 1 | 1 | **3.28 MB/s** | 3.28 MB/s |
| 8 | 8 distinct | **9.21 MB/s** batch · **13.46 MB/s** at `en0` | ~1.8 MB/s |

Downloads therefore scale the same way uploads do, using the same claim-a-distinct-remote pattern — **no multi-tunnel or multi-exit-IP machinery is required.** (Tailscale's exit node is machine-global, so per-process egress IPs would mean running several userspace `tailscaled` instances with their own SOCKS ports. Worth knowing that buys nothing here.)

`_prefetch_parallel()` (`predownload.py`) runs `config.PREDOWNLOAD_WORKERS` (6) concurrent fetches, one per account, and differs from the upload phase in two deliberate ways:

* **Held below `UPLOAD_WORKERS`.** Prefetch is speculative work whose whole purpose is making playback instant, so it must never be why a cold title stutters. `mediafs` prioritizes interactive reads over its *own* fill workers but cannot deprioritize a separate predownload process.
* **Strict priority order, not largest-first.** `to_download` is ordered by how likely you are to want the file next (active playlist → folder look-ahead → unwatched-ahead). Reordering it for packing efficiency would discard the only property that makes prefetching worth doing.

Budget and the SSD floor are re-checked live under the lock, since several workers consume the same disk concurrently.

### Bulk downloads rotate the VPN only on failure

`transfer.chunked_download()` must never call `rotate_exit_node()` after a chunk merely because it *finished* — the same rule the streaming path follows (*Progressive streaming*). Rotating on success costs 10–40 s of dead air per chunk (tunnel drop + re-auth) for no benefit, and because a rotation resets every TCP connection on the machine, a background chunk completing would tear down every concurrent upload alongside it. A chunk that genuinely fails does rotate before retrying, which is the case rotation exists for.

---

## Failure mode: an account pushed past its 20 GB quota

**Fingerprint: `rclone about` reports a remote at more than `REMOTE_CAP_BYTES` used, and MEGA starts refusing writes to it.** Three separate mechanisms cause it, and they compound.

### 1. MEGA does not replace a file on same-path upload

MEGA keys on **node ids, not paths**, so a second `copyto` to an existing path adds *another node* rather than overwriting. One path can therefore hold several nodes, which breaks the single-residence invariant (`remote_inventory.json` is a path→remote map and silently keeps one of them) and leaves the rest as orphans nothing will ever delete.

The trigger is retrying an upload that only *appeared* to fail. `run_command` kills rclone at `TIMEOUT(file_size)`, and a killed local process says nothing about what MEGA received — the bytes may all be there. Re-uploading then duplicates rather than replaces.

`_remote_has()` closes this: before a failure is believed, the remote is asked whether it already holds the file at the right size. If it does, the transfer succeeded and only the local process died. It is deliberately conservative — any doubt (failed listing, size mismatch) answers "not there" and the caller retries, because a needless retry costs bandwidth while a wrong "it is there" loses the file.

**Repairing existing duplicates:** `rclone dedupe --dedupe-mode largest <remote>:` removes the extra nodes, and it must be followed by `rclone cleanup <remote>:` — see below.

### 2. Deleted files keep consuming quota until the rubbish bin is emptied

MEGA moves deletions to a rubbish bin that **still counts against the account's quota**. A dedupe that removes 14 GB of duplicate nodes frees nothing until `rclone cleanup` runs; `about` keeps reporting the old figure and the account stays over quota. Every delete path in this repo pairs `deletefile` with `cleanup` for exactly this reason.

### 3. A path must live on exactly ONE remote, and duplicates are invisible

`remote_inventory.json` is a **path → remote** map, so if the same path exists on two
remotes the inventory records one and the other becomes an orphan: nothing reads it,
nothing deletes it, and it consumes that account's quota permanently. Nothing in the normal
cycle will ever notice.

Two things create them, and both look like success at the time:

* **A retried upload that landed.** The retry picks a *different* remote, so the same path
  ends up on two accounts. `_remote_has()` is the fix (§ 1).
* **Any ad-hoc upload for measurement or testing.** Benchmarking upload throughput by
  pushing real library files to spare remotes leaves exactly this residue — 7 GB of it in
  one session. **If you must benchmark with real files, upload to a path outside the
  library namespace and delete it afterwards**, or the pool quietly grows orphans.

Auditing for them is a full-pool scan (~40 s parallel): list every remote, group by path,
and report any path appearing more than once. Repair keeps the copy the inventory already
points at — verified present at the right size first — then deletes the others and runs
`cleanup` on each affected remote.

### 4. Two allocators against one pool

The upload phase's free-space ledger is arithmetic on a snapshot: seeded once from `free_space.json`, debited locally as files are claimed. It is correct only while **nothing else writes to the pool**. Run a second uploader concurrently — even a one-off maintenance script — and both read the same snapshot, both believe the same space is free, and both spend it. Neither is wrong on its own terms; together they overfill.

**The metadata-backup remote is excluded from allocation entirely.** `vm_mega1` is written by three daemons on their own schedules — Media-Syncer's `backup_state`, and Torrent-Ingest's `db_guardian` and `backup_metadata` — none of which can take the upload lock without either starving behind a days-long upload phase or blocking it. Serialising them is the wrong answer, so `config.upload_excluded_remotes()` keeps the media uploader from ever placing a file there and the contention cannot arise.

> **Excluded remotes are still SCANNED by `build_remote_index`.** They already hold media, and dropping them from the inventory would make that media look absent from the pool — so it would be re-uploaded elsewhere, creating exactly the duplicate paths this mechanism exists to prevent. Exclusion applies to **allocation only**, including the live-resync path, which must not resurrect an excluded remote however much room it reports.

**This is enforced, not merely documented** (`scripts/uploader_lock.py`). `media_sync` holds an exclusive `flock` on `upload.lock` for the duration of its upload phase; a maintenance script that writes to the pool calls `uploader_lock.acquire_or_die()` and exits with an explanation rather than competing. The lock uses `flock` precisely so it is released if the holder is killed — `media_sync` is killed routinely by the reaper, and a lock that outlived that would wedge the fleet.

A daemon that cannot get the lock **skips the upload phase for that cycle** rather than queueing behind the other holder: by the time it acquired the lock its ledger snapshot would be stale anyway.

> Stopping the daemon for maintenance still takes two steps, because the watchdog will otherwise undo the first.
>
> ```
> launchctl bootout gui/$UID/com.mikeyferguson.mediasyncwatchdog
> launchctl kill SIGTERM gui/$UID/com.mikeyferguson.mediasync
> ```
>
> A plain kill leaves media_sync down for `ABSENT_GRACE_SEC` and then the watchdog correctly relaunches it — mid-maintenance, on whatever code is on disk. Reinstate both afterwards with `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.mikeyferguson.mediasyncwatchdog.plist`. (The reaper does not need this dance: it writes `REAP_PAUSED_MARKER`, which the watchdog honours.)

Three guards limit the damage when the ledger drifts anyway:

* **`REMOTE_FILL_MARGIN_BYTES` (512 MB)** — a remote is treated as full while its ledger free space is below this, so rubbish-bin lag, a timed-out-but-landed upload, or a capacity that is not exactly `REMOTE_CAP_BYTES` cannot tip it over.
* **`LEDGER_RESYNC_SEC` / `LEDGER_RESYNC_WHEN_BELOW`** — the remote's figure is re-read live before anything is written to it when *either* the claim would leave the ledger under 2 GB, or **the claim itself is larger than 2 GB**. Rate-limited per remote by `LEDGER_RESYNC_SEC`, so the extra `about` costs at most one round trip per remote per 15 minutes. Both tests measure the same thing from opposite ends — drift is dangerous in proportion to what is about to be spent — and the size test is the one that catches a single large file, which the balance test cannot (§ 5).
* **`operations.usable_free()`** — the ledger seed and every live re-read are the **minimum of two independent signals**, `rclone about` and the inventory (§ 5).

### 5. One snapshot, many upload phases (the temporal allocator)

**Fingerprint: an account well past `REMOTE_CAP_BYTES` whose `remote_inventory.json` byte total matches its `rclone about` usage exactly** — no duplicate nodes, no rubbish-bin lag, no second process. Every byte is a live, tracked, singly-resident file. The allocator simply placed more than 20 GB there and had no way to know.

§ 4 is the *spatial* version of the hazard: two processes spending one snapshot. This is the *temporal* one, and it needs only the one daemon. The upload phase seeds its ledger from `free_space.json`, debits it in memory, and never writes those debits back. `free_space.json` is refreshed by the `about` sweep on the `REMOTE_RESCAN_SEC` (6 h) cadence — but a phase ends when the local backlog is drained, and `main()` has no sleep, so the next cycle starts immediately and **reseeds from the same snapshot**. Every phase between two sweeps therefore begins believing the pool is as empty as it was at the last sweep, and each is free to spend the same bytes again. Neither the margin nor the pre-claim resync notices, because the drift is not in the arithmetic — the arithmetic is correct, on a premise that is hours stale.

The failure it produced, which is also the shape of the arithmetic: eight phases ran in the three and a half hours after one sweep, all seeding a fresh account at 10.24 GB free. By the eighth its real contents were 15.6 GB. A 7.98 GB claim (a 7.84 GB film plus the margin) cleared the 10.24 GB the ledger believed, and the account finished at **23.46 GB against a 20 GB quota**. It never looked nearly full at any point: post-claim the ledger still read 2.26 GB, above the 2 GB resync threshold by 260 MB, so no live check was triggered. Multithreading is not implicated — within a phase the ledger is debited under `_upload_lock` at claim time and is exactly right.

**The fix is a second signal that cannot go stale, and `remote_inventory.json` already is one.** A row is added to it inside the upload worker's critical section, so it knows about a file the instant it lands; the `about` figure learns hours later. `operations.usable_free(entry, placed)` returns the **stingier** of the two, and the two fail in opposite directions, which is why the minimum is the right combinator:

| Signal | Blind to | Covered by |
|---|---|---|
| `rclone about` used | everything uploaded since the last sweep | the inventory |
| inventory bytes | bytes present but not in the path→remote map (an orphaned duplicate node, an ad-hoc upload outside the library namespace) | `about`, because MEGA charges for them |

`free_space_ledger()`, `get_remote_free_space()`, `mega_accounts.pool_free_bytes()` and `check_space.py` all resolve through it, so there is **one** definition of how much room an account has. A local copy of the arithmetic is how the phone report ends up disagreeing with the allocator about which accounts are full. Inside a phase the same figure is tracked forward in `placed`, seeded from the inventory and incremented on every success — the ledger counts *down* from a snapshot and so inherits its age, while `placed` counts *up* and does not.

The inventory errs toward reporting a remote **fuller** than it is: the reaper's deletions leave their rows until the next rescan, so a little capacity is stranded for at most one rescan interval. That is the correct direction to be wrong in.

**Repair: `scripts/rebalance_overfull.py`.** Once an account is over quota MEGA refuses writes to it, so it is dead capacity until it comes back under. The script moves its largest files off, gaplessly — verify the destination holds the file at the right size, *then* delete the source copy and empty that remote's rubbish bin — so an interrupted run leaves a duplicate, never a hole. It prefers the local copy as the transfer source when one is present at the right size, since a remote-to-remote copy streams down and back up through this machine.

### 6. The metadata-backup remote filling itself

`rebalance_overfull.py` cannot help the one remote this repo's own allocator never touches: `vm_mega1` (the metadata-backup remote, excluded from allocation in § 4) can still go **over quota**, but by a different writer — its own backups. Its `metadata-backup/_versions/` trees (`backup_state`'s hourly state versions, and Torrent-Ingest's nightly `backup_metadata.py` versions) are created by `--backup-dir` and, before the retention fix, were **never pruned**, so they grew without bound. Worse, `use_trash = true` means every delete — including `--backup-dir`'s version moves and `db_guardian`'s pruned DB snapshots — sits in the MEGA rubbish bin and keeps consuming quota until `rclone cleanup` runs, and nothing was running it. The two compound: unbounded live versions **plus** an ever-growing rubbish bin filled the account until `rclone about` flipped to MEGA's garbage "max-int free" and every backup started returning `Request over quota`.

**Fingerprint:** `backup_state` logging `FAILED (exit 1): … Request over quota` on `vm_mega1`, while `rclone about vm_mega1:` shows `used` above `REMOTE_CAP_BYTES` and a max-int `free`. (`check_space.py` does not flag it — the remote is excluded from the pool scan, which is exactly why it can fill unseen.)

**Repair (done, and now prevented):** prune the stale version dirs and empty the bin. The fix is structural, not a one-off:

* `backup_state.py` retains only the newest `STATE_BACKUP_KEEP_VERSIONS` (72) state version dirs, then runs `rclone cleanup` on the remote every run.
* Torrent-Ingest's `backup_metadata.py` retains `METADATA_BACKUP_KEEP_VERSIONS` (14) nightly version dirs and cleans up likewise, and its `db_guardian.py` runs `cleanup` after pruning old DB snapshots.

So the account's footprint is bounded and its rubbish bin can never accumulate. **Do not** "fix" an over-quota `vm_mega1` by provisioning a new account (§ provisioning is about *placeable* pool space, and this remote is not in the pool); prune + `cleanup` is the whole repair.

---

## Pool capacity provisioning: continuous, and demand-aware

Capacity is checked **during** the upload phase, not once per cycle. The pool drains at ~68 GB/h and a large backlog keeps a single cycle running for days, so a per-cycle check fires long after the pool ran dry — and every upload past that point fails with `Could not upload ... No suitable remote found`, which is only a per-file warning. The backlog stops draining with nothing in any log tying it to capacity.

* **A provisioner thread runs for the life of the upload phase**, checking every `POOL_PROVISION_CHECK_SEC` (5 min). The check is free: the phase already keeps a live free-space ledger, so "how much room is left" is a sum over a dict, not an `rclone about` sweep. It runs on its own thread so a provisioning run — account registration, IMAP confirmation, a git push, i.e. minutes — never blocks a transfer. New accounts merge into `REMOTES` and the ledger immediately, so workers can use them without waiting for the next cycle.
* **It budgets against *placeable* free, not aggregate free.** Every claim reserves `REMOTE_FILL_MARGIN_BYTES` (512 MB) before placing anything, so a remote holding ~512 MB free can accept *no* file, and the metadata-backup remote is excluded from allocation entirely. Aggregate free counts those margin slivers and the backup remote as spendable, which is how the pool stalled at **224 GB "free" and ~1 GB placeable** with the provisioner convinced it had room to spare. `operations.placeable_free()` = `max(0, usable_free − margin)`, `mega_accounts.pool_placeable_bytes()` sums it over the pool (excluded remotes dropped), and `placeable_free_sum()` does the same over a live ledger. All three provisioning triggers — reactive floor, proactive `pending > free`, and the initial up-front check — now use the placeable figure, so fragmentation reads as a shortage and gets provisioned away instead of idling the uploader forever.
* **The floor is hours of drain, not bytes.** A fixed byte floor is a time budget in disguise and silently decays whenever throughput changes. `provision_floor_bytes()` derives it from the **observed** drain rate (`POOL_PROVISION_LEAD_HOURS`, 6 h), so the lead time stays constant however fast the uploader gets.
* **Batch size closes the actual deficit.** `accounts_for_bytes()` sizes the run to the gap, capped at `ACCOUNTS_MAX_PER_RUN` (25). A fixed small batch buys under two hours at the current drain rate and just triggers provisioning again immediately.
* **Proactive, not only reactive.** `find_local_files()` knows the pending (not-yet-uploaded) byte count, so the deficit — `pending − placeable free` — is knowable the moment content lands, long before placeable free approaches any floor. The upload phase provisions for the whole backlog up front, before a byte moves.

> **An account going over quota is never a capacity problem, so do not reach for provisioning to fix one.** The two are unrelated: overfilling happens with hundreds of GB free and dozens of empty accounts sitting in the pool, because the allocator's belief about *one* account was wrong, not because the pool was short. Diagnose it as § *One snapshot, many upload phases*; provisioning more accounts just spreads the same mistake over more of them.

### The media_sync watchdog

`com.mikeyferguson.mediasync` carries **no `KeepAlive`** on purpose: the reaper kills it while purging deletions, and a KeepAlive would relaunch it mid-purge. The reaper restarts it afterwards itself (`reap.ms_resume`, which retries and verifies), so the normal pause/resume cycle needs no help.

What that leaves uncovered is media_sync dying for some *other* reason — an unhandled crash, an OOM kill, a broken interpreter upgrade. Its loop swallows every exception so this is rare, but nothing brings it back and the failure is silent: uploads simply stop.

`scripts/mediasync_watchdog.py` (agent `com.mikeyferguson.mediasyncwatchdog`, KeepAlive **on** — it holds no lock and touches no media, so it is useless if it dies) relaunches media_sync only when **both** hold:

* the process is absent, **and**
* the reaper's `REAP_PAUSED_MARKER` is **not** present.

So a deliberate pause is respected and an accidental death is repaired. It reads the marker path from Torrent-Ingest's own config so the two cannot drift, and waits `ABSENT_GRACE_SEC` (120 s) before acting — there is a brief window during a pause where the reaper has killed the daemon but not yet written its marker. `python3 -m scripts.mediasync_watchdog --once` prints the current verdict (`running` / `paused` / `relaunched` / `relaunch-failed`) without looping.

### `.DS_Store` is pruned during the upload scan

Finder writes a `.DS_Store` into every folder it opens, including folders on the library drives. They are never uploaded (`find_local_files()` skips dotfiles) but they accumulate on disk and travel with a drive. The upload scan already walks every root once per cycle, so the prune rides inside that walk — no extra traversal, no extra `stat`. A non-zero sweep logs `pruned N .DS_Store file(s) from the library roots`.

Torrent-Ingest does the same for the GetComics iCloud watch folder, where they are worse: those files sync to every device.

### Failure mode: a hot-plugged drive is never backed up (the "that drive has been plugged in for weeks" bug)

**Fingerprint: a drive is mounted, `mediafs` serves its files, the pre-downloader happily *fills* it — and none of its content ever reaches the pool. No error, no warning, `media_sync` reports healthy the entire time.**

The drive list must be resolved **per call**, never captured at import. `media_sync` is a long-lived daemon — days of uptime is normal — so a module-level list built once from `discover_library_drives()` is a snapshot of whatever happened to be mounted at launch. Two consequences follow, and the second is the dangerous one:

* `find_local_files()` never walks the new drive, so its content is not even a *candidate* for upload.
* In the upload phase, `get_root_for_path()` returns `None` for a file under an unknown root, and the call site's failure branch is a bare `continue` (`media_sync.py`). **Silent skip.** A whole drive can sit un-backed-up indefinitely while every log line says the daemon is fine.

The two halves of the system also drift apart: `fill_drives()` and `_drive_resident_rels()` in `predownload.py` resolve drives **live** every cycle, so the pre-downloader pours predicted content *onto* a hot-plugged drive that `media_sync` never uploads *from* — actively growing an invisible un-backed-up set. Combined with the landmine above (a Jellyfin item DELETE is a real file delete, and backlog content has no pool copy to restore from), that is a path to permanent loss.

`config.scan_dirs()` and `config.local_media_roots()` are therefore **functions**, re-resolved per call and memoised on a 5-second TTL — long enough that an `os.walk` driver does not re-stat `/Volumes` per file, short enough that plugging a drive in takes effect within one cycle.

> **The general rule:** in a daemon that runs for days, *any* module-level constant derived from mutable system state is a bug waiting for a schedule. `discover_library_drives()` is correct and generic; what matters is **when** it is called.

### Failure mode: the pre-download cache starves the torrent downloader (the "a show just never downloaded" bug)

**Fingerprint: a title you dropped a `.torrent` for never appears, its `.torrent` still sits in the iCloud watch folder (not in `finished/` or `failed/`), and *nothing anywhere* explains why.** Its journal record reads `"status": "queued"` forever. This is a deadlock between two daemons that share the Mac SSD and are each individually correct.

Torrent-Ingest's `admit_downloads()` requires `MIN_FREE_BYTES` (20 GiB) + `size × SPACE_SAFETY_FACTOR` (1.15) of **real free space on the SSD** before it starts a torrent. If that does not fit it leaves the record `QUEUED`, on the assumption that the drive drains. `predownload.py` is the space manager on the other side, and it *fills* that same SSD with predicted content while evicting only media already proven on a remote — so the two sets of floors have to stay compatible. Three ways they come apart:

* **A ratio is not a floor.** `storage_plan()` keeps `breathing = reference // 10` free, where `reference = free + cache_used`, which *slides down with the disk*: at 31 GB free it is content to keep 36 GB free while a queued torrent needs 48 GB. The clamp `breathing = max(ref // 10, SSD_MIN_FREE_BYTES)` is what makes it a real floor, and that floor is a **fraction of total disk capacity** (0.175, clamped to 25–200 GiB; 80.6 GiB here) rather than of free space — see *The floor scales to the host* above, since a fraction of *free* space is exactly the failure in this bullet. `reconcile()` also evicts against the **live** `shutil.disk_usage().free`, not only its own budget arithmetic, because the budget model cannot see bytes the disk lost to anything but the cache.
* **Budget and enforcement must measure the same bytes the same way.** The progressive-streaming partials (`*.streaming`) are **sparse** files created at their full final length, so apparent size wildly overstates them — 175.5 GB apparent against 27.0 GB really allocated across 94 files. If the budget sums `st_size` while occupancy skips partials entirely, the daemon believes it has ~115 GB of unused budget on a disk with 31 GB free. `config._cache_bytes_on_disk()` sums `st_blocks × 512` (real allocation) for everything.
* **A long transfer phase starves `reconcile()`, which is where the self-healing lives.** `fill_drives()` is called at the *tail* of `reconcile()` and walks the entire pool inventory; against a mostly-empty 7 TB drive that is **days** inside one call. Stale-partial reaping and SSD-cache eviction both live in `reconcile()`, so neither runs again for the duration — 94 orphaned partials against a 30-minute reap threshold, and a `cycle:` line that has not been written in 20 hours. Both transfer phases are therefore time-bounded (`PREDOWNLOAD_DRIVE_FILL_MAX_SEC`, `PREDOWNLOAD_PREFETCH_MAX_SEC`), which is safe because progress is monotonic: already-present files are skipped, so the next cycle resumes where the last stopped.

**Both sides log now, which is what makes this diagnosable.** `admit_downloads()` emits a throttled (hourly, per torrent) `DEFERRED <name> (<need> needed, <admittable>, <free> free, <floor> floor)` line, and `reconcile()` warns when the SSD is under its floor and eviction *cannot* reach it. That warning is narrow: both evictors have already run, so what remains is predicted, just-read, or **not yet uploaded** — and the inventory guard correctly refuses to evict un-uploaded media. **A standing `eviction cannot reach it` is therefore a symptom of upload throughput, not of eviction.** Check the uploader before touching anything else. **If a drop never appears, grep `DEFERRED` in `torrent_ingest.log` first.**

> **Diagnosing a repeat, in order:** (1) is the `.torrent` still in the watch folder rather than `finished/`? (2) `grep -c '"status": "queued"'` the Torrent-Ingest journal for the record and read its `total_size`; (3) compare `df` free space on the SSD against `20 GiB + size × 1.15`; (4) check whether `predownload.log`'s last `cycle:` line is hours stale — if so, `fill_drives` is inside a long walk and `reconcile`'s eviction isn't running. **Do not hard-kill `predownload` mid-`chunked_download`:** the partial-cleanup `unlink` only runs on the retry-exhaustion path, not on process death, so a `SIGKILL` leaves a short file at a **real library path** on the drive, which `mediafs` then serves as truncated media. Note the in-flight file from the last `drive-fill:` log line and delete it after stopping the daemon. When auditing a drive for such partials, **only a file SMALLER than its inventory size is suspect** — plenty of drive files are legitimately *larger* than the pool copy (a better local encode; 61 such on Seagate, e.g. *Yu Yu Hakusho* at 611 MB vs 191 MB in the pool). Those are not corruption, but note they can never be evicted either: the `_is_uploaded` delete guard demands an exact size match, so they are pinned to the drive forever.

### Failure mode: a show serves ZERO episodes with a perfectly healthy DB (`SeriesPresentationUniqueKey` drift)

**The fourth "Jellyfin looks broken but the DB is fine" fingerprint, and the most deceptive one.** A series exists, its files are on disk, `/Items?Recursive=true` finds every episode with the correct `SeriesId` — and `/Shows/{id}/Episodes` returns **0**. The show is simply absent from the app. Nothing reports damage: `PRAGMA integrity_check` is `ok`, `ParentId` is right, `SeriesId` is right, `AncestorIds` has the right row count, no error appears in any log, and the request returns **HTTP 200** with an empty list.

The cause is that `/Shows/{id}/Seasons` and `/Shows/{id}/Episodes` **do not filter on `SeriesId`**. They filter on `SeriesPresentationUniqueKey == series.PresentationUniqueKey`. That key is derived from the series' **provider id**, and Jellyfin stamps it into each child **once, at child creation**. So when a series' children are created *before* its identity resolves, they capture the fallback key — the series' raw GUID — and the moment the provider id arrives the series' key becomes `<tvdbid>-en-<hash>`, stranding every child under a key nothing looks up any more.

Two ways into it, both routine: a **freshly ingested show** whose episodes get created before the metadata fetch lands, and **re-identifying a series** (`RemoteSearch/Apply`), which changes the series' key out from under its existing children.

**Nothing self-heals it.** A per-item metadata refresh does **not** recompute the key (verified directly against the DB). Neither does a full library scan — `media_doctor` fired scan after scan at *Soul Eater NOT!* for hours to no effect, because a scan re-walks folders and adds *missing* items; these items were not missing. The only Jellyfin-native cure would be deleting and recreating the children, and on this fleet **`DELETE /Items/{id}` deletes real files** (see that landmine above). So it must be repaired in the database.

**It is now repaired automatically, by `db_guardian`** (Torrent-Ingest — the daemon that already owns `jellyfin.db`): `reconcile_series_presentation_keys()` re-points any child whose key disagrees with its series. It is safe to automate — the column is a denormalized lookup with no foreign keys pointing at it, the statement is idempotent (it only touches disagreeing rows), and it runs **immediately after a verified backup has been promoted**, so the fallback is a known-good copy from seconds earlier. It re-runs `integrity_check` afterwards and alerts rather than continuing if anything is off. Disable with `DBG_RECONCILE_PRESENTATION_KEYS = False`.

`media_doctor` was also hardened: its `Jellyfin.episodes()` now re-checks an empty `/Shows/{id}/Episodes` result the robust way (filtering `/Items` by `SeriesId`) before believing it, so this class of drift can no longer make it report "resolved 0 of N" and hammer the library with scans that cannot possibly help.

> **Diagnosing a repeat:** the query below returns 0 on a healthy library. Any row is a show that is silently serving nothing.
>
> ```sql
> SELECT s.Name, COUNT(*) FROM BaseItems c JOIN BaseItems s ON s.Id = c.SeriesId
>  WHERE (c.Type LIKE '%Episode%' OR c.Type LIKE '%Season%')
>    AND c.SeriesPresentationUniqueKey <> s.PresentationUniqueKey GROUP BY s.Id;
> ```
>
> Repair order if you ever do it by hand, same as the `ExtraIds` fix: stop the **library supervisor** (or it relaunches Jellyfin mid-edit) → stop **db_guardian** (so its auto-heal can't race you) → quit Jellyfin → copy the DB → `UPDATE` → `integrity_check` → restart Jellyfin → re-bootstrap guardian and supervisor.

### Failure mode: one hung `ffprobe` holds the whole library scan hostage

**Fingerprint: `Scan Media Library` reports `state=Running` with `progress=0` for hours, its log goes silent, new titles never appear, and `media_doctor` reports `N flagged, 0 auto-fixed` pass after pass.** The mount is *fine* and the DB is *fine* — this is neither the wedged-mount nor the `ExtraIds` fingerprint above.

Jellyfin probes media with `ffprobe -analyzeduration 200M -probesize 1G`. On a **cold pool file** served through `mediafs` at MEGA's ~2–3 MB/s, reading up to 1 GB to probe one episode takes minutes at best, and if the fetch stalls the probe never returns — it parks in **uninterruptible sleep (`ps` state `U`)**, which per the wedged-mount section survives even `kill -9` until its read unblocks. Jellyfin's library scheduler runs a bounded worker pool, so a handful of parked probes consumes every slot and the scan **stops dead at 0%** while still reporting `Running`.



**Diagnosis and recovery:**

1. `ps axo pid,stat,etime,command | grep MacOS/ffprobe` — anything older than a few minutes, especially in state `U`, is parked. The `file:` argument names the victim.
2. **Prove the mount is healthy first**, so you don't misdiagnose this as the wedged-mount outage: a timeout-bounded `ls` of `~/MediaLibrary/Shows` should return in well under a second (234 entries in 0.1 s when healthy). If it returns fast, the mount is fine and only these reads are stuck.
3. **Kill the parked probes by PID.** They may not die until their read unblocks — that is expected for state `U`, and the `SIGTERM` still lands once it does. Jellyfin treats a killed probe as a cancelled one (`OperationCanceledException` in its log) and moves on; the item just lacks stream info until a later refresh, which is strictly better than a scan frozen for hours.
4. **Never `pkill -f ffprobe`-style pattern matching** — the same trap as `ffmpeg`: match on `MacOS/ffprobe` and verify each PID's parent is the Jellyfin backend, then kill by number. Killing Jellyfin itself leaves its menu-bar wrapper alive, which fools the library-supervisor's `pgrep` liveness check so it never relaunches the backend.
5. Re-check `progress` — it should climb immediately.

**Three mechanisms keep cold reads survivable**, and each addresses a distinct cost.

**1. Cold misses coalesce into persisted 4 MB blocks (`config.STREAM_COALESCE_SEGS`).** A FUSE read is ~128 KB. Fetching *exactly that range* with its own `rclone cat` costs a process spawn plus a full MEGA login to deliver 128 KB — auth dominates the transfer — and discarding it afterwards makes a *sequential* cold reader pay that toll about 8 times per megabyte, never getting faster. (`_ranged_fetch` does exactly that, correctly, for what it is built for: one seek, one MP4 `moov` tail, one comic index.) `read_stream` instead fetches the whole **segment-aligned block** covering the miss via `_fetch_into`, writes it into the `.streaming` partial and marks those segments `done` — the same result the background fill would have produced — so the next ~32 sequential reads are plain `os.pread`s with no network. Segments are claimed through `s.inflight` so a fill worker and a coalescing reader never fetch the same block twice. Measured: cold miss 6.2 s, **adjacent read 0.013 s**; per 4 MB sequential **~190 s → ~7 s (≈27×)**.

**2. The fill worker's yield to interactive reads is now bounded (`config.STREAM_FILL_YIELD_MAX_SEC`, 20 s).** `_seg_worker` waited on `readers_waiting > 0` *without a deadline*, which is a **priority inversion**: a sequential cold reader holds that counter above zero essentially continuously, so both fill workers parked forever, the efficient 4 MB bulk fill never ran, every read stayed a tiny on-demand fetch — which kept the counter up. The fill starved precisely when it was needed most. Past the ceiling the worker proceeds anyway.

**3. Metadata probers are refused cold pool reads outright (`config.MEDIAFS_DENY_PROBER_READS`).** This is the same principle as trickplay and chapter-image extraction being off: *nothing on a virtual library should read whole media files just to describe them.* `mediafs` identifies the calling process via `fuse_get_context()` and, if its executable basename is in `config.MEDIAFS_PROBER_NAMES`, fails the request with `EIO`. Jellyfin logs a cancelled probe and moves on instantly. Measured: **6h49m → 0.027 s.**

Four details that make it safe rather than blunt:

* **`ffprobe` only — deliberately NOT `ffmpeg`.** ffmpeg is Jellyfin's *transcoder*; denying it would break real playback of a cold file. ffprobe is metadata analysis and never serves frames.
* **The denial fires in `open()`, not `read()`.** Denying at first read is too late — `open()` has by then started a stream worker (which immediately fetches the head *and* tail priority segments, ~8 MB), fired `trigger_prefetch` (pulling the *next* episodes), and recorded an access that skews the pre-downloader's prediction. Across a ~10k-file scan that is tens of GB hydrated and a poisoned prediction signal. Verified: a denied probe now starts **zero** streams and triggers **zero** prefetch.
* **Local files are structurally unaffected.** Anything on the SSD or a drive, or fully cached, is served by the passthrough `fd` branch and never reaches the streaming path — so a freshly-ingested title (Torrent-Ingest lands new media on the SSD) still probes normally at disk speed. Verified: local probe 0.305 s, cold probe `EIO`.
* **The prober check fails OPEN.** Any error identifying the caller returns "not a prober" and serves the read: wrongly denying a legitimate reader would look like corrupt media, while wrongly allowing a prober merely costs bandwidth. The PID→verdict map is cached (30 s TTL) because this is consulted on every cold read and shelling out to `ps` would cost more than serving the bytes.

**Result:** the same scan that had sat at `progress=0` for hours went **40% → 85% in 35 seconds**, with no parked probes — Jellyfin now moves through cold files at ~20/second.

**Probing is allowed whenever the bytes are already local — which is what makes this safe.** The denial is not "cold files never get stream info"; it is "no probe may pull bytes over MEGA". Three cases, and only the third is refused:

| file state | probe |
| --- | --- |
| local on the SSD or a drive | normal, disk speed (passthrough `fd`) |
| pool file **fully in the tier cache** | normal, disk speed (passthrough `fd` from the cache) |
| pool file not cached | refused, ~0.03 s |

That middle row is load-bearing: `open()` must check the tier cache, not only `lower` and the drives. Otherwise a file the pre-downloader has already cached is still opened as a stream and its probe refused — a file that is free to read gets protected from reading. `open()` serves any fully-cached pool file (size matched against the inventory) as a plain read-only passthrough. Since `predownload.py` keeps the cache full of what you are about to watch, the content that matters is probed normally and refusals land on genuinely cold long-tail files.

> **The stream info is not lost — measured, not assumed.** Jellyfin logs `Error in "Probe Provider" — FfmpegException: ffprobe failed - streams and format are both null` and `File changed, pruning extracted data` for each refused file, which looks alarming and reads like it is destroying metadata. It is not: `pruning extracted data` refers to Jellyfin's *extracted* artifacts (subtitles/attachments/chapter images), not the `MediaStreams` rows. An audit across all 14,653 items found: **7156 of 7158 cold files still had full stream info**, as did 7491 of 7495 local ones — 6 gaps total in the whole library, and 3 of those were Soul Eater episodes, casualties of the *parked* probes rather than of the denial. Watch state is untouched regardless (`UserData` keys off the item id). Episode runtime also still displays from the `.nfo`'s `<runtime>`, and playback is unaffected because it runs through `ffmpeg`, which is deliberately not denied.
>
> **Healing a gap** (an item showing no duration/codecs): hydrate it and refresh — `python3 -m scripts.tier --hydrate '<relpath>'`, then `POST /Items/{id}/Refresh?metadataRefreshMode=FullRefresh&replaceAllMetadata=false`. Once cached it takes the passthrough path above, so the probe just works. Keep `replaceAllMetadata=false` — a full replace steamrolls `<lockdata>` (see Torrent-Ingest's README).
>
> Set `MEDIAFS_DENY_PROBER_READS = False` to disable the denial entirely. The coalescing and priority-inversion fixes are independent and stay in effect either way, so even then cold sequential reading is ~27× faster than before.

### Failure mode: a show keeps the wrong show's artwork after being re-identified

**Fingerprint: the metadata is perfect and the pictures are someone else's.** Plots, titles, episode numbering, provider ids — all correct. The poster is a different series. `PRAGMA integrity_check` is `ok`, every image file is present, well-formed, and the right resolution, and nothing in any log, report, or health check says a word. It is the only failure in this document that **no mechanical check catches by inspecting the file**, because there is nothing wrong with the file except its subject.

The cause is a two-part interaction that is individually reasonable on both sides:

1. **Jellyfin only ever fills an image slot that is EMPTY.** An ordinary refresh — including the recursive per-item refresh `media_doctor` fires, and including a full library scan — never *replaces* an image it already has. Only `replaceAllImages=true` does.
2. **Re-identifying a series does not replace its artwork.** `POST /Items/RemoteSearch/Apply/{id}` was called with `replaceAllImages=false`, deliberately: for the case it was written for — a series carrying **no** provider ids, whose artwork had been supplied locally — keeping the existing artwork is correct. But the *other* reason to call it is to correct a **wrong** match, and there keeping the artwork pins the wrong show's face on permanently.

So the moment a mis-identified show is repaired, its text heals and its pictures freeze. The repair looks like a complete success.



**Two things had to change, because the artwork breaks in two unrelated ways.**

**1. Detect the cause: `artwork_identity_stale`.** `media_doctor` now records each series' `Tmdb`/`Tvdb`/`Imdb` ids (`state/doctor_state.json`, key `pids`) and flags any id that was **set and then changed** — a re-match. Deliberately *not* absent→set, which is the legitimate fill-in case above whose local artwork must be kept. The heal is an images-only recursive refresh (`metadataRefreshMode=None`, `imageRefreshMode=FullRefresh`, `replaceAllImages=true`), so the freshly-corrected text is not disturbed and no `ffprobe` is provoked. The identity is recorded **only after** the refresh succeeds, so a failed heal retries next cycle instead of being silently forgotten; and `pids` survives the episode-count ladder reset, or dropping an episode would erase the evidence a re-match ever happened.

**2. Detect the shape: `artwork_bogus`.** The identity check is blind to art that was always wrong, and to the provider's own fallbacks — so, independently, an episode image is not a still if it is **portrait**, or **byte-identical to ≥3 siblings**, or **byte-identical to the series/season poster**. Each is re-adopted individually through `RemoteImages/Download`, ranked **landscape-first and aspect-checked**, because TMDB will hand back a portrait season poster as an episode `Primary` — which is precisely how season 4 got 24 copies of one poster, and why rung 1 alone is not enough (a full-replace refresh happily re-installs the same fallback). If the provider genuinely has no still, the existing image is **left alone** rather than replaced with a poster.

`RemoteSearch/Apply` also gained a `replace_images` flag, and the escalation prompt now tells the AI fixer that correcting an identity means replacing the artwork **and verifying it by eye** — a same-resolution, well-formed JPEG of the wrong series passes every mechanical check there is.

> **Two traps found while building the check.** (1) `Season */*-thumb.jpg` also matches the macOS **AppleDouble** `._<name>-thumb.jpg` that appears beside every file written through the mount. Those are all exactly 4096 bytes and byte-identical, so they read as "one image smeared across the whole season" — a false positive on **every** show Jellyfin has just written artwork for. Skip dotfiles; `sweep_junk` deletes them each pass but the detector must not be fooled in the window before it runs, and must never try to re-adopt one. (2) The check runs over the whole library, so it must never read an image in full: dimensions come from a **header-only** read, and MD5 is computed **only** for files whose size already collides with another candidate. Verdicts are cached per show in `state/doctor_art_cache.json` against a (name, size, mtime) signature, so a settled library re-verifies for the cost of one `stat` per file.
>
> **Auditing by hand:** portrait episode images and repeated-image seasons are found by dimensions and hashes alone (`_scan_episode_art` is the reference), but **wrong-show artwork is only detectable by looking at it.** Open `folder.jpg` and two or three `-thumb.jpg` files and confirm they depict the show. A library-wide sweep of 234 shows found 4 with structurally bogus episode art (*Shaman King – Flowers* 13, *Classroom of the Elite* 11, *Grimoire of Zero* 1, *That Time I Got Reincarnated as a Slime* 1) — all auto-fixed on the next pass — plus the one wrong-show case, which no amount of arithmetic would have surfaced.

### Tailscale is an unsupervised single point of failure — `tailscale_watchdog.py`

Tailscale has no launchd supervision: the Mac app's network extension is started by the GUI app, so when it dies **nothing brings it back**. That silently halts two things at once — qBittorrent is bound to the 100.x CGNAT address, so Torrent-Ingest refuses to start or continue *any* download (`Tailscale down; not starting/continuing downloads. Idling.`, which is correct — it doesn't leak — but it never recovers), and every MEGA transfer loses its exit node.

`scripts/tailscale_watchdog.py` (`com.mikeyferguson.tailscalewatchdog`, `KeepAlive`) closes it. Health is the **same env-independent test Torrent-Ingest uses** — is a `100.64.0.0/10` CGNAT address bound to an interface, read from `/sbin/ifconfig` — deliberately *not* `tailscale status`, whose socket the CLI can't reach under launchd's stripped environment, and which doesn't test the thing qBittorrent actually depends on. Recovery escalates cheapest-first with a grace period per step (re-auth takes tens of seconds, so a single post-fix check would report failure while recovery is working): `open -a Tailscale` → `tailscale up` → quit + relaunch. A `TS_WATCHDOG_FAIL_STREAK` (3 polls ≈ 90 s) debounce keeps a brief reconfigure from triggering an app restart — which would cause the very outage it guards against.

> **The exit-node check must be debounced and rotation-aware, or the watchdog fights the rotator.** `media_sync`/`mediafs` rotate the exit node constantly and there is a window mid-switch where no node reads as `selected`. Re-selecting on that single observation would pin a node the rotator just moved off — and each `--exit-node` set resets every TCP connection on the machine (the same hazard `ROTATE_MIN_INTERVAL_SEC` exists for), killing in-flight AI calls. So `ensure_exit_node()` acts only after a sustained streak **and** only when `ROTATE_STAMP_FILE` shows no rotation was claimed within `ROTATE_MIN_INTERVAL_SEC`. Selection routes through `vpn.get_exit_nodes()` so the `BLOCKED_EXIT_COUNTRIES` blocklist is honoured.

#### A tunnel that is up is not a tunnel that works — `ensure_traffic_flows()`

A bound CGNAT address and a selected exit node are each necessary and together still not sufficient. **A Mullvad exit node can go dead while remaining selected and reading as `active`** — the fingerprint in `tailscale status` is *tx climbing against rx frozen at zero*. Every local signal stays green while the box has no internet at all: no DNS, no route, torrents stalled, MEGA stalled. Nothing downstream notices, because everything downstream only sees timeouts.

So `ensure_traffic_flows()` probes whether traffic actually leaves the machine, and the probe targets are load-bearing:

- **Reached through the exit node.** `split_tunnel_anthropic.sh` pins Anthropic (`160.79.104.0/21`) and every Google netblock to the physical gateway, so probing `8.8.8.8` — or anything Anthropic — would succeed over the direct route and report a dead node healthy. `TS_WATCHDOG_PROBE_IPS` is Cloudflare and Quad9, which are on neither list.
- **Addressed by IP, never a hostname.** DNS is one of the things that dies with the exit node, so a hostname probe cannot tell "no exit node" from "no resolver".
- **Two providers**, so one operator's outage is not read as a dead tunnel.

Repair is rotation and nothing else: if the *tunnel* were broken the address check would already have caught it, so here the tunnel is fine and the node is not, and the only fix is a different node. Each successive round moves one further along the list. Rotation goes through `vpn.rotate_exit_node()` so the country blocklist and the cross-process rate limit both still apply — this must not become a second, competing rotator. `TS_WATCHDOG_PROBE_FAIL_STREAK` (4 polls) is deliberately higher than the address streak: mid-rotation the path is legitimately dead for a few seconds, and a watchdog that fired on that would chase the rotator around the node list forever.

> Without this the only recovery was luck. When a node died on 2026-08-14 the box lost all routing for minutes and the watchdog logged nothing, because the CGNAT address never went away; it came back only because Media-Syncer's transfer layer independently rotated on its own failures. On an idle box — nothing transferring, nothing to fail — that rotation never comes at all.

### Capacity manages itself: automatic MEGA account provisioning (`scripts/mega_accounts.py`)

When the pool's **placeable** free space — each account capped at the 20 GB free tier (never trust promo space above that) and reduced by the per-claim `REMOTE_FILL_MARGIN_BYTES`, with excluded remotes dropped — falls below `POOL_LOW_FREE_BYTES` (200 GB), the daemon creates new free accounts automatically: `megatools reg` → read the confirmation email over IMAP → verify → prove login → append the account to the untracked `rclone.conf` (password rclone-obscured, **never** session tokens) → publish it atomically to `~/.config/rclone/rclone.conf`. New accounts are named `automega<n>` with the email alias `<base>+automega<n>@…` (Gmail/iCloud `+`-aliases all land in the one base inbox). Measuring *placeable* rather than *aggregate* free is what stops the pool stalling while it still looks empty — see *Pool capacity provisioning* above. If the next `automega<n>` alias turns out to already be registered at MEGA (an orphan from a prior run that died before it could commit — `_next_index()` only sees `rclone.conf`, not MEGA's registry, so it can't know), registration returns `EEXIST` and the daemon **skips to the next alias** instead of aborting the batch, bounded by `ACCOUNTS_MAX_PER_RUN` so a pathological run of orphans can't spin forever.

**It is inert (monitor-only) until an IMAP app-password is present** at `~/.config/media-syncer/email_app_password` (a Gmail **App Password** — 16 lowercase letters — *not* the account password, which can't do IMAP under 2FA; the file lives **outside the repo** and must never be committed — it bypasses 2FA and is far more sensitive than the throwaway MEGA creds in the (untracked) `rclone.conf`). Without it, the system just logs when capacity is low.

## Not getting throttled *and* not breaking the AI APIs

The exit node is **machine-global**, so it serves two masters that pull in opposite directions: MEGA wants a **rotating** IP (per-IP throttle avoidance), while the AI APIs want a **stable, supported-country** connection. Two APIs matter: **DeepSeek**, which carries the fleet's unattended calls (identify/playlist/prediction/getcomics), and **Anthropic**, which carries the interactive Claude Code session used to build and debug the fleet. Three mechanisms reconcile them:

* **Country blocklist** (`config.BLOCKED_EXIT_COUNTRIES`) — sized to **Anthropic**, the tighter of the two constraints: it supports ~195 countries, so rotation is allowed everywhere **except** the unsupported set (China, Hong Kong, Macau, Russia, Belarus, Iran, North Korea, Cuba, Syria). Routing through an unsupported country (Hong Kong was the real culprit) hard-fails the session. DeepSeek is not the binding constraint — it is served globally from CloudFront and reachable from every country this list already allows — so a list that satisfies Anthropic satisfies both. Parsed from the Mullvad hostname's country code; ~90 nodes across ~49 countries remain, so throttle avoidance stays strong.
* **Rotation throttle** (`config.ROTATE_MIN_INTERVAL_SEC`, 180 s) — switching the exit node resets **every** live TCP connection, so rotating on every failed MEGA op (hundreds per scan) is what actually kills in-flight AI requests. Actual switches are now capped to at most once per interval, coordinated across `media_sync` + `mediafs` via a shared timestamp file (`ROTATE_STAMP_FILE`). MEGA still gets fresh IPs every few minutes (backed by per-account quarantine); the connection just stops churning.
* **Opt-in split-tunnel** (`scripts/split_tunnel_anthropic.sh` + `com.mikeyferguson.splittunnel.plist`) — for near-total immunity, route the AI endpoints **around** the exit node via the physical gateway. The two are pinned differently because they are hosted differently:
  * **Anthropic** has its own dedicated network (`160.79.104.0/21`, NetName AP-2440 — *not* Cloudflare, confirmed by whois), so the whole block is pinned. Surgical and complete.
  * **DeepSeek** cannot be pinned that way: `api.deepseek.com` is a **CloudFront alias**, so it has no dedicated allocation — only shared Amazon space whose enclosing prefix carries a large fraction of the internet. Pinning that would take most of the web off the VPN to protect one API. So only what the hostname currently **resolves to** is pinned, refreshed each time the daemon re-asserts; a resolution that changes in between rides the VPN until the next assertion.

  That residual gap is tolerable in a way it was not for the streaming CLI this fleet used to run: an agent turn is a short discrete request, not one thirty-minute stream, and `ai_client` retries a failed request four times with backoff before the caller's retry budget is touched. A rotation now costs a retried turn, not a run.

  MEGA stays fully tunneled either way, so throttle avoidance is unaffected. It changes the routing table, so it needs **root** and is **not auto-installed** — activate with the one-time `sudo` steps at the top of the script. The rotation throttle above is the no-root default that already makes API errors rare.

  > **The installed copy is what runs at boot.** Editing the script in the repo does not change the daemon until you re-copy it: `sudo cp scripts/split_tunnel_anthropic.sh /usr/local/bin/split_tunnel_anthropic.sh`. The YouTube ingest's preflight warns when the two diverge, and the script warns about itself when it notices.

### …and not getting the YouTube session revoked (a third master)

The same script pins a **second** network around the exit node, for the YouTube ingest (`~/Developer/Media-Orchestrator/YouTube-Downloader`). Mullvad exits are commercial-VPN **datacenter** IPs, which YouTube rate-limits and `403`s far more aggressively than a residential address — each failure wasting a download wave.

(The ingest holds **no** Google credential — every tracked playlist is public — so there is no session left to revoke. The throttling hazard is the one that remains, and it is why these routes stay: a rotating datacenter exit gets rate-limited and 403'd far more than a residential IP.)

* **Google is pinnable after all**, which is why this is a route and not a workaround. Google publishes its netblocks at `https://www.gstatic.com/ipranges/goog.json` — **99 IPv4 prefixes**, regenerated daily. Few enough for static routes and authoritative, unlike a `dig` snapshot that goes stale. The daemon caches the file under `/usr/local/share/split-tunnel/`, refreshes it at most daily, and **promotes a download only if it parses and yields prefixes** — so a captive portal or a truncated fetch can never replace a good cache, and a failed fetch keeps the existing routes rather than silently dropping YouTube back onto the tunnel.
* **MEGA and torrents are unaffected.** MEGA is on its own infra, so avoidance is not weakened; torrents keep exiting through Mullvad.
* **The tradeoff, plainly:** the ISP sees YouTube traffic as YouTube traffic. Identical concession to the Anthropic one, and a different risk class from torrenting.
* **IPv6 is deliberately not pinned.** Pinning v6 needs the physical interface's v6 router, whose discovery Tailscale owns, and a half-working v6 route is *worse than none* — v4 going direct while v6 still tunnels would silently defeat the whole thing. The leak is closed at the other end instead: the YouTube ingest passes `--force-ipv4` on every `yt-dlp` call (`ytconfig.FORCE_IPV4`), so it can never reach Google over v6 and slip back out through the exit node.
* **Preview before installing:** `bash scripts/split_tunnel_anthropic.sh --dry-run` prints the gateway and the prefix counts, changes nothing, and needs no root. To check whether it is currently in force, `route -n get 142.250.72.14` — a `utun*` interface means YouTube is still exiting via the VPN (the YouTube repo's `--status` reports this line too).
* **It waits up to 60s for a DHCP gateway at boot.** `RunAtLoad` on a system daemon fires before DHCP has necessarily handed out a router, and the original behaviour of exiting on the first miss left *nothing* pinned until `StartInterval` fired 30 minutes later. The YouTube ingest's LaunchAgent starts at **login**, comfortably inside that window, so it would have authenticated through the exit node and risked the session. Polling covers a normal boot; the 30-minute re-assert and the ingest's own egress gate are the backstops.

> **This daemon is a hard dependency of another repo, and the coupling is invisible from here.**
> `~/Developer/Media-Orchestrator/YouTube-Downloader` **refuses to run a cycle at all** while Google egresses
> through the tunnel (`REQUIRE_DIRECT_EGRESS`), because authenticating a YouTube session from
> a rotating Mullvad datacenter IP does not get captcha'd — it gets the session **revoked**,
> wasting download waves on a throttled datacenter IP. So:
>
> * Editing `scripts/split_tunnel_anthropic.sh` here changes nothing until the copy at
>   `/usr/local/bin/split_tunnel_anthropic.sh` is replaced with `sudo`. A **stale installed
>   copy** is the single most likely cause of "the YouTube ingest silently does nothing," and
>   because it needs `sudo` it is the step most often skipped after a change.
>   **This is now self-detecting from both sides:** the installed script compares itself to
>   the repo copy on every run and logs the exact `sudo cp` needed (see
>   `/tmp/split_tunnel.log`), and the YouTube ingest's `--preflight` reports the same
>   divergence as a warning. Deliberately a warning, not an error: stale routes that are
>   currently up still work, so nothing is broken today — it bites at the next boot.
> * Removing or disabling this daemon does not degrade the YouTube ingest gracefully — it
>   stops it, deliberately and quietly (it logs why each cycle). That is the intended
>   trade: one deferred hourly cycle self-heals, a wave burned against a throttled exit
>   does not.
> * Only **IPv4** is pinned here. The IPv6 half is closed in that repo instead, by forcing
>   IPv4 on every `yt-dlp` call. Changing either side alone reopens the leak.

### The config pull must never go through `repeat_command`

`repeat_command` judges success by an **empty stderr**, and `git pull` writes to stderr
*even when it succeeds*. The explicit `origin main` refspec guarantees it, because naming a
refspec always prints the fetch banner:

```
From https://github.com/Pirate-Hunter-Zoro/Media-Orchestrator
 * branch            main       -> FETCH_HEAD
```

That is **102 bytes of stderr on a pull whose stdout is a contented `Already up to date.`**
Judged by the empty-stderr rule, every successful pull reads as a failure and burns the
full `MAX_DOWNLOAD_TRIES` budget — **three exit-node rotations per cycle**, spent on a
command that just succeeded. Rotation is the expensive operation this repo throttles
everywhere else (`ROTATE_MIN_INTERVAL_SEC`).

Note the interaction, because it constrains any future change here: the explicit `origin
main` exists to dodge the `Cannot fast-forward to multiple branches` abort, and an explicit
refspec is precisely what makes the banner unconditional. The two cannot both be avoided.

`git_pull()` therefore uses `run_command` with a content-based check
(`is_git_pull_failure`) — the same rule the rclone `copyto` transfers follow, for the same
reason: **a command that is chatty on success cannot be judged by whether it spoke, only by
*what* it said.** It matches real failure vocabulary — `fatal:`, `error:`,
resolver/auth/connection refusals, and `run_command`'s own `timeout` sentinel — and is
bounded by `GIT_PULL_TIMEOUT_SEC` (120 s), since the pull is best-effort and a cycle must
never block on it; the next cycle is the retry budget.

This class of bug breaks nothing, which is why it survives: the pull succeeds and config
propagates fine. It costs rotations and fills the log with errors for a healthy operation,
which is its own kind of damage — it trains you to ignore the log.

### Failure mode: the split-tunnel daemon dies on `$HOME` and the routes only *look* fine

A **root LaunchDaemon gets a minimal environment from launchd, and `HOME` is not in it.**
The script runs `set -uo pipefail`, so any `$HOME`-derived path is a fatal unbound-variable
error and **every scheduled run dies on that line before pinning a single route** — while
the manual run that installed it, from a login shell, succeeds. `/tmp/split_tunnel.err`
collects one failure per 30 min `StartInterval`. Never reference `$HOME` in a root
LaunchDaemon script; derive paths explicitly.

The reason this hid so well is worth internalising, because every obvious check says the
daemon is healthy:

* `launchctl` reports the job loaded, with exit status 0.
* `netstat -rn` shows all 103 routes pinned to the physical gateway.
* `route -n get 142.250.72.14` returns `en0`, so the documented "is it in force?" check
  passes.
* The YouTube ingest's `REQUIRE_DIRECT_EGRESS` preflight passes, so it keeps running.

All true, and all irrelevant: **routes already in the kernel table are not removed by the
daemon failing.** They persist until a reboot or a Tailscale route rewrite. The daemon's
entire purpose is to *re-assert* them when that happens — so the failure is invisible right
up to the moment it matters, and the symptom then appears as the YouTube session revocations
this split tunnel exists to prevent.

The staleness check that caused this was itself added to catch a *different* silent
divergence. Both instincts were right; the bug was making a diagnostic able to abort the
work it was diagnosing. It now resolves the console user's home when `HOME` is absent, and a
home it cannot resolve **skips the check** instead of killing the run.

**Checking `.err`, not just `.log`, is the lesson.** A daemon writing nothing to stdout for
hours reads as "quiet, nothing to do" and is indistinguishable from "dying on line 66 every
time" unless you look at stderr, whose mtime is the tell.

### Jellyfin serving fixes worth remembering

* **Missing cover art after a bad scan window.** If Jellyfin ever scans while the mount is only half-populated (e.g. an SSD-only window before drives/pool are merged), it can drop image associations and even episodes for the affected titles. Episodes come back with a forced recursive item refresh. **Covers are trickier: a full metadata refresh makes Jellyfin `ffprobe` every video, which on pool/slow media throws socket errors and stalls the whole refresh** — so covers never re-attach. The reliable fix is to bypass the refresh entirely and push the local poster straight in: `POST /Items/{id}/Images/Primary` with the `-poster.jpg` bytes (base64). For a title with **no** local poster (or wrong metadata like a bad TMDB id — *Deadman Wonderland* was tagged as *The Forbidden Kingdom*, tmdb 45613), use `POST /Items/RemoteSearch/Series` + `/Items/RemoteSearch/Apply/{id}` to re-identify against TMDB (which is reachable through an allowed exit node).
* **Turn chapter-image extraction OFF** (Movies + Shows library `options.xml`, like trickplay). It reads every media file during a scan/refresh — which hydrates pool content and clogs refreshes — for thumbnails the virtual library shouldn't be generating.

### Drives as a cycling cache, deduped, with an upload-verified delete guard

External drives are now a managed cache, not just fill-once storage:

* **Cycling (safe eviction):** when a predicted file doesn't fit on a drive, `predownload._evict_drive()` deletes the drive's **coldest** media that is (a) proven uploaded, (b) not in the predicted set, and (c) not read recently — freeing room while the file stays served from the pool. So a drive behaves like the SSD cache: hot/predicted content in, cold content out.
* **THE delete guard (`_is_uploaded`):** *nothing* is ever evicted unless the inventory proves it is on a remote with a matching size. An un-uploaded original is **never** deleted for space — it waits until it's backed up, then becomes eligible. This is what lets eviction run on any drive immediately, with no risk and no manual "wait until the backlog finishes" step.
* **No duplicate downloads:** every download path checks all local locations first — the SSD-cache prefetch skips files already on any drive (`_drive_resident_rels`); `fill_drives` skips files already in the SSD cache or on another drive; `tier.ensure_cached` skips files already on a drive (`_present_on_a_drive`). A file is stored locally at most once across the Mac SSD and every drive.

### Backing-store naming

The SSD library root and every external drive use a **`Media/`** folder (`~/Media`, `/Volumes/<drive>/Media`) holding the `Shows/Movies/Comics` tree; discovery prefers `Media/`, with `MediaStore/` and the drive root kept as back-compat. The browsable mount stays `~/MediaLibrary`. A drive is marked with a `.media-library` file; drop a `.no-media-library` file on any drive you do NOT want auto-used/organized.

### Unplug resilience

Unplugging/replugging a drive is a non-event: `mediafs` re-discovers drives every 60 s and each drive access is exception-guarded, so a removed drive's files fall back to pool serving within ~60 s and return to local serving when replugged. The sync/pre-download/drive-ingest daemons all tolerate a missing drive (skip and retry). The only visible effect is that a file *actively playing from that drive* at the instant of removal will stop and must be replayed (then it streams from the pool).
