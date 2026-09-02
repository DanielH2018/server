"""Guards that ty's two hand-kept path lists still cover the repo.

`[tool.ty.src] include` decides which files are checked, and `[tool.ty.environment] extra-paths`
decides which imports resolve. Both are enumerated by hand, and a gap in either is quiet: a file
outside `include` produces no error and no skip, because from ty's side it does not exist.

WHY `include` IS HAND-KEPT AT ALL. Omitting it does not fall back to the whole project. Measured
2026-09-02 with ty 0.0.77: with `include` absent, a deliberately broken file at the repo root was
not checked, while the same file under `scripts/` still was.

This repo has no `__init__.py` files and installs nothing: a script reaches a sibling directory
through the `sys.path` bootstrap the root CLAUDE.md describes, and a role's test reaches its
`files/` the same way. Two tools have to be told about that, and they are told separately —
pytest by `[tool.pytest.ini_options] pythonpath`, ty by `[tool.ty.environment] extra-paths`.

Two hand-kept lists over one convention drift. The failure is quiet in one direction and loud in
the other, which is why the check runs on the quiet one. Adding a `pythonpath` entry and
forgetting `extra-paths` leaves ty resolving a first-party import against nothing; it reports
`unresolved-import` on a module that imports fine at runtime, and the reflex fix is to silence
the rule rather than to add the path. The reverse costs nothing, so an `extra-paths` entry with
no `pythonpath` twin is allowed: `extra-paths` deliberately also carries `.claude/hooks` and the
`files/` sibling of every `testpaths` entry, neither of which pytest needs listed.

Clean/flagged pairs below, per the repo rule that a new check ships with a proof it can go RED.
"""

from __future__ import annotations

import subprocess
import tomllib

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


def _missing(pythonpath: list[str], extra_paths: list[str]) -> list[str]:
    """Every pytest import root ty cannot resolve from."""
    have = set(extra_paths)
    return [p for p in pythonpath if p not in have]


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
