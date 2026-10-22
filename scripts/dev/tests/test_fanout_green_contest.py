"""A red test with a false premise, contested by the implementer (#5400, #4874).

In #4874 the red author assumed `claude-rc.service` ran on every host, and the correct fix
failed the red test. The implementer could not contest it, and the green gate's hold did not
say that the reviewer had found the fix correct. These run the pipeline against scripted gate
verdicts, as `test_fanout_review_red_green.py` does.

Run: uv run pytest scripts/dev/tests/test_fanout_green_contest.py
"""

from _review_fakes import PR, _finding, _pipeline, _report
from fanout_lib.review.red_gate import Gate, Gates, green_finding
from fanout_lib.review.green_contest import contested
from fanout_lib.review.review_prompts import ci_fix_prompt

RED_NODE = "t.py::test_a_cgroup_missing_on_one_host_is_flagged_and_named"
UNMET = f"pytest exited 1; not passing: {RED_NODE} (FAILED)"
RED_GATE = Gate(files=["t.py"], nodes=[RED_NODE])
# The implementer's answer to the green finding: the red test's premise, contradicted at a line.
CONTEST = (
    f"{RED_NODE} is wrong: it assumes claude-rc.service runs on every host, but "
    "ansible/inventory/host_vars/daniel-box.yml:12 is the only host that enables it. "
    f"{PR}"
)


def _run(tmp_path, review, fix_reply, delta):
    """One red batch whose green gate fails the same red node before and after the fix."""
    tmp_path.mkdir()
    reports = [
        _report(structured={"behaviours": [{"behaviour": "b", "tests": [RED_NODE]}]}),
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": review}),
        _report(fix_reply),
        _report(structured={"summary": "", "findings": delta}),
        # Spare answers, for a phase the fix adds.
        *[_report(structured={"summary": "", "findings": []})] * 3,
    ]
    greens = [UNMET, UNMET]
    gates = Gates(
        red=lambda run, wt, base, head: RED_GATE,
        green=lambda run, wt, sha, gate: greens.pop(0),
    )
    pipeline, run = _pipeline(
        tmp_path, reports, heads=("base", "red1", "aaa", "bbb"), gates=gates
    )
    return pipeline.run_all(), run


def test_a_green_gate_hold_says_whether_the_reviewer_judged_the_fix_correct(tmp_path):
    """Two holds that differ only in what the reviewers found must not read the same."""
    reply = f"Fixed. {PR}"
    clean, _ = _run(tmp_path / "clean", [], reply, [])
    wrong = [_finding("the fix misses daniel-server")]
    faulted, _ = _run(tmp_path / "faulted", wrong, reply, wrong)
    assert clean["result"] != faulted["result"]


def test_a_red_test_contested_with_a_cited_fact_the_fresh_reviewer_upholds_lands(
    tmp_path,
):
    """The delta reviewer reads the contest and does not report the green finding again."""
    upheld, run = _run(tmp_path / "upheld", [], CONTEST, [])
    assert not upheld["result"].startswith("needs input: the PR fails the green gate")
    assert run.lands
    # A contest the fresh reviewer rejects leaves the PR held.
    rejected = [green_finding(UNMET, "red1", RED_GATE)]
    held, run = _run(tmp_path / "rejected", [], CONTEST, rejected)
    assert held["result"].startswith("needs input: the PR fails the green gate")
    assert run.lands == []
    # A contest that cites no line is not one, however quiet the review.
    uncited, run = _run(tmp_path / "uncited", [], f"{RED_NODE} is wrong. {PR}", [])
    assert uncited["result"].startswith("needs input: the PR fails the green gate")
    assert run.lands == []


def test_a_contest_of_a_fix_that_commits_nothing_still_gets_its_fresh_reader(tmp_path):
    """A correct fix held by a wrong red test has nothing to commit in its fix round."""
    reports = [
        _report(structured={"behaviours": [{"behaviour": "b", "tests": [RED_NODE]}]}),
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
        _report(CONTEST),
        _report(structured={"summary": "", "findings": []}),
        *[_report(structured={"summary": "", "findings": []})] * 3,
    ]
    gates = Gates(red=lambda *_: RED_GATE, green=lambda *_: UNMET)
    pipeline, run = _pipeline(
        tmp_path, reports, heads=("base", "red1", "aaa", "aaa"), gates=gates
    )
    pipeline.run_all()
    # The second `review` is the delta review, which reads the contest.
    phases = [phase for _, _, phase in run.claude]
    assert phases == ["red", "implement", "review", "fix", "review"]
    assert run.lands
    assert pipeline.record.green_contest == f"upheld: {RED_NODE}"


def test_a_rejected_contest_is_named_in_the_hold(tmp_path):
    rejected = [green_finding(UNMET, "red1", RED_GATE)]
    held, _ = _run(tmp_path / "rejected", [], CONTEST, rejected)
    assert f"rejected the implementer's contest of {RED_NODE}" in held["result"]


def test_only_a_red_test_left_failing_can_be_contested():
    """A refusal about what the fix touched says nothing about whether a test is wrong."""
    assert contested(UNMET, CONTEST, RED_GATE) == [RED_NODE]
    lock = (
        "the fix changed what the red tests stand on: t.py changes or removes `def f`"
    )
    assert contested(lock, CONTEST, RED_GATE) == []
    # A contest must name every red node left failing.
    two = Gate(files=["t.py"], nodes=[RED_NODE, "t.py::test_other"])
    both = f"{UNMET}, t.py::test_other (FAILED)"
    assert contested(both, CONTEST, two) == []


def test_the_green_gate_gets_the_names_the_issue_spells_in_backticks(tmp_path):
    seen = []
    gates = Gates(
        red=lambda *_: RED_GATE,
        green=lambda run, wt, sha, gate: seen.append(gate.subjects) or "",
    )
    reports = [
        _report(structured={"behaviours": [{"behaviour": "b", "tests": [RED_NODE]}]}),
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, _ = _pipeline(
        tmp_path, reports, heads=("base", "red1", "aaa"), gates=gates
    )
    pipeline.brief = pipeline.brief.replace(
        "body one", "`_restarts_privately()` counts a comment, unlike `deploy.sh`"
    )
    pipeline.run_all()
    assert seen == [{"_restarts_privately"}]
    # The scripted gate every batch shares is left as it was.
    assert RED_GATE.subjects == set()


def test_the_ci_round_after_an_upheld_contest_may_correct_the_red_test():
    prompt = ci_fix_prompt(PR, "WAIT: failed", [], f"upheld: {RED_NODE}")
    assert f"upheld your contest of these red tests: {RED_NODE}" in prompt
    assert "upheld your contest" not in ci_fix_prompt(PR, "WAIT: failed", [])
