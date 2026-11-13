# CI throughput

GitHub runs at most 20 hosted jobs at once for this account, and one full run of `ci.yml` is
more jobs than that. This page records what the cap costs a landing, the rule for when a job
may skip without taking a runner, and the plan to get under the cap in three phases.

## What the cap costs

Every figure is a measurement from 2026-11-12.

- **A landing's wait is runner queueing, not run time.** One 25-job pull-request run took
  217 s while the pool was idle, and each job waited 2 s for a runner. The same 25 jobs took
  993 s three hours later, and each job waited 44 to 443 s.
- **Landings waited 173 hours in 7 days.** The landing annotations hold 576 landings from
  2026-11-05 to 2026-11-12, with a median of 10 minutes and a p90 of 41 minutes. The wait
  splits evenly between the merge wait, master CI and the promotion.
- **Each merge cost about 2.1 full runs.** From 16:30 to 22:50 UTC, 28 of 42 master push runs
  ran in full, beside about 62 full pull-request runs. The `prove` job skipped the other 14.
- **A run with no host change spends 10 of its 25 jobs on nothing.** The two `host_packages`
  legs, the six `host_packages_bootstrap` legs, `k3s_download` and `host_tools_arm64` each
  start a runner, skip every step that does work, and end green in 13 to 21 s.
- **The hosts cannot replace the hosted pool.** daniel-box and daniel-server hold about 15
  idle cores between them, against the 80 cores of 20 hosted runners. daniel-box is also
  bound by memory rather than CPU
  ([ADR-0027](adr/0027-fanout-batches-stay-on-host-cgroups.md)).

## When a job may skip at job level

A job with a false job-level `if:` reads `skipped` and takes no runner. A job that skips its
steps one by one still waits for a slot.

**The `prek` gate passes a skipped need on two paths only.** The first is the proven path:
`prove` said the pushed tree was tested, and every dependent skipped. The second is a job that
reads the build's artifacts and had nothing to run. That job skipped, `host_packages_build`
succeeded, and the build's output said `false` for it: `run-cases` for the cases legs and
`arm64-tools` for the arm64 job. Any other skipped need fails the gate, because GitHub reads a
skipped required job as a pass.

**`host_packages_build` owns the answer to whether a case has anything to run.** Its
`run-cases` output
is true on a `workflow_dispatch` run, and when the render reports `changed` or `cases`.
`k3s_download` skips at job level on it.

**`jobs_verdict` applies the gate's rule without the outputs.** A check-run does not carry a
job output. `hosts/lib/ci_verdict.py:jobs_verdict` therefore passes a build reader that skipped
beside a build that succeeded, and leans on the workflow for the rest: a reader's own `if:`
must skip it only on the build's output. `jobs_verdict` decides only while the gate job has not
concluded, and a concluded gate is believed.

**A host with an older package loses the early verdict, not the verdict.** Each installed host
package carries its own copy of `ci_verdict.py`. An older copy reads a skipped build reader as
`pending` and waits for the gate job's own conclusion, which costs the wait for a runner that
`jobs_verdict` exists to remove.

## The plan

The phases run in order, and each slice is one pull request.

### Phase A: skip the host-package jobs that have nothing to run

A run with no host change falls from 25 jobs to 15.

1. **A1, landed.** `k3s_download` skips at job level.
1. **A2, landed.** The gate and `jobs_verdict` pass a build reader that skipped because the
   build had no work for it, and the build reports `run-cases`. No gate need skips yet.
1. **A3, planned.** The eight case legs skip at job level on `run-cases`, and
   `host_tools_arm64` skips on `arm64-tools`. Their step-level conditions go.

A3 waits until every host reports a package at or past A2's merge commit in
`homelab_host_applied_info`, so no host's reader loses the early verdict.

### Phase B: a merge queue on daniel-box that tests the merged tree

The queue orders the merges and runs one sweep of each stacked tree on daniel-box. Landings
keep merging their own pull requests, so every rule in `scripts/deploy_tools/land_lib/` stays
where it is. The queue then records the tested tree, and the push run's `prove` job skips the
hosted sweep. A merge costs about 1.1 hosted runs in place of 2.1.

1. **B1, planned.** A shadow sweep of every master commit records its verdict, its duration
   and its peak memory. It changes no behaviour.
1. **B2, planned.** A queue of one, behind a flag, for the fan-out server lander. The local
   sweep replaces the branch-update re-test.
1. **B3, planned.** The queue records the tested tree before each merge, and
   `scripts/dev/tested_tree.py` accepts that record.
1. **B4, planned.** Batches of up to four, with a bisect when the stacked tree is red.
1. **B5, planned.** Every landing goes through the queue, and the merge pacing and the
   branch-update re-test are deleted.

B2 waits for B1 to run for 7 days and 300 commits. No commit may read green locally and red
on a hosted job the sweep covers, and the p90 sweep must take 10 minutes or less.

Three decisions are the operator's, and none is made:

- **Who records the tested tree.** `prove` trusts a record by its creator, so the queue needs
  a creator of its own. A GitHub App that can write a commit status and nothing else is the
  recommended one.
- **What a batch proves.** A batch sweeps only its last stacked tree, and that sweep stands
  for the commits before it. `scripts/deploy_tools/await_ci.py` already lets a descendant's
  verdict stand for a commit that has none.
- **Whose memory the sweep uses.** The sweep's cap comes out of the agent fleet's budget or
  the homelab plane's. B1 measures the peak first.

Two limits are known. daniel-box runs Ubuntu 24.04 with no Docker, so the sweep cannot run
the host-package cases, and a stacked tree that changes a host input gets no record. A hand
merge and Renovate's auto-merge still move master outside the queue, and each costs one more
local sweep.

### Phase C: self-hosted runners for the `pytest` shards

This phase starts only if, two weeks after B5, the p90 wait for a hosted runner in a busy
hour is still above 60 seconds.

1. **C1, planned.** An ADR supersedes
   [ADR-0018](adr/0018-the-repository-stays-public-and-ci-stays-hosted.md), and a check
   reports a fork approval policy other than `all_external_contributors`.
1. **C2, planned.** One sandboxed runner on daniel-box runs a shadow shard that gates
   nothing.
1. **C3, planned.** A repository variable routes real shards to the runner, and a value of 0
   sends every shard back to the hosted pool.
1. **C4, planned.** The shard count rises one at a time.

## See also

- [Landing a merged PR](landing.md) holds the merge pacing this plan replaces.
- [Self-hosted runner spike](archive/self-hosted-runner-spike.md) holds the 2026-09-22
  measurements behind ADR-0018.
