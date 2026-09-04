"""Cross-phase invariants: what runs before what, and the annotation main() always writes.

Run: uv run pytest scripts/deploy_tools/tests/test_land_pipeline.py
"""

from __future__ import annotations

import pytest

from _land_fakes import MERGE_SHA, PRIMARY, Fakes, build_tools


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
