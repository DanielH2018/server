#!/usr/bin/env bash
# The daniel-server half of the staging gate. Two arguments: the SHA under test, and the
# comma-separated deploy tags.
#
# ONE CALLER. It is exec'd from the gate's checkout by the pre-deployed dispatcher behind the
# restricted ssh key (roles/setup/hypervisor/templates/staging-gate-dispatch.sh.j2). Nothing
# invokes it directly and nothing pipes it any more.
#
# staging_gate.py used to send this file over ssh to `bash -s`. That gave anything able to
# invoke the gate a full shell on this host (2026-08-29 review M-3), and a forced command alone
# would not have closed it: ssh forwards stdin regardless of `command=`, so a far side still
# reading stdin executes whatever the caller pipes. The fix is that the far side reads a request
# — an operation name and arguments — and never a script body.
#
# WHY THE BODY IS A FUNCTION. This file is executed from disk, and it fast-forwards the very
# checkout it lives in. bash reads a script by byte offset as it goes, so a `git merge --ff-only`
# that rewrites this file mid-run would resume at a meaningless offset. Wrapping everything in
# `main` and calling it on the last line makes bash parse the whole body before any of it runs,
# so the rewrite cannot reach the code still to execute. Piping was immune to this for free,
# because the body arrived on stdin rather than from the file being rewritten; executing from
# disk is not. Do not unwrap this.
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

fail_prep() {
  echo "staging-gate: prep failed: $1" >&2
  exit "$PREP_FAILED"
}

main() {
  local SHA="${1:?usage: staging_gate_remote.sh <sha> <tags>}"
  local TAGS="${2:?usage: staging_gate_remote.sh <sha> <tags>}"

  cd "$REPO" || fail_prep "no checkout at $REPO — run initial_setup.yml --tags hypervisor"

  # One gate run at a time against that one tree. The timer's run and an operator driving
  # staging_gate.py by hand would otherwise interleave a fetch, a merge and a deploy on a tree
  # each believes it pinned. Contention is a PREP failure, not a verdict: this run learned
  # nothing about the SHA.
  #
  # A DIFFERENT lock from /var/lock/server-git-tree.lock, which deploy.sh takes below. flock
  # attaches to the open file description rather than to the process, so a second `exec 9>` on
  # the same path conflicts with the first even inside one process tree — which is also why the
  # dispatcher does not take this lock before exec'ing here.
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
  #
  # --skip-staleness-check is CORRECT here and is not a bypass. deploy.sh refuses a tree behind
  # origin/master because, on a production host, a stale tree renders stale templates and reverts
  # live config while every repo-side check reads green. This tree is behind origin/master by
  # construction and on purpose: it is pinned to the SHA under test, which is the SHA the tick
  # will deploy to prod (gitops_deploy.py resolves `origin` once, ff-merges to it, asks staging
  # about it, and deploys it — see main()'s `if cs.k8s_deploy:` block). A merge landing during the
  # run moves the tip but does NOT change what this tick deploys, so refusing on that basis
  # answers a question nobody asked.
  #
  # Before this flag, any merge inside the run's window turned a good change into exit 4, which
  # classify() maps to NO_VERDICT. Two of four hand-runs on 2026-08-29 died that way, and every
  # one of them feeds the false-failure rate slice 4's entry condition is waiting on. See
  # docs/staging-phase-c.md, Decision 4.
  ./scripts/deploy.sh --tags "$TAGS" -e target=daniel-stage --skip-staleness-check
}

main "$@"
