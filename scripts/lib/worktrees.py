"""What a Claude session worktree is, and whether it is done with: the worktree library.

`scripts/dev/prune_worktrees.py` is the CLI that reports and removes worktrees. Everything
that READS a worktree, or decides about one, lives here, so the other consumers import it
without importing a CLI: `findings_lib` judges a claim stale by `classify`, `fanout_lib`
removes a finished batch with `remove`, and the SessionStart banner names the other live
sessions with `session_is_alive`.

Two layers. The readers -- the `Worktree` record, the porcelain parser, the lock-liveness
check, the cherry and merge-tree verdicts, the forge lookup, `default_ref` -- are shared with
the dotfiles `prune-worktrees.py` hook through the deployed `claude_worktree` package, and are
re-exported here. `lib._claude_worktree` is the bootstrap onto it and says why a missing
deploy raises rather than falls back.

The policy on top of them is this repo's own and stays here rather than in that package:
`is_merged` runs its four layers against `lib.git`, which strips every inherited `GIT_*`
variable, and `classify` encodes which condition keeps a tree. The dotfiles hook makes its
own call on both. `remove` adds one refusal `classify` cannot make in advance: a live
process using the tree at the moment of removal (`processes_using`).
"""

import functools
import os
import sys
from collections.abc import Callable
from pathlib import Path

# The `lib` convention for a module that imports its siblings as `lib.<name>`; `render_guard`
# carries the same insert.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import _claude_worktree  # noqa: F401
from lib.git import git, git_dirty, git_stdout
from lib.repo_paths import REPO
from claude_worktree import (
    Worktree,
    cherry_says_landed,
    default_ref,
    forge_says_merged,
    merge_tree_says_contained,
    parse_worktree_list,
    session_is_alive,
)

__all__ = [
    "KEEP",
    "REMOVABLE",
    "Worktree",
    "cherry_says_landed",
    "classify",
    "default_ref",
    "forge_says_merged",
    "is_dirty",
    "is_merged",
    "locally_landed",
    "memoised_merged",
    "merge_tree_says_contained",
    "parse_worktree_list",
    "primary_checkout",
    "processes_using",
    "remove",
    "session_is_alive",
    "worktree_facts",
]

REMOVABLE = "removable"
KEEP = "keep"


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


def is_merged(
    repo: str, head: str, branch: str = "", base: str = "origin/master"
) -> bool:
    """True when `head`'s work is already on `base`, by ancestry or by patch.

    `base` is origin/master for this repo. `findings.py --repo` passes another repo's default
    branch (the dotfiles repo's is origin/main), which is the only caller that changes it.

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
    if locally_landed(repo, head, base=base):
        return True
    # Fourth and last: squash-merged AND master has since drifted into a conflict on a file the
    # branch also touched. `git merge-tree` then exits non-zero, which is the right local answer
    # ("no verdict") and the wrong final one.
    #
    # Ask the forge, which knows what it merged. This runs LAST because it is the only check
    # needing a network round-trip and credentials; every branch the local checks settle never
    # reaches it. No `gh`, no auth, or no answer all mean no verdict, which reads as not merged.
    return forge_says_merged(repo, branch, head)


def locally_landed(
    repo: str, head: str, ancestry_known: bool = False, base: str = "origin/master"
) -> bool:
    """The first three layers of is_merged — the ones that cost no network round-trip.

    Split out for the branch sweep, which asks this question of every orphan `worktree-*`
    branch and must not reach the forge to answer it. `is_merged`'s fourth layer is one
    `gh pr list` per unsettled branch; the memo on `memoised_merged` records what that class
    of traffic already cost once, and a sweep over a couple of hundred branches is the
    same bug at a larger N.

    `ancestry_known` says the caller has already settled the ancestry layer in bulk, through
    `ancestry_landed_branches`, so this skips the per-branch `merge-base` call.
    """
    if not ancestry_known:
        ancestor = git("merge-base", "--is-ancestor", head, base, cwd=repo, check=False)
        if ancestor.returncode == 0:
            return True
    cherry = git("cherry", base, head, cwd=repo, check=False)
    # A failed `git cherry` prints nothing, and empty output otherwise means "merged" — so
    # the return code has to gate this, or an unknown ref would read as safe to delete.
    if cherry.returncode != 0:
        return False
    if cherry_says_landed(cherry.stdout, empty_means=True):
        return True
    master_tree = git("rev-parse", f"{base}^{{tree}}", cwd=repo, check=False)
    if master_tree.returncode != 0:
        return False
    # Exit is non-zero on a conflict, and on a git too old for --write-tree (added in 2.38).
    # Both mean "no verdict", which must read as not merged.
    merged_tree = git("merge-tree", "--write-tree", base, head, cwd=repo, check=False)
    return merged_tree.returncode == 0 and merge_tree_says_contained(
        merged_tree.stdout, master_tree.stdout
    )


def is_dirty(path: str) -> bool:
    # Untracked counted: an unlanded scratch file in a worktree is exactly the thing that must
    # stop it being pruned. lib.git.git_dirty makes that scope explicit at the call site.
    return git_dirty(path, include_untracked=True)


def _inside(held: str, tree: Path) -> bool:
    """Whether the path `held` is `tree` or somewhere below it."""
    path = Path(held).resolve()
    return path == tree or tree in path.parents


def processes_using(path: str) -> list[tuple[int, str]]:
    """(pid, how) for every live process whose cwd, or `CLAUDE_PROJECT_DIR`, is inside `path`.

    A Claude session whose project dir is deleted loses every repo hook (#3887), and its
    worktree lock is no proof it is gone: the session can run from a tree it never locked,
    and a lock's owner pid says nothing about a shell or hook that `cd`'d in. So the removal
    asks the kernel which processes still use the directory.

    `CLAUDE_PROJECT_DIR` is read from every process, not only from `claude` ones: the
    binary's command name is its version string (`2.1.295`), so a name match finds nothing,
    and a hook or tool shell a session spawned carries the variable too.

    Only processes this uid may inspect are seen. `/proc/<pid>/cwd` and `environ` refuse
    another user's process, and those are skipped rather than counted as holders, because
    root's daemons are always unreadable and counting them would refuse every removal.
    """
    tree = Path(path).resolve()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            cwd = ""
        # A cwd whose directory was deleted reads back as "<path> (deleted)", which never
        # resolves inside an existing tree.
        if cwd and _inside(cwd, tree):
            found.append((pid, f"cwd {cwd}"))
            continue
        try:
            environ = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            # DECIDED: another uid's unreadable process is skipped, not counted (#3994). The
            # `ubuntu` prune of /home/ubuntu/server cannot miss a `claude` session: /home/ubuntu
            # is 0750 ubuntu:ubuntu and `claude` is in no `ubuntu` group, so no `claude`
            # process can hold a cwd there. Counting a process whose uid can traverse to the
            # tree fails the other way: the agent clone is 2770 claude:ubuntu, so every
            # `ubuntu` process could reach it and a `claude`-run removal would refuse every tree.
            continue
        for var in environ:
            if var.startswith(b"CLAUDE_PROJECT_DIR="):
                held = var.partition(b"=")[2].decode(errors="replace")
                if held and _inside(held, tree):
                    found.append((pid, f"CLAUDE_PROJECT_DIR={held}"))
                break
    return found


def remove(repo: str, tree: Worktree) -> tuple[bool, str]:
    """Refuse while a process uses the tree; otherwise unlock if needed, then remove.

    The process check runs first, before the unlock, so a refusal leaves the lock exactly
    as it was. It is skipped when the directory is already gone: deregistering a deleted
    tree cannot break the session still pointing at it, which #3887's hook deny covers.
    The refusal names each pid, so the operator can find the session.

    Never --force: git's own refusal on a tree with uncommitted or untracked files is the backstop
    that makes auto-unlock safe here — classify() only marks a locked tree REMOVABLE once
    session_is_alive() has confirmed the owner is dead, so this never releases a lock a live session
    still holds. Without the unlock, `git worktree remove` fails outright on a locked tree ("cannot
    remove a locked working tree") and the caller would report success while removing nothing.
    """
    if Path(tree.path).exists():
        users = processes_using(tree.path)
        if users:
            named = "; ".join(f"pid {pid} ({how})" for pid, how in users)
            return False, f"in use by a live process: {named}"
    if tree.locked:
        git("worktree", "unlock", tree.path, cwd=repo, check=False)
    result = git("worktree", "remove", tree.path, cwd=repo, check=False)
    return result.returncode == 0, result.stderr.strip()


def primary_checkout(start: str | None = None) -> str | None:
    """The checkout the worktrees hang off, or None when there is no checkout to name.

    Not the current one: worktrees live under the primary's .claude/worktrees/, and
    --show-toplevel run from inside a worktree returns the worktree itself, which made the
    orphan scan look in a directory that doesn't exist.

    The primary is the parent of the common git dir only when that dir is named `.git`. A
    separated git dir or a bare repo has no checkout beside it, so either answers None, the
    same rule as the hooks' `_hook_common.primary_checkout`.

    Args:
        start: the directory to ask from; the process cwd when None.
    """
    common_dir = git_stdout(
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        cwd=start,
        check=False,
    )
    if not common_dir or Path(common_dir).name != ".git":
        return None
    return str(Path(common_dir).parent)


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
    command. It deliberately does NOT touch `classify`: guarding on `not tree.locked` would
    re-derive `classify`'s branch structure outside `classify` and get it wrong — `classify`
    skips the merged read only on `locked AND session_is_alive`. `classify` still receives an
    eagerly-computed bool.

    ``ask`` is the read itself, defaulted to `is_merged`. A test counts its calls through
    this parameter rather than by patching the module attribute, which is the direction the
    monkeypatch ratchet in `ansible/tests/repo/` pushes callers.
    """
    cache: dict[str, bool] = {}

    def merged(tree: Worktree) -> bool:
        if tree.path not in cache:
            cache[tree.path] = ask(repo, tree.head, tree.branch or "")
        return cache[tree.path]

    return merged


def worktree_facts(
    checkout: str | None = None,
) -> tuple[list[Worktree], Callable[[str], bool], Callable[[Worktree], bool], bool]:
    """(worktrees, dirty, merged, ok): the staleness inputs, plus whether the read worked.

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
        return [], is_dirty, merged, False
    result = git("worktree", "list", "--porcelain", cwd=repo, check=False)
    if result.returncode != 0:
        return [], is_dirty, merged, False
    trees = parse_worktree_list(result.stdout)
    return trees, is_dirty, merged, True
