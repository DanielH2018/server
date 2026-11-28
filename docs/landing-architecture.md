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
- **Ruled, 2026-11-27: the queue is distributed.** Each landing runs `schedule` over the
  tickets it reads, and the `flock` is the only mutual exclusion. A coordinator daemon may be
  added later as an adviser on order. *The queue: tickets over the flock* holds the
  comparison behind the ruling.
- **Ruled, 2026-11-27: a typed contract at each stage boundary,** and one read model, in
  place of one state machine for the whole change. *The cycle around the landing* holds the
  comparison.
- **Asked, 2026-11-27: use resources well at the landing bottleneck,** local runners
  included. *What a turn costs* holds the measurements and the answer.
- **Asked, 2026-11-27: the deploy UI tracks a change from state to state,** and its progress
  within a state. *The deploy UI reads the machine's state* holds the design.
- **Ruled, 2026-11-27: a serial PR yields the turn** while other landings stand in the
  line, for at most the first of its two merge budgets
  ([#10284](https://github.com/DanielH2018/server/issues/10284)). `slots._yields` holds the
  rule in one function, so slice 5 can move it into `schedule` unchanged.

Slice 1 is built: `land_lib/transitions.py` writes the state and transition records.
Slice 2 is built: `land_lib/core/queue_machine.py` holds `may_take` and `stackable`, which
`turn._wait` and `batch._members` call. Slice 3 is built: those two functions serve the
turn by ticket, under *The ticket protocol* below. Nothing else here is built. The validation below read the code at master `ce5c3abfa`. The
milestone is `Landing pipeline state machine`.

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
- **The queue has no owner and no order.** Slice 3 replaced the first two facts below with
  tickets.
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
  ([#10559](https://github.com/DanielH2018/server/issues/10559), fixed). `turn.held`'s
  `step_out` asked for the turn back with no draft, and `line.Place` drops the line for a
  place with no head. No holder could stack that landing, so it could only win the turn by
  polling first. `step_out` now asks with `Landing.line_head`, the draft's head until the
  PR is marked ready.

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
    landing_state.py         LandingState: frozen dataclasses, one per state
    queue_state.py           Place, Queue: the queue as its decisions read it
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
every module under `land_lib/`, those in `core/` included. It names the members of `core/` in
`CORE`, and refuses a module there that imports anything but its siblings and the stdlib
modules in `PURE_STDLIB`.

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
  where a holder can stack it, and so does a draft back from a step-out. A green ready head,
  and a landing that lands alone, hold a place with no head, which no holder stacks.
- **Holding.** `turn._wait` returned the lock. `slots.refuse_before_run` runs, then
  `batch.form`.
- **Yielded.** `Landing.without_turn` gives the turn back for a rate-limit wait, for a live
  serial landing, or for a serial PR's yield to the line (`slots._yields`), and asks for it
  again afterwards.
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
- **A landing tries the lock only when no fresh ticket is older than its own.** The stand that
  refreshes a landing's place also returns every other fresh ticket, of either UNIX user, and
  `may_take` reads them. A landing on another host runs one ssh command for a poll that
  defers, and a second, the try of the `flock`, only for a poll that may take the turn.
- **A holder stacks the oldest drafts**, in ticket order.
- **A landing that steps out keeps its ticket.** It leaves its place for the wait, so no
  landing defers to it and no holder stacks it. When its wait ends it writes the same ticket
  back, and it is the oldest ticket again.
- **A place a landing on older code wrote holds no ticket**, and reads as older than every
  ticket. Such a landing defers to nobody, and no landing on newer code starves it.
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

One interaction needed a ruling before slice 3 lands. A serial PR that holds the turn waits
there up to `DRAIN_S` for the landings past their merge. Under the race it wins the turn
rarely. Under tickets it wins in its order, and every landing behind it waits. The operator
ruled on 2026-11-27 that the serial PR yields the turn while the line holds a landing
([#10284](https://github.com/DanielH2018/server/issues/10284)), which removes that wait for
the drafts in the line. `slots._yields` owns the rule.

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

**Ruled, 2026-11-27: distributed, with `schedule` kept pure and the `flock` as the only
mutual exclusion.** That is the hybrid's fallback, built first and used always. A daemon can
be added later as an adviser on order, and adding it changes who calls `schedule`, not what
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
  `failing_checks.find`, `batch.RUN_TIMEOUT_S` and `slots.DRAIN_S`. It would have caught
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

**Ruled, 2026-11-27: the contracts.** They reach the three losses the day's data shows:
failed landings found late, the retry loop run by hand, and bursts. One machine for the
change adds a store and a migration of the claim and fan-out tools, and it still could not
see into the implementation stage.

## What a turn costs

One landing at a time holds the turn, so the turn's seconds are the scarce resource. The
green batches of 2026-11-26 and 2026-11-27 whose logs are clean show where they go.

- **A turn takes about 220 to 250 s for each PR it merges.** Twenty PRs merged in 4380 s of
  turn on 2026-11-27, and 29 in 7170 s the day before.
- **The batch's CI run is two thirds of the turn**, with a median of 360 s.
- **The rest is polling.** The holder's own merge follows the green run by a median of 45 to
  60 s, and each member's by 30 s. Both are whole polls of 30 s. A turn that merged four PRs
  took 510 s, and about 150 s of it was the gap between a check finishing and the next poll.
- **A batch merged 2.5 PRs on average**, against a limit of 4.

### The run waits on one job, not on runners

Two batch runs, read job by job from GitHub:

- Run 46054678957, with no host change, took 358 s. The `hooks` job ran 335 s, and its full
  sweep step 283 s. The next job to finish was a `pytest` shard at 243 s.
- Run 46065308899, with a host change, took 555 s. `host_packages (26.04, 3.14)` ran 417 s,
  and its updater cases 375 s. It starts 114 s in, after the build job. The next job
  finished at 240 s.
- No job in either run waited more than 22 s for a runner. In the 16:50 to 18:00 burst a
  median of 4.5 hosted jobs ran against the cap of 20.

So the run lasts as long as its slowest job, and the runners beside that job sit idle.

### What a local runner would change

Nothing that helps. `docs/ci-throughput.md`, *Phase C, not planned*, measured runner pods on
2026-11-20, and the operator ruled against them. Its three findings still hold, and the
figures above agree with them:

- **A pod is slower than a hosted leg.** A hosted `hooks` or `pytest` job ran 145 s at the
  median, each on a machine of its own. One pod on daniel-box ran the suite in 209 s on 8
  workers. A full hook sweep on daniel-box, timed for this page on 16 cores, took 201 s of
  wall time and 554 s of CPU, against 166 s and 283 s for that step in the two hosted runs.
- **The run does not wait for a runner.** A pod wins only when the hosted wait is over about
  100 s, and the batch runs above waited 22 s at most.
- **The hosts are loaded when landings burst**, and both fell below the 8 GiB memory floor in
  the fortnight measured.

The question reopens on one measurement: two batch runs at once, under pipelining, would
need 18 to 24 hosted jobs against the cap of 20. The simulator of slice 6 reports the peak.

Local work does help in one place, before the queue. A draft's first CI verdict comes at its
turn, and eleven landings ended `pr-ci-red` on 2026-11-27. A pre-flight on the session's own
host ([#10521](https://github.com/DanielH2018/server/issues/10521)) names a red PR before it
costs a turn. Its result is advisory, by ADR-0032.

### What would use the turn better, in order

1. **Fill the batch.** Tickets and the fix for #10559 put every waiting draft where a holder
   can stack it, and [#10527](https://github.com/DanielH2018/server/pull/10527) raises the
   limit to 6. The same run then carries more PRs.
1. **Poll faster while holding the turn**
   ([#10574](https://github.com/DanielH2018/server/issues/10574)). Only the holder polls at
   the faster rate, so the quota cost is one landing's.
1. **Split the run's slowest job**
   ([#10575](https://github.com/DanielH2018/server/issues/10575)): the `hooks` sweep and the
   host-package updater cases, onto hosted runners that sit idle. `docs/ci-throughput.md`,
   *The `hooks` and `host_packages` jobs run two legs each*, has the split. A run with a host
   change stays near 400 s, because the build job and `upgrade_path` run in series
   ([#10591](https://github.com/DanielH2018/server/issues/10591)).
1. **Stop spending turns on landings that cannot merge.** The pre-flight, and the `next=`
   field that tells a session what a failed landing needs.
1. **Overlap the next batch's run with this batch's merges** (slice 7), and let a batch run
   its CI while a serial landing deploys.

## The deploy UI reads the machine's state

`hosts/components/deploy-ui/` is the one browser view of landings in progress, by the
operator's ruling of 2026-11-06. `probe.py landing` reads its API. The page reads each
landing's state record, described below, and the turn, serial and slot locks. It shows the
landings of both hosts in queue order, each with its state, its time in that state and its
progress. Its log viewer still groups a log's lines by their `== N/M` step headers, but no
state comes from a log sentence. `hosts/components/deploy-ui/CLAUDE.md`, *Landing queue*, has
the rules.

Two gaps remain:

- A yielded landing's record does not say what it waits for, because `step_out` enters
  `yielded` with no progress.
- Nothing removes old records. The page hides a record older than a day, and the sticky
  directory lets only the writing user remove its own.

### One state record for each live landing

The transition writer of slice 1, `land_lib/transitions.py`, has two outputs.
`transitions.State` is the one enum of state names.

- **A state record on the turn host**, `/run/lock/homelab-land-line/<pr>.<uid>.state`, one
  JSON object written by an atomic rename. It holds `pr`, `host`, `pid`, `state`, `since` (when
  the landing entered the state, in epoch seconds), `history` (the states before it with
  their `since`), `progress` (`done`, `total` and `what`, or null) and `verdict`. A landing
  on daniel-server writes it through ssh, as it writes its place in the line. The record
  stays after the verdict and names it, so a record whose pid is gone and whose state is not
  `verdict` is a landing that died. The uid in the name is the writer's: the directory is
  sticky, so no user can replace a record another user left, and a PR landed by both
  `ubuntu` and `claude` has one record from each. The reader takes the newest `since`.
  Removing old records is the reader's job.
- **A transition record in syslog**, one logfmt line under the `landing-annotation` tag:
  `event=land_transition pr from to at verdict`. Loki keeps it, and `pipeline_timeline.py`
  can read it. The event is not named `landing...`, because every Landings board query
  selects `|= "event=landing"`.

Progress comes from reads the landing already makes: check-runs finished of check-runs
listed from each CI read (`await_ci.wait`'s `on_runs`), Kustomizations settled of those
waited on from each `land_wait` poll (`on_ready`), hosts applied of hosts from the host wait,
and members merged of members from a batch's merges.

The page reads the state records. A record is a file on the page's own host, so the page
does not depend on Loki to show a landing that is deploying Loki.

### What the page shows

- **The queue, in order:** the landing that holds the turn, then each waiting landing in the
  order the turn serves them, with its position. A landing that stepped out shows what it
  waits for.
- **Each landing's state,** with the time in it and the states behind it.
- **Progress within the state.** Each comes from a read the landing already makes, so it
  adds no GitHub call:
    - Queued: the tickets ahead.
    - Testing, Proving and MasterCI: checks finished of checks expected.
    - Merging a batch: members merged of members.
    - Yielded: the serial landing it waits for, or the seconds to the quota's reset.
    - Promoting: Kustomizations ready of total, then the soak's status.
    - HostWait: hosts applied of hosts.
    - Grading: tags gated of tags.
- **The change around the landing**
  ([#10577](https://github.com/DanielH2018/server/issues/10577)): one row for each open
  change, with its stage and its state within the stage, each read from the tool that owns
  the stage. This is the live half of the read model, and `pipeline_timeline.py` is the
  half that looks back.

### How the page stays current

- The state names are one enum in `land_lib`, and a test fails when a state has no label on
  the page. The verdict names are already held to `outcome.VERDICTS` this way.
- The page renders a state it does not know by its raw name, so a landing on newer code is
  never blank.
- The phase stops coming from log sentences, and that derivation is deleted.
- `hosts/components/deploy-ui/CLAUDE.md` changes in the PR that changes the page.

## The plan, in slices

Each slice lands on its own through `land.sh`, keeps every check, and leaves something to
exercise. Order is middle-out: the contract and the model first, then the code behind them.

1. **State and transition records**
   ([#10561](https://github.com/DanielH2018/server/issues/10561)). `Ledger` holds a landing's
   stamps and writes them once, at the end. This slice writes a state record on the turn
   host and a transition record in syslog at each state change. A landing that hangs or dies
   then leaves its history, and the deploy UI and `pipeline_timeline.py` can read it. No
   behaviour change.
1. **The turn's ordering decisions as pure functions**
   ([#10562](https://github.com/DanielH2018/server/issues/10562)). `may_take` and `stackable`
   move into `core/`, with the rules the code follows at master, and `turn._wait` and
   `batch._members` call them. The prototype's state machine checks the functions the code
   calls. No behaviour change.
1. **Tickets** ([#10564](https://github.com/DanielH2018/server/issues/10564)), built. The fix
   for #10559 landed first: a draft that steps out keeps its place. Then the ticket in each
   place, deferral to an older fresh ticket, and stacking in ticket order. This is the first
   behaviour change. The operator gave the go-ahead on 2026-11-28.
1. **The landing machine for the pre-merge half, by direct cutover**
   ([#10570](https://github.com/DanielH2018/server/issues/10570)).
    1. `LandingState`, `step` and the characterization traces land first. Nothing calls
       `step`, and the tests hold it to the traces.
    1. One PR replaces the loop in `merge.await_merge` with the driver and deletes the loop.
       The differential test and the traces guard it.
1. **The queue machine owns scheduling**
   ([#10571](https://github.com/DanielH2018/server/issues/10571)). Batch formation, the
   serial rule and slot grants move from `turn`, `line`, `batch` and `slots` into `schedule`.
   #10284 becomes a ruling and a one-line change.
1. **Replay simulator** ([#10576](https://github.com/DanielH2018/server/issues/10576)). It
   compares batch size, serial-last and pipelining on recorded days, and reports the peak of
   concurrent CI jobs.
1. **Pipelining** ([#10578](https://github.com/DanielH2018/server/issues/10578)), if the
   simulator shows the gain. A batch run peaks at 9 to 12 concurrent jobs. In the 16:50 to
   18:00 burst a median of 4.5 jobs ran against the cap of 20, so a second run fits.
1. **Post-merge phases as states**
   ([#10572](https://github.com/DanielH2018/server/issues/10572)). Resolving, master CI,
   promotion, hosts and health join the machine for the transition records and the verdict
   map, with little code change.

Eight more slices stand beside those eight. None waits for the landing machine.

- **The stage contracts:**
    - `LAND-RESULT:` names the next action
      ([#10565](https://github.com/DanielH2018/server/issues/10565)).
    - The pre-flight result ([#10521](https://github.com/DanielH2018/server/issues/10521)).
    - The launch gate reads the queue's depth
      ([#10566](https://github.com/DanielH2018/server/issues/10566)).
    - The timeline counts the landings for each PR
      ([#10568](https://github.com/DanielH2018/server/issues/10568)).
- **The turn's time:**
    - The holder polls faster ([#10574](https://github.com/DanielH2018/server/issues/10574)).
    - The run's slowest job is split
      ([#10575](https://github.com/DanielH2018/server/issues/10575)).
- **The deploy UI:**
    - The landing view, after slice 1
      ([#10573](https://github.com/DanielH2018/server/issues/10573)).
    - The change view, after it
      ([#10577](https://github.com/DanielH2018/server/issues/10577)).

Two defects found during the validation are filed apart from the slices:
[#10559](https://github.com/DanielH2018/server/issues/10559), the lost place, and
[#10560](https://github.com/DanielH2018/server/issues/10560), a lock that outlives
daniel-server.

## Costs and risks

- **This is the code every session lands through.** A bug in a slice stalls every landing.
  The cutover in slice 4 is the largest single change. The fresh-checkout re-run still
  protects landings that started on older code, and a revert of one PR restores the loop.
- **A model in the test tree can drift from the code.** The prototype is such a model, and a
  review of its tests marked nine of ten as discards for that reason. Slice 2 therefore
  lands the functions with the code calling them, so no second implementation exists.
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
