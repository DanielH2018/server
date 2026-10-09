"""The review pipeline's red and green phases, against scripted gate verdicts.

The gates themselves run against real git and pytest in `test_fanout_red_gate.py`.

Run: uv run pytest scripts/dev/tests/test_fanout_review_red_green.py
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from _review_fakes import ISSUES, PR, _finding, _pipeline, _report
from fanout_lib.brief import ISSUES_HEADING, render_brief
from fanout_lib.red_gate import Gate, Gates
from fanout_lib.review import Pipeline
from fanout_lib.target import SERVER_TARGET
from lib.proc_testing import DEFAULT_TIMEOUT
from lib.git_testing import commit, git, git_out, init_repo


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

    assert ["reset", "--quiet", "--hard", "base"] in run.git
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


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("passed", [True, False], ids=["passed", "refused"])
def test_nothing_the_red_phase_hid_from_the_gates_reaches_the_implementer(
    tmp_path, passed
):
    """A skip-worktree edit, an ignored overwrite, an untracked root file and a background
    process (#3852). A process the implementer leaves is not the red phase's, and survives:
    the land phase's `land.sh --detach` depends on that."""
    repo = init_repo(tmp_path / "repo")
    files = {"land.sh": "real\n", ".gitignore": "/*.local.md\n.claude/*.local.json\n"}
    commit(repo, "base", **files)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text("{}")
    (repo / ".fanout").mkdir()

    def orphan():
        """Start a `setsid` process whose parent exits at once, and return its pid."""
        script = "(setsid sleep 60 >/dev/null 2>&1 </dev/null & echo $!)"
        started = subprocess.run(
            ["sh", "-c", script], capture_output=True, timeout=DEFAULT_TIMEOUT
        )
        return int(started.stdout)

    def red_author():
        orphans.append(orphan())
        red = commit(repo, "red", **{"tests/test_x.py": "def test_x():\n    pass\n"})
        git(repo, "update-index", "--skip-worktree", "land.sh")
        (repo / "land.sh").write_text("planted\n")
        (repo / ".claude" / "settings.local.json").write_text('{"allow": ["Bash(*)"]}')
        (repo / "CLAUDE.local.md").write_text("planted\n")
        return red

    seen = []
    orphans = []
    reports = [
        _red_report(),
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]

    def runner(argv, stdin):
        if argv[0] == "git":
            return git(repo, *argv[3:], check=False)
        if argv[0] in ("gh", "uv"):
            return subprocess.CompletedProcess(argv, 0, "", "")
        phase = (repo / ".fanout" / "phase").read_text().strip()
        if phase == "red":
            red_author()
        if phase == "implement":
            seen.append(
                {
                    "red_orphan": _alive(orphans[0]),
                    "land.sh": (repo / "land.sh").read_text(),
                    "settings": (repo / ".claude" / "settings.local.json").exists(),
                    "local_md": (repo / "CLAUDE.local.md").exists(),
                    "head": git_out(repo, "log", "-1", "--format=%s"),
                }
            )
            orphans.append(orphan())
        return subprocess.CompletedProcess(argv, 0, json.dumps(reports.pop(0)), "")

    gate = Gate(files=["tests/test_x.py"], nodes=["t"]) if passed else Gate("no")
    pipeline = Pipeline(
        repo,
        "1345",
        "daniel-server",
        SERVER_TARGET,
        render_brief(ISSUES, "daniel-server", "1345", "w", [], review=True),
        run=runner,
        clock=lambda: 0.0,
        state_dir=tmp_path / "state",
        red_green=True,
        gates=Gates(red=lambda *_: gate, green=lambda *_: ""),
    )
    pipeline.run_all()
    try:
        assert _alive(orphans[1])
        # A subreaper flag left set would have made this process its parent.
        stat = Path(f"/proc/{orphans[1]}/stat").read_text()
        assert stat.rpartition(")")[2].split()[1] != str(os.getpid())
    finally:
        os.kill(orphans[1], 9)

    assert seen == [
        {
            "red_orphan": False,
            "land.sh": "real\n",
            "settings": False,
            "local_md": False,
            "head": "red" if passed else "base",
        }
    ]


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
