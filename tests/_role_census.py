"""The one reader of a role-tree census, shared by every guard that walks one.

`role_dirs` answers "which roles", so a guard that asks sees the same set as every other guard
asking it (#3717, #3726).

Lives in its own module rather than in `_helpers.py`, which was at its line cap.
The `role-tree-walks-use-role-dirs` row of `tests/repo/test_census_rows_suite.py` keeps
the next role walk from being written inline.
"""

from pathlib import Path

from lib.k8s_roles import role_dirs as _role_dirs

from _helpers import K8S_ROLES


def role_dirs(roles_dir: Path = K8S_ROLES, *, skip_dotted: bool = False) -> list[Path]:
    """Every real role directory under `roles_dir`, sorted, a retired role's debris skipped.

    Retiring a role is what makes this load-bearing. The deployer's fast-forward removes the
    role's TRACKED files, but a gitignored `__pycache__/` left by any pytest run in the primary
    checkout keeps `roles/<plane>/<role>/` on disk. A guard that walked the tree with a
    bare `iterdir()` then read that shell as a role: the ones that go on to read `defaults/`
    raise `FileNotFoundError`, and the rest credit or census a role that no longer
    exists in git. CI reads a fresh checkout, so neither shows up there.

    A k8s guard that censuses such a shell miscounts, and one that asserts set equality against
    a declaration reads the shell as an undeclared role and fails outright.

    `lib.k8s_roles.role_dirs` is the walk, the one `scripts/` reads too, and its predicate is
    the deployer's own. It skips only a directory with NO non-`.pyc` file; an empty
    directory is not debris and is still returned, which is what keeps a synthetic role built
    by a test from vanishing out of a caller that passes its own `roles_dir`.

    `skip_dotted` also drops a dotted directory, for a guard that reads a file every role must
    carry (`CLAUDE.md`), where a stray dotted directory would read as a role missing it. It is
    off by default because the deployer's filter does not apply it.
    """
    return [
        p for p in _role_dirs(roles_dir) if not (skip_dotted and p.name.startswith("."))
    ]
