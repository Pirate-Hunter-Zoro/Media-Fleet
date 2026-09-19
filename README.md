# Media-Orchestrator — one repository

This is the whole fleet on one Mac: a set of launchd daemons that take a `.torrent`
dropped into `iCloud Drive/Torrents/`, download it, ask a free AI model where each file
belongs, validate that answer, and file it into a Jellyfin library on a FUSE mount
(`~/MediaLibrary`) backed by an SSD (`~/Media`) and a pool of MEGA accounts.

**Read `HANDOFF.md` first.** It is the briefing: the rules that have already cost real
damage, the current state, and what is known-broken-but-accepted. It is kept next to
this file and is updated at the end of each working session.

## Repository layout

This is **one repository**; the five projects are directories in it. The directory names
are the interface: every launchd plist, `config.py`, cross-project import and runbook line
addresses these paths absolutely, and renaming a directory silently breaks a daemon.

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

Outside the repo, at `~/Developer/`: `.megaignore`, MEGAsync's ignore list for the sync
root, with its patterns pointing into `Media-Orchestrator/`.

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

**This repository is public: nothing tracked here may carry a credential.** The two
credential stores were moved out of the tree on 2026-09-19 and are covered by the root
`.gitignore`:

* `.env` — machine-local paths and keys (library roots, the Jellyfin API key, the
  provisioner's base email). `.env.example` is the tracked template; `fleet_env.py`
  loads it with the process environment taking precedence.
* `Media-Syncer/rclone.conf` — the MEGA account pool (`user`/`pass` per remote).
  `Media-Syncer/rclone.conf.example` is the tracked template; the live config with
  rclone's cached session tokens stays at `~/.config/rclone/rclone.conf`.

The launchd plists carry the `__JELLYFIN_API_KEY__` placeholder; `Torrent-Ingest/startup.sh`
substitutes the real value at install time. The boundary is enforced by
`Torrent-Ingest/scripts/test_no_tracked_secrets.py` (verify_fleet.sh check #54) and the
`.githooks/pre-commit` hook. `state/` (journal, library DB, decisions log), `*.log`, and
any runtime JSON/JSONL are still ignored everywhere; each project's `.gitignore` applies
to its own directory and the root file covers the root.

## Everything else

The fleet's day-to-day reports land in `iCloud Drive/Torrents/` (`fleet_health.txt`,
`library_health.txt`, `fleet_doctor.txt`, `mega_free_space.txt`). When something looks
wrong, read the file on disk and check its timestamp before believing it — those sync to
other devices, and a stale copy has already sent one session chasing a fixed problem.
