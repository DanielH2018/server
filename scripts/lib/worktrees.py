"""This repo's view of a Claude session worktree, over the dotfiles `claude_worktree` library.

The worktree pruner is the dotfiles repo's: `~/.claude/hooks/prune-worktrees.py`, which reads
this repo's policy from `.claude/prune-worktrees.json`. The library that judges ONE tree is the
dotfiles `claude_worktree` module, and this module re-exports it, so the consumers here import
`lib.worktrees` and never the deployed path: `findings_lib` judges a claim stale by `classify`,
`fanout_lib` removes a finished batch with `remove`, and the SessionStart banner names the other
live sessions with `lock_is_live`. `lib._claude_worktree` is the bootstrap onto the deployed
copy and says why a missing or stale deploy raises.

What is defined here is only what this repo adds: the fan-out lock pattern bound into
`lock_is_live`, origin/master as the base a branch lands on, and the claim-liveness readers
`findings.py` needs (`held_by_readable_process`, `worktree_facts`).
"""

# pyright: reportMissingImports=false

import functools
from collections.abc import Callable, Sequence
from typing import NamedTuple
from pathlib import Path


from lib import _claude_worktree  # noqa: F401
from lib import fanout_lock
from lib.git import git, git_dirty
from lib.repo_paths import REPO
import claude_worktree
from claude_worktree import (
    FOREIGN_UID,
    IN_USE,
    KEEP,
    REMOVABLE,
    UNIT_LOCK_GRACE_S,
    WORKTREE_HOLDER_ROOTS,
    WORKTREE_HOLDERS_SOCKET,
    Worktree,
    default_ref,
    escape_holder,
    holder_root,
    holds_tree,
    parse_worktree_list,
    primary_checkout,
    privileged_holders,
    process_holds,
    processes_using,
    read_answer,
    remove,
    session_is_alive,
    tree_roots,
    unit_state,
)

__all__ = [
    "IN_USE",
    "KEEP",
    "REMOVABLE",
    "UNIT_LOCK_GRACE_S",
    "WORKTREE_HOLDERS_SOCKET",
    "WORKTREE_HOLDER_ROOTS",
    "Worktree",
    "classify",
    "coordinator_project",
    "default_ref",
    "escape_holder",
    "has_live_session",
    "held_by_readable_process",
    "holder_root",
    "is_dirty",
    "is_merged",
    "lock_is_live",
    "memoised_merged",
    "parse_worktree_list",
    "primary_checkout",
    "privileged_holders",
    "processes_using",
    "read_answer",
    "remove",
    "session_is_alive",
    "WorktreeFacts",
    "worktree_facts",
]


def lock_is_live(
    tree: Worktree,
    state: Callable[[str], str | None] = unit_state,
    now: float | None = None,
    foreign: Callable[[str], str | None] | None = None,
) -> bool:
    """Whether `tree`'s lock owner may still be using it: a Claude session or a fan-out unit.

    `claude_worktree.lock_is_live` with this repo's fan-out lock pattern. `state` and `now`
    are its test seams, and `foreign` judges a fan-out tree another user owns.
    """
    return claude_worktree.lock_is_live(tree, fanout_lock.REASON, state, now, foreign)


# Names no pid, so `session_is_alive` reads it as live while the lock stands, keeping the weekly
# sweep off the tree.
COORDINATOR_LOCK = "coordinator: project "


def coordinator_project(tree: Worktree) -> str:
    """The title of the coordinator project `tree` is locked for, or "" when it holds none."""
    if not tree.locked or not tree.lock_reason.startswith(COORDINATOR_LOCK):
        return ""
    return tree.lock_reason.removeprefix(COORDINATOR_LOCK).strip()


def has_live_session(
    tree: Worktree,
    foreign: Callable[[str], str | None] | None = None,
    in_use: Callable[[str], list[tuple[int, str]]] | None = None,
) -> bool:
    """Whether a session works in the locked `tree` at this moment.

    `lock_is_live` answers for a lock that names a session's pid or a fan-out batch's unit.
    Any other lock, such as a coordinator project's or the docs-refresh cron's, outlives
    whatever took it, so the tree is live only while a process has its cwd or its
    `CLAUDE_PROJECT_DIR` inside it. A desktop session keeps its process between turns, so
    an idle sub-coordinator counts. So does any other process left in the tree, and a
    scan the root helper could not finish: a doubt reads as live.

    Args:
        tree: a locked worktree.
        foreign: `lock_is_live`'s reader for a fan-out tree another user owns.
        in_use: the process read, `processes_using` when None.
    """
    names_session = claude_worktree.LOCK_OWNER.search(tree.lock_reason)
    names_batch = fanout_lock.REASON.fullmatch(tree.lock_reason)
    if names_session or names_batch:
        return lock_is_live(tree, foreign=foreign)
    return bool((in_use or processes_using)(tree.path))


def classify(
    tree: Worktree,
    merged: bool,
    dirty: bool,
    lock_live: Callable[[Worktree], bool] = lock_is_live,
) -> tuple[str, str]:
    """Return (verdict, reason) for one worktree, by `claude_worktree.classify`.

    `merged` is `is_merged`'s answer, which counts every layer of the landed ladder, so a
    merged tree is REMOVABLE here whichever layer found it. `lock_live` is a test seam.
    """
    return claude_worktree.classify(
        tree, "ancestry" if merged else None, dirty, lock_live=lock_live
    )


def is_dirty(path: str) -> bool:
    """Whether the tree at `path` has uncommitted changes, untracked files counted.

    `lib.git.git_dirty`, which raises when git cannot read the tree. `fanout_lib.clean`
    reports that error by name and treats a directory that is gone as its own case, so this
    is not the pruner's `claude_worktree.is_dirty`, which answers True for both.
    """
    return git_dirty(path, include_untracked=True)


def is_merged(
    repo: str, head: str, branch: str = "", base: str = "origin/master"
) -> bool:
    """True when `head`'s work is already on `base`, by any layer of the landed ladder.

    `claude_worktree.merged_layer` owns the ladder: ancestry, then patch-id, then content,
    then the forge. Every failure there is closed, so an unknown reads as NOT merged.

    `base` is origin/master for this repo. `findings.py --repo` passes another repo's default
    branch, which is the only caller that changes it.
    """
    return claude_worktree.merged_layer(repo, head, branch, base) is not None


@functools.cache
def _readable_holds() -> tuple[tuple[int, str, str], ...]:
    return tuple(process_holds())


def held_by_readable_process(
    path: str,
    holds: Callable[[], Sequence[tuple[int, str, str]]] = _readable_holds,
) -> bool:
    """Whether a process this uid can read has its cwd or `CLAUDE_PROJECT_DIR` inside `path`.

    The claim-liveness counterpart of `processes_using`, which fails closed because it gates
    a deletion. A claim held on no evidence is never reaped, so an unreadable process (a
    FOREIGN_UID hold, whose path is unknown) counts for nothing here. The /proc scan runs
    once per process, shared by every tree asked about; `holds` is its test seam.

    The main worktree, the one whose `.git` is a directory, is never held: every linked
    session tree sits under it, sessions also run in it, and its branch is `master`, which
    no claim may name.
    """
    if Path(path, ".git").is_dir():
        return False
    roots = tree_roots(path)
    return any(
        how != FOREIGN_UID and held and holds_tree(held, roots)
        for _pid, held, how in holds()
    )


def memoised_merged(
    repo: str, ask: Callable[[str, str, str], bool] = is_merged
) -> Callable[[Worktree], bool]:
    """`is_merged` for a worktree, answered once per tree and cached for the rest of the call.

    A fan-out claims every issue under ONE orchestrator worktree, so `claim_states` asks the
    identical question once per claimed issue — and for an unmerged branch `is_merged` is four
    layers deep, ending in a `gh pr list` NETWORK call. N claimed issues meant N of those on
    every `claims`, `reap` and `next`; the memory entry
    `agent-polling-starves-the-deployers-ci-gate` records what that class of traffic costs.

    The memo is keyed on the worktree PATH, whose answer is stable for the duration of one
    command. It spares even the cache read; `claude_worktree`'s on-disk verdict cache is what
    carries a verdict from one command to the next. It deliberately does NOT touch `classify`:
    guarding on `not tree.locked` would re-derive `classify`'s branch structure outside
    `classify` and get it wrong — `classify` skips the merged read only on `locked AND
    lock_is_live`. `classify` still receives an eagerly-computed bool.

    ``ask`` is the read itself, defaulted to `is_merged`. A test counts its calls through
    this parameter rather than by patching the module attribute, which is the direction the
    monkeypatch ratchet in `tests/repo/` pushes callers.
    """
    cache: dict[str, bool] = {}

    def merged(tree: Worktree) -> bool:
        if tree.path not in cache:
            cache[tree.path] = ask(repo, tree.head, tree.branch or "")
        return cache[tree.path]

    return merged


class WorktreeFacts(NamedTuple):
    """The staleness inputs `worktree_facts` reads, plus whether the read worked."""

    trees: list[Worktree]
    dirty: Callable[[str], bool]
    merged: Callable[[Worktree], bool]
    ok: bool


def worktree_facts(checkout: str | None = None) -> WorktreeFacts:
    """Read the worktrees and the verdict functions that judge them.

    `ok` is False only when the git call itself failed, never merely because it found no
    worktrees — `findings.py`'s `reap` needs that distinction to avoid releasing every claim
    in the register on a transient git error: `git worktree list --porcelain` run with
    `check=False` returns an empty string on failure, which `parse_worktree_list` reads as
    zero worktrees, which makes every claim read as "no worktree — stale".

    Separated so a caller replaces one attribute rather than patching three modules, and so
    `findings.py`'s `claims` and `reap` handlers share exactly one definition of the facts.

    `checkout` reads another repo's worktrees instead of this one's, merged against that
    repo's own default branch: `findings.py --repo DanielH2018/dotfiles` judges a dotfiles
    claim against the chezmoi source tree, whose default is origin/main. A checkout with no
    default branch to read is a failed read, because without a merge target every tree would
    read as unmerged and nothing could be judged stale.
    """
    if checkout is None:
        repo, base = primary_checkout() or str(REPO), "origin/master"
    else:
        repo, base = checkout, default_ref(checkout)
    merged = memoised_merged(repo, functools.partial(is_merged, base=base or ""))
    if base is None:
        return WorktreeFacts([], is_dirty, merged, False)
    result = git("worktree", "list", "--porcelain", cwd=repo, check=False)
    if result.returncode != 0:
        return WorktreeFacts([], is_dirty, merged, False)
    trees = parse_worktree_list(result.stdout)
    return WorktreeFacts(trees, is_dirty, merged, True)
