"""`land.sh --detach --await-verdict` end to end, plus the unit rules of `land_lib/detach.py`.

Run: uv run pytest scripts/deploy_tools/tests/test_land_detach.py

The end-to-end half runs the shim as a process against the same `gh`/`git` stubs
`test_land_arm_merge_through_the_shim.py` uses, because what is under test is the wiring: the
flags reaching `Options`, the fork, the logfile, and the parent exiting with the CHILD's code
and printing the CHILD's verdict. A unit test against a fake `Tools` cannot see any of that.

THE PROVEN PATH MUST STAY BYTE-IDENTICAL. `--detach` was added to a script that lands every PR
in this repo, unattended, so `test_no_flags_reaches_the_same_landing_as_before` asserts a run
with neither flag never forks and never resolves `--since` for itself. That is the invariant
that makes the new mode safe to ship before it has been exercised on a real landing (#2853).
"""

import io
import os
import subprocess
from pathlib import Path

import pytest

from deploy_tools.land_lib import detach
from lib.exit_codes import LAND_GAVE_UP
from lib.git_testing import scrubbed_env
from lib.proc_testing import fake_bin, path_with

_LAND_SH = Path(__file__).resolve().parents[1] / "land.sh"

_GH_STUB = """#!/bin/sh
case "$*" in
  *"--json state,title"*)  printf '{"state":"MERGED","title":"t"}\\n' ;;
  *mergeCommit*)           printf '{"mergeCommit":{"oid":"1f0e7c4a9b2d5e6f8a0c1b3d4e5f6071"}}\\n' ;;
  *changedFiles*)          printf '{"files":[],"changedFiles":0}\\n' ;;
  *)                       printf '{}\\n' ;;
esac
"""

_GIT_STUB = """#!/bin/sh
printf '%s\\t%s\\n' "$PWD" "$*" >> "{calls}/git-calls"
case "$*" in
  "rev-parse origin/master") printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n' ;;
esac
"""


def _run(tmp_path: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    bin_dir = fake_bin(
        tmp_path / "bin",
        gh=_GH_STUB,
        git=_GIT_STUB.replace("{calls}", str(tmp_path)),
    )
    (tmp_path / "git-calls").touch()
    job_dir = tmp_path / "job"
    (job_dir / "tmp").mkdir(parents=True)
    env = {
        # `git commit` exports GIT_DIR and GIT_INDEX_FILE to its hooks, and a test inheriting
        # them has written the real repo.
        **scrubbed_env(),
        "PATH": path_with(bin_dir),
        "LAND_PRIMARY": str(tmp_path),
        detach.LOG_DIR_ENV: str(job_dir),
    }
    return subprocess.run(
        ["bash", str(_LAND_SH), "--pr", "939", *flags],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _git_argv(tmp_path: Path) -> list[str]:
    lines = (tmp_path / "git-calls").read_text().splitlines()
    return [line.partition("\t")[2] for line in lines]


def _logs(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "job" / "tmp").glob("land939-*.log"))


def test_await_verdict_prints_the_childs_verdict_and_exits_with_its_code(tmp_path):
    result = _run(tmp_path, "--detach", "--await-verdict")

    assert result.returncode == 0, result.stderr
    assert "land --detach: running in background (pid " in result.stdout
    assert "VERDICT: nothing-to-deploy" in result.stdout
    # The parent printed it; the child WROTE it, which is the whole point of the log. The
    # count is asserted rather than unpacked: a glob that stops matching returns empty, and
    # `in log.read_text()` over nothing would never run.
    logs = _logs(tmp_path)
    assert len(logs) == 1, logs
    assert "VERDICT: nothing-to-deploy" in logs[0].read_text()


def test_detach_alone_returns_at_once_and_names_the_log(tmp_path):
    """The reject half of the wait: without `--await-verdict` the parent must not block."""
    result = _run(tmp_path, "--detach")

    assert result.returncode == 0, result.stderr
    assert "tail: " in result.stdout
    assert "Waiting up to" not in result.stdout
    assert "VERDICT:" not in result.stdout


def test_detach_resolves_since_from_origin_master_itself(tmp_path):
    """The `git rev-parse origin/master` the skill asked the caller to run first."""
    _run(tmp_path, "--detach", "--await-verdict")
    assert "rev-parse origin/master" in _git_argv(tmp_path)


def test_no_flags_reaches_the_same_landing_as_before(tmp_path):
    """The invariant that makes `--detach` safe to add to the script that lands every PR.

    Neither flag: no fork, no logfile, no `--since` resolved for the caller, and the verdict
    on this process's own stdout.
    """
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "VERDICT: nothing-to-deploy" in result.stdout
    assert "land --detach" not in result.stdout
    assert _logs(tmp_path) == []
    assert "rev-parse origin/master" not in _git_argv(tmp_path)


# -- land_lib/detach.py's own rules ---------------------------------------------------------


def test_the_log_name_leads_with_the_pr_number(tmp_path):
    """`fanout_lib/status.py` globs `land*.log` to find a batch's landing."""
    path = detach.log_path("2853", log_dir=tmp_path)
    assert path.name.startswith("land2853-") and path.suffix == ".log"
    assert path.parent == tmp_path


def test_the_log_dir_follows_claude_job_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(detach.LOG_DIR_ENV, str(tmp_path))
    assert detach.log_path("7").parent == tmp_path / "tmp"


def test_the_log_dir_falls_back_outside_a_claude_session(monkeypatch):
    """The reject half: the flag has to work from a plain shell and from a systemd unit."""
    monkeypatch.delenv(detach.LOG_DIR_ENV, raising=False)
    assert detach.log_path("7").parent == detach.FALLBACK_LOG_DIR


def test_the_verdict_is_read_out_of_the_log(tmp_path):
    log = tmp_path / "land7.log"
    log.write_text("  == 4/6 deploying\nVERDICT: settled (PR #7, tags: sonarr)\n")
    assert detach.verdict_in(log) == "VERDICT: settled (PR #7, tags: sonarr)"


@pytest.mark.parametrize(
    "text",
    [
        "  == 4/6 deploying\nno verdict here\n",
        # Indented, so not the landing's own line: `say()` prints two-space progress lines.
        "  VERDICT: settled (x)\n",
    ],
)
def test_a_log_with_no_verdict_line_reads_as_none(tmp_path, text):
    """The reject half. PR #2437 exited 1 having printed no verdict at all."""
    log = tmp_path / "land7.log"
    log.write_text(text)
    assert detach.verdict_in(log) is None


def test_an_unreadable_log_reads_as_none(tmp_path):
    assert detach.verdict_in(tmp_path / "absent.log") is None


def test_await_verdict_gives_up_on_its_budget_and_leaves_the_child_running(tmp_path):
    """A landing holds the deploy locks, so killing it mid-apply is worse than losing sight
    of it. The give-up is `LAND_GAVE_UP`, and it says where to look."""
    log = tmp_path / "land7.log"
    log.write_text("still going\n")
    ticks = iter([0.0, 1.0, 99.0])
    out = io.StringIO()
    child = subprocess.Popen(["sleep", "30"])
    try:
        rc = detach.await_verdict(
            child.pid,
            log,
            timeout_s=10,
            poll_s=0,
            clock=lambda: next(ticks),
            sleep=lambda _: None,
            out=out,
        )
    finally:
        child.kill()
        child.wait()
    assert rc == LAND_GAVE_UP
    assert "no verdict within 10s" in out.getvalue()
    assert str(log) in out.getvalue()


def test_await_verdict_reports_a_child_that_finished_without_a_verdict(tmp_path):
    """PR #2437 exited 1 with no `VERDICT:` line. The exit code is the authority, so the wait
    must end on the child rather than sit out its whole budget waiting for a line."""
    log = tmp_path / "land7.log"
    log.write_text("a traceback, and nothing else\n")
    out = io.StringIO()
    # A raw fork, not Popen: `await_verdict` reaps the pid itself, and Popen's finalizer warns
    # when it finds the child already gone.
    pid = os.fork()
    if pid == 0:
        os._exit(1)

    # A real clock and a short poll: only the reap may end this wait, so the budget is far
    # larger than the child takes to exit.
    rc = detach.await_verdict(pid, log, timeout_s=60, poll_s=0.05, out=out)
    assert rc == 1
    assert "(none printed" in out.getvalue() and str(log) in out.getvalue()
