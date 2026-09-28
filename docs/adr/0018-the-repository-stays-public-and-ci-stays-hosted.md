---
id: "0018"
title: The repository stays public, and CI stays on GitHub-hosted runners
status: Accepted
date: 2026-09-28
governs: []
---

# ADR-0018: The repository stays public, and CI stays on GitHub-hosted runners

## Status

Accepted.

## Context

The 2026-09-21 Actions quota exhaustion forced the question. Issue #2245 recorded the
allowance running out between 08:44 and 22:00 in one day, and the spike at
`docs/archive/self-hosted-runner-spike.md` measured what carrying this repository's CI on
`daniel-server` would cost instead. That spike concluded with a verdict it could not close:
**no-go while the repository is public, and conditional after that.** The open term is the
repository's visibility, which is the operator's call and not a measurement.

**GitHub bills Actions minutes on private repositories only.** A public repository has no
Actions bill, so a self-hosted runner saves nothing on one. The 2,000-minute monthly allowance
that ran out on 2026-09-21 is the private-repository allowance, and the repository has been
public since before this record: `gh repo view DanielH2018/server --json visibility` reports
`PUBLIC`.

**A self-hosted runner on a public repository executes fork pull-request workflows on the
runner's host.** GitHub's own documentation tells you not to do that. `daniel-server` holds the
age key that decrypts `ansible/vars/secrets.yml`, a `gh` token carrying `repo` and `workflow`
scopes, and an agent node of the production cluster. Arbitrary code on that host is a total
compromise of the homelab.

The spike examined the obvious way out of that, and it does not work. Keeping `pull_request`
jobs hosted and running only `push` and `merge_group` jobs self-hosted halves the bill rather
than removing it, and on a public repository there is no bill to halve. The two branches
therefore collapse into a single viable configuration: **private repository, every job
self-hosted.** Going private is what removes fork execution, because only collaborators can
open a pull request on a private repository.

The spike priced that configuration. Every figure below is its 2026-09-22 measurement on
`daniel-server` against `origin/master` at `52826dd8`, not a live reading:

- CI wall clock rises to 1.4x GitHub's on an idle host, and to 2.8x while two fan-out agents
  run their own `pytest` — which is the condition that holds on a landing day.
- The 4-shard `pytest` matrix has to collapse to one job, because four `-n auto` runs contend
  for the same eight cores.
- The runner needs its own systemd slice under `system.slice`, and the fan-out fleet's
  unspent margin beside its cap falls from about 3 GiB to about 0.8 GiB.

Going private also costs something the spike did not price. GitHub offers CodeQL default setup
at no cost on public repositories and requires a paid plan for code scanning on private ones,
so the arrangement ADR-0016 records would need re-deciding. Nobody has priced that, and it is
one more reason the visibility change is a larger decision than the runner it enables.

## Decision

The repository stays public, and every CI job stays on GitHub-hosted runners. No
`github_runner` role is built, and `ansible/tests/repo/test_workflow_runners_are_pinned.py`
keeps rejecting the `self-hosted` label. Secrets stay out of the tree in the SOPS and age form
ADR-0003 records, which is what makes a world-readable tree safe to publish.

## Consequences

**The Actions bill is zero, and the 2026-09-21 exhaustion cannot recur.** The allowance that
ran out applies to private repositories, so the failure that opened #2245 is not reachable from
this configuration. That is the whole of the benefit, and it is the reason the runner work is
not worth doing.

**CI keeps GitHub's wall clock, and `daniel-server` keeps its memory for the fan-out agents.**
The fleet-cap derivation in `ansible/inventory/host_vars/daniel-server.yml` stands as written,
because no runner competes with `user-1000.slice` for reclaim.

**The 4-shard `pytest` matrix keeps earning its shape.** GitHub gives every job its own
machine, so a run costs the slowest shard rather than the sum. The one-line matrix-collapse
lever the spike identified trades wall clock for billed minutes, and with no bill to cut there
is nothing on the other side of that trade — it is filed as issue #2933 rather than decided
here.

**Every commit, workflow log and issue body is world-readable, immediately and permanently.**
A secret committed in plaintext is published rather than merely leaked, which is what gitleaks
in the prek sweep and the SOPS discipline exist to prevent. This is the cost this decision
accepts, and it is paid continuously rather than once.

**A fork pull request runs on GitHub's machines with a read-only token.** Nothing a contributor
writes executes on homelab hardware, and no workflow run can reach the age key or the cluster.

**Reversing this means a new ADR, not an edit to this one.** If the repository goes private, the
follow-up is a record superseding this one plus the role issue the spike describes — the shape
is in `docs/archive/self-hosted-runner-spike.md` under *Shape, if the answer becomes go*, and
its registration-token scope claim rests on GitHub's documentation rather than on an
observation. Building the runner without the visibility change is the one combination that is
actively unsafe.

## Governs

No line in the tree enforces this. A repository's visibility is a GitHub setting, in the same
way ADR-0016's code-scanning choice is, so `governs:` is empty. The nearest thing to
enforcement is `ansible/tests/repo/test_workflow_runners_are_pinned.py`, which rejects the
`self-hosted` label — it fails by accident of its regexes rather than by referencing this
decision, so it carries no marker.
