"""The second fix round a medium-or-worse leftover earns, and the cases that keep it to one (#7687).

12 of 13 `needs-input` batches with findings left after one fix round held a real defect, so a
finding of medium severity or worse that survives the first delta review gets one more fix and
delta review. Nothing else does.

Run: uv run pytest scripts/dev/tests/test_fanout_review_second_round.py
"""

import json

from _review_fakes import PR, _finding, _pipeline, _report

HEADS = ("aaa", "bbb", "ccc")


def _record(tmp_path):
    (path,) = [f for f in (tmp_path / "state").iterdir() if f.suffix == ".json"]
    return json.loads(path.read_text())


def _phases(run):
    return [phase for _, _, phase in run.claude]


def _findings(*found):
    return _report(structured={"summary": "", "findings": list(found)})


def _round_one(*left):
    """Reports up to the first delta review, which leaves `left`."""
    return [
        _report(f"Opened {PR}"),
        _findings(_finding("off by one", confidence=0.9)),
        _report(f"Fixed. {PR}"),
        _findings(*left),
    ]


def test_a_medium_leftover_gets_a_second_fix_and_delta_review_that_can_clear_it(
    tmp_path,
):
    survivor = _finding("still off by one", severity="medium", confidence=0.7)
    reports = _round_one(survivor) + [
        _report(f"Fixed again. {PR}"),
        _findings(),
    ]
    pipeline, run = _pipeline(tmp_path, reports, heads=HEADS)
    final = pipeline.run_all()
    assert _phases(run) == ["implement", "review", "fix", "review", "fix", "review"]
    assert final["result"] == f"VERDICT: settled (deployed)\n{PR}"
    # The second delta review reads only the second fix, inside the whole change.
    assert "The fix round: `git diff bbb..ccc`" in run.claude[5][1]
    assert "git diff base0...ccc" in run.claude[5][1]
    record = _record(tmp_path)
    assert record["fix_rounds"] == 2
    assert record["remaining"] == []
    assert record["head"] == "ccc"
    assert "A second fix round ran" in run.comments[0]


def test_the_second_fix_is_asked_only_for_the_medium_leftover(tmp_path):
    survivor = _finding("still off by one", severity="medium", confidence=0.7)
    vacuous = _finding("assertion cannot fail", severity="low", confidence=0.9)
    vacuous.update(category="test", subkind="vacuous")
    reports = _round_one(survivor, vacuous) + [
        _report(f"Fixed again. {PR}"),
        _findings(),
    ]
    pipeline, run = _pipeline(tmp_path, reports, heads=HEADS)
    pipeline.run_all()
    assert "still off by one" in run.claude[4][1]
    assert "assertion cannot fail" not in run.claude[4][1]
    assert run.claude[4][0][-2:] == ["--resume", "sid-1"]


def test_a_leftover_after_the_second_round_holds_the_pr_and_no_third_round_runs(
    tmp_path,
):
    survivor = _finding("still off by one", severity="medium", confidence=0.8)
    reports = _round_one(survivor) + [
        _report(f"Fixed again. {PR}"),
        _findings(survivor),
    ]
    pipeline, run = _pipeline(tmp_path, reports, heads=HEADS)
    final = pipeline.run_all()
    assert _phases(run).count("fix") == 2
    assert final["result"].startswith("needs input:")
    assert "still off by one (scripts/x.py)" in final["result"]
    assert run.lands == []
    assert _record(tmp_path)["fix_rounds"] == 2


def test_a_vacuous_test_leftover_alone_does_not_earn_a_second_round(tmp_path):
    vacuous = _finding("assertion cannot fail", severity="low", confidence=0.9)
    vacuous.update(category="test", subkind="vacuous")
    reports = _round_one(vacuous) + [_report(f"Filed #9. {PR}")]
    pipeline, run = _pipeline(tmp_path, reports, heads=HEADS)
    pipeline.run_all()
    assert _phases(run).count("fix") == 1
    assert _record(tmp_path)["fix_rounds"] == 1


def test_a_failed_delta_review_does_not_earn_a_second_round(tmp_path):
    reports = [
        _report(f"Opened {PR}"),
        _findings(_finding("off by one", confidence=0.9)),
        _report(f"Fixed. {PR}"),
        _report(is_error=True),
    ]
    pipeline, run = _pipeline(tmp_path, reports, heads=HEADS)
    pipeline.run_all()
    assert _phases(run).count("fix") == 1
    assert _record(tmp_path)["fix_rounds"] == 1


def test_a_batch_with_no_actionable_finding_runs_no_fix_round(tmp_path):
    reports = [_report(f"Opened {PR}"), _findings(_finding("nit", severity="low"))]
    pipeline, _run = _pipeline(tmp_path, reports, host="daniel-server", heads=HEADS)
    pipeline.run_all()
    assert _record(tmp_path)["fix_rounds"] == 0
