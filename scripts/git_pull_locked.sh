#!/bin/bash
# Serialized `git pull` for the ONE working tree every daemon shares.
#
# Why this exists: every run_*.sh launcher pulls before exec'ing its daemon, and startup
# bootstraps the agents about a second apart -- so many launchers ran `git pull` against
# the same working tree at once. Concurrent pulls each append a mergeable line to the
# shared .git/FETCH_HEAD, so the next one to read it sees two candidates for 'main' and
# dies with
#     fatal: Cannot fast-forward to multiple branches.
# The launcher then shrugged ("using local files") and started the daemon on stale code.
# One such interleave got far enough that git asked for `git reset --hard` to recover the
# tree -- see the GetComics.log entries from 2026-08-02.
#
# THE MONOREPO (2026-09-13). All five projects live in one repository now, so there is
# exactly ONE lock, shared by every launcher, no matter which sub-project's run script
# calls in. The repo is resolved with `git rev-parse --absolute-git-dir`, not by looking
# for `$repo/.git`: a caller hands us its own sub-directory (`Torrent-Ingest`,
# `Media-Syncer`, ...), which is inside the work tree but has no `.git` of its own.
#
# macOS ships no flock(1), so the lock is an atomic mkdir. This file is sourced, not run.

# Kill a `git pull` and everything it spawned. git delegates the network to a `git fetch`
# child and that to a `git-remote-*` grandchild, so killing the parent alone leaves the
# connection -- and the hang -- exactly where it was.
_git_pull_kill_tree() {
    local root="$1" kids grandkids
    kids="$(pgrep -P "$root" 2>/dev/null)"
    for k in $kids; do
        grandkids="$(pgrep -P "$k" 2>/dev/null)"
        for g in $grandkids; do kill -TERM "$g" 2>/dev/null || true; done
        kill -TERM "$k" 2>/dev/null || true
    done
    kill -TERM "$root" 2>/dev/null || true
    sleep 2
    for k in $kids; do
        for g in $(pgrep -P "$k" 2>/dev/null); do kill -KILL "$g" 2>/dev/null || true; done
        kill -KILL "$k" 2>/dev/null || true
    done
    kill -KILL "$root" 2>/dev/null || true
}

# git_pull_locked <dir_inside_the_repo>
#   Echoes git's output. Returns git's exit status, 0 if there is nothing to pull,
#   or 1 if the lock could not be acquired, or the pull itself timed out.
git_pull_locked() {
    local repo="$1"
    local git_bin="${GIT:-$(command -v git || true)}"
    [ -n "$git_bin" ] || return 0

    # Resolve the work tree from ANY directory inside it. The launchers pass their own
    # sub-project directory, which has no `.git`; `--absolute-git-dir` finds the master
    # repo and is also correct if `.git` is ever a file (a worktree).
    local git_dir
    git_dir="$("$git_bin" -C "$repo" rev-parse --absolute-git-dir 2>/dev/null)" || return 0
    [ -n "$git_dir" ] || return 0

    local lock="$git_dir/pull.lock"
    local waited=0
    local rc=0

    until mkdir "$lock" 2>/dev/null; do
        # Anything at this path that is NOT a directory was not put there by this function,
        # and `mkdir` can never succeed against it -- so the wait below would burn its full
        # timeout on every launch and fall back to stale code permanently. Clear it.
        if [ -e "$lock" ] && [ ! -d "$lock" ]; then
            rm -f "$lock" 2>/dev/null || true
            continue
        fi
        # Reclaim a lock orphaned by a launcher that was killed mid-pull. A pull that
        # legitimately runs longer than 5 minutes is already a broken network, not a peer.
        # `rm -rf`, not `rmdir`: a lock dir that somehow holds a file would be immortal.
        if [ -n "$(find "$lock" -maxdepth 0 -mmin +5 2>/dev/null)" ]; then
            rm -rf "$lock" 2>/dev/null || true
            continue
        fi
        sleep 1
        waited=$((waited + 1))
        if [ "$waited" -ge 120 ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] git pull: lock held 120s; using local files."
            return 1
        fi
    done

    # BOUNDED. `git pull` has no timeout of its own, and on 2026-09-01 that stopped being
    # theoretical: the VPN exit node went dead, every launcher's fetch hung on a network
    # that was never going to answer, and mediafs -- which had just crashed -- sat here for
    # 36 MINUTES instead of remounting. The library was unmounted that whole time, and
    # Jellyfin, whose library path is that mount, spent it logging every title as missing.
    # A pull is best-effort code propagation. It must never be able to stop a daemon from
    # STARTING; running a cycle on local files is always the better failure.
    #
    # Two bounds, because they fail differently. The ssh options cover the common case --
    # every fleet remote is SSH, and a dead network there blocks in connect() rather than
    # returning -- so the fetch gives up in seconds. The wall clock is the backstop for a
    # hang anywhere else, and it kills the whole process TREE: git hands the network to
    # `git fetch`, which hands it to `git-remote-*`, and killing only the parent leaves the
    # grandchild holding the socket and the lock's meaning with it.
    local out
    out="$(mktemp -t gitpull)" || out="$git_dir/pull.out"
    GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3}" \
        "$git_bin" -C "$repo" pull --ff-only --no-rebase >"$out" 2>&1 &
    local pull_pid=$!
    local elapsed=0
    local timed_out=0
    while kill -0 "$pull_pid" 2>/dev/null; do
        if [ "$elapsed" -ge "${GIT_PULL_TIMEOUT_SEC:-90}" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] git pull: still running after ${elapsed}s;" \
                 "abandoning it and using local files."
            _git_pull_kill_tree "$pull_pid"
            timed_out=1
            break
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    wait "$pull_pid" 2>/dev/null
    rc=$?
    [ "$timed_out" -eq 1 ] && rc=1
    cat "$out" 2>/dev/null
    rm -f "$out" 2>/dev/null || true

    # Released explicitly rather than by an EXIT trap: every caller ends in `exec`, which
    # replaces the shell and would never fire the trap.
    rm -rf "$lock" 2>/dev/null || true
    return "$rc"
}
