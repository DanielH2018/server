"""The one reader of a role-tree census, shared by every guard that walks one.

Covers all three trees: `roles/k8s/`, `roles/setup/` and the Pi's `roles/containers/`. A
retired role ghosts the same way in each (#2964), so they read through one predicate.

Lives in its own module rather than in `_helpers.py`, which is at its 500-line cap.
`ansible/tests/repo/test_role_tree_walks_use_role_dirs.py` is the guard that keeps the next
walk from being written bare.
"""

from pathlib import Path

from k8s_autodeploy import is_leftover_dir

from _helpers import K8S_ROLES


def role_dirs(roles_dir: Path = K8S_ROLES) -> list[Path]:
    """Every real role directory under `roles_dir`, sorted, a retired role's debris skipped.

    Retiring a role is what makes this load-bearing. The deployer's fast-forward removes the
    role's TRACKED files, but a gitignored `__pycache__/` left by any pytest run in the primary
    checkout keeps `roles/<plane>/<role>/` on disk (#2882). A guard that walked the tree with a
    bare `iterdir()` then read that shell as a role: the ones that go on to read `defaults/` or
    `tasks/` raise `FileNotFoundError`, and the rest credit or census a role that no longer
    exists in git. CI reads a fresh checkout, so neither shows up there (#2952, #2964).

    The three trees fail differently and all fail. A k8s guard miscensuses; the Pi's
    `containers_list` guard and the setup-playbook routing guard assert set equality against a
    declaration, so the shell reads as an undeclared role and they fail outright.

    `is_leftover_dir` is the predicate the deployer's own filter uses, so every walker agrees
    on what counts as a role. It skips only a directory with NO non-`.pyc` file; an empty
    directory is not debris and is still returned, which is what keeps a synthetic role built
    by a test from vanishing out of a caller that passes its own `roles_dir`.
    """
    return sorted(
        p for p in roles_dir.iterdir() if p.is_dir() and not is_leftover_dir(str(p))
    )
