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

### Phase B: a merge queue whose run is a PR's only full run

In the day from 2026-11-13 00:30 UTC, 123 pull requests merged. They cost 177 pull-request
runs that ran jobs, which is 1.44 a merge, and 123 master runs, about 0.7 of them full per
merge. 6 of 164 finished pull-request runs were red. The pull-request runs are the larger
share, and most of them test a master that has moved on by the time the PR merges.

The queue therefore gives each PR one run, at the moment it lands. A PR stays a draft, and a
draft runs no job. At its turn the landing marks the PR ready and updates its branch from
master, and the run that starts tests the PR on the master it merges into. Master's own run
then proves that tree and skips. Every
rule in `scripts/deploy_tools/land_lib/` stays where it is, and so does every check a
pull-request run makes, because the one run is still a pull-request run.

The queue is never what makes a merge correct. When master moves between a PR's run and its
merge, the trees differ, nothing is proven, and master's run sweeps in full as it did before.

The slices:

1. **B1, ended.** A shadow sweep of master, and replays of pull-request runs that hosted CI
   read red. *What the shadow sweep found* below has the result, which changed this plan.
1. **R1, landed.** The shadow sweep's timer, its tool and its probe are deleted.
1. **B2, in progress.** A queue of one. With `LAND_QUEUE` set, a landing takes the landing
   turn, marks its draft PR ready, updates its branch and merges after that one run
   ([Landing a merged PR](landing.md)). The fan-out's server lander follows: it stops
   marking a batch's PR ready before the landing, and lands the draft.
1. **B3, planned.** Batches of up to four. The queue opens one pull request that holds the
   batch's heads stacked on master, and that PR's run is the batch's one full run. Its gate
   rebuilds each stacked tree from master and the heads and records it on each head. Each
   member then merges in turn, and its own run and master's prove the recorded tree and skip.
   A red batch splits in half.
1. **B4, planned.** Every landing goes through the queue, on both hosts. The merge pacing is
   deleted, and so is the branch update that follows a green run on a stale base. A landing
   then takes its slot under the limit on live landings when it gets the turn. That limit
   exists because every live landing polls GitHub on one token. A landing that waits for the
   turn polls only a lock file, so any number can wait, and the limit bounds the landings
   that spend quota.

Three things were settled before B2. The tree of a squash merge equals what `git merge-tree`
builds for the same base and head: for #8474 both read `dfa8d4331c36`, with master 11 paths
past the PR's base. The token the lander uses can create and delete a `queue/` branch under
the rulesets. A workflow dispatch was the first design for the queue's run, and it was
dropped: a dispatched run skips every step that reads a pull request, so the render-diff
gates and the cluster-pin gate would run nowhere.

The first pilot of the turn was #8551 on 2026-11-14. The landing took the turn and marked the
draft ready, and the PR got one run that ran jobs. Master's run still swept in full, for two
reasons. The run tested the PR on the master of the PR's creation, 16 minutes old: GitHub
builds the tree a pull-request run tests when the PR's branch is pushed, and neither a ready
flip nor a read of the PR builds it again. The landing therefore updates the branch at its
turn, which is a push, for a head that has had no run. The other reason is that two landings
without the turn merged during the run, and that lasts until every landing takes turns.

B3's gate rebuilds the trees because the batch's pull request names its members. Anyone who
can open a pull request could name a head and a tree of their choosing, so the gate takes
pull-request numbers only and records what it built. B3 first checks what `promote` publishes
when the commits of one batch reach master seconds apart, because only the last of them is a
tree that a run tested.

What the queue is worth is an estimate. The measured row is one afternoon's counts on
2026-11-12, and the batch row assumes the 3.7% red rate above, with a red batch run again in
halves:

| Configuration | Full runs per merge on the hosted pool |
|---|---|
| After Phase A, measured | 2.1: 1.5 pull-request runs and 0.7 full master runs |
| A queue of one | About 1.0 |
| Batches of four | About 0.3 to 0.4 |

The decisions, all the operator's:

- **Whether a PR may go without a run of its own until it lands. Decided on 2026-11-14:
  yes.** The run at the PR's turn tests the tree that lands, so an earlier run is weaker
  evidence of the same thing. An author gets no hosted verdict before the landing, and
  sessions still run `prek` and their tests on the hosts. The first plan kept every
  pull-request run and trimmed only master's, which reached about 1.7 runs a merge.

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

**Phase C stopped at C1 on 2026-11-14.** The queue of Phase B removes more runs than runner
pods could take, and it needs no runner. C2 and C3 are kept here as they were planned, with
what the survey for C2 found. The operator's direction is to look at them again after B3 only
if runs still queue, and to change the set-up of the tests first so that a pod costs less.

One full run spends 1,243 of its 1,424 runner-seconds in seven jobs: `hooks` and the six
`pytest` shards (run 43354559178). Those seven would move to runner pods in the cluster. The
other eight jobs stay hosted, because they are short or need what a pod does not have.

1. **C1, done.** [ADR-0032](adr/0032-cis-heavy-jobs-may-run-on-runner-pods.md) supersedes
   ADR-0018, and the `github-checks` component alerts on a fork approval policy other than
   `all_external_contributors`.
1. **C2, paused.** A launcher on daniel-box starts one runner pod for each heavy job of a run
   it dispatches, and the `workflow-runners-are-pinned` row accepts the one `runs-on` expression
   that routes a job to a pod. A timer dispatches shadow runs of master that gate nothing, and
   a probe sets each beside the hosted run of the same commit. The suite's Docker tests move to
   a job of their own, which stays hosted. The slice ends after a week with no shadow job green
   where its twin was red.
1. **C3, paused.** A repository variable routes the heavy jobs of a queue run to the pods. A
   value of 0 sends every job back to the hosted pool, and a watch sets it to 0 when no
   runner answers.

A pull-request run would stay hosted, because the launcher starts pods only for a run it
dispatched. The pods would therefore serve one or two runs an hour and sit idle for the rest.

The survey for C2 found three costs the plan had not counted:

- **The jobs need root.** `hooks` and `pytest` call `sudo`, install apt packages and write to
  `/usr/local/bin`, and a pod under the `restricted` standard can do none of it. A pod needs
  an image of its own with those tools in it, and steps that differ between a pod and a
  hosted runner. That is a second copy of the environment, which is how the sweep went wrong.
- **A test refuses any ResourceQuota.** `test_no_resourcequota_is_declared_anywhere` fails on
  one, and ADR-0032 names a quota as a bound on the launcher.
- **The launcher's token takes operator steps.** It would reach daniel-box as the read-only
  token does: copied from the cluster into SOPS by hand, then synced to the host.

The order of work is R1, C1, B2, B3 and then B4.

The decisions, all the operator's:

- **Whether a runner may run in the cluster. Decided on 2026-11-13: yes, in a pod.** ADR-0018
  keeps every job hosted, because a runner on the host would run a fork's code beside the age
  key, a `gh` token and a cluster node. A runner pod holds none of the three: it has no host
  mount, no ServiceAccount token and no privilege, and it runs one job. Two things stay
  true. Egress NetworkPolicies are not enforced on this cluster, so a job in a pod can reach
  the LAN. A container escape would land on a production node. ADR-0032 is the record.
- **Which credential registers a runner. Decided on 2026-11-13: it stays on daniel-box.** The
  launcher mints a one-job runner configuration with the token the lander uses, and creates the
  pod that holds it. A runner therefore exists only while a dispatched run waits for it. The
  launcher creates pods through a ServiceAccount that can write Jobs in the runner namespace and
  nothing else. That is a second write path to the cluster beside Flux, bounded by the
  namespace, its Pod Security standard and a ResourceQuota. The option refused was GitHub's
  runner controller in the cluster. It starts a pod for any queued job, so a pull-request run
  could use one, and it needs no launcher. It needs a credential with the repository's
  `Administration: write` permission in a cluster Secret, which can rewrite the rulesets and
  delete the repository, and no credential in the cluster can do either. The operator minted
  one configuration by hand on 2026-11-13 with the token the lander uses, so that token may.
  GitHub gave the runner exactly the labels the call passed and no `self-hosted` label.
- **Which node, and whose memory. Open.** A pod lives under `kubepods`, outside the agent
  fleet's bound on `user.slice`. daniel-box has 16 cores and 14 GiB of 28 available.
  daniel-server has 8 cores and 22 GiB of 30 available, and ran the suite four times slower.
  C2 would measure one run on each node.

## See also

- [Landing a merged PR](landing.md) holds the merge pacing this plan replaces.
- [Self-hosted runner spike](archive/self-hosted-runner-spike.md) holds the 2026-09-22
  measurements behind ADR-0018.
