"""The git state outside a red batch's worktree that the red author could write (#3864, #3879).

`reset_worktree` returns the worktree to a commit and runs every git call under `_hardened`, so
nothing planted in git's own state runs during the reset. That state still outlives it. Every
worktree shares one common git dir, so a hook, a filter driver, an `include` or a
`core.fsmonitor` planted there runs in the implementer's `git commit`, in the landing, and in
every other worktree on the host. A rewritten `.git` pointer file aims the worktree at another
git dir. `~/.gitconfig` reaches every git call no `_hardened` prefix guards.

DECIDED: refuse on change, never restore. Other sessions write `branch.*` keys into the shared
config while a batch runs, so a restore would undo their writes, which is the reason
`_hardened`'s own DECIDED note gives. The snapshot therefore leaves `branch.*` out and keeps
every other key, and the pipeline fails the batch before the implementer runs when anything
else differs.
"""

import hashlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]

# Reads git's own files, so no config the red author wrote can change what the read returns.
_READ = ("env", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1", "git")
# The global read keeps `~/.gitconfig`, which is the file it reads.
_READ_GLOBAL = ("env", "GIT_CONFIG_NOSYSTEM=1", "git")
# The one section other sessions write while a batch runs: `worktree add` and `push -u`.
_SHARED_PREFIX = "branch."


@dataclass
class GitState:
    """What the red author could have changed, each entry a name and a digest of its content.

    Attributes:
        git_dir: the worktree's own git dir, as resolved at snapshot time.
        common_dir: the git dir every worktree shares.
        entries: `name -> digest` for the pointer file, each config key, each hook file and
            `info/attributes`.
    """

    git_dir: str = ""
    common_dir: str = ""
    entries: dict[str, str] = field(default_factory=dict)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _config(
    run: Runner, label: str, *where: str, read: tuple[str, ...] = _READ
) -> dict[str, str]:
    """`label:key -> digest` for every key in one config file but the `branch.*` section."""
    listed = run([*read, "config", *where, "--null", "--list"], None).stdout
    out: dict[str, str] = {}
    for entry in listed.split("\0"):
        key, _, value = entry.partition("\n")
        if key and not key.lower().startswith(_SHARED_PREFIX):
            # A key may repeat, as `include.path` does, so each value joins the digest.
            name = f"{label}:{key}"
            out[name] = _digest((out.get(name, "") + value).encode())
    return out


def _files(label: str, root: Path) -> dict[str, str]:
    if root.is_file():
        return {label: _digest(root.read_bytes())}
    if not root.is_dir():
        return {}
    return {
        f"{label}/{p.relative_to(root)}": _digest(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def snapshot(run: Runner, worktree: Path) -> GitState:
    """Read the git state outside `worktree`'s tree."""
    resolved = run(
        [
            *_READ,
            "-C",
            str(worktree),
            "rev-parse",
            "--absolute-git-dir",
            "--git-common-dir",
        ],
        None,
    ).stdout.split("\n")
    git_dir = resolved[0].strip() if resolved else ""
    common = resolved[1].strip() if len(resolved) > 1 else ""
    common_dir = str((Path(git_dir) / common).resolve()) if common else ""
    # A linked worktree's `.git` is a pointer file. In a primary checkout it is the git dir
    # itself, whose index and objects every commit changes, and `git_dir` already names it.
    pointer = worktree / ".git"
    entries = _files(".git", pointer) if pointer.is_file() else {}
    if common_dir:
        entries |= _config(run, "config", "--file", f"{common_dir}/config")
        entries |= _files("hooks", Path(common_dir) / "hooks")
        entries |= _files("info/attributes", Path(common_dir) / "info" / "attributes")
    if git_dir:
        worktree_config = Path(git_dir) / "config.worktree"
        if worktree_config.is_file():
            entries |= _config(run, "config.worktree", "--file", str(worktree_config))
    entries |= _config(run, "global", "--global", read=_READ_GLOBAL)
    return GitState(git_dir, common_dir, entries)


def changed(before: GitState, after: GitState) -> list[str]:
    """Each entry the two snapshots disagree on, added, removed or rewritten."""
    names = sorted(set(before.entries) | set(after.entries))
    diff = [n for n in names if before.entries.get(n) != after.entries.get(n)]
    if (before.git_dir, before.common_dir) != (after.git_dir, after.common_dir):
        diff.insert(0, "the worktree's git dir")
    return diff
