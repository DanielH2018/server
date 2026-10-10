"""Which of a PR's new tests still pass with its code changes taken out, on a scratch repo.

Run: uv run pytest scripts/dev/tests/test_fanout_base_check.py
"""

from _scratch_pytest import run
from fanout_lib.base_check import unproven_tests
from lib.git_testing import commit, init_repo

CODE = "def double(x):\n    return x\n"
FIXED = "def double(x):\n    return 2 * x\n"
NEEDS_FIX = "from mod import double\n\n\ndef test_two():\n    assert double(2) == 4\n"
PASSES_ANYWAY = (
    "from mod import double\n\n\ndef test_zero():\n    assert double(0) == 0\n"
)


def _repo(tmp_path):
    repo = init_repo(tmp_path / "repo")
    base = commit(
        repo,
        "base",
        **{"mod.py": CODE, "pyproject.toml": "[tool.pytest.ini_options]\n"},
    )
    return repo, base


def test_a_new_test_that_passes_without_the_fix_is_flagged(tmp_path):
    """#4182: the reviewer found an implementer test that passed on the unchanged code."""
    repo, base = _repo(tmp_path)
    head = commit(
        repo,
        "fix",
        **{"mod.py": FIXED, "tests/test_mod.py": NEEDS_FIX + "\n\n" + PASSES_ANYWAY},
    )
    check = unproven_tests(run, repo, base, head)
    assert check.new == 2
    assert check.passing == ["tests/test_mod.py::test_zero"]


def test_a_new_test_that_needs_the_fix_is_clean(tmp_path):
    repo, base = _repo(tmp_path)
    head = commit(repo, "fix", **{"mod.py": FIXED, "tests/test_mod.py": NEEDS_FIX})
    assert unproven_tests(run, repo, base, head).passing == []


def test_a_test_of_a_module_the_fix_adds_is_clean_beside_one_that_is_flagged(tmp_path):
    """A test module that imports new code fails to collect without it, which proves nothing
    either way, and must not stop the other modules from running."""
    repo, base = _repo(tmp_path)
    head = commit(
        repo,
        "fix",
        **{
            "mod.py": FIXED,
            "newmod.py": "def triple(x):\n    return 3 * x\n",
            "tests/test_newmod.py": (
                "from newmod import triple\n\n\ndef test_three():\n"
                "    assert triple(1) == 3\n"
            ),
            "tests/test_mod.py": PASSES_ANYWAY,
        },
    )
    check = unproven_tests(run, repo, base, head)
    assert check.passing == ["tests/test_mod.py::test_zero"]


def test_the_red_nodes_are_left_out(tmp_path):
    repo, base = _repo(tmp_path)
    head = commit(repo, "fix", **{"mod.py": FIXED, "tests/test_mod.py": PASSES_ANYWAY})
    check = unproven_tests(
        run, repo, base, head, exclude=["tests/test_mod.py::test_zero"]
    )
    assert (check.new, check.passing) == (0, [])


def test_a_change_with_no_new_test_reads_nothing(tmp_path):
    repo, base = _repo(tmp_path)
    head = commit(repo, "fix", **{"mod.py": FIXED})
    calls = []

    def counting(argv, stdin):
        calls.append(argv)
        return run(argv, stdin)

    assert unproven_tests(counting, repo, base, head).new == 0
    assert not any("pytest" in argv for argv in calls)
