---
name: worktree-cleanup
description: Retire a finished Claude worktree in this repo. Use when ExitWorktree refuses to remove a worktree whose PR has already merged, when deciding whether a branch still holds unlanded work, or when sweeping merged worktrees with prune_worktrees.py. Covers the squash/rebase-merge blind spot and the lock rules.
allowed-tools: Bash, Read, Grep, Glob
---

# Retiring a worktree

## `ExitWorktree` refuses a merged worktree, and it is not wrong to

It reports `N commits on <branch>` after a squash or rebase merge. Both land the content on
master under **new SHAs**, so the tool cannot see that the work survived — the branch reads
byte-identically to one holding real unlanded work.

**Do not pass `discard_changes` to argue with it.** That flag is how you lose work that was
never landed, and from the tool's side the two cases are indistinguishable.

Verify by content instead. The branch has nothing left to give when its merge result equals
master's tree:

```bash
git merge-tree --write-tree origin/master <branch>
git rev-parse origin/master^{tree}
```

Equal SHAs mean the work is in. Leave the tree for the pruner rather than removing it by hand.

## The pruner

```bash
uv run python scripts/dev/prune_worktrees.py            # report
uv run python scripts/dev/prune_worktrees.py --prune    # remove the merged, clean, unlocked
```

It applies the same content check, so it collects exactly what `ExitWorktree` refused.

**Locks.** A lock held by a *running* session is never overridden. A lock whose process is gone
is ignored, because Claude Code does not release the lock when a session ends — which is why
a worktree the current session still holds stays `keep` until that session exits. Nothing you
can do from inside a worktree makes the pruner collect it.

`git worktree remove` refuses outright while a worktree is locked, and unlocking is a separate
step — without it the command reports success having removed nothing.

## The stash trap

The stash stack and the index are **shared across every worktree** of this repo, and other
sessions push and pop concurrently. Never use a bare `git stash` / `git stash pop` while
cleaning up. Prefer a temporary WIP commit; if you must stash, push with a unique message,
capture the entry's SHA from `git stash list --format='%H %gs'`, and restore with
`git stash apply <sha>`.
