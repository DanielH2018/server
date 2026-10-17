"""The review pipeline's CI wait, on both hosts (#4737, #5498).

The fake runner answers `cc-wait gh-ci` with the scripted `run.ci` exits and output, the PR's
`statusCheckRollup` with `run.rollup`, and a failed job's log with `run.job_logs`.

Run: uv run pytest scripts/dev/tests/test_fanout_review_ci.py
"""

import json

from _review_fakes import PR, _pipeline, _report
from fanout_lib.review.review_ci import CI_RECHECK_S, CI_SLICE_S, CI_WAIT_S
from fanout_lib.status import BLOCKER, PR_URL
from fanout_lib.target import Target

PASSED = (0, "WAIT: passed abcd1234: 20 check runs passed\n")
FAILED = (1, "WAIT: failed abcd1234: 1 failed: pytest\n")
CANCELLED = (1, "WAIT: cancelled abcd1234: 1 cancelled: pytest\n")
JOB_URL = "https://github.com/DanielH2018/server/actions/runs/9/job/777"


class Clock:
    """A clock the CI wait's pauses and cc-wait slices advance, since the default never moves."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _clean_review():
    return [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]


def _server_batch(tmp_path, reports, clock=None, host="daniel-server"):
    clock = clock or Clock()
    pipeline, run = _pipeline(
        tmp_path, reports, host=host, clock=clock, sleep=clock.sleep
    )
    # A cc-wait call that returns 75 has spent its slice, and one that returns 0 or 1 took none.
    inner = pipeline.run

    def ticking(argv, stdin):
        out = inner(argv, stdin)
        if out.returncode == 75:
            clock.now += CI_SLICE_S
        return out

    pipeline.run = ticking
    return pipeline, run, clock


def _record(tmp_path):
    (path,) = (tmp_path / "state").glob("1345-*.json")
    return json.loads(path.read_text())


def test_green_ci_ends_the_batch_done_and_says_so_on_the_pr(tmp_path):
    pipeline, run, _ = _server_batch(tmp_path, _clean_review())
    final = pipeline.run_all()
    assert final["result"] == f"Opened {PR}"
    assert [phase for _, _, phase in run.claude] == ["implement", "review"]
    (call,) = run.ci_calls
    assert call[-5:] == ["gh-ci", "--pr", "4000", "--repo", "DanielH2018/server"]
    assert "CI gate: passed." in run.comments[0]
    assert _record(tmp_path)["ci_gate"] == "passed"


def test_red_ci_resumes_the_agent_once_with_the_failed_log_and_ends_done_when_green(
    tmp_path,
):
    reports = [*_clean_review(), _report(f"Fixed the weight record. {PR}")]
    pipeline, run, _ = _server_batch(tmp_path, reports)
    run.ci = [FAILED, PASSED]
    run.rollup = [
        {"name": "lint", "conclusion": "SUCCESS", "detailsUrl": JOB_URL},
        {"name": "pytest", "conclusion": "FAILURE", "detailsUrl": JOB_URL},
        {"name": "gitguardian", "conclusion": "CANCELLED", "detailsUrl": ""},
    ]
    run.job_logs = {"777": "collected 9 items\nFAILED test_x - no weight recorded\n"}
    final = pipeline.run_all()
    assert final["result"] == f"Fixed the weight record. {PR}"
    argv, prompt, phase = run.claude[-1]
    assert phase == "ci-fix" and argv[-2:] == ["--resume", "sid-1"]
    assert "pytest" in prompt and "no weight recorded" in prompt
    assert "gitguardian" not in prompt and "lint" not in prompt
    assert [p for _, _, p in run.claude].count("ci-fix") == 1
    assert len(run.ci_calls) == 2
    assert "CI gate: passed after one resume." in run.comments[0]
    assert _record(tmp_path)["ci_resumed"] is True


def test_red_ci_after_the_one_resume_ends_needs_input_naming_the_checks(tmp_path):
    reports = [*_clean_review(), _report(f"Tried a fix. {PR}")]
    pipeline, run, _ = _server_batch(tmp_path, reports)
    run.ci = [FAILED]
    run.rollup = [{"name": "pytest", "conclusion": "FAILURE", "detailsUrl": JOB_URL}]
    final = pipeline.run_all()
    assert final["result"].startswith(
        "needs input: CI is red after one resume, so the PR needs a hand: pytest."
    )
    # `status` reads a PR URL in the text as `done`, ahead of a `needs input:` line.
    assert BLOCKER.search(final["result"]) and not PR_URL.search(final["result"])
    assert final["result"].endswith(PR.removeprefix("https://"))
    assert [p for _, _, p in run.claude].count("ci-fix") == 1
    assert "CI gate: failed after one resume: pytest." in run.comments[0]
    assert _record(tmp_path)["outcome"] == "needs-input"


def test_a_cancelled_run_is_waited_on_again_and_never_resumes_the_agent(tmp_path):
    pipeline, run, clock = _server_batch(tmp_path, _clean_review())
    run.ci = [CANCELLED, PASSED]
    final = pipeline.run_all()
    assert final["result"] == f"Opened {PR}"
    assert [phase for _, _, phase in run.claude] == ["implement", "review"]
    assert len(run.ci_calls) == 2
    assert clock.sleeps == [CI_RECHECK_S]


def test_ci_that_stays_cancelled_ends_needs_input_when_the_budget_runs_out(tmp_path):
    pipeline, run, _ = _server_batch(tmp_path, _clean_review())
    run.ci = [CANCELLED]
    final = pipeline.run_all()
    assert final["result"].startswith("needs input: CI gave no verdict within its wait")
    assert "WAIT: cancelled" in final["result"]
    assert BLOCKER.search(final["result"]) and not PR_URL.search(final["result"])
    assert [phase for _, _, phase in run.claude] == ["implement", "review"]
    assert len(run.ci_calls) == CI_WAIT_S // CI_RECHECK_S
    assert _record(tmp_path)["ci_gate"].startswith("no verdict: WAIT: cancelled")


def test_a_wait_that_uses_its_slice_is_re_run_without_a_pause(tmp_path):
    pipeline, run, clock = _server_batch(tmp_path, _clean_review())
    timeout = (75, "WAIT: timeout still running after 300s; re-run: cc-wait ...\n")
    run.ci = [timeout, timeout, PASSED]
    assert pipeline.run_all()["result"] == f"Opened {PR}"
    assert len(run.ci_calls) == 3 and clock.sleeps == []


def test_a_daniel_box_batch_with_red_ci_is_resumed_once_then_lands(tmp_path):
    reports = [*_clean_review(), _report(f"Fixed the weight record. {PR}")]
    pipeline, run, _ = _server_batch(tmp_path, reports, host="daniel-box")
    run.ci = [FAILED, PASSED]
    run.rollup = [{"name": "pytest", "conclusion": "FAILURE", "detailsUrl": JOB_URL}]
    run.job_logs = {"777": "FAILED test_x - no weight recorded\n"}
    final = pipeline.run_all()
    assert final["result"] == f"VERDICT: settled (deployed)\n{PR}"
    assert [p for _, _, p in run.claude].count("ci-fix") == 1
    assert len(run.ci_calls) == 2 and len(run.lands) == 2
    assert "CI gate: passed after one resume." in run.comments[0]
    record = _record(tmp_path)
    assert record["ci_resumed"] is True and record["outcome"] == "pr"


def test_a_daniel_box_batch_with_green_ci_lands_without_a_resume(tmp_path):
    pipeline, run, _ = _server_batch(tmp_path, _clean_review(), host="daniel-box")
    final = pipeline.run_all()
    assert final["result"] == f"VERDICT: settled (deployed)\n{PR}"
    assert [phase for _, _, phase in run.claude] == ["implement", "review"]
    assert len(run.ci_calls) == 1 and len(run.lands) == 2
    assert "CI gate: passed." in run.comments[0]
    assert _record(tmp_path)["ci_gate"] == "passed"


def test_a_daniel_box_batch_still_red_after_its_resume_ends_needs_input_unlanded(
    tmp_path,
):
    reports = [*_clean_review(), _report(f"Tried a fix. {PR}")]
    pipeline, run, _ = _server_batch(tmp_path, reports, host="daniel-box")
    run.ci = [FAILED]
    run.rollup = [{"name": "pytest", "conclusion": "FAILURE", "detailsUrl": JOB_URL}]
    final = pipeline.run_all()
    assert final["result"].startswith(
        "needs input: CI is red after one resume, so the PR needs a hand: pytest."
    )
    assert [p for _, _, p in run.claude].count("ci-fix") == 1
    assert run.lands == []
    assert _record(tmp_path)["outcome"] == "needs-input"


def test_another_repos_batch_on_daniel_server_waits_for_no_ci(tmp_path):
    clock = Clock()
    other = Target("DanielH2018/dotfiles", "/home/ubuntu/dotfiles", "origin/main")
    pipeline, run = _pipeline(
        tmp_path, _clean_review(), host="daniel-server", target=other, clock=clock
    )
    run.ci = [FAILED]
    assert pipeline.run_all()["result"] == f"Opened {PR}"
    assert run.ci_calls == []
