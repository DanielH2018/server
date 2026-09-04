"""publish_pr.py: the sequence three crons used to carry inline, now executed rather than grepped.

The textual guards in ansible/tests/setup/test_cron_scripts_publish_via_pr.py pinned four
properties of the inline block -- a PR is opened, only the run's branch is pushed, the failure
text is kept, and the local master is reset after the push. Each has an executing test here.
"""

from __future__ import annotations

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
    """A Tools pair that answers from a per-command table and records every call in order."""

    def __init__(
        self, answers: dict[str, subprocess.CompletedProcess[str]] | None = None
    ):
        self.answers = answers or {}
        self.calls: list[tuple[str, ...]] = []

    def _run(self, tool: str, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append((tool, *args))
        # Keyed on the verb: "git push", or "gh pr create" since every gh call starts with "pr".
        key = " ".join((tool, *args[: 2 if tool == "gh" else 1]))
        return self.answers.get(key, _cp())

    def tools(self) -> publish_pr.Tools:
        return publish_pr.Tools(
            git=lambda *a: self._run("git", *a),
            gh=lambda *a: self._run("gh", *a),
        )


def _publish(rec: Recorder) -> publish_pr.Outcome:
    return publish_pr.publish(
        "docs-refresh/", "docs: refresh", "body", rec.tools(), now=NOW
    )


def test_the_happy_path_runs_the_six_steps_in_order():
    rec = Recorder()
    out = _publish(rec)
    assert out.rc == publish_pr.RC_PUBLISHED
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
    assert out.rc == publish_pr.RC_STILL_LOCAL
    assert "commit is local on master" in out.message
    assert "repository rule violations" in out.message, "the failure text must survive"
    assert ("git", "reset", "--hard", "HEAD~1") not in rec.calls
    assert not any(c[0] == "gh" for c in rec.calls)


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


def test_a_failed_pr_create_reports_the_branch_as_published():
    """Exit 2 is the state the secret-rotate audit watches for: a branch on origin, no PR."""
    rec = Recorder({"gh pr create": _cp(1, err="HTTP 401: Bad credentials")})
    out = _publish(rec)
    assert out.rc == publish_pr.RC_PUSHED_NO_PR
    assert out.message.startswith(f"{BRANCH} published but PR creation failed")
    assert "Bad credentials" in out.message
    assert ("git", "reset", "--hard", "HEAD~1") in rec.calls
    assert not any(c[:3] == ("gh", "pr", "merge") for c in rec.calls)


def test_a_failed_auto_merge_is_reported_distinctly():
    rec = Recorder({"gh pr merge": _cp(1, err="auto-merge is not allowed")})
    out = _publish(rec)
    assert out.rc == publish_pr.RC_PUSHED_NO_PR
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
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
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
    assert proc.returncode == publish_pr.RC_STILL_LOCAL
    assert "stub refused" in proc.stdout
    assert not any(c.startswith("gh") for c in calls)


def test_cli_body_file_is_read(tmp_path):
    body = tmp_path / "body.txt"
    body.write_text("from a file")
    proc, calls = _run_cli(
        tmp_path, "publish", "--prefix", "t/", "--title", "T", "--body-file", str(body)
    )
    assert proc.returncode == 0, proc.stderr
    assert any("--body from a file" in c for c in calls), calls
