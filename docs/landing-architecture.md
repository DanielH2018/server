# Landing architecture: an explicit state machine with property-tested invariants

The operator asked on 2026-11-27 for a landing pipeline that is robust, well defined and
optimised from start to finish, and that can be tested, maintained and extended. This page is
the target architecture and the plan to reach it in slices. `docs/landing-speed.md` holds the
measurements that motivate it.

**Recommendation: split the landing's decisions into a pure core.** The core is two state
machines: one for a single landing, and one for the shared queue that the turn, the line, the
slots and the serial lock implement. Each is a pure transition function over a typed state.
Hypothesis drives both through random orders of events and checks named invariants on every
step. The GitHub, git, flock and cluster calls stay behind the `Tools` interface, and do only
what the core decides. Every safety rule, `DECIDED:` marker and operator ruling stays, and
becomes a named invariant with a test.

## Where each decision stands

- **Ruled, 2026-11-27: direct cutover.** The pre-merge loop is replaced in one slice, guarded
  by characterization traces and a differential test. No shadow mode runs beside it.
- **Open: who runs `schedule`.** *The queue: tickets over the flock* compares a distributed
  queue, a coordinator daemon and a hybrid, and recommends the first.
- **Open: how far the design reaches past the landing.** *The cycle around the landing*
  compares one machine for the whole change with a typed contract at each stage boundary,
  and recommends the second.

Nothing here is built. The validation below read the code at master `ce5c3abfa`.

## What the pipeline is, validated against the code

Each fact names the symbol that holds it.

- **Size.** `land_lib/` holds 42 modules and 10,056 lines, and `land.py` 463 more. The
  pre-merge half is the largest and most coupled: `batch` 622 lines, `merge` 604, `turn` 455,
  `slots` 446, `stale_base` 373 and `stall` 348.
- **A landing's state is implicit, and one fact has several copies.** `Landing.__init__` sets
  31 mutable fields. `merge.await_merge` adds 18 locals, and `turn.held` adds three closure
  variables. Whether the landing holds the turn is stored three times: `in_turn` in
  `await_merge`, `release` in `turn.held`, and `Landing.give_back_turn` being set. `step_out`
  changes the second and third and cannot reach the first. Two different variables are named
  `asked`, one in each of those functions.
- **A batch member has two `Landing` objects in two processes.** The member's own landing
  polls its place in the line. The holder builds a second `Landing` for the same PR
  (`batch._admit`), arms it with the holder's options, and runs `merge.await_merge` on it. The
  two processes exchange four messages, which are the answers `line._op` writes into a place:
  `merged`, `alone`, `split` and `conflict`.
- **Outcomes are raised from 68 `die` sites in 14 modules and 15 `finish` sites in 5.** No
  single place lists which state can end in which verdict.
  `scripts/deploy_tools/tests/land/test_land_every_exit_names_a_verdict.py` checks that each
  exit names one.
- **The queue has no owner and no order.**
    - `turn._wait` tries a non-blocking `flock` before every 30 s sleep. The first poll after
      a release wins, and a landing that arrives while the turn is free takes it ahead of
      every waiter.
    - `line._op` lists places with `sorted(os.listdir)`. That is the order of the file names
      as strings, which is PR-number order only while every number has the same count of
      digits. It is not arrival order.
    - `slots._bar` reads `DEPLOY_HOLD`, the no-promotion window, the slots and the serial
      lock. For a serial PR it also takes the serial lock (`slots._keep_serial`), so a
      function named as a read holds a lock until the process ends.
- **The turn is not a safety gate, by its own docstring.** A landing that waits
  `merge_timeout` (5520 s) for the turn, or cannot reach `TURN_HOST`, goes on without it. It
  then merges under `stale_base.refuse_stale_overlap` alone. So "one landing between its CI
  run and its merge" is a goal the code approximates, not an invariant it holds.
- **Every rule in `slots._bar` fails open.** `live_limit.fail_open` carries the `DECIDED:`
  marker: a read that fails bars nothing.
- **Tests assert call order on fakes.** The `land`, `land_merge` and `land_deploy` suites hold
  64 test modules and 691 tests. Most build a `Fakes` object of scripted answers and assert
  the order of recorded calls. Two state machines exist, `SlotHolders`
  (`test_land_slots_properties.py`) and `StallWait` (`test_land_stall_properties.py`). No
  test models the turn, the line or a whole landing.
- **One module already has the target shape.** `stall.step` is a pure decision over a frozen
  `Watch`, `stall.on_poll` performs the action it returns, and `StallWait` checks it. The
  target is that shape for the rest of the pre-merge half.

## What the 2026-11-27 logs show

The landing logs of that day hold 123 landings of 71 PRs, which is 1.7 landings for each PR.
Of the 90 verdicts, 31 were `settled`, 30 `nothing-to-deploy`, 11 `pr-ci-red`, 9
`deploy-failed`, 5 `refused` and 4 `merge-conflict`. Fifty-four landings re-ran from a fresh
checkout because their landing code lagged master.

Three defects lived in the gaps between the pieces above:

- A stale `turn_interrupted` flag threw away green batches
  ([#10507](https://github.com/DanielH2018/server/pull/10507), fixed).
- A turn holder waited on a failing check that could never pass
  ([#10525](https://github.com/DanielH2018/server/pull/10525), fixed).
- **A landing that steps out of the turn leaves the line**
  ([#10559](https://github.com/DanielH2018/server/issues/10559), open). `turn.held`'s
  `step_out` asks for the turn back with no draft, and `line.Place` drops the line for a
  place with no head. No holder can stack that landing, so it can only win the turn by
  polling first.

The third is the starvation the measurements showed, and its cause is not the polling race
alone. #10465 and #10449 both stepped out at about 16:09 for the serial landing. Each then
waited 5520 s outside the line and went on without the turn. Fourteen landings took the turn
during #10465's wait, and twelve of them had arrived after it. Those two waits are 3.1 h of
the day's 9.4 h of turn wait. #10472 arrived at 17:05, never stepped out, stood in the line
and was merged by a holder's batch after 2670 s. Twenty-seven landings stepped out for a
serial landing that day.

#10482 also waited 5520 s, for a different reason. It is a serial PR, so it stood in the line
and no batch may carry it.

## The target shape

```text
land_lib/
  core/                      pure: no I/O, no clock, no sleep
    state.py                 LandingState, QueueState: frozen dataclasses, one per state
    landing_machine.py       step(LandingState, Observation) -> (LandingState, [Action])
    queue_machine.py         schedule(QueueState) -> [Grant]: order, batches, serial rule
    invariants.py            each named invariant as a predicate over states
  driver.py                  loop: observe through ln.tools, step, execute, log the transition
  sim/                       replays recorded arrivals through the core to compare policies
  (every other module)       keeps its I/O; its decisions move into core/
```

The core decides and `Tools` acts. A decision such as `rejoin the line`, `merge this head` or
`give the turn back` becomes an `Action` value the core returns. The driver carries each one
out through `ln.tools`, which already puts every process boundary behind one faked interface
(`_land_fakes.py:build_tools`), and reports what came back as an `Observation`. No I/O layer
is added: a `github.py` that only renamed `Tools` would fail the deletion test.

`scripts/deploy_tools/tests/land/test_land_imports.py` requires its `MODULES` list to equal
`land_lib/*.py`, and
its glob does not descend. A module under `land_lib/core/` would fall outside the
import-direction guard without any error. The slice that creates `core/` extends the guard to
it, with a non-vacuity assertion that names its members.

## The landing state machine

One landing moves through these states. A verdict is reachable only from the states that list
it, and the test suite checks that list.

```text
Armed ─► OwnRun ─► Queued ◄────────┐
  │        │         │  ▲          │
  │        │         │  └ Yielded ◄┤   (rate limit, serial landing)
  │        │         ▼             │
  │        │      Holding ─► Testing ─► Proving ─► Merging ─► Merged
  │        │         │          │                     │          │
  │        │         │          └ Split               └ Rejoining┘ (master moved)
  │        │         └─► NoTurn ──────────────────────► Merging
  │        ▼
  └─► HandedOff          Merged ─► Resolving ─► MasterCI ─► Promoting ─► HostWait ─► Grading
```

Each state, and the code that implements it:

- **Armed.** `pr_lock.take`, then `merge.arm_merge`. The refusals are `landing-live`, a closed
  PR, the author rule, a stray closing keyword, a private repository, the landing policy and
  changes requested.
- **HandedOff.** Three paths end this process and continue in another:
  `fresh.LandFresh` before the merge, `fresh.GradeFresh` after it, and `turn.HandOver`, which
  passes the turn's locked descriptor. A fourth, `handoff.land_through_unit`, makes the whole
  landing a client of a lander unit. The verdict is the other landing's.
- **OwnRun.** A PR opened ready has a CI run of its own, and its landing polls that run
  outside the turn (`merge.await_merge`, before its first `ask`). A draft skips this state.
- **Queued.** The landing waits in `turn._wait`. A draft with no CI run stands in the line,
  where a holder can stack it. A green ready head, and a landing back from a step-out, wait
  with no place.
- **Holding.** `turn._wait` returned the lock. `slots.refuse_before_run` runs, then
  `batch.form`.
- **Yielded.** `Landing.without_turn` gives the turn back for a rate-limit wait or for a live
  serial landing, and asks for it again afterwards.
- **Testing.** One CI run covers a tree stacked on a known base: the batch's run on
  `queue/<pr>` (`batch._await_run`), or the PR's own run.
- **Split.** A batch that is not green. The holder's own run decides whether the members go
  back to the line or land alone (`batch._settle`).
- **Proving.** After a green batch each PR is labelled and marked ready, and its own run skips
  every job but still has to report the required checks. #10465 spent 210 s here.
- **Merging.** `slots.refuse_before_merge`, the pin gate, `stale_base.batch_tested` or
  `refuse_stale_overlap`, then `merge._merge_directly` pinned to the head.
- **Rejoining.** `stale_base.rejoin_line` and `rejoin_in_turn` convert the PR to draft and
  update its branch, and `merge._start_run` stands it in the line again.
- **NoTurn.** The wait for the turn timed out, or `TURN_HOST` did not answer. The landing
  marks its draft ready with no update and merges under the stale-overlap rule.
- **Resolving.** Step 1: `classify.resolve`, the stale-code check, `classify.classify` and
  `classify.shortcut_if_nothing`, which ends a PR that reaches nothing `nothing-to-deploy`.
- **MasterCI, Promoting, HostWait, Grading.** Steps 2 to 5 of `pipeline._STEPS`.
  `slots.held` holds a landing slot from step 2 to the verdict.
- **Verdict.** Terminal. Exactly one for each landing, from `outcome.Verdict`, recorded once.

The target changes four things in this machine:

- **One record holds the state.** `LandingState` replaces the three copies of whether the
  landing holds the turn, the 18 locals and the flags such as `turn_interrupted`. Testing records the tested
  tree and its base.
- **Queued always has a place.** Every waiting landing holds a ticket, and Yielded keeps it.
- **A batch member is one machine, not two objects.** The line's answers are the messages.
  The holder sends `tested on base B` and `merged`, and the member's machine receives them.
- **NoTurn is a named transition with a bound.** It stays, because the turn is not a safety
  gate. The machine lists what a landing in NoTurn may and may not do.

## The queue: tickets over the flock

The queue owns the shared resources: the turn, the line, the slots and the serial lock. One
pure function, `schedule`, decides who gets what. The `flock` files stay as the storage and
the only mutual exclusion, because a dead process still releases its lock.

### The ticket protocol

- **Every waiting landing holds a ticket.** The ticket is the time the landing first asked
  for the turn, read from the turn host's clock and written once into its place. A landing
  with no draft to stack holds a place too, marked so that no holder stacks it.
- **A landing tries the lock only when no fresh ticket is older than its own.** One operation
  on the turn host reads the tickets and tries the `flock`. A landing on another host runs it
  through one ssh command, where two run for each poll before the change.
- **A holder stacks the oldest drafts**, in ticket order.
- **A landing that steps out keeps its ticket.** Its place is marked as waiting elsewhere, so
  no landing defers to it and no holder stacks it. When its wait ends it is the oldest ticket
  again.
- **No lock guards the tickets.** Each place is written by an atomic rename. Two landings
  that both read themselves as oldest both try the `flock`, and it admits one.

### Failure cases

- **A waiter dies.** Its ticket stays fresh for `FRESH_S` (120 s), and younger landings defer
  to it for that long. The turn stays free for at most `FRESH_S` plus one poll, 150 s. Many
  waiters that die together cost the same 150 s, because their tickets go stale together.
- **The holder dies.** The kernel releases a lock taken on daniel-box. A lock daniel-server
  holds through ssh is released when its session ends, which a dead process on a live host
  causes at once.
- **daniel-server vanishes while it holds a lock.** Nothing releases the lock. sshd on
  daniel-box reads `clientaliveinterval 0` and `tcpkeepalive no`, so it never probes the
  client, and the remote script blocks on its stdin for good
  ([#10560](https://github.com/DanielH2018/server/issues/10560)). Tickets neither cause this
  nor fix it. A heartbeat on the session's stdin fixes it.
- **daniel-server loses ssh to daniel-box.** The waiter can neither refresh its ticket nor try
  the lock. `turn._wait` already ends such a wait and the landing goes on without the turn.
  Its ticket goes stale within `FRESH_S`.
- **A live waiter polls slower than `FRESH_S`.** It reads as dead and a younger landing takes
  the turn. Order is lost for one grant. Safety is not, because the `flock` decides.
- **Landings on older code.** A landing that started before the change waits for up to
  `merge_timeout` without a ticket and without deferring. Order is partial for that long.

### The prototype's answer

A pure model of the turn and the line, with a Hypothesis `RuleBasedStateMachine` over it, is
on branch `claude/landing-queue-model`. It is not wired to the code. Its rules are an arrival,
a burst of arrivals, a poll interval, a release, a step-out and a death.

- Under the rules the code follows, Hypothesis finds a landing that a later arrival
  overtakes, and a scripted trace reproduces #10465: a landing that stepped out is stacked
  by no holder.
- Under tickets no landing that can use the turn is overtaken, in 300 runs of 40 steps.
- Under tickets the turn is never free for longer than `FRESH_S` plus one poll after the
  latest of a release, a death and the waiter's own return.

So slice 3 is worth building, and its first part is the fix for
[#10559](https://github.com/DanielH2018/server/issues/10559).

One interaction needs a ruling before slice 3 lands. A serial PR that holds the turn waits
there up to `DRAIN_S` for the landings past their merge. Under the race it wins the turn
rarely. Under tickets it wins in its order, and every landing behind it waits.
[#10284](https://github.com/DanielH2018/server/issues/10284), the serial PR yielding the
turn, removes that wait.

### Who runs `schedule`

Three designs can own the queue. The invariants hold under each.

- **Distributed.** Each landing runs `schedule` over the tickets it reads. The landing that
  holds the `flock` is the only one that acts, so it is the coordinator for its turn, and the
  next coordinator is the oldest ticket.
    - For: no host unit and no process to keep alive. A dead holder is replaced by the next
      poll. Every landing runs its own code from its own checkout, so the fresh-checkout
      re-run keeps working.
    - Against: a grant waits for the next poll, 15 s on average. A policy that needs two
      landings to agree, such as pipelining, needs one more lock file and one more shared
      file. The queue's history is spread over the landings' logs.
- **A coordinator daemon** on daniel-box owns the queue, and each landing asks it for grants.
    - For: one process holds the queue, so the model and the code have the same shape, and
      one log holds every grant. Grants are pushed, with no poll gap. Pipelining and
      priorities are decisions inside one loop.
    - Against: a host unit with a restart policy. It runs one version of the landing code,
      and that code changed often enough on 2026-11-27 for 54 landings to re-run from a fresh
      checkout. The daemon must restart on each change without losing the queue, or start a
      worker from a fresh checkout for each batch. A landing needs a path for when the
      daemon does not answer.
- **A hybrid:** the daemon while it answers, the distributed queue when it does not.
    - It is safe only if both modes take the same `flock`. A landing that cannot reach a
      daemon that is alive would otherwise merge beside the landing the daemon granted. The
      daemon is then an adviser on order, and the `flock` and the tickets are still the
      mechanism.
    - It costs both implementations, and the fallback is the one that runs least. A path that
      rarely runs is where a defect stays hidden.

**Recommendation: distributed, with `schedule` kept pure and the `flock` as the only mutual
exclusion.** That is the hybrid's fallback, built first and used always. A daemon can be
added later as an adviser on order, and adding it changes who calls `schedule`, not what
`schedule` decides. The replay simulator (slice 6) measures whether the poll gap and
pipelining are worth a daemon before one is built.

## Invariants

Each invariant is a predicate in `core/invariants.py`, and the Hypothesis state machines check
every one after every step.

- **Tested-tree merge.** The tree a merge makes equals a tree a green run tested, or master's
  own run tests it. Held by `merge._merge_directly`'s `sha` pin, `stale_base.batch_tested`,
  `stale_base.refuse_stale_overlap` and `scripts/dev/tested_tree.py`. It would have caught
  #10368.
- **One holder.** At most one landing that holds the turn is in Holding, Testing, Proving or
  Merging. Held by the `flock` in `turn._lock`. A landing in NoTurn is outside it by design.
- **Serial alone.** A serial landing merges only when no other landing is past its merge, and
  no landing that moves a bundle merges while it is live. Held by `slots._bar`, unless one of
  its reads fails.
- **Holder progress.** Holding never waits on a condition only a push or a re-run can change.
  No single check holds it. Five bounds approximate it: `stall.step`, `stuck_checks`,
  `merge._failing_checks`, `batch.RUN_TIMEOUT_S` and `slots.DRAIN_S`. It would have caught
  #10525 and #10288.
- **No discarded proof.** A green Testing whose base is still master's tip ends in Merging,
  never Rejoining. Held, since #10507, by the `batch_tested` read before
  `rejoin_in_turn` in `merge.await_merge`.
- **No starvation.** A Queued landing reaches Holding within the tickets ahead of it times
  the bound on a turn, or ends with a named reason. Nothing holds it. The only bound is
  `merge_timeout`, after which the landing leaves the queue.
- **One verdict.** Every landing ends in exactly one verdict, and only from a state that
  lists it. `pipeline.run` and `test_land_every_exit_names_a_verdict.py` hold the first half.
  Nothing holds the second.
- **Conservation.** Every place in the line belongs to a live Queued landing, and every answer
  goes to a landing that can read it. Held in part by `line._op`'s `FRESH_S` rule and
  `Place.leave`. The fix for #10559 adds a case it must cover: `merged`, answered while a
  landing waits to take the turn back.

## How it is tested

- **Transition tables.** Each state's `step` has a table of cases it must accept and cases
  it must refuse, in the repo's red-proof pattern. An executable table is how a refusal rule
  converges under review.
- **Queue model, stateful.** The prototype's machine, extended with CI green or red, a
  conflict, master moving, a rate limit, a serial PR and `DEPLOY_HOLD`. It checks every
  invariant after every rule. Each machine has a coverage test, as `SlotHolders` has, so a
  generator that never fills a queue fails.
- **Characterization traces of the code being replaced.** Before the cutover, Hypothesis
  generates scenarios, and the `Fakes` harness records the call trace `await_merge`, `batch`
  and `turn` produce for each. The replacement must reproduce every trace, except for the
  deliberate changes its slice lists. The cutover is direct, so these traces and the
  differential test are its whole guard.
- **Differential test of the driver.** The same random scenario drives the real driver over
  faked I/O and the pure model. The test asserts both produce the same action trace. That
  ties the code that ships to the model the properties checked.
- **Replay simulation.** `sim/` replays a day's recorded arrivals, CI durations and outcomes
  through `schedule` under different policies, such as batch size 4 against 6, tickets
  against the race, serial-last, or pipelining. It reports the merges an hour and the wait
  percentiles. A policy change is compared before it is built.
  [#10522](https://github.com/DanielH2018/server/issues/10522) and
  [#10521](https://github.com/DanielH2018/server/issues/10521) can be evaluated the same way.

## The cycle around the landing

A change passes eight stages. The landing is the sixth.

1. **Intake.** `findings.py next` and `claim`. The state is the issue and its `Claim:`
   comment.
1. **Isolation.** A worktree from `EnterWorktree` or the desktop app. Nothing refuses a
   second session; `session-health.py --sessions` advises.
1. **Implementation.** A session, or a fan-out batch (`fanout.py place launch`) with its
   review phase. A batch's state is in its run manifest.
1. **Local checks.** `prek`, `repo_guards.py` and `uv run pytest`. Nothing records that they
   ran.
1. **Pull request.** A draft, which runs no CI until its turn (D9).
1. **Landing.** `land.sh`. Its outputs are the `VERDICT:` and `LAND-RESULT:` lines.
1. **Deploy.** `promote`, Flux, the soak and the health gate, which the landing waits on.
1. **Close-out.** The issue closes on merge. The claim, the milestone and the worktree are
   retired by `findings.py`, a workflow and the worktree pruner.

Four facts about the cycle bear on the design:

- **A change takes 1.7 landings.** A landing that ends `pr-ci-red`, `merge-conflict` or
  `deploy-failed` sends the change back to implementation. Twenty-four of the day's 90
  verdicts did. A draft gets its first CI verdict at its turn, after its wait.
- **Prose carries most hand-offs.** The fan-out lands its batches through
  `fanout_lib/land_result.py`, which reads `LAND-RESULT:`. A session moves from stage to stage
  on a sentence in `CLAUDE.md` or a skill: run the guards before the PR, land after the PR,
  verify the change, release the claim.
- **Two stages keep the same fact separately.** `land_result.py` restates the verdict words of
  `outcome.py`. A claim and a PR's `Closes #N` both say who is working an issue, and the claim
  outlives the close.
- **Arrivals come in bursts the landing does not shape.** Eleven landings started between
  16:05 and 16:06. Nothing upstream reads how deep the landing queue is before it starts more
  work.

`scripts/dev/pipeline_timeline.py` already joins the stages of each merged PR into five gaps,
from issue to done. It is the measurement the cycle has.

### Two ways to widen the design

- **One machine for the change.** A state machine from issue to done, with the claim, the
  batch, the PR, the landing attempts and the close-out as its states.
    - For: one record answers where a change is and what it needs next. The loop back
      from a failed landing becomes a transition a tool makes. Close-out cannot be skipped.
    - Against: the implementation stage is a Claude session, which no tool observes except
      through its commits and its PR, so the machine can gate that stage's exit and nothing
      inside it. The machine needs a store keyed by the change. That store is a sixth copy of
      the state unless it replaces the claim comment, the run manifest and the verdict
      record, which are the tools every live session uses. Each stage has a different
      owner, so one machine has many owners or one very large module.
- **A typed contract at each stage boundary, and one read model.** Each stage keeps its
  owner and its own machine. What crosses a boundary is a typed value a tool emits and the
  next tool reads.
    - **Out of the landing:** `LAND-RESULT:` gains a `next=` field from one table in
      `outcome.py`, verdict to next action: done, re-run, wait for a named source, fix and
      re-run, or the operator. The fan-out reads the field instead of restating the verdicts,
      and a session reads it from `cc-wait land`.
    - **Into the landing:** a draft joins the line with a recorded pre-flight result for its
      head ([#10521](https://github.com/DanielH2018/server/issues/10521)). The result is
      advisory: ADR-0032 replayed 19 hosted-red runs on the host, and the host read some
      green for four separate causes. A red result still names a PR before it costs a turn.
    - **Back to implementation:** the fan-out's launch gate reads the depth of the landing
      queue, so a deep queue slows new batches.
    - **One read model:** `pipeline_timeline.py` gains the landings for each PR and the time
      each failed landing cost, fed by the transition log of slice 1.
    - For: each piece is a small change in one tool with one owner, and each removes a prose
      hand-off or a second copy. The landing's machine does not wait for any of them.
    - Against: no single record says where a change is. The read model answers that after
      the fact.

**Recommendation: the contracts.** They reach the three losses the day's data shows: failed
landings found late, the retry loop run by hand, and bursts. One machine for the change adds a
store and a migration of the claim and fan-out tools, and it still could not see into the
implementation stage.

## The plan, in slices

Each slice lands on its own through `land.sh`, keeps every check, and leaves something to
exercise. Order is middle-out: the contract and the model first, then the code behind them.

1. **Transition log.** `Ledger` holds a landing's stamps and writes them once, at the end.
   This slice writes one record for each state change as it happens: PR, from-state,
   to-state, event and time. A landing that hangs or dies then leaves its history, and
   `pipeline_timeline.py` can read it. No behaviour change.
1. **The queue model and its invariants.** The prototype moves from the test tree into
   `core/` as the functions slice 3 calls, with the extended import guard. The code does not
   call it in this slice.
1. **Tickets.** First the fix for #10559: a landing that steps out keeps its place. Then the
   ticket in each place, deferral to an older fresh ticket, and stacking in ticket order. This
   is the first behaviour change, and it waits on the ruling for #10284.
1. **The landing machine for the pre-merge half, by direct cutover.**
    1. `LandingState`, `step` and the characterization traces land first. Nothing calls
       `step`, and the tests hold it to the traces.
    1. One PR replaces the loop in `merge.await_merge` with the driver and deletes the loop.
       The differential test and the traces guard it.
1. **The queue machine owns scheduling.** Batch formation, the serial rule and slot grants
   move from `turn`, `line`, `batch` and `slots` into `schedule`. #10284 becomes a ruling and
   a one-line change.
1. **Replay simulator.** It compares batch size, serial-last and pipelining on recorded days.
1. **Pipelining**, if the simulator shows the gain. A batch run peaks at 9 to 12 concurrent
   jobs. In the 16:50 to 18:00 burst a median of 4.5 jobs ran against the cap of 20, so a
   second run fits.
1. **Post-merge phases as states.** Resolving, master CI, promotion, hosts and health join
   the machine for the transition log and the verdict map, with little code change.

If the contracts are ruled in, they are four more slices, each independent of the eight
above: the `next=` field, the pre-flight result, the launch gate and the timeline's attempts.

## Costs and risks

- **This is the code every session lands through.** A bug in a slice stalls every landing.
  The cutover in slice 4 is the largest single change. The fresh-checkout re-run still
  protects landings that started on older code, and a revert of one PR restores the loop.
- **A model in the test tree can drift from the code.** Slice 2 exists for one slice only.
  From slice 3 the code calls the model's functions, so no second implementation remains.
- **Complexity delta.** The target moves logic rather than adding it. The pre-merge modules
  hold about 2,850 lines. The core plus the slimmer modules should hold about the same, with
  the decisions in pure functions and the I/O behind `Tools`. Tickets add one field to a
  place and one check before the lock. The test suite shifts from call-order assertions to
  invariant and table tests, and the call-order tests that only pin a sequence retire as each
  slice lands.
- **Rulings that stay.** D9 (drafts run no CI), D10, the serial ruling of 2026-11-19, the
  slot count, `DRAIN_S`, ADR-0032's runner pods, and every `DECIDED:` marker under
  `land_lib/`. Each becomes a named constant or invariant in the core, with its marker moved
  beside it.
