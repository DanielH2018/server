"""Which render prebuilds a pytest leg runs before its first test, and what they cost it.

A leg builds the shared k8s render when it holds a module that imports `_k8s_render`, the
fleet render when it holds a `render_share` consumer, and the census pass when it holds a
census rows file. `pytest_shard.assign` packs these seconds beside each shard's test seconds,
and `pytest_shard.py`'s module docstring has why.

Split out of `pytest_shard.py` when the pull-request selection took that module past
its length cap.
"""

import functools
import re
import sys as _sys
from pathlib import Path, PurePosixPath

# Reach `render_share`, which decides which test modules read the fleet render.
_sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "tests"))

from lib.git import git
from lib.repo_paths import REPO

K8S_RENDER_WALL_SECONDS = 11.0
FLEET_RENDER_WALL_SECONDS = 6.0
# The census rows' pass `scripts/tests/conftest.py` runs: 16.0s of CI wall time.
CENSUS_PASS_WALL_SECONDS = 16.0
CENSUS_FILE_PREFIX = "scripts/tests/test_census_rows_test_"
# The module whose import makes a leg build the shared k8s render.
K8S_RENDER_MODULE = "_k8s_render"
# The xdist workers on each leg's runner, every one of which waits out the prebuilds.
CI_WORKERS = 4


_IMPORT_LINE = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import\b|import\s+([\w., ]+))", re.M
)


def _imported_names(text: str) -> set[str]:
    """The first and last dotted component of every module an import line in `text` names."""
    names = set()
    for source, listed in _IMPORT_LINE.findall(text):
        for module in [source] if source else listed.split(","):
            parts = module.split()[0].split(".") if module.split() else []
            names.update(parts[:1] + parts[-1:])
    return names


def _python_files(repo: Path) -> list[Path]:
    listed = git("ls-files", "-z", "*.py", cwd=repo).stdout.split("\0")
    return [repo / rel for rel in listed if rel]


@functools.cache
def k8s_render_readers(repo: Path = REPO) -> frozenset[str]:
    """The module names whose import loads `_k8s_render`, directly or through other modules.

    Modules are matched by basename, as pytest's `pythonpath` imports them, and a basename
    two files share counts when either file reads the render. An import inside a function
    counts too: overcharging a leg one render costs less than a leg the split missed.
    """
    imports: dict[str, set[str]] = {}
    for path in _python_files(repo):
        try:
            text = path.read_text()
        except OSError, UnicodeDecodeError:
            continue
        if path.stem != "conftest":
            imports.setdefault(path.stem, set()).update(_imported_names(text))
    readers = {K8S_RENDER_MODULE}
    while grown := {
        m for m, names in imports.items() if m not in readers and names & readers
    }:
        readers |= grown
    return frozenset(readers)


def _ancestor_conftests(rel: str, repo: Path) -> list[Path]:
    """The `conftest.py` files pytest loads to collect `rel`: its own directory's and each parent's."""
    parents = PurePosixPath(rel).parents
    return [
        repo / d / "conftest.py"
        for d in parents
        if (repo / d / "conftest.py").is_file()
    ]


@functools.cache
def _conftest_imports(path: Path) -> frozenset[str]:
    return frozenset(_imported_names(path.read_text()))


@functools.cache
def prebuild_kinds(rel: str, repo: Path = REPO) -> frozenset[str]:
    """The prebuilds a leg runs because it holds the test module at `rel`.

    `k8s` when the module or a `conftest.py` above it imports `_k8s_render`, directly or
    through `k8s_render_readers`, `fleet` when it is a `render_share` consumer, and `census` for a census rows file. A
    module that cannot be read triggers none.
    """
    import render_share

    try:
        text = (repo / rel).read_text()
    except OSError, UnicodeDecodeError:
        return frozenset()
    kinds = set()
    readers = k8s_render_readers(repo)
    if _imported_names(text) & readers or any(
        _conftest_imports(conftest) & readers
        for conftest in _ancestor_conftests(rel, repo)
    ):
        kinds.add("k8s")
    if render_share.CONSUMER_IMPORT in text:
        kinds.add("fleet")
    if rel.startswith(CENSUS_FILE_PREFIX):
        kinds.add("census")
    return frozenset(kinds)


def prebuild_wall(kinds: frozenset[str]) -> float:
    wall = max(
        K8S_RENDER_WALL_SECONDS if "k8s" in kinds else 0.0,
        FLEET_RENDER_WALL_SECONDS if "fleet" in kinds else 0.0,
    )
    return wall + (CENSUS_PASS_WALL_SECONDS if "census" in kinds else 0.0)


def prebuild_seconds(members: list[str], repo: Path = REPO) -> float:
    """The runner seconds a shard of `members` spends in prebuilds before its first test.

    Every worker of a leg waits until the prebuilds finish, so they cost their wall time once
    per worker. A shard pays the k8s render when it holds a module that imports `_k8s_render`. A shard
    holding a `render_share` consumer renders the fleet on another worker meanwhile, so one
    holding both waits the longer of the two. A census file adds the census pass.
    """
    kinds = frozenset().union(*(prebuild_kinds(f, repo) for f in members))
    return prebuild_wall(kinds) * CI_WORKERS
