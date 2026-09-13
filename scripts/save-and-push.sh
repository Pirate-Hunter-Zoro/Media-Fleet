#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# save-and-push.sh -- commit everything in the monorepo and push it.
#
#   scripts/save-and-push.sh ["commit message"]
#
# Run at the end of a session. Prefer scripts/ship.sh (or the root
# ship-fleet.sh) when the change touches code a daemon has loaded -- a push
# alone leaves every running daemon on the old code, which is invisible from
# the outside.
#
# This is the ONE commit path for the repository. The per-project
# `scripts/save-and-push.sh` files in the sub-directories still exist for
# muscle memory; they commit the same tree and push the same remote.
#
# The commit is authored by whoever `git config user.name` says, and carries no
# trailers, no co-authors, and no attribution to any assistant. The work is the
# repository owner's; the history should say so and nothing else.
#
# Exits 0 on success or when there was simply nothing to commit. Any other exit
# means the push did not happen, and the reason is on stdout.
# ---------------------------------------------------------------------------
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || { echo "cannot enter $ROOT"; exit 1; }

MSG="${1:-fleet updates}"

# Never sit waiting on a credential prompt nobody can see: fail and say so.
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/bin/false

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "not a git repository: $ROOT"
  exit 1
}

# A clone has to opt into tracked hooks once. Do it here rather than making
# anyone remember, so the attribution stripper and the secret guard are on from
# the first commit.
if [ -d "$ROOT/.githooks" ] && [ -z "$(git config core.hooksPath || true)" ]; then
  git config core.hooksPath .githooks
  echo "enabled .githooks for this clone"
fi

git add -A || { echo "git add failed"; exit 1; }

if git diff --cached --quiet; then
  echo "nothing to commit"
  ahead="$(git rev-list --count @{upstream}..HEAD 2>/dev/null || echo 0)"
  [ "$ahead" = "0" ] && { echo "already up to date"; exit 0; }
  echo "$ahead commit(s) not yet pushed; pushing those"
else
  git commit -m "$MSG" || { echo "git commit failed"; exit 1; }
  echo "committed: $MSG"
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "committed locally; no 'origin' remote to push to"
  exit 0
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if git rev-parse --abbrev-ref '@{upstream}' >/dev/null 2>&1; then
  out="$(git push 2>&1)"
else
  out="$(git push -u origin "$branch" 2>&1)"
fi
status=$?

if [ $status -ne 0 ]; then
  echo "push failed:"
  echo "$out" | tail -5
  exit $status
fi

echo "pushed $branch to origin"
exit 0
