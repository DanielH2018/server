"""Cross-phase invariants: what runs before what, and the annotation main() always writes.

Run: uv run pytest scripts/deploy_tools/tests/test_land_pipeline.py
"""

import subprocess

import pytest

from _land_fakes import MERGE_SHA, PRIMARY, Fakes, build_tools
from deploy_tools.land_lib.tools import Classifier
from deploy_tools.promote_lib.wait import WaitResult

_ORDERED = ("await_ci", "land_wait", "gate")


def _names(calls):
    return [c[0] for c in calls]


def test_nothing_to_deploy_is_decided_before_the_ci_wait(land_run):
    """Sixteen of 45 landings waited a median seven minutes of CI to learn this."""
    files = {"files": [{"path": "docs/landing.md"}], "changedFiles": 1}
    fakes = Fakes(gh_views={"files,changedFiles": files}, derived=([], "pr"))
    rc, out, _, calls, logline = land_run([], fakes)
    assert rc == 0 and "VERDICT: nothing-to-deploy" in out
    assert "await_ci" not in _names(calls)
    assert "verdict=nothing-to-deploy cause= exit=0" in logline


def test_preflight_runs_before_the_ci_wait(land_run):
    _, _, _, calls, _ = land_run([], Fakes(await_ci=[(1, "red")]))
    blockers = next(
        i
        for i, c in enumerate(calls)
        if c[0] == "deploy_tags" and c[1][0] == "blockers"
    )
    assert blockers < _names(calls).index("await_ci")


def test_the_annotation_is_written_on_a_verdict(land_run):
    _, _, _, _, logline = land_run([], Fakes(await_ci=[(75, "no verdict")]))
    assert (
        "event=landing pr=999" in logline
        and "verdict=ci-timeout cause= exit=75" in logline
    )


def test_an_unexpected_exception_annotates_as_aborted_and_propagates(monkeypatch):
    """Built by hand rather than through `land_run`, because the raising gh_json must be
    injected into `tools`; the primary override is repeated here for the same reason the
    fixture carries it -- Options defaults to a checkout that exists only on the deploy host.
    """
    import land
    from deploy_tools.land_lib.options import Options

    monkeypatch.setattr(
        land, "parse_args", lambda argv, desc: Options(pr="7", primary=PRIMARY)
    )
    tools, calls = build_tools(Fakes())
    tools.gh_json = lambda *a, **k: 1 / 0
    with pytest.raises(ZeroDivisionError):
        land.main(["--pr", "7"], tools=tools)
    assert "verdict=aborted cause= exit=1" in next(
        c[1][0] for c in calls if c[0] == "logger"
    )


def test_an_unexpected_exception_still_prints_and_records_a_verdict(tmp_path, capsys):
    """Direct, it reads as it does through the handoff unit: `refused`, naming the raise."""
    import land
    from deploy_tools.land_lib.options import Options

    verdict_file = tmp_path / "7.verdict"
    opts = Options(pr="7", primary=PRIMARY, verdict_file=str(verdict_file))
    tools, _ = build_tools(Fakes())
    tools.gh_json = lambda *a, **k: 1 / 0
    with pytest.raises(ZeroDivisionError):
        land._land(opts, tools, None)
    from deploy_tools.land_lib import verdict_log

    expected = "VERDICT: refused (PR #7 — the landing raised ZeroDivisionError: division by zero)"
    assert capsys.readouterr().out.splitlines()[-1] == expected
    assert verdict_file.read_text() == expected + "\n"
    assert [(e["pr"], e["rc"], e["line"]) for e in verdict_log.latest()] == [
        ("7", 1, expected)
    ]


def test_a_verdict_is_recorded_for_probe_landing_verdicts(land_run, verdict_record):
    """The per-user record is what replaces grepping a background task's output file."""
    from deploy_tools.land_lib import verdict_log

    rc, out, _, _, _ = land_run([], Fakes(await_ci=[(75, "no verdict")]))
    line = out.rstrip().splitlines()[-1]
    assert rc == 75 and line.startswith("VERDICT: ci-timeout")
    assert [
        (e["pr"], e["rc"], e["line"]) for e in verdict_log.latest(verdict_record)
    ] == [("999", 75, line)]


@pytest.mark.parametrize("argv", [["--bogus"], []])
def test_a_usage_error_does_not_annotate(argv):
    """The rejecting half: a bad flag and a missing `--pr` are usage errors, not landings,
    and must write nothing to the stream the Landings board counts. Both reach argparse's
    SystemExit before a Landing or a Ledger exists, so the only line they could write is
    `pr=unknown verdict=aborted` -- 592 of which reached Loki's 744h window. `main`
    renumbers the code to `LAND_BAD_ARGS` on the way out and still annotates nothing.

    Real `parse_args`, not the `land_run` fixture's wrapper: the wrapper always calls the
    real parser first and only replaces `primary` afterwards, so an unrecognized flag still
    raises a usage-error SystemExit through it same as here.
    """
    import land
    from _land_fakes import build_tools
    from lib.exit_codes import LAND_BAD_ARGS

    tools, calls = build_tools(Fakes())
    with pytest.raises(SystemExit) as exc:
        land.main(argv, tools=tools)
    assert exc.value.code == LAND_BAD_ARGS
    assert not any(c[0] == "logger" for c in calls)


def test_help_does_not_annotate():
    """`--help` (exit 0) is deliberately NOT reproduced: bash's trap logged a junk
    `verdict=aborted exit=0` line for it, which this port correctly drops."""
    import land
    from _land_fakes import build_tools

    tools, calls = build_tools(Fakes())
    with pytest.raises(SystemExit) as exc:
        land.main(["--help"], tools=tools)
    assert exc.value.code == 0
    assert not any(c[0] == "logger" for c in calls)


def _sonarr_settled(**kwargs: object) -> Fakes:
    """land-wait's changed set names sonarr, and the owner that keeps it gated."""
    fakes = Fakes(
        land_wait=[WaitResult("settled", detail="1 applied", tags=("sonarr",))],
    )
    for name, value in kwargs.items():
        setattr(fakes, name, value)
    return fakes


def _refs(state: str, repo: str = "o/server", reopened_at: str | None = None) -> dict:
    """The closing-reference read: PR #999 merged at noon, naming issue #7."""
    issue = {"number": 7, "state": state, "repo": repo, "reopenedAt": reopened_at}
    return {"repo": "o/server", "mergedAt": "2026-11-05T12:00:00Z", "issues": [issue]}


def _closes(calls) -> list[tuple]:
    return [c[1] for c in calls if c[0] == "gh" and c[1][:2] == ("issue", "close")]


def _relayed(out: str) -> str:
    """The lines of `out` that `handoff._relay` passes to an agent landing through the unit."""
    return "\n".join(ln for ln in out.splitlines() if ln.startswith("== "))


def test_an_issue_github_left_open_is_closed_without_changing_the_verdict(land_run):
    rc, out, _, calls, _ = land_run([], _sonarr_settled(closing_refs=[_refs("OPEN")]))
    assert rc == 0 and "VERDICT: settled" in out
    assert [c[:3] for c in _closes(calls)] == [("issue", "close", "7")]
    assert "--reason" in _closes(calls)[0] and "completed" in _closes(calls)[0]


def test_an_issue_github_already_closed_is_not_closed_again(land_run):
    _, _, _, calls, _ = land_run([], _sonarr_settled(closing_refs=[_refs("CLOSED")]))
    assert _closes(calls) == []
    assert not [c for c in calls if c[0] == "sleep"]


def test_an_issue_github_closes_during_the_grace_read_is_not_closed(land_run):
    fakes = _sonarr_settled(closing_refs=[_refs("OPEN"), _refs("CLOSED")])
    _, _, _, calls, _ = land_run([], fakes)
    assert _closes(calls) == []


@pytest.mark.parametrize(
    ("refs", "reason"),
    [
        (_refs("OPEN", repo="o/dotfiles"), "not closing another repo's issue"),
        (_refs("OPEN", reopened_at="2026-11-05T13:00:00Z"), "reopened after the merge"),
    ],
)
def test_an_open_issue_the_landing_must_not_close_stays_open(land_run, refs, reason):
    _, out, _, calls, _ = land_run([], _sonarr_settled(closing_refs=[refs]))
    assert _closes(calls) == [] and reason in out


def test_an_issue_reopened_before_the_merge_is_closed(land_run):
    refs = _refs("OPEN", reopened_at="2026-11-05T11:00:00Z")
    _, _, _, calls, _ = land_run([], _sonarr_settled(closing_refs=[refs]))
    assert [c[:3] for c in _closes(calls)] == [("issue", "close", "7")]


def test_a_failed_close_names_the_remedy_and_keeps_the_verdict(land_run):
    fakes = _sonarr_settled(closing_refs=[_refs("OPEN")], gh_merge_rc=[1])
    rc, out, _, _, _ = land_run([], fakes)
    assert rc == 0 and "VERDICT: settled" in out
    assert "findings.py close 7 --fixed --pr 999" in _relayed(out)


def test_a_failed_read_says_so_and_keeps_the_verdict(land_run):
    failure = subprocess.CalledProcessError(1, "gh", stderr="HTTP 403")
    rc, out, _, calls, _ = land_run([], _sonarr_settled(closing_refs=[failure]))
    assert rc == 0 and "VERDICT: settled" in out
    assert "could not read PR #999's closing references" in _relayed(out)
    assert _closes(calls) == []


@pytest.mark.parametrize(
    ("failing", "wanted"),
    [
        (
            {
                "closing_refs": [
                    subprocess.CalledProcessError(1, "gh", stderr="HTTP 403\nDenied")
                ]
            },
            "could not read PR #999's closing references (HTTP 403 Denied)",
        ),
        (
            {"closing_refs": [_refs("OPEN")], "gh_merge_rc": [1]},
            "findings.py close 7 --fixed --pr 999",
        ),
    ],
)
def test_a_failed_read_or_close_ends_the_verdict_line_cc_wait_prints(
    land_run, tmp_path, failing, wanted
):
    verdict_file = tmp_path / "999.verdict"
    fakes = _sonarr_settled(**failing)
    rc, *_ = land_run(["--verdict-file", str(verdict_file)], fakes)
    line, *rest = verdict_file.read_text().splitlines()
    assert rc == 0 and line.startswith("VERDICT: settled (") and not rest
    assert wanted in line


def test_a_close_that_times_out_is_retried(landing):
    from deploy_tools.land_lib import closing_refs

    ln, _ = landing(Fakes(closing_refs=[_refs("OPEN")]))
    attempts = []

    def gh(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise subprocess.TimeoutExpired("gh", 1)

    ln.tools.gh = gh
    closing_refs.close_stranded(ln)
    assert len(attempts) == 2


def test_the_close_comes_before_any_wait_a_later_step_can_die_in(land_run):
    _, _, _, calls, _ = land_run([], _sonarr_settled(closing_refs=[_refs("OPEN")]))
    names = [
        "close" if c[0] == "gh" and c[1][:2] == ("issue", "close") else c[0]
        for c in calls
    ]
    assert names.index("close") < names.index("await_ci")


def test_a_docs_only_landing_still_closes_a_stranded_issue(land_run):
    fakes = Fakes(
        closing_refs=[_refs("OPEN")],
        gh_views={"files,changedFiles": {"files": [], "changedFiles": 0}},
    )
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 0 and "VERDICT: nothing-to-deploy" in out
    assert [c[:3] for c in _closes(calls)] == [("issue", "close", "7")]


def test_a_landing_that_ends_before_the_merge_commit_closes_nothing(land_run):
    fakes = _sonarr_settled(
        closing_refs=[_refs("OPEN")],
        gh_views={"mergeCommit": {"mergeCommit": None}},
    )
    rc, _, _, calls, _ = land_run([], fakes)
    assert rc != 0
    assert _closes(calls) == [] and "gh:closing-refs" not in [c[0] for c in calls]


def test_settled_end_to_end(land_run):
    rc, out, _, calls, logline = land_run([], _sonarr_settled())
    assert rc == 0 and out.rstrip().endswith(
        f"VERDICT: settled (PR #999, {MERGE_SHA}, tags: sonarr)"
    )
    assert [c[0] for c in calls if c[0] in _ORDERED] == list(_ORDERED)
    assert "verdict=settled cause= exit=0" in logline and "tags=sonarr" in logline
    for k in ("wait_merge", "wait_ci", "deploy"):
        assert f"{k}=" in logline and f"{k}= " not in logline


def test_the_diff_fallback_derives_between_the_ci_wait_and_the_promotion_wait(land_run):
    _, _, _, calls, _ = land_run(
        ["--since", "abc"], Fakes(derived=([], "fallback"), changed="sonarr")
    )
    names = [c[1][0] if c[0] == "deploy_tags" else c[0] for c in calls]
    assert names.index("await_ci") < names.index("changed") < names.index("land_wait")


def test_a_missing_primary_checkout_is_named_before_any_phase_runs(land_run):
    """Every phase runs git there, so it is checked first and named plainly.

    It ends `refused`, as every exit-1 stop does (#4451), rather than the `aborted` the
    board reserves for a path that named no verdict.
    """
    from pathlib import Path

    rc, _, err, calls, logline = land_run([], primary=Path("/nonexistent"))
    assert rc == 1
    assert "cannot cd to /nonexistent" in err
    assert "gh" not in _names(calls)
    assert "verdict=refused cause= exit=1" in logline


def test_land_never_bypasses_the_staleness_guard():
    """The tempting fix for exit 4 is the flag that disables the check.

    Every module in the package, not just deploy.py: the flag would work from any of them,
    and a guard reading one file goes silently vacuous the day the deploy call moves. The
    count assertion is what stops the glob returning nothing after a rename.
    """
    from pathlib import Path

    from deploy_tools.land_lib import deploy

    modules = sorted(Path(deploy.__file__).parent.glob("*.py"))
    assert len(modules) >= 12, [p.name for p in modules]
    for module in modules:
        assert "--skip-staleness-check" not in module.read_text(), module.name


def test_every_verdict_is_produced_by_a_running_test():
    """The census: every verdict asserted by name in a land test."""
    import re
    from pathlib import Path

    from deploy_tools.land_lib.outcome import VERDICTS

    tests = Path(__file__).parent
    files = sorted(tests.glob("test_land_*.py"))
    assert len(files) >= 10, [p.name for p in files]
    text = "\n".join(p.read_text() for p in files)
    named = set(re.findall(r'"([a-z-]+)"', text)) & VERDICTS
    assert named == VERDICTS, sorted(VERDICTS - named)


def test_prepare_stdio_line_buffers_a_real_file_stream(tmp_path, monkeypatch):
    """A landing always runs with stdout redirected to a file, where Python block-buffers it.

    Asserted against a real `TextIOWrapper` over a file rather than through `land.main` under
    capsys: pytest's capture object is not the stream the fix reconfigures, so asserting
    `sys.stdout.line_buffering` after a captured run would pass whether or not the call is
    there.
    """
    import land

    with (tmp_path / "land.log").open("w") as fh:
        assert fh.line_buffering is False
        monkeypatch.setattr(land.sys, "stdout", fh)
        land._prepare_stdio()
        assert fh.line_buffering is True


def test_prepare_stdio_tolerates_a_stdout_that_cannot_reconfigure(monkeypatch):
    """capsys replaces sys.stdout with an object that has no `reconfigure`, and so does any
    session that redirects it in-process. Failing there would break every captured test."""
    import land

    class _Plain:
        def write(self, s):
            return len(s)

    monkeypatch.setattr(land.sys, "stdout", _Plain())
    land._prepare_stdio()  # must not raise


def test_the_real_classifier_derives_a_tag_from_a_real_path(land_run):
    """One pipeline run with the REAL `Classifier` and every process boundary still fake.

    The fakes replace all five classifiers with constant lambdas, so no other pipeline test
    runs real tag derivation -- the answer is whatever `Fakes.derived` says. This one hands
    `land.main` a real `Classifier` and a real repo path, so `role_for` and the containers_list
    lookup both actually run.

    `pull_ref_rc=1` makes `pr_range` fail to read the PR's own range, which is what keeps
    `quiet_paths` from shelling out to git: with no range it returns immediately, and every
    broad path stays loud -- the direction a wrong answer there must fall.
    """
    fakes = _sonarr_settled(
        pull_ref_rc=1,
        gh_views={
            "files,changedFiles": {
                "files": [{"path": "ansible/roles/k8s/sonarr/defaults/main.yml"}],
                "changedFiles": 1,
            }
        },
    )
    rc, out, _, calls, logline = land_run([], fakes, classifier=Classifier())
    assert (rc, "VERDICT: settled" in out) == (0, True)
    assert "tags=sonarr" in logline.split()
    assert [c[1] for c in calls if c[0] == "gate"] == [(["sonarr"],)]


def test_the_real_classifier_derives_nothing_from_a_docs_only_path(land_run):
    """The reject half: a path that maps to no role must not produce a tag."""
    fakes = Fakes(
        pull_ref_rc=1,
        gh_views={
            "files,changedFiles": {
                "files": [{"path": "docs/python-code-organization.md"}],
                "changedFiles": 1,
            }
        },
    )
    rc, out, _, calls, _ = land_run([], fakes, classifier=Classifier())
    assert rc == 0 and "VERDICT: nothing-to-deploy" in out
    assert "gate" not in _names(calls)


def test_a_setup_role_only_pr_ends_nothing_to_deploy_without_a_tick(land_run):
    """H7.49a: the deployer's timer applies the role, and the landing neither starts nor awaits it.

    The real `Classifier` runs over a real setup-role path. Before H7.49a this PR was
    `self_applied`, so the landing awaited a tick and graded the deployer's markers.
    """
    fakes = Fakes(
        pull_ref_rc=1,
        gh_views={
            "files,changedFiles": {
                "files": [
                    {
                        "path": "ansible/roles/setup/renovate_agent/files/renovate_agent.py"
                    }
                ],
                "changedFiles": 1,
            }
        },
    )
    rc, out, *_ = land_run([], fakes, classifier=Classifier())
    assert rc == 0 and "VERDICT: nothing-to-deploy" in out


def test_every_numbered_step_header_uses_the_same_denominator(land_run):
    """Finding 27: `/6` was written out seven times against eight prints, checked by nobody."""
    from deploy_tools.land_lib import pipeline

    _, out, _, _, _ = land_run([], _sonarr_settled())
    headers = [ln for ln in out.splitlines() if ln.startswith("== ")]
    numbered = [h for h in headers if "/" in h.split()[1]]
    assert [h.split()[1] for h in numbered] == [
        f"{n}/{pipeline.STEP_COUNT}" for n in range(1, pipeline.STEP_COUNT + 1)
    ]
    # A step that stops printing its header, or one added to the list and never reached,
    # both show up here. `STEP_COUNT == len(_STEPS)` would be a tautology.
    assert len(numbered) == pipeline.STEP_COUNT
