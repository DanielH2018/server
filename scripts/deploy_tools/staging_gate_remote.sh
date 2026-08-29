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

# The gate's OWN checkout, not this host's. Until 2026-08-29 (review M-2) this was
# /home/ubuntu/server, which the gate fast-forwarded to the SHA under test, unlocked, and
# never restored — so asking staging a question moved an operator's tree to an arbitrary
# commit, and a dirty tree there answered PREP_FAILED for every commit until someone noticed.
# A NO VERDICT is silent by design, so nothing would have said so.
#
# Provisioned by roles/setup/hypervisor (install.yml clones it, teardown.yml removes it).
# These two paths are duplicated from that role's defaults; ansible/tests/
# test_staging_gate_paths_agree.py pins them equal, because the copy that runs is this one.
REPO=/home/ubuntu/server-staging
LOCK=/var/lock/staging-gate.lock
SHA="${1:?usage: staging_gate_remote.sh <sha> <tags>}"
TAGS="${2:?usage: staging_gate_remote.sh <sha> <tags>}"

fail_prep() {
  echo "staging-gate: prep failed: $1" >&2
  exit "$PREP_FAILED"
}

cd "$REPO" || fail_prep "no checkout at $REPO — run initial_setup.yml --tags hypervisor"

# One gate run at a time against that one tree. The timer's run and an operator driving
# staging_gate.py by hand would otherwise interleave a fetch, a merge and a deploy on a tree
# each believes it pinned. Contention is a PREP failure, not a verdict: this run learned
# nothing about the SHA.
#
# A DIFFERENT lock from /var/lock/server-git-tree.lock, which deploy.sh takes below. flock
# re-opens the file it is given and POSIX locks are not reentrant across a fresh open, so
# sharing one here would deadlock the gate against itself for the full wait.
exec 9>"$LOCK" || fail_prep "cannot open $LOCK"
flock -n 9 || fail_prep "another staging-gate run holds $LOCK"

# A dirty tree is prep failure, not a verdict: deploy.sh renders from the working directory, so
# an uncommitted edit here would make the gate measure something other than the SHA under test.
# Now that the gate owns this tree nothing should ever dirty it, which makes this check cheap
# insurance rather than a routine outcome — and a hit here means someone edited the gate's
# checkout by hand, which is worth the loud stop.
[ -z "$(git status --porcelain)" ] || fail_prep "working tree is dirty"

git fetch --quiet || fail_prep "git fetch failed"

# `cat-file -e` rather than rev-parse: rev-parse resolves a ref-ish name that is not the commit
# we mean, and the point here is to prove this checkout HAS the object.
git cat-file -e "${SHA}^{commit}" 2>/dev/null || fail_prep "commit $SHA is not in this checkout"

# --ff-only, never checkout: a detached HEAD here would silently persist and every later gate
# run would render from it. If the SHA is not a fast-forward, that is a real divergence for an
# operator to look at, not something to force past. Moving THIS tree forward is the gate's job;
# what M-2 objected to was it moving the operator's.
git merge --ff-only "$SHA" >/dev/null || fail_prep "cannot fast-forward to $SHA"

# The verdict-bearing command. Its exit code is returned verbatim.
./scripts/deploy.sh --tags "$TAGS" -e target=daniel-stage
