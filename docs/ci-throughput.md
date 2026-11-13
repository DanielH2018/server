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

### Phase B: a merge queue on daniel-box that tests the merged tree

The queue orders the merges and runs one sweep of each stacked tree on daniel-box. Landings
keep merging their own pull requests, so every rule in `scripts/deploy_tools/land_lib/` stays
where it is. The queue then records the tested tree, and the push run's `prove` job skips the
hosted sweep. A merge costs about 1.1 hosted runs in place of 2.1.

1. **B1.** A shadow sweep of master records each swept commit's verdict and duration. It
   changes no behaviour, and it lands in three parts.
    - **B1a, landed.** `scripts/deploy_tools/local_sweep.py <commit>` sweeps one commit by
      hand. *What the local sweep runs* below has its checks and its cost.
    - **B1b, landed.** `homelab-local-sweep.timer` on daniel-box sweeps `origin/master` 15
      minutes after the last sweep ended, and each commit at most once. *The shadow sweep*
      below has how it runs and how to read it.
    - **B1c, landed.** `probe.py sweep-agreement` compares each sweep's verdict with the
      hosted run of the same commit, and says where B2's gate stands.
    - **B1d, proposed.** Sweep the trees of pull requests that hosted CI read red, so the
      comparison holds hosted reds. The gate below says why master cannot supply them.
1. **B2, planned.** A queue of one, behind a flag, for the fan-out server lander. The local
   sweep replaces the branch-update re-test.
1. **B3, planned.** The queue records the tested tree before each merge, and
   `scripts/dev/tested_tree.py` accepts that record.
1. **B4, planned.** Batches of up to four, with a bisect when the stacked tree is red.
1. **B5, planned.** Every landing goes through the queue, and the merge pacing and the
   branch-update re-test are deleted.

B2 waits for B1 to run for 7 days and to compare 600 checks, which is 300 swept commits. No
check may read green locally and red hosted, and the p90 sweep must take 10 minutes or less.

The comparison must also hold 20 checks that hosted CI read red. A sweep can only miss a red
tree when hosted CI read one red, so a window of green hosted runs shows how often the sweep
raises a false alarm and nothing about what it catches. Master cannot supply 20 reds: 4 of
its 526 CI runs failed in the week to 2026-11-13, and most of its green runs are proven skips
that ran neither job. `GATE_HOSTED_REDS` in
`scripts/diagnostics/probe_lib/sweep_agreement.py` is the count.

Three decisions are the operator's, and all three are made:

- **Who records the tested tree. Decided on 2026-11-13: a GitHub App.** `prove` trusts a
  record by its creator, and GitHub sets a status's creator from the token that wrote it. No
  personal token can write as `danielh2018-homelab-tested-tree[bot]`, so a session cannot
  record its own tree. The App is number 5745632, installed on this repository alone as
  installation 181619225, and commit statuses are its only write permission. Its private key
  is `tested_tree_app_private_key` in `ansible/vars/secrets.yml`. Nothing reads the key
  before B3. The key is as safe as that file: a session that can decrypt it can record a
  tree.
- **What a batch proves. Decided on 2026-11-13: the commits before its last.** A batch sweeps
  only its last stacked tree, and that sweep stands for the commits before it. An earlier
  commit of a batch can therefore be red by itself. `scripts/deploy_tools/await_ci.py`
  already lets a descendant's verdict stand for a commit that has none. The alternative was
  to record only the last commit and leave the others to their hosted push run.
- **Whose memory the sweep uses. Decided on 2026-11-13: the fleet's.** daniel-box has no
  unbudgeted memory, so the sweep runs inside the agent fleet's bound on `user.slice`, with
  a `MemoryHigh` of its own. No budget line moves. A sweep costs the fleet about one fan-out
  batch of memory for the five minutes it runs. The alternative was a permanent share, with
  the fleet's bound lowered from 14G to 9.5G.

#### What the local sweep runs

The local sweep runs two of the gate's eleven jobs. `hooks` runs the command that the swept
commit's own `ci.yml` carries. `pytest` runs the whole suite in one run, which the six hosted
shards partition. `HOSTED` in `local_sweep.py` names the other nine jobs and why daniel-box
runs none of them, so a local `pass` says nothing about those. A test holds the two tables to
the gate's `needs`.

One sweep took 290 s on daniel-box on 2026-11-13, at commit `592f0c8e1`, with the checks one
after the other. Both checks passed.

| Check | Seconds | Peak memory |
|---|---|---|
| `pytest`, 20,838 tests on 16 workers | 216 | 5,990 MiB |
| `pytest` on 8 workers | 209 | 3,776 MiB |
| `pytest` on 4 workers | 321 | 2,313 MiB |
| `hooks` | 74 | 1,030 MiB |

The memory column is anonymous plus shared memory, sampled once a second, which is what the
check cannot give back under pressure. The cgroup's `memory.peak` read 8,454 MiB for the
16-worker run, because it also counts page cache.

Eight workers cost no time and hold 2.2 GiB less than sixteen, so the shadow sweep runs
eight. A whole sweep on eight workers under `MemoryHigh=4500M` took 301 s and peaked at
3,499 MiB.

The scratch tree goes under `~/.cache/local-sweep`. A tree under `/tmp` fails one test of the
Bash hook on every commit, because that hook reads any path under `/tmp` as scratch.

#### The shadow sweep

`homelab-local-sweep.service` runs the tool from the primary checkout as the deploy user, the
way the docs refresh runs its generators. It sweeps `origin/master` as `homelab-checkout-sync`
last fetched it, so it fetches nothing. Nothing reads its verdict. The component's
`component.yml` under `hosts/components/local-sweep/` holds its autonomous-role contract.

- To see whether the sweeps agree with hosted CI, run
  `uv run python scripts/diagnostics/probe.py sweep-agreement`. It reads the sweeps of the
  last 8 days from Loki and the check-runs of each swept commit from GitHub. It lists each
  disagreement, and its last line before the verdict says which parts of B2's gate are met.
  It makes one GitHub read per swept commit, about 300 for a full window, so run it by hand
  and not in a loop.
- To list the sweeps themselves, run
  `uv run python scripts/diagnostics/probe.py loki-query '{job="syslog"} |= "SWEEP-RESULT:"'`.
  Each line carries the commit, the verdict and each check's seconds.
- To see what failed in a red sweep, read `journalctl -t local-sweep` on daniel-box. The
  lines above the result line name each failed test or hook.
- To pause it, run `sudo systemctl mask --now homelab-local-sweep.timer`.

The probe sets two verdicts side by side for each of the two jobs the sweep runs. Four rules
decide the hosted one. A job that skipped beside a green gate counts as green, because the
gate passes a skip only for a tree that a pull-request run already tested. The count line
says how many of the compared checks hosted CI ran itself, because a proven skip is green by
construction. A re-run job takes its newest attempt. A job that was cancelled, or is still
running, has no verdict, and the check is counted as not comparable. Any other conclusion but
success is red, as `hosts/lib/ci_verdict.py` reads it.

The probe exits 1 only for a sweep that read green where
hosted CI read red, which is a red tree a queue would have merged. A sweep that read red where
hosted CI read green is listed with the tests that failed, because a queue would have refused
a good pull request.

The timer samples the tip of master. A commit that merges and is superseded between two runs
is never swept, and at most three sweeps start in an hour.

The unit fails only when the sweep did not happen. Under `--once` a sweep that reaches a
verdict exits 0, red or green. A sweep with no verdict exits 69, and a tool that did not start
exits 1. Nothing pages on a failed run, and a commit whose sweep died is not swept again.

The deploy user suits a sweep of merged commits and nothing else. A sweep of an unmerged
pull request runs code no review has passed, so slice B2 moves the sweep to a user that holds
no credentials before any landing reads its verdict.

That user needs more than an empty home. A sweep run with an empty home directory on
2026-11-13 passed the hooks and failed 251 tests, with 14 modules that did not import. The
import errors name `claude_worktree`, and about 190 of the failures are in the Bash-hook
tests. CI's `pytest` job installs two dotfiles packages, `claude-guard` and
`claude-worktree`, before it runs the suite. Nothing has confirmed that those two close the
whole gap.

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
