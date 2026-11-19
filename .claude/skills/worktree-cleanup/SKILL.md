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

Ask the pruner instead. `--check` runs the same four-layer ladder `--prune` deletes by:
ancestry, patch-id, content (`git merge-tree`), then the forge's record of which head it merged
(`scripts/lib/worktrees.py:merged_layer`).

```bash
uv run python scripts/dev/prune_worktrees.py --check <branch>
```

It prints `<branch>: landed (<layer>)` and exits 0
(`scripts/dev/tests/test_prune_worktrees.py::test_check_names_the_layer_that_settled_a_rebase_landed_branch`),
or `<branch>: unlanded` and exits 1
(`scripts/dev/tests/test_prune_worktrees.py::test_check_exits_1_for_a_branch_holding_unlanded_work`).
Exit 64 means the branch does not resolve (`scripts/dev/prune_worktrees.py:check`,
`scripts/lib/exit_codes.py:USAGE_ERROR`). Only the forge layer settles a squash-merged branch
that master has since drifted into a conflict with
(`scripts/lib/worktrees.py:forge_says_landed`), so a hand comparison of `merge-tree` SHAs
reads that branch as unlanded. Leave a landed tree for the pruner rather than removing it by hand.

## The pruner

```bash
uv run python scripts/dev/prune_worktrees.py            # report
uv run python scripts/dev/prune_worktrees.py --prune    # remove the merged, clean, unlocked
uv run python scripts/dev/prune_worktrees.py --gc       # the object-store repair alone
```

It applies the same content check (`scripts/lib/worktrees.py:is_merged`), so it collects
exactly what `ExitWorktree` refused.

**`--prune` also deletes the orphan `worktree-*` branches** — the ones no worktree holds whose
content is already on `origin/master` (#2430). Removing a worktree leaves its branch, and
roughly 249 had accumulated by 2026-09-24. Report-only runs count them; the containment check
is the same ladder, minus the forge lookup (`scripts/dev/prune_worktrees.py:landed_orphan_branches`).
The sweep lists `worktree-*` only (`scripts/dev/prune_worktrees.py:BRANCH_PREFIX`). A desktop-app
session's branch is `claude/<name>`, so `--prune` removes its worktree and leaves its branch.

**A prune also repairs the shared object store.** Removing a worktree makes its branch's objects
unreachable, and `git gc --auto` only runs once loose objects pass 6700 or packs pass 50 — so the
churn accumulates below the threshold and `git fetch` warns about it (#1435). `--prune` and `--gc`
both drop unreachable objects older than a day and remove a stale `gc.log`
(`scripts/lib/git.py:repair_object_store`). A weekly timer on daniel-box and daniel-server,
`homelab-worktree-sweep.timer`, runs `--prune --brief`
(`hosts/components/worktree-sweep/templates/worktree-sweep.sh.j2`), because a worktree-isolated
session's git commands are refused against the primary checkout: the sessions that see the
warning are the ones that cannot clear it. That timer is the only automatic remover — the
SessionStart banner reports and removes nothing, for the reason in
`scripts/dev/prune_worktrees.py:DECIDED: the banner REPORTS, the weekly timer PRUNES`.

**Locks.** A lock held by a *running* session is never overridden
(`scripts/lib/tests/test_worktrees.py::test_a_live_lock_wins_over_a_merged_branch`). A lock
whose process is gone is ignored
(`scripts/lib/tests/test_worktrees.py::test_a_stale_lock_does_not_keep_a_merged_worktree`),
because Claude Code does not release the lock when a session ends — which is why
a worktree the current session still holds stays `keep` until that session exits. Nothing you
can do from inside a worktree makes the pruner collect it.

`git worktree remove` refuses outright while a worktree is locked, and unlocking is a separate
step — a caller that skips the unlock and ignores the refusal reports success having removed
nothing, so the pruner unlocks first (`scripts/lib/worktrees.py:remove`).

**Live processes.** A lock is not the only sign of use: a session can run from a tree whose
lock names a dead pid, and deleting its `$CLAUDE_PROJECT_DIR` breaks every repo hook it runs
(#3887). `lib.worktrees.remove`, which both the pruner and `fanout.py place clean` call,
refuses a tree that any live process has as its cwd or its `CLAUDE_PROJECT_DIR`
(`scripts/lib/tests/test_worktrees.py::test_remove_refuses_a_tree_a_live_process_has_as_cwd_is_flagged`).
The refusal reads `could not remove <tree>: in use by a live process: pid N (...)`. The pruner
counts it as a skip, not a failure, so the weekly sweep still exits 0 and removes the tree the
next week (#7949,
`scripts/dev/tests/test_prune_worktrees.py::test_a_weekly_prune_that_skips_a_worktree_a_live_process_holds_exits_0`).
Find that session and end it rather than deleting the directory by hand. The root helper
behind `worktree-holders.socket` lets the check see another uid's processes
(`scripts/lib/worktrees.py:privileged_holders`). Without it, the
check sees only the processes of the uid running the removal, plus another uid's process inside
that uid's login slice (`scripts/lib/worktrees.py:processes_using`).

## Deleting the branch afterwards

**Try `git branch -d` first, whatever you expect it to say.** It refuses a branch not merged into
HEAD **or its upstream**, and the upstream half is what makes it look inconsistent on a
squash-merged branch.

**HISTORY —** Observed both ways on 2026-08-21: `worktree-gitops-eval-doc`, squash-merged
minutes earlier, deleted cleanly because the local `refs/remotes/origin/<branch>` had not been
pruned yet and still carried the tip — even though GitHub had already deleted the remote branch.
Four older squash-merged branches were refused, their tracking refs having since been pruned.

So the window in which `-d` works closes at the next `git fetch --prune`, not at the merge. After
that only `-D` does. A refusal is information: it tells you the tracking ref is gone, not that the
work is unlanded.

**`-D` is not reliably denied by the classifier.** Treat the classifier as judging the
situation rather than the flag — don't hand the operator a chore you can finish, and don't assume
the call in advance either way.

**HISTORY —** It was approved twice on 2026-08-27
(`worktree-pi-detached-container-arm`, `worktree-cron-kubeconfig-guard`), in both cases where the
session had just merged the PR whose head was that branch tip.

`prune_worktrees.py` tries `-d` first, so git arbitrates where it can, and falls back to `-D`
only for a branch its own containment ladder has already settled
(`scripts/dev/prune_worktrees.py:delete_branch`) — the ancestry test alone
misses every rebase- and squash-landed branch, which is most of them here
(`scripts/dev/tests/test_prune_worktrees.py::test_a_rebase_landed_branch_is_deleted_though_git_branch_d_refuses_it`).
It never forces a branch no layer settled.

## The stash trap

The stash stack is **shared across every worktree** of this repo, and other
sessions push and pop concurrently. Never use a bare `git stash` / `git stash pop` while
cleaning up. Prefer a temporary WIP commit; if you must stash, push with a unique message,
capture the entry's SHA from `git stash list --format='%H %gs'`, and restore with
`git stash apply <sha>`.
