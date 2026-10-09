"""The review pipeline's red and green phases, against scripted gate verdicts.

The gates themselves run against real git and pytest in `test_fanout_red_gate.py`.

Run: uv run pytest scripts/dev/tests/test_fanout_review_red_green.py
"""

import json

import pytest

from _review_fakes import PR, _finding, _pipeline, _report
from fanout_lib.brief import ISSUES_HEADING
from fanout_lib.red_gate import Gate, Gates


def _red_report(behaviours=1):
    tested = [{"behaviour": f"b{i}", "tests": [f"t{i}"]} for i in range(behaviours)]
    return _report(structured={"behaviours": tested})


def _gates(red, green=()):
    """The red gate's verdict and each green gate run's, in order."""
    greens = list(green)
    return Gates(
        red=lambda run, wt, base, head: red,
        green=lambda run, wt, sha, gate: greens.pop(0),
    )


def test_a_red_green_batch_hands_the_implementer_the_red_commit_it_must_not_edit(
    tmp_path,
):
    gates = _gates(Gate(files=["t.py"], nodes=["t.py::test_a"]), green=[""])
    reports = [
        _red_report(behaviours=2),
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
        _report(f"{PR}\nVERDICT: settled"),
    ]
    pipeline, run = _pipeline(
        tmp_path, reports, heads=("base", "red1", "red1"), gates=gates
    )
    pipeline.anti_patterns = "ANTI-PATTERNS READ AT START"
    pipeline.run_all()

    assert [phase for _, _, phase in run.claude] == [
        "red",
        "implement",
        "review",
        "land",
    ]
    red_argv, red_stdin, _ = run.claude[0]
    assert "--resume" not in red_argv and "--json-schema" in red_argv
    assert "body one" in red_stdin and "land.sh" not in red_stdin
    assert "ANTI-PATTERNS READ AT START" in red_stdin
    brief = run.claude[1][1]
    assert brief.index("## Red tests") < brief.index(ISSUES_HEADING)
    assert "red1" in brief and "t.py::test_a" in brief
    assert pipeline.record.red_gate == "passed"
    assert (pipeline.record.red_behaviours, pipeline.record.red_tests) == (2, 1)
    assert "Red gate passed: 1 tests for 2 stated behaviours" in run.comments[0]


def test_a_refused_red_commit_is_reset_away_and_the_implementer_runs_without_it(
    tmp_path,
):
    gates = _gates(Gate("these new tests did not fail on the unchanged code: x"))
    reports = [
        _red_report(),
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, run = _pipeline(
        tmp_path,
        reports,
        host="daniel-server",
        heads=("base", "red1", "aaa"),
        gates=gates,
    )
    pipeline.run_all()

    assert ["reset", "--hard", "base"] in run.git
    assert "## Red tests" not in run.claude[1][1]
    assert pipeline.record.red_gate.startswith("these new tests did not fail")
    assert "Red gate refused the test author's commit" in run.comments[0]
    (record,) = [f for f in (tmp_path / "state").iterdir() if f.suffix == ".json"]
    assert json.loads(record.read_text())["red_gate"].startswith("these new tests")


@pytest.mark.parametrize(
    "red",
    [
        Gate(files=["t.py"], nodes=["t.py::test_a"]),
        Gate("the red commits add no test node"),
    ],
    ids=["passed", "refused"],
)
def test_a_red_batchs_implementer_loads_no_settings_file_the_red_author_could_write(
    tmp_path, red
):
    """The red author could leave an ignored `.claude/settings.local.json` or a hidden hook
    edit, and `git clean -fd` after a refusal keeps ignored files (#3846)."""
    reports = [
        _red_report(),
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, run = _pipeline(
        tmp_path,
        reports,
        host="daniel-server",
        heads=("base", "red1", "red1"),
        gates=_gates(red, green=[""]),
    )
    pipeline.project_claude_md = "CLAUDE.MD AT START"
    pipeline.run_all()

    argv, _, phase = run.claude[1]
    assert phase == "implement"
    assert "--setting-sources" in argv
    assert argv[argv.index("--setting-sources") + 1] == "user"
    settings = json.loads(argv[argv.index("--settings") + 1])
    hooks = pipeline.hook_root / ".claude" / "hooks"
    commands = [
        h["command"]
        for groups in settings["hooks"].values()
        for g in groups
        for h in g["hooks"]
    ]
    assert commands and all(c.startswith(f"{hooks}/run-hook.sh ") for c in commands)
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert prompt.startswith(pipeline.headless_prompt)
    assert prompt.endswith("CLAUDE.MD AT START")
    # The fix, land and file phases resume that session, whose transcript lacks CLAUDE.md.
    resumed = pipeline._resume()
    assert resumed[resumed.index("--append-system-prompt") + 1] == prompt


def test_a_pr_still_failing_the_green_gate_after_the_fix_is_not_landed(tmp_path):
    edited = "the fix changed what the red tests stand on: t.py"
    gates = _gates(Gate(files=["t.py"], nodes=["t.py::a"]), green=[edited, edited])
    reports = [
        _red_report(),
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
        _report(f"Fixed. {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, run = _pipeline(
        tmp_path, reports, heads=("base", "red1", "aaa", "bbb"), gates=gates
    )
    final = pipeline.run_all()

    assert [phase for _, _, phase in run.claude] == [
        "red",
        "implement",
        "review",
        "fix",
        "review",
    ]
    assert edited in run.claude[3][1]
    assert "git checkout red1 -- t.py" in run.claude[3][1]
    assert final["result"].startswith("needs input: the PR fails the green gate")
    assert final["result"].endswith(PR)


def test_a_fix_round_after_a_passing_green_gate_runs_the_gate_again_and_holds_a_failure(
    tmp_path,
):
    """The fixer may edit a red test even when the implementer's head passed the gate."""
    edited = "the fix changed what the red tests stand on: t.py"
    gates = _gates(Gate(files=["t.py"], nodes=["t.py::a"]), green=["", edited])
    reports = [
        _red_report(),
        _report(f"Opened {PR}"),
        _report(
            structured={"summary": "", "findings": [_finding("red test is wrong")]}
        ),
        _report(f"Fixed. {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, run = _pipeline(
        tmp_path, reports, heads=("base", "red1", "aaa", "bbb"), gates=gates
    )
    final = pipeline.run_all()

    assert "The red tests committed at red1 stay as they are" in run.claude[3][1]
    assert final["result"].startswith("needs input: the PR fails the green gate")
    assert pipeline.record.green_gate == edited
    assert "land" not in [phase for _, _, phase in run.claude]
