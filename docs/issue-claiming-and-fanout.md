# Issue claiming and fan-out

Several Claude sessions work this repo at once. `findings.py` gives the backlog a status field
and an owner-of-record, but nothing says which *session* is working an issue right now, and
nothing marks an issue as yours alone. Two sessions can pick the same issue, and an issue you
want to keep for yourself gets picked up by the next session that reads the register.

This page describes three things in `scripts/dev/findings.py` and `.claude/skills/issue-fanout/`:
a claim protocol, a `manual` reservation, and a fan-out skill that dispatches Opus agents at a
batch of issues after triage.

## What already exists

`scripts/dev/findings.py` files, re-observes, escalates and closes findings as GitHub Issues.
It is split four ways: the CLI, `findings_lib/issue_model.py` (vocabulary and pure reads),
`findings_lib/plans.py` (the `gh` argv every command plans) and `findings_lib/gh_calls.py` (the calls). Every
command plans a list of argv first, then runs it, so `--dry-run` writes nothing.

`scripts/lib/worktrees.py` decides whether a session worktree is done with: `is_merged`,
`classify` and `worktree_facts`. `scripts/dev/prune_worktrees.py` is only the CLI over it, and
nothing else imports the CLI. `.claude/hooks/session-health.py` imports `parse_worktree_list`
and `session_is_alive` from `lib.worktrees` to print the other-live-sessions banner. Since
#2133 those two, `Worktree` and the cherry and merge-tree readers come from the
`claude_worktree` module the dotfiles repo deploys to `~/.local/share/claude-worktree`, and
`lib.worktrees` re-exports them. `scripts/lib/_claude_worktree.py` is the bootstrap, and it
raises rather than falls back, so a host without the deploy prunes and reaps nothing.

Every open issue except #3 carries the `claude` label. #3 is Renovate's Dependency Dashboard,
created 2026-06-06 with no labels. Scoping claims to `claude`-labelled issues therefore costs
nothing, and the claim protocol keeps that scope.

## Why the claim is not an assignee

`gh` authenticates as a single account. Assigning an issue would name the operator, not the
session, so every claim would be indistinguishable from every other. The claim has to carry a
session-level identity itself.

## Vocabulary

`LABELS` in `findings_lib/issue_model.py` holds every label that `sync-labels` creates. The table
below is generated from it. This spec added two of them, `manual` and `claimed`, both grey state
markers. `claimed` is a cheap filter, because the payload is a comment.

--8<-- "assets/generated/fragments/findings-labels.md"

`manual` is one word rather than a prefix. The Renovate convention already uses "manual — …"
inside group names, so the bare word is mildly overloaded; the label namespace is separate
enough that this has not been worth a longer name.

A third label is dated and therefore not in `LABELS`: `not-before:<YYYY-MM-DD>`, one label per
date, created on demand by `plan_ensure_label` the first time `defer` or `open --not-before`
needs it. It means "workable on that date, not earlier" and it expires by comparison: `next`
and `claim` read it against today's UTC date, so nobody clears it. That is the property
`manual` lacks. #1288 could be re-derived only once seven days of a metric existed, said so in
its body, and was claimed, read and released unchanged six times in four days because the only
withholding label was permanent and meant the wrong thing (#1739).

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
orchestrator claims every issue of a batch *before* that batch's agent starts, so a fan-out has
no internal race. `fanout.py place launch` takes those claims itself. The only residual race is between independent ad-hoc sessions, where the
cost of losing is a duplicated triage rather than corruption. The read-back handles it —
the session whose claim comment sorts first holds the issue, and the loser releases.

## Staleness: a claim expires with its worktree, not with a pid

A claim is reclaimable when either holds:

- the worktree it names is absent from `git worktree list`, or
- `lib.worktrees.classify` already judges that worktree REMOVABLE — merged into `origin/master`,
  clean, and holding no live session lock.

A claim is **not** reclaimable when the named worktree still exists with uncommitted changes,
even if its lock owner is dead. That case is the 2026-09-05 incident: the container restarted,
killed 14 agents mid-work, and every worktree kept its uncommitted edits so each session was
resumed in place. Expiring those claims would have handed half-finished issues to a second
agent, which is worse than leaving them held.

There is no heartbeat and no TTL. Both would have expired exactly those claims, because the
process was gone while the work was not.

Reusing `lib.worktrees` is what makes this self-healing without either. The staleness
question — is this worktree done with — is a question that module already answers for
`prune_worktrees.py`, and `session-health.py` already imports it from outside `scripts/`.

A claim on another repo's register is judged against that repo's worktrees. With
`--repo DanielH2018/dotfiles`, `findings.py` reads `git worktree list` in the chezmoi checkout
and merges against its `origin/main`, through the checkout table `REGISTER_CHECKOUTS` in
`scripts/dev/findings_lib/boundaries.py`. A repo missing from that table makes `claim`,
`claims`, `reap` and `next` exit 2. Against this repo's worktrees, every claim on it would
name a branch nothing here has checked out, so each would read as stale.

## Commands

`findings.py --help` lists the subcommands, and `findings.py <cmd> --help` owns each one's
flags. This page does not restate the flags, because a third copy drifts whenever a flag changes.
The table of subcommands is generated from the `add_parser` calls in `findings_lib/cli.py`.

--8<-- "assets/generated/fragments/findings-subcommands.md"

`docs/reference/scripts.md` maps the modules behind the CLI. The argv goes in
`findings_lib/plans.py`, the parsing in `findings_lib/issue_model.py`, and the `gh` calls in
`findings_lib/gh_calls.py`. Every write command has a dry-run mode that plans and writes nothing.

Three design rules hold across the subcommands, and no docstring states them:

- **A claim that reads stale the moment it lands is worse than no claim.** It reads as
  protection while `next` re-offers the issue and `reap` releases it. So `claim` checks the
  worktree it is given before it writes. A name that matches no branch fails that check, and
  so does a branch git never locks, such as `master`.
- **No view hides a row.** `list` marks manual, deferred and claimed issues instead of dropping
  them, and `next` names each deferred issue after the free rows. A hidden row is how an issue
  like #1132 stops being visible to anyone, including the operator who reserved it.
- **Every path that closes or reopens an issue releases its claim.** `claims`, `reap` and
  `next` read only open issues, so a claim stranded on a closed issue leaves every view at once.
  A PR body's `Closes #<n>` posts no release, and the next re-observation that reopens the issue
  would otherwise bring the claim back live.

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

**An orchestrator reads its own claims with `claims --worktree <its branch>`.** The filter keeps
the claims that branch holds and adds the branch of every standing batch a `fanout.py place
launch` run under it started in the same register, read from the run manifests. A batch in this
repo is claimed under the orchestrator's branch, so the branch alone covers it. A dotfiles batch
is claimed under its own branch, and only the manifest ties it back. The filter counts the claims
it drops rather than hiding them, so an empty result does not read as a register with nothing claimed.

A session an operator drives can still name its own worktree `issue-1132`, because it has no
cwd override. Only the fan-out is constrained.

The banner improvement is recoverable later with a gitignored `.claim` marker file written into
the worktree root, which `session-health.py` could read with no network call. That is not in
scope here.

## Placement across hosts

`scripts/dev/fanout.py place` names each worktree `fanout-<batch>` because it creates the
worktree itself rather than going through the Agent tool, which only ever produces the
`agent-<hash>` name above. Claims stay under the orchestrator's own worktree because
`findings.py` reads `git worktree list` on daniel-box only, so it cannot see a claim naming a
worktree that lives on daniel-server. A daniel-server agent stops at `gh pr create` and does
not land its PR, because only daniel-box runs deploys. The manifest under
`~/.claude/fanout/<run-id>.json` is what `status`, `clean`, and the SessionStart banner's
`remote_fanout_lines` (`.claude/hooks/hooklib/worktree_lines.py`) all read to find a fan-out's
live worktrees on the other host.

The dispatcher scores a host on memory headroom, and an agent is throttled by two cgroup caps
rather than one: `user.slice`, the fleet, and `user-1000.slice`, the login plane it runs in as
a transient user service. It reads both and takes the smaller headroom, so the tighter cap
decides and a host the fleet number alone would allow can still be refused. Each placement
spends one 2.5 GiB reservation (`RESERVATION_BYTES` in `scripts/dev/fanout_lib/placement.py`),
and the host with the most left takes the next batch.

`clean` records each removal in the manifest, because the act destroys its own evidence: the
remote leg reads the worktree it deletes, so a later pass has nothing left to ask. A batch
already removed is skipped without an ssh call, `status` reports it as `cleaned` rather than
reading a host whose report file went with the worktree, and the manifest is deleted only once
every batch is removed.

`launch` refuses daniel-server as a host until the operator registers its SSH key as a GitHub
signing key once (`gh ssh-key add ~/.ssh/id_ed25519.pub --type signing`): the repo's ruleset
requires a verified commit signature, and a PR signed with an unregistered key sits `BLOCKED`
with every check green (PR #1572 needed a hand re-sign), so the dispatcher checks the key
before it spends an agent.

`launch` fast-forwards a host with no GitOps tick to `origin/master` before it creates the
worktree, between the `fetch` and the `worktree add`. `.claude/settings.json` names every hook
by an absolute path into that host's primary checkout, so a worktree cut from a fresher
`origin/master` registers hook scripts the checkout does not have yet: `/bin/sh` exits 127,
Claude Code logs a non-blocking hook error, and the tool call runs with the guard skipped.
About 2,100 Bash calls on daniel-server ran with no `PreToolUse:Bash` guard across two windows
in September 2026 — one opened by the commit adding `inject-nested-docs`, one by the commit
adding `bash-pretool.sh`, each closing when that checkout next pulled (issue #2675).

The fast-forward is gated on HEAD being `master`, because `merge --ff-only origin/master` on a
checkout parked on another branch takes master's commits onto that branch. A refusal —
detached HEAD, a local commit, a tree `--ff-only` cannot cross — refuses the launch: the host's
hook state is then unknown, which is exactly when a batch must not be placed on it.

**It does nothing on daniel-box.** The tick pulls that checkout every 10 minutes, so the window
there is bounded already, and the tick takes `/var/lock/server-git-tree.lock` for its own
`--ff-only` merge. A launch does not take that lock. The tick holds it for its whole unit run,
which can outlast `LAUNCH_TIMEOUT_S` (120 seconds), so taking it would turn a bounded stale-hook
window into a failed launch. A hand `deploy.sh` holds the same lock only for its snapshot
([ADR-0017](adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md)).

That check compares each candidate host's `user.signingkey` against the account's live list,
read with `gh api /users/<login>/ssh_signing_keys`. `launch` exits 6 when no candidate host
passes, and also when the list could not be read at all — a `gh` outage refuses the launch
rather than placing a batch whose PR might not merge. `read` prints the same verdict per host
as `signing=ok|unverified|unknown`, where `unknown` is that unreadable list, so it answers
what exit 6 would refuse without spending an agent.

## The `/issue-fanout` skill

`.claude/skills/issue-fanout/SKILL.md` owns the five steps (triage, claim, spawn, land, report),
the brief every agent receives, and the width bound. This page owns the claim protocol the
steps rely on.

Three facts from that protocol shape the skill:

- **Every issue of a batch is claimed before that batch's agent starts.** `launch` takes the
  claims itself, under the orchestrator's worktree name, because a subagent's worktree name is
  unknown until it starts and a claim naming a worktree that does not exist yet reads as stale.
- **A landing queues on per-service locks, not on the tree lock.** `deploy.sh` holds
  `/var/lock/server-git-tree.lock` only for its snapshot
  ([ADR-0017](adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md)), so two agents landing
  disjoint services proceed together. A `deploy.sh` exit 75 is a resume point to retry, not a
  failure to report.
- **Width follows memory headroom, not a count.** `scripts/dev/fanout.py place` reads the
  `user.slice` and `user-1000.slice` caps and places each batch on the host with the most left.
  `MemoryHigh` throttles rather than kills, so an over-wide fan-out stalls the host, as it did
  on 2026-09-05 (issue #1264).
