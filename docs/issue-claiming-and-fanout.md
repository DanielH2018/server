# Issue claiming and fan-out

**Status: live.** The claim protocol and the `/issue-fanout` skill described below are in
`scripts/dev/findings.py` and `.claude/skills/issue-fanout/`.

Several Claude sessions work this repo at once. `findings.py` gives the backlog a status field
and an owner-of-record, but nothing says which *session* is working an issue right now, and
nothing marks an issue as yours alone. Two sessions can pick the same issue, and an issue you
want to keep for yourself gets picked up by the next session that reads the register.

This spec adds three things: a claim protocol, a `manual` reservation, and a fan-out skill that
dispatches Opus agents at a batch of issues after triage.

## What already exists

`scripts/dev/findings.py` files, re-observes, escalates and closes findings as GitHub Issues.
It is split four ways: the CLI, `findings_lib/issue_model.py` (vocabulary and pure reads),
`findings_lib/plans.py` (the `gh` argv every command plans) and `findings_lib/gh_calls.py` (the calls). Every
command plans a list of argv first, then runs it, so `--dry-run` writes nothing.

`scripts/dev/prune_worktrees.py` decides whether a session worktree is done with. It exports
`parse_worktree_list` and `session_is_alive`; `.claude/hooks/session-health.py` already imports
both to print the other-live-sessions banner.

Every open issue except #3 carries the `claude` label. #3 is Renovate's Dependency Dashboard,
created 2026-06-06 with no labels. Scoping claims to `claude`-labelled issues therefore costs
nothing, and this spec keeps that scope.

## Why the claim is not an assignee

`gh` authenticates as a single account. Assigning an issue would name the operator, not the
session, so every claim would be indistinguishable from every other. The claim has to carry a
session-level identity itself.

## Vocabulary

Two labels join `LABELS` in `findings_lib/issue_model.py` and are created by the existing `sync-labels`:

| Label | Colour role | Meaning |
|---|---|---|
| `manual` | state marker (grey) | Reserved for the operator. No session claims it, and no fan-out dispatches it. |
| `claimed` | state marker (grey) | A session is working this issue. A cheap filter; the payload is a comment. |

`manual` is one word rather than a prefix. The Renovate convention already uses "manual — …"
inside group names, so the bare word is mildly overloaded; the label namespace is separate
enough that this has not been worth a longer name.

## The claim record

A claim is a **comment**, following the machine-readable trailer convention
`issue_model.trailer()` already uses for fingerprints:

```
Claimed by `worktree-issue-1132` (session `cse_01ABC…`) at 2026-09-05T18:40Z

Claim: `worktree-issue-1132`
```

A release is the matching form:

```
Released by `worktree-issue-1132` at 2026-09-05T21:02Z — landed in #1270

Released: `worktree-issue-1132`
```

Reading who holds an issue means folding the comment list forward in `createdAt` order: a
`Claim:` line opens if no claim is open already, and a matching `Released:` line closes.

**The first writer wins, not the last.** gh returns comments in `createdAt` order, so the
earlier of two racing claims is the earlier comment. Letting a later claim overwrite a live
one would mean a session could take an issue out from under another by claiming it again —
and worse, the first claimant's own `release` would then be refused, so it could not clean up
after losing.

**Why a comment rather than a body edit.** Comments are append-only and carry `createdAt`.
Two sessions commenting concurrently both succeed and the ordering is total, so a read-back
settles which one holds the issue. Two sessions editing a body race, and the loser's write
disappears with no trace.

**Why no compare-and-swap.** GitHub offers none, and the fan-out does not need one: the
orchestrator claims every issue in every batch *before* it spawns a single agent, so a fan-out
has no internal race. The only residual race is between independent ad-hoc sessions, where the
cost of losing is a duplicated triage rather than corruption. The read-back handles it —
the session whose claim comment sorts first holds the issue, and the loser releases.

## Staleness: a claim expires with its worktree, not with a pid

A claim is reclaimable when either holds:

- the worktree it names is absent from `git worktree list`, or
- `prune_worktrees.py` already judges that worktree REMOVABLE — merged into `origin/master`,
  clean, and holding no live session lock.

A claim is **not** reclaimable when the named worktree still exists with uncommitted changes,
even if its lock owner is dead. That case is the 2026-09-05 incident: the container restarted,
killed 14 agents mid-work, and every worktree kept its uncommitted edits so each session was
resumed in place. Expiring those claims would have handed half-finished issues to a second
agent, which is worse than leaving them held.

There is no heartbeat and no TTL. Both would have expired exactly those claims, because the
process was gone while the work was not.

Reusing `prune_worktrees` is what makes this self-healing without either. The staleness
question — is this worktree done with — is a question that module already answers, and
`session-health.py` already demonstrates importing it from outside its own directory.

## Commands

Five new subcommands, plus a change to `list` and one to `close`. All of them match
`findings.py`'s plan-then-run split: the argv goes in `findings_lib/plans.py`, the parsing in
`findings_lib/issue_model.py`, the `gh` calls in `findings_lib/gh_calls.py`.

### `claim <n>… --worktree <name> [--session <id>] [--force]`

Claims one or more issues for a worktree. Refuses an issue labelled `manual`. Refuses an issue
already held by a different live claim. Refuses an issue that lacks the `claude` label —
`load_issues` filters on it, so a claim on an issue outside the register is invisible to
`claims`, `reap` and `next` alike and only a hand-typed `release` could ever clear it. Prints
what it took and what it refused.

Exit 3 on refusal, matching the existing contract in the CLI that 3 means *nothing was written
because the issue refuses it*.

**It checks its own `--worktree` first.** A claim that reads STALE the moment it lands is worse
than no claim, because it reads as protection: `next` re-offers the issue and `reap` releases
it while the session is still working it. Two shapes reach that state, and one guard covers
both. The name may match no branch at all — the attribution table below puts worktree
`issue-1132` beside branch `worktree-issue-1132`, and the liveness rule matches on the branch.
Or it may match a real branch whose state is REMOVABLE: `master` and every other primary
checkout, which git never locks, and a crashed-and-resumed orchestrator, whose lock named a pid
and a process start time that a restart does not bring back. Either way the guard exits 3 and
names the reason. `--force` claims anyway, for the resumed orchestrator that legitimately still
holds its work.

A git read that FAILS does not refuse. This guard is advisory and only declines to warn, which
is the opposite of `reap` — `reap` writes on a bad read, so it refuses outright.

### `release <n>… --worktree <name> [--reason <text>]`

The reverse state. Posts the release comment and removes the `claimed` label.

`--worktree` is **required**, not optional. A release names who is releasing, and
`plan_release` refuses any claim but that worktree's own — without the name, one session
could release the claim another session holds. It also creates the `claimed` label first if
the repo lacks it. `gh issue edit --remove-label` fails on a label that does not exist, and
the release comment is already posted by then. `reap` does the same, for the same reason.

**The name must be one the trailer can carry.** `claim` and `release` both refuse a
`--worktree` holding a backtick, a line break, or surrounding whitespace, and exit 2. Such a
name used to write the comment and the label and then fail to parse its own trailer on
read-back, which `claim` reported as a race lost to nobody.

The reason is collapsed to one line before it is written. `current_claim` reads a `Claim:`
line anywhere in a comment body, and the reason sits above the trailer, so a multi-line
reason naming a worktree turned a release into a claim by that worktree. `reap` builds its
own reason out of a worktree's lock reason, which is free text nobody here writes.

### The `claimed` label is repaired, in both directions

The comment is the claim; the label is decorative, and every read path uses `current_claim`.
The two can still disagree, because `claim` posts the comment first and a failed
`--add-label` — a rate limit, a transient 502 — leaves the claim held with the label off. So
a reclaim that finds its own claim already posted plans the missing label edit rather than
nothing, and a `release` that finds no claim but a stuck label plans the label removal rather
than refusing. Without those, a label that went wrong once stayed wrong permanently and
silently: a wrong answer for anyone filtering GitHub by `label:claimed`.

### `claims [--json]`

Every open claim: issue number, worktree, age, and whether the holder is live or stale. This is
the way to see the state — a claim protocol with no way to list claims is a one-way door.

### `reap [--dry-run]`

Releases every stale claim, printing why each was judged stale. `--dry-run` plans and writes
nothing, like every other command here.

### Every path that closes or reopens releases the claim it finds

Closing a claimed issue posts its release and drops the `claimed` label first. `claims`,
`reap` and `next` all read open issues, so a claim stranded on a closed issue leaves every
view at once — invisible rather than wrong, which is harder to notice.

`close` is not the only such path, and the other two used to strand a claim exactly that way.
`verify --close` called the close planner directly and skipped the release. And the closing
mechanism this document itself names — the PR body's `Closes #<n>`, which GitHub honours —
posts no release comment at all and leaves the label on; `open` then reopens that issue for a
later re-observation and the stale claim comes back LIVE, blocking `claim` and withholding the
issue from `next` for as long as the claiming worktree exists. All three now go through one
helper, `_release_held_claim`, and release whoever holds the claim rather than only the caller.

**`verify --close` releases only a claim it is allowed to close over.** Releasing whoever
holds the issue stops the claim being stranded; it does not stop the close itself, so an
unrelated `verify --all --close` could still close an issue out from under the session
working it. Every other write in the protocol refuses on a live claim, so this one does too:
a LIVE claim withholds the close, the run prints who holds the issue, and it exits 3.
`--close-claimed` closes anyway, and skips the worktree read entirely. A FAILED worktree read
withholds the close as well — `next`'s conservative degradation rather than `reap`'s outright
refusal — because a git error must not read as "no claims are live." Nothing pays for the
worktree read unless a claim really sits on an issue the run is about to close.

The reopen posts its release as its own comment rather than folding the trailer into the
regression note, so no comment body ever carries a `Claim:` and a `Released:` line at once.

### `next [--limit N]`

The picking command. Returns open issues that are: `claude`-labelled, not `manual`, not
live-claimed, and not already referenced by an open PR, ordered by the existing
`issue_model.sort_key`.

**There is no default bound.** `--limit` defaulted to 10, and an orchestrator read
`next --json`, took the ten rows for the whole free set, and never saw the twelve behind them.
A view that truncates without saying so is blind to real state in the same way the stranded
claims above are. `--limit N` is still there for anyone who wants a bounded list.

An ad-hoc session runs this instead of eyeballing `list` and guessing. The open-PR check is
what stops a session picking up work another session has already finished but not landed.

### `list`

`list` gains no flag. It **marks** a manual row `[manual]` and a claimed one
`[claimed:<worktree>]`, and hides neither. Hiding a manual row is how an issue like #1132
stops being visible to anyone, including the operator who reserved it — and a flag defaulting
to "show" that nothing can turn off is a flag that documents the opposite of what it does.

## What a fan-out agent may not do

A fanned-out agent may close an issue with `close --fixed --pr <n>` and nothing else.

`--refuted` and `--accepted` are terminal: `plan_open` returns early on both, so the fingerprint
can never be re-filed. An agent holding that authority could permanently bury a real finding,
and the burial would be invisible — the next review simply never re-files it. Both stay
operator-only.

This is a rule in the skill brief, not a flag guard, per the repo's escalation ladder: a check
is what a rule becomes after it has actually been violated.

## Attribution: the worktree name is the record

A worktree's name carries the issues it is working, so the mapping is derived rather than
recorded a second time:

| Case | Worktree | Branch |
|---|---|---|
| One issue | `issue-1132` | `worktree-issue-1132` |
| Several | `issue-1132+1140+1175` | `worktree-issue-1132+1140+1175` |

`EnterWorktree` allows letters, digits, dots, underscores and dashes, up to 64 characters,
which bounds a multi-issue name at roughly five issues.

What this buys:

- **Issue → session** is the `Claim:` comment.
- The `session-health.py` banner prints `worktree-issue-1132` where it prints
  `worktree-agent-a1f5b5e3cdf2f9684` today, so the other-live-sessions list becomes readable
  at a glance.
- `prune_worktrees.py` reports the same way.
- The PR body carries `Closes #1132`, so the merge closes the issue and the `land.sh` verdict
  attaches to it.

### Measured: a subagent cannot own a named worktree

The naming table above describes what an operator-driven session does. **A fanned-out subagent
cannot reach it.** Measured 2026-09-05 with one probe agent:

| Mechanism | Result |
|---|---|
| Agent calls `EnterWorktree` with `name:` | Refused. "EnterWorktree cannot create a worktree from a subagent with a cwd override (isolation: "worktree" or explicit cwd) — it would mutate the parent session's process-wide working directory." |
| Agent calls `EnterWorktree` with `path:` into a pre-created worktree | Accepted by the tool, then every subsequent command refused: "This agent is isolated in the worktree … Refusing to run it there." |

The refusal names a third mechanism — spawn the agent with `cwd` set to the worktree — but the
Agent tool as exposed here takes no `cwd` parameter. So a fanned-out agent gets the
auto-generated `agent-a0291ece…` name and keeps it.

**What this costs, and what it does not.** Issue-to-session attribution is unaffected: the
claim comment records whatever worktree name the agent reports, auto-generated or not, and
`claims` renders the mapping. Session-to-issue attribution loses the readable branch name, so
the `session-health.py` banner keeps printing `worktree-agent-a0291ece…`.

A session an operator drives can still name its own worktree `issue-1132`, because it has no
cwd override. Only the fan-out is constrained.

The banner improvement is recoverable later with a gitignored `.claim` marker file written into
the worktree root, which `session-health.py` could read with no network call. That is not in
scope here.

## The `/issue-fanout` skill

1. **Triage.** Read `findings.py next --json`. Group the candidates so that no two agents touch
   the same Ansible role — the repo's parallel-sessions guidance already warns about several
   sessions editing a shared role, and two agents in one role is the same hazard with more
   agents. Present the grouping and **stop for approval**: spawning N Opus agents is not a
   routine action.
2. **Claim, then spawn.** Claim every issue in every batch serially, before spawning anything.
   This is what removes the race from the fan-out. The claim goes under the **orchestrator's**
   worktree name, because a subagent's worktree name is auto-generated and unknown until it
   starts — and a claim naming a worktree that does not exist yet would read as stale
   immediately. The orchestrator's worktree is live for the whole fan-out, so the claim is too.
3. **Spawn.** One Opus agent per batch, spawned with `isolation: "worktree"` and
   `model: "opus"` on the `Agent` call. Both are load-bearing and neither is a default: an
   `Agent` call without `isolation` runs in the orchestrator's own checkout, so every agent
   shares one working tree and commits over the others — the race the claim protocol assumes
   away. The worktree it gets is auto-named, per the measurement below. The brief carries the
   issue bodies, the claim the agent already holds, the repo's `land-after-merge` contract, the
   blocking wait on the `VERDICT:` line (a backgrounded `land.sh` with redirected output is not
   a harness-tracked child, so nothing wakes the agent when it finishes), the
   `close --fixed` restriction above, and the instruction to file anything it does not fix with
   `findings.py open`.
4. **Land.** Each agent goes all the way to a verified deploy. Every agent's `land.sh` queues on
   `/var/lock/server-git-tree.lock`, so the brief must say that exit 75 is a resume point to
   retry rather than a failure to report.
5. **Report.** A table of issue → worktree → PR → verdict. Any issue still claimed when the
   fan-out ends is named explicitly, so nothing is held silently.

### Width is unbounded

The skill takes no agent-count parameter. The bound is the host's cgroup configuration, which
exists already and is the right place for it — a per-skill number would be a second bound that
drifts from the first.

Two facts a wide fan-out runs into, measured on daniel-box 2026-09-05:

| Scope | MemoryHigh | MemorySwapMax | MemoryMax |
|---|---|---|---|
| `user-1000.slice` | 8G | 2G | infinity |
| `claude-rc.service` | 8G | 2G | infinity |

Host: 28 GB RAM, 16 cores, 7 GB swap.

The two planes carry **independent** caps, so a fan-out split across both can draw 16G plus 4G
of swap before either throttles. And `MemoryHigh` throttles rather than caps — the 2026-09-05
reclaim stall happened with it in force, leaving remote control unreachable for ~30 minutes
while the unit read `active (running)`. The failure mode of an over-wide fan-out is that stall,
not an OOM kill.

Filed as issue #1264; out of scope for this change.

The deploy lock serialises the other half. Every agent's landing queues on it, so past some width
the fan-out buys parallel *implementation* and no parallel *landing* at all.

## Testing

Following the repo's rule that a new check ships with a proof it can go red, and that a check
finding its own subject by pattern ships with a named member it must find:

The claim tests live in five files under `scripts/dev/tests/`, split by what each reads:

| File | What it covers |
|---|---|
| `test_findings_claim_record.py` | the comment format and the fold: who holds an issue, and how the parser is hardened |
| `test_findings_claim_plans.py` | the pure argv `plan_claim` and `plan_release` return |
| `test_findings_lib/claim_cli.py` | `claim`, `release`, `claims` and `reap` driven through `main()` |
| `test_findings_claim_staleness.py` | `claim_is_live` against invented worktree state |
| `test_findings_claim_reap_then_claim.py` | the reap-then-claim path `next` sends a session down |

Two of them carry the checks this page asked for by name:

- The staleness pair, named so a rule that stops matching fails its own test:
  `test_claim_is_stale_when_its_worktree_is_gone` and
  `test_claim_is_not_stale_when_its_worktree_is_dirty_with_a_dead_owner`.
- The non-vacuity assertion on the claim parser, which the page asked for and nothing wrote
  until #1285: `CLAIM_PARSER_FIXTURES` in `test_findings_claim_record.py` names each case,
  `REQUIRED_CLAIM_PARSER_CASES` asserts every name is present, and the verdicts are asserted
  per name. A rename then fails saying which member went missing, rather than passing over
  whatever survived.

## Documentation changes, same PR

- `docs/reference/backlog.md` is generated and a hook rejects hand edits, so the claim column
  goes into `scripts/docs/reference/backlog.py`.
- Root `CLAUDE.md` gains a routing row: picking up an open issue starts at `findings.py next`,
  then `/issue-fanout`.
- This page loses its "drafted, not started" banner when the work lands.
