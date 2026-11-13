"""`local_sweep.py`: one verdict for one commit, from the gate jobs this host can run.

Every sweep here runs stand-in checks against a scratch repository, so the suite does not run
itself. Three tests read the real `ci.yml` and hold the tool's tables to it.

Run: uv run pytest scripts/deploy_tools/tests/test_local_sweep.py
"""

import functools
import time
from pathlib import Path

import pytest

import local_sweep
from lib import yaml_fast
from lib.exit_codes import (
    SWEEP_BAD_ARGS,
    SWEEP_FAILED,
    SWEEP_NO_VERDICT,
    SWEEP_PASSED,
)
from lib.git_testing import commit, init_repo
from lib.proc_testing import path_with
from lib.repo_paths import REPO
from local_sweep import Check, Local, Sweep, sweep

CI_YML = str(local_sweep.CI_WORKFLOW)
WORKFLOW = "jobs:\n  hooks:\n    steps:\n      - name: {name}\n        run: {run}\n"


def _bash(script: str, job: str = "pytest", timeout: int = 60) -> dict[str, Local]:
    """A stand-in for `job` that runs `script`, with the real check's failure pattern."""
    failed = local_sweep.LOCAL[job].failed
    return {job: Local(lambda tree: ["bash", "-c", script], failed, timeout)}


def _ci_jobs() -> dict:
    return yaml_fast.safe_load((REPO / local_sweep.CI_WORKFLOW).read_text())["jobs"]


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path / "cache" / "local-sweep"


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path / "repo", initial_commit="first")


def test_every_job_the_gate_needs_is_local_or_hosted_with_a_reason():
    local, hosted = set(local_sweep.LOCAL), set(local_sweep.HOSTED)
    assert local | hosted == set(_ci_jobs()["prek"]["needs"]) - {"prove"}
    assert not local & hosted
    assert all(local_sweep.HOSTED.values())


def test_the_hosted_hooks_step_is_only_the_command_the_local_check_runs():
    """A step `env:`, `shell:` or `working-directory:` is one the local check would not apply."""
    (step,) = [
        s
        for s in _ci_jobs()["hooks"]["steps"]
        if s.get("name") == local_sweep.HOOKS_STEP
    ]
    assert set(step) == {"name", "run"}


def test_the_pytest_check_uses_the_launcher_the_hosted_shards_use():
    (step,) = [
        s
        for s in _ci_jobs()["pytest"]["steps"]
        if s.get("name") == "Run the test suite"
    ]
    assert " ".join(local_sweep.LOCAL["pytest"].command(REPO)) in step["run"]


def test_the_hooks_check_runs_the_command_the_swept_commit_carries(repo):
    hooks = {"hooks": local_sweep.LOCAL["hooks"]}
    step = local_sweep.HOOKS_STEP
    green = commit(repo, "green", **{CI_YML: WORKFLOW.format(name=step, run="exit 0")})
    commit(repo, "red", **{CI_YML: WORKFLOW.format(name=step, run="exit 1")})

    assert sweep(green, repo=repo, checks=hooks).verdict == "pass"
    assert sweep("HEAD", repo=repo, checks=hooks).verdict == "fail"


def test_a_commit_whose_workflow_lost_the_hooks_step_gets_no_verdict(repo):
    commit(repo, "renamed", **{CI_YML: WORKFLOW.format(name="Run hooks", run="exit 0")})

    result = sweep("HEAD", repo=repo, checks={"hooks": local_sweep.LOCAL["hooks"]})

    assert result.verdict == "none"
    assert local_sweep.HOOKS_STEP in result.checks[0].detail[0]


def test_a_check_whose_command_is_not_installed_gets_no_verdict(repo):
    missing = Local(
        lambda tree: ["no-such-sweep-tool"], local_sweep.LOCAL["hooks"].failed
    )
    assert sweep("HEAD", repo=repo, checks={"hooks": missing}).verdict == "none"


@pytest.mark.parametrize(
    ("verdicts", "overall"),
    [
        (("pass", "pass"), "pass"),
        (("none", "pass"), "none"),
        (("none", "fail"), "fail"),
        ((), "none"),
    ],
)
def test_a_failure_decides_the_sweep_and_a_missing_verdict_is_never_a_pass(
    verdicts, overall
):
    checks = tuple(Check(f"job{i}", verdict, 1) for i, verdict in enumerate(verdicts))
    assert Sweep("abc", "def", checks, 2).verdict == overall


@pytest.mark.parametrize(
    ("job", "output", "detail"),
    [
        # Colour codes go, and the lines around the ones that name a failure go with them.
        (
            "pytest",
            r"collected 3\n\033[31mFAILED\033[0m a::b - boom\nERROR c - gone\n1 failed\n",
            ("FAILED a::b - boom", "ERROR c - gone"),
        ),
        # The line prek prints under `--color always`.
        (
            "hooks",
            r"ruff\033[32m....\033[39m\033[32mPassed\033[0m\n"
            r"vale\033[32m....\033[39m\033[31;7mFailed\033[0m\n- hook id: vale\n",
            ("vale....Failed",),
        ),
        # Nothing names a failure, so the end of the log is all there is to show.
        ("pytest", r"Traceback\nboom\n", ("Traceback", "boom")),
    ],
    ids=["pytest", "hooks", "no-line-names-it"],
)
def test_a_failed_check_lists_the_lines_that_name_what_failed(
    repo, job, output, detail
):
    failing = _bash(f"printf '{output}'; exit 1", job)
    (check,) = sweep("HEAD", repo=repo, checks=failing).checks
    assert (check.verdict, check.detail) == ("fail", detail)


def _gone(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return True
    return state == "Z"


def test_a_check_that_outruns_its_limit_is_killed_with_the_workers_it_started(
    repo, tmp_path
):
    worker = tmp_path / "worker.pid"
    hung = _bash(f"sleep 60 & echo $! > {worker}; wait", timeout=2)

    result = sweep("HEAD", repo=repo, checks=hung)

    assert result.verdict == "none"
    pid = int(worker.read_text())
    deadline = time.monotonic() + 5
    while not _gone(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(pid)


def test_a_check_does_not_inherit_the_virtualenv_that_runs_the_sweep(repo, monkeypatch):
    venv = "/srv/checkout/.venv"
    monkeypatch.setenv("VIRTUAL_ENV", venv)
    monkeypatch.setenv("PATH", path_with(f"{venv}/bin"))
    shows_env = _bash('echo "FAILED venv=${VIRTUAL_ENV:-unset} path=$PATH"; exit 1')

    (check,) = sweep("HEAD", repo=repo, checks=shows_env).checks

    assert "venv=unset" in check.detail[0]
    assert venv not in check.detail[0]


def test_the_scratch_tree_goes_unless_the_caller_keeps_it(repo, cache):
    checks = _bash("exit 0", "hooks")

    assert sweep("HEAD", repo=repo, checks=checks).kept is None
    assert list(cache.iterdir()) == []

    kept = sweep("HEAD", repo=repo, checks=checks, keep=True).kept
    assert kept is not None
    assert (kept / "tree" / ".git").exists()
    assert (kept / "hooks.log").exists()


def test_the_report_counts_first_and_lists_only_what_did_not_pass():
    named = tuple(f"FAILED t::{n}" for n in range(local_sweep.MAX_DETAIL + 5))
    result = Sweep(
        "abc", "def", (Check("hooks", "pass", 3), Check("pytest", "fail", 9, named)), 13
    )

    lines = local_sweep.report(result)

    assert lines[0] == "1 pass, 1 fail"
    assert not any(line.startswith("hooks") for line in lines)
    assert [line.strip() for line in lines[2:-2]] == list(
        named[: local_sweep.MAX_DETAIL]
    )
    assert "5 more" in lines[-2]
    assert lines[-1] == (
        "SWEEP-RESULT: verdict=fail sha=abc tree=def seconds=13"
        " hooks=pass hooks_seconds=3 pytest=fail pytest_seconds=9"
    )


@pytest.mark.parametrize(
    ("script", "verdict", "code"),
    [("exit 0", "pass", SWEEP_PASSED), ("exit 1", "fail", SWEEP_FAILED)],
)
def test_the_command_ends_on_the_result_line_and_exits_its_verdict(
    repo, capsys, script, verdict, code
):
    scratch = functools.partial(sweep, repo=repo, checks=_bash(script))

    assert local_sweep.main(["HEAD", "--keep"], run=scratch) == code

    *above, last = capsys.readouterr().out.splitlines()
    assert last.startswith(f"SWEEP-RESULT: verdict={verdict} ")
    assert any(line.startswith("kept ") for line in above)


def test_a_commit_git_cannot_resolve_gets_no_verdict():
    assert local_sweep.main(["no-such-commit"]) == SWEEP_NO_VERDICT


def test_a_sweep_that_raises_is_no_verdict_and_never_a_failed_check():
    def crash(commit, **_):
        raise RuntimeError("the cache directory is read-only")

    assert local_sweep.main(["HEAD"], run=crash) == SWEEP_NO_VERDICT


@pytest.mark.parametrize("argv", [[], ["HEAD", "HEAD~1"], ["--fast"]])
def test_anything_but_one_commit_is_a_usage_error(argv):
    def swept(commit, **_):
        raise AssertionError(f"a usage error reached the sweep with {commit}")

    assert local_sweep.main(argv, run=swept) == SWEEP_BAD_ARGS
