"""Guards that ty's two hand-kept path lists still cover the repo.

`[tool.ty.src] include` decides which files are checked, and `[tool.ty.environment] extra-paths`
decides which imports resolve. Both are enumerated by hand, and both drift.

WHY `include` IS HAND-KEPT AT ALL. Omitting it does not fall back to the whole project. Measured
2026-09-02 with ty 0.0.77: with `include` absent, a deliberately broken file at the repo root was
not checked, while the same file under `scripts/` still was.

This repo has no `__init__.py` files and installs nothing: a script reaches a sibling directory
through the `sys.path` bootstrap the root CLAUDE.md describes, and a role's test reaches its
`files/` the same way. Two tools have to be told about that, and they are told separately —
pytest by `[tool.pytest.ini_options] pythonpath`, ty by `[tool.ty.environment] extra-paths`.

Two hand-kept lists over one convention, so both drift. They are guarded for different reasons,
and it is worth being clear which is which — an earlier version of this paragraph claimed the
checks run only on the quiet failure and then described a loud one, which reads as an argument
for deleting the include guard.

The `include` gap is the quiet one, and `test_every_tracked_python_file_is_inside_a_ty_source_root`
is the check for it. A file outside every source root is neither checked nor skipped; the gate
reads green and nobody learns the file exists to ty.

The `extra-paths` gaps are loud: ty reports `unresolved-import` on code that imports fine at
runtime. They are guarded anyway because loud in the wrong place is still misleading — the
reflex fix for an `unresolved-import` on first-party code is to silence the rule, not to add the
missing search path, and these tests name the path instead. Two shapes are covered: a
`pythonpath` entry with no `extra-paths` twin, and a `testpaths` entry whose sibling `files/` is
missing from `extra-paths` (a role's tests reach its module through that directory).

The reverse costs nothing and is allowed: `extra-paths` carries `.claude/hooks` with no
`pythonpath` twin, and pytest needs no such listing.

Clean/flagged pairs below, per the repo rule that a new check ships with a proof it can go RED.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import PurePosixPath

from _helpers import REPO


def _config() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text())


def _pythonpath(cfg: dict) -> list[str]:
    paths = cfg["tool"]["pytest"]["ini_options"]["pythonpath"]
    assert paths, "pyproject.toml declares no pytest pythonpath"
    return paths


def _extra_paths(cfg: dict) -> list[str]:
    paths = cfg["tool"]["ty"]["environment"]["extra-paths"]
    assert paths, "pyproject.toml declares no ty extra-paths"
    return paths


def _norm(path: str) -> str:
    """One spelling per directory.

    `./scripts`, `scripts/` and `scripts` all work for pytest, so without this the guard can fail
    naming a path that IS covered — and the reflex fix is to add a duplicate entry.
    """
    return PurePosixPath(path).as_posix().rstrip("/")


def _missing(pythonpath: list[str], extra_paths: list[str]) -> list[str]:
    """Every pytest import root ty cannot resolve from."""
    have = {_norm(p) for p in extra_paths}
    return [p for p in pythonpath if _norm(p) not in have]


def _files_siblings(testpaths: list[str], extra_paths: list[str]) -> list[str]:
    """Every `<role>/files` a role's tests import from that is not a ty search path.

    A role's test reaches its module through the sibling `files/` directory, so a new role added
    to `testpaths` needs that directory on `extra-paths` as well.
    """
    have = {_norm(p) for p in extra_paths}
    out = []
    for tp in testpaths:
        sib = _norm(f"{PurePosixPath(tp).parent}/files")
        if (REPO / sib).is_dir() and sib not in have:
            out.append(sib)
    return out


def test_every_role_files_sibling_of_a_testpath_is_a_ty_search_path():
    cfg = _config()
    missing = _files_siblings(
        cfg["tool"]["pytest"]["ini_options"]["testpaths"], _extra_paths(cfg)
    )
    assert not missing, (
        "a role's tests import from its sibling `files/`, and ty cannot resolve modules in "
        f"these: {missing}. Add them to [tool.ty.environment] extra-paths."
    )


def test_a_covered_files_sibling_is_clean():
    assert (
        _files_siblings(
            ["ansible/roles/k8s/ical-proxy/tests"],
            ["ansible/roles/k8s/ical-proxy/files"],
        )
        == []
    )


def test_an_uncovered_files_sibling_is_flagged():
    assert _files_siblings(["ansible/roles/k8s/ical-proxy/tests"], []) == [
        "ansible/roles/k8s/ical-proxy/files"
    ]


def test_a_testpath_with_no_files_sibling_is_not_flagged():
    """`ansible/tests` and `evals` have no sibling `files/`; absence is not a gap."""
    assert _files_siblings(["ansible/tests", "evals"], []) == []


def test_an_alternative_spelling_of_a_covered_path_is_not_flagged():
    assert _missing(["./scripts", "scripts/"], ["scripts"]) == []


def test_every_pythonpath_entry_is_a_ty_search_path():
    cfg = _config()
    missing = _missing(_pythonpath(cfg), _extra_paths(cfg))
    assert not missing, (
        "pytest imports from these directories but ty cannot resolve modules in them, so ty "
        "will report `unresolved-import` on first-party code that runs fine: "
        f"{missing}. Add them to [tool.ty.environment] extra-paths."
    )


def test_every_ty_search_path_exists():
    """A path that no longer exists resolves nothing and hides the next real drift."""
    gone = [p for p in _extra_paths(_config()) if not (REPO / p).is_dir()]
    assert not gone, f"ty extra-paths names directories that do not exist: {gone}"


def _unchecked(tracked: list[str], include: list[str], exclude: list[str]) -> list[str]:
    """Every tracked Python file no ty source root reaches."""
    out = []
    for f in tracked:
        if any(f == x or f.startswith(f"{x}/") for x in exclude):
            continue
        if not any(f == i or f.startswith(f"{i}/") for i in include):
            out.append(f)
    return out


def _tracked_python() -> list[str]:
    """Every Python file in THIS commit.

    `git ls-files`, not `rglob`, for the reason `_helpers.discover_docs` gives: this repo grows
    a full working tree per live session under `.claude/worktrees/<name>/`.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in listed.split("\0") if p]


def test_every_tracked_python_file_is_inside_a_ty_source_root():
    """`include` does not fall back to the whole project when omitted, so it is hand-kept.

    A file outside it is silently unchecked: ty reports no error and no skip, because from its
    side the file does not exist.
    """
    src = _config()["tool"]["ty"]["src"]
    unchecked = _unchecked(_tracked_python(), src["include"], src["exclude"])
    assert not unchecked, (
        "these tracked Python files are outside every ty source root, so the type gate does "
        f"not see them: {unchecked}. Add their directory to [tool.ty.src] include."
    )


def test_a_file_under_an_included_root_is_clean():
    assert _unchecked(["scripts/dev/x.py"], ["scripts"], ["ansible/collections"]) == []


def test_a_file_outside_every_root_is_flagged():
    assert _unchecked(["tools/x.py"], ["scripts"], ["ansible/collections"]) == [
        "tools/x.py"
    ]


def test_an_excluded_file_is_not_flagged():
    assert (
        _unchecked(["ansible/collections/x.py"], ["scripts"], ["ansible/collections"])
        == []
    )


def test_a_covered_pythonpath_is_clean():
    assert (
        _missing(
            ["scripts", "ansible/tests"], ["ansible/tests", "scripts", ".claude/hooks"]
        )
        == []
    )


def test_an_uncovered_pythonpath_entry_is_flagged():
    assert _missing(["scripts", "scripts/newdir"], ["scripts"]) == ["scripts/newdir"]
