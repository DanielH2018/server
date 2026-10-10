"""The review pipeline's own landing (#3960): land.sh run once, the model resumed on need.

The fake runner answers `bash -l` with a scripted landing: land.sh writes a log ending on
`run.verdict`, and cc-wait exits with each of `run.waits` in turn.

Run: uv run pytest scripts/dev/tests/test_fanout_review_land.py
"""

import json

from _review_fakes import PR, _pipeline, _report


def _clean_review():
    return [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
        _report(f"Applied it. {PR}"),
    ]


def test_a_settled_landing_ends_the_batch_without_resuming_the_implementer(tmp_path):
    pipeline, run = _pipeline(tmp_path, _clean_review())
    final = pipeline.run_all()
    assert final["result"] == f"VERDICT: settled (deployed)\n{PR}"
    assert [phase for _, _, phase in run.claude] == ["implement", "review"]
    (record,) = (tmp_path / "state").glob("1345-*.json")
    assert json.loads(record.read_text())["verdict"] == "VERDICT: settled (deployed)"


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
        if "cc-wait" in argv:
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
