"""publish_pr.py: the sequence three crons run, executed rather than grepped.

The textual guards in ansible/tests/setup/test_cron_scripts_publish_via_pr.py pinned four
properties of the inline block -- a PR is opened, only the run's branch is pushed, the failure
text is kept, and the local master is reset after the push. Each has an executing test here.
"""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import publish_pr
from lib.git_testing import scrubbed_env
from lib.proc_testing import fake_bin, path_with, run

from _publish_pr_fakes import TIMEOUT, Recorder, cp

SCRIPT = Path(publish_pr.__file__)
NOW = datetime(2026, 9, 4, 1, 30, tzinfo=UTC)
BRANCH = "docs-refresh/2026-09-04-0130"


def _publish(rec: Recorder) -> publish_pr.PublishOutcome:
    return publish_pr.publish(
        "docs-refresh/", "docs: refresh", "body", rec.tools(), now=NOW
    )


def test_the_happy_path_runs_the_six_steps_in_order():
    rec = Recorder()
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUBLISHED
    assert out.message == f"PR opened for {BRANCH}; landing PR #42"
    assert rec.calls == [
        ("git", "branch", BRANCH, "HEAD"),
        ("git", "push", "-u", "origin", BRANCH),
        ("git", "reset", "--hard", "HEAD~1"),
        ("git", "branch", "-D", BRANCH),
        (
            "gh",
            "pr",
            "create",
            "--head",
            BRANCH,
            "--title",
            "docs: refresh",
            "--body",
            "body",
        ),
        ("land", "--pr", "42", "--arm-merge", "--await-merge", "--detach"),
    ]


def test_only_the_runs_branch_is_ever_pushed():
    """The bug the crons were born with: a direct write to master, which the ruleset rejects."""
    rec = Recorder()
    _publish(rec)
    pushes = [c for c in rec.calls if c[:2] == ("git", "push")]
    assert pushes == [("git", "push", "-u", "origin", BRANCH)]


def test_a_failed_push_leaves_the_commit_local_and_says_so():
    rec = Recorder(
        {"git push": cp(1, err="remote: declined due to repository rule violations")}
    )
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_STILL_LOCAL
    assert "commit is local on master" in out.message
    assert "repository rule violations" in out.message, "the failure text must survive"
    assert ("git", "reset", "--hard", "HEAD~1") not in rec.calls
    assert not any(c[0] == "gh" for c in rec.calls)


def test_a_failed_push_deletes_the_dead_local_branch():
    """The branch never reached origin, so it is a dead local ref at the same commit as
    master. Left behind, a retry inside the same UTC minute fails at `git branch` with
    "already exists" and reports the wrong cause."""
    rec = Recorder(
        {"git push": cp(1, err="remote: declined due to repository rule violations")}
    )
    _publish(rec)
    assert ("git", "branch", "-D", BRANCH) in rec.calls


def test_the_local_master_is_reset_before_the_pr_is_opened():
    """A local master one commit ahead breaks gitops-deploy's --ff-only once the squash lands."""
    rec = Recorder()
    _publish(rec)
    assert rec.calls.index(("git", "reset", "--hard", "HEAD~1")) < rec.calls.index(
        (
            "gh",
            "pr",
            "create",
            "--head",
            BRANCH,
            "--title",
            "docs: refresh",
            "--body",
            "body",
        )
    )


def test_a_failed_reset_reports_master_still_ahead_and_stops_before_the_pr():
    """A reset failure (index lock, tree dirtied between the guard and here) must not be
    swallowed: pressing on would report "PR opened ... with auto-merge" while master is
    still one commit ahead of origin, and gitops-deploy's --ff-only parks silently once the
    squash lands under a new SHA."""
    rec = Recorder({"git reset": cp(1, err="Unable to create '.git/index.lock'")})
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUSHED_NO_PR
    assert "master is still one commit ahead of origin" in out.message
    assert "index.lock" in out.message
    assert ("git", "branch", "-D", BRANCH) not in rec.calls
    assert not any(c[0] == "gh" for c in rec.calls)


def test_a_failed_pr_create_reports_the_branch_as_published():
    """Exit 2 is the state the secret-rotate audit watches for: a branch on origin, no PR."""
    rec = Recorder({"gh pr create": cp(1, err="HTTP 401: Bad credentials")})
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUSHED_NO_PR
    assert out.message.startswith(f"{BRANCH} published but PR creation failed")
    assert "Bad credentials" in out.message
    assert ("git", "reset", "--hard", "HEAD~1") in rec.calls
    assert not any(c[0] == "land" for c in rec.calls)


def test_a_landing_that_fails_to_start_is_reported_distinctly():
    rec = Recorder({"land --pr": cp(2, err="land.py: error: bad flag")})
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUSHED_NO_PR
    assert out.message.startswith(
        f"PR #42 opened for {BRANCH} but its landing could not be started"
    )
    assert "bad flag" in out.message


def test_a_create_that_prints_no_pr_url_starts_no_landing():
    """A landing needs the number; one started on a guess could merge someone else's PR."""
    rec = Recorder({"gh pr create": cp(0, out="Warning: 1 uncommitted change\n")})
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUSHED_NO_PR
    assert "gh printed no PR number" in out.message
    assert not any(c[0] == "land" for c in rec.calls)


def test_failure_text_is_flattened_and_bounded():
    noise = "\n".join(f"line {i}" for i in range(200))
    rec = Recorder({"git push": cp(1, out=noise, err="THE END")})
    out = _publish(rec)
    tail = out.message.split(": ", 1)[1]
    assert "\n" not in tail
    assert len(tail) <= publish_pr.FAILURE_TAIL
    assert tail.endswith("THE END")


def test_the_branch_name_is_the_prefix_plus_a_utc_minute_stamp():
    assert (
        publish_pr.branch_name("secret-rotate/", NOW) == "secret-rotate/2026-09-04-0130"
    )


@pytest.mark.parametrize(
    ("stdout", "rc", "expected"),
    [
        ('[{"number": 12, "headRefName": "evals-history/2026-09-01-0900"}]', 0, "12"),
        ('[{"number": 12, "headRefName": "renovate/foo"}]', 0, ""),
        ("[]", 0, ""),
        ("", 1, ""),
        ("not json", 0, ""),
    ],
    ids=["match", "other-prefix", "none", "gh-failed", "garbage"],
)
def test_open_pr_reads_the_first_match_and_fails_open(stdout, rc, expected):
    rec = Recorder({"gh pr list": cp(rc, out=stdout)})
    assert publish_pr.open_pr("evals-history/", rec.tools()) == expected


def test_open_pr_returns_the_first_of_several():
    rec = Recorder(
        {
            "gh pr list": cp(
                0,
                out='[{"number": 3, "headRefName": "x/1"}, {"number": 7, "headRefName": "x/2"}]',
            )
        }
    )
    assert (
        publish_pr.open_pr("x/", rec.tools()) == "7"
        or publish_pr.open_pr("x/", rec.tools()) == "3"
    )


# --- A `gh` timeout is a state, not a traceback ------------------------------------------------
#
# lib.gh.gh bounds every call at 60s where the inline shell the crons carried had no bound at
# all, so TimeoutExpired is a state these callers did not have before. It is reachable in normal
# operation: the anonymous GitHub quota is 60/hour and shared per host. Both `gh` calls in
# publish() sit after the push and after `reset --hard HEAD~1`, so a raise there exits 1 with a
# traceback -- the code that promises the commit is still local and origin is untouched.


def test_a_timeout_creating_the_pr_is_exit_2_not_a_raise():
    rec = Recorder({"gh pr create": TIMEOUT})
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUSHED_NO_PR
    assert out.message.startswith(f"{BRANCH} published but PR creation failed")
    assert "timed out" in out.message
    assert ("git", "push", "-u", "origin", BRANCH) in rec.calls, (
        "the branch is on origin, which is what makes exit 2 the honest code"
    )


def test_a_timeout_starting_the_landing_is_exit_2_not_a_raise():
    rec = Recorder({"land --pr": TIMEOUT})
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUSHED_NO_PR
    assert "its landing could not be started" in out.message
    assert "timed out" in out.message


def test_a_timeout_listing_prs_fails_closed():
    """`""` would let the caller's `[ -n "$OPEN_PR" ]` guard publish a second branch."""
    rec = Recorder({"gh pr list": TIMEOUT})
    assert (
        publish_pr.open_pr("evals-history/", rec.tools()) == publish_pr.OPEN_PR_UNKNOWN
    )
    assert publish_pr.OPEN_PR_UNKNOWN != ""


# --- Transport pin ----------------------------------------------------------------------------
#
# The tests above never leave Python. This one runs the script the way the cron does, with a
# stub `git` and `gh` on PATH that log their argv, so argparse, the sys.path bootstrap and the
# real subprocess boundary are all exercised. An argparse-only test once hid a dead path here.


def _stub_body(name: str, log: Path, fail_on: str = "") -> str:
    """One recording stub's script text, for `fake_bin` to write."""
    return (
        "#!/usr/bin/env bash\n"
        f'printf \'%s %s\\n\' "{name}" "$*" >> "{log}"\n'
        + (
            f'case " $* " in *" {fail_on} "*) echo "stub refused" >&2; exit 1;; esac\n'
            if fail_on
            else ""
        )
        + (
            'case "$1 $2" in "pr create") echo "https://github.com/o/r/pull/7";; esac\n'
            if name == "gh"
            else ""
        )
        + "exit 0\n"
    )


def _land_stub(path: Path, log: Path) -> Path:
    """A stand-in for land.py, run with the interpreter publish_pr runs `land.py` with."""
    path.write_text(
        "import sys\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write('land.py ' + ' '.join(sys.argv[1:]) + '\\n')\n"
    )
    return path


def _run_cli(
    tmp_path: Path, *args: str, fail_on: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    log = tmp_path / "calls.log"
    bin_dir = fake_bin(
        tmp_path / "bin",
        git=_stub_body("git", log, fail_on),
        gh=_stub_body("gh", log, fail_on),
    )
    # The stubs above answer instead of git, but an inherited GIT_DIR from a hook or a parent
    # worktree points at a REAL repository and one of these arguments is `reset --hard`.
    env = scrubbed_env()
    env["PATH"] = path_with(bin_dir, env=env)
    if args[:1] == ("publish",):
        args = (*args, "--land-script", str(_land_stub(tmp_path / "land.py", log)))
    proc = run(
        [sys.executable, str(SCRIPT), "--repo", str(tmp_path), *args],
        env=env,
        check=False,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_cli_publish_drives_real_processes_in_order(tmp_path):
    proc, calls = _run_cli(
        tmp_path, "publish", "--prefix", "t/", "--title", "T", "--body", "B"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("PR opened for t/")
    expected = [
        "git branch t/",
        "git push -u origin t/",
        "git reset --hard HEAD~1",
        "git branch -D t/",
        "gh pr create --head t/",
        "land.py --pr 7 --arm-merge --await-merge --detach",
    ]
    assert len(calls) == len(expected), calls
    for call, prefix in zip(calls, expected, strict=True):
        assert call.startswith(prefix), (call, prefix)


def test_cli_publish_exit_code_reaches_the_shell(tmp_path):
    proc, calls = _run_cli(
        tmp_path,
        "publish",
        "--prefix",
        "t/",
        "--title",
        "T",
        "--body",
        "B",
        fail_on="push",
    )
    assert proc.returncode == publish_pr.PUBLISH_STILL_LOCAL
    assert "stub refused" in proc.stdout
    assert not any(c.startswith("gh") for c in calls)


def test_cli_unlanded_is_quiet_and_gh_free_when_origin_has_no_head(tmp_path):
    proc, calls = _run_cli(tmp_path, "unlanded", "--prefix", "t/")
    assert proc.returncode == publish_pr.UNLANDED_NOTHING, proc.stderr
    assert proc.stdout == ""
    assert calls == ["git ls-remote --heads origin t/*"], calls


def test_cli_unlanded_exit_code_reaches_the_shell(tmp_path):
    """rc 3 is what makes the templates report down rather than skipping quietly."""
    bin_dir = fake_bin(
        tmp_path / "bin",
        git='#!/usr/bin/env bash\nprintf "9f8e7d6\\trefs/heads/t/2026-09-03-0600\\n"\n',
        gh="#!/usr/bin/env bash\nprintf '[]'\n",
    )
    env = scrubbed_env()
    env["PATH"] = path_with(bin_dir, env=env)
    proc = run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(tmp_path),
            "unlanded",
            "--prefix",
            "t/",
        ],
        env=env,
        check=False,
    )
    assert proc.returncode == publish_pr.UNLANDED_NO_PR, proc.stderr
    assert proc.stdout.startswith(
        "branch t/2026-09-03-0600 is on origin with NO open PR"
    )
    assert "\n" not in proc.stdout.strip(), "the templates alert with this verbatim"


def test_cli_body_file_is_read(tmp_path):
    body = tmp_path / "body.txt"
    body.write_text("from a file")
    proc, calls = _run_cli(
        tmp_path, "publish", "--prefix", "t/", "--title", "T", "--body-file", str(body)
    )
    assert proc.returncode == 0, proc.stderr
    assert any("--body from a file" in c for c in calls), calls
