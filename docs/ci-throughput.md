# CI throughput

GitHub runs at most 20 hosted jobs at once for this account, and one full run of `ci.yml` is
more jobs than that. This page records what the cap costs a landing, the rule for when a job
may skip without taking a runner, and the plan to get under the cap in two phases, A and B.
Phase C, runner pods for the heavy jobs, is not planned: the operator ruled on 2026-11-20
that CI gets no self-hosted runner and no paid GitHub plan
([#8982](https://github.com/DanielH2018/server/issues/8982)).

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

**A required context in its own workflow may skip at job level on a draft only.** The
`flux-deletion-drill` and `pull + boot changed images` jobs are required contexts with no
gate in front of them, so their skip is the pass GitHub reads. On a draft that pass merges
nothing: GitHub refuses to merge a draft, every landing marks the PR ready first
(`merge._mark_ready`), and the `ready_for_review` event in each workflow's `types` starts
the run that counts. `ci_verdict` takes the worst outcome per check name, so once the ready
run registers, the draft run's skip no longer decides. On a ready PR both jobs still start a
runner and scope themselves out step by step. A `paths:` filter would leave the required
context waiting as `Expected`. A job-level skip would need an earlier job to decide the scope, and
that job takes the same slot.

### The workflows beside `ci.yml`, counted

Four workflows beside `ci.yml` held 17.7% of the slots while the cap bound, from 17:00 UTC
on 2026-11-19 to 02:30 on 2026-11-20
([#9650](https://github.com/DanielH2018/server/issues/9650)). The count below reads each
run's job from GitHub's jobs listing. A flux-deletion-drill run found nothing to do when its
`Fetch the pinned flux CLI` step skipped. An image-smoke run found nothing to do when its
`Pull + boot each image` step skipped.

| Workflow | Runs | Runs with nothing to do | Their slot-seconds | All slot-seconds |
|---|---|---|---|---|
| Flux deletion drill | 292 | 216 (74%) | 3,207 | 20,181 |
| image-smoke | 292 | 277 (95%) | 4,261 | 5,009 |
| Bootstrap drill | 78 | 0 | 0 | 29,661 |
| render-diff | 169 | 0 | 0 | 9,707 |

- **flux-deletion-drill and image-smoke skip a draft at job level.** Both triggered on every
  push to a draft, about three runs a merge. A run on a draft takes no runner.
- **Bootstrap drill and render-diff have no run with nothing to do.** Each has a `paths:`
  filter and no step that scopes the job out, so every run that starts does its work.
- **Bootstrap drill and render-diff skip a draft at job level too**
  ([#9949](https://github.com/DanielH2018/server/issues/9949)). Neither is a required
  context, so the skip decides no merge. GitHub evaluates `paths:` against the whole PR diff
  on `ready_for_review`, so marking the PR ready starts a run whenever the diff touches a
  filtered path. A person reads render-diff's printed diff before merge, not while the PR
  is a draft.

## A pull request runs the test modules its diff can affect

A pull-request run's `pytest` legs run a selection (#8458, ruled by the operator on
2026-11-15), and so does a push to master whose tree no pull-request run tested. A
dispatch runs every module. A push whose tree a pull-request run tested runs nothing
([ADR-0036](adr/0036-a-selected-run-proves-its-tree-and-a-nightly-sweep-is-the-net.md)).

- **The table.** `scripts/dev/affected_tests.json` holds, for each test module, the tracked
  files its own run resolved, the directories it listed, the paths it asked for and did not
  find, and whether it read git's index. `scripts/dev/affected_tests.py record` measures a
  row by running the module alone under `strace`. The docs-refresh cron records one twelfth
  of the rows on each run, after every module that has no current row.
- **The selection.** `scripts/dev/pytest_shard.py --changed-since HEAD^1` diffs the merge
  commit against its base and asks `scripts/dev/affected_lib/table.py:select` which modules
  the diff reaches. A module with no row always runs, and so does a module edited since its
  row was recorded. The whole census runs when the diff touches a path that decides what a
  run is, such as `pyproject.toml`, `uv.lock`, a `conftest.py` or `ci.yml`, and when a
  changed file is in no row.
- **The legs.** The selection is packed into as few legs as keep each no heavier than a full
  sweep's leg, out of `pytest_shard.CI_LEGS`. GitHub fixes the leg count before any step
  runs, so the `pytest_plan` job plans the legs first: `pytest_shard.py --plan` runs the
  selection once and publishes the leg numbers, and the `pytest` matrix starts only those
  (#8944). A selection of no modules starts one leg, because GitHub fails a job whose matrix
  is empty, and that leg skips every step after its select step. A run with no base, and a
  run whose plan failed, starts every leg. A failed plan still fails the gate, so its run
  needs a rerun. The plan step and every leg diff against the same base, which
  `tests/repo/ci_workflows/test_ci_pytest_selects_affected_modules.py` checks for each event `ci.yml`
  runs on. Each leg passes the plan back to `pytest_shard.py --planned`, and the leg fails
  when its own selection fills more legs than the plan started. `scripts/dev/ci_slot_cost.py
  --recent 20 --pull-requests` counts the empty legs.
- **What the plan costs.** On the 20 pull-request runs measured on 2026-11-20, the empty
  legs held 1046 slot-seconds, about twice what one planning job on each run costs. Only
  `pytest` waits for the planning job, which starts its legs about 28s later. That wait does
  not lengthen the run, because `hooks` and the host packages finish minutes after a
  selected leg.
- **What a selection gives up.** A module can gain a dependency after its row was recorded.
  A pull request that changes the dependency then leaves the module out, and nothing runs
  it until the nightly sweep below. The operator accepted that cost.
- **A selected run proves its tree.** The `prek` gate records `ci/tested-tree` on the
  pull request's head whether the legs ran the whole census or a selection. The push run of
  the same tree would diff it against the same base with the same table, so its selection
  would be the one the pull-request run already ran, and it skips every job
  (`tests/repo/ci_workflows/test_ci_skip_proven_master.py::test_a_run_records_the_tree_it_tested_unless_it_was_proven`).
  A landing batch's run records its members the same way. Its selection comes from the
  diff of every member stacked on master, so it holds each module any one member can
  affect.
- **An unproven push selects against the newest green ancestor.** Master moved under the
  pull request, so no run tested the pushed tree. The `prove` job names a base,
  `scripts/dev/tested_tree.py:green_base`: the newest first-parent ancestor whose `prek`
  gate passed. The legs select the modules the diff from that base can affect. The base is
  not always the parent. A selection against a parent whose run is red would leave out
  what the parent broke, and master would read green with that module failing
  (`scripts/dev/tests/ci/test_tested_tree.py::test_a_red_or_unfinished_parent_is_skipped_for_the_ancestor_behind_it`).
  A parent whose run has not finished is skipped the same way. With no green ancestor
  within `MAX_BASE_WALK` commits, or a lookup that fails, the push runs every module.

## pytest-testmon selects local reruns only

To rerun only the tests a local edit can affect, install the `local` dependency group and
run testmon:

```bash
TESTMON_DATAFILE="/tmp/testmondata-$(basename "$PWD")" uv run --group local pytest tests --testmon --testmon-forceselect
```

CI never loads testmon, because `local` is not in `default-groups` and the recorded table
above stays the CI selector. Never pass `--tmnet`.

- **Why not CI.** Testmon traces Python only. On 2026-11-25 with testmon 2.4.0 it caught 3
  of 6 failing modules where the recorded table caught 6 of 6, because it misses shell and
  YAML inputs.
- **Cost.** On 2026-11-26 with testmon 2.3.1, the first run records and took 403 s against
  200 s for a plain run of `tests/` (4,986 tests, on a host with other sessions running).
  An unchanged rerun took 1.4 s. The 2.4.0 measurement was 5 s against 29 s for 2,228
  tests, with a recording run of 38 s.
- **The pin.** `pyproject.toml` caps testmon below 2.4.0, which shipped on 2026-11-25.
- **Why `--testmon-forceselect`.** `addopts` carries `-m 'not ui and not image'`, and a
  `-m` expression disables testmon's selection unless this flag is set.
- **One data file per worktree, outside the checkout.** `TESTMON_DATAFILE` names a path
  under `/tmp` that ends in the worktree's directory name. The recording is not
  committed, and a session in another worktree never selects against it.

## A nightly sweep runs every module

`.github/workflows/pytest-sweep.yml` runs the whole suite on master's head once a day, on
one runner and with no selection. It finds a module whose row in the table is older than a
dependency the module gained, which no selected run is told to run.

- **It starts hours after its schedule.** The cron line says 09:30 UTC. GitHub started
  the Renovate canary, scheduled for 09:00 UTC, between 17:38 and 18:58 UTC on each of
  2026-11-16 to 2026-11-18. A stale-row miss can therefore go unseen for more than a day.
- **It grades no commit.** No job in it carries the name of the `prek` gate, so
  `hosts/lib/ci_verdict.py:ci_verdict` never reads it and a red sweep holds no landing
  (the `pytest-sweep-result-cannot-grade-a-commit` row of
  `tests/repo/census_rows/test_census_rows_workflows.py`).
- **It reports through one issue.** `scripts/dev/scheduled_report.py` opens an issue titled
  `Nightly pytest sweep is red` that names the failed modules, comments on it while the
  sweep stays red, and closes it on the next green sweep. The issue carries no `claude`
  label, because the findings register drops an issue a bot opened.
- **It runs the tests marked `image` too.** `addopts` deselects them from every pytest
  run, and `image-leg.yml` runs them only on a pull request whose diff matches its
  `paths:` list, which misses modules the image tests import. A second step in the sweep
  runs `-m image` over the whole tree after the suite, red or green, and appends to the
  log the sweep issue reads its failed modules from. The step needs Docker, so the sweep
  stays on a hosted runner (the `pytest-sweep-runs-the-image-tests` row of
  `tests/repo/census_rows/test_census_rows_workflows.py`).
- **It runs the suite in random order.** `uv run --with pytest-randomly` adds the plugin
  to this run only, so a test that depends on an earlier test's state fails here and in no
  leg. The log's header prints `Using --randomly-seed=N`. The seed fixes the collection
  order, but `--dist loadscope` hands modules to workers as each one frees up, so a rerun
  can put a polluting module on a different worker. To replay one order for the seed, run
  the whole suite serially with
  `uv run --with pytest-randomly pytest -n 0 --randomly-seed=N`. A single module
  cannot reproduce a dependency on state another module left behind.
- **It shares its tool installs with the pytest legs.** Both call
  `.github/actions/pytest-tools`, which holds the SOPS, age, apt-utils and dotfiles steps.
- **To run the whole suite on a branch,** dispatch it there:
  `gh workflow run pytest-sweep.yml --ref <branch>`. A dispatch off master reports nothing.

**The saving is on the pull-request run, and the push run keeps it.** On 2026-11-14 a run
with no host change used a median 1,414 slot-seconds, about 1,020 of them in the six
`pytest` legs. A pull request whose selection fits one leg saves most of those 1,020, and
its push run skips as proven. The nightly sweep costs one runner for about ten minutes a
day. `scripts/dev/ci_slot_cost.py --recent 20` prints the measured figure.

**A pytest leg's setup is spread over many short steps, and the apt cache is not one of the
costs.** On 2026-11-22, `ci_slot_cost.py --recent 10` read a median 26.0s per leg outside
`Run the test suite` over 41 master legs, and `--pull-requests` read 25.0s over 35 legs. The
master medians per step were these:

- checkout: 7s;
- the setup action: 5s;
- `Set up job`: 4s;
- the tool installs: 3s;
- the runner's time before the first step and after the last: 3s;
- the select step: 2s.

Four leg logs from runs 44969299707, 44842751621 and 44952060308 print
`age and apt-utils: cached`, so the apt install takes about 0.02s. Of checkout's 7s, between
4.6s and 5.4s is the `fetch-depth: 0` fetch of every branch. That is the only step whose cost
stays above 3s on every leg. #9905 measures a narrower fetch before anyone changes it. No
other step justifies its own work. The `$/` tarball in `Set up job` is a `DECIDED:` in
`.github/actions/setup/action.yml`. `uv sync` restores a warm cache and installs 42 packages
in 23ms. A step change cannot remove the runner's start and teardown time.

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

## The bootstrap legs run two shards per release

**Three package builds on one 4-CPU runner cost about 15 s per leg, and no leg ends last.**
`host_packages_bootstrap` runs two shards per release, and
`scripts/validate/validate_lib/bootstrap_plan.py` deals three builds and one kit red to each.
On 2026-11-22, before the cut from three shards to two, 19 pull-request runs measured these
figures:

- each leg ran two builds at once, and each build took 55 to 61 s;
- the bootstrap case step took 57 to 83 s;
- the last bootstrap leg ended 100 to 448 s before the last `host_packages` leg.

The five pull-request runs after the cut that ran host legs on both releases are 45053197910,
45054223562, 45056306349, 45061101940 and 45068121495. Each had four bootstrap legs, and
their 20 legs measured these figures:

- the bootstrap case step took 71 to 100 s, and 8 of the 20 legs ran past 83 s;
- each bootstrap leg ended 320 to 441 s before the last `host_packages` leg.

The smallest margin, 320 s, is larger than a 198 s wait for a runner, so a bootstrap leg
still ends before the `host_packages` legs after a wait that long. Restore three shards per
release if a bootstrap case step grows past about 120 s or a bootstrap leg ends last.

A pull request that edits the test image's Dockerfile builds the image in each bootstrap leg.
In run 45069920787 that build took about 120 s of a 230 s leg, while the case step took 88 to
91 s.

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
merge, the trees differ, nothing is proven, and master's run repeats every job but `pytest`,
whose legs run a selection against the newest green ancestor.

**D9, ruled by the operator on 2026-11-14: every landing forms a batch, and sessions and the
fan-out open their PRs as drafts.** B3 saves runs only for a draft whose head has had no CI
run, and sessions opened their PRs ready, so almost no landing could use it. Three
measurements stand behind the ruling:

- Four batches landed 11 PRs on 5 full runs on 2026-11-14. One at a time, those PRs cost
  11 master runs.
- Of the newest 800 pull-request runs up to 22:00 UTC that day, 476 ended green or red and
  31 were red, which is 6.5%. A branch's first run was red 17 times in 260, also 6.5%. A
  first run is red no more often than a later one, so a draft gives up little by having no
  run when it is opened. At least four of the 31 were one flaky test (#8736).
- At 6.5% a batch of four is red 24% of the time. A red batch costs its run and one run
  for each member, so a PR in a batch of four costs about 0.5 full runs, against about 2
  for a PR opened ready.

The cost is the one this phase accepted when it gave each PR one run: the author of a draft
gets no hosted verdict until the landing. With `LAND_BATCH=0` a landing forms no batch.
Its draft still stands in the line, so another landing can stack it.

Who opens a draft, and what lands it:

- **A session** opens its PR with `gh pr create --draft` and lands it with `land.sh`
  ([Landing a merged PR](landing.md)).
- **A fan-out batch on daniel-box** leaves its PR a draft through the review, and its
  pipeline lands the draft (`scripts/dev/fanout_lib/review/review_ci.py:land_draft`), so
  no run is spent before the landing. A landing that ends `pr-ci-red` resumes the batch's
  agent once, and the pipeline lands the PR again.
- **A fan-out batch on daniel-server** lands its draft the same way since option B below,
  unless the rule of the server lander holds the PR for a person. **HISTORY —** Until option B its
  pipeline marked the PR ready, waited for that run, and the lander unit on daniel-box
  landed it, as the operator chose on 2026-11-14. From 2026-11-08 to 2026-11-14 the review
  pipeline wrote 245 batch records on daniel-server and 187 on daniel-box, so 57% of
  batches kept a run of their own.

Option B, a later step: a daniel-server pipeline lands its own draft from daniel-server.
The operator ruled on its two preconditions on 2026-11-15:

- **A daniel-server fan-out pipeline may land its own PRs.** This lifts the rule that only
  daniel-box's lander unit lands a fan-out PR. Sessions already landed from daniel-server.
- **The health gate on daniel-server reads a daniel-pi container through daniel-box
  (#8773).** daniel-pi accepts no key from daniel-server, so the gate's `docker inspect`
  ended `gate-unreadable`. `probe_lib/core.py:pi_ssh_argv` runs the Pi's command through an
  ssh session on daniel-box, the host the turn, the line and the slots already reach. Two
  options were refused: giving daniel-server a key the Pi accepts widens what daniel-server
  can reach, and handing a Pi landing to daniel-box keeps it on the slow path.

**Option B, ruled by the operator on 2026-11-15: build it, whatever the measurement shows.**
A daniel-server review pipeline lands its own draft (`review_ci.server_lands`). It does so
only for a PR the server lander would land unattended: `own_surface.pr_problems` holds a
PR that changes the landing code, the fan-out tooling, `secrets/`, `.github/`, `.claude/`
or the `claude-code` component, and one whose body names an operator step. A held PR
stays a draft with no run, and the server lander leaves it for a person, whose `land.sh`
marks it ready at its turn ([#9896](https://github.com/DanielH2018/server/issues/9896)). A CI fix that reaches a held path ends the batch `needs input`. A plain daniel-server
batch still stops at its PR.

The slices:

1. **B1, ended.** A shadow sweep of master, and replays of pull-request runs that hosted CI
   read red. *What the shadow sweep found* below has the result, which changed this plan.
1. **R1, landed.** The shadow sweep's timer, its tool and its probe are deleted.
1. **B2, landed.** A queue of one. Every landing that merges its PR takes the landing
   turn, marks a draft PR ready, updates its branch and merges after that one run
   ([Landing a merged PR](landing.md)). The turn spans both hosts. D9 ruled on the two steps that followed: sessions open their PRs
   as drafts, and a daniel-box fan-out batch lands its PR as a draft.
1. **B3, landed.** Batches of up to four. The landing that holds the turn opens one pull
   request that holds the batch's heads stacked on master, and that PR's run is the batch's
   one full run. Its gate rebuilds each stacked tree from master and the heads and records
   it on each head. Each member then merges in turn, and its own run and master's prove the
   recorded tree and skip. The holder's own run splits a red batch (*A red batch* below).
1. **B4, landed.** D10 below rules its shape, in five pull requests, each landed before
   the next:
    1. A green ready PR that master moved past goes back in the line as a draft
       (`stale_base.rejoin_line`). Landed in #8818, after #8816 moved code to make room.
    1. The branch update that follows a green run on a stale base is deleted. Landed in
       #8820.
    1. The merge pacing is deleted. Master's run count moves to the limit on live landings.
       Landed in #8821.
    1. The limit on live landings becomes one count of 6 across both hosts, taken after the
       merge. The turn is the slot before the merge. The 6 landing slots landed first
       (`land_lib/slots.py`, #8826). Then the three rules that refuse a merge moved inside
       the turn (`slots.refuse_before_merge`, #8830), and last the per-host limit's count,
       its registry and the slow-runs drop were deleted (#8834).
    1. The switch that turned the turn off for one landing is deleted, so every landing that
       merges directly takes the turn.
       `LAND_TURN_DIR` moves the turn's lock and line for a test that runs a whole landing.

   That limit exists because every live landing polls GitHub on one token. A landing that
   waits for the turn as a draft polls only a lock file, so any number can wait, and the
   limit bounds the landings that spend quota.

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

The second pilot was #8668 on the same day, a draft pushed 20 minutes and three master commits
before its landing. The landing took the turn, updated the branch across 29 changed paths and
merged 240 s later on one run. That run recorded the tree `02974102da74`, which is what
`git merge-tree` builds from the master of the turn and the PR's head, so the run tested the
master of the turn. Master's run still swept in full, in 8 minutes 40 seconds: a landing
without the turn merged 68 s before the pilot did. The pilot also waited 16 minutes for a
slot under the limit on live landings before it could ask for the turn, which is the wait B4
removes.

The first master run the turn proved was #8674's, the next landing. Its run recorded the tree
`31fc245857a5`, the merge made the same tree, and master's run took 15 seconds. That is what
shortens a landing: its slot is held mostly after the merge, while master's run waits for
runners.

The limit on live landings was what held the backlog of 2026-11-14, not CI. At 12:55 UTC, 25
of 29 open PRs were green. The fan-out's lander on daniel-box had been refused a slot 70
times in six hours and had started two landings, because sessions' landings held all three
slots. Two facts about the limit came out of it:

- A landing takes its slot before it waits for anything and holds it to its verdict. Read
  from the code, one landing spends about 6 GitHub calls a minute while its PR's CI runs, 6
  while master's CI runs, 2 to 4 during the promotion, and none while it waits for the turn
  or for health. Every one of those repeated calls is a REST call. Two of the six in the
  merge wait were `gh pr view`, which spends the separate GraphQL quota, until
  `Landing.view` took the PR's state from the REST pull object (`pr_json.rest_filter`).
  `docs/github-quota.md` holds the account-wide design.
- The count is a directory of pid files on the host that runs the landing. daniel-box and
  daniel-server therefore each admit three landings against the one token, and seven were
  live at once that afternoon.

B3's gate rebuilds the trees because the batch's pull request names its members. Anyone who
can open a pull request could name a head and a tree of their choosing, so the gate takes
pull-request numbers only and records what it built.

B3 lands in four steps, each one exercised before the next:

1. **A labelled PR run asks the proof (#8703).** `prove` runs on a pull request that
   carries the `landing-batch` label, and `tested_tree.py prove-head` answers whether the
   head carries a tested-tree record. A member's own run then takes two short jobs. The
   label only decides whether `prove` takes a runner, so an unlabelled PR run pays nothing.
   The question does not compare trees: the tree a pull-request run checks out is the
   merge GitHub built at the PR's last push, and the batch tested a different one. The
   landing merges a head a batch tested only while master is still the commit the batch
   stacked it on, and master's run compares the tree again after.
1. **The batch's gate records each member's tree (#8721).** On a PR from a `queue/` branch
   of this repository, the gate reads the member numbers from the PR's body, rebuilds each stacked
   tree from the run's base and the members' heads, and records a tree on a head only when
   the last rebuilt tree is the tree the run tested (`tested_tree.py record-batch`). The
   run tested only that last tree, and the record on an earlier member names a tree the
   run never checked out: the decision under *What a batch proves* accepts that. A batch
   the gate cannot rebuild is not red: its members land one at a time. A batch's run
   executes its members' code with a token that can write statuses, so a fork's head must
   never be stacked. The gate refuses a batch that names one, and that refusal is not the
   boundary: a pull-request run takes its workflow and its scripts from the merge it tests,
   so a stacked fork head would supply the gate's own code. The boundaries are who can push
   a `queue/` branch and the landing in the next step, which stacks no fork's head. The
   `agent branch fence` ruleset lets only the admin role and Renovate create a branch
   outside `worktree-claude+**`, so only a landing that can already merge to master can
   push a `queue/` branch.
1. **The landing that holds the turn forms the batch.** It lands in two parts.

    - **The line (#8734).** A landing that waits for the turn with a draft that has had no CI run
      stands in the line: one file per PR in `/run/lock/homelab-land-line` on daniel-box,
      holding the head (`land_lib/line.py`). The landing refreshes its place on every poll
      and gives it up when the wait ends. A place that has gone two minutes without a
      refresh, which is four polls at the default interval, belongs to a landing that
      died, and the next read of the line removes it. The holder of the turn writes what
      became of a PR into that PR's place, and a landing whose place reads `merged` stops
      waiting. A landing reads and answers only the places its own user wrote. For a
      landing on daniel-server that user is the one its ssh logs in as on daniel-box.
    - **The batch.** A landing with a draft that takes the turn stacks up to three more
      drafts from the line behind its own (`land_lib/batch.py`). It builds the stack on
      GitHub: a `queue/<its PR>` branch at master's tip, then one call to the `merges`
      endpoint for each head. It opens the batch's pull request and waits for its run. On
      green it labels and merges its own PR. It then takes each member in the order they
      were stacked: it labels the member, marks it ready, waits for the member's two short
      jobs and squash-merges it. It writes `merged` into each member's place, and that
      member's own landing then follows the deploy as it does for a merge made elsewhere.
      Every landing forms a batch this way since D9, and `LAND_BATCH=0` turns it off for
      one landing.

      A landing stacks only the heads it would have been allowed to land. It runs its own
      checks on each member, the landing policy included, and the member's own landing ran
      its checks before it stood in the line. It stacks only landings of its own user. A
      serial PR (`live_limit.kind`, listed in `docs/landing.md`) lands alone: the
      cluster-pin gate refuses a pin bump that shares its PR. A member merges
      under its PR's title, whatever `--subject` its own landing was given.

      A landing compares its own code with master once, when it forms the batch
      (`classify.code_lags_master`). It does not compare again before a member's merge.
      Until the last member merges, master moves only by the batch's own merges, and the
      batch's run tested those. On 2026-11-14 the landing of PR 8550 compared before each
      member: its own merge changed `scripts/renderer/renderer_lib/cluster.py`, which a
      landing imports, so it refused the two PRs stacked behind it and they cost a second
      run.

      A holder whose code is older hands the turn to a landing of its PR from a fresh
      checkout of origin/master, which forms the batch (`turn.HandOver`). The new landing
      inherits the turn's locked descriptor, so no other landing takes the turn in
      between. A turn held over ssh, a lander unit's turn and a turn already handed over
      are not handed on: that holder lands its PR alone. Before the hand-over, a holder
      whose code was older formed no batch at all. A landing waits 30 to 60 minutes for
      the turn under load, and master's landing code changes in that time, so most holders
      formed none. On the night of 2026-11-18, 12 of the 17 holders that checked the line
      for a batch logged `no batch ... its landing code is older than master's`. Each such holder held the turn for one full run per PR, so the line moved
      one PR per run, and the waits for the turn reached 3060 to 4950 s.

      The landing pushes nothing. A `merges` call makes a commit with two parents where
      the squash merge makes one, and both make the same tree: for two open heads on
      2026-11-14 the endpoint and the gate's own stack both gave `90a57d70` and
      `fbc228e3`. A head that conflicts with the PRs before it answers HTTP 409 and is
      left in the line.

      A labelled run on a head that carries a record skips every job and compares no
      tree, so the label is the dangerous part. Three rules hold it. A PR is labelled
      only by the landing that is about to merge it. It is labelled only at the head the
      batch stacked and only while master is the commit that head was stacked on, which
      for a member is the commit the PR before it merged as
      (`stale_base.batch_tested`). A PR that was labelled and marked ready and then did
      not merge is converted to draft and back with no label, which starts a full run,
      so no later landing merges it on the skipped one. A member whose head moved, whose
      own landing left the line, or that master moved under is never labelled, and it
      and the members after it land alone.

      A batch never fails a landing. A batch that cannot be formed leaves the holder to
      land its own PR as before, and a failure while the members merge ends the batch and
      leaves the holder's deploy to go on. The members' merges come out of the two merge
      budgets `LAND_DEADLINE` already counts for the holder's own wait, up to ten minutes
      each. A member that no longer fits in what that wait left lands alone, so a batch
      asks no lander unit for a longer deadline.

   Only a draft whose head has had no CI run that ran jobs stands in the line. A ready flip
   is the one event that starts a member's short labelled run, and a PR that is already
   ready has no flip left to make. A green PR converted to draft does not qualify by itself:
   it needs a head whose only run skipped. B4's first step does that in the landing: a green
   ready PR that master moved past is converted to draft and its branch updated
   (`stale_base.rejoin_line`).

   The first batch ran on 2026-11-14. The landing of PR 8546 stacked PRs 8547 and 8552
   behind it. One run of 7 min 54 s tested the three, they merged within 90 seconds of
   each other, and master's run after each merge took 13 to 17 seconds.
1. **A red batch.** The holder's own run splits a red batch in two: the holder's PR and
   its members (`land_lib/batch.py:_settle`). With a red rate of 3.7% a batch of four is red
   about one time in seven, which is where the estimate of 0.3 to 0.4 runs a merge comes
   from.

   When the batch's run is red or gives no verdict in 45 minutes, the holder forms no
   second batch and gets a run of its own PR, which it owes in any case. Until that run
   decides, each member's place reads `split`, which no holder stacks. When the run is red,
   the red PR was the holder's, and the holder clears each member's answer, so the next
   holder can stack them. When it is green, or the holder's landing ends another way, the
   holder writes `alone` into each member's place. A member's own landing reads it on its
   next poll, leaves the line, and waits on for the turn as a ready PR does. When it takes
   the turn it forms no batch and gets its own run, which says whether it was the red one. A
   place that carries an answer is not listed as waiting, so the next landing to take the
   turn does not stack that PR again in the seconds before it leaves. A place keeps its
   first answer other than `split`, so a later one cannot turn `merged` into `alone`. A
   member that takes the turn while its place reads `split`, because the holder stepped out
   of the turn for a rate-limit wait, lands alone.

   **HISTORY —** Before any answer, the members stayed in the line, and each later landing
   stacked the red one again: a batch of four whose last PR is red cost three red batches
   and four runs alone. Telling every member `alone` cut that to the batch's one run and
   four runs alone, the holder's included. On 2026-11-20, 6 PRs paid that way
   ([#9667](https://github.com/DanielH2018/server/issues/9667)).

   The split never costs a run more than every member landing alone would. A batch of four
   whose red PR is the holder's costs its run and the holder's, and its three members go
   back to the line to be stacked again. One whose red PR is a member costs what it cost
   before. A member goes back to the line only behind a holder whose own run is red, so a
   red member is stacked again only while every holder it is stacked behind is red too.

   Red batches ran live, all from landings on daniel-box
   ([#8983](https://github.com/DanielH2018/server/issues/8983)):

   - **The earliest in the surviving logs, on 2026-11-15.** #8905's landing stacked #8904 and #8906. The batch's run
     on `2c599e97` was red, and the holder's log reads `the batch is not green: its run is
     red (2c599e97: CI RED); each of its PRs lands alone`.
   - **Every member alone, on 2026-11-22 at 23:14 UTC.** #10012's landing stacked #10013,
     and the batch's run on `cbe30f3b` was red. #10013's log reads `a batch that tested PR
     #10013 was not green; it lands alone`. #10013 took the turn, got a run of its own, and
     ended `settled`. #10012's own run was red, so its landing ended `pr-ci-red`. No later
     landing stacked either PR.
   - **The split on the holder's own run, on 2026-11-23 at 13:47 UTC**, the first red batch
     after `_settle` landed. #10046's landing stacked #10048, and the batch's run on
     `bfa6dc98` was red. #10046's own run passed, so it merged and its log reads `PR
     #10046's own run was not red, so #10048 each land alone`. #10048's log reads `a batch
     that tested PR #10048 was not green; it lands alone`. Its own run was red, so its
     landing ended `pr-ci-red`. The red PR was the member, and no later landing stacked it.

Four facts about the workflows shaped those steps:

- Only the `prek` gate reports on a push to master. The other three required contexts
  report on a pull request alone, and two of them, `pull + boot changed images` and
  `flux-deletion-drill`, run on a draft's pushes. A draft member therefore lacks only the
  gate, and GitHub refuses to merge a draft, so the landing marks it ready and its run must
  pass the gate. Without step 1 that run is a full sweep.
- The render-diff and cluster-pin gates take the run's base as `HEAD^1`, so on a batch they
  judge the members together. Two rules can refuse a batch whose members each pass alone:
  the pin rule above, and the rule that refuses a new edge beside a change to its
  dependent. Such a batch is a red batch.
- A squash merge of each member keeps one commit a PR on master, with the PR marked merged.
  Merging the batch's own pull request would be one run and no member runs at all, and it
  was refused: a rebase merge leaves the members' PRs closed and unmerged, and a merge
  commit puts every commit of every member's branch on master.
- `promote` publishes a bundle for a commit only when its `images` job runs, and a newer
  pending `images` job evicts an older one. When a batch's commits reach master within a
  minute, the middle ones can get no bundle, and `decide` promotes the newest commit that
  has one, which is usually the batch's last and so the tree the batch's run tested. A
  landing of a middle commit already waits for a promotion of a later commit to cover it.

What the queue is worth is an estimate. The first measured row is one afternoon's counts on
2026-11-12, before any batch ran, so every run it counts is a full run. The batch row assumes the 3.7% red rate above, with a red batch run again in
halves:

| Configuration | Full runs per merge on the hosted pool |
|---|---|
| After Phase A, measured | 2.1: 1.5 pull-request runs and 0.7 full master runs |
| A queue of one | About 1.0 |
| Batches of four | About 0.3 to 0.4 |
| Batches of 2.8 on average, measured 2026-11-19 to 2026-11-20 | 1.38: 1.01 pull-request runs, 0.23 batch runs, 0.11 Renovate runs and 0.03 master runs |

*Runs per merge on 2026-11-19 and 2026-11-20, attributed* says where the measured row's
runs went.

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
  sweep tested. A run of the real workflow records its own, so the App has no use. Nothing
  read its private key. On 2026-11-14 the operator removed the key from
  `secrets/secrets.yml` and deleted the App, number 5745632.
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

#### What B4 measured, and D10

B4's text predated D9, so three questions came first. The measurement is the 26 master push
runs of `ci.yml` from 22:50 UTC on 2026-11-14 to 00:34 on 2026-11-15, each set beside its
PR's timeline and landing log:

- 10 runs skipped because the tree was tested. Five followed the stale-base update of a
  ready PR inside the turn, three the turn's update of a draft, one a draft whose master had
  not moved, and one a merge of master a session pushed.
- 16 swept in full. Each followed a ready PR merged without an update, because master's
  changes missed its paths: 15 landing logs read `merging without a branch update`, and the
  sixteenth log was gone with its worktree. None followed a landing that skipped the turn, or a hand merge.
  Every landing in the window took the turn, and none was paced.
- No batch formed: a ready holder never stacks drafts, so each draft took a run of its own.
- From merge to verdict, a landing held its slot a median of 222 s and a p90 of 468 s.
  The fan-out lander was refused a slot four times from 23:41 to 00:11.

**D10, ruled by the operator on 2026-11-15.**

- **What tests a ready PR on the master of its turn.** The landing converts a green ready
  PR that master moved past to draft and updates its branch, so it stands in the line
  (`stale_base.rejoin_line`). The overlap rule of 2026-10-29 had saved a PR run; under the turn
  the untested merge costs a full master sweep instead, so it saves none. Updating every
  stale ready PR inside the turn was refused: each update holds the turn for a whole run.
  **Amended by the operator on 2026-11-15:** a green ready PR that master moved past while
  it waited for the turn gives the turn back once it takes it, and rejoins the line as a
  draft (`merge.await_merge`). It holds no turn for its run, and a holder can stack it. Four
  of the nine master sweeps measured under load came from such PRs.
- **Pacing is deleted.** A landing that holds the turn skips it, so it held nothing after
  18:20 UTC on 2026-11-14, and 39 holds before that were all of landings without the turn.
  The slow-runs drop in `live_limit.py` stayed until the count was decided, and went with
  the per-host count.
- **The count.** Six lock files on daniel-box, beside the turn. The turn is the slot before
  the merge, because only its holder polls PR CI then, and the holder never waits for a slot
  while it holds the turn. Every landing, the holder and each batch member, takes a counted
  slot after its merge and holds it to its verdict. A landing that cannot reach daniel-box
  counts on its own host, as before. The number is 6, the old total of 3 a host: the
  operator measured about 1,700 of 5,000 core calls and 2,250 of 5,000 GraphQL calls an hour
  with 6 to 7 landings live on 2026-11-14.
- **A landing-code PR may be a batch member.** 72 of 310 master commits touch landing code.
  The holder compares its code with master once, when the batch forms (#8768), so its own
  merge no longer strands its members. Such a PR lands as a draft through the turn with
  `LAND_BATCH=0`, not with the turn switched off: a merge without the turn during a batch's
  run breaks that batch, which is why B4's last step removed that switch.

#### What B4 changed, measured

The window is 02:40 to 13:50 UTC on 2026-11-15, from the deletion of pacing (#8821) to the
last B4 merge but one. The baseline is the window D10 measured, 22:50 UTC on 2026-11-14 to
00:34 on 2026-11-15. Both counts come from GitHub's run and job listings. The window under
B4 is thin: 9 merges in 11 hours, mostly overnight. Re-measure over a busy day before
ruling on anything from it.

- **Full CI runs per merge: 2.0, against 1.76.** Under B4, 18 pull-request runs ran jobs and
  no master run swept, for 9 merges. The baseline had 29 and 15 for 25 merges. Every full
  run moved to the pull-request side. One session's branch, which two of the 9 PRs shared,
  ran 8 of the 18; the other 7 merges took 10, or 1.43 each. Neither window held a
  batch member's three-job proof run, so both figures count full runs only.
- **Master runs that skipped as tested: 10 of 10, against 10 of 25.** A run counts as
  skipped when it ran 2 or fewer jobs.
- **The waits.** In the landing logs on both hosts, 9 landings took the turn: 8 after 0 s
  and one on daniel-server after 210 s. 5 landings took a landing slot, all after 0 s and
  all on daniel-box; the slots have existed since 13:00. No log reads that a landing waited
  for a slot or went on without one.
- **The 20-job cap: 0.9 minutes at it, against 25.4.** That is about 0.1 minutes per merge
  against 1.0. The peak was 20 in both windows. A job counts while it runs between its start
  and end, and only when it ended in success or failure: a cancelled job carries a start
  time it never ran from, and counting those reads a peak of 34.

#### B4's paths, run live

**A landing from daniel-server reaches the turn, the line and the serial lock over ssh.** The
landing of #8841 on 2026-11-15 started on daniel-server with `--fresh`. It stood in the line
on daniel-box, took the turn after 240 s and merged on one run. The serial check runs inside
the turn and prints nothing when it passes. Had its ssh session failed, the log would read
`did not answer for the landing slots`, and the log carries no such line. #8841 was docs-only
and so took no slot, because a landing takes its slot from step 2. A direct call of
`slots._locker("daniel-server")` from that landing's checkout took slot 0 over ssh, and
`lslocks` on daniel-box listed the holder.

**A batch formed with no `LAND_BATCH` set.** The landing of #8836 took the turn at about
14:03 UTC and stacked #8835, a green ready PR that its own landing had sent back into the
line (`stale_base.rejoin_line`). One run of 420 s tested both, and they merged at 14:11:07
and 14:11:50.

**A merged member then took the turn.** The holder writes `merged` into a member's place and
frees the turn. The member's landing tried the lock before it read its place, so it took the
freed turn and began to form a batch for a PR that had already merged. A check on stale
landing code stopped it there. A member that takes the turn with its place reading `merged`
now gives the turn back at once (`turn._wait`).

#### B4 under load, measured

The window is 13:50 to 21:07:07 UTC on 2026-11-15, from the last B4 merge to option B's
merge (#8887), which changed how daniel-server batches land. It held 37 merges and 34
landings, against 9 merges in the window B4 first measured. The counts come from GitHub's run
and job listings and from the landing logs of both hosts, as before.

- **Runs with more than two jobs per merge: 1.78.** 57 pull-request runs ran more than two
  jobs, and 9 master runs swept in full. Four of the 57 were batch runs, and three belong to
  PRs merged after the window. A recount from GitHub's job listings found 9 three-job proof
  runs of batch members in the window, which *Runs per merge on 2026-11-19 and 2026-11-20,
  attributed* explains. If all 9 are among the 57, full runs per merge read 1.54.
- **Master runs that skipped as tested: 28 of 37.** The 9 sweeps came from two docs-refresh
  PRs (#8848, #8880), three daniel-server fan-out PRs the server lander merged (#8858, #8862,
  #8865), one Renovate PR (#8828), two ready session PRs that master moved past while they
  waited in the turn (#8838, #8839), and #8861, below.
- **Batches form under load.** Four formed, of 2, 3, 2 and 2 PRs: #8836 with #8835, #8866
  with #8856 and #8868, #8872 with #8870, and #8882 with #8887. Of the drafts that landed
  alone, 13 took the turn with no other draft in the line, and two formed no batch because
  their landing code was older than master's. Nine landings were ready PRs, which no batch
  stacks: three daniel-server fan-out PRs, which option B moves to drafts, three cron PRs,
  and three sessions' PRs opened ready.
- **The waits.** 11 of 29 landings took the turn after 0 s, and the longest wait was 1020 s.
  Every one of 28 landings took a slot after 0 s.
- **The 20-job cap: 24.1 minutes at it**, with a peak of 20, against 0.9 in B4's first
  window and 25.4 in D10's. Per merge that is 0.65 minutes, against 0.1 and 1.0.

**A rejoined PR got its run outside the turn.** `stale_base.rejoin_line` converts a stale
ready PR to draft and updates its branch. The draft's own run on the new head is still running
for its first seconds, and `merge._ready_run_on` read that run as a ready one. The landing then
skipped the line, marked the PR ready and got a full run outside the turn. Master moved again
during that run, so the PR rejoined and repeated: #8879 rejoined three times and #8867 twice.
#8861 then merged 24 s after another PR, on a tree no run tested, and master swept in full.
About five full runs and one sweep in the window came from this. A rejoined head now counts as
untested (`merge._start_run`), because its update made it while the PR was a draft.

#### After option B and the turn fixes, measured

The window is 22:01 UTC on 2026-11-15 to 01:55 on 2026-11-16, from the merge of the give-back
(#8908) until 30 landings after it had ended. It held 28 merges. The counts come from GitHub's run and
job listings. Batches are counted from their `queue/` pull requests, because the copy of
daniel-server's landing logs collected none. The per-landing counts in the section above read
daniel-box's logs only, and its batch count matches GitHub's.

- **Runs with more than two jobs per merge: 1.75**, against 1.78 under load. 47 pull-request
  runs ran more than two jobs, and 2 master runs swept. The recount found 6 three-job proof
  runs in the window. If all 6 are among the 47, full runs per merge read 1.54, the same as
  under load.
- **Master runs that skipped as tested: 25 of 27**, against 28 of 37. The sweeps left are
  Renovate's digest bumps, whose PRs open ready.
- **Batches: three**, of two PRs each: #8911 with #8903, #8909 with #8913, and #8923 with
  #8922. #8911's batch is the first a daniel-server pipeline formed under option B.
- **The 20-job cap: 18.4 minutes at it**, 0.66 per merge, against 0.65 under load. It came in
  bursts: 11 of those minutes fell in the hour from 01:00 UTC. Pull-request runs of sessions
  and fan-out batches, image-smoke and promote held the slots, and cancelled runs held almost
  none.
- **The waits.** Every one of 21 landings took a slot after 0 s, and the longest wait for the
  turn was 570 s.

Option B ran live. Six daniel-server pipelines landed their own drafts with an empty
`land_hold`: #8911, #8913, #8921, #8923, #8936 and #8940. The pipeline for #8949 left it for a
person, because it changes `.github/workflows/ci.yml` and `.claude/rules/generated-docs.md`.
#8936's landing ended `pr-ci-red`, `land_draft` resumed its agent once, and its record reads
`failed after one resume: pytest (shard 2 of 6)`. That is the `pr-ci-red` branch the
remaining-work brief listed for daniel-box, and the code is the same on both hosts.

**A draft was marked ready before its branch update.** `merge._start_run` marked a draft ready
and then updated its branch, so the flip started a run on the stale head. The update's push
cancelled it, minutes later: #8921's run on `051b6a86` went from 23:05:05 to 23:07:11 before
the run it merged on started. The landing now updates the branch first, waits up to
60 s for GitHub to show the new head, and then marks the draft ready.

The reorder ran live on 2026-11-22
([#8984](https://github.com/DanielH2018/server/issues/8984)). Master had moved 46 paths past
#9979's base when its landing on daniel-server took the turn. Its log reads `updating its
branch so CI tests it against master before the merge`, and the next line reads `PR #9979
was a draft; marked it ready`. GitHub lists only skipped draft runs and one
`session-milestone` run on the old head `8423d4ce`, and none of them was cancelled. The
landings of #10012, #10013, #10046 and #10048 logged the same order, and their old heads
hold no cancelled run either.

#### Runs per merge on 2026-11-19 and 2026-11-20, attributed

The window is the one *What 2026-11-20 measured* reads: 04:00 UTC on 2026-11-19 to 20:00 on
2026-11-20, with 217 pushes to master. The counts come from GitHub's run and job listings,
each pull request's timeline of `ready_for_review` and `convert_to_draft` events, the
`queue/` pull requests' member lists, the landing verdicts of both hosts, and the fan-out
review records of both hosts. A recount listed 332 pull-request runs, 28 Renovate runs and
416 runs in all that ran more than two jobs, against the 328, 26 and 410 of the first count;
the six are runs that finished after the first listing.

**Full runs per merge: 1.38, not 1.89.** A batch member's own run has exactly three jobs:
`prek`, `prove the pushed tree was tested` and `renovate config validator`. It uses about
30 slot-seconds, against 1,408 for a full pull-request run at the median. The count of runs
with more than two jobs takes in 117 of these proof runs, 112 on pull requests and 5 on
Renovate's, and that count rises as batching improves. With them left out, 299 full runs
ran for the 217 merges: 220 on pull requests, 49 batch runs, 23 on Renovate's pull requests
and 7 on master. Measure a change to the queue by full runs.

**Batches formed for 119 merges.** 49 batches ran, of 2 PRs (19), 3 (20) and 4 (10), and 43
were green. 94 of 208 session and fan-out PRs and 4 of 9 Renovate PRs landed alone. Batch members are
counted from the `queue/` pull requests' titles.

The 220 full pull-request runs, each given one cause:

| Cause | Runs | Share of their slot-seconds |
|---|---|---|
| A held daniel-server fan-out PR was marked ready at the end of its review, and its run went stale before a person landed it | 52 | 26.7% |
| The landing run of a PR that took the turn with no other draft in the line | 50 | 19.9% |
| A green run on a ready PR went stale, and `stale_base.rejoin_line` converted the PR back to a draft | 36 | 16.3% |
| The landing run of a held daniel-server fan-out PR that landed alone | 18 | 9.1% |
| A red run, after which the PR was fixed and run again | 16 | 8.0% |
| A run that a later push cancelled | 13 | 6.7% |
| The landing run of a member of a red batch | 15 | 6.7% |
| The landing run of a PR whose landing code was older than master's | 7 | 2.8% |
| A PR opened ready: its runs before the landing, and its landing run | 8 | 2.8% |
| A green run followed by another run before the merge | 3 | 1.1% |
| A PR that merged after the window or not at all | 2 | under 1% |

The rows read in three groups:

- **93 runs are the one run a PR that lands alone owes.** The plan's estimate assumed few
  of them, and the queue formed batches for little more than half the merges. Most PRs that
  landed alone took the turn with no other draft waiting.
- **125 runs are extra: a PR's second run or later.** None of them tested the tree that
  merged.
- **2 runs belong to PRs outside the window.**

**The largest cause is a held PR, at 70 runs and 35.8% of the slot-seconds**
([#9896](https://github.com/DanielH2018/server/issues/9896)). 39 of the 70 daniel-server
fan-out PRs that merged in the window were held for a person. 38 of them were held because
they change a path the server lander never lands unattended (`own_surface.pr_problems`), and
one because it changed 100 files. `review_ci.ready_gate` marks a held PR ready when its
review ends, so it gets a full run at once. By the time a person lands it, master has moved,
and the landing's `stale_base.rejoin_line` converts it back to a draft. The 39 held PRs
spent 79 full runs, 2.0 a PR, counting 9 red or cancelled runs. 24 of them still merged as
batch members, whose own run is a proof run, so their early run bought nothing. Had each
held PR stayed a draft until its landing, about 55 fewer runs would have run, which is
about 1.12 per merge. The operator ruled option 1 on 2026-11-22: a held PR stays a draft
until a person starts its landing, so the landing's turn gives it its one run
(`review_ci.server_lands` sets `person_lands`).

Two of the plan's candidates were small. PRs opened ready cost 8 runs. Holders whose landing
code was older than master's cost 7.

## Phase C, not planned: runner pods for `hooks` and `pytest`

**The operator ruled on 2026-11-20 that CI gets no self-hosted runner**, and closed
[#8982](https://github.com/DanielH2018/server/issues/8982) as not planned. The same day the
operator ruled out a paid GitHub plan, whose Pro tier allows 40 concurrent hosted jobs. The cap
is therefore relieved only by taking fewer slots. *What 2026-11-20 measured* below holds the
evidence and what would reopen the question.
[ADR-0032](adr/0032-cis-heavy-jobs-may-run-on-runner-pods.md) stands: it sets the conditions
a runner would exist under, and no runner exists.

**The operator took Phase C out of the plan on 2026-11-18.** The plan ends
with Phase B. The cap still bound for 18.4 minutes in the window after option B and the turn
fixes, in bursts (*After option B and the turn fixes, measured*). This section keeps C2 and C3
as they were planned, as history.

### What 2026-11-20 measured

The re-measurement of 2026-11-19 on #8982 said that a busy day of merges after `763092f`
would rule on Phase C. The window is 04:00 UTC on 2026-11-19 to 20:00 on 2026-11-20, which
held 217 pushes to master. The counts come from GitHub's run and job listings, by the method
of the sections above. The job listings of every workflow were read only for the 9.5 busiest
hours, 17:00 UTC on 2026-11-19 to 02:30 on 2026-11-20, which held 104 merges. Reading all 40
hours would have spent most of an hour of the API quota that live landings share.

- **The 20-job cap: 99.0 minutes at it in the 9.5 hours**, which is 17% of the window and
  0.95 minutes per merge, against 0.66 when Phase C was set aside. The window is busier than
  that one, at 11 merges an hour against 7. No regression is behind the figure: 100 of the
  104 master runs skipped as tested, and 210 of 217 over the 40 hours.
- **The waits.** Over the 40 hours a CI job waited 2 s for a runner at the median, 83 s at
  p90 and 387 s at p99, and 88 of 5,784 jobs waited over 300 s. In the 9.5 hours waiting was
  31.1% of the CI jobs' wall time.
- **What held the slots at the cap.** Pull-request runs held 50.7%, and their `hooks` and
  `pytest` jobs 31.5%. Landing batches held 15.7%, and their `hooks` and `pytest` jobs 7.5%.
  Renovate's pull requests held 8.5%. Four workflows beside `ci.yml` held 17.7%: the
  Bootstrap drill 8.3%, the Flux deletion drill 5.0%, `render-diff` 2.9% and `image-smoke`
  1.5% ([#9650](https://github.com/DanielH2018/server/issues/9650)). `promote` held 4.8%
  and master pushes under 1%.
- **Runs per merge: 1.89, and full runs per merge 1.38.** 410 runs ran more than two jobs
  for the 217 merges: 328 pull-request runs, 49 batch runs, 26 Renovate runs and 7 master
  runs. 117 of a recount's 416 were a batch member's three-job proof run. The estimate for batches of
  four was 0.3 to 0.4. *Runs per merge on 2026-11-19 and 2026-11-20, attributed* gives each
  run its cause. A run costs less than it did:
  the median pull-request run used 1,039 slot-seconds, against 1,414 for a run with no host
  change on 2026-11-14.
- **The heavy jobs' share fell.** `hooks` and the `pytest` legs were 57.9% of CI's
  slot-seconds, against 1,243 of one full run's 1,424 on 2026-11-13, because a pull request
  runs a selection.

Three facts decided against runner pods:

- **Pods reach little of what holds the cap.** The launcher starts pods only for a run it
  dispatched, so pods serve batch runs and master pushes. The `hooks` and `pytest` jobs of
  those runs held 8.3% of the slots at the cap and were 11.9% of CI's slot-seconds.
- **A pod is slower than a hosted leg.** A hosted `hooks` or `pytest` job ran for 145 s at
  the median, each leg on a machine of its own. One pod on daniel-box ran the whole suite in
  209 s on 8 workers, before the pod starts and the runner registers. A pod therefore wins
  only when the hosted wait is over about 100 s. For batch and master runs the waits past
  100 s summed to 7.3 minutes in the 9.5 hours, which is 4.2 s a merge.
- **The hosts are loaded when the cap binds, and short of memory.** The cap binds during
  fan-out bursts. From 18:00 to 20:00 UTC on 2026-11-19 daniel-box ran at 40% CPU with 8.2 GiB
  available at its lowest. One run's heavy jobs need about 5 GiB under `kubepods`, outside the
  fleet's bound, and both hosts fell below the 8 GiB floor in the same fortnight
  ([#9199](https://github.com/DanielH2018/server/issues/9199)).

The question reopens on one of three changes. The heavy jobs of launcher-dispatched runs come
to hold more than about a third of the slots at the cap. The operator reopens the runner
controller's credential or paid capacity. The repository goes private, which is the question
of [ADR-0018](adr/0018-the-repository-stays-public-and-ci-stays-hosted.md).

### Phase C as it was planned

**D8 stayed closed, ruled by the operator on 2026-11-15**, after the measurement under load.
The pool spent 24.1 minutes at the cap in that window, in 245 spans of at most 0.7 minutes,
and 17.3 of them fell from 19:00 to 21:00 UTC during a burst of daniel-server fan-out
batches. The measurement after option B, the rejoin fix and the rejoin at the turn found
18.4 minutes in a window of 28 merges.

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
   a probe sets each beside the hosted run of the same commit. The suite's Docker tests already
   run in a hosted job of their own, `image-leg.yml`, so the `pytest` job needs no Docker
   daemon. The nightly sweep runs those tests as well, so it would stay on a hosted runner.
   The slice ends after a week with no shadow job green where its twin was red.
1. **C3, paused.** A repository variable routes the heavy jobs of a queue run to the pods. A
   value of 0 sends every job back to the hosted pool, and a watch sets it to 0 when no
   runner answers.

A pull-request run would stay hosted, because the launcher starts pods only for a run it
dispatched. The pods would therefore serve one or two runs an hour and sit idle for the rest.

The survey for C2 found costs the plan had not counted. The list below gives each one and
what has changed it:

- **The jobs needed root.** A pod under the `restricted` standard has no `sudo` and cannot
  write a system path. Steps that differ between a pod and a hosted runner would be a second
  copy of the environment, which is how the sweep went wrong. Since 2026-11-19 the two jobs'
  own tool installs need no root
  ([#9126](https://github.com/DanielH2018/server/issues/9126)).
  `.github/actions/pytest-tools` puts SOPS, age and apt-utils in `~/.local/bin`, which the
  runner image has on PATH. It refreshes stale apt lists into a directory under
  `$RUNNER_TEMP`, not `/var/lib/apt/lists`. `hooks` unpacks Vale into `$RUNNER_TEMP/bin`.
  One use of root remains: `.github/actions/setup` installs `libyaml-dev` when it is
  missing, so a pod's base image carries it. The same action adds the fallback apt mirror to
  `/etc/apt/apt-mirrors.txt` through `sudo -n`. A runner that refuses sudo keeps its list
  and prints a notice.
- **A pod needs a base image and a writable home.** The base image carries what the hosted
  image ships and the jobs do not install: `systemd-analyze`, Go, `jq`, `libyaml-dev` (PyYAML
  ships no wheel for the pinned Python), and the binaries test modules skip without. It puts
  `~/.local/bin` on PATH. The last step of `pytest-tools` fails a leg that lacks one of those
  binaries, so a pod's run cannot pass the gated tests as skips.
  `tests/repo/ci_workflows/test_ci_baseline_binaries.py` holds the step's list against every skip gate in
  the suite. `strace` is the one gated binary the hosted image lacks, so its test skips on both
  runner types. One gate reads a fixed path the census cannot see: a `scripts/lib` kubectl
  test skips unless `/usr/local/bin/kubectl` exists, so the base image installs kubectl there. `HOME` must be writable, because `~/.local/bin`, the dotfiles packages and
  the real `uv`'s cache live under it; setting `UV_CACHE_DIR` moves the cache. Child
  processes write `__pycache__` beside first-party modules in the checkout. A measurement on
  2026-11-19 left open whether the suite needs a writable checkout.
- **A quota is allowed in one namespace.**
  `test_no_resourcequota_is_declared_outside_the_runner_namespace` refuses a ResourceQuota
  anywhere but `ci-runners`, the runner namespace ADR-0032 bounds with one.
- **The heaviest module rendered the tree twice.** `test_render_k8s.py` hands the
  validator each role's render from the run's shared k8s render, which other modules on the
  leg build anyway. Its tests took 12.8 s on daniel-box and take 1.8 s. A leg where it is
  the only reader of that render still pays the render once, 6.6 s on daniel-box.
- **The launcher's token takes operator steps.** It would reach daniel-box as the read-only
  token does: copied from the cluster into SOPS by hand, then synced to the host.

The order of work is R1, C1, B2, B3 and then B4.

The decisions, all the operator's:

- **Whether a runner may run in the cluster. Decided on 2026-11-13: yes, in a pod.** ADR-0018
  keeps every job hosted, because a runner on the host would run a fork's code beside the age
  key, a `gh` token and a cluster node. A runner pod holds none of the three: it has no host
  mount, no ServiceAccount token and no privilege, and it runs one job. Two things stay
  true. No egress NetworkPolicy fences the runner pods, so a job in a pod can reach
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

- [Landing a merged PR](landing.md) holds the turn, the line and the batch as built.
- [Self-hosted runner spike](archive/self-hosted-runner-spike.md) holds the 2026-09-22
  measurements behind ADR-0018.
