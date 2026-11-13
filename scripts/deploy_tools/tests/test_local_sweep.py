"""`local_sweep.py`: one verdict for one commit, from the gate jobs this host can run.

Every sweep here runs stand-in checks against a scratch repository, so the suite does not run
itself. Three tests read the real `ci.yml` and hold the tool's tables to it.

Run: uv run pytest scripts/deploy_tools/tests/test_local_sweep.py
"""

import fcntl
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import local_sweep
from lib import yaml_fast
from lib.git import git
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


def _hosted_step(job: str) -> dict:
    """The step of the real workflow that `job`'s local check stands for."""
    (step,) = [
        s
        for s in _ci_jobs()[job]["steps"]
        if s.get("name") == local_sweep.LOCAL[job].step
    ]
    return step


@pytest.mark.parametrize("job", sorted(local_sweep.LOCAL))
def test_a_local_check_can_apply_everything_its_hosted_step_runs_under(job):
    """A step's own `env:`, `shell:` or `working-directory:`, and a job `env:` value only a
    runner can evaluate, are ones the local check would not apply."""
    assert set(_hosted_step(job)) == {"name", "run"}
    literal = local_sweep._job_env(REPO, job)
    assert literal == {k: str(v) for k, v in _ci_jobs()[job].get("env", {}).items()}


def test_the_pytest_check_uses_the_launcher_the_hosted_shards_use():
    # A runner's environment is new, so the hosted command needs no --exact.
    launcher = [a for a in local_sweep.LOCAL["pytest"].command(REPO) if a != "--exact"]
    assert " ".join(launcher) in _hosted_step("pytest")["run"]


def test_a_check_runs_under_the_env_the_swept_commits_workflow_gives_its_job(repo):
    workflow = (
        "jobs:\n  pytest:\n    env:\n      UV_NO_GROUP: lint\n"
        "      TOKEN: ${{ github.token }}\n"
    )
    commit(repo, "a workflow", **{CI_YML: workflow})
    shows_env = _bash('echo "FAILED ${UV_NO_GROUP:-unset} ${TOKEN:-unset}"; exit 1')

    (check,) = sweep("HEAD", repo=repo, checks=shows_env).checks

    assert check.detail == ("FAILED lint unset",)


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


def test_a_check_runs_under_a_hosted_runners_locale_not_this_hosts(repo, monkeypatch):
    for name in ("LANG", "LC_ALL", "LC_COLLATE", "LANGUAGE"):
        monkeypatch.setenv(name, "en_US.UTF-8")
    shows_locale = _bash(
        'echo "FAILED $LANG ${LC_ALL:-unset} ${LC_COLLATE:-unset} ${LANGUAGE:-unset}";'
        " exit 1"
    )

    (check,) = sweep("HEAD", repo=repo, checks=shows_locale).checks

    assert check.detail == ("FAILED C.UTF-8 unset unset unset",)


def test_the_scratch_tree_goes_unless_the_caller_keeps_it(repo, cache):
    checks = _bash("exit 0", "hooks")

    assert sweep("HEAD", repo=repo, checks=checks).kept is None
    assert not list(cache.glob("*-*"))

    kept = sweep("HEAD", repo=repo, checks=checks, keep=True).kept
    assert kept is not None
    assert (kept / "tree" / ".git").exists()
    assert (kept / "hooks.log").exists()


def test_a_sweep_removes_what_a_killed_sweep_left_behind_and_nothing_else(repo, cache):
    for name in ("sweep-killed", "kept-by-hand", "sweep-still-running"):
        (cache / name).mkdir(parents=True)
    # 1970 is older than any stale limit, and the third directory keeps the mtime it was made at.
    os.utime(cache / "sweep-killed", (0, 0))
    os.utime(cache / "kept-by-hand", (0, 0))

    sweep("HEAD", repo=repo, checks=_bash("exit 0"))

    assert sorted(p.name for p in cache.iterdir()) == [
        "kept-by-hand",
        "lock",
        "sweep-still-running",
    ]


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


def test_a_result_line_reads_back_as_the_sweep_it_was_written_from():
    written = Sweep(
        "abc", "def", (Check("hooks", "pass", 3), Check("pytest", "none", 9)), 13
    )
    in_syslog = (
        f"2026-11-13T15:16:09+00:00 daniel-box local-sweep[7]: {written.result_line()}"
    )

    assert local_sweep.parse_result_line(in_syslog) == written
    assert local_sweep.parse_result_line("local-sweep[7]: 1 pass, 1 none") is None
    # A line cut short by the log pipeline is not half a sweep.
    assert local_sweep.parse_result_line(in_syslog.rsplit(" ", 1)[0]) is None
    # A verdict the tool never writes would otherwise compare as "not red".
    assert local_sweep.parse_result_line(in_syslog.replace("=pass", "=ok")) is None


def test_no_part_of_a_runs_result_line_reads_as_another_run_or_a_commits_sweep():
    of_a_run = Sweep("abc", "def", (Check("pytest", "fail", 9),), 13, run=12345)
    line = of_a_run.result_line()

    assert local_sweep.parse_result_line(line) == of_a_run
    assert not any(
        local_sweep.parse_result_line(line[:end]) for end in range(len(line))
    )


@pytest.mark.parametrize(
    ("script", "verdict", "code"),
    [("exit 0", "pass", SWEEP_PASSED), ("exit 1", "fail", SWEEP_FAILED)],
)
def test_the_command_ends_on_the_result_line_and_exits_its_verdict(
    repo, capsys, script, verdict, code
):
    argv = ["HEAD", "--keep"]

    assert local_sweep.main(argv, repo=repo, checks=_bash(script)) == code

    *above, last = capsys.readouterr().out.splitlines()
    assert last.startswith(f"SWEEP-RESULT: verdict={verdict} ")
    assert any(line.startswith("kept ") for line in above)


def _raises(tree):
    raise RuntimeError("the check could not be built")


# A sweep that dies before any check reports.
DIES = {"pytest": Local(_raises, local_sweep.LOCAL["pytest"].failed)}


@pytest.mark.parametrize(
    ("checks", "swept", "printed"),
    [
        # A timer's run succeeded when the sweep reached a verdict, a red one included.
        (_bash("exit 1"), SWEEP_PASSED, "SWEEP-RESULT: verdict=fail "),
        # The record is written before the first check, so a sweep that died is not repeated.
        (DIES, SWEEP_NO_VERDICT, ""),
    ],
    ids=["red", "died"],
)
def test_with_a_record_the_command_sweeps_each_commit_once(
    repo, tmp_path, capsys, checks, swept, printed
):
    argv = ["HEAD", "--once", str(tmp_path / "last-swept")]

    def run() -> tuple[int, str]:
        return local_sweep.main(argv, repo=repo, checks=checks), capsys.readouterr().out

    code, out = run()
    assert code == swept
    assert printed in out
    # Neither stand-in can exit 0 in silence, so this run swept nothing.
    assert run() == (SWEEP_PASSED, "")
    commit(repo, "the next commit", a="1")
    assert run() != (SWEEP_PASSED, "")


def test_a_commit_git_cannot_resolve_gets_no_verdict(repo):
    assert local_sweep.main(["no-such-commit"], repo=repo) == SWEEP_NO_VERDICT


def test_a_sweep_waits_for_the_one_before_it_and_does_not_count_the_wait(
    repo, cache, tmp_path
):
    ran = tmp_path / "ran"
    cache.mkdir(parents=True)
    with (cache / "lock").open("w") as another_sweep, ThreadPoolExecutor(1) as pool:
        fcntl.flock(another_sweep, fcntl.LOCK_EX)
        waiting = pool.submit(sweep, "HEAD", repo=repo, checks=_bash(f"touch {ran}"))
        time.sleep(2)
        ran_beside_it = ran.exists()
        fcntl.flock(another_sweep, fcntl.LOCK_UN)
        result = waiting.result()

    assert not ran_beside_it
    assert ran.exists()
    assert result.seconds < 2


@pytest.fixture
def merge(repo, tmp_path):
    """A commit that only `repo`'s origin holds, as a pull request's merge is."""
    upstream = init_repo(tmp_path / "upstream", initial_commit="base")
    git("remote", "add", "origin", str(upstream), cwd=repo)
    return commit(upstream, "the merge", a="1")


def _github(merge, event="pull_request", actor="User", fork=False, log=None):
    """A stand-in for `gh api` over one run. Its first job skipped, so GitHub holds no log
    of it, and its second logged its checkout."""
    run = {
        "event": event,
        "head_sha": "f" * 40,
        "actor": {"login": "renovate[bot]", "type": actor},
        "repository": {"full_name": "owner/server"},
        "head_repository": {"full_name": "fork/server" if fork else "owner/server"},
    }
    jobs = {
        "jobs": [{"id": 1, "conclusion": "skipped"}, {"id": 2, "conclusion": "failure"}]
    }
    if log is None:
        log = f"git fetch origin +refs/heads/*:x +{merge}:refs/remotes/pull/7/merge\n"

    def read(*args: str) -> str:
        path = args[-1]
        if path.endswith("/jobs/1/logs"):
            raise subprocess.CalledProcessError(1, ["gh"], stderr="gh: Not Found")
        if path.endswith("/jobs/2/logs"):
            return log
        return json.dumps(jobs if path.endswith("/jobs?per_page=100") else run)

    return read


def test_a_run_sweeps_the_merge_its_log_names_and_tags_the_result(repo, merge, capsys):
    code = local_sweep.main(
        ["--run", "5"], repo=repo, checks=_bash("exit 0"), github=_github(merge)
    )

    assert code == SWEEP_PASSED
    last = capsys.readouterr().out.splitlines()[-1]
    assert f" sha={merge} " in last
    assert " run=5 " in last


@pytest.mark.parametrize(
    ("run", "why"),
    [
        ({"actor": "Bot"}, "renovate[bot] started it"),
        ({"fork": True}, "its branch is in the fork fork/server"),
        ({"event": "push"}, "it is a push run"),
        (
            {"log": "git checkout --force refs/heads/master\n"},
            "no job's log names the merge",
        ),
    ],
    ids=[
        "a-bot-started-it",
        "from-a-fork",
        "not-a-pull-request",
        "no-merge-in-the-log",
    ],
)
def test_a_run_with_no_tree_for_this_host_is_not_swept(repo, merge, capsys, run, why):
    # The first three stand-ins still log a merge the sweep could fetch and pass.
    code = local_sweep.main(
        ["--run", "5"], repo=repo, checks=_bash("exit 0"), github=_github(merge, **run)
    )

    assert code == SWEEP_NO_VERDICT
    assert f"no tree to sweep for run 5: {why}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["HEAD", "HEAD~1"],
        ["--fast"],
        ["HEAD", "--once"],
        ["HEAD", "--run", "5"],
        ["--run", "five"],
    ],
)
def test_anything_but_one_commit_or_one_run_is_a_usage_error(repo, argv):
    # A usage error that reached GitHub or the sweep would die there and exit 69.
    assert (
        local_sweep.main(argv, repo=repo, checks=DIES, github=_raises) == SWEEP_BAD_ARGS
    )
