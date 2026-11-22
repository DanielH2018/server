"""The review pipeline's CI wait on daniel-server, and its one resume on both hosts.

The fake runner answers `cc-wait gh-ci` with the scripted `run.ci` exits and output, the PR's
`statusCheckRollup` with `run.rollup`, and a failed job's log with `run.job_logs`.

Run: uv run pytest scripts/dev/tests/test_fanout_review_ci.py
"""

import json
import subprocess

import pytest

from _review_fakes import PR, _finding, _pipeline, _report
from dev.fanout_lib.launch import REVIEW_RUNTIME_MAX_S, RUNTIME_MAX_S
from dev.fanout_lib.review.review_ci import (
    CI_RECHECK_S,
    CI_SLICE_S,
    CI_WAIT_MAX_S,
    CI_WAIT_S,
    MERGEABLE_RECHECK_S,
    READY_POLL_S,
    READY_WAIT_S,
)
from dev.fanout_lib.review.review_land import LAND_MARGIN_S, WAIT_MARGIN_S
from dev.fanout_lib.status import BLOCKER, PR_URL
from dev.fanout_lib.target import Target

PASSED = (0, "WAIT: passed abcd1234: 20 check runs passed\n")
FAILED = (1, "WAIT: failed abcd1234: 1 failed: pytest\n")
CANCELLED = (1, "WAIT: cancelled abcd1234: 1 cancelled: pytest\n")
JOB_URL = "https://github.com/DanielH2018/server/actions/runs/9/job/777"
PR_CI_RED = "VERDICT: pr-ci-red (PR #4000 cannot merge: its own CI is red)"


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


def _server_batch(
    tmp_path, reports, clock=None, host="daniel-server", heads=("aaa", "bbb")
):
    clock = clock or Clock()
    pipeline, run = _pipeline(
        tmp_path, reports, host=host, heads=heads, clock=clock, sleep=clock.sleep
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
    record = _record(tmp_path)
    assert record["outcome"] == "needs-input"
    assert record["blocker"] == (
        "needs input: CI is red after one resume, so the PR needs a hand: pytest."
    )


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
    # Four 300s slices exceed a 15-minute budget.
    pipeline, run, clock = _server_batch(tmp_path, _clean_review())
    timeout = (75, "WAIT: timeout still running after 300s; re-run: cc-wait ...\n")
    run.ci = [timeout] * 4 + [PASSED]
    assert pipeline.run_all()["result"] == f"Opened {PR}"
    assert len(run.ci_calls) == 5 and clock.sleeps == []


def test_ci_still_running_past_the_budget_is_waited_on_and_ends_done(tmp_path):
    # A run that passes at 48.5 minutes lands after the 35-minute budget.
    pipeline, run, _ = _server_batch(tmp_path, _clean_review())
    running = (75, "WAIT: timeout still running after 300s; re-run: cc-wait ...\n")
    run.ci = [running] * 10 + [PASSED]
    assert pipeline.run_all()["result"] == f"Opened {PR}"


def test_ci_still_running_at_the_cap_ends_needs_input_naming_the_time_waited(
    tmp_path,
):
    pipeline, run, _ = _server_batch(tmp_path, _clean_review())
    run.ci = [(75, "WAIT: timeout still running after 300s; re-run: cc-wait ...\n")]
    final = pipeline.run_all()
    assert "within its wait of 60 min (WAIT: timeout still running" in final["result"]
    assert len(run.ci_calls) == CI_WAIT_MAX_S // CI_SLICE_S
    assert "(waited 60 min)" in _record(tmp_path)["ci_gate"]


def test_a_head_with_no_check_runs_still_stops_at_the_budget(tmp_path):
    pipeline, run, _ = _server_batch(tmp_path, _clean_review())
    run.ci = [(75, "WAIT: timeout still pending after 300s; re-run: cc-wait ...\n")]
    final = pipeline.run_all()
    assert "within its wait of 35 min (WAIT: timeout still pending" in final["result"]
    assert len(run.ci_calls) == CI_WAIT_S // CI_SLICE_S


def test_two_full_ci_waits_fit_in_a_review_batchs_run_time_and_still_leave_room_to_land():
    # The implementer can use RUNTIME_MAX_S; the CI waits and the landing share what is left.
    slack = REVIEW_RUNTIME_MAX_S - RUNTIME_MAX_S
    assert 2 * CI_WAIT_MAX_S + WAIT_MARGIN_S <= slack
    assert 2 * CI_WAIT_MAX_S + LAND_MARGIN_S <= slack


def _flaky_batch(tmp_path, report="Nothing to fix; the failure looks flaky."):
    reports = [*_clean_review(), _report(f"{report} {PR}")]
    pipeline, run, clock = _server_batch(tmp_path, reports)
    run.ci = [FAILED, PASSED]
    run.rollup = [{"name": "pytest", "conclusion": "FAILURE", "detailsUrl": JOB_URL}]
    run.failed_runs = [[8, 1], [9, 1]]
    return pipeline, run, clock


def test_a_resume_that_pushes_nothing_re_runs_the_failed_jobs_before_the_wait(tmp_path):
    pipeline, run, _ = _flaky_batch(tmp_path)
    assert pipeline.run_all()["result"].endswith(PR)
    (rerun,) = run.reruns
    assert rerun[3:5] == ["9", "--failed"]
    assert run.log[-1] == ("gh-ci",)
    record = _record(tmp_path)
    assert record["ci_rerun"] is True and record["outcome"] == "pr"
    assert (
        record["ci_gate"] == "passed after one resume and a re-run of the failed jobs"
    )


def test_a_flake_that_passes_on_re_run_keeps_its_job_name_in_the_record(tmp_path):
    pipeline, run, _ = _flaky_batch(tmp_path)
    run.rollup = [
        {
            "name": "pytest (shard 3)",
            "conclusion": "FAILURE",
            "detailsUrl": JOB_URL,
        }
    ]
    pipeline.run_all()
    record = _record(tmp_path)
    assert record["ci_gate"].startswith("passed")
    assert record["ci_rerun_jobs"] == ["pytest (shard 3)"]


def test_a_resume_that_pushes_a_fix_re_runs_nothing(tmp_path):
    pipeline, run, _ = _flaky_batch(tmp_path, "Fixed the weight record.")
    run.fix_pushes = True
    pipeline.run_all()
    assert run.reruns == []
    assert _record(tmp_path)["ci_gate"] == "passed after one resume"


def test_a_re_run_that_never_registers_waits_on_the_old_failure(tmp_path):
    pipeline, run, clock = _flaky_batch(tmp_path)
    run.ci = [FAILED]
    run.attempts = [1]
    final = pipeline.run_all()
    assert len(run.reruns) == 1 and READY_POLL_S in clock.sleeps
    assert _record(tmp_path)["ci_rerun"] is False
    assert final["result"].startswith("needs input: CI is red after one resume,")


def _red_landing(tmp_path, verdicts):
    """A daniel-box batch whose landings end on `verdicts`, the first on a red `pytest` job."""
    reports = [*_clean_review(), _report(f"Fixed the weight record. {PR}")]
    pipeline, run, _ = _server_batch(tmp_path, reports, host="daniel-box")
    run.verdicts = verdicts
    run.rollup = [{"name": "pytest", "conclusion": "FAILURE", "detailsUrl": JOB_URL}]
    run.job_logs = {"777": "FAILED test_x - no weight recorded\n"}
    run.fix_pushes = True
    return pipeline, run


def test_a_daniel_box_landing_that_ends_pr_ci_red_resumes_the_agent_once_then_lands(
    tmp_path,
):
    pipeline, run = _red_landing(tmp_path, [PR_CI_RED])
    final = pipeline.run_all()
    assert final["result"] == f"VERDICT: settled (deployed)\n{PR}"
    argv, prompt, phase = run.claude[-1]
    assert phase == "ci-fix" and argv[-2:] == ["--resume", "sid-1"]
    assert PR_CI_RED in prompt and "no weight recorded" in prompt
    assert [c[0].endswith("land.sh") for c in run.lands] == [True, False, True, False]
    record = _record(tmp_path)
    assert record["ci_gate"] == "passed after one resume"
    assert record["outcome"] == "pr" and record["verdict"] == run.verdict


def test_a_daniel_box_landing_still_pr_ci_red_after_its_resume_ends_needs_input(
    tmp_path,
):
    pipeline, run = _red_landing(tmp_path, [PR_CI_RED, PR_CI_RED])
    final = pipeline.run_all()
    assert final["result"].startswith(
        "needs input: CI is red after one resume, so the PR needs a hand: pytest."
    )
    assert [p for _, _, p in run.claude].count("ci-fix") == 1
    assert len(run.lands) == 4
    record = _record(tmp_path)
    assert record["outcome"] == "needs-input" and record["verdict"] == PR_CI_RED
    assert record["ci_gate"] == "failed after one resume: pytest"


def test_a_daniel_box_resume_that_pushes_nothing_re_runs_the_failed_jobs_then_lands(
    tmp_path,
):
    pipeline, run = _red_landing(tmp_path, [PR_CI_RED])
    run.fix_pushes = False
    run.failed_runs = [[9, 1]]
    assert pipeline.run_all()["result"] == f"VERDICT: settled (deployed)\n{PR}"
    (rerun,) = run.reruns
    assert rerun[3:5] == ["9", "--failed"]
    gate = _record(tmp_path)["ci_gate"]
    assert gate == "passed after one resume and a re-run of the failed jobs"


def test_a_second_landing_that_ends_before_the_merge_records_no_ci_verdict(tmp_path):
    stopped = "VERDICT: merge-timeout (PR #4000 did not merge)"
    pipeline, _ = _red_landing(tmp_path, [PR_CI_RED, stopped])
    final = pipeline.run_all()
    assert final["result"].startswith("needs input: the landing of")
    assert _record(tmp_path)["ci_gate"] == f"no verdict after one resume: {stopped}"


def test_a_ci_fix_that_leaves_too_little_run_time_is_not_landed_again(tmp_path):
    pipeline, run = _red_landing(tmp_path, [PR_CI_RED])
    clock, inner = pipeline.clock, pipeline.run

    def slow_fix(argv, stdin):
        out = inner(argv, stdin)
        if run.claude and run.claude[-1][2] == "ci-fix":
            clock.now = REVIEW_RUNTIME_MAX_S - LAND_MARGIN_S + 1
        return out

    pipeline.run = slow_fix
    final = pipeline.run_all()
    assert final["result"].startswith("needs input: the review phases left too little")
    assert [c[0].endswith("land.sh") for c in run.lands] == [True, False]


LANDABLE = {
    "headRefName": "worktree-fanout-1345",
    "baseRefName": "master",
    "isCrossRepository": False,
    "author": {"login": "DanielH2018"},
    "files": [{"path": "docs/landing.md"}],
    "body": "",
}
OWN_SURFACE = {**LANDABLE, "files": [{"path": "scripts/deploy_tools/land.sh"}]}


def test_a_daniel_server_pr_the_server_lander_would_land_lands_as_a_draft(tmp_path):
    pipeline, run, _ = _server_batch(tmp_path, _clean_review(), heads=("bbb",))
    run.draft = True
    run.pr_fields = LANDABLE
    pipeline.run_all()
    assert run.readied == [] and run.ci_calls == []
    assert run.lands[0][0].endswith("land.sh")
    assert _record(tmp_path)["land_hold"] == ""


def test_a_daniel_server_pr_on_the_landers_surface_stays_a_draft_with_no_ci(tmp_path):
    """The person who lands it gives it its one run: `land.sh` marks it ready at its turn."""
    pipeline, run, _ = _server_batch(tmp_path, _clean_review(), heads=("bbb",))
    run.draft = True
    run.pr_fields = OWN_SURFACE
    assert pipeline.run_all()["result"] == f"Opened {PR}"
    assert run.readied == [] and run.ci_calls == [] and run.lands == []
    hold = "it changes the lander's own surface: scripts/deploy_tools/land.sh"
    record = _record(tmp_path)
    assert record["land_hold"] == hold and record["person_lands"] is True
    assert f"Not landed by the pipeline: {hold}." in run.comments[0]
    assert "It stays a draft" in run.comments[0]


def test_a_ci_fix_that_reaches_the_landers_surface_is_left_for_a_person(tmp_path):
    reports = [*_clean_review(), _report(f"Fixed the lander. {PR}")]
    pipeline, run, _ = _server_batch(tmp_path, reports, heads=("bbb",))
    run.pr_fields, run.pr_fields_fixed = LANDABLE, OWN_SURFACE
    run.fix_pushes = True
    run.verdicts = [PR_CI_RED]
    run.rollup = [{"name": "pytest", "conclusion": "FAILURE", "detailsUrl": JOB_URL}]
    final = pipeline.run_all()
    assert final["result"].startswith(
        "needs input: the CI fix left the PR for a person: it changes the lander's own"
    )
    assert [c[0].endswith("land.sh") for c in run.lands].count(True) == 1


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
    pipeline, run, clock = _server_batch(tmp_path, _clean_review())
    run.draft = True
    final = pipeline.run_all()
    assert final["result"].startswith(
        f"needs input: no CI run started on PR #4000's head bbb within {READY_WAIT_S}s of "
        "marking it ready; the PR is back to a draft."
    )
    assert run.ci_calls == []
    assert run.log[1] == ("ready",) and run.log[-1] == ("undo",)
    assert sum(clock.sleeps) == READY_WAIT_S


@pytest.mark.parametrize("host", ["daniel-server", "daniel-box"])
def test_a_draft_held_for_its_findings_is_still_marked_ready(tmp_path, host):
    """A PR left a draft gets no CI and nothing reports it, so a held batch readies it too."""
    finding = _finding("Drops the lock", severity="critical", confidence=0.95)
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [finding]}),
        _report(f"Could not fix it. {PR}"),
        _report(structured={"summary": "", "findings": [finding]}),
        _report(f"Could not fix it either. {PR}"),
    ]
    pipeline, run, _ = _server_batch(tmp_path, reports, host=host)
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
    """GitHub runs no `pull_request` workflow on a CONFLICTING PR."""
    pipeline, run, _, _ = _mergeable_batch(tmp_path, ["CONFLICTING"])
    final = pipeline.run_all()["result"]
    assert final.startswith("needs input: PR #4000 conflicts with master")
    assert (
        "Run `fanout.py place resume <run-id> <batch>` once this unit has ended"
        in final
    )
    assert "Merge or rebase" not in final
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
