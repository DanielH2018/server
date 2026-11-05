"""The unloadable-caller tests go red on a reader that crashes (#7303).

Each test here runs the tests of `test_probe_releases_consumers_unloadable.py` against a broken
reader. The pre-#7302 reader comes from git history, so this file needs a full clone, which CI's
pytest job has.

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_consumers_unloadable_goes_red.py
"""

import importlib.util
import types
from pathlib import Path

from diagnostics.probe_lib import releases_consumers as rc
from lib.proc_testing import run
from lib.repo_paths import REPO

READER = Path("scripts/diagnostics/probe_lib/releases_consumers.py")
COMMIT_BEFORE_7302 = "3e83da931^"
UNLOADABLE_TESTS = Path(__file__).with_name(
    "test_probe_releases_consumers_unloadable.py"
)
SIBLING = "test_a_caller_that_does_not_load_keeps_the_role_on_every_services_paths"


def _reader_before_7302():
    source = run(
        ["git", "-C", str(REPO), "show", f"{COMMIT_BEFORE_7302}:{READER}"], check=True
    ).stdout
    reader = types.ModuleType("releases_consumers_before_7302")
    # The module's sys.path bootstrap reads its own location.
    reader.__file__ = str(REPO / READER)
    exec(compile(source, str(READER), "exec"), vars(reader))
    return reader


def _tests_passing_on(reader, tmp_path):
    """Run every test in the unloadable-caller module against `reader`.

    Args:
        reader: the object the module's tests reach as `rc`.
        tmp_path: a directory to hold one scratch tree per test.

    Returns:
        The names of the tests that passed.
    """
    spec = importlib.util.spec_from_file_location("unloadable_tests", UNLOADABLE_TESTS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    vars(module)["rc"] = reader

    tests = {name: fn for name, fn in vars(module).items() if name.startswith("test_")}
    assert SIBLING in tests

    return [name for name, test in tests.items() if _passes(test, tmp_path / name)]


def _passes(test, tree):
    tree.mkdir()
    try:
        test(tree)
    except Exception:
        return False
    return True


def test_each_unloadable_caller_test_goes_red_on_the_reader_before_7302(tmp_path):
    assert _tests_passing_on(_reader_before_7302(), tmp_path) == []


def test_each_unloadable_caller_test_goes_red_on_any_crash_at_the_unloadable_tree(
    tmp_path,
):
    def crash_on_a_tree_with_no_graph(shared_roles, repo):
        answer = rc.consumers_for(shared_roles, repo=repo)
        if not answer:
            raise RuntimeError("no caller graph")
        return answer

    crashing_reader = types.SimpleNamespace(
        consumers_for=crash_on_a_tree_with_no_graph, role_paths_for=rc.role_paths_for
    )
    assert _tests_passing_on(crashing_reader, tmp_path) == []
