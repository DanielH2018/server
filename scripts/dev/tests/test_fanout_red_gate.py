"""The red and green gates against real git and real pytest, on a scratch repo.

`_scratch_pytest.run` is the runner, so every verdict below is read off output pytest
really printed.

Run: uv run pytest scripts/dev/tests/test_fanout_red_gate.py
"""

import pytest

from lib.proc_testing import write_exec

from _scratch_pytest import run
from fanout_lib.brief import Issue
from fanout_lib.review.red_gate import (
    RED_GREEN_LABEL,
    REPEATED_GREEN,
    REPEATED_RED,
    Gates,
    anti_patterns,
    green_cause,
    green_gate,
    red_gate,
    review_flags,
)
from fanout_lib.review.hardened_runs import ResetFailed
from fanout_lib.review.worktree_reset import reset_worktree, unhide_index
from lib.git_testing import commit, git, git_out, init_repo

CODE = "def double(x):\n    return x\n"
FIXED = "def double(x):\n    return 2 * x\n"
OLD_TEST = "from mod import double\n\n\ndef test_zero():\n    assert double(0) == 0\n"
# The gate runs pytest with `-c pyproject.toml`, so every scratch base carries one.
CONFIG = {"pyproject.toml": "[tool.pytest.ini_options]\n"}
NEW_TEST = (
    "\n\ndef test_two():\n    from mod import double\n\n    assert double(2) == 4\n"
)


def _repo(tmp_path, **red_files):
    """A repo whose base holds `mod.py` and one passing test, then the red commit."""
    repo = init_repo(tmp_path / "repo")
    base = commit(
        repo, "base", **{"mod.py": CODE, "tests/test_mod.py": OLD_TEST, **CONFIG}
    )
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


def test_the_gate_names_a_red_test_that_failed_only_on_a_missing_name(tmp_path):
    # A node id long enough that a `-q` summary cuts the failure text to "ImportE..." (#4023).
    long_name = "test_triple_of_one_is_three_with_a_name_long_enough_to_be_cut_short"
    absent = f"\n\ndef {long_name}():\n    from mod import triple\n\n    assert triple(1) == 3\n"
    repo, base, red = _repo(
        tmp_path, **{"tests/test_mod.py": OLD_TEST + NEW_TEST + absent}
    )
    gate = red_gate(run, repo, base, red)
    assert gate.passed, gate.reason
    assert gate.absent == [f"tests/test_mod.py::{long_name}"]


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


def test_an_uncommitted_edit_that_breaks_the_code_is_refused(tmp_path):
    """Base already doubles, so the new test passes there; only the dirty edit fails it."""
    repo = init_repo(tmp_path / "repo")
    base = commit(
        repo, "base", **{"mod.py": FIXED, "tests/test_mod.py": OLD_TEST, **CONFIG}
    )
    red = commit(repo, "red", **{"tests/test_new.py": NEW_TEST})
    (repo / "mod.py").write_text(CODE)
    gate = red_gate(run, repo, base, red)
    assert gate.reason.startswith("the test author left uncommitted changes: M mod.py")


def test_the_red_brief_carries_the_skills_anti_patterns_section_and_nothing_after(
    tmp_path,
):
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "# Skill\n\n## Anti-patterns\n\n- Assert the effect.\n\n## Next\n\nx\n"
    )
    assert anti_patterns(skill) == "## Anti-patterns\n\n- Assert the effect."
    assert anti_patterns(tmp_path / "missing.md") == ""


def test_the_green_gate_passes_a_fix_and_refuses_an_edited_red_test(tmp_path):
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": NEW_TEST})
    gate = red_gate(run, repo, base, red)
    commit(repo, "fix", **{"mod.py": FIXED})
    assert green_gate(run, repo, red, gate) == ""
    commit(repo, "weaken", **{"tests/test_new.py": "def test_two():\n    pass\n"})
    assert "tests/test_new.py" in green_gate(run, repo, red, gate)
    assert git_out(repo, "rev-parse", "HEAD") != red


def test_the_green_gate_passes_a_fix_that_appends_a_test_to_a_red_file(tmp_path):
    """#4213: the fix round added the test a reviewer asked for to the red file (#4214)."""
    repo, base, red = _repo(tmp_path, **{"tests/test_mod.py": OLD_TEST + NEW_TEST})
    gate = red_gate(run, repo, base, red)
    extra = "\n\ndef test_three():\n    from mod import double\n\n    assert double(3) == 6\n"
    commit(
        repo,
        "fix",
        **{"mod.py": FIXED, "tests/test_mod.py": OLD_TEST + NEW_TEST + extra},
    )
    assert green_gate(run, repo, red, gate) == ""


def test_the_green_gate_refuses_an_edit_to_a_test_already_in_a_red_file(tmp_path):
    """#4183: the fix edited a pre-existing test in the red file instead of the code."""
    repo, base, red = _repo(tmp_path, **{"tests/test_mod.py": OLD_TEST + NEW_TEST})
    gate = red_gate(run, repo, base, red)
    weakened = OLD_TEST.replace("double(0) == 0", "True") + NEW_TEST
    commit(repo, "fix", **{"mod.py": FIXED, "tests/test_mod.py": weakened})
    assert green_gate(run, repo, red, gate).endswith(
        "tests/test_mod.py changes or removes `def test_zero`"
    )


# A test that counts its runs in a file outside the repo, so no gate sees an untracked file,
# and asserts on the count's parity.
FLAKY = (
    "\n\ndef test_flaky():\n    import pathlib\n\n    from mod import double\n\n"
    "    count = pathlib.Path('{counter}')\n"
    "    n = int(count.read_text()) if count.exists() else 0\n"
    "    count.write_text(str(n + 1))\n"
    "    assert double(2) == 4 {op} n % 2 == 1\n"
)


def test_a_red_test_that_fails_only_sometimes_on_the_base_is_refused(tmp_path):
    """#4178 was itself a minute-boundary flake: one failing run proves nothing."""
    flaky = FLAKY.format(counter=tmp_path / "runs", op="or")
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": flaky})
    gate = red_gate(run, repo, base, red, runs=3)
    assert gate.reason.startswith("failed in only 2 of 3 runs on the unchanged code")


def test_a_red_test_that_passes_only_sometimes_after_the_fix_is_refused(tmp_path):
    """The base runs fail on the assertion; the fix's runs then pass on odd counts only."""
    flaky_after_fix = FLAKY.format(counter=tmp_path / "runs", op="and")
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": flaky_after_fix})
    gate = red_gate(run, repo, base, red, runs=3)
    assert gate.passed, gate.reason
    commit(repo, "fix", **{"mod.py": FIXED})
    reason = green_gate(run, repo, red, gate, runs=3)
    assert reason.startswith(
        "pytest exited 1; not passing: tests/test_new.py::test_flaky (FAILED) in run 2 of 3"
    )
    # A pass that does not repeat is not the red phase's catch, which `unmet` counts.
    assert green_cause(reason) == "flaky"


def test_the_pipeline_runs_each_gate_three_times():
    gates = Gates()
    assert gates.red is REPEATED_RED and REPEATED_RED.keywords == {"runs": 3}
    assert gates.green is REPEATED_GREEN and REPEATED_GREEN.keywords == {"runs": 3}


def test_the_green_gate_refuses_an_uncommitted_edit_the_pushed_head_lacks(tmp_path):
    """pytest runs the tree and the PR ships HEAD, so a vacuous uncommitted test is no pass."""
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": NEW_TEST})
    gate = red_gate(run, repo, base, red)
    commit(repo, "not a fix", **{"mod.py": CODE + "\n"})
    (repo / "tests/test_new.py").write_text("def test_two():\n    assert True\n")
    assert green_gate(run, repo, red, gate).endswith(
        "commit or discard these changes: M tests/test_new.py"
    )


def test_the_green_gate_runs_head_not_an_edit_hidden_from_git_status(tmp_path):
    """The implementer owns the index: a skip-worktree entry hides an edit from `status`."""
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": NEW_TEST})
    gate = red_gate(run, repo, base, red)
    commit(repo, "not a fix", **{"mod.py": CODE + "\n"})
    git_out(repo, "update-index", "--skip-worktree", "tests/test_new.py")
    (repo / "tests/test_new.py").write_text("def test_two():\n    assert True\n")
    assert git_out(repo, "status", "--porcelain") == ""
    assert green_gate(run, repo, red, gate).startswith("pytest exited 1; not passing")


def test_the_green_gate_passes_a_fixed_red_test_that_calls_git(tmp_path):
    """A red test that lists tracked files needs a repository to run in (#3837)."""
    calls_git = (
        "\n\ndef test_tracked():\n    import subprocess\n\n    from mod import double\n\n"
        "    listed = subprocess.run(\n        ['git', 'ls-files', 'mod.py'],\n"
        "        capture_output=True, text=True, check=True,\n    ).stdout\n"
        "    assert listed == 'mod.py\\n' and double(2) == 4\n"
    )
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": calls_git})
    gate = red_gate(run, repo, base, red)
    assert gate.passed, gate.reason
    commit(repo, "fix", **{"mod.py": FIXED})
    assert green_gate(run, repo, red, gate) == ""


DIFFS_BASE = (
    "\n\ndef test_changed():\n    import subprocess\n\n"
    "    changed = subprocess.run(\n"
    "        ['git', 'diff', '--name-only', 'origin/master', 'HEAD'],\n"
    "        capture_output=True, text=True, check=True,\n    ).stdout.split()\n"
    "    assert 'mod.py' in changed\n"
)


def test_the_green_gate_reads_the_same_origin_master_as_the_red_gate(tmp_path):
    """A clone of a path would map the source's local branch to `origin/master`."""
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": DIFFS_BASE})
    git_out(repo, "update-ref", "refs/remotes/origin/master", base)
    gate = red_gate(run, repo, base, red)
    assert gate.passed, gate.reason
    commit(repo, "fix", **{"mod.py": FIXED})
    assert green_gate(run, repo, red, gate) == ""


def test_moving_origin_master_after_the_red_gate_does_not_move_the_green_gates(
    tmp_path,
):
    """Every worktree shares `origin/master`, and a fetch or the implementer can move it (#3845).

    Moved to the fix itself, the live ref would leave `mod.py` out of the diff.
    """
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": DIFFS_BASE})
    git_out(repo, "update-ref", "refs/remotes/origin/master", base)
    gate = red_gate(run, repo, base, red)
    assert gate.passed, gate.reason
    fix = commit(repo, "fix", **{"mod.py": FIXED})
    git_out(repo, "update-ref", "refs/remotes/origin/master", fix)
    assert green_gate(run, repo, red, gate) == ""


def test_a_smudge_filter_cannot_rewrite_what_the_green_gate_runs(tmp_path):
    """The implementer owns the repo's config and `info/attributes` (#3837)."""
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": NEW_TEST})
    gate = red_gate(run, repo, base, red)
    commit(repo, "not a fix", **{"mod.py": CODE + "\n"})
    git_out(repo, "config", "filter.x.smudge", "sed 's/assert .*/assert True/'")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("tests/test_new.py filter=x\n")
    assert green_gate(run, repo, red, gate).startswith("pytest exited 1; not passing")


def test_an_ignored_root_conftest_is_refused_by_both_gates(tmp_path):
    """`git status` and `git diff` cannot see an ignored file, and `/*` ignores every root path."""
    repo = init_repo(tmp_path / "repo")
    files = {
        "mod.py": CODE,
        "tests/test_mod.py": OLD_TEST,
        ".gitignore": "/conftest.py\n__pycache__/\n",
    }
    base = commit(repo, "base", **files, **CONFIG)
    red = commit(repo, "red", **{"tests/test_new.py": NEW_TEST})
    gate = red_gate(run, repo, base, red)
    assert gate.passed, gate.reason
    (repo / "conftest.py").write_text("x = 1\n")
    assert red_gate(run, repo, base, red).reason == (
        "untracked pytest configuration: conftest.py"
    )
    commit(repo, "fix", **{"mod.py": FIXED})
    assert (
        green_gate(run, repo, red, gate)
        == "untracked pytest configuration: conftest.py"
    )


def _planted(tmp_path):
    """A repo at `red` whose tracked `land.sh` carries a skip-worktree edit."""
    repo, _, _ = _repo(tmp_path, **{"tests/test_new.py": NEW_TEST})
    commit(repo, "ignore", **{".gitignore": "/*.local.md\n", "land.sh": "real\n"})
    git_out(repo, "update-index", "--skip-worktree", "land.sh")
    (repo / "land.sh").write_text("planted\n")
    return repo


def test_reset_worktree_leaves_the_commits_tree_and_fanout_only(tmp_path):
    """Every change the red phase could hide from the gates is gone, `.fanout/` stays (#3852)."""
    repo = _planted(tmp_path)
    git_out(repo, "update-index", "--assume-unchanged", "mod.py")
    (repo / "mod.py").write_text(FIXED)
    (repo / "CLAUDE.local.md").write_text("ignored\n")
    (repo / ".mcp.json").write_text("{}\n")
    (repo / ".fanout").mkdir()
    (repo / ".fanout" / "brief.md").write_text("brief\n")
    (repo / ".fanout" / "land1.log").write_text("VERDICT: landed\n")
    head = git_out(repo, "rev-parse", "HEAD")

    reset_worktree(run, repo, head)

    assert (repo / "land.sh").read_text() == "real\n"
    assert (repo / "mod.py").read_text() == CODE
    assert git_out(repo, "ls-files", "-v", "land.sh", "mod.py") == "H land.sh\nH mod.py"
    assert not (repo / "CLAUDE.local.md").exists()
    assert not (repo / ".mcp.json").exists()
    assert (repo / ".fanout" / "brief.md").read_text() == "brief\n"
    assert not (repo / ".fanout" / "land1.log").exists()


def test_reset_worktree_runs_no_hook_filter_or_replace_ref_the_red_author_planted(
    tmp_path,
):
    """Each would otherwise change what the reset does, or run a command (#3871)."""
    repo, _, _ = _repo(tmp_path, **{"tests/test_new.py": NEW_TEST})
    script = write_exec(repo / "run.sh", "true\n")
    # A tracked line names the driver, so only blanking it in config keeps it from running.
    target = commit(repo, "script", **{".gitattributes": "*.py filter=planted\n"})
    fake = commit(repo, "fake", **{"mod.py": FIXED})
    git_out(repo, "reset", "--quiet", "--hard", target)
    git_out(repo, "replace", target, fake)
    git_out(repo, "config", "core.fileMode", "false")
    fired = tmp_path / "fired"
    write_exec(repo / ".git" / "hooks" / "post-index-change", f"echo hook >> {fired}\n")
    # `process` takes precedence over `smudge`, so the driver runs only if both survive.
    for key in ("smudge", "process"):
        command = f"sh -c 'echo {key} >> {fired}; cat'"
        git_out(repo, "config", f"filter.planted.{key}", command)

    def planted_reset(reset):
        (repo / "mod.py").write_text("planted\n")
        script.chmod(0o644)
        reset()
        ran = fired.read_text() if fired.exists() else ""
        return (repo / "mod.py").read_text(), ran, bool(script.stat().st_mode & 0o100)

    assert planted_reset(lambda: reset_worktree(run, repo, target)) == (CODE, "", True)
    # The control: plain git runs the hook and the filter, and checks out the replacement.
    plain = planted_reset(
        lambda: git(repo, "reset", "--quiet", "--hard", target, check=False)
    )
    assert plain[0] == FIXED and "process" in plain[1] and "hook" in plain[1]
    # And `fileMode=false` reads a cleared exec bit as no change, which `fileMode=true` does
    # not. The control reads that diff rather than the bit plain git's reset leaves: whether the
    # reset rewrites run.sh anyway turns on its stat cache, and on CI it once did (#3992).
    script.chmod(0o644)
    assert git_out(repo, "diff", "--name-only", "--", "run.sh") == ""
    assert (
        git_out(repo, "-c", "core.fileMode=true", "diff", "--name-only", "--", "run.sh")
        == "run.sh"
    )


def test_reset_worktree_refuses_a_repo_with_info_attributes(tmp_path):
    """A `working-tree-encoding` line there rewrites what the reset checks out (#3871)."""
    repo, _, red = _repo(tmp_path, **{"tests/test_new.py": NEW_TEST})
    (repo / ".git" / "info" / "attributes").write_text(
        "mod.py working-tree-encoding=UTF-16\n"
    )
    with pytest.raises(ResetFailed, match="info/attributes"):
        reset_worktree(run, repo, red)
    (repo / ".git" / "info" / "attributes").write_text("")
    reset_worktree(run, repo, red)
    assert (repo / "mod.py").read_text() == CODE


def test_reset_worktree_raises_when_a_git_step_fails(tmp_path):
    """While an `index.lock` exists every index write fails, and `git clean` still exits 0."""
    repo = _planted(tmp_path)
    (repo / ".git" / "index.lock").write_text("")
    with pytest.raises(ResetFailed, match="update-index"):
        reset_worktree(run, repo, git_out(repo, "rev-parse", "HEAD"))


def test_a_skip_worktree_edit_to_the_code_is_refused_once_unhidden(tmp_path):
    """Hidden, it would make the red tests fail for a reason no diff shows."""
    repo, base, red = _repo(tmp_path, **{"tests/test_new.py": NEW_TEST})
    git_out(repo, "update-index", "--skip-worktree", "mod.py")
    (repo / "mod.py").write_text("def double(x):\n    return -1\n")
    assert unhide_index(run, repo) == ["mod.py"]
    assert red_gate(run, repo, base, red).reason == (
        "the test author left uncommitted changes: M mod.py"
    )


def test_a_pytest_inifile_under_tests_is_not_a_test_file(tmp_path):
    repo, base, red = _repo(
        tmp_path,
        **{"tests/test_new.py": NEW_TEST, "tests/pytest.ini": "[pytest]\n"},
    )
    gate = red_gate(run, repo, base, red)
    assert gate.reason.endswith("not tests: tests/pytest.ini")


def test_only_a_review_batch_whose_every_issue_carries_the_label_gets_the_red_phase(
    capsys,
):
    red = Issue(1, "t", "b", labels=("claude", RED_GREEN_LABEL))
    plain = Issue(2, "t", "b", labels=("claude",))
    assert review_flags("1", [red], review=True, server=True) == (True, True)
    assert review_flags("1-2", [red, plain], review=True, server=True) == (True, False)
    assert review_flags("1", [red], review=False, server=True) == (False, False)
    assert "runs without a red phase" in capsys.readouterr().err
