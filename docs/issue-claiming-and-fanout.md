# Issue claiming and fan-out

**Status: drafted, not started.** This page describes a system that does not exist yet. It
changes how every session picks up work, so a reader arriving from the backlog should find it
rather than discover the behaviour changed under them.

Several Claude sessions work this repo at once. `findings.py` gives the backlog a status field
and an owner-of-record, but nothing says which *session* is working an issue right now, and
nothing marks an issue as yours alone. Two sessions can pick the same issue, and an issue you
want to keep for yourself gets picked up by the next session that reads the register.

This spec adds three things: a claim protocol, a `manual` reservation, and a fan-out skill that
dispatches Opus agents at a batch of issues after triage.

## What already exists

`scripts/dev/findings.py` files, re-observes, escalates and closes findings as GitHub Issues.
It is split four ways: the CLI, `findings_model.py` (vocabulary and pure reads),
`findings_plans.py` (the `gh` argv every command plans) and `findings_gh.py` (the calls). Every
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

Two labels join `LABELS` in `findings_model.py` and are created by the existing `sync-labels`:

| Label | Colour role | Meaning |
|---|---|---|
| `manual` | state marker (grey) | Reserved for the operator. No session claims it, and no fan-out dispatches it. |
| `claimed` | state marker (grey) | A session is working this issue. A cheap filter; the payload is a comment. |

`manual` is one word rather than a prefix. The Renovate convention already uses "manual — …"
inside group names, so the bare word is mildly overloaded; the label namespace is separate
enough that this has not been worth a longer name.

## The claim record

A claim is a **comment**, following the machine-readable trailer convention
`findings_model.trailer()` already uses for fingerprints:

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
`Claim:` line opens, a matching `Released:` line closes, and the last unclosed claim wins.

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

All six extend `findings.py`, matching its plan-then-run split: the argv goes in
`findings_plans.py`, the parsing in `findings_model.py`, the `gh` calls in `findings_gh.py`.

### `claim <n>… --worktree <name> [--session <id>]`

Claims one or more issues for a worktree. Refuses an issue labelled `manual`. Refuses an issue
already held by a different live claim. Prints what it took and what it refused.

Exit 3 on refusal, matching the existing contract in the CLI that 3 means *nothing was written
because the issue refuses it*.

### `release <n>… [--reason <text>]`

The reverse state. Posts the release comment and removes the `claimed` label.

### `claims [--json]`

Every open claim: issue number, worktree, age, and whether the holder is live or stale. This is
the way to see the state — a claim protocol with no way to list claims is a one-way door.

### `reap [--dry-run]`

Releases every stale claim, printing why each was judged stale. `--dry-run` plans and writes
nothing, like every other command here.

### `next [--limit N]`

The picking command. Returns open issues that are: `claude`-labelled, not `manual`, not
live-claimed, and not already referenced by an open PR, ordered by the existing
`findings_model.sort_key`.

An ad-hoc session runs this instead of eyeballing `list` and guessing. The open-PR check is
what stops a session picking up work another session has already finished but not landed.

### `list --include-manual`

`list` gains the flag, but the default **marks** manual rows rather than hiding them. Hiding
them is how an issue like #1132 stops being visible to anyone, including the operator who
reserved it.

## What a fan-out agent may not do

A fanned-out agent may close an issue with `close --fixed --pr <n>` and nothing else.

`--refuted` and `--accepted` are terminal: `plan_open` returns early on both, so the fingerprint
can never be re-filed. An agent holding that authority could permanently bury a real finding,
and the burial would be invisible — the next review simply never re-files it. Both stay
operator-only.

This is a rule in the skill brief, not a flag guard, per the repo's escalation ladder: a check
is what a rule becomes after it has actually been violated.

## Attribution: the worktree name is the record

A worktree's name carries the issues it is working, so both lookups are derived and cannot go
stale:

| Case | Worktree | Branch |
|---|---|---|
| One issue | `issue-1132` | `worktree-issue-1132` |
| Several | `issue-1132+1140+1175` | `worktree-issue-1132+1140+1175` |

`EnterWorktree` allows letters, digits, dots, underscores and dashes, up to 64 characters,
which bounds a multi-issue name at roughly five issues.

What this buys:

- **Issue → session** is the `Claim:` comment.
- **Session → issue** is the branch name, parsed.
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
3. **Spawn.** One Opus agent per batch, each in its own named worktree. The brief carries the
   issue bodies, the claim the agent already holds, the repo's `land-after-merge` contract, the
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

- `test_findings_claim.py` — pure argv plan tests for `claim`, `release` and `reap`, in the same
  style as the existing plan tests.
- A fold-forward test over a synthetic comment list: claim, claim-and-release, and a
  release with no matching claim.
- The staleness pair, named so a rule that stops matching fails its own test:
  `test_claim_is_stale_when_its_worktree_is_gone` and
  `test_claim_is_not_stale_when_its_worktree_is_dirty_with_a_dead_owner`.
- A non-vacuity assertion on the claim parser: it must find a named fixture set, not merely a
  non-zero count.

## Documentation changes, same PR

- `docs/reference/backlog.md` is generated and a hook rejects hand edits, so the claim column
  goes into `scripts/docs/reference/backlog.py`.
- Root `CLAUDE.md` gains a routing row: picking up an open issue starts at `findings.py next`,
  then `/issue-fanout`.
- This page loses its "drafted, not started" banner when the work lands.
