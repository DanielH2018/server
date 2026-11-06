"""repo_guards runs every listed guard, and it proposes only a stale guard no hook pins.

Run: uv run pytest scripts/dev/tests/test_repo_guards.py
"""

import subprocess
from datetime import date

import pytest
import repo_guards
from lib.repo_paths import REPO


def test_every_guard_exists_and_the_list_covers_every_hook_file():
    """A renamed guard or a widened hook turns this red instead of the list going stale (#5077)."""
    hooks = repo_guards.hook_files()
    assert "ansible/tests/repo/test_module_length_ratchet.py" in hooks
    missing = [
        p for p in repo_guards.GUARDS if not (REPO / p.split("::", 1)[0]).exists()
    ]
    assert missing == []
    uncovered = [
        f for f in hooks if not any(f.startswith(p) for p in repo_guards.GUARDS)
    ]
    assert uncovered == []


@pytest.mark.parametrize("code", [0, 1])
def test_a_run_names_every_guard_and_returns_pytests_exit_code(code):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, code)

    assert repo_guards.run_guards(run) == code
    argv, kwargs = calls[0]
    assert argv[1:3] == ["-m", "pytest"]
    assert set(repo_guards.GUARDS) <= set(argv)
    assert kwargs["cwd"] == REPO


def test_a_drop_candidate_is_older_than_the_window_and_holds_no_hook_file():
    guards = {
        "old_test.py": "2026-09-01",
        "recent_test.py": "2026-10-20",
        "hook_tests": "2026-09-01",
    }
    hooks = ["hook_tests/test_ratchet.py"]
    today = date(2026, 11, 6)
    assert repo_guards.drop_candidates(guards, hooks, today, 8) == ["old_test.py"]
    assert repo_guards.drop_candidates(guards, hooks, today, 1) == [
        "old_test.py",
        "recent_test.py",
    ]


def test_drop_candidates_prints_the_stale_guard_and_runs_nothing(capsys):
    def run(argv, **kwargs):
        pytest.fail(f"ran {argv}")

    guards = {"old_test.py": "2020-01-01"}
    assert repo_guards.main(["--drop-candidates"], guards, run) == 0
    assert "old_test.py: last counted failure 2020-01-01" in capsys.readouterr().out
