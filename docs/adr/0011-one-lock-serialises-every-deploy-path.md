---
id: "0011"
title: One lock serialises every path that writes the git tree
status: Accepted
date: 2026-08-23
governs:
  - ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2:77
---

# ADR-0011: One lock serialises every path that writes the git tree

## Status

Accepted. The exit-code half was settled on 2026-08-23.

## Context

Several things deploy from the same checkout: the GitOps timer every 30 minutes, an
operator running `./scripts/deploy.sh`, the weekly secret-rotation cron, and the twice-daily
docs refresh. Several Claude sessions can be working the repo at once, each in its own
worktree but all deploying from the one primary checkout.

Every deploy renders its templates from that tree, and the GitOps deployer rewrites the tree
mid-run with a `git pull`. So two overlapping runs are not merely racing for the cluster —
one can be rendering from a tree the other is changing underneath it.

## Decision

A single lock, `/var/lock/server-git-tree.lock`, is taken by every path that reads or writes
the git tree: `scripts/deploy.sh`, `gitops-deploy.service`, the secret-rotate cron and the
docs refresh. It guards the tree, not the cluster, which is why a deploy targeting the Pi
takes it too.

**Contention exits 75 and the systemd unit succeeds.** It is a resume point, not a failure.

## Consequences

**Exit 75 means the lock stayed busy and nothing was deployed.** Callers retry rather than
treat it as an error. `scripts/deploy.sh` uses the same contract.

**Treating contention as failure paged for a week.** Until 2026-08-23 a timeout exited 1, so
every long operator deploy — roughly 20 minutes, holding this same lock — made the timer's
run page through `OnFailure`. Seven such ticks in seven days, each logging nothing at all,
because `flock` died before the deployer started.

**Raising the timeout cannot fix that**, which is why the exit code had to change instead.
The unit's own worst case is 2220 seconds against a 2700-second `TimeoutStartSec`, so the
largest legal `-w` is 480 seconds — still far short of a routine 20-minute deploy.
`test_gitops_discord_contract.py:458` asserts that bound and goes red if someone tries.

**Contention is not silent starvation.** The `flock` path never writes `last_run`, so
contention outlasting `GITOPS_MAX_AGE_S` still pages through the GitOps-Alive monitor.

**`--check` runs unlocked** because it mutates nothing, and so does `--dry-run`.

## Governs

`ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2:77` — the marker
recording that contention exits 75 and the unit succeeds.
