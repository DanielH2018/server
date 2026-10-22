"""The staleness tests never read the host's deployer state directory (#5784).

`staleness.main` reads `behind_since` from `--state-dir`, which defaults to the live
`/var/lib/gitops-deploy`. A test that omits the flag passes in CI, which has no such
directory, and fails on daniel-box while the deployer is parked. The docs-refresh cron runs
the suite there, so one park paged twice.

This is a census over `test_deploy_staleness.py` rather than a row of
`scripts/tests/test_census_rows_test_modules.py`: it lives apart from the file it reads, so a
fix to that file never has to edit the check.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_staleness_isolation.py
"""

import ast
import subprocess

import pytest

from lib.git_testing import commit, init_repo
from lib.repo_paths import REPO

STALENESS_TESTS = "scripts/deploy_tools/tests/test_deploy_staleness.py"

# 15 `main([...])` calls when this census was written (2026-10-22).
CALL_FLOOR = 12


def _main_calls() -> list[tuple[str, ast.List]]:
    """Every `main([...])` call's argv list in the staleness tests, with its test function."""
    tracked = subprocess.run(
        ["git", "ls-files", "--", STALENESS_TESTS],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split()
    assert tracked == [STALENESS_TESTS], f"{STALENESS_TESTS} is not tracked"
    tree = ast.parse((REPO / STALENESS_TESTS).read_text())
    calls = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "main"
                and node.args
                and isinstance(node.args[0], ast.List)
            ):
                calls.append((fn.name, node.args[0]))
    return calls


def test_every_staleness_main_call_names_its_own_state_dir():
    calls = _main_calls()
    assert len(calls) >= CALL_FLOOR, (
        f"found {len(calls)} main([...]) calls, under the floor of {CALL_FLOOR}: "
        "the census no longer finds its subject"
    )
    assert "test_a_sha_that_is_itself_behind_still_refuses" in {
        name for name, _ in calls
    }, "the #5784 test is no longer among the calls this census finds"
    offenders = sorted(
        f"{name} (line {argv.lineno})"
        for name, argv in calls
        if not any(
            isinstance(arg, ast.Constant) and arg.value == "--state-dir"
            for arg in argv.elts
        )
    )
    assert not offenders, (
        "these main([...]) calls read the host's /var/lib/gitops-deploy, so they fail "
        "while the deployer is parked; pass --state-dir with a tmp_path:\n  "
        + "\n  ".join(offenders)
    )


def _synthetic_tree(tmp_path, monkeypatch, *, omit_in: str | None) -> None:
    """Point the census at a repo whose staleness tests are CALL_FLOOR functions.

    Each function makes one `main([...])` call naming `--state-dir`, except `omit_in`'s.
    """
    names = ["test_a_sha_that_is_itself_behind_still_refuses"] + [
        f"test_case_{i}" for i in range(CALL_FLOOR - 1)
    ]
    source = "".join(
        f"def {name}(clone, state):\n"
        + (
            '    main(["--repo", clone, "--no-fetch"])\n'
            if name == omit_in
            else '    main(["--repo", clone, "--no-fetch", "--state-dir", state])\n'
        )
        for name in names
    )
    repo = init_repo(tmp_path / "repo")
    commit(repo, "staleness tests", **{STALENESS_TESTS: source})
    monkeypatch.setitem(globals(), "REPO", repo)


def test_the_census_flags_a_call_that_omits_the_state_dir(tmp_path, monkeypatch):
    """The rejecting half: one call without `--state-dir` is named, and only that one."""
    _synthetic_tree(tmp_path, monkeypatch, omit_in="test_case_3")
    with pytest.raises(AssertionError) as caught:
        test_every_staleness_main_call_names_its_own_state_dir()
    assert "test_case_3 (line" in str(caught.value)
    assert "test_case_4" not in str(caught.value)


def test_the_census_passes_a_tree_where_every_call_names_one(tmp_path, monkeypatch):
    """The accepting half, over the same synthetic tree with no call left out."""
    _synthetic_tree(tmp_path, monkeypatch, omit_in=None)
    test_every_staleness_main_call_names_its_own_state_dir()
