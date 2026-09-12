---
id: "0017"
title: The tree lock guards the tree, and per-service locks guard the cluster
status: Accepted
date: 2026-09-11
governs:
  - "scripts/deploy.sh#the lock order is"
  - "scripts/deploy.sh#ownership is this lock"
  - "ansible/roles/setup/gitops_deploy/files/deploy_locks.py#the lock order is"
---

# ADR-0017: The tree lock guards the tree, and per-service locks guard the cluster

Amends [ADR-0011](0011-one-lock-serialises-every-deploy-path.md). ADR-0011's decision — one
lock on every path that reads or writes the git tree — still stands. What changes is how long
that lock is held and what else a deploy takes.

## Status

Accepted.

## Context

ADR-0011 says the lock guards the tree. `scripts/deploy.sh` held it for the whole playbook.

Measured over the runs since 2026-09-04, a routine deploy held
`/var/lock/server-git-tree.lock` for p50 102s, p75 146s, p90 295s. 93 of 147 routine runs paid
the fixed 60s `k8s_rollout_stabilise_seconds` pause inside that hold — up to 38% of routine lock
time — and most of the rest was `rollout status`. Neither reads the git tree. Rendering does,
and rendering is seconds.

So two landings on disjoint services queued behind each other for minutes, for work that could
not have conflicted. The cost fell on the thing the homelab is measured by: how long a merged
PR takes to reach the cluster.

Two options were rejected. Releasing the lock early without a snapshot leaves the playbook
rendering from a tree the GitOps tick can `git pull` underneath it, which is the exact hazard
ADR-0011 was written for. Giving every deploy its own full clone pays a clone per deploy and
puts a second object store on disk.

## Decision

`scripts/deploy.sh` holds the tree lock only to copy `HEAD` into a detached worktree under
`/tmp/homelab-deploy-snapshots/`, then releases it and runs the playbook against that snapshot.
Locking the cluster moves to one lock per deploy tag, `/var/lock/server-deploy-<tag>.lock`,
held across the whole playbook. A run naming no tag takes `/var/lock/server-deploy-all.lock`
exclusively; a scoped run takes it shared, so a full run and a scoped run still exclude each
other while two scoped runs proceed together. The GitOps deployer takes the same per-service
locks inside the tree lock it already holds.

## Consequences

**A snapshot is safe because rendered manifests are a function of the commit.** Every template
`src` is a tracked path, and the per-host variables the render also reads come from the same
tree. `roles/k8s/manifests` already records the commit in
`/var/lib/homelab/k8s-releases.d/<svc>.json`, so the release record names exactly what the
snapshot rendered.

**`tree_dirty` in that record is now structurally false.** It was the flag saying whether the
release came from a tree with uncommitted edits. A detached snapshot never has any, so it stops
carrying signal rather than starting to lie. What replaces it is the stronger property: the
recorded commit IS what was deployed.

**An uncommitted edit is no longer deployed.** This is the cost. A session iterating on a
template without committing used to deploy its working tree and now deploys the previous
commit, with nothing on the run saying so. `--check` and `--dry-run` stay unlocked and
un-snapshotted and still read the working tree, which is where an uncommitted edit is meant to
be exercised.

**What still serializes on the tree lock:** the snapshot step, the GitOps tick's own
fetch/fast-forward/apply, the weekly secret-rotate cron and the docs refresh. Every one of
those writes the tree.

**What no longer serializes:** two deploys of different services, and a deploy against a tick
that is only fast-forwarding. Two deploys of the SAME service still serialize, which is the
part that was ever load-bearing.

**The lock order is fixed and stated at both sites.** `all` first — shared for a scoped run,
exclusive for a full one — then each tag in sorted order. `deploy.sh` takes the tree lock,
snapshots, releases it and only then takes service locks, and never re-takes the tree lock; the
deployer takes the tree lock and holds it across its service locks. Neither order can close a
cycle, because the wrapper never waits on the tree lock while holding a service lock.

**A crashed run leaves a registered worktree, and what says a snapshot is still in use is an
advisory lock rather than a pid.** The process running the playbook holds
`<snapshot>/.deploy-owner.lock`; `deploy.sh` reaps a snapshot directory on its next run only
once `flock -n` on that file succeeds, the way it already clears a stale Ansible fact cache.
The pid in the directory name is for a human reading `ls` and decides nothing: `--detach` runs
its playbook in a background shell whose `$$` is still the parent's, and the parent exits
at once — so a pid-based reaper found a dead pid for the whole life of every detached deploy
and deleted the worktree out from under it. `scripts/dev/prune_worktrees.py` is the other
collector, and it keeps any detached worktree rather than classifying it.

**`uv` resolves its project from the working directory**, and a snapshot has no `.venv`. The
playbook run pins `UV_PROJECT_ENVIRONMENT` to the calling checkout's, so the snapshot reuses
one environment instead of building one it then deletes — and the shared, host-keyed Ansible
fact cache is not pinned to an interpreter path that disappears.

**A busy service lock defers the deployer's tick; it does not fail it.** Contention means
nothing ran, so the tick undoes its fast-forward (`git reset --hard <local>`) and lets the next
one re-evaluate the range — no `hold_sha`, no rollback, no page.
`deploy_locks.ServiceLockBusy` is its own exception type so each handler can tell it from a
playbook that failed, and `deploy_defer.for_contention` is the one place that acts on it.

**A broad apply takes `all` exclusively whatever its tags say.** `initial_setup.yml --tags
<role>` names a tag, but what it reconfigures is the host every workload runs on, so sharing
`all` with a scoped deploy the way a service deploy does would let a host-plane apply overlap a
rollout.

**The deployer's service-lock wait comes out of the phase it is waiting for.** That unit holds
the tree lock across its whole run, so it now waits for a service lock while holding it. Every
job that waits on the tree lock sizes its own wait from the sum of the deployer's four phase
timeouts, and a wait budgeted on its own would double the two k8s terms in that sum — so
`deploy_locks.locked_budget` shares one deadline between the wait and the playbook. A phase
queued behind an operator's deploy runs on what is left of its budget and holds the tree lock
for no longer than a phase that never queued. The cost is that such a phase can be cut short and
read as a failed deploy.

**Exit 77 is new**: the snapshot could not be created, and nothing was deployed. It is separate
from 76 because the two are fixed in different places — 76 is the lock file, 77 the snapshot
root or the object store.

## Governs

The `# DECIDED:` markers recording the lock order at each of the two deploy paths
(`scripts/deploy.sh`, `ansible/roles/setup/gitops_deploy/files/deploy_locks.py`), and the one
in `scripts/deploy.sh` recording that a snapshot's owner is an advisory lock rather than the
pid in its name. The anchors are the markers' own text: a line number is wrong the moment
anything above it moves, and these three moved twice while this record was being written.
