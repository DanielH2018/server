"""The review pipeline's CI wait, on both hosts (#4737, #5498).

The fake runner answers `cc-wait gh-ci` with the scripted `run.ci` exits and output, the PR's
`statusCheckRollup` with `run.rollup`, and a failed job's log with `run.job_logs`.

Run: uv run pytest scripts/dev/tests/test_fanout_review_ci.py
"""

import json
import subprocess

from _review_fakes import PR, _finding, _pipeline, _report
from fanout_lib.launch import REVIEW_RUNTIME_MAX_S, RUNTIME_MAX_S
from fanout_lib.review.review_ci import (
    CI_RECHECK_S,
    CI_SLICE_S,
    CI_WAIT_S,
    MERGEABLE_RECHECK_S,
    READY_POLL_S,
    READY_WAIT_S,
)
from fanout_lib.review.review import LAND_MARGIN_S
from fanout_lib.review.review_land import WAIT_MARGIN_S
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


def test_ci_that_runs_twenty_minutes_on_a_loaded_host_still_ends_done(tmp_path):
    # Runs took 1089-1404s on 2026-10-29 (#6195). Four 300s slices exceed a 15-minute budget.
    pipeline, run, clock = _server_batch(tmp_path, _clean_review())
    timeout = (75, "WAIT: timeout still running after 300s; re-run: cc-wait ...\n")
    run.ci = [timeout] * 4 + [PASSED]
    assert pipeline.run_all()["result"] == f"Opened {PR}"
    assert len(run.ci_calls) == 5 and clock.sleeps == []


def test_ci_still_running_at_the_budget_ends_needs_input_naming_the_time_waited(
    tmp_path,
):
    pipeline, run, _ = _server_batch(tmp_path, _clean_review())
    run.ci = [(75, "WAIT: timeout still running after 300s; re-run: cc-wait ...\n")]
    final = pipeline.run_all()
    assert "within its wait of 35 min (WAIT: timeout still running" in final["result"]
    assert len(run.ci_calls) == CI_WAIT_S // CI_SLICE_S
    assert "(waited 35 min)" in _record(tmp_path)["ci_gate"]


def test_two_full_ci_waits_fit_in_a_review_batchs_run_time_and_still_leave_room_to_land():
    # The implementer can use RUNTIME_MAX_S; the CI waits and the landing share what is left.
    slack = REVIEW_RUNTIME_MAX_S - RUNTIME_MAX_S
    assert 2 * CI_WAIT_S + WAIT_MARGIN_S <= slack
    assert 2 * CI_WAIT_S + LAND_MARGIN_S <= slack


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


def test_a_draft_pr_is_marked_ready_and_the_ci_wait_starts_after_the_ready_run(
    tmp_path,
):
    """The draft run's skipped checks would pass cc-wait, so the wait starts on a new run."""
    pipeline, run, clock = _server_batch(tmp_path, _clean_review())
    run.draft = True
    run.ci_runs = [[1], [1], [1, 2]]
    assert pipeline.run_all()["result"] == f"Opened {PR}"
    (ready,) = run.readied
    assert ready[3:] == ["4000", "-R", "DanielH2018/server"]
    assert clock.sleeps == [READY_POLL_S]
    assert run.log == [
        ("runs", [1]),
        ("ready",),
        ("runs", [1]),
        ("runs", [1, 2]),
        ("gh-ci",),
    ]


def test_a_ready_pr_whose_ci_run_never_starts_ends_needs_input_without_a_wait(
    tmp_path,
):
    pipeline, run, clock = _server_batch(tmp_path, _clean_review(), host="daniel-box")
    run.draft = True
    final = pipeline.run_all()
    assert final["result"].startswith(
        f"needs input: no CI run started on PR #4000's head bbb within {READY_WAIT_S}s of "
        "marking it ready; the PR is back to a draft."
    )
    assert run.ci_calls == [] and run.lands == []
    assert run.log[1] == ("ready",) and run.log[-1] == ("undo",)
    assert sum(clock.sleeps) == READY_WAIT_S


def test_a_draft_held_for_its_findings_is_still_marked_ready(tmp_path):
    """A PR left a draft gets no CI and nothing reports it, so a held batch readies it too."""
    finding = _finding("Drops the lock", severity="critical", confidence=0.95)
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [finding]}),
        _report(f"Could not fix it. {PR}"),
        _report(structured={"summary": "", "findings": [finding]}),
    ]
    pipeline, run, _ = _server_batch(tmp_path, reports)
    run.draft = True
    run.ci_runs = [[1], [1, 2]]
    final = pipeline.run_all()
    assert BLOCKER.search(final["result"])
    assert len(run.readied) == 1 and run.ci_calls == []


def _mergeable_batch(tmp_path, answers):
    """A draft whose ready run never starts, with `gh pr view --json mergeable` scripted."""
    pipeline, run, clock = _server_batch(tmp_path, _clean_review())
    run.draft = True
    inner, reads = pipeline.run, []

    def run_with_mergeable(argv, stdin):
        if argv[:3] == ["gh", "pr", "view"] and argv[-1] == "mergeable":
            reads.append(argv)
            state = answers.pop(0) if len(answers) > 1 else answers[0]
            body = json.dumps({"mergeable": state})
            return subprocess.CompletedProcess(argv, 0, body, "")
        return inner(argv, stdin)

    pipeline.run = run_with_mergeable
    return pipeline, run, clock, reads


def test_a_conflicting_pr_that_starts_no_ci_is_blamed_on_the_conflict_not_actions(
    tmp_path,
):
    """GitHub runs no `pull_request` workflow on a CONFLICTING PR (#5629)."""
    pipeline, run, _, _ = _mergeable_batch(tmp_path, ["CONFLICTING"])
    final = pipeline.run_all()["result"]
    assert final.startswith("needs input: PR #4000 conflicts with master")
    assert "Merge or rebase master into the branch and push" in final
    assert "gh pr ready" not in final and "Actions" not in final
    # The PR stays ready: it cannot merge while it conflicts, and the push starts CI.
    assert ("undo",) not in run.log and run.ci_calls == []


def test_a_mergeable_pr_that_starts_no_ci_keeps_the_actions_message(tmp_path):
    pipeline, run, _, _ = _mergeable_batch(tmp_path, ["MERGEABLE"])
    final = pipeline.run_all()["result"]
    assert "Run `gh pr ready 4000` once Actions is up" in final
    assert "conflicts" not in final and run.log[-1] == ("undo",)


def test_an_unknown_mergeable_state_is_read_again_before_the_message_is_chosen(
    tmp_path,
):
    pipeline, _, clock, reads = _mergeable_batch(tmp_path, ["UNKNOWN", "CONFLICTING"])
    assert "conflicts with master" in pipeline.run_all()["result"]
    assert len(reads) == 2 and clock.sleeps[-1] == MERGEABLE_RECHECK_S
