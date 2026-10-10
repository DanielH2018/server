"""The review pipeline a `launch --review` batch runs: phase order, the filter, disclosure.

Every process goes through one fake runner, `_review_fakes.FakeRunner`, which answers `git`,
records `gh` and replays a scripted report per `claude` call. It also records `.fanout/phase`
at each call, since the Stop hook reads that file to decide what a session owes. The red/green
phases are tested in `test_fanout_review_red_green.py`.

Run: uv run pytest scripts/dev/tests/test_fanout_review.py
"""

import json

from _review_fakes import PR, _finding, _pipeline, _report
from fanout_lib.review import PROMPT_FILE, Pipeline, actionable
from fanout_lib.target import SERVER_TARGET, Target


def test_an_implementer_with_no_pr_ends_the_run_before_any_review(tmp_path):
    pipeline, run = _pipeline(tmp_path, [_report("needs input: CI is red")])
    assert pipeline.run_all()["result"] == "needs input: CI is red"
    assert [phase for _, _, phase in run.claude] == ["implement"]
    assert run.comments == []


def _record(tmp_path):
    (path,) = (tmp_path / "state").glob("1345-*.json")
    return json.loads(path.read_text())


def test_a_batch_that_opens_no_pr_still_writes_a_record_naming_its_outcome(tmp_path):
    report = _report("needs input: CI is red")
    report["permission_denials"] = [{"tool_name": "Bash"}]
    pipeline, _run = _pipeline(tmp_path, [report])
    pipeline.run_all()
    record = _record(tmp_path)
    assert record["outcome"] == "needs-input"
    assert record["permission_denials"] == {"implement": 1}
    assert set(record["durations"]) == {"implement"}


def test_a_resumed_phase_records_only_what_it_added_to_the_session(tmp_path):
    """`--resume` reports the session's running total, so the fix costs 4.0 - 3.0."""
    reports = [
        _report(f"Opened {PR}", cost=3.0),
        _report(structured={"summary": "", "findings": [_finding("off by one")]}),
        _report(f"Fixed it. {PR}", cost=4.0),
        _report(structured={"summary": "resolved", "findings": []}),
    ]
    pipeline, _run = _pipeline(tmp_path, reports)
    pipeline.run_all()
    record = _record(tmp_path)
    assert record["costs"] == {
        "implement": 3.0,
        "review": 1.0,
        "fix": 1.0,
        "review-delta": 1.0,
    }
    assert record["outcome"] == "pr"


def test_an_actionable_finding_runs_a_fix_a_delta_review_and_the_landing_in_order(
    tmp_path,
):
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [_finding("off by one")]}),
        _report(f"Fixed it. {PR}"),
        _report(structured={"summary": "resolved", "findings": []}),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    final = pipeline.run_all()

    assert final["result"] == f"VERDICT: settled (deployed)\n{PR}"
    assert [phase for _, _, phase in run.claude] == [
        "implement",
        "review",
        "fix",
        "review",
    ]
    reviewer, review_stdin, _ = run.claude[1]
    assert "--disallowedTools" in reviewer and "--json-schema" in reviewer
    assert "--resume" not in reviewer
    # The reviewer learns the issue and the diff range, not the landing instructions.
    assert "body one" in review_stdin and "git diff base0...aaa" in review_stdin
    assert "land.sh" not in review_stdin
    assert run.claude[2][0][-2:] == ["--resume", "sid-1"]
    # The delta reviewer gets the fix round and the whole change it sits in, each named (#3954).
    assert "The fix round: `git diff aaa..bbb`" in run.claude[3][1]
    assert (
        "The whole change, the fix included: `git diff base0...bbb`" in run.claude[3][1]
    )
    # The pipeline lands the PR itself (#3960), under a login shell for the agent user's
    # LAND_HANDOFF_UNIT.
    land_sh = [str(tmp_path / "scripts/deploy_tools/land.sh"), "--pr", "4000"]
    assert run.lands[0][:3] == land_sh and "--detach" in run.lands[0]
    assert run.lands[1][:3] == ["cc-wait", "land", "4000"]
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


def _fix_round(left):
    """Reports for a batch whose fix round leaves `left` behind, then lands."""
    first = _finding("off by one", confidence=0.9)
    return [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [first]}),
        _report(f"Fixed. {PR}"),
        _report(structured={"summary": "", "findings": left}),
        _report(f"Filed #9. {PR}"),
    ]


def test_a_confident_medium_leftover_holds_the_pr_instead_of_landing(tmp_path):
    left = [_finding("still off by one", severity="medium", confidence=0.8)]
    pipeline, run = _pipeline(tmp_path, _fix_round(left))
    final = pipeline.run_all()
    assert final["result"].startswith("needs input:")
    assert "still off by one (scripts/x.py)" in final["result"]
    assert final["result"].endswith(PR)
    assert run.lands == []


def test_a_leftover_under_the_hold_bar_still_lands(tmp_path):
    left = [_finding("maybe off by one", severity="medium", confidence=0.79)]
    pipeline, run = _pipeline(tmp_path, _fix_round(left))
    pipeline.run_all()
    # Filed before the merge, so the PR body can name the leftover's issue.
    assert [phase for _, _, phase in run.claude][-1] == "file"
    assert len(run.lands) == 2


def test_the_pr_comment_lists_only_actionable_findings_and_counts_the_rest(tmp_path):
    findings = [
        _finding("off by one", confidence=0.9),
        _finding("naming nit", severity="low", confidence=0.9),
        _finding("unsure", severity="high", confidence=0.4),
    ]
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": findings}),
        _report(f"Fixed. {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    pipeline.run_all()
    comment = run.comments[0]
    assert "off by one" in comment
    assert "naming nit" not in comment and "unsure" not in comment
    assert "2 findings below the actionable bar" in comment
    assert "naming nit" in _record(tmp_path)["findings"][1]["title"]


def test_a_security_finding_stays_off_the_public_comment_and_the_tracker(tmp_path):
    # Actionable (0.6+) but under the 0.8 hold bar, so the batch still reaches the landing.
    held = _finding("token leaks to the log", confidence=0.7, category="security")
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [held]}),
        _report(f"Fixed. {PR}"),
        _report(structured={"summary": "", "findings": [held]}),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    final = pipeline.run_all()

    assert "token leaks" not in run.comments[0]
    assert "1 security findings are held" in run.comments[0]
    # A held finding is never filed, so no session resumes to file it.
    assert "file" not in [phase for _, _, phase in run.claude]
    assert "held off the public tracker" in final["result"]
    (record,) = [f for f in (tmp_path / "state").iterdir() if f.suffix == ".json"]
    assert "token leaks" in record.read_text()


def test_a_failed_review_is_said_on_the_pr_and_the_batch_still_lands(tmp_path):
    reports = [
        _report(f"Opened {PR}"),
        _report(is_error=True),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    pipeline.run_all()
    assert "did not complete" in run.comments[0]
    assert [phase for _, _, phase in run.claude] == ["implement", "review"]
    assert len(run.lands) == 2


def test_the_landing_is_skipped_when_too_little_run_time_is_left(tmp_path):
    # The pipeline reads the clock once at start to set its deadline; every later read, the
    # phase timings and the landing check alike, is past the cap.
    reads = []

    def clock():
        reads.append(None)
        return 0.0 if len(reads) == 1 else float(10**6)

    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, run = _pipeline(tmp_path, reports, clock=clock)
    final = pipeline.run_all()
    assert final["result"].startswith("needs input:")
    assert [phase for _, _, phase in run.claude] == ["implement", "review"]
    assert run.lands == []


def test_actionable_keeps_medium_at_the_confidence_floor_and_drops_the_rest():
    kept = _finding("kept", "medium", 0.6)
    assert actionable(
        [kept, _finding("unsure", "medium", 0.59), _finding("nit", "low")]
    ) == [kept]


def _test_finding(subkind, confidence=0.6):
    return dict(_finding(subkind, "low", confidence, "test"), subkind=subkind)


def test_a_low_vacuous_or_scaffold_test_finding_reaches_the_fix_round():
    # The severity floor dropped these anti-patterns unfiled (#4023).
    vacuous, scaffold = _test_finding("vacuous"), _test_finding("scaffold")
    assert actionable([vacuous, scaffold]) == [vacuous, scaffold]


def test_a_low_missing_coverage_or_unsure_vacuous_finding_stays_below_the_bar():
    assert (
        actionable(
            [
                _test_finding("missing-coverage"),
                _test_finding("vacuous", confidence=0.59),
            ]
        )
        == []
    )


def test_the_reviewer_reads_the_anti_patterns_read_at_start(tmp_path):
    pipeline, _ = _pipeline(tmp_path, [])
    pipeline.anti_patterns = "ANTI-PATTERNS AT START"
    reviewer = pipeline._reviewer()
    prompt = reviewer[reviewer.index("--append-system-prompt") + 1]
    assert "ANTI-PATTERNS AT START" in prompt


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
    assert reviewer[reviewer.index("--append-system-prompt") + 1].startswith(
        "PROMPT AT START"
    )
    assert Pipeline(tmp_path, "1", "h", SERVER_TARGET, "").review_prompt == (
        PROMPT_FILE.read_text()
    )


def test_every_phase_runs_the_prompt_and_hooks_read_at_start(tmp_path):
    """The implementer can rewrite the worktree's prompt file, settings and hooks, so none is
    read again once it has run (#3794, #3810)."""
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [_finding("off by one")]}),
        _report(f"Fixed it. {PR}"),
        _report(structured={"summary": "resolved", "findings": []}),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    pipeline.headless_prompt = "PROMPT AT START"
    pipeline.hooks = dict(pipeline.hooks, **{"fanout-stop.py": b"HOOK AT START"})
    assert "hooklib/worktree_lines.py" in pipeline.hooks
    assert not [name for name in pipeline.hooks if name.startswith("tests/")]

    hooks = pipeline.hook_root / ".claude" / "hooks"
    hook = hooks / "fanout-stop.py"
    seen = []

    def agent_edits_the_hooks(argv, stdin):
        if "--settings" in argv:
            seen.append((hook.read_bytes(), (hooks / "json.py").exists()))
            hook.chmod(0o644)
            hook.write_bytes(b"EDITED")
            (hooks / "json.py").write_text("PLANTED")
        return run(argv, stdin)

    pipeline.run = agent_edits_the_hooks
    pipeline.run_all()

    for argv in (run.claude[0][0], run.claude[2][0]):
        assert "--append-system-prompt-file" not in argv
        assert argv[argv.index("--append-system-prompt") + 1] == "PROMPT AT START"
        settings = json.loads(argv[argv.index("--settings") + 1])
        stops = [h for group in settings["hooks"]["Stop"] for h in group["hooks"]]
        assert [h["command"] for h in stops] == [f"{hooks}/run-hook.sh fanout-stop"]
    # Every phase starts from the bytes read at start, not the last phase's edit or plant.
    assert seen == [(b"HOOK AT START", False)] * 4
    # The worktree's own copy stands down on this marker; the snapshot path is the proof.
    assert (tmp_path / ".fanout" / "stop-hook").read_text().strip() == str(hook)


def test_a_resumed_phase_loads_no_settings_file_the_agent_can_write(tmp_path):
    """The fix and apply phases resume the implementer's session after it could edit
    `.claude/settings.json` and every guard hook (#3810)."""
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [_finding("off by one")]}),
        _report(f"Fixed it. {PR}"),
        _report(structured={"summary": "resolved", "findings": []}),
        _report(f"Applied. {PR}"),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    run.verdict = "VERDICT: needs-manual-apply (run the playbook)"
    held = json.loads(pipeline.project_settings)
    pipeline.run_all()

    hooks = pipeline.hook_root / ".claude" / "hooks"
    fix, apply = run.claude[2][0], run.claude[4][0]
    for argv in (fix, apply):
        assert argv[argv.index("--setting-sources") + 1] == "user"
        settings = json.loads(argv[argv.index("--settings") + 1])
        commands = [
            h["command"]
            for groups in settings["hooks"].values()
            for group in groups
            for h in group["hooks"]
        ]
        assert len(commands) == len(
            [h for groups in held["hooks"].values() for g in groups for h in g["hooks"]]
        )
        assert all(c.startswith(f"{hooks}/run-hook.sh ") for c in commands)
        assert settings["permissions"] == held["permissions"]
        assert (hooks / "block-protected-edits.py").is_file()
    # The guard reads the secret-bearing paths derived at start, not the agent's tree.
    assert run.derived[-1] == "scripts/secrets_mgmt/secret_bearing_host_paths.py"
    assert json.loads((hooks / "secret_bearing_host_paths.json").read_text()) == {
        "/usr/local/bin/a.sh": ["one_token", "two_token"]
    }
    # With no red phase before it, the implement phase keeps the project source.
    assert "--setting-sources" not in run.claude[0][0]


def test_the_reviewers_load_no_settings_file_and_get_claude_md_read_at_start(tmp_path):
    """The reviewer starts after the implementer could edit the settings, the guard hooks
    and `CLAUDE.md`, and it has Bash (#3825)."""
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [_finding("off by one")]}),
        _report(f"Fixed it. {PR}"),
        _report(structured={"summary": "resolved", "findings": []}),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    assert pipeline.project_claude_md.startswith("# Server Homelab")
    pipeline.project_claude_md = "CLAUDE.MD AT START"
    held = json.loads(pipeline.project_settings)
    pipeline.run_all()

    hooks = pipeline.hook_root / ".claude" / "hooks"
    for argv in (run.claude[1][0], run.claude[3][0]):
        assert argv[argv.index("--setting-sources") + 1] == "user"
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["permissions"] == held["permissions"]
        commands = [
            h["command"]
            for groups in settings["hooks"].values()
            for group in groups
            for h in group["hooks"]
        ]
        assert commands and all(c.startswith(f"{hooks}/run-hook.sh ") for c in commands)
        prompt = argv[argv.index("--append-system-prompt") + 1]
        assert prompt.startswith(pipeline.review_prompt)
        assert prompt.endswith("CLAUDE.MD AT START")
        assert "--disallowedTools" in argv


def test_another_repos_later_phases_keep_its_own_project_settings(tmp_path):
    """The pipeline holds no copy of another repo's hooks, so dropping its source drops them."""
    dotfiles = Target("DanielH2018/dotfiles", str(tmp_path), "origin/main")
    pipeline, _ = _pipeline(tmp_path, [], target=dotfiles)
    pipeline.session = "sid-1"
    argv = pipeline._resume()
    assert "--setting-sources" not in argv
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert list(settings["hooks"]) == ["Stop"]
    reviewer = pipeline._reviewer()
    assert "--setting-sources" not in reviewer
    pipeline.anti_patterns = ""
    reviewer = pipeline._reviewer()
    assert reviewer[reviewer.index("--append-system-prompt") + 1] == (
        pipeline.review_prompt
    )


def test_the_final_report_replaces_whatever_was_already_in_report_json(tmp_path):
    """systemd opens the file without truncating it, and status reads its last result line,
    so a line the red author planted past the report's end would otherwise win (#3871)."""
    from fanout_review import write_report

    planted = json.dumps(_report(f"Opened {PR}")) + "\n"
    report = tmp_path / "report.json"
    report.write_text(" " * 4096 + "\n" + planted)
    with report.open("r+") as out:
        write_report(_report("failed: no PR"), out)

    assert report.read_text() == json.dumps(_report("failed: no PR")) + "\n"
