---
name: memory-consolidation
description: Consolidate the project memory store — merge duplicates, retire stale entries, re-verify what survives, and propose a diff for review. Use when MEMORY.md is over its line cap, when the index has drifted from what sessions actually cite, or on a schedule. Proposes changes; never writes to the store itself.
---

# Memory consolidation

Read the memory store as a whole, from outside any one session's task, and propose what should
**leave** it. The store grows on its own — every session can append, nothing demotes — so a pass
whose output is only additions has failed.

## Why this is a separate pass, and not something a session does at the end

The session that did the work is the worst-placed writer of the memory about it. It is optimising
for finishing the task; the memory is an afterthought written from inside one run's view, which is
exactly the view that cannot see that four other entries already say the same thing. Separating
the memory-quality objective from the task-completion objective is the whole point — so run this
as its own session or subagent, and do not fold it into the end of a working session.

## Hard rules

- **Propose a diff. Do not write to the store.** Several worktree sessions append to `MEMORY.md`
  concurrently, so a pass that edits it directly races them and can silently drop an entry — one
  was already lost that way on 2026-08-28. Emit the proposed changes for the operator to apply.
- **A run that only appends is a failed run.** Every run reports under all four headings below,
  and `Retire` and `Merge` may not both be empty without an explicit statement of why.
- **Retiring is not deleting.** Move the file to `memory/archive/` and add its pointer to
  `archive/README.md`. A retired memory that turns out to still be true is then one `git mv` back.
- **Never retire on the survey alone.** `unreferenced` means no session cited it in the window,
  not that it is wrong. A B2 fact goes unmentioned for a month and is still true the moment
  someone touches B2. Age is a reason to *read* an entry, never a reason to drop it.

## Procedure

### 1. Get the evidence

```bash
uv run python scripts/dev/memory_survey.py --transcript-days 30
uv run python scripts/dev/memory_survey.py --duplicate-threshold 0.05   # if the default reports none
```

The survey reports the injected cost of the index, dead index links, orphans, per-entry
last-reference dates, and near-duplicate candidates. It makes no judgement — that is this pass.

Read its own docstring on what each number is worth. Every one is a proxy: `/context` reports the
real token share and the survey does not, and a transcript scan finds a slug that was *mentioned*,
which is weaker than one that was *acted on*.

### 2. Read the candidates, not just the survey

For every entry the survey flags, open it. The survey ranks; you decide. Specifically:

- **Dead links** — the index promises a file that is not there. Either restore the file from
  `archive/` or git history, or drop the pointer. This is the one condition that fails the survey.
- **Orphans** — a file nothing links to. It is invisible to every session, so it is either worth
  an index line or worth archiving. Leaving it is the one option that is always wrong.
- **Near-duplicates** — read both bodies. Merge into whichever is better written, keep the
  surviving name, and make the other's `[[links]]` point at it.
- **Unreferenced** — read it and ask whether it is still true, then treat age as evidence about
  the *index line*, not the entry: a fact worth keeping whose pointer nothing follows may belong
  in an agent's own memory directory instead (see below).

### 3. Check what a durable owner should have taken

The repo's escalation ladder is run-local note → memory fact → CLAUDE.md rule → executable check.
An entry that has been cited repeatedly and describes something a machine could enforce is a
candidate for promotion *out* of memory: a pytest guard, a prek hook, or a `validate/` rule ends
the entry rather than restating it. Promoting an entry removes its line from the index — that is
the point, and it is the cheapest kind of shrink because nothing is lost.

Lines already marked ENFORCED in `MEMORY.md` are the worked examples: they exist to name the check,
not to carry the fact.

### 4. Check whether an entry has a narrower consumer

An entry read by exactly one agent belongs in that agent's memory directory, not in the index every
session pays for. `home-assistant-engineer` is the working example — `memory: project` in its
frontmatter, its own `MEMORY.md` under `.claude/agent-memory/<agent>/`, which it reads at the start
of every run.

Two traps before moving anything:

- **Name the consumer from the skill, not from the topic.** `homelab-review`'s dated ledgers are
  read by the *dispatching* session at its priming step, not by the reviewer subagents, so moving
  them into reviewer memory would break priming. Read the skill and find where the file is read.
- **A cross-domain file stays put.** Duplicating one into several agent directories reinvents the
  consistency problem this pass exists to fix.

### 5. Emit the proposal

Four headings, always all four, each entry naming the file and the evidence:

```
## Merge
- `a.md` + `b.md` → `a.md` (overlap 0.34; b's second paragraph is the only unique content)

## Retire
- `c.md` → archive/ (superseded by the check in scripts/validate/foo.py, added PR #123)

## Re-verify
- `d.md` — re-read 2026-08-29 against ansible/roles/k8s/bar/tasks/main.yml:41; still accurate.

## Promote
- `e.md` → a pytest guard in ansible/tests/, cited 6 times in 30d and mechanically checkable
```

`Re-verify` is not filler. An entry nobody has contradicted is not thereby confirmed, and a dated
line saying someone checked it against real evidence is what stops the next pass re-litigating it.
Add the date and the `file:line` you checked against, or leave the entry out of the heading.

Close with the arithmetic: pointer lines before and after, and injected bytes before and after.
A pass that cannot state how much smaller the index got has not finished.

## Cadence

Run it when the SessionStart hook reports the index over its cap, and at each model upgrade
alongside the scaffolding delete pass — a newer model needs fewer of the entries that exist to
compensate for an older one.
