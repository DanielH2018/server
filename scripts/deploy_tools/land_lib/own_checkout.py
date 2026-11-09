"""Which of the landing's own modules lag master, read from the checkout land.py runs in.

land.sh runs land.py from the checkout it lives in, so the helpers it imports are the same
release as land.py (`tools.py` has why). That checkout can be a worktree branched hours
before the merge. Its readers then read the merge commit's layout with older code: on
2026-11-09 a reader from before `cluster/services.yml` read the #7767 merge commit as
declaring no k8s tags at all (#7820). A layout move that leaves the old reader a non-empty
but wrong answer would pass silently, so `classify.refuse_stale_code` refuses the landing
before it reads anything at the merge.

The code compared is every module this process has loaded from the checkout when the
refusal runs. A module a later phase imports lazily is not in it.
"""

import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.git import git

# The checkout land.sh cds to and runs land.py from.
OWN_CHECKOUT = Path(__file__).resolve().parents[3]

Git = Callable[..., subprocess.CompletedProcess[str]]


def loaded_paths(root: Path, modules: Iterable[object]) -> list[str]:
    """The repo-relative paths of the loaded `modules` whose file lies under `root`, sorted."""
    paths = set()
    for module in modules:
        file = getattr(module, "__file__", None)
        if not file:
            continue
        try:
            paths.add(Path(file).resolve().relative_to(root).as_posix())
        except ValueError:
            continue
    return sorted(paths)


def stale_paths(git: Git, root: Path, base: str, paths: list[str]) -> list[str] | None:
    """The `paths` whose content in `root`'s HEAD lags `base`; None when git could not say.

    A path lags when a commit reachable from `base` and not from HEAD touches it, and HEAD's
    content differs from `base`'s. The content test lets a branch whose own commits carry the
    same file pass, as after a squash merge.
    """
    touched = git(
        "log", "--format=", "--name-only", base, "^HEAD", "--", *paths,
        cwd=root, check=False,
    )  # fmt: skip
    differs = git(
        "diff", "--name-only", "HEAD", base, "--", *paths, cwd=root, check=False
    )
    if touched.returncode != 0 or differs.returncode != 0:
        return None
    return sorted(set(touched.stdout.split()) & set(differs.stdout.split()))


def stale_landing_code(base: str) -> list[str] | None:
    """`stale_paths` for every module this process loaded from `OWN_CHECKOUT`."""
    loaded = loaded_paths(OWN_CHECKOUT, list(_sys.modules.values()))
    return stale_paths(git, OWN_CHECKOUT, base, loaded)
