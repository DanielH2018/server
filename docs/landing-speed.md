# Landing speed: where a landing's time goes, and what would shorten it

The operator asked on 2026-11-27 for a look at speeding up the landing process. The queue had
grown to 14 landings in flight, and the landing turn was held for 36 to 48 minutes at a
stretch. This page holds that day's measurements, the two fixes that landed from them, and
the remaining options ranked by the time they free against the complexity they add.

**Recommendation: the time goes to the landing turn and the serial lock, not to CI, the
checkout or the deploy.** Two bugs made holders keep the turn without merging, and both are
fixed. The largest remaining lever is the order in which a serial PR takes the turn,
[#10284](https://github.com/DanielH2018/server/issues/10284), which needs the operator's
ruling.

## Where the numbers come from

Every landing writes one `event=landing` line to syslog, and Loki keeps it
(`docs/landing.md`, *The annotation, and the Landings board*). The landing logs carry no
timestamps, so each duration below comes from one of three sources:

- The Loki annotations from 00:00 to 19:26 UTC: 104 landings, 91 on daniel-box and 13 on
  daniel-server, of which 82 merged.
- `ghread run list -w ci.yml --created 2026-11-27`: 346 `ci.yml` runs.
- The 99 `land*.log` files of the day in `/tmp/homelab-landings-ubuntu/` on daniel-box. The
  waits each log prints (`took the landing turn after Ns`, `the serial landing ended after
  Ns`) are the only record of what happened inside the turn. daniel-server kept 6 logs, and
  no batch worktree kept a `.fanout/land*.log`.

The split of a `settled` deploy into stages, in *Where a settled deploy's time goes*, reads
three more sources:

- The job and step timings of the 280 `promote.yml` runs from 2026-11-26 to 2026-11-28, read
  through the Actions jobs API.
- The commit statuses on master's commits. Flux posts one `flux/<name>` status per
  Kustomization, and posts it again on every reconcile while `prod` names that commit, so
  only each context's first post counts. `homelab/promote-verdict` names the commit T that
  each promotion moved `prod` to.
- The soak Job's own log in Loki (`{namespace="flux-system", container="soak"}`). Its pass
  line, `soak: <R> judged <services> over <N>s; pass`, gives the window from T0, the
  moment Flux fetched R, to the end of W.

## The measured breakdown

Each phase below is summed over the day's landings.

| Phase | Source | Sum | Median | Notes |
|---|---|---|---|---|
| Fresh checkout and `.venv` | logs, `docs/worktree-storage.md` | under 1 s each | 0.3 s | `Installed 66 packages in 231ms` |
| Waiting for the landing turn | `turn_wait` | 9.4 h | 195 s | p90 2130 s, max 5520 s |
| Waiting behind a serial landing | `serial_wait` | 3.9 h | 0 | 13 waits of 1320-1440 s, two of 2280 and 2760 s |
| PR CI, ready to green | `ci.yml` runs | 7.6 h of run wall time | 281 s | p90 404 s; 13 red runs |
| Master CI after the merge | `wait_ci` | 1.2 h | 21 s | the tested-tree skip works |
| Promotion and soak | `deploy` | 9.4 h | 501 s | `settled` median 962 s; `nothing-to-deploy` median 206 s |
| Health gate and host wait | `total` less `wait_merge`, `wait_ci` and `deploy` | 0.5 h | 4 s | over the 42 `settled` landings; about 70 s for a fleet-wide tag set |

The first four phases come before the merge, and the time spent on them grows with the
line. A landing waits in the landing turn and behind the serial lock one at a time, so a wait
there delays every landing behind it. Master CI and the deploy run after the merge, in up to
6 landing slots side by side, and delay only their own landing. One exception: a serial PR
drains every landing past its merge before it merges, so a slow deploy lengthens that drain.

## Why the line grew to 14

The landing turn lets one landing at a time hold a PR's CI run and merge. A holder tests the
drafts in line with its own in one run, so a turn that works costs one CI run, about 5
minutes, for up to 4 PRs. Five things held the turn or the serial lock without a merge:

1. **Serial landings back to back.** A landing of
   [#10458](https://github.com/DanielH2018/server/pull/10458), which edits `k8s/services.yml`,
   took the turn and the serial lock at 15:04. Its log stops there with no exit recorded.
   [#10464](https://github.com/DanielH2018/server/pull/10464), a Longhorn PR, waited 1440 s
   behind it, then held the serial lock itself until about 16:32. Eleven landings started
   near 16:06 each waited about 1380 s behind it. #10458 took the serial lock again at 18:02
   and from about 19:16, so one PR held it three times in four and a half hours. Every PR
   with a path other than documents and tests waits in line while the serial lock is held.
2. **A serial PR's drain inside the turn.** A serial PR that holds the turn waits there, up to
   `slots.DRAIN_S` (2400 s), for the landings past their merge to reach their verdicts. #10458
   waited 1170 s this way, and [#10381](https://github.com/DanielH2018/server/pull/10381)
   waited 1350 s at 01:30. No other landing merges meanwhile. A `settled` landing's deploy has
   a median of 962 s, so a drain behind one or two of them lasts about 20 minutes.
3. **Green batches thrown away.** Three holders in a row,
   [#10393](https://github.com/DanielH2018/server/pull/10393),
   [#10451](https://github.com/DanielH2018/server/pull/10451) and
   [#10448](https://github.com/DanielH2018/server/pull/10448), each ran a batch that went
   green on master `a7b941a1` between about 17:00 and 17:40, then put their own PR back in the line
   without merging. [#10361](https://github.com/DanielH2018/server/pull/10361) was in all three
   batches, and it waited 2130 s of its 2160 s for the turn. On 2026-11-26 this happened once
   in 13 green batches. [#10507](https://github.com/DanielH2018/server/pull/10507) fixes it.
4. **A failing required check.** GitHub refused #10458's merge with `required check renovate
   config validator failing`, and the landing kept waiting while it held the turn. The fix is
   the second change below.
5. **`DEPLOY_HOLD`.** A hold stopped promotion from about 02:30 to 13:30, and again from 14:31
   to 16:05. Each hold refused every k8s PR. 8 landings ended `deploy-failed` in the first
   hold and 7 in the second. The operator clears a hold, so this page does not propose a
   change to it.

PRs arrived faster than the turn could serve them: 46 `ci.yml` pull-request runs were created
between 14:00 and 15:00. When a serial landing is live, the turn serves only docs- and
tests-only PRs, so the line keeps growing until the serial landing ends.

The log line `waited 5520s for the landing turn, reaching merge_timeout` reports
how long the waiting landing waited. Several holders had the turn in that time.

## Where a settled deploy's time goes

**The soak's window W takes half of a `settled` deploy, and the queue behind an earlier
promotion takes another fifth.** Neither can get shorter without changing what the soak
checks. The `promote` jobs and Flux's fetch take about a sixth between them, and nothing
there is worth building for.

`deploy` runs from master CI green to the moment `promote.py land-wait` reports the
promotion settled (`scripts/deploy_tools/land_lib/deploy.py:95`). The health gate runs after
that stamp, so it sits outside the 962 s and has its own row in the table above.

The split below covers 30 of the 42 `settled` landings of 2026-11-27. Their stages sum to
each landing's logged `deploy` with nothing left over, and their median deploy is 940 s.
The other 12 were left out for one of three reasons:

- Four had already been covered by an earlier promotion, so their deploy lasted 2 to 6 s.
- Three promoted no service and had no soak to split.
- Five settled on statuses that `land-wait` read before the commit's
  `homelab/promote-verdict` was posted, which the match requires.

The longest deploy of the day, 1893 s, is one of those five.
[#10366](https://github.com/DanielH2018/server/pull/10366) changed 46 tags, `flux` among them.
Its own soak posted `failure` at 00:46:58, which is provisional while `flux` is in the set
(`docs/flux-deploy-plane.md`, *Verification*). A later promotion, with W = 1020 s, then
settled the landing.

| Stage | Median | Sum over 30 | Share |
|---|---|---|---|
| Queue behind an earlier promotion | 1 s | 6673 s | 22.2% |
| `render` job | 46 s | 1323 s | 4.4% |
| `images` job | 13 s | 636 s | 2.1% |
| `publish` job | 18 s | 537 s | 1.8% |
| `decide`, the `promote` job's own work | 28 s | 877 s | 2.9% |
| Job starts and handoffs between the jobs | 10 s | 425 s | 1.4% |
| `tag` job, which moves `prod` | 12 s | 405 s | 1.3% |
| Flux fetches `prod` (T0) | 27 s | 905 s | 3.0% |
| Rollout, from T0 to the last judged pod's Ready | 58 s | 2730 s | 9.1% |
| The soak's window W | 240 s | 15540 s | 51.7% |
| `verify` and `land-wait` read the result | 19 s | 637 s | 2.1% |
| A run that started before `land.sh` saw CI green | -2 s | -635 s | -2.1% |
| **`deploy`** | **940 s** | **30053 s** | 100% |

What each stage is, and whether it can get faster without changing the soak or the gate:

- **The soak's window W.** The soak Job waits until W has passed since the last judged pod
  read Ready. It counts from T0 when the change restarts no pod. W is the longest `for:`
  among the critical or `verify="true"` rules that can fire for a changed service, at least
  `k8s_rollout_stabilise_seconds`, plus 120 s of scrape and evaluation
  (`scripts/renderer/renderer_lib/soak.py:fleet_wait`). Rules with no static `service`
  label give every service at least 120 s, so W is 240 s for most changes. W took four
  values across the 30 landings:
    - 240 s for 17 landings.
    - 420 s for one, set by `jellyfin`.
    - 720 s for four, set by `monitor-bridge` and `autofix-bridge`, whose rules carry
      `for: 10m`.
    - 1020 s for eight. One rule, `AlertmanagerNotificationsRateLimited`
      (`for: 15m`, `service: observability`), set it for seven of them, and `longhorn`'s
      15-minute rules set the eighth.

  Shortening W weakens the soak, so it goes to the operator first:
  [#10603](https://github.com/DanielH2018/server/issues/10603).
- **The queue behind an earlier promotion.** One promotion verifies at a time. `decide`
  waits while `prod` differs from `last-good`, and a landing whose own run did not move
  `prod` waits for a later run. 18 of the 30 landings queued for under a minute. The other 12
  waited 156 to 1120 s, most of it the earlier promotion's own W. Overlapping two
  verifications would change which revision the soak judges, so this is not a free saving
  either.
- **The rollout, a median of 58 s.** Flux applies the changed Kustomizations, and their pods
  restart. `kustomize-controller` also reconciles every unchanged Kustomization at the new
  revision, about 130 in all, with `k8s_rollout_batch_width` plus one workers. A changed tag
  can therefore wait its turn behind unchanged ones. The Ready-wait for every changed
  Kustomization, its `-verify` and `-deferred` kinds included, runs inside W, so it lengthens
  the deploy only when it outlasts W. Raising the worker count would change the rollout
  width, which is a separate decision.
- **Flux's fetch, a median of 27 s.** The `homelab` OCIRepository polls `prod` once a minute
  (`k8s/roles/flux/templates/sync.yaml.j2`). A webhook receiver could cut the wait, but it
  would add an inbound path into `flux-system` to save about 30 s.
- **The `promote` jobs before the move, about 127 s together.** The jobs are separate so that
  no job that runs repository code holds a token that can sign, push, move a tag or post a
  status. The header of `.github/workflows/promote.yml` records that rule. `render` is the
  largest job, and its `Build the bundle` step has a median of 17 s.

The deploy runs after the merge, so a faster deploy frees no landing-turn time except inside
a serial PR's drain (`slots.DRAIN_S`). There, a drain waits out the W of each landing past
its merge.

## Changes built and landed

Each change keeps every check: the turn, the stale-base rules, the soak, the verify and the
hold.

1. **Merge a green landing batch after its holder waited out a serial landing**
   ([#10507](https://github.com/DanielH2018/server/pull/10507)). A wait for a serial landing
   gives the turn back through `step_out`, which sets `turn_interrupted`. The flag survived
   into the merge, which then rejoined the line before `stale_base.batch_tested` could show
   that the batch had tested the exact tree the merge would make. The fix checks
   `batch_tested` first. Saves one batch run and one wait in line, 5 to 11 minutes of turn
   time, each time it fires. The change adds 5 lines.
2. **End a landing whose required check failed** (the PR that adds this page). A merge
   refused for a failing required check ends `pr-ci-red`. A check that is in progress or
   queued keeps the wait going. A merge budget (5520 s) of turn time bounds the saving per
   occurrence. The one occurrence in the 1010 retained logs held the turn about 6 minutes
   before its process ended with no exit recorded. That validator had concluded `failure`,
   and no landing in the logs merged after such a refusal. The change adds 10 lines.
3. **Let a landing batch hold six PRs, not four** (the PR that adds this item). The operator
   chose six. In the burst from 16:00 to 18:00 every batch was full at four while 10 or more
   PRs stood in line, and one turn merges one batch. At the 3.7% to 6.5% per-PR red rate
   `docs/ci-throughput.md` measured, a batch of four is red 14% to 24% of the time and a batch
   of six 20% to 33%. A red batch splits, and never costs more runs than its members landing
   alone.

## Options that need the operator, ranked

Each option is ranked by the turn time it frees against the complexity it adds. The ranking
uses the day's numbers.

1. **A serial PR yields the turn while others stand in line**
   ([#10284](https://github.com/DanielH2018/server/issues/10284)). The serial lock is held
   from the serial draft's turn to the end of its deploy, about 23 minutes plus the drain.
   With 11 PRs in line, those 23 minutes cost 11 waits of 23 minutes. If the serial PR went
   after the line instead, the PRs in line would merge first. PRs that arrive during the
   serial window still wait behind it, so the saving depends on how quiet the line is when the
   serial PR goes. The 3.9 h of `serial_wait` measured today is the upper bound. Complexity: one rule in `slots._bar_after_waits`
   or `batch._members`, bounded by the serial PR's merge budgets. The cost is the serial PR's
   own latency, and it reorders landings. It weakens no check.
2. **Exclude from the drain the landings that gate no tag.** 30 `nothing-to-deploy` landings
   waited a median of 206 s, and up to 1303 s, on the promotion of `homelab` alone. A serial
   PR's drain waits for them too. Ending them at master CI green would shorten drains and free
   slots. It weakens a check: such a landing would no longer see a hold or a failed promotion
   that includes its commit. Ask the operator first.
3. **Shorten the soak.** A `settled` landing's deploy has a median of 962 s, and the soak's
   window W is 52% of it (*Where a settled deploy's time goes*). The largest single term is
   one 15-minute rule, which holds every `observability` change at W = 1020 s instead of
   240 s ([#10603](https://github.com/DanielH2018/server/issues/10603)). The deploy runs
   after the merge, so a shorter soak frees no turn time except inside a serial drain. It
   weakens a safety check, so it ranks last.
4. **Narrow which `k8s/services.yml` edits are serial.** The operator ruled on 2026-11-19 that
   every edit is serial (`live_limit.py`, `DECIDED:`). Nothing measured today reopens that
   ruling. #10458 adds one CronJob and holds the serial lock for it.

## What does not need changing

- **The fresh checkout and `.venv`.** PRs 10477 and 10487 moved them to disk.
  `docs/worktree-storage.md` measures `git worktree add` at 0.32 s and `uv sync` at 0.25 s.
- **Master CI.** The tested-tree skip makes the median wait 21 s.
- **PR CI.** Batching shares a run among up to 6 PRs, 4 before 2026-11-27. The run's 281 s
  median is not the bottleneck. GitHub's 20-job cap was not binding either: see *Runner use
  and queue order* below. The operator ruled out a paid plan on 2026-11-20.
  [ADR-0032](adr/0032-cis-heavy-jobs-may-run-on-runner-pods.md) allows runner pods for CI's
  two heavy jobs, and none is built.

## Runner use and queue order

These measurements were taken after the first two changes, at the operator's request.

**Runners sat mostly idle during the burst.** Every Actions job from 16:50 to 18:00 UTC was
read: 217 runs and 1073 jobs across all workflows, all on GitHub-hosted runners.

- A median of 4.5 jobs ran at once against the cap of 20.
- Fewer than 10 ran 78% of the time, and 15 or more ran 8% of the time.
- The cap was reached once, and a job waited a median of 2 s to start, at most 17 s.
- One batch run has 12 to 15 jobs, peaks at 9 to 12 running at once, and uses about 25
  job-minutes over 4.5 to 6 minutes.

So the landing turn, not the runner pool, set the pace. A second batch run alongside the
first, which is what pipelining needs, would fit.

**A green batch's members wait little on their own required checks.** Each member is marked
ready after the batch goes green and merges once the checks outside `ci.yml` pass. This was
read across every retained log, 2026-11-22 to 2026-11-27:

- 45 member merges took 2610 s in all, a median of 30 s, which is one poll. The p90 was 180 s
  and the longest 240 s.
- The holder's own merge met at least 18 refusals in 27 batches, about 9 minutes. The log
  prints a repeated refusal once, so the count is a lower bound.
- The checks that cause the waits are `renovate config validator`, `flux-deletion-drill`,
  and the aggregate `2 of 4 required status checks are in progress`.

That is 1 to 2 minutes of turn per batch, too little to build for.

**The turn has no ordering.** Each waiting landing polls a non-blocking lock every 30 s
(`turn._wait`), so the first poll after a release takes the turn. The holder stacks drafts in
PR-number order (`sorted(os.listdir)` in `line.py`), not in arrival order. Three landings ran
out the 5520 s merge budget while later arrivals took the turn or were stacked. Not every one
of those waits comes from the ordering: #10482 is serial, so no batch can carry it, and #10449's
wait includes a serial wait. A proposed architecture, on branch `claude/landing-architecture`,
models the queue so the effect of the ordering is measured before a change is built.
