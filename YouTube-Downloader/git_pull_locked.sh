#!/bin/bash
# Thin delegator. The serialized pull has ONE implementation at the repository root;
# copies would drift (§4.106). Every launcher that sources this file gets that function,
# which resolves the shared work tree itself -- the sub-project directory handed to it has
# no `.git` of its own.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/git_pull_locked.sh"
