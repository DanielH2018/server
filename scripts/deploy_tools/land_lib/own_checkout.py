"""Which of the landing's own modules lag master, read from the checkout land.py runs in.

land.sh runs land.py from the checkout it lives in, so the helpers it imports are the same
release as land.py (`tools.py` has why). That checkout can be a worktree branched hours
before the merge. Its readers then read the merge commit's layout with older code: on
2026-11-09 a reader from before `cluster/services.yml` read the #7767 merge commit as
declaring no k8s tags at all (#7820). A layout move that leaves the old reader a non-empty
but wrong answer would pass silently, so `classify.refuse_stale_code` refuses the landing
before it reads anything at the merge.

The code compared is every module this process has loaded from the checkout, plus every
module those import, read from their source: a later phase's function-level import
(`lib/k8s_roles.py` importing `lib.ansible_inventory`) has not run when the refusal does.
`classify.refuse_stale_code_before_merge` runs the same comparison against origin/master
before `--arm-merge` merges anything (#7830).
"""

import ast
import functools
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


@functools.cache
def _module_file(base: Path, dotted: str) -> Path | None:
    target = base.joinpath(*dotted.split("."))
    for candidate in (target.with_suffix(".py"), target / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imported_names(tree: ast.AST) -> list[tuple[int, str]]:
    """(level, dotted name) for every import in `tree`, function bodies included."""
    names: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [(0, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.append((node.level, module))
            # `from pkg import mod` may name a submodule rather than an attribute.
            names += [
                (node.level, f"{module}.{alias.name}" if module else alias.name)
                for alias in node.names
            ]
    return names


def imported_paths(
    root: Path, search: Iterable[Path], seeds: Iterable[str]
) -> list[str]:
    """`seeds` and every module under `root` they import, transitively, as sorted rel paths.

    A name resolves against each `search` directory and the importing file's own directory,
    the way a script's `sys.path` does. A file that does not parse imports nothing, and nor
    does one under a dot-directory: the virtualenv sits in the checkout but is not its code.
    """
    search = list(dict.fromkeys(search))
    found: set[str] = set()
    pending = list(seeds)
    while pending:
        rel = pending.pop()
        if rel in found:
            continue
        found.add(rel)
        file = root / rel
        if file.suffix != ".py" or rel.startswith("."):
            continue
        try:
            tree = ast.parse(file.read_text(), str(file))
        except OSError, SyntaxError, ValueError:
            continue
        for level, dotted in _imported_names(tree):
            if level:
                bases = [file.parents[level - 1]]
            else:
                bases = [*search, file.parent]
            for base in bases:
                hit = _module_file(base, dotted) if dotted else None
                if hit is None:
                    continue
                try:
                    pending.append(hit.resolve().relative_to(root).as_posix())
                except ValueError:
                    pass
                break
    return sorted(found)


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


def landing_code() -> list[str]:
    """Every module this process loaded from `OWN_CHECKOUT`, and every one they import."""
    loaded = loaded_paths(OWN_CHECKOUT, list(_sys.modules.values()))
    search = []
    for entry in _sys.path:
        path = Path(entry or ".").resolve()
        inside = path.is_relative_to(OWN_CHECKOUT)
        if (
            inside
            and path.is_dir()
            and not path.relative_to(OWN_CHECKOUT).as_posix().startswith(".")
        ):
            search.append(path)
    return imported_paths(OWN_CHECKOUT, search, loaded)


def stale_landing_code(base: str) -> list[str] | None:
    """`stale_paths` for `landing_code()`."""
    return stale_paths(git, OWN_CHECKOUT, base, landing_code())
