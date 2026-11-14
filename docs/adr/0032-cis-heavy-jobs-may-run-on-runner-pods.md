---
id: "0032"
title: CI's two heavy jobs may run on runner pods, and the repository stays public
status: Accepted
date: 2026-11-13
governs:
  - "hosts/components/github-checks/templates/github-ruleset-drift.sh.j2#a fork's pull request waits for approval"
---

# ADR-0032: CI's two heavy jobs may run on runner pods, and the repository stays public

## Status

Accepted. It supersedes
[ADR-0018](0018-the-repository-stays-public-and-ci-stays-hosted.md), which kept every job on
GitHub-hosted runners. The half of ADR-0018 that keeps the repository public stands, and this
record restates it.

## Context

**The cost that forced the question is a queue, not a bill.** ADR-0018 weighed a runner against
the Actions bill, and a public repository has none. That is still true. What a landing waits on
is GitHub's limit on concurrent hosted jobs: with several sessions landing at once, a run's jobs
wait for a free runner. [CI throughput](../ci-throughput.md) holds the measurements. At the time
of this record, `hooks` and the `pytest` shards were 1,243 of one full run's 1,424
runner-seconds.

**A test on the host cannot stand in for a hosted run.** The first plan swept each tree with a
script on daniel-box and compared its verdict with the hosted one. In 19 replays of hosted-red
runs the sweep read green for four separate causes: the host's locale, tools the host has
installed, a step only the workflow runs, and files only the host has. A wrong green is silent.
The stand-in for a hosted run therefore has to be the same workflow on a real runner, which
fails where the hosted job fails. The operator ruled on 2026-11-13 that such a runner may run in
the cluster, in a pod.

**ADR-0018's objection was to a runner on a host.** A host process runs a job beside the age key
that decrypts `secrets/secrets.yml`, a `gh` token that can administer the repository, and a
cluster node's own credentials. A pod can be denied all three, and the API server can enforce
the denial.

**The other two options stay closed.** A runner as a host process is what ADR-0018 refused, and
nothing about a host has changed. A private repository removes fork pull requests, but GitHub
does not enforce rulesets on a private repository on the free plan, which opened the merge gate
for nine hours on 2026-09-21 (#3610), and code scanning needs a paid plan there.

**A pod adds a way to start code on a node, not code the nodes do not already run.** Every tree
a runner pod tests is a branch of this repository. Agent sessions run the same hooks and the same
test suite on daniel-box and daniel-server, as a user with a home directory and a `gh` login
([ADR-0027](0027-fanout-batches-stay-on-host-cgroups.md)). The question a pod raises is
therefore who can start a job on one.

**Two repository settings answer that, and both can lapse.** The interaction limit
`collaborators_only` stops anyone outside the repository opening a pull request at all, and
GitHub expires it after six months. The fork approval policy makes a fork's pull request wait for
a collaborator's approval before any workflow runs. Neither setting is in the tree.

## Decision

The repository stays public. `hooks` and `pytest` may run on a self-hosted runner, and every
other job stays on GitHub-hosted runners. A runner exists only under these conditions:

1. **It is a pod, never a host process.** Its namespace enforces the `restricted` Pod Security
   standard, so the API server refuses a pod with a host path, host networking or added
   privilege. The pod mounts no ServiceAccount token and no Secret rendered from
   `secrets/secrets.yml`.
1. **It runs one job.** The pod is deleted after its job, so no job reads what an earlier one
   left.
1. **Only a collaborator can start a job on it.** The interaction limit stays
   `collaborators_only` and the fork approval policy stays `all_external_contributors`. The
   `github-checks` component re-applies the first daily and alerts on the second.
1. **It runs only jobs that hold no secret.** `hooks` and `pytest` read no repository secret, and
   their token is read-only.
1. **No credential that can administer the repository is stored in the cluster.** The credential
   that registers a runner stays on daniel-box, where the token that lands a PR already is.

## Consequences

**A job in a pod can reach the LAN.** Egress NetworkPolicies are not enforced on this cluster
([ADR-0009](0009-networkpolicy-default-deny-ingress.md)), so the pod's network is the node's.
A job can reach every LAN service that asks for no login. The agent sessions' own test runs have
the same reach.

**A container escape lands on a production node.** The `restricted` standard removes the
configured ways out of a container and leaves the kernel's. This is the cost the decision
accepts, and condition 3 is what bounds it: the code is a collaborator's.

**A dependency bump runs in a pod once it is on a branch a collaborator tests.** It also runs in
the next agent session's `uv run pytest` on the host, with more reach. The pod does not add this
exposure and does not remove it.

**The suite's Docker tests stay hosted.** A `restricted` pod has no Docker daemon. Those tests
move to a job of their own before a pod's verdict gates anything.

**The pods' memory is outside the agent fleet's bound.** A pod lives under `kubepods`, and the
fleet cap ADR-0027 describes bounds `user.slice`. The runner role sets its own limit, and
[CI throughput](../ci-throughput.md) records the measurement that sizes it.

**The cluster gets a second write path.** The launcher on daniel-box creates each runner pod
through a ServiceAccount that can write Jobs in the runner namespace and nothing else. Flux
stays the only write path to every other workload. The namespace's Pod Security standard and a
ResourceQuota bound what that ServiceAccount can start.

**A pull-request run cannot use a pod.** A runner exists only for a run the launcher
dispatched. GitHub's runner controller would start a pod for any queued job, and it needs the
credential condition 5 refuses.

**The hosted pool stays a working path.** A hosted runner still runs every job when no pod
answers. A lapse of either setting in condition 3 is a reason to route every job back to the
hosted pool until the setting is restored.

**The `workflow-runners-are-pinned` row keeps rejecting the `self-hosted` label** until the
slice that adds the runner role. That slice changes the row to accept the one `runs-on`
expression the routing needs and nothing else.

**A fork's pull request still runs on GitHub's machines with a read-only token**, after a
collaborator approves it, as under ADR-0018.

**Reversing this means deleting the runner role, and a new record.** If the repository goes
private, the free plan stops enforcing the rulesets, which is a larger failure than the one this
record manages.

## Governs

The check that reads the fork approval policy is in
`hosts/components/github-checks/templates/github-ruleset-drift.sh.j2`. The `# DECIDED:` marker
there opens "a fork's pull request waits for approval" and cites this record.
`hosts/tests/test_github_ruleset_drift.py` holds its accepting and rejecting cases. The same
component's `github-interaction-limit` script re-applies the interaction limit. Conditions 1, 2
and 5 have no line in the tree until the runner role exists, and that role's markers join this
list in the change that adds it.
