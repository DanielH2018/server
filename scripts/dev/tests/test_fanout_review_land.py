"""The review pipeline's own landing (#3960): land.sh run once, the model resumed on need.

The fake runner answers `bash -l` with a scripted landing: land.sh writes a log ending on
`run.verdict`, and cc-wait exits with each of `run.waits` in turn.

Run: uv run pytest scripts/dev/tests/test_fanout_review_land.py
"""

import json
import subprocess

from _review_fakes import PR, _pipeline, _report

EARLIER = "https://github.com/DanielH2018/server/pull/7295"
EARLIER_ROW = {"url": EARLIER, "number": 7295, "state": "MERGED"}


def _clean_review():
    return [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
        _report(f"Applied it. {PR}"),
    ]


def test_a_no_pr_batch_citing_its_branchs_merged_pr_starts_no_landing(tmp_path):
    """A relaunch reuses the branch name, so the earlier launch's merged PR is on it (#7402)."""
    pipeline, run = _pipeline(tmp_path, [_report(f"Already fixed by {EARLIER}.")])
    run.branch_prs = [EARLIER_ROW]
    final = pipeline.run_all()
    assert run.lands == []
    assert [phase for _, _, phase in run.claude] == ["implement"]
    assert final["result"] == f"Already fixed by {EARLIER}."
    (record,) = (tmp_path / "state").glob("1345-*.json")
    assert json.loads(record.read_text())["outcome"] == "no-pr"


def test_a_batchs_own_pr_lands_though_its_text_cites_an_earlier_merged_one_first(
    tmp_path,
):
    reports = _clean_review()
    reports[0] = _report(f"{EARLIER} missed a case, so I opened {PR}")
    pipeline, run = _pipeline(tmp_path, reports)
    run.branch_prs = [EARLIER_ROW, {"url": PR, "number": 4000, "state": "OPEN"}]
    pipeline.run_all()
    (argv, *_) = run.lands
    assert argv[argv.index("--pr") + 1] == "4000"


def test_a_settled_landing_ends_the_batch_without_resuming_the_implementer(tmp_path):
    pipeline, run = _pipeline(tmp_path, _clean_review())
    final = pipeline.run_all()
    assert final["result"] == f"VERDICT: settled (deployed)\n{PR}"
    assert [phase for _, _, phase in run.claude] == ["implement", "review"]
    (record,) = (tmp_path / "state").glob("1345-*.json")
    assert json.loads(record.read_text())["verdict"] == "VERDICT: settled (deployed)"


def test_the_ci_wait_runs_before_land_sh_starts(tmp_path):
    pipeline, _ = _pipeline(tmp_path, _clean_review())
    order = []
    inner = pipeline.run

    def recording(argv, stdin):
        if argv[0] == "bash":
            order.append("ci" if "gh-ci" in argv else "land")
        return inner(argv, stdin)

    pipeline.run = recording
    pipeline.run_all()
    assert order == ["ci", "land", "land"]


def test_a_needs_manual_apply_verdict_resumes_the_implementer_with_the_log(tmp_path):
    pipeline, run = _pipeline(tmp_path, _clean_review())
    run.verdict = "VERDICT: needs-manual-apply (run initial_setup.yml --tags dns)"
    final = pipeline.run_all()
    assert final["result"] == f"Applied it. {PR}"
    argv, prompt, phase = run.claude[-1]
    assert phase == "apply" and argv[-2:] == ["--resume", "sid-1"]
    assert run.verdict in prompt and "waiting for CI" in prompt
    # The review brief never carried the apply-owed section, so the prompt does.
    assert "MANUAL APPLY PENDING" in prompt


def test_a_wait_that_runs_out_is_resumed_without_starting_a_second_landing(tmp_path):
    pipeline, run = _pipeline(tmp_path, _clean_review())
    run.waits = [75, 75, 0]
    pipeline.run_all()
    assert [c[0].endswith("land.sh") for c in run.lands] == [True, False, False, False]


def test_a_landing_still_running_at_the_deadline_names_the_wait_to_resume(tmp_path):
    now = [0.0]
    pipeline, run = _pipeline(tmp_path, _clean_review(), clock=lambda: now[0])
    run.waits = [75]

    def first_wait_outlasts_the_unit(argv, stdin):
        if "cc-wait" in argv and "gh-ci" not in argv:
            now[0] = float(10**6)
        return run(argv, stdin)

    pipeline.run = first_wait_outlasts_the_unit
    final = pipeline.run_all()
    assert len(run.lands) == 2
    assert final["result"].startswith("needs input: the landing of")
    assert "cc-wait land 4000 --log-dir" in final["result"]


def test_a_land_sh_refusal_is_needs_input_with_its_output(tmp_path):
    pipeline, run = _pipeline(tmp_path, _clean_review())
    run.land_rc = 1
    final = pipeline.run_all()
    assert final["result"].startswith("needs input: land.sh printed no VERDICT")
    assert "refused: bad body" in final["result"]
    assert len(run.lands) == 1
    assert "apply" not in [phase for _, _, phase in run.claude]


def test_a_give_up_verdict_is_needs_input_and_resumes_nobody(tmp_path):
    pipeline, run = _pipeline(tmp_path, _clean_review())
    run.verdict = "VERDICT: ci-red (master CI failed on abc)"
    run.waits = [1]
    final = pipeline.run_all()
    assert final["result"].startswith("needs input: the landing of")
    assert run.verdict in final["result"]
    assert "apply" not in [phase for _, _, phase in run.claude]


def test_a_deferred_landing_is_finished_though_cc_wait_exits_4(tmp_path):
    pipeline, run = _pipeline(tmp_path, _clean_review())
    run.verdict = "VERDICT: deferred (the next tick applies it)"
    run.waits = [4]
    assert pipeline.run_all()["result"] == f"{run.verdict}\n{PR}"


def _checkout(tmp_path):
    return tmp_path / ".fanout" / "land-checkout-4000"


def test_land_sh_runs_from_a_fresh_checkout_of_the_base_not_the_batch_worktree(
    tmp_path,
):
    """The batch worktree is branched at launch, so its landing code goes stale (#7871)."""
    pipeline, run = _pipeline(tmp_path, _clean_review())
    pipeline.run_all()
    (argv, *_) = run.lands
    assert argv[0] == str(_checkout(tmp_path) / "scripts/deploy_tools/land.sh")
    assert ["fetch", "--quiet", "origin", "master"] in run.git
    added = ["worktree", "add", "--detach", str(_checkout(tmp_path)), "origin/master"]
    assert added in run.git
    # The landing ended, so nothing reads the checkout and it is removed.
    removed = ["worktree", "remove", "--force", str(_checkout(tmp_path))]
    assert run.git.index(removed, run.git.index(added)) > run.git.index(added)


def test_a_landing_still_running_keeps_its_checkout(tmp_path):
    now = [0.0]
    pipeline, run = _pipeline(tmp_path, _clean_review(), clock=lambda: now[0])
    run.waits = [75]

    def first_wait_outlasts_the_unit(argv, stdin):
        if "cc-wait" in argv and "gh-ci" not in argv:
            now[0] = float(10**6)
        return run(argv, stdin)

    pipeline.run = first_wait_outlasts_the_unit
    pipeline.run_all()
    removed = ["worktree", "remove", "--force", str(_checkout(tmp_path))]
    added = ["worktree", "add", "--detach", str(_checkout(tmp_path)), "origin/master"]
    assert removed not in run.git[run.git.index(added) :]


def test_a_checkout_that_cannot_be_made_is_needs_input_and_starts_no_landing(tmp_path):
    pipeline, run = _pipeline(tmp_path, _clean_review())
    inner = pipeline.run

    def add_fails(argv, stdin):
        if argv[-5:-2] == ["worktree", "add", "--detach"]:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: no space left")
        return inner(argv, stdin)

    pipeline.run = add_fails
    final = pipeline.run_all()
    assert run.lands == []
    assert final["result"].startswith("needs input: land.sh printed no VERDICT")
    assert "fatal: no space left" in final["result"]
