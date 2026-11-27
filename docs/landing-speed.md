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

## The measured breakdown

Each phase below is summed over the day's landings.

| Phase | Source | Sum | Median | Notes |
|---|---|---|---|---|
| Fresh checkout and `.venv` | logs, `docs/worktree-storage.md` | under 1 s each | 0.3 s | `Installed 66 packages in 231ms` |
| Waiting for the landing turn | `turn_wait` | 9.4 h | 195 s | p90 2130 s, max 5520 s |
| Waiting behind a serial landing | `serial_wait` | 3.9 h | 0 | 13 waits of 1320-1440 s, two of 2280 and 2760 s |
| PR CI, ready to green | `ci.yml` runs | 7.6 h of runner time | 281 s | p90 404 s; 13 red runs |
| Master CI after the merge | `wait_ci` | 1.2 h | 21 s | the tested-tree skip works |
| Promotion, soak and health gate | `deploy` | 9.4 h | 501 s | `settled` median 962 s; `nothing-to-deploy` median 206 s |

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
   to 16:05. Each hold refused every k8s PR and ended five landings `deploy-failed`. The
   operator clears a hold, so this page does not propose a change to it.

PRs arrived faster than the turn could serve them: 46 `ci.yml` pull-request runs were created
between 14:00 and 15:00. When a serial landing is live, the turn serves only docs- and
tests-only PRs, so the line keeps growing until the serial landing ends.

The log line `another landing has held the turn for 5520s, reaching merge_timeout` reports
how long the waiting landing waited. Several holders had the turn in that time.

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
   queued keeps the wait going. Saves up to a merge budget (5520 s) of turn time per
   occurrence. The change adds 10 lines.

## Options that need the operator, ranked

Each option is ranked by the turn time it frees against the complexity it adds. The ranking
uses the day's numbers.

1. **A serial PR yields the turn while others stand in line**
   ([#10284](https://github.com/DanielH2018/server/issues/10284)). The serial lock is held
   from the serial draft's turn to the end of its deploy, about 23 minutes plus the drain.
   With 11 PRs in line, those 23 minutes cost 11 waits of 23 minutes. If the serial PR went
   after the line instead, one landing would wait. Up to the 3.9 h of `serial_wait` measured
   today would become one serial PR's wait. Complexity: one rule in `slots._bar_after_waits`
   or `batch._members`, bounded by the serial PR's merge budgets. The cost is the serial PR's
   own latency, and it reorders landings. It weakens no check.
2. **Exclude from the drain the landings that gate no tag.** 30 `nothing-to-deploy` landings
   waited a median of 206 s, and up to 1303 s, on the promotion of `homelab` alone. A serial
   PR's drain waits for them too. Ending them at master CI green would shorten drains and free
   slots. It weakens a check: such a landing would no longer see a hold or a failed promotion
   that includes its commit. Ask the operator first.
3. **Shorten the soak.** A `settled` landing's deploy has a median of 962 s, most of it
   promotion and soak. The deploy runs after the merge, so a shorter soak frees no turn time
   except inside a serial drain. It weakens a safety check, so it ranks last.
4. **Narrow which `k8s/services.yml` edits are serial.** The operator ruled on 2026-11-19 that
   every edit is serial (`live_limit.py`, `DECIDED:`). Nothing measured today reopens that
   ruling. #10458 adds one CronJob and holds the serial lock for it.

## What does not need changing

- **The fresh checkout and `.venv`.** PRs 10477 and 10487 moved them to disk.
  `docs/worktree-storage.md` measures `git worktree add` at 0.32 s and `uv sync` at 0.25 s.
- **Master CI.** The tested-tree skip makes the median wait 21 s.
- **PR CI.** Batching already shares a run among up to 4 PRs. The run's 281 s median is not
  the bottleneck. GitHub's 20-job cap stays, because the operator ruled out a paid plan and
  self-hosted runners.
