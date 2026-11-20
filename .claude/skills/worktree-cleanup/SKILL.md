---
name: worktree-cleanup
description: Retire a finished Claude worktree in this repo. Use when ExitWorktree refuses to remove a worktree whose PR has already merged, when deciding whether a branch still holds unlanded work, or when sweeping merged worktrees with the worktree pruner (`~/.claude/hooks/prune-worktrees.py`). Covers the squash/rebase-merge blind spot and the lock rules.
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
ancestry, patch-id, content (`git merge-tree`), then the forge's record of which head it merged.

```bash
~/.claude/hooks/prune-worktrees.py --check <branch>
```

It prints `<branch>: landed (<layer>)` and exits 0, or `<branch>: unlanded` and exits 1.
Exit 64 means the branch does not resolve. Only the forge layer settles a squash-merged branch that
master has since drifted into a conflict with, so a hand comparison of `merge-tree` SHAs reads
that branch as unlanded. Leave a landed tree for the pruner rather than removing it by hand.

## The pruner

One pruner serves every repo, and the dotfiles repo owns it. `chezmoi apply` deploys it to
`~/.claude/hooks/prune-worktrees.py`, with its library in `~/.local/share/claude-worktree`. It
reads what this repo lets it do from `.claude/prune-worktrees.json` in the primary checkout.
The package's `README.md` lists that file's keys. Run it from anywhere inside the repo:

```bash
~/.claude/hooks/prune-worktrees.py            # report
~/.claude/hooks/prune-worktrees.py --prune    # remove the merged, clean, unlocked
~/.claude/hooks/prune-worktrees.py --gc       # the object-store repair alone
```

It applies the same content check `--check` does, so it collects exactly what `ExitWorktree`
refused. This repo's policy lets a patch-id or content match remove a tree (`landed_by`). In a
repo with no policy file, only ancestry and the forge remove one.

**`--prune` also deletes the orphan session branches.** Removing a worktree leaves its
branch. A session branch is `worktree-<name>` from `EnterWorktree` or `claude/<name>` from the
desktop app (`scripts/lib/worktree_owner.py:SESSION_BRANCH_PREFIXES`, which the policy's
`branch_prefixes` repeats), and the sweep touches no other branch. It deletes a local branch
only when all three hold:

- no worktree holds it;
- its work is on `origin/master` by the same four-layer ladder, the forge layer included, which
  is what settles a squash-merged branch master has since drifted from;
- the policy's `branch_holds` command does not print it. Here that command is
  `scripts/dev/held_branches.py`, which prints the head of every open PR and every branch a
  claim in the findings register names, stale claims included.

A holds command that fails keeps every landed branch
(`scripts/dev/tests/test_held_branches.py::test_main_fails_and_prints_no_branch_when_a_register_is_unread`).
The sweep never deletes a remote branch. To see what a sweep would delete and why, with a
count of the branches it keeps under each reason, run `--check` with no branch:

```bash
~/.claude/hooks/prune-worktrees.py --check
```

**A prune also repairs the shared object store.** Removing a worktree makes its branch's objects
unreachable, and `git gc --auto` only runs once loose objects pass 6700 or packs pass 50 — so the
churn accumulates below the threshold and `git fetch` warns about it. `--prune` and `--gc`
both drop unreachable objects older than a day and remove git's stale gc log.
A weekly timer on daniel-box and daniel-server, `homelab-worktree-sweep.timer`, runs
`--require 2 --prune --brief`
(`hosts/components/worktree-sweep/templates/worktree-sweep.sh.j2`), because a worktree-isolated
session's git commands are refused against the primary checkout: the sessions that see the
warning are the ones that cannot clear it. That timer is the only automatic remover. The
SessionStart banner reports and removes nothing, and the pruner's own SessionStart sweep stays
out of this repo, for the reason in
`hosts/components/worktree-sweep/templates/worktree-sweep.sh.j2:DECIDED: the banner REPORTS, the weekly timer PRUNES`.

**Locks.** A lock held by a *running* session is never overridden. A lock whose process is
gone is ignored, because Claude Code does not release the lock when a session ends — which is
why a worktree the current session still holds stays `keep` until that session exits. Nothing
you can do from inside a worktree makes the pruner collect it. A fan-out batch's lock names
its systemd unit, and it holds the tree while the unit runs
(`scripts/lib/tests/test_fanout_lock.py::test_a_merged_tree_whose_batch_unit_runs_is_kept`)
and for a day after the lock was written (`scripts/lib/fanout_lock.py`).

`git worktree remove` refuses outright while a worktree is locked, and unlocking is a separate
step — a caller that skips the unlock and ignores the refusal reports success having removed
nothing, so the pruner unlocks first.

**Live processes.** A lock is not the only sign of use: a session can run from a tree whose
lock names a dead pid, and deleting its `$CLAUDE_PROJECT_DIR` breaks every repo hook it runs.
The pruner's `remove`, which `fanout.py place clean` calls too (`scripts/lib/worktrees.py`),
refuses a tree that any live process has as its cwd or its `CLAUDE_PROJECT_DIR`. A prune prints
`kept <tree>: in use by a live process: pid N (...); removed once it exits`. The pruner
counts it as a skip, not a failure, so the weekly sweep still exits 0 and removes the tree the
next week. Find that session and end it rather than deleting the directory by hand. The root
helper behind `worktree-holders.socket` lets the check see another uid's processes. Without
it, the check sees only the processes of the uid running the removal, plus another uid's
process inside that uid's login slice.

**A host with no pruner, or a stale one, fails loudly.** The timer exits 127 with no launcher,
69 with no library and 78 when the deployed copy is older than `--require` names, and each
reaches Healthchecks.io as a failure
(`hosts/tests/test_worktree_sweep_cron_reports_its_exit.py::test_a_host_with_no_pruner_fails_the_run_and_pings_the_failure`).
The fix is `chezmoi apply` on that host, after a `git pull --ff-only` of its dotfiles checkout.

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

The pruner tries `-d` first, so git arbitrates where it can, and falls back to `-D` only for a
branch its own containment ladder has already settled, and only while the branch still points
at the commit it judged. The ancestry test alone misses every rebase- and squash-landed
branch, which is most of them here. It never forces a branch no layer settled.

## The stash trap

The stash stack is **shared across every worktree** of this repo, and other
sessions push and pop concurrently. Never use a bare `git stash` / `git stash pop` while
cleaning up. Prefer a temporary WIP commit; if you must stash, push with a unique message,
capture the entry's SHA from `git stash list --format='%H %gs'`, and restore with
`git stash apply <sha>`.
