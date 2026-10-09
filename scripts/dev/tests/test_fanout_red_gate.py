"""The red and green gates against real git and real pytest, on a scratch repo.

The runner hands `git` to git under a scrubbed environment and turns the gate's
`uv run --directory <tree> pytest ...` into this interpreter's pytest in that tree, so every
verdict below is read off output pytest really printed.

Run: uv run pytest scripts/dev/tests/test_fanout_red_gate.py
"""

import subprocess
import sys

from fanout_lib.brief import Issue
from fanout_lib.red_gate import (
    RED_GREEN_LABEL,
    green_gate,
    red_gate,
    review_flags,
)
from lib.git_testing import commit, git_out, init_repo, scrubbed_env

CODE = "def double(x):\n    return x\n"
FIXED = "def double(x):\n    return 2 * x\n"
OLD_TEST = "from mod import double\n\n\ndef test_zero():\n    assert double(0) == 0\n"
NEW_TEST = (
    "\n\ndef test_two():\n    from mod import double\n\n    assert double(2) == 4\n"
)


def run(argv, stdin):
    if argv[0] == "uv":
        tree = argv[3]
        argv = [sys.executable, "-m", "pytest", *argv[5:]]
        return subprocess.run(
            argv, cwd=tree, capture_output=True, text=True, check=False
        )
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True, env=scrubbed_env()
    )


def _repo(tmp_path, **red_files):
    """A repo whose base holds `mod.py` and one passing test, then the red commit."""
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, "base", **{"mod.py": CODE, "tests/test_mod.py": OLD_TEST})
    red = commit(repo, "red", **red_files)
    return repo, base, red


def test_new_tests_that_fail_on_the_base_pass_the_gate_and_old_nodes_are_left_out(
    tmp_path,
):
    repo, base, red = _repo(
        tmp_path,
        **{
            "tests/test_mod.py": OLD_TEST + NEW_TEST,
            "tests/test_new.py": NEW_TEST.replace("test_two", "test_three"),
        },
    )
    gate = red_gate(run, repo, base, red)
    assert gate.passed, gate.reason
    assert gate.nodes == [
        "tests/test_mod.py::test_two",
        "tests/test_new.py::test_three",
    ]
    assert (repo / "tests/test_mod.py").read_text() == OLD_TEST + NEW_TEST


def test_a_module_level_import_of_a_missing_module_is_refused(tmp_path):
    missing = (
        "from not_written_yet import thing\n\n\ndef test_it():\n    assert thing()\n"
    )
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": missing})
    gate = red_gate(run, repo, base, red)
    assert gate.reason.startswith("pytest exited 2: a collection")


def test_a_new_test_that_already_passes_is_refused(tmp_path):
    passes = "def test_vacuous():\n    assert True\n"
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": passes})
    gate = red_gate(run, repo, base, red)
    assert "tests/test_new.py::test_vacuous (PASSED)" in gate.reason


def test_a_skipped_new_test_is_refused_beside_a_failing_one(tmp_path):
    skipped = (
        NEW_TEST
        + "\n\nimport pytest\n\n\n@pytest.mark.skip\ndef test_later():\n    pass\n"
    )
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": skipped})
    gate = red_gate(run, repo, base, red)
    assert "tests/test_new.py::test_later (not run)" in gate.reason


def test_a_red_commit_that_touches_a_conftest_is_refused(tmp_path):
    repo, base, red = _repo(
        tmp_path,
        **{"tests/test_new.py": NEW_TEST, "tests/conftest.py": "x = 1\n"},
    )
    gate = red_gate(run, repo, base, red)
    assert gate.reason.endswith("tests/conftest.py")


def test_the_green_gate_passes_a_fix_and_refuses_an_edited_red_test(tmp_path):
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": NEW_TEST})
    gate = red_gate(run, repo, base, red)
    commit(repo, "fix", **{"mod.py": FIXED})
    assert green_gate(run, repo, red, gate) == ""
    commit(repo, "weaken", **{"tests/test_new.py": "def test_two():\n    pass\n"})
    assert "tests/test_new.py" in green_gate(run, repo, red, gate)
    assert git_out(repo, "rev-parse", "HEAD") != red


def test_only_a_review_batch_whose_every_issue_carries_the_label_gets_the_red_phase(
    capsys,
):
    red = Issue(1, "t", "b", labels=("claude", RED_GREEN_LABEL))
    plain = Issue(2, "t", "b", labels=("claude",))
    assert review_flags("1", [red], review=True, server=True) == (True, True)
    assert review_flags("1-2", [red, plain], review=True, server=True) == (True, False)
    assert review_flags("1", [red], review=False, server=True) == (False, False)
    assert "runs without a red phase" in capsys.readouterr().err
