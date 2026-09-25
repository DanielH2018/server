"""Find the paths under a directory that another uid owns, and say how to clear them.

An Ansible play escalated with `become` once wrote root-owned `__pycache__` into worktree
venvs. #2456 stopped new ones. The trees that already hold them fail `git worktree remove`
with an error that names no path, and a session cannot run sudo to clear them.
`prune_worktrees.py` prints this module's advice beside such a failure (#2572).
"""

import os
import shlex
from collections.abc import Callable


def _owner_uid(path: str) -> int | None:
    """The uid owning `path` itself (a symlink, not its target), or None once it is gone."""
    try:
        return os.lstat(path).st_uid
    except OSError:
        return None


def foreign_owned_paths(
    root: str, owner: Callable[[str], int | None] = _owner_uid
) -> list[str]:
    """The topmost paths under `root` that a uid other than ours owns.

    Neither `git worktree remove` nor a plain `rm` can delete such a path, and the error they
    print names no path. A foreign-owned directory is reported without its contents: deleting
    it deletes them, and it is often unreadable anyway.
    """
    uid = os.getuid()

    def foreign(path: str) -> bool:
        # A path that vanished mid-walk, or a root that never existed, blocks nothing.
        return owner(path) not in (uid, None)

    if foreign(root):
        return [root]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames):
            path = os.path.join(dirpath, name)
            if foreign(path):
                found.append(path)
                dirnames.remove(name)
        found.extend(
            path
            for path in (os.path.join(dirpath, name) for name in sorted(filenames))
            if foreign(path)
        )
    return sorted(found)


def foreign_owned_advice(
    root: str, owner: Callable[[str], int | None] = _owner_uid
) -> list[str]:
    """Lines naming what under `root` blocks its removal, and the command that clears it.

    Empty when every path is ours, so a removal git refused for another reason (a dirty tree)
    reports only git's own error. The command deletes the named paths and nothing else: git
    has already deleted the rest of the tree by the time it fails on these, but when it refused
    earlier the tree still holds work that a `rm -rf` of the whole tree would lose. A session
    cannot run sudo here, so this names the fix for the operator rather than attempting it.
    """
    paths = foreign_owned_paths(root, owner)
    if not paths:
        return []
    return [
        f"  {len(paths)} path(s) owned by another user, which uid {os.getuid()} cannot delete:",
        *(f"    {path}" for path in paths),
        "  → an operator clears them with: sudo rm -rf -- "
        + " ".join(shlex.quote(path) for path in paths),
    ]
