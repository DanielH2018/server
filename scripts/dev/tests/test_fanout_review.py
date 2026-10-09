"""The review pipeline a `launch --review` batch runs: phase order, the filter, disclosure.

Every process goes through one fake runner, which answers `git`, records `gh` and replays a
scripted report per `claude` call. It also records `.fanout/phase` at each call, since the
Stop hook reads that file to decide what a session owes.

Run: uv run pytest scripts/dev/tests/test_fanout_review.py
"""

import json
import subprocess

from fanout_lib.brief import ISSUES_HEADING, Issue, render_brief
from fanout_lib.red_gate import Gate, Gates
from fanout_lib.review import PROMPT_FILE, Pipeline, actionable
from fanout_lib.target import SERVER_TARGET

PR = "https://github.com/DanielH2018/server/pull/4000"
ISSUES = [Issue(1345, "Traefik startupProbe has no red-proof", "body one")]


def _report(result="", session="sid-1", structured=None, is_error=False, cost=1.0):
    out = {
        "type": "result",
        "result": result,
        "session_id": session,
        "is_error": is_error,
        "total_cost_usd": cost,
    }
    if structured is not None:
        out["structured_output"] = structured
    return out


def _finding(title, severity="high", confidence=0.9, category="correctness"):
    return {
        "title": title,
        "file": "scripts/x.py",
        "line": 3,
        "severity": severity,
        "confidence": confidence,
        "category": category,
        "detail": "fails on an empty list",
    }


class FakeRunner:
    def __init__(self, worktree, reports, heads=("aaa", "bbb")):
        self.worktree = worktree
        self.reports = list(reports)
        self.heads = list(heads)
        self.claude = []  # (argv, stdin, phase file at call time)
        self.comments = []
        self.git = []

    def __call__(self, argv, stdin):
        if argv[0] == "git":
            self.git.append(argv[3:])
            if "merge-base" in argv:
                out = "base0"
            elif "rev-parse" in argv:
                out = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
            else:
                out = ""
            return subprocess.CompletedProcess(argv, 0, out + "\n", "")
        if argv[0] == "gh":
            self.comments.append(stdin)
            return subprocess.CompletedProcess(argv, 0, "", "")
        phase = (self.worktree / ".fanout" / "phase").read_text().strip()
        self.claude.append((argv, stdin, phase))
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.reports.pop(0)), "")


def _pipeline(
    tmp_path, reports, host="daniel-box", heads=("aaa", "bbb"), clock=None, gates=None
):
    (tmp_path / ".fanout").mkdir()
    brief = render_brief(ISSUES, host, "1345", "worktree-orch", [], review=True)
    run = FakeRunner(tmp_path, reports, heads)
    pipeline = Pipeline(
        tmp_path,
        "1345",
        host,
        SERVER_TARGET,
        brief,
        run=run,
        clock=clock or (lambda: 0.0),
        state_dir=tmp_path / "state",
        red_green=gates is not None,
        gates=gates or Gates(),
    )
    return pipeline, run


def test_an_implementer_with_no_pr_ends_the_run_before_any_review(tmp_path):
    pipeline, run = _pipeline(tmp_path, [_report("needs input: CI is red")])
    assert pipeline.run_all()["result"] == "needs input: CI is red"
    assert [phase for _, _, phase in run.claude] == ["implement"]
    assert run.comments == []


def test_an_actionable_finding_runs_a_fix_a_delta_review_and_the_landing_in_order(
    tmp_path,
):
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [_finding("off by one")]}),
        _report(f"Fixed it. {PR}"),
        _report(structured={"summary": "resolved", "findings": []}),
        _report(f"{PR}\nVERDICT: settled"),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    final = pipeline.run_all()

    assert final["result"].endswith("VERDICT: settled")
    assert [phase for _, _, phase in run.claude] == [
        "implement",
        "review",
        "fix",
        "review",
        "land",
    ]
    reviewer, review_stdin, _ = run.claude[1]
    assert "--disallowedTools" in reviewer and "--json-schema" in reviewer
    assert "--resume" not in reviewer
    # The reviewer learns the issue and the diff range, not the landing instructions.
    assert "body one" in review_stdin and "git diff base0...aaa" in review_stdin
    assert "land.sh" not in review_stdin
    for argv, _, _ in (run.claude[2], run.claude[4]):
        assert argv[-2:] == ["--resume", "sid-1"]
    assert "git diff aaa..bbb" in run.claude[3][1]
    assert "./scripts/deploy_tools/land.sh" in run.claude[4][1]
    assert "1 findings, 1 actionable" in run.comments[0]
    assert "0 left after the fix round" in run.comments[0]


def test_findings_below_the_bar_skip_the_fix_round_and_a_non_landing_host_stops(
    tmp_path,
):
    reports = [
        _report(f"Opened {PR}"),
        _report(
            structured={
                "summary": "",
                "findings": [_finding("nit", "low"), _finding("maybe", "high", 0.3)],
            }
        ),
    ]
    pipeline, run = _pipeline(tmp_path, reports, host="daniel-server")
    final = pipeline.run_all()
    assert final["result"] == f"Opened {PR}"
    assert [phase for _, _, phase in run.claude] == ["implement", "review"]
    assert "2 findings, 0 actionable" in run.comments[0]


def test_a_security_finding_stays_off_the_public_comment_and_the_tracker(tmp_path):
    held = _finding("token leaks to the log", category="security")
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [held]}),
        _report(f"Fixed. {PR}"),
        _report(structured={"summary": "", "findings": [held]}),
        _report(f"{PR}\nVERDICT: settled"),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    final = pipeline.run_all()

    assert "token leaks" not in run.comments[0]
    assert "1 security findings are held" in run.comments[0]
    assert (
        "token leaks" not in run.claude[4][1]
    )  # the land prompt files public ones only
    assert "held off the public tracker" in final["result"]
    (record,) = [f for f in (tmp_path / "state").iterdir() if f.suffix == ".json"]
    assert "token leaks" in record.read_text()


def test_a_failed_review_is_said_on_the_pr_and_the_batch_still_lands(tmp_path):
    reports = [
        _report(f"Opened {PR}"),
        _report(is_error=True),
        _report(f"{PR}\nVERDICT: settled"),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    pipeline.run_all()
    assert "did not complete" in run.comments[0]
    assert [phase for _, _, phase in run.claude] == ["implement", "review", "land"]
    assert "did not complete" in run.claude[2][1]


def test_the_landing_is_skipped_when_too_little_run_time_is_left(tmp_path):
    # The pipeline reads the clock once at start; the landing check reads it past the cap.
    ticks = iter([0.0, 10**6])
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, run = _pipeline(tmp_path, reports, clock=lambda: next(ticks))
    final = pipeline.run_all()
    assert final["result"].startswith("needs input:")
    assert [phase for _, _, phase in run.claude] == ["implement", "review"]


def test_actionable_keeps_medium_at_the_confidence_floor_and_drops_the_rest():
    kept = _finding("kept", "medium", 0.6)
    assert actionable(
        [kept, _finding("unsure", "medium", 0.59), _finding("nit", "low")]
    ) == [kept]


def test_the_reviewer_prompt_is_the_text_read_before_the_implementer_ran(tmp_path):
    """The implementer can write the worktree the module loads from, so a prompt file read
    at review time is one it could have rewritten."""
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, run = _pipeline(tmp_path, reports, host="daniel-server")
    pipeline.review_prompt = "PROMPT AT START"
    pipeline.run_all()
    reviewer = run.claude[1][0]
    assert "--append-system-prompt-file" not in reviewer
    assert reviewer[reviewer.index("--append-system-prompt") + 1] == "PROMPT AT START"
    assert Pipeline(tmp_path, "1", "h", SERVER_TARGET, "").review_prompt == (
        PROMPT_FILE.read_text()
    )


def test_every_phase_runs_the_prompt_and_stop_hook_read_at_start(tmp_path):
    """The implementer can rewrite the worktree's prompt file and hook, so neither is read
    again once it has run (#3794)."""
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [_finding("off by one")]}),
        _report(f"Fixed it. {PR}"),
        _report(structured={"summary": "resolved", "findings": []}),
        _report(f"{PR}\nVERDICT: settled"),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    pipeline.headless_prompt = "PROMPT AT START"
    pipeline.stop_hook = dict(
        pipeline.stop_hook, **{"fanout-stop.py": b"HOOK AT START"}
    )

    hook = pipeline.hook_root / ".claude" / "hooks" / "fanout-stop.py"
    seen = []

    def agent_edits_the_hook(argv, stdin):
        if "--settings" in argv:
            seen.append(hook.read_bytes())
            hook.chmod(0o644)
            hook.write_bytes(b"EDITED")
        return run(argv, stdin)

    pipeline.run = agent_edits_the_hook
    pipeline.run_all()

    for argv in (run.claude[0][0], run.claude[2][0], run.claude[4][0]):
        assert "--append-system-prompt-file" not in argv
        assert argv[argv.index("--append-system-prompt") + 1] == "PROMPT AT START"
        settings = json.loads(argv[argv.index("--settings") + 1])
        command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert command.startswith(str(pipeline.hook_root))
        assert str(tmp_path / ".claude") not in command
    # Every phase starts from the bytes read at start, not the last phase's edit.
    assert seen == [b"HOOK AT START"] * 5
    # The worktree's own copy stands down on this marker; the snapshot path is the proof.
    assert (tmp_path / ".fanout" / "stop-hook").read_text().strip() == str(hook)


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
