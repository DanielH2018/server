"""The one reader of a role-tree census, shared by every guard that walks one.

`role_dirs` answers "which roles", `task_files` answers "which task files". Both are here so a
guard that asks either question sees the same set as every other guard asking it (#3717,
#3726).

Covers all three trees: `roles/k8s/`, `roles/setup/` and the Pi's `roles/containers/`. A
retired role ghosts the same way in each, so they read through one predicate.

Lives in its own module rather than in `_helpers.py`, which is at its 500-line cap.
The `role-tree-walks-use-role-dirs` and `role-task-walks-use-task-files` rows of
`ansible/tests/repo/test_census_rows_suite.py` are the guards that keep the next walk of either
kind from being written inline.
"""

from pathlib import Path

from lib.k8s_roles import role_dirs as _role_dirs

from _helpers import K8S_ROLES, ROLES


def role_dirs(roles_dir: Path = K8S_ROLES, *, skip_dotted: bool = False) -> list[Path]:
    """Every real role directory under `roles_dir`, sorted, a retired role's debris skipped.

    Retiring a role is what makes this load-bearing. The deployer's fast-forward removes the
    role's TRACKED files, but a gitignored `__pycache__/` left by any pytest run in the primary
    checkout keeps `roles/<plane>/<role>/` on disk. A guard that walked the tree with a
    bare `iterdir()` then read that shell as a role: the ones that go on to read `defaults/` or
    `tasks/` raise `FileNotFoundError`, and the rest credit or census a role that no longer
    exists in git. CI reads a fresh checkout, so neither shows up there.

    The three trees fail differently and all fail. A k8s guard miscensuses; the Pi's
    `containers_list` guard and the setup-playbook routing guard assert set equality against a
    declaration, so the shell reads as an undeclared role and they fail outright.

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


def every_plane_role_dirs(roles_root: Path = ROLES) -> list[Path]:
    """Every role in every plane under `roles_root` (`containers/`, `k8s/`, `setup/`), sorted.

    `roles_root` is a parameter so a guard's red proof can build a synthetic plane tree in
    `tmp_path`.
    """
    return sorted(
        role
        for plane in sorted(roles_root.iterdir())
        if plane.is_dir()
        for role in role_dirs(plane)
    )


def role_task_files(role: Path) -> list[Path]:
    """Every `*.yml` under one role's `tasks/`, subdirectories included, sorted.

    Recursive because `include_tasks: sub/x.yml` is legal, and a guard that read only the top
    level would pass a nested offender. `test_host_lib_sibling_copies.py`'s red proof builds
    exactly that shape.
    """
    tasks = role / "tasks"
    return sorted(tasks.rglob("*.yml")) if tasks.is_dir() else []


def task_files(plane: Path | None = None) -> list[Path]:
    """Every role task file in one plane, or in every plane when `plane` is None.

    # DECIDED: no `archive` exclusion. Two of the copies this replaced skipped any path with an
    # `archive` part, written for `roles/containers/archive/`. That tree was deleted in
    # 8d825a872, and a role directory is a role whatever it is named, so the filter matched
    # nothing and only let two guards disagree with the rest about the same question.
    """
    return [path for _role, path in task_files_by_role(plane)]


def task_files_by_role(plane: Path | None = None) -> list[tuple[Path, Path]]:
    """`(role directory, task file)` for every file `task_files(plane)` returns, in its order.

    For a guard that names the role a task file belongs to. `path.parent.parent` is only the
    role for a top-level file; a nested `tasks/sub/x.yml` would name `tasks` instead.
    """
    roles = every_plane_role_dirs() if plane is None else role_dirs(plane)
    return [(role, path) for role in roles for path in role_task_files(role)]
