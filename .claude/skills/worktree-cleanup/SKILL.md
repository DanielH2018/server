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

## Deleting the branch afterwards

**Try `git branch -d` first, whatever you expect it to say.** It refuses a branch not merged into
HEAD **or its upstream**, and the upstream half is what makes it look inconsistent on a
squash-merged branch. Observed both ways on 2026-08-21: `worktree-gitops-eval-doc`, squash-merged
minutes earlier, deleted cleanly because the local `refs/remotes/origin/<branch>` had not been
pruned yet and still carried the tip — even though GitHub had already deleted the remote branch.
Four older squash-merged branches were refused, their tracking refs having since been pruned.

So the window in which `-d` works closes at the next `git fetch --prune`, not at the merge. After
that only `-D` does. A refusal is information: it tells you the tracking ref is gone, not that the
work is unlanded.

**`-D` is not reliably denied by the classifier.** It was approved twice on 2026-08-27
(`worktree-pi-detached-container-arm`, `worktree-cron-kubeconfig-guard`), in both cases where the
session had just merged the PR whose head was that branch tip. Treat the classifier as judging the
situation rather than the flag — don't hand the operator a chore you can finish, and don't assume
the call in advance either way.

`prune_worktrees.py` uses `-d` deliberately, so git arbitrates, and it reports rather than reaps
anything the ancestor test misses.

## The stash trap

The stash stack and the index are **shared across every worktree** of this repo, and other
sessions push and pop concurrently. Never use a bare `git stash` / `git stash pop` while
cleaning up. Prefer a temporary WIP commit; if you must stash, push with a unique message,
capture the entry's SHA from `git stash list --format='%H %gs'`, and restore with
`git stash apply <sha>`.
