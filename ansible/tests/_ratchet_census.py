"""The two ratchets themselves, and the census of the tree that feeds them.

`_ratchet.py` holds the pure half — the caps, the allowlist parser, the two comparisons
and the patch counter. This module is the impure half that reads the working tree: the
line counts, the monkeypatch counts and the conftest-fixture census. It sits beside the
pure module rather than inside the test module so the `--tighten` writer
(`scripts/dev/tighten_ratchets.py`) imports the SAME census the ratchet asserts on.
Importing a `test_*.py` from a script would work under pytest and nowhere else.

The reads of the merge base with `origin/master`, and the tests for all of this, stay in
`ansible/tests/repo/test_module_length_ratchet.py`.
"""

import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

from _helpers import REPO, is_test_file
from _ratchet import (
    Ratchet,
    cap_for,
    count_module_patches,
    first_party_module_names,
    module_fixture_names,
)
from lib.proc_testing import run

HERE = REPO / "ansible" / "tests" / "repo"


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    """`git` in the repo root, never raising — the caller reads `returncode`."""
    return run(["git", *args], cwd=REPO, check=False)


LENGTHS = Ratchet(
    path=HERE / "module_length_allowlist.txt",
    unit="lines",
    remedy="Split it: docs/python-code-organization.md says where the pieces go.",
    cap_of=cap_for,
)

PATCHES = Ratchet(
    path=HERE / "monkeypatch_allowlist.txt",
    unit="monkeypatch.setattr calls on a first-party module",
    remedy=(
        "Give the module under test a seam instead — a frozen dataclass of injectable "
        "boundaries, as in scripts/deploy_tools/land_lib/tools.py with its fakes in "
        "scripts/deploy_tools/tests/_land_fakes.py."
    ),
    cap_of=lambda rel: 0,
)


def tracked_files(*pathspec: str) -> list[str]:
    """Every tracked path matching `pathspec` (all of them if none), repo-relative.

    `-z`, because without it git C-quotes a path holding a non-ASCII byte, a quote or a
    newline, and the quoted form names no file. Raises on a failed `git ls-files`: an empty
    list would pass every guard that asserts over it.
    """
    listed = run(
        ["git", "ls-files", "-z", "--", *pathspec], cwd=REPO, check=True
    ).stdout
    return [rel for rel in listed.split("\0") if rel]


def tracked_python_files() -> list[str]:
    """Every tracked first-party `.py` path, repo-relative.

    `git ls-files` rather than a walk, which from the repo root descends into
    `.claude/worktrees/<name>/` — see test_tracked_python_hazards.py for that incident.
    """
    return [
        rel
        for rel in tracked_files("*.py")
        if not rel.startswith("ansible/collections/")
    ]


def line_counts() -> dict[str, int]:
    """Repo-relative path -> line count, counting newlines the way `wc -l` does."""
    return {
        rel: (REPO / rel).read_bytes().count(b"\n") for rel in tracked_python_files()
    }


def module_fixtures_by_dir(tracked: Iterable[str]) -> dict[str, frozenset[str]]:
    """Directory (repo-relative, posix; "" is the repo root) -> its conftest's module fixtures."""
    return {
        rel.rpartition("/")[0]: module_fixture_names((REPO / rel).read_text())
        for rel in tracked
        if rel.rpartition("/")[2] == "conftest.py"
    }


def module_fixtures_for(
    rel: str, by_dir: Mapping[str, frozenset[str]]
) -> frozenset[str]:
    """The module fixtures visible to `rel`, unioned over its directory chain.

    pytest resolves a fixture from the test's own directory upwards, so a conftest anywhere
    above the test contributes. Shadowing does not matter here: both definitions would have to
    return a module for the name to count at all.
    """
    parts = rel.split("/")[:-1]
    return frozenset().union(
        *(
            by_dir.get("/".join(parts[:depth]), frozenset())
            for depth in range(len(parts) + 1)
        )
    )


def monkeypatch_counts() -> dict[str, int]:
    """Repo-relative test-module path -> its module-patch count, zeros included.

    Zeros are in the mapping so a listed file whose patches are all gone reads as "remove it"
    rather than as a path that no longer exists.
    """
    tracked = tracked_python_files()
    first_party = first_party_module_names(tracked)
    by_dir = module_fixtures_by_dir(tracked)
    return {
        rel: count_module_patches(
            (REPO / rel).read_text(errors="replace"),
            first_party,
            module_fixtures_for(rel, by_dir),
        )
        for rel in tracked
        if is_test_file(Path(rel))
    }
