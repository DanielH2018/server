"""The test-module census judges `hosts/lib/tests/`, which `pyproject.toml`'s `testpaths` lists (#7616).

The census rows live in `test_census_rows_test_modules.py` and `test_census_rows_test_renders.py`;
this file checks only that their census and their prek gate reach the host library's tests.

Run: uv run pytest scripts/tests/test_census_roots_cover_host_lib.py
"""

import re
import tomllib

import _test_module_rules
import pytest
from _test_module_rules import pytest_only_modules

from lib.repo_paths import REPO

HOST_LIB_MODULES = [
    "hosts/lib/tests/test_checkout_sync.py",
    "hosts/lib/tests/_bootstrap_host.py",
]


def test_the_census_roots_name_hosts_lib():
    assert "hosts/lib/" in _test_module_rules._TEST_ROOTS


@pytest.mark.parametrize("path", HOST_LIB_MODULES)
def test_the_census_reads_the_host_library_tests(path: str):
    """A test module and a fixture module under `hosts/lib/tests/` are both judged."""
    assert path in pytest_only_modules()


@pytest.mark.parametrize("path", HOST_LIB_MODULES)
def test_the_census_hook_gate_matches_the_host_library_tests(path: str):
    """A commit touching only `hosts/lib/tests/` still runs the census rows."""
    hooks = tomllib.loads((REPO / "prek.toml").read_text())["repos"]
    gate = next(
        hook["files"]
        for repo in hooks
        for hook in repo.get("hooks", [])
        if hook.get("id") == "static-ratchet-tests-test-modules"
    )
    assert re.search(gate, path)
