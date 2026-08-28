#!/usr/bin/env bash
# The daniel-server half of the staging gate. Piped over ssh by staging_gate.py — it is never
# invoked directly, and it is deliberately NOT deployed anywhere: `ssh daniel-server 'bash -s'`
# sends the caller's own copy, so the version that runs is always the one in the tree the
# deployer is testing.
#
# Two arguments: the SHA under test, and the comma-separated deploy tags.
#
# EXIT CODE IS THE WHOLE INTERFACE. `PREP_FAILED` means the deploy never started, so staging has
# no opinion about the change; anything else is deploy.sh's own exit code passed through
# untouched, because the caller has to tell "staging rejected this" apart from "staging could not
# be asked". Do not collapse them into a bare 1.
set -uo pipefail

PREP_FAILED=70 # outside deploy.sh's vocabulary (2, 3, 4, 75) and outside 0/1

REPO=/home/ubuntu/server
SHA="${1:?usage: staging_gate_remote.sh <sha> <tags>}"
TAGS="${2:?usage: staging_gate_remote.sh <sha> <tags>}"

fail_prep() {
  echo "staging-gate: prep failed: $1" >&2
  exit "$PREP_FAILED"
}

cd "$REPO" || fail_prep "no checkout at $REPO"

# A dirty tree is prep failure, not a verdict: deploy.sh renders from the working directory, so
# an uncommitted edit here would make the gate measure something other than the SHA under test.
[ -z "$(git status --porcelain)" ] || fail_prep "working tree is dirty"

git fetch --quiet || fail_prep "git fetch failed"

# `cat-file -e` rather than rev-parse: rev-parse resolves a ref-ish name that is not the commit
# we mean, and the point here is to prove this checkout HAS the object.
git cat-file -e "${SHA}^{commit}" 2>/dev/null || fail_prep "commit $SHA is not in this checkout"

# --ff-only, never checkout: a detached HEAD here would silently persist and every later deploy
# from this host would render from it. If the SHA is not a fast-forward, that is a real
# divergence for an operator to look at, not something to force past.
git merge --ff-only "$SHA" >/dev/null || fail_prep "cannot fast-forward to $SHA"

# The verdict-bearing command. Its exit code is returned verbatim.
./scripts/deploy.sh --tags "$TAGS" -e target=daniel-stage
