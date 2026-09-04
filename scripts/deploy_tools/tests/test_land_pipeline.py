"""Cross-phase invariants: what runs before what, and the annotation main() always writes.

Run: uv run pytest scripts/deploy_tools/tests/test_land_pipeline.py
"""

import pytest

from _land_fakes import MERGE_SHA, PRIMARY, Fakes, build_tools
from deploy_tools.land_lib.tools import Classifier


def _names(calls):
    return [c[0] for c in calls]


def test_nothing_to_deploy_is_decided_before_the_ci_wait(land_run):
    """Sixteen of 45 landings waited a median seven minutes of CI to learn this."""
    rc, out, _, calls, logline = land_run([], Fakes(derived=([], "pr")))
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


def test_a_bad_argument_annotates_before_reraising_exit_2():
    """argparse raises before a Landing/Ledger exists, so a bad flag used to reach the
    Landings board as nothing at all -- bash's EXIT trap was installed before its arg loop
    and covered this (#1085 item 2). Real `parse_args`, not the `land_run` fixture's
    wrapper: the wrapper always calls the real parser first and only replaces `primary`
    afterwards, so an unrecognized flag still raises SystemExit(2) through it same as here.
    """
    import land
    from _land_fakes import build_tools

    tools, calls = build_tools(Fakes())
    with pytest.raises(SystemExit) as exc:
        land.main(["--bogus"], tools=tools)
    assert exc.value.code == 2
    logline = next(c[1][0] for c in calls if c[0] == "logger")
    assert "event=landing pr=unknown" in logline
    assert "verdict=aborted" in logline and "exit=2" in logline


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


def test_settled_end_to_end(land_run):
    rc, out, _, calls, logline = land_run([], Fakes())
    assert rc == 0 and out.rstrip().endswith(
        f"VERDICT: settled (PR #999, {MERGE_SHA}, tags: sonarr)"
    )
    assert [c[0] for c in calls if c[0] in ("await_ci", "tick", "deploy", "gate")] == [
        "await_ci",
        "tick",
        "deploy",
        "gate",
    ]
    assert "verdict=settled cause= exit=0" in logline and "tags=sonarr" in logline
    for k in ("wait_merge", "wait_ci", "tick", "deploy"):
        assert f"{k}=" in logline and f"{k}= " not in logline


def test_the_diff_fallback_reaches_the_tick_before_deriving(land_run):
    _, _, _, calls, _ = land_run(
        ["--since", "abc"], Fakes(derived=([], "fallback"), changed="sonarr")
    )
    names = [c[1][0] if c[0] == "deploy_tags" else c[0] for c in calls]
    assert names.index("tick") < names.index("changed") < names.index("deploy")


def test_a_missing_primary_checkout_is_named_before_any_phase_runs(land_run):
    """Every phase runs git and deploy.sh there, so it is checked first and named plainly.

    No verdict: bash annotated this as `aborted` too, and it is a broken invocation rather
    than a landing outcome.
    """
    from pathlib import Path

    rc, _, err, calls, logline = land_run([], primary=Path("/nonexistent"))
    assert rc == 1
    assert "cannot cd to /nonexistent" in err
    assert "gh" not in _names(calls)
    assert "verdict=aborted cause= exit=1" in logline


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
    """The #1012 census: thirteen verdicts, each asserted by name in a land test."""
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
    `land.main` a real `Classifier` and a real repo path, so `role_for`, the containers_list
    lookup and `expand_build_couplings` all actually run.

    `pull_ref_rc=1` makes `pr_range` fail to read the PR's own range, which is what keeps
    `quiet_paths` from shelling out to git: with no range it returns immediately, and every
    broad path stays loud -- the direction a wrong answer there must fall (issue #848).
    """
    fakes = Fakes(
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
    assert "tags=sonarr " in logline
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


def test_every_numbered_step_header_uses_the_same_denominator(land_run):
    """Finding 27: `/6` was written out seven times against eight prints, checked by nobody."""
    from deploy_tools.land_lib import pipeline

    _, out, _, _, _ = land_run([])
    headers = [ln for ln in out.splitlines() if ln.startswith("== ")]
    numbered = [h for h in headers if "/" in h.split()[1]]
    assert [h.split()[1] for h in numbered] == [
        f"{n}/{pipeline.STEP_COUNT}" for n in range(1, pipeline.STEP_COUNT + 1)
    ]
    # A step that stops printing its header, or one added to the list and never reached,
    # both show up here. `STEP_COUNT == len(_STEPS)` would be a tautology.
    assert len(numbered) == pipeline.STEP_COUNT
