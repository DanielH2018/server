#!/usr/bin/env python3
"""Report and remove Claude session worktrees under .claude/worktrees/ that are done with.

Several Claude sessions work this repo at once, each in its own worktree. Nothing removed
them when the work merged, so merged trees accumulated on disk alongside the live ones and
it stopped being obvious which was which.

A worktree is removable only when all three hold: its branch is merged into
origin/master, it has no uncommitted changes, and no live session holds its lock.

"Merged" is checked three ways, cheapest first: ancestry, then patch-id, then content. PRs
land here rebased or squashed, never fast-forwarded, so the branch tip is not an ancestor of
origin/master — on ancestry alone this script reported "nothing to remove" while merged trees
piled up. `git cherry` compares by patch-id and settles the rebase case. A squash defeats
both, because collapsing several commits into one leaves no patch-id to match; `git merge-tree`
settles that by asking whether merging the branch would change master at all. See is_merged.

The lock is the interesting one. Claude Code locks a session's worktree with a reason
naming the owning process — `claude session <name> (pid 1285937 start 2164388)` — and does
not release it when the session ends. Treating any lock as "in use" would therefore keep
every abandoned worktree forever. The `start` field is the process start time from
/proc/<pid>/stat, so it distinguishes a live owner from a dead one whose pid has since been
reused, and a lock whose owner is gone is ignored rather than obeyed.

Usage:
    uv run python scripts/dev/prune_worktrees.py            # report only (default)
    uv run python scripts/dev/prune_worktrees.py --prune    # also remove the removable ones
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.git import git_stdout

REMOVABLE = "removable"
KEEP = "keep"
ORPHAN = "orphan"

LOCK_OWNER = re.compile(r"\(pid (\d+) start (\d+)\)")


@dataclass
class Worktree:
    """One entry from `git worktree list --porcelain`.

    Attributes:
        branch: the checked-out branch, or None when the worktree is detached.
        lock_reason: the reason text `git worktree lock` recorded, empty when unlocked.
    """

    path: str
    head: str
    branch: str | None
    locked: bool
    lock_reason: str = ""


def parse_worktree_list(porcelain: str) -> list[Worktree]:
    """Parse `git worktree list --porcelain` into records, primary checkout first."""
    trees: list[Worktree] = []
    path = head = branch = None
    locked, reason = False, ""
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
            head, branch, locked = None, None, False
        elif line.startswith("HEAD "):
            head = line[len("HEAD ") :]
        elif line.startswith("branch "):
            branch = line[len("branch ") :].removeprefix("refs/heads/")
        elif line == "locked" or line.startswith("locked "):
            locked = True
            reason = line[len("locked ") :] if line.startswith("locked ") else ""
        elif line == "" and path is not None:
            trees.append(Worktree(path, head or "", branch, locked, reason))
            path = head = branch = None
            locked, reason = False, ""
    if path is not None:
        trees.append(Worktree(path, head or "", branch, locked, reason))
    return trees


def session_is_alive(lock_reason: str) -> bool:
    """Is the process named in a worktree's lock reason still running?

    The reason Claude Code writes carries the owning pid and its start time, e.g.
    `claude session foo (pid 1285937 start 2164388)`. Comparing the start time against
    /proc/<pid>/stat rejects a pid that has been reused since the session died. A reason
    in any other format is treated as live: an unrecognized lock is someone else's, and
    guessing wrong destroys work.
    """
    match = LOCK_OWNER.search(lock_reason)
    if not match:
        return True
    pid, start = match.group(1), match.group(2)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # The comm field can itself contain spaces and parentheses, so field numbering is only
    # reliable after the final ')'. starttime is field 22, the 20th of those that follow.
    fields = stat.rpartition(")")[2].split()
    return len(fields) > 19 and fields[19] == start


def find_orphan_dirs(worktrees_dir: str, registered: set[str]) -> list[str]:
    """Directories under .claude/worktrees/ that git no longer tracks as worktrees.

    `git worktree prune` deregisters a tree whose administrative data is gone but leaves
    the directory behind, so these are invisible to `git worktree list` and to the removal
    path below — they have to be reported separately and deleted by hand.
    """
    try:
        entries = sorted(Path(worktrees_dir).iterdir())
    except OSError:
        return []
    return [str(e) for e in entries if e.is_dir() and str(e) not in registered]


def classify(tree: Worktree, merged: bool, dirty: bool) -> tuple[str, str]:
    """Return (verdict, reason) for one worktree.

    Reasons are reported in priority order so the output names the blocking condition a
    person would act on first, rather than listing every condition that happens to fail.
    """
    if tree.locked and session_is_alive(tree.lock_reason):
        return KEEP, f"in use — {tree.lock_reason or 'locked'}"
    if dirty:
        return KEEP, "uncommitted changes"
    if tree.branch is None:
        return KEEP, "detached HEAD — no branch to check"
    if not merged:
        return KEEP, f"{tree.branch} not merged into origin/master"
    if tree.locked:
        return REMOVABLE, f"{tree.branch} merged, clean, lock owner is dead"
    return REMOVABLE, f"{tree.branch} merged, clean, unlocked"


def _git(args: list[str], cwd: str | None = None) -> str:
    return git_stdout(*args, cwd=cwd, check=False)


def cherry_says_merged(cherry_output: str) -> bool:
    """Read `git cherry origin/master <head>` output: True when every commit is upstream.

    One line per commit on `head`, prefixed `-` when an equivalent patch is already on
    origin/master and `+` when it isn't. No lines means nothing is ahead of upstream, which
    is merged.
    """
    lines = [line for line in cherry_output.splitlines() if line.strip()]
    return all(line.startswith("-") for line in lines)


def merge_tree_says_contained(merge_tree_stdout: str, master_tree: str) -> bool:
    """Read `git merge-tree --write-tree origin/master <head>`: True when the merge is a no-op.

    The command prints the OID of the tree merging the branch would produce. When that equals
    origin/master's own tree, the branch has nothing master does not already hold — which is
    what a squash merge leaves behind, and what neither ancestry nor patch-id can see, because
    a squash keeps the content while discarding the commits that carried it.

    This asks about content, not history, so it also covers the ancestry and rebase cases the
    two cheaper checks handle first. It is last because it is the expensive one: it performs a
    real merge.

    Empty input is a failure to read a verdict, not a match, so it returns False — both
    arguments must be present for a comparison to mean anything.
    """
    lines = [line.strip() for line in merge_tree_stdout.splitlines() if line.strip()]
    master = master_tree.strip()
    if not lines or not master:
        return False
    return lines[0] == master


def is_merged(repo: str, head: str, branch: str = "") -> bool:
    """True when `head`'s work is already on origin/master, by ancestry or by patch.

    Ancestry alone misses how PRs actually land here. `gh pr merge --rebase` replays the
    commits onto master as new objects, so the branch tip is never an ancestor of
    origin/master and a merge-base check calls every landed worktree unmerged — which is why
    the pruner reported "nothing to remove" indefinitely while merged trees piled up. The
    ancestry check is kept because it is cheap and settles the fast-forward and merge-commit
    cases; `git cherry` compares by patch-id and settles the rebase case.

    Squash merges defeat both: several commits collapse into one, so the tip is not an
    ancestor and no patch-id matches either. `git merge-tree` settles that case by asking a
    different question — not "are these commits upstream" but "does this branch still have
    anything to give master". See merge_tree_says_contained.

    `git merge-tree` in turn fails on a squash-merged branch once master has DRIFTED into a
    conflict on a file the branch also touched: it exits non-zero, which is the right local
    answer (no verdict) and the wrong final one, so the tree sits there forever. The fourth
    layer asks the forge which head SHA it actually merged. It runs last because it is the only
    one needing a network round-trip and credentials.

    All four failures are closed: an unknown reads as NOT merged, because this decides what
    to DELETE.
    """
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", head, "origin/master"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if ancestor.returncode == 0:
        return True
    cherry = subprocess.run(
        ["git", "cherry", "origin/master", head],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    # A failed `git cherry` prints nothing, and empty output otherwise means "merged" — so
    # the return code has to gate this, or an unknown ref would read as safe to delete.
    if cherry.returncode != 0:
        return False
    if cherry_says_merged(cherry.stdout):
        return True
    master_tree = subprocess.run(
        ["git", "rev-parse", "origin/master^{tree}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if master_tree.returncode != 0:
        return False
    # Exit is non-zero on a conflict, and on a git too old for --write-tree (added in 2.38).
    # Both mean "no verdict", which must read as not merged.
    merged_tree = subprocess.run(
        ["git", "merge-tree", "--write-tree", "origin/master", head],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if merged_tree.returncode == 0 and merge_tree_says_contained(
        merged_tree.stdout, master_tree.stdout
    ):
        return True
    # Fourth and last: squash-merged AND master has since drifted into a conflict on a file the
    # branch also touched. `git merge-tree` then exits non-zero, which is the right local answer
    # ("no verdict") and the wrong final one — the branch landed days ago and the tree sits there
    # forever. Observed 2026-08-27: worktree-review-2026-08-24-remediation, landed as PR #400 on
    # 2026-08-24, held by a later master change to wg-easy/tasks/main.yml.
    #
    # Ask the forge, which knows what it merged. This runs LAST because it is the only check
    # needing a network round-trip and credentials; every branch the local checks settle never
    # reaches it. No `gh`, no auth, or no answer all mean no verdict, which reads as not merged.
    if not branch:
        return False
    pr_list = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "merged",
            "--head",
            branch,
            "--json",
            "headRefOid",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if pr_list.returncode != 0:
        return False
    return pr_head_says_merged(pr_list.stdout, head)


def pr_head_says_merged(stdout: str, head: str) -> bool:
    """Read `gh pr list --state merged --head <branch> --json headRefOid`:

    True when one of those merged PRs was merged from exactly this commit.

    Matching on the head SHA, never on "a merged PR exists for this branch name". Branch names are
    reused here — one session landed three PRs from `worktree-pi-detached-container-arm` on
    2026-08-27, each with a different tip — so a name match would delete a branch carrying work that
    never landed. SHA equality is the whole guarantee.
    """
    try:
        prs = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(prs, list):
        return False
    return any(isinstance(p, dict) and p.get("headRefOid") == head for p in prs)


def is_dirty(path: str) -> bool:
    return bool(_git(["status", "--porcelain"], cwd=path))


def remove(repo: str, tree: Worktree) -> tuple[bool, str]:
    """Unlock if needed, then remove.

    Never --force: git's own refusal on a tree with uncommitted or untracked files is the backstop
    that makes auto-unlock safe here — classify() only marks a locked tree REMOVABLE once
    session_is_alive() has confirmed the owner is dead, so this never releases a lock a live session
    still holds. Without the unlock, `git worktree remove` fails outright on a locked tree ("cannot
    remove a locked working tree") and the whole prune silently no-ops while reporting the tree as
    removed.
    """
    if tree.locked:
        subprocess.run(
            ["git", "worktree", "unlock", tree.path],
            cwd=repo,
            capture_output=True,
            check=False,
        )
    result = subprocess.run(
        ["git", "worktree", "remove", tree.path],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0, result.stderr.strip()


def primary_checkout() -> str | None:
    """The checkout the worktrees hang off, or None when we are not in a git repo.

    Not the current one: worktrees live under the primary's .claude/worktrees/, and
    --show-toplevel run from inside a worktree returns the worktree itself, which made the
    orphan scan look in a directory that doesn't exist.
    """
    common_dir = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"])
    return str(Path(common_dir).parent) if common_dir else None


def survey(repo: str) -> list[tuple[str, Worktree, str]]:
    """(verdict, worktree, reason) for every session worktree, primary excluded."""
    trees = parse_worktree_list(_git(["worktree", "list", "--porcelain"], cwd=repo))
    out = []
    for tree in trees[1:]:
        verdict, reason = classify(
            tree,
            merged=is_merged(repo, tree.head, tree.branch),
            dirty=is_dirty(tree.path),
        )
        out.append((verdict, tree, reason))
    return out


def brief() -> int:
    """One line per removable worktree; silent when there is nothing to remove.

    This exists for the SessionStart banner, which prints nothing on a healthy day and has to
    stay cheap to read. Claude Code's own worktree keeper already lists every branch whose
    commits landed by squash or rebase, but each line ends by asking the reader to go run
    `gh pr list --state merged --head <branch>` themselves — the lookup is_merged() already
    performs. This prints the answer instead of the homework.
    """
    repo = primary_checkout()
    if repo is None:
        return 0
    removable = [
        (tree, reason) for verdict, tree, reason in survey(repo) if verdict == REMOVABLE
    ]
    if not removable:
        return 0
    print(f"\U0001f9f9 {len(removable)} merged worktree(s) can be removed:")
    for tree, reason in removable:
        print(f"  {Path(tree.path).name} — {reason}")
    print("  → uv run python scripts/dev/prune_worktrees.py --prune")
    return 0


def main() -> int:
    """Report each session worktree's removable/keep/orphan verdict, and prune with `--prune`.

    Exits 1 when not inside a git repository, 0 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prune",
        action="store_true",
        help="remove the worktrees reported as removable (default: report only)",
    )
    parser.add_argument(
        "--brief",
        action="store_true",
        help="one line per removable worktree and nothing else; silent when clean",
    )
    args = parser.parse_args()

    if args.brief:
        return brief()

    repo = primary_checkout()
    if repo is None:
        print("not inside a git repository", file=sys.stderr)
        return 1

    tracked = {
        t.path
        for t in parse_worktree_list(
            _git(["worktree", "list", "--porcelain"], cwd=repo)
        )
    }
    for path in find_orphan_dirs(str(Path(repo) / ".claude" / "worktrees"), tracked):
        print(
            f"[{ORPHAN:9}] {path}\n            git does not track this — remove by hand"
        )

    surveyed = survey(repo)
    if not surveyed:
        print("no session worktrees")
        return 0

    removable = []
    for verdict, tree, reason in surveyed:
        print(f"[{verdict:9}] {tree.path}\n            {reason}")
        if verdict == REMOVABLE:
            removable.append(tree)

    if not removable:
        print("\nnothing to remove")
        return 0

    if not args.prune:
        print(f"\n{len(removable)} removable — re-run with --prune to remove")
        return 0

    for tree in removable:
        ok, error = remove(repo, tree)
        if ok:
            print(f"removed {tree.path}")
        else:
            print(f"could not remove {tree.path}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
