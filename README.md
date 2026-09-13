# Media fleet — one repository

This is the whole fleet on one Mac: a set of launchd daemons that take a `.torrent`
dropped into `iCloud Drive/Torrents/`, download it, ask a free AI model where each file
belongs, validate that answer, and file it into a Jellyfin library on a FUSE mount
(`~/MediaLibrary`) backed by an SSD (`~/Media`) and a pool of MEGA accounts.

**Read `HANDOFF.md` first.** It is the briefing: the rules that have already cost real
damage, the current state, and what is known-broken-but-accepted. It is kept next to
this file and is updated at the end of each working session.

## Repository layout

Since 2026-09-13 this is **one repository**. Before that it was five repositories with
five remotes; they have been merged and the old `.git` directories were removed, so the
history starts here. The directory names did **not** change, and that is deliberate:
every launchd plist, `config.py`, cross-project import and runbook line addresses these
paths absolutely, and renaming a directory would silently break a daemon. The names are
the interface.

| directory | what it is | deep docs |
|---|---|---|
| `Torrent-Ingest/` | the pipeline: torrent watch → download → AI identify → validate → file; the reaper that mirrors deletions to the MEGA pool; most of the daemons | `README.md` (~3,900 lines, architecture), `OPERATING.md` (the runbook) |
| `Media-Syncer/` | FUSE mount (`mediafs`), SSD↔MEGA replication, the account pool, the deletion queue | `README.md` (~1,350 lines) |
| `Title-Scout/` | the one thing allowed to search: a title written into `find.txt` produces `.torrent` candidates the owner hand-picks | `README.md` |
| `YouTube-Downloader/` | YouTube playlist sync into the library | `README.md` |
| `Open-Code-Doctor/` | multi-machine dev hygiene (repo cleanup, brew upkeep) | `README.md` |
| `scripts/` | deploy plumbing shared by everything: `ship.sh`, `save-and-push.sh`, and the ONE `git_pull_locked.sh` every launcher sources | this file |
| `ship-fleet.sh` | the full deploy: commit, push, restart every daemon in a safe order | `HANDOFF.md` §2, `OPERATING.md` §7 |

Supporting documents at the root:

* `HANDOFF.md` — the briefing every session starts from.
* `.githooks/` — `commit-msg` strips assistant attribution; `pre-commit` refuses to
  commit MEGA session tokens.

Outside the repo, at `~/Developer/`: `.megaignore` (MEGAsync's ignore list for the sync
root, with its patterns re-pointed into `Media-Fleet/`). The old `HANDOFF-history` and
`.archive` were retired with the 2026-09-13 restructure.

## Deploying

```bash
bash ship-fleet.sh "what changed"     # commit, push, restart every daemon
bash scripts/ship.sh "what changed"   # same thing, canonical entry point
bash scripts/save-and-push.sh "msg"   # commit + push only (scripts no daemon loads)
```

Every `run_*.sh` launcher does a serialized `git pull --ff-only` before exec, so a
restart is what actually moves the running daemons onto pushed code. The pull is
best-effort and bounded — it can never stop a daemon from starting — and it is
serialized through one lock at the repository root (`scripts/git_pull_locked.sh`),
because all launchers now share one work tree and an unserialized pull corrupts
`.git/FETCH_HEAD`.

**Before any deploy:** `bash Torrent-Ingest/scripts/verify_fleet.sh` must print
`ALL CHECKS PASSED`, and `pgrep -f ai_runner.py` must be empty. `ship-fleet.sh` enforces
the second and the no-draining-reaper rule itself.

## Secrets and state

Tracked on purpose: `Media-Syncer/rclone.conf` — the MEGA account list and credentials
that `Torrent-Ingest/config.py` seeds the machine-local config from. It must contain
**no** `session_id`/`master_key` tokens (they are what the pre-commit hook blocks), and
the live tokens live in `~/.config/rclone/rclone.conf`, never here.

Ignored everywhere: `state/` (journal, library DB, decisions log), `*.log`, and any
runtime JSON/JSONL. Each project's own `.gitignore` still applies to its directory; the
root `.gitignore` covers the root.

## Everything else

The fleet's day-to-day reports land in `iCloud Drive/Torrents/` (`fleet_health.txt`,
`library_health.txt`, `fleet_doctor.txt`, `mega_free_space.txt`). When something looks
wrong, read the file on disk and check its timestamp before believing it — those sync to
other devices, and a stale copy has already sent one session chasing a fixed problem.
