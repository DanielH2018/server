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
`run-cases` output is true on a `workflow_dispatch` run, and when the render reports `changed`
or `cases`. The eight cases legs and `k3s_download` skip at job level on it.
`host_tools_arm64` skips when `arm64-tools` is not true, and a dispatch runs it whatever was
built.

**`jobs_verdict` applies the gate's rule without the outputs.** A check-run does not carry a
job output. `hosts/lib/ci_verdict.py:jobs_verdict` therefore passes a build reader that skipped
beside a build that succeeded, and leans on the workflow for the rest: a reader's own `if:`
must skip it only on the build's output. `jobs_verdict` decides only while the gate job has not
concluded, and a concluded gate is believed.

**GitHub lists a skipped matrix job once, under the job's own name.** A matrix job that skips
at job level gets one check-run, named after the job and not after its legs: the `name:`
template with its expressions unexpanded, or the job id. `jobs_verdict` reads that check-run
as each leg skipped, from `CI_SKIPPED_MATRIX_JOBS`. Slice A3 shipped without this. From its
merge at 02:42 UTC on 2026-11-13 until slice A4, `jobs_verdict` read every run with no host
change as `pending`, and each reader waited for the gate job's own conclusion.

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
1. **A3, landed.** The eight cases legs skip at job level on `run-cases`, and
   `host_tools_arm64` skips on `arm64-tools`. Their step-level conditions are gone.
1. **A4, landed.** `jobs_verdict` reads a skipped matrix job under the one name GitHub gives
   it, so a run with no host change has its early verdict again.

Run 43354559178, the first push after A3 with no host change, took a runner for 15 jobs and
skipped the nine, with the gate green.

Phase A cut the median landing from 16.8 minutes to 12.7. The figures come from the
pipeline-timeline snapshots: the 41 pull requests merged in the 11 hours after A3, against
the 159 of the two days before, at the same rate of merges. The longest wait for a runner
fell from a median of 1.0 minute to 0.2, and from a p90 of 5.6 minutes to 1.9. The time from
a merge to the end of its landing did not move, at a median of 9.6 minutes.

### Phase B: a merge queue whose test is the real workflow

The queue orders the merges and tests each stacked tree once, by running `ci.yml` on a branch
that holds that tree. Landings keep merging their own pull requests, so every rule in
`scripts/deploy_tools/land_lib/` stays where it is. The run's own token records the tested
trees, as a pull-request run does, so `scripts/dev/tested_tree.py` trusts the record as it
trusts one today.

The queue works in this order:

1. It takes the first queued heads that stack on master without a conflict.
1. It builds the tree of master with the first pull request, then with the second added, and
   so on. A pull request whose own run already recorded its tree needs no queue run.
1. It pushes one commit that holds the last tree to a `queue/` branch, and dispatches `ci.yml`
   on it with the batch's pull-request numbers.
1. Every job of that run runs in full. Its gate rebuilds each tree from master and the heads,
   checks that the run's own tree is the last one, and records each tree on its head.
1. On green, each landing merges in turn, and each push run proves its tree and skips.
1. On red, the queue dispatches each half and evicts the pull request that fails.

The gate rebuilds the trees because a dispatch takes inputs. Anyone who can dispatch could
name a head and a tree of their choosing. The gate therefore takes pull-request numbers only,
and records what it built.

The slices:

1. **B1, ended.** A shadow sweep of master, and replays of pull-request runs that hosted CI
   read red. *What the shadow sweep found* below has the result, which changed this plan.
1. **R1, landed.** The shadow sweep's timer, its tool and its probe are deleted.
1. **B2, planned.** A queue of one, behind a flag, for the fan-out server lander. It saves no
   runs. It moves the full run that follows a merge to before the merge, so master stops
   taking a tree that no run tested.
1. **B3, planned.** Batches of up to four, with each half dispatched when the stacked tree is
   red.
1. **B4, planned.** Every landing goes through the queue, and the merge pacing and the
   branch-update re-test are deleted.

B2 first settles three things on a throwaway pull request. The tree of a squash merge must
equal what `git merge-tree` builds for the same base and head. The token the lander uses must
be able to create and delete a `queue/` branch under the rulesets. The token of a dispatch
run must be able to write a status on a head the run did not check out.

B3 first checks what `promote` publishes when the commits of one batch reach master seconds
apart, because only the last of them is a tree that a run tested.

What the queue is worth is an estimate, from one afternoon's counts on 2026-11-12:

| Configuration | Full runs per merge on the hosted pool |
|---|---|
| After Phase A, measured | 2.1: 1.5 pull-request runs and 0.7 full master runs |
| A queue of one | About 2.1, with the 0.7 ahead of the merge |
| Batches of four | About 1.7 |
| Batches of four, heavy jobs on runner pods | About 1.5 |

The decisions, all the operator's:

- **What tests a stacked tree. Decided on 2026-11-13: the real workflow.** The first plan
  ran a sweep on daniel-box and trusted its verdict. The replays showed that a sweep on the
  host reads green for trees that a runner fails, and that it does so in silence. On a real
  runner a difference shows as a red run of the real workflow.
- **What a batch proves. Decided on 2026-11-13: the commits before its last.** A batch tests
  only its last stacked tree, and that run stands for the commits before it. An earlier
  commit of a batch can therefore be red by itself. `scripts/deploy_tools/await_ci.py`
  already lets a descendant's verdict stand for a commit that has none.
- **Who records the tested tree. Withdrawn.** A GitHub App was to record the tree a local
  sweep tested. A run of the real workflow records its own, so the App has no use. Its
  private key is `tested_tree_app_private_key` in `secrets/secrets.yml`, and nothing
  reads it. Removing the key and deleting the App, number 5745632, are the operator's steps.
- **Whose memory a sweep uses. Withdrawn with the sweep.** Phase C asks the question again
  for runner pods.

#### What the shadow sweep found

A sweep ran the gate's two heavy jobs on daniel-box: `hooks`, with the command the swept
commit's own `ci.yml` carries, and the whole `pytest` suite. Six pull requests built and
corrected it, numbers 8367, 8394, 8410, 8424, 8437 and 8441. A timer swept the tip of master,
and a probe set each sweep beside the hosted run of the same commit.

Master's hosted runs are nearly all green: 4 of its 526 CI runs failed in the week to
2026-11-13. A sweep can only miss a failure on a tree that hosted CI read red, so agreement
on master shows how often a sweep raises a false alarm and nothing about what it catches.
The reds came from pull requests. The sweep tool replayed what a failed pull-request run had
tested, which is the merge of the head into master as master stood.

In the first 19 replays, four causes made the sweep read green where the hosted job was red.
None was a flake:

- **The locale.** Run 43336474461 failed a test that pinned the order `sort` gives two
  patterns. daniel-box is `en_US.UTF-8`, where `sort` ignores punctuation, and a runner is
  `C.UTF-8`. At the run's merge commit the test passes under the first and fails under the
  second.
- **The lint tools.** Run 43146864552 failed two tests with
  `FileNotFoundError: 'actionlint'`. The hosted pytest job sets `UV_NO_GROUP=lint`, and the
  sweep's tree held the lint tools from its hooks check. `uv run` keeps a package that an
  earlier run installed, and `uv run --exact` removes it.
- **A step the sweep did not run.** Runs 43108849551 and 42875490362 passed their suites and
  failed the shard-weight check that follows. The hosted job was red, and the tree was not.
- **The host's own files.** Run 42352711746 failed four tests with
  `cannot stat '/usr/lib/homelab/bin/chezmoi'`. daniel-box's host package installs that
  file, and a runner has none.

The sweep tool fixed the first three. The fourth is a class and not a case. A sweep on
daniel-box can reach about 80 tools in the deploy user's `~/.local/bin`, `kubectl` and
`rootq` in `/usr/local/bin`, and everything under `/usr/lib/homelab`. The tool cannot hide
them: daniel-box has no `bwrap`, and AppArmor refuses an unprivileged user namespace. A
dedicated user does not close the gap by itself either. A sweep with an empty home directory
failed 251 tests, because CI installs two dotfiles packages before its suite.

What the sweep measured stays useful for Phase C:

| Check, on daniel-box on 2026-11-13 | Seconds | Peak memory |
|---|---|---|
| `pytest`, 20,838 tests on 16 workers | 216 | 5,990 MiB |
| `pytest` on 8 workers | 209 | 3,776 MiB |
| `pytest` on 4 workers | 321 | 2,313 MiB |
| `hooks` | 74 | 1,030 MiB |

The memory column is anonymous plus shared memory, sampled once a second, which is what a
check cannot give back under pressure. The cgroup's `memory.peak` read 8,454 MiB for the
16-worker run, because it also counts page cache. On daniel-server, with 8 cores and sessions
running, the same suite took 841 s.

### Phase C: runner pods for `hooks` and `pytest`

One full run spends 1,243 of its 1,424 runner-seconds in seven jobs: `hooks` and the six
`pytest` shards (run 43354559178). Those seven move to runner pods in the cluster. The other
eight jobs stay hosted, because they are short or need what a pod does not have.

1. **C1, planned.** An ADR supersedes
   [ADR-0018](adr/0018-the-repository-stays-public-and-ci-stays-hosted.md), and a check
   reports a fork approval policy other than `all_external_contributors`.
1. **C2, planned.** Runner pods run a shadow `hooks` and a shadow `pytest` that gate nothing,
   and a probe sets each beside its hosted twin. The suite's Docker tests move to a job of
   their own, which stays hosted. The slice ends after a week with no shadow job green where
   its twin was red.
1. **C3, planned.** A repository variable routes the heavy jobs of a queue run to the pods. A
   value of 0 sends every job back to the hosted pool, and a watch sets it to 0 when no
   runner answers.
1. **C4, planned.** Pull-request shards follow, a count at a time, for a run that a person
   started from a branch of this repository. A bot's run and a fork's run stay hosted.

The order of work is R1, C1, C2, B2, B3, C3, C4 and then B4. C2's week runs while B2 and B3
are built.

The decisions, all the operator's:

- **Whether a runner may run in the cluster. Decided on 2026-11-13: yes, in a pod.** ADR-0018
  keeps every job hosted, because a runner on the host would run a fork's code beside the age
  key, a `gh` token and a cluster node. A runner pod holds none of the three: it has no host
  mount, no ServiceAccount token and no privilege, and it runs one job. Two things stay
  true. Egress NetworkPolicies are not enforced on this cluster, so a job in a pod can reach
  the LAN. A container escape would land on a production node. C1 writes the ADR.
- **Which credential registers a runner. Open.** A runner controller in the cluster needs a
  credential with the repository's `Administration: write` permission, which also covers the
  rulesets. The alternative keeps that credential out of the cluster: the queue on daniel-box
  mints a one-job runner token with the token the lander uses and hands it to a waiting pod. C1
  prices both.
- **Which node, and whose memory. Open.** A pod lives under `kubepods`, outside the agent
  fleet's bound on `user.slice`. daniel-box has 16 cores and 14 GiB of 28 available.
  daniel-server has 8 cores and 22 GiB of 30 available, and ran the suite four times slower.
  C2 measures one run on each node.

## See also

- [Landing a merged PR](landing.md) holds the merge pacing this plan replaces.
- [Self-hosted runner spike](archive/self-hosted-runner-spike.md) holds the 2026-09-22
  measurements behind ADR-0018.
