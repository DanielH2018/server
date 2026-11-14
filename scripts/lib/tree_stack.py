"""Stack pull-request heads on a base, one merge each, as a landing batch lands them.

A landing batch tests several PRs in one CI run: each head merged in turn onto master
(`docs/ci-throughput.md`, slice B3). The landing builds that stack to push it, and the run's
gate builds it again to record what it tested, so both call `stack`. The tree after each merge
is the tree GitHub's squash merge of that head makes when the heads before it have merged:
for #8474, `git merge-tree` and the squash both gave `dfa8d4331c36`.

Run: uv run pytest scripts/lib/tests/test_tree_stack.py
"""

import sys
from collections.abc import Sequence
from pathlib import Path

# The `lib` convention for a module that imports its siblings as `lib.<name>`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.git import git

# `git commit-tree` needs an identity, and `lib.git` strips the one the environment carries.
_IDENTITY = ("-c", "user.name=landing-batch", "-c", "user.email=landing-batch@invalid")


class Conflict(Exception):
    """A head that does not merge cleanly onto the ones stacked before it.

    Attributes:
        index: The head's place in the `heads` that `stack` was given.
    """

    def __init__(self, index: int) -> None:
        super().__init__(f"head {index} does not merge onto the ones before it")
        self.index = index


def stack(
    cwd: str | Path,
    base: str,
    heads: Sequence[str],
    subjects: Sequence[str] | None = None,
) -> list[tuple[str, str]]:
    """Merge each of `heads` in turn onto `base`, in the repository at `cwd`.

    Every commit named must already be in that repository. Nothing is checked out and no ref
    moves: each merge is `git merge-tree`, and each result is a commit object with one parent,
    as a squash merge makes. The merged trees stay in the object store, which
    `lib.git.merge_tree` avoids, because each stacked commit names one.

    Args:
        subjects: The message of each stacked commit, one per head. Without it each commit
            names the head it merged.

    Returns:
        The stacked commit and its tree for each head, in order.

    Raises:
        Conflict: A head does not merge cleanly onto the ones before it.
        subprocess.CalledProcessError: git could not merge or commit at all.
    """
    stacked = []
    tip = base
    for index, head in enumerate(heads):
        merged = git("merge-tree", "--write-tree", tip, head, cwd=cwd, check=False)
        # git exits 1 for a conflict and for a commit it cannot read. Only a conflict prints
        # the merged tree.
        if merged.returncode == 1 and merged.stdout.strip():
            raise Conflict(index)
        merged.check_returncode()
        tree = merged.stdout.split()[0]
        subject = subjects[index] if subjects else f"Stack {head}"
        tip = git(
            *_IDENTITY, "commit-tree", tree, "-p", tip, "-m", subject, cwd=cwd
        ).stdout.strip()
        stacked.append((tip, tree))
    return stacked
