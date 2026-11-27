# Landing architecture: an explicit state machine with property-tested invariants

The operator asked on 2026-11-27 for a landing pipeline that is robust, well defined and
optimised from start to finish, and that can be tested, maintained and extended. This page is
the target architecture and the plan to reach it in slices. It is a proposal, not yet ruled
on. `docs/landing-speed.md` holds the measurements that motivate it.

**Recommendation: split the landing's decisions into a pure core.** The core is two state
machines: one for a single landing, and one for the shared queue that the turn, the line, the
slots and the serial lock implement today. Each is a pure transition function over a typed
state. Hypothesis drives both through random orders of events and checks named invariants on
every step. The GitHub, git, flock and cluster calls stay behind the existing `Tools`
interface, and do only what the core decides. Every safety rule, `DECIDED:` marker and operator ruling stays, and becomes a
named invariant with a test.

## What the pipeline is today

The facts below come from a survey of `scripts/deploy_tools/land_lib/` on 2026-11-27.

- **Size.** 42 modules and 10,056 lines, plus `land.py` (463 lines). The pre-merge half is
  the largest and most coupled: `batch` 622 lines, `merge` 604, `turn` 455, `slots` 446,
  `stale_base` 373 and `stall` 348.
- **State is implicit.** A landing's state is spread over about 30 mutable `Landing` fields
  (`landing.py:67-120`), the `Ledger`, closure state in `turn.held`, and about 15 locals in
  `merge.await_merge`: `in_turn`, `rejoined`, `drafted`, `updated_from`, `asked`, `waited`
  and `wait_ends_at`, which is recomputed at four sites. Some fields are callables, such as the
  turn handles. Batch members are separate `Landing` objects, and a holder writes fields of
  another landing's object.
- **Outcomes are raised from 68 `die` sites and 15 `finish` sites** in 14 modules. No single
  place lists which state can end in which verdict.
- **The shared queue has no owner.** The turn is a non-blocking `flock` that every waiter
  polls every 30 s, so the first poll after a release wins. The line picks members in PR-number
  order (`sorted(os.listdir)` in `line.py`). The slots, the serial lock and `DEPLOY_HOLD` are
  read in `slots._bar`. The ordering policy is spread over `turn`, `line`, `batch` and `slots`.
- **Tests assert call order on fakes.** 64 test modules hold 691 tests. Most build a `Fakes`
  object of scripted answers and assert the order of recorded calls. Only two state machines
  exist: `SlotHolders` (`test_land_slots_properties.py`) and `StallWait`
  (`test_land_stall_properties.py`). No test models the turn, the line or a whole landing.

Three of the day's defects lived in the gaps between these pieces:

- A stale `turn_interrupted` flag threw away green batches
  ([#10507](https://github.com/DanielH2018/server/pull/10507)).
- A turn holder waited on a failing check that could never pass
  ([#10525](https://github.com/DanielH2018/server/pull/10525)).
- The turn has no ordering. Three landings waited out the 5520 s merge budget while later
  arrivals took the turn or were stacked. Not every one of those waits is the race: #10482 is
  serial and no batch can carry it, and #10449's wait includes a serial wait. Whether the race
  starves a landing is the first thing the queue model measures.

The first two break a rule that a state machine would have named.

## The target shape

```
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
(`_land_fakes.py:build_tools`), and reports what came back as an `Observation`. No new I/O
layer is added: a `github.py` that only renamed `Tools` would fail the deletion test. The
driver writes one transition record per step, with a timestamp, so every landing's timeline
is in Loki. The `land*.log` files carry no timestamps today.

`tests/land/test_land_imports.py` requires its `MODULES` list to equal `land_lib/*.py`, so a
module under `land_lib/core/` would fall outside the import-direction guard without any error.
The slice that creates `core/` extends the guard to it, with a non-vacuity assertion that names
its members.

## Who runs `schedule`

No queue process exists today. Each landing is its own process, on one of two hosts, and
landings coordinate through `flock` files and line files on daniel-box. A pure `schedule`
needs a consistent snapshot of the queue, so one of two designs has to own it. The choice is
the operator's, and the invariants hold under either.

- **Distributed.** Each landing takes a short lock on the line, reads a snapshot, runs
  `schedule`, and acts on its own grant. This keeps today's crash safety: a dead process
  releases its locks. The costs are staleness, because a place counts as live for `FRESH_S`
  (120 s), and an ssh round trip per read from daniel-server.
- **A coordinator daemon** on daniel-box owns the queue, and each landing is a client that
  asks for grants. The queue is then one state in one process, which makes it the easiest to
  model and to replay. The costs are a new host unit, and a single point of failure, which
  needs a restart policy and a way for landings to go on without it, as they go on without the
  turn today.

The distributed design changes less and fits slices 2 to 5. The daemon fits pipelining and the
simulator better.

The post-merge phases (master CI, promotion, hosts, health) are a linear sequence and already
reasonably separated. They join the state machine as states, so the transition log and the
verdict mapping cover them, but their code changes little.

## The landing state machine

One landing moves through these states. A verdict is reachable only from the states that list
it, and the test suite checks that list.

```
Armed ─► Queued ─────────► Holding ─► Testing ─► Merging ─► Merged
  │        │  ▲              │  │        │          │
  │        │  └─ Yielded ◄───┘  │        │          └─► MasterCI ─► Promoting ─► HostWait ─► Grading
  │        │    (rate limit,    │        └─ Split (red batch)                              │
  │        │     serial wait,   └─ Rejoining (master moved)                              ▼
  │        │     older waiter)                                                       Verdict
  ▼        ▼
Verdict  Verdict   (refused, merge-conflict, pr-ci-red, merge-timeout ...)
```

- **Queued.** The landing has a place in the line and a ticket: its arrival time. It holds
  nothing.
- **Holding.** It holds the turn. It must leave Holding within a bound, by merging, yielding
  or ending, and it never sleeps on a wait the turn cannot shorten.
- **Yielded.** It gave the turn back for a wait: a rate limit, a serial landing, or an older
  waiter's claim. It keeps its ticket, so it does not lose its place.
- **Testing.** One CI run covers a tree stacked on a known base. The state records the
  tested tree and the base, never a separate flag such as `turn_interrupted`.
- **Merging.** It merges only the tree Testing recorded, onto the base Testing recorded.
  Otherwise it goes to Rejoining.
- **Verdict.** Terminal. Exactly one per landing, from the closed `Verdict` set, and recorded
  once.

Four control paths run on exceptions today. Each becomes an explicit transition:

- **Re-run fresh.** `fresh.LandFresh` before the merge and `fresh.GradeFresh` after it re-run
  the landing from a fresh checkout of master. This becomes a transition to a Handed-off
  state, whose verdict is the fresh landing's.
- **Hand over the turn.** `turn.HandOver` passes the turn's file descriptor to that fresh
  landing. This becomes a transition that carries the grant to the successor.
- **The PR lock and `--detach`.** They decide whether a landing starts at all. They become the
  entry transition into Armed, with `landing-live` as its refusal.
- **Batch members.** These are separate `Landing` objects whose fields the holder writes
  today (`tested_on`, `merge_wait_s`). They become messages between machines: the holder sends
  `tested on base B` and `merged`, and the member's machine receives them. No machine writes
  another's state.

## The queue state machine

The queue owns the shared resources: the turn, the line, the slots and the serial lock. One
pure function, `schedule`, decides who gets what. The `flock` files stay as the storage and the
crash-safety mechanism, because a dead process still releases its lock. The policy moves out of
them.

`schedule` owns these rules:

- **Order.** Tickets are served first come, first served. The holder stacks the oldest drafts.
  A waiter that sees an older live waiter defers to it, bounded so a dead waiter blocks nobody.
- **Batch formation.** Up to `MAX_STACKED` members, skipping drafts a rule bars, in ticket
  order.
- **Serial rule.** A serial PR lands alone and drains first. These rules are unchanged:
  [#10284](https://github.com/DanielH2018/server/issues/10284), the serial PR yielding while
  others wait, becomes a one-line change here once ruled.
- **Slots and holds.** The slot cap, the low-quota cap, `DEPLOY_HOLD` and the no-promotion
  window.
- **Pipelining, later.** A second grant, `next`, that may run CI stacked on the holder's batch
  but may not merge.

## Invariants

Each invariant is a predicate in `core/invariants.py`, and the Hypothesis state machines check
every one after every step.

| Invariant | What it states | Defect it would have caught |
|---|---|---|
| Tested-tree merge | The tree a merge makes equals a tree a green run tested, or master's own run tests it | #10368 (a merge onto an untested master) |
| One holder | At most one landing is in Holding, Testing or Merging | |
| Serial alone | A serial landing merges only when no other landing is past its merge, and no non-quiet landing merges while it is live | |
| Holder progress | Holding never waits on a condition only a push or a re-run can change | #10525 (a failing check), #10288 (cancelled runs) |
| No discarded proof | A green Testing whose base is still master's tip ends in Merging, never Rejoining | #10507 |
| No starvation | A Queued landing reaches Holding within (tickets ahead × the bound on a turn), or ends with a named reason | today's 5520 s waits |
| One verdict | Every landing ends in exactly one Verdict, and only from a state that lists it | |
| Conservation | Every place in the line belongs to a live Queued landing, and every answer to a landing that can read it | |

## How it is tested

- **Transition tables.** Each state's `step` has a table of cases it must accept and cases
  it must refuse, in the repo's red-proof pattern. An executable table is how a refusal rule
  converges under review.
- **Queue model, stateful.** A `RuleBasedStateMachine` with up to about 15 simulated
  landings. Its rules: arrive, CI green or red, conflict, master moves, rate limit, serial PR
  arrives, landing dies, `DEPLOY_HOLD` set or cleared. It checks every invariant after every
  rule. `SlotHolders` and `StallWait` already show the pattern works in this suite.
- **Characterization traces of today's code.** Before any cutover, Hypothesis generates
  scenarios, and the existing `Fakes` harness records the call trace today's `await_merge`,
  `batch` and `turn` produce for each. The new code must reproduce every trace, except for the
  deliberate changes a slice lists. This is what makes it safe to replace the riskiest code:
  it compares the new code with the old, not only with the model.
- **Differential test of the driver.** The same random scenario drives the real driver over
  faked I/O and the pure model. The test asserts both produce the same action trace. That
  ties the code that ships to the model the properties checked.
- **Replay simulation.** `sim/` replays a day's recorded arrivals, CI durations and outcomes
  through `schedule` under different policies, such as batch size 4 against 6, first-come
  against the race, serial-last, or pipelining. It reports the merges an hour and the wait
  percentiles. A policy change is compared before it is built.
  [#10522](https://github.com/DanielH2018/server/issues/10522) and
  [#10521](https://github.com/DanielH2018/server/issues/10521) can be evaluated the same way.

## The plan, in slices

Each slice lands on its own through `land.sh`, keeps every check, and leaves something to
exercise. Order is middle-out: the contract and the model first, then the code behind them.

1. **Transition log.** The driver's predecessor, today's code, emits one structured record
   per phase change: PR, from-state, to-state, event, timestamp. It goes to syslog and Loki.
   Every later slice is measured against it. Small, and no behaviour change.
2. **The model and its invariants, not yet wired in.** `core/state.py`, `queue_machine.py` and
   `invariants.py`, encoding today's rules, polling race included, with the stateful test and
   the extended import guard. If the no-starvation property fails against the race, that red
   is the evidence for slice 3. If it passes, slice 3 drops out.
3. **First-come tickets.** The line records arrival time, and the turn and the batch serve in
   ticket order. The model's no-starvation property goes green. This is the first behaviour
   change.
4. **The landing machine for the pre-merge half.** First, the characterization traces of
   today's code. Then `merge.await_merge`'s loop becomes `landing_machine.step` over an
   explicit state, and the driver executes its actions through `ln.tools`. Split into
   sub-slices: the state record first, then rejoin and stale-base decisions, then stall and
   stuck checks. The traces and the differential test guard each one.
5. **The queue machine owns scheduling.** Batch formation, the serial rule and slot grants
   move from `turn`, `line`, `batch` and `slots` into `schedule`. #10284 becomes a ruling and a
   one-line change.
6. **Replay simulator.** It compares batch size, serial-last and pipelining on recorded days.
7. **Pipelining**, if the simulator shows the gain. A batch run peaks at 9-12 concurrent jobs.
   In the 16:50-18:00 burst a median of 4.5 jobs ran against the cap of 20, so a second run
   fits.
8. **Post-merge phases as states.** Master CI, promotion, hosts and health join the machine
   for the transition log and the verdict map, with little code change.

## Costs and risks

- **This is the code every session lands through.** A bug in a slice stalls every landing.
  Each slice is small, the differential test runs before any cutover, and the stale-code
  re-run from a fresh checkout still protects landings that started on older code.
- **Shadow mode is a choice for the operator.** Slice 4 could first run the pure `step`
  beside today's loop and log every divergence, then cut over. That is a temporary second
  path, which CLAUDE.md's *Finish the move* rule forbids unless asked. The alternative is a
  direct cutover guarded by the differential test alone.
- **Complexity delta.** The target moves logic rather than adding it. The pre-merge modules
  hold about 2,850 lines. The core plus the slimmer modules should hold about the same, with the decisions in
  pure functions and the I/O behind `Tools`, as it is today. The test suite shifts from call-order assertions to
  invariant and table tests, and the call-order tests that only pin today's sequence retire
  as each slice lands.
- **Rulings that stay.** D9 (drafts run no CI), D10, the serial ruling of 2026-11-19, the
  slot count, `DRAIN_S`, ADR-0032's runner pods, and every `DECIDED:` marker under
  `land_lib/`. Each becomes a named constant or invariant in the core, with its marker moved
  beside it.
