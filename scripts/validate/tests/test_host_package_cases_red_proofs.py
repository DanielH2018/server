"""Red proofs for the static tests that claim the container cases serve an expired index.

`test_validate_host_package_cases.py` reads `host_package_cases.py` to hold that some case
serves a repository past its `Valid-Until`, since the case itself runs only in CI's
`host_packages` job. Each test here breaks that case in a copy of the tree and requires every
such test to go red on the copy (#5235, #5236).

Run: uv run pytest scripts/validate/tests/test_host_package_cases_red_proofs.py
"""

import ast
from collections.abc import Callable
import importlib
import inspect
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

from lib import proc_testing
from lib.repo_paths import REPO

CASES = "scripts/validate/host_package_cases.py"
EXPIRED = "expired_release"
PAST = "datetime.now(UTC) - timedelta(hours=1)"
FUTURE = "datetime.now(UTC) + timedelta(days=7)"


def _module() -> ModuleType:
    return importlib.import_module("test_validate_host_package_cases")


def _expired_case_tests(module: ModuleType) -> dict[str, Callable[[], object]]:
    """The zero-argument tests that read the cases source and claim the expired case."""
    found = {
        name: test
        for name, test in vars(module).items()
        if name.startswith("test_")
        and callable(test)
        and ("valid_until" in name or "expired" in name)
        and not inspect.signature(test).parameters
        and "host_package_cases.py" in inspect.getsource(test)
    }
    assert found, (
        "no test in test_validate_host_package_cases.py claims the expired case"
    )
    return found


def _tree_with(tmp_path: Path, source: str) -> Path:
    """A git work tree whose only tracked file is `source` at the cases' path."""
    path = tmp_path / CASES
    path.parent.mkdir(parents=True)
    path.write_text(source)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"}
    }
    proc_testing.run(["git", "init", "-q"], cwd=tmp_path, env=env, check=True)
    proc_testing.run(["git", "add", CASES], cwd=tmp_path, env=env, check=True)
    return tmp_path


def _survivors(monkeypatch, tmp_path: Path, source: str) -> list[str]:
    """The expired-case tests that still pass when the tree holds `source`."""
    module = _module()
    tests = _expired_case_tests(module)
    monkeypatch.setattr(
        module, "fx", SimpleNamespace(REPO=_tree_with(tmp_path, source))
    )
    survivors = []
    for name, test in tests.items():
        try:
            test()
        except AssertionError:
            continue
        survivors.append(name)
    return survivors


def test_every_expired_case_test_fails_with_the_expired_case_deleted(
    monkeypatch, tmp_path
):
    source = (REPO / CASES).read_text()
    (case,) = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == EXPIRED
    ]
    lines = source.splitlines(keepends=True)
    kept = lines[: case.lineno - 1] + lines[case.end_lineno :]
    mutant = "".join(line for line in kept if f", {EXPIRED})" not in line)
    assert EXPIRED not in mutant
    ast.parse(mutant)

    assert _survivors(monkeypatch, tmp_path, mutant) == []


def test_every_expired_case_test_fails_with_a_future_valid_until(monkeypatch, tmp_path):
    source = (REPO / CASES).read_text()
    assert source.count(PAST) == 1
    mutant = source.replace(PAST, FUTURE)

    assert _survivors(monkeypatch, tmp_path, mutant) == []
