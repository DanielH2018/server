"""Pure readers over a repository's Claude session worktrees.

Two scripts prune the worktrees Claude Code creates under `.claude/worktrees/`: the
dotfiles `prune-worktrees.py` SessionStart hook, and the server repo's
`scripts/dev/prune_worktrees.py`. They agree on how to READ a worktree — the porcelain
list, whether a lock's owner is still running, whether `git cherry` or `git merge-tree`
says a branch has landed — and disagree on what to DO about it: the hook reports a
squash-merge match and never removes it, the server script removes it and asks the forge
first. This module is the shared reading; each caller keeps its own delete authority.

Every function here takes text and returns a verdict, or runs a single read-only git
query. None of them removes anything. A caller that needs the deployed copy imports it
from `~/.local/share/claude-worktree` (`CLAUDE_WORKTREE_HOME` overrides the path), the
way `claude_guard` is reached from `~/.local/share/claude-guard`.

Python 3.10 is the floor, not 3.14: the SessionStart hook runs under the system
interpreter, so nothing here may use syntax the system python3 lacks.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
    # The comm field can itself contain spaces and parentheses, so field numbering is
    # only reliable after the final ')'. starttime is field 22, the 20th of those after.
    fields = stat.rpartition(")")[2].split()
    return len(fields) > 19 and fields[19] == start


def cherry_says_landed(cherry_output: str, *, empty_means: bool) -> bool:
    """Read `git cherry <target> <head>`: True when every commit is on the target.

    One line per commit on `head`, prefixed `-` when a patch-identical commit is already
    on the target and `+` when none is. Any `+` line is a commit the target lacks.

    `empty_means` is the verdict for output with no lines at all, and the caller MUST
    choose it, because the two callers need opposite answers. The server pruner gates on
    the command's exit status first, so for it an empty listing means nothing is ahead
    of upstream — merged. The dotfiles hook has no such gate and reads emptiness as no
    evidence, because a freshly created worktree with no commits of its own also lists
    nothing, and calling that "landed" would report every new worktree.
    """
    lines = [line for line in cherry_output.splitlines() if line.strip()]
    if not lines:
        return empty_means
    return all(line.startswith("-") for line in lines)


def merge_tree_says_contained(merge_tree_stdout: str, target_tree: str) -> bool:
    """Read `git merge-tree --write-tree <target> <head>`: True on a no-op merge.

    The command prints the OID of the tree merging the branch would produce. When that
    equals the target's own tree, the branch has nothing the target does not already
    hold — which is what a squash merge leaves behind, and what neither ancestry nor
    patch-id can see, because a squash keeps the content while discarding the commits
    that carried it.

    Empty input is a failure to read a verdict, not a match, so it returns False — both
    arguments must be present for a comparison to mean anything.
    """
    lines = [line.strip() for line in merge_tree_stdout.splitlines() if line.strip()]
    target = target_tree.strip()
    if not lines or not target:
        return False
    return lines[0] == target


def _git_stdout(args: list[str], cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def default_ref(repo: str) -> str | None:
    """The remote's default branch ref, or None when there is no merge target at all.

    `origin/HEAD` is what the remote itself says, so it survives a repo whose default is
    neither main nor master. It is only a local symref and can be missing on a clone
    made with --single-branch, hence the two guesses behind it. Returning None keeps
    every tree: without a merge target, nothing can be shown to have landed.
    """
    head = _git_stdout(
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo
    )
    if head:
        return head
    for guess in ("origin/main", "origin/master"):
        if _git_stdout(["rev-parse", "--verify", "--quiet", guess], cwd=repo):
            return guess
    return None
