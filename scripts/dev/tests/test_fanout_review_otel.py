"""Each review phase's `claude -p` carries the batch and phase as OTEL resource attributes (#5673).

Run: uv run pytest scripts/dev/tests/test_fanout_review_otel.py
"""

from _review_fakes import PR, _finding, _pipeline, _report

from fanout_lib.review import processes


def _raw_claude_calls(tmp_path, reports, monkeypatch, inherited=None):
    if inherited is None:
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    else:
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", inherited)
    pipeline, run = _pipeline(tmp_path, reports)
    raw = []

    def recording(argv, stdin):
        raw.append(argv)
        return run(argv, stdin)

    pipeline.run = recording
    pipeline.run_all()
    return [argv for argv in raw if "claude" in argv]


def test_every_phase_names_its_batch_and_phase(tmp_path, monkeypatch):
    reports = [
        _report(f"Opened {PR}", cost=3.0),
        _report(structured={"summary": "", "findings": [_finding("off by one")]}),
        _report(f"Fixed it. {PR}", cost=4.0),
        _report(structured={"summary": "resolved", "findings": []}),
    ]
    calls = _raw_claude_calls(tmp_path, reports, monkeypatch)
    assert [argv[:2] for argv in calls] == [
        ["env", f"OTEL_RESOURCE_ATTRIBUTES=fanout.batch=1345,fanout.phase={phase}"]
        for phase in ("implement", "review", "fix", "review-delta")
    ]
    assert all(argv[2] == "claude" for argv in calls)


def test_an_attribute_the_unit_already_carries_is_kept(tmp_path, monkeypatch):
    calls = _raw_claude_calls(
        tmp_path,
        [_report("needs input: CI is red")],
        monkeypatch,
        inherited="process.owner=claude",
    )
    assert calls[0][1] == (
        "OTEL_RESOURCE_ATTRIBUTES=process.owner=claude,"
        "fanout.batch=1345,fanout.phase=implement"
    )


def test_the_assignment_has_one_shape_for_every_phase():
    assert processes.otel_attributes("5651-5654", "red") == (
        "OTEL_RESOURCE_ATTRIBUTES=fanout.batch=5651-5654,fanout.phase=red"
    )
