---
name: issue-fanout
description: Dispatch parallel Opus agents at a batch of open GitHub issues, one worktree and one PR each, claiming every issue before any agent starts. Use when asked to work through the backlog, clear the open findings, or fan out on issues. Not for a single issue — claim it and work it directly.
allowed-tools: Bash, Read, Grep, Glob, Agent
---

# Fanning out on the backlog

Five steps, in order: triage, claim, spawn, land, report. Claiming happens **before** any
agent starts, under the orchestrator's own worktree name — that ordering is what makes the
fan-out race-free, and the rest of this skill exists to keep it that way.

## 1. Triage

Clear the claims whose worktree is gone first, then read what is free:

```bash
uv run python scripts/dev/findings.py reap
uv run python scripts/dev/findings.py next --json
```

`reap` releases every claim whose worktree no longer holds work — a session that died after
its PR landed, or a worktree somebody pruned. **This is the one place that invokes it**: no
cron and no hook does, and without it a stale claim sits on the register indefinitely. It
refuses outright rather than releasing anything if the git read fails, so a non-zero exit here
is a stop, not a warning.

`next` withholds `manual` issues, anything a LIVE claim holds, and anything an open PR already
closes — everything it prints is free to take. An issue whose claim is stale is offered,
marked `[stale claim by ...]`; `claim` reaps that claim itself on the way past, so a refusal
from `claim` means the claim is live and another session is really working it.

Group the results so that **no two agents touch the same Ansible role**. Read each issue's
cited file to find its role; two agents editing one role concurrently is the hazard
`CLAUDE.md`'s parallel-sessions section already warns about, with more agents.

**Stop here for approval.** Present the grouping — which issues, which batch, why they're
split this way — and wait. Spawning several Opus agents is not a routine action.

Done when: `reap` has run, every issue `next` returned is in exactly one batch, no two batches
share a role, and the operator has approved the grouping.

## 2. Claim before spawning, under the orchestrator's own worktree name

Read the orchestrator's own branch name first, as its own command — command substitution
(`$(git branch --show-current)`) is rejected outright by the auto-approve classifier and would
prompt on every claim:

```bash
git branch --show-current
```

Paste that name into a `claim` call per batch, run serially, before spawning anything:

```bash
uv run python scripts/dev/findings.py claim <n> <n> <n> --worktree <branch>
```

**Why the orchestrator's name, not the agent's.** A fanned-out agent cannot own a worktree the
orchestrator names — `EnterWorktree` with `name:` is refused from a subagent with a cwd
override, and `path:` is accepted only for every later command to be refused by the isolation
guard. The Agent tool exposes no `cwd` parameter, so the orchestrator cannot know an agent's
worktree name before spawning it either. Claiming under a name that doesn't exist yet would
read as stale immediately. The orchestrator's own worktree is live for the whole fan-out, so a
claim under it stays live for the whole fan-out.

Exit 3 means at least one issue in that call was refused — closed, `manual`, held by another
worktree, or lost a race. Drop the refused issue from its batch and say so; the rest of the
batch is still claimed.

Done when: every issue that will be spawned has a live claim under the orchestrator's
worktree, and every refusal is named out loud, not silently dropped.

## 3. Spawn

One Opus agent per batch. Each agent starts with none of this conversation's context, so its
brief must carry, in full:

- The **issue bodies verbatim** — not a paraphrase, not a summary.
- That the issues are **already claimed** under the orchestrator's worktree, and it must not
  claim them again.
- That its **first act** is to post a plain comment naming its own worktree, so the thread
  records which agent actually took the work — `findings.py` never learns this name, because
  the claim stays under the orchestrator's:

  ```bash
  git branch --show-current
  gh issue comment <n> --body "Worked by \`<its own branch>\`"
  ```

- That `land.sh` (the `land-after-merge` skill) is the landing path, and a hook denies
  hand-polling CI.
- That `deploy.sh` exit 75 is a **resume point to retry**, not a failure to report.
- That it closes a fixed issue with exactly `findings.py close <n> --fixed --pr <n>`, and may
  **not** use `--refuted` or `--accepted` — those are terminal and operator-only; an agent
  holding that authority could bury a real finding invisibly.
- That anything it does not fix gets filed with `findings.py open`, not left unmentioned.

**Width is unbounded.** This skill takes no agent-count parameter — the bound is the host
cgroup, not a number here, because a second number would drift from the first. `user-1000.slice`
and `claude-rc.service` carry independent 8G `MemoryHigh` caps (issue #1264), and `MemoryHigh`
throttles rather than kills, so an over-wide fan-out stalls in reclaim instead of failing loudly.
Keep batches to what the triage step actually produced; don't split further just to add width.

Done when: every batch has a spawned agent, and every brief names the claim already held, the
comment it must post first, the landing path, and the close restriction.

## 4. Land

Each agent goes through to a verified deploy, per `land-after-merge`. Every agent's `land.sh`
queues on the same `/var/lock/server-git-tree.lock`, so `deploy.sh` exit 75 is expected under
width and is a retry, never a report.

Done when: every agent has either landed (a `VERDICT:` line) or is still queued on the lock, and
none has been reported failed on a resume-point exit.

## 5. Report

A table of issue → worktree → PR → verdict, one row per issue the fan-out touched.

**Release what you don't finish.** Before this report goes out, release any issue no agent
finished:

```bash
uv run python scripts/dev/findings.py release <n> --worktree <orchestrator-branch> --reason "..."
```

Anything left claimed past this point sits until the next fan-out's triage step reaps it —
releasing explicitly is faster and says why.

Done when: the table accounts for every issue in every batch, and nothing is left claimed
without an explicit reason in the report.
