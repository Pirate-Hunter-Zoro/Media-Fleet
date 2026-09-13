# Open-Code-Doctor

Unattended maintenance for the machine's developer tooling. Two daemons:

| daemon | cadence | what it keeps healthy |
|--------|---------|-----------------------|
| **opencode doctor** (`doctor.py`) | every minute, while opencode is idle | the `opencode` install and the `coder` command |
| **package upgrader** (`brew_upgrade.py`) | once a day | every Homebrew formula, plus npm's globals |

They live together on purpose: a `brew upgrade` of node relinks the prefix out from
under the globally-installed `opencode-ai`, and the doctor repairs exactly that, within
a minute, on its own.

## The opencode doctor

Keeps the `opencode` install and the `coder` command healthy, automatically.

The `coder` command used to break almost daily because it was wired through
`npx --yes opencode-ai --auto` — an alias that re-resolves the npm package on every
invocation and dies on a stale cache, a stray local `package.json`, or a freshly-swapped
platform binary. This repo replaces that fragility with a tiny doctor that runs every
minute (whenever opencode is **not** running) and repairs four things, idempotently:

1. **The `opencode` binary exists and runs.** If not, it does a clean global reinstall
   (`npm install -g opencode-ai --foreground-scripts` + postinstall) — the old
   `fix_opencode.bash` folded into code.
2. **`~/bin/coder` is a direct wrapper** that execs the real binary
   (`exec opencode --auto "$@"`) — no npx, no npm resolution, nothing to rot.
3. **No stale `alias coder=...`** survives in `~/.zshrc` / `~/.bashrc`.
4. **`~/.config/opencode/opencode.jsonc` carries `"permission": "allow"`** — permissions
   bypassed by default, so opencode acts without prompting.

The reinstall (1) is the only disruptive step and only runs while opencode is idle; the
other three are cheap and safe. When opencode is currently running, the doctor exits
silently and tries again next minute.

### Running it

```bash
./startup.sh          # install both launch agents
./cancel_doctor.sh    # stop the doctor
python3 doctor.py     # run one pass manually (no-op if opencode is running)
```

Logs: `~/Library/Logs/OpenCodeDoctor.log`.

### Why the permissions bypass is config, not a flag

`opencode --auto` auto-approves permissions, but only for that one invocation. Putting
`"permission": "allow"` in `~/.config/opencode/opencode.jsonc` makes bypass the *default*
for every launch — `coder`, `opencode`, the TUI, and headless runs alike — which is what
the doctor enforces.


## The brew upgrader

`brew upgrade` is the maintenance nobody does until something is already broken — a
year-old openssl, an rclone that predates the remote's API change, a node the global
npm packages no longer match. `brew_upgrade.py` runs it once a day, unattended, and
writes down what moved.

```bash
/usr/bin/python3 brew_upgrade.py --dry-run   # what would it upgrade? changes nothing
/usr/bin/python3 brew_upgrade.py --force     # upgrade now, ignoring the schedule
./cancel_brew_upgrade.sh                     # stop the daemon
```

Logs: `~/Library/Logs/BrewUpgrade.log`. State: `state/brew_upgrade.json` (untracked).

### Once a day, but scheduled hourly

launchd wakes it every hour and `state/brew_upgrade.json` decides whether there is
anything to do; 23 of those 24 wake-ups cost a `stat()` and exit silently.

A plain `StartCalendarInterval` at 04:00 would be the obvious way to write this, and on
a laptop it is the wrong one: if the machine is asleep, off, or on battery at 04:00, the
day is simply lost and nothing retries until tomorrow. Hourly plus a gate is still one
upgrade a day, and it catches up the moment the machine is awake and on power. The gate
is four rules:

* **not more often than every 20 hours** (20, not 24, so a run that lands at 04:10 is not
  pushed a little later every day until it falls out of the window);
* **only during the quiet hours, 03:00–07:59** — unless nothing has succeeded for 48
  hours, at which point it stops being fussy and takes the next chance it gets. That
  clock runs from the last success, or from the install on a box that has never had one,
  so a fresh install waits for the coming window instead of upgrading at whatever hour
  someone happened to install it — which is the one hour they are certainly sitting at
  the machine;
* **not on battery below 40%** — a large upgrade that loses power mid-link leaves a
  half-installed Cellar;
* **one at a time**, via a lock file that is reclaimed if a run dies holding it.

### Formulae always; the fleet's casks never

Upgrading a formula swaps a binary that running daemons already hold open. That is safe:
the running process keeps the inode it started with and picks the new one up at its next
restart, which is why `rclone` and `tailscale` upgrade under a live sync without
incident.

Casks are applications, and three of them are load-bearing for the fleet:

| held cask | why it is not upgraded at 04:00 |
|-----------|-------------------------------|
| `miniconda` | hosts the conda env every Torrent-Ingest daemon's python lives in |
| `fuse-t` | the FUSE layer the mediafs mount rides on |
| `jellyfin` | the media server `librarysupervisor` starts and stops |

Those are *reported* as outdated in the log and upgraded by hand, in a window someone is
watching. Every other cask is upgraded one at a time — one that wants an admin password
or refuses to quit a running app must not take the rest of the list down with it — and
non-greedily, so an app that self-updates is left to do so. Nothing here escalates: a
cask that needs `sudo` cannot get it from launchd, so it fails, gets logged, and waits
for a person.

### npm globals, and what is deliberately left alone

`npm update -g` runs after the brew pass and never before it: npm lives in the node brew
just replaced, and updating globals against the outgoing node links them to a prefix that
is about to move. It is the only other package manager on this box that upgrades without
a password.

Not updated, on purpose: the **conda base** (the fleet's python lives in it — same reason
the `miniconda` cask is held), **system gems**, and **`softwareupdate`**, all of which
want root that a LaunchAgent cannot get and should not have.

The run finishes with `brew autoremove` and `brew cleanup --prune=7`, which reclaims the
space the upgrade just spent while keeping a week of downloads, so a bad upgrade can
still be rolled back from the cache.

### Why /usr/bin/python3

`run_brew_upgrade.sh` execs the *system* python, not the Homebrew one. This job upgrades
Homebrew's python; the interpreter running the script should not be the file being
replaced underneath it. `brew_upgrade.py` is stdlib-only and 3.9-clean for that reason.

### Registered fleet-wide

Both agents are in `~/Developer/Media-Fleet/ship-fleet.sh` — `opencodedoctor` in `LABELS` (restarted
on every fleet deploy), `brewupgrade` in `PERIODIC` (checked for being loaded, never
kickstarted). Every scheduled job in the fleet is now in one of those two lists.

### Deploys do not trigger upgrades

`scripts/ship.sh` restarts the doctor but only *verifies* that `brewupgrade` is loaded --
it is in `PERIODIC`, not `LABELS`. `launchctl kickstart -k` fires a job immediately, and
a deploy should not be able to start a multi-gigabyte upgrade as a side effect. It does
not need to either: the launcher does its own `git pull --ff-only` before every exec, so
the next tick is already on the pushed code. `RunAtLoad` is `false` for the same reason.

## Files

| file | purpose |
|------|---------|
| `doctor.py` | the idempotent opencode check-and-repair logic (stdlib-only) |
| `run_doctor.sh` | launchd launcher (git pull, then exec the doctor) |
| `com.mikeyferguson.opencodedoctor.plist` | launchd agent, `StartInterval=60` |
| `brew_upgrade.py` | the daily `brew update && brew upgrade`, gated (stdlib-only) |
| `run_brew_upgrade.sh` | launchd launcher (git pull, then exec under `/usr/bin/python3`) |
| `com.mikeyferguson.brewupgrade.plist` | launchd agent, `StartInterval=3600`, no `RunAtLoad` |
| `startup.sh` | install both agents (add every new plist's label to its `AGENTS` list) |
| `cancel_doctor.sh` / `cancel_brew_upgrade.sh` | stop one daemon each |
| `archive/` | retired scripts kept for reference (the old nyaa surfer, MEGA ignore rules) |
