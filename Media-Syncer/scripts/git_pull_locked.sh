#!/bin/bash
# Thin delegator. Since the monorepo (2026-09-13) there is ONE implementation of the
# serialized pull at the repository root; five copies of it would drift (§4.106). Every
# launcher that sources this file gets the root function, which resolves the shared
# work tree itself -- the sub-project directory handed to it has no `.git` of its own.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/git_pull_locked.sh"
