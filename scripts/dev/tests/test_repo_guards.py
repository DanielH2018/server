"""repo_guards runs every listed guard, and it proposes only a stale guard no hook pins.

Run: uv run pytest scripts/dev/tests/test_repo_guards.py
"""

import subprocess

import pytest
import repo_guards
from lib.repo_paths import REPO


def test_every_guard_exists_and_the_list_covers_every_hook_file():
    """A renamed guard or a widened hook turns this red instead of the list going stale (#5077)."""
    hooks = repo_guards.hook_files()
    assert "tests/repo/test_module_length_ratchet.py" in hooks
    missing = [
        p for p in repo_guards.GUARDS if not (REPO / p.split("::", 1)[0]).exists()
    ]
    assert missing == []
    uncovered = [
        f for f in hooks if not any(f.startswith(p) for p in repo_guards.GUARDS)
    ]
    assert uncovered == []


def test_a_new_ratchet_hook_file_becomes_a_guard_unless_a_listed_directory_holds_it(
    tmp_path,
):
    (tmp_path / "prek.toml").write_text(
        '[[repos]]\nrepo = "local"\n[[repos.hooks]]\nid = "static-ratchet-tests-new"\n'
        'entry = "uv run pytest -q scripts/new/tests/test_new.py '
        'tests/repo/test_held.py"\n'
        '[[repos.hooks]]\nid = "other"\nentry = "uv run pytest scripts/x/test_other.py"\n'
    )
    guards = repo_guards.guards(tmp_path)
    assert "scripts/new/tests/test_new.py" in guards
    assert "tests/repo/test_held.py" not in guards
    assert "scripts/x/test_other.py" not in guards
    assert guards[: len(repo_guards.LISTED)] == repo_guards.LISTED


@pytest.mark.parametrize("code", [0, 1])
def test_a_run_names_every_guard_and_returns_pytests_exit_code(code):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, code)

    assert repo_guards.run_guards(run, lambda: "") == code
    argv, kwargs = calls[0]
    assert argv[1:3] == ["-m", "pytest"]
    assert set(repo_guards.GUARDS) <= set(argv)
    assert kwargs["cwd"] == REPO


def test_the_advice_follows_the_guards_and_changes_no_exit_code(capsys):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0)

    assert repo_guards.run_guards(run, lambda: "facts notice: 1 to re-read.") == 0
    assert capsys.readouterr().out == "facts notice: 1 to re-read.\n"


def test_advice_that_cannot_run_says_why_in_one_line_per_source(tmp_path):
    assert repo_guards.advice(tmp_path).splitlines() == [
        "facts notice skipped: no merge base with origin/master; is it fetched?",
        "removed names skipped: no merge base with origin/master; is it fetched?",
    ]

    def crash(repo):
        raise KeyError("boom")

    sources = (("facts notice", crash), ("removed names", lambda repo: ""))
    assert repo_guards.advice(tmp_path, sources) == (
        "facts notice skipped: KeyError: 'boom'"
    )


def test_a_drop_candidate_holds_no_failing_module_and_no_hook_file():
    guards = ["quiet_test.py", "failing_tests", "node_test.py::test_x", "hook_tests"]
    hooks = ["hook_tests/test_ratchet.py"]
    failing = {"failing_tests/test_one.py", "node_test.py", "failing_tests_other.py"}
    assert repo_guards.drop_candidates(guards, hooks, failing) == ["quiet_test.py"]


def test_drop_candidates_prints_the_quiet_guard_and_runs_nothing(capsys):
    def run(argv, **kwargs):
        pytest.fail(f"ran {argv}")

    windows = []

    def survey(since, until):
        windows.append((until - since).days)
        return {"failing_test.py"}, 0

    guards = ["quiet_test.py", "failing_test.py"]
    assert repo_guards.main(["--drop-candidates"], guards, run, survey) == 0
    out = capsys.readouterr().out
    assert "quiet_test.py: no CI failure since" in out
    assert "failing_test.py" not in out
    assert windows == [7 * repo_guards.DEFAULT_WEEKS]


def test_drop_candidates_fails_when_the_survey_cannot_reach_github(capsys):
    def survey(since, until):
        raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 401")

    assert (
        repo_guards.main(["--drop-candidates"], ["quiet_test.py"], survey=survey) == 1
    )
    captured = capsys.readouterr()
    assert "HTTP 401" in captured.err
    assert captured.out == ""


def test_drop_candidates_warns_when_a_red_attempt_had_no_readable_log(capsys):
    def survey(since, until):
        return set(), 2

    assert (
        repo_guards.main(["--drop-candidates"], ["quiet_test.py"], survey=survey) == 0
    )
    out = capsys.readouterr().out
    assert "2 red attempts since" in out
    assert "had no readable log" in out
    assert "quiet_test.py: no CI failure since" in out
