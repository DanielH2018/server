"""publish_pr.py: the sequence three crons used to carry inline, now executed rather than grepped.

The textual guards in ansible/tests/setup/test_cron_scripts_publish_via_pr.py pinned four
properties of the inline block -- a PR is opened, only the run's branch is pushed, the failure
text is kept, and the local master is reset after the push. Each has an executing test here.
"""

import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publish_pr

SCRIPT = Path(publish_pr.__file__)
NOW = datetime(2026, 9, 4, 1, 30, tzinfo=UTC)
BRANCH = "docs-refresh/2026-09-04-0130"


def _cp(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


class Recorder:
    """A Tools pair that answers from a per-command table and records every call in order.

    An answer may be an exception instance instead of a completed process, which the runner
    raises. That is the only way to reach the timeout path: ``lib.gh.gh`` bounds every call at
    60s, and a stub that actually sleeps past it is not a test anyone can run.
    """

    def __init__(
        self,
        answers: dict[str, subprocess.CompletedProcess[str] | Exception] | None = None,
    ):
        self.answers = answers or {}
        self.calls: list[tuple[str, ...]] = []

    def _run(self, tool: str, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append((tool, *args))
        # Keyed on the verb: "git push", or "gh pr create" since every gh call starts with "pr".
        key = " ".join((tool, *args[: 2 if tool == "gh" else 1]))
        answer = self.answers.get(key, _cp())
        if isinstance(answer, Exception):
            raise answer
        return answer

    def tools(self) -> publish_pr.PublishTools:
        # **kw absorbs the `timeout=` the ls-remote pre-flight passes; the stub never blocks,
        # so the value is nothing to record.
        return publish_pr.PublishTools(
            git=lambda *a, **kw: self._run("git", *a),
            gh=lambda *a, **kw: self._run("gh", *a),
        )


def _publish(rec: Recorder) -> publish_pr.PublishOutcome:
    return publish_pr.publish(
        "docs-refresh/", "docs: refresh", "body", rec.tools(), now=NOW
    )


def test_the_happy_path_runs_the_six_steps_in_order():
    rec = Recorder()
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUBLISHED
    assert out.message == f"PR opened for {BRANCH} with auto-merge"
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
        ("gh", "pr", "merge", "--auto", "--squash", "--delete-branch", BRANCH),
    ]


def test_only_the_runs_branch_is_ever_pushed():
    """The bug the crons were born with: a direct write to master, which the ruleset rejects."""
    rec = Recorder()
    _publish(rec)
    pushes = [c for c in rec.calls if c[:2] == ("git", "push")]
    assert pushes == [("git", "push", "-u", "origin", BRANCH)]


def test_a_failed_push_leaves_the_commit_local_and_says_so():
    rec = Recorder(
        {"git push": _cp(1, err="remote: declined due to repository rule violations")}
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
    "already exists" and reports the wrong cause (issue #1086)."""
    rec = Recorder(
        {"git push": _cp(1, err="remote: declined due to repository rule violations")}
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
    squash lands under a new SHA (issue #1086)."""
    rec = Recorder({"git reset": _cp(1, err="Unable to create '.git/index.lock'")})
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUSHED_NO_PR
    assert "master is still one commit ahead of origin" in out.message
    assert "index.lock" in out.message
    assert ("git", "branch", "-D", BRANCH) not in rec.calls
    assert not any(c[0] == "gh" for c in rec.calls)


def test_a_failed_pr_create_reports_the_branch_as_published():
    """Exit 2 is the state the secret-rotate audit watches for: a branch on origin, no PR."""
    rec = Recorder({"gh pr create": _cp(1, err="HTTP 401: Bad credentials")})
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUSHED_NO_PR
    assert out.message.startswith(f"{BRANCH} published but PR creation failed")
    assert "Bad credentials" in out.message
    assert ("git", "reset", "--hard", "HEAD~1") in rec.calls
    assert not any(c[:3] == ("gh", "pr", "merge") for c in rec.calls)


def test_a_failed_auto_merge_is_reported_distinctly():
    rec = Recorder({"gh pr merge": _cp(1, err="auto-merge is not allowed")})
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUSHED_NO_PR
    assert out.message.startswith(
        f"PR opened for {BRANCH} but auto-merge could not be enabled"
    )


def test_failure_text_is_flattened_and_bounded():
    noise = "\n".join(f"line {i}" for i in range(200))
    rec = Recorder({"git push": _cp(1, out=noise, err="THE END")})
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
    rec = Recorder({"gh pr list": _cp(rc, out=stdout)})
    assert publish_pr.open_pr("evals-history/", rec.tools()) == expected


def test_open_pr_returns_the_first_of_several():
    rec = Recorder(
        {
            "gh pr list": _cp(
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

TIMEOUT = subprocess.TimeoutExpired(cmd=["gh"], timeout=60.0)


def test_a_timeout_creating_the_pr_is_exit_2_not_a_raise():
    rec = Recorder({"gh pr create": TIMEOUT})
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUSHED_NO_PR
    assert out.message.startswith(f"{BRANCH} published but PR creation failed")
    assert "timed out" in out.message
    assert ("git", "push", "-u", "origin", BRANCH) in rec.calls, (
        "the branch is on origin, which is what makes exit 2 the honest code"
    )


def test_a_timeout_enabling_auto_merge_is_exit_2_not_a_raise():
    rec = Recorder({"gh pr merge": TIMEOUT})
    out = _publish(rec)
    assert out.rc == publish_pr.PUBLISH_PUSHED_NO_PR
    assert "auto-merge could not be enabled" in out.message
    assert "timed out" in out.message


def test_a_timeout_listing_prs_fails_closed():
    """`""` would let the caller's `[ -n "$OPEN_PR" ]` guard publish a second branch."""
    rec = Recorder({"gh pr list": TIMEOUT})
    assert (
        publish_pr.open_pr("evals-history/", rec.tools()) == publish_pr.OPEN_PR_UNKNOWN
    )
    assert publish_pr.OPEN_PR_UNKNOWN != ""


# --- unlanded: is a previous run's branch still on origin --------------------------------------

_HEAD = "9f8e7d6\trefs/heads/docs-refresh/2026-09-03-0600"


def _unlanded(rec: Recorder) -> publish_pr.PublishOutcome:
    return publish_pr.unlanded("docs-refresh/", rec.tools())


def test_no_remote_head_means_nothing_is_unlanded():
    out = _unlanded(Recorder({"git ls-remote": _cp(0, out="")}))
    assert out.rc == publish_pr.UNLANDED_NOTHING
    assert out.message == ""


def test_an_unreachable_origin_fails_closed():
    """`|| true` here would read an unreachable origin as "no stale branch" and publish."""
    rec = Recorder(
        {"git ls-remote": _cp(128, err="Could not resolve host: github.com")}
    )
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_ORIGIN_UNREADABLE
    assert "Could not resolve host" in out.message
    assert not any(c[0] == "gh" for c in rec.calls), (
        "the PR lookup must not run when origin could not be read"
    )


def test_a_remote_head_with_an_open_pr_is_the_benign_code():
    rec = Recorder(
        {
            "git ls-remote": _cp(0, out=_HEAD),
            "gh pr list": _cp(
                0, out='[{"number": 41, "headRefName": "docs-refresh/2026-09-03-0600"}]'
            ),
        }
    )
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_PR_OPEN
    assert "PR #41" in out.message
    assert out.branch == "docs-refresh/2026-09-03-0600"


def test_a_remote_head_with_no_open_pr_is_the_stuck_code():
    """The state a failed `gh pr create` leaves: a branch on origin, no PR, no local trace."""
    rec = Recorder({"git ls-remote": _cp(0, out=_HEAD), "gh pr list": _cp(0, out="[]")})
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_NO_PR
    assert "NO open PR" in out.message


def test_an_open_pr_on_a_sibling_branch_does_not_clear_an_orphan():
    """Two heads under one prefix is the state issue #1066 says orphans accumulate into.

    Matching the PR by prefix would return the sibling's number, downgrade the stuck state to
    the benign one, and name the wrong branch while doing it.
    """
    rec = Recorder(
        {
            "git ls-remote": _cp(0, out=_HEAD),
            "gh pr list": _cp(
                0, out='[{"number": 41, "headRefName": "docs-refresh/2026-09-04-1800"}]'
            ),
        }
    )
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_NO_PR
    assert out.branch == "docs-refresh/2026-09-03-0600"
    assert "NO open PR" in out.message


def test_an_origin_that_never_answers_is_bounded_and_fails_closed():
    """`lib.git.git` has no default timeout and this runs under the git-tree lock.

    An unbounded ls-remote against a blackholed origin parks the GitOps deployer, which waits
    `flock -w 180`. A raise here would also reach Kuma as a flattened Python traceback.
    """
    rec = Recorder(
        {"git ls-remote": subprocess.TimeoutExpired(cmd=["git"], timeout=30.0)}
    )
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_ORIGIN_UNREADABLE
    assert "did not answer within 30s" in out.message
    assert not any(c[0] == "gh" for c in rec.calls)


def test_the_pre_flight_bounds_its_own_ls_remote():
    """The bound must reach the transport, not just exist as a constant."""
    seen: dict[str, object] = {}

    def git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen[args[0]] = kwargs.get("timeout")
        return _cp()

    publish_pr.unlanded(
        "t/", publish_pr.PublishTools(git=git, gh=lambda *a, **kw: _cp())
    )
    assert seen["ls-remote"] == publish_pr.LS_REMOTE_TIMEOUT_S


def test_a_pr_lookup_that_times_out_cannot_clear_the_branch():
    """ls-remote decides; the PR number only labels. A `gh` failure must not read as clean."""
    rec = Recorder({"git ls-remote": _cp(0, out=_HEAD), "gh pr list": TIMEOUT})
    out = _unlanded(rec)
    assert out.rc == publish_pr.UNLANDED_NO_PR
    assert "did not answer" in out.message


def test_the_clean_path_spends_no_gh_call():
    """The quota that makes `gh` time out is 60/hour and shared per host.

    ls-remote goes over git's own credential path, so the common case costs nothing from it.
    """
    rec = Recorder({"git ls-remote": _cp(0, out="")})
    _unlanded(rec)
    assert not any(c[0] == "gh" for c in rec.calls)


# --- Transport pin ----------------------------------------------------------------------------
#
# The tests above never leave Python. This one runs the script the way the cron does, with a
# stub `git` and `gh` on PATH that log their argv, so argparse, the sys.path bootstrap and the
# real subprocess boundary are all exercised. An argparse-only test once hid a dead path here.


def _stub(bin_dir: Path, name: str, log: Path, fail_on: str = "") -> None:
    script = bin_dir / name
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s %s\\n\' "{name}" "$*" >> "{log}"\n'
        + (
            f'case " $* " in *" {fail_on} "*) echo "stub refused" >&2; exit 1;; esac\n'
            if fail_on
            else ""
        )
        + "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _run_cli(
    tmp_path: Path, *args: str, fail_on: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _stub(bin_dir, "git", log, fail_on)
    _stub(bin_dir, "gh", log, fail_on)
    # Every GIT_* stripped. The stubs above answer instead of git, but an inherited GIT_DIR
    # from a hook or a parent worktree points at a REAL repository, and one of these arguments
    # is `reset --hard`. A git-driving fixture under prek has already written the real repo once.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(tmp_path), *args],
        env=env,
        capture_output=True,
        text=True,
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
        "gh pr merge --auto --squash --delete-branch t/",
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
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("git", 'printf "9f8e7d6\\trefs/heads/t/2026-09-03-0600\\n"\n'),
        ("gh", "printf '[]'\n"),
    ):
        stub = bin_dir / name
        stub.write_text(f"#!/usr/bin/env bash\n{body}exit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    proc = subprocess.run(
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
        capture_output=True,
        text=True,
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
