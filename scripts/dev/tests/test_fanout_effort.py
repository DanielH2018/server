"""The model and effort every fan-out session runs at, pinned on its command line.

The operator ruled on 2026-11-11 that `fanout_lib.sizing` may pick Fable and reach `xhigh`, and
never `max`. Each phase passes `--model` and `--effort` itself, because an inherited setting
would move with the next settings edit. The reviewer's own effort test sits in
`test_fanout_review.py`, beside its record.

Run: uv run pytest scripts/dev/tests/test_fanout_effort.py
"""

import pytest

from fanout_lib.brief import Issue
from fanout_lib.launch import systemd_run_command
from fanout_lib import review_unit
from fanout_lib.place import main
from fanout_lib.sizing import LADDER, by_name, size
from _fanout_fakes import HOST_KEY, fake_tools, ok
from _review_fakes import _pipeline

HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"
TOP = by_name("fable-xhigh")


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_no_rung_runs_at_max_and_only_the_top_rung_reaches_xhigh():
    assert all(rung.effort != "max" for rung in LADDER)
    assert [rung for rung in LADDER if rung.effort == "xhigh"] == [TOP]
    assert LADDER[-1] == TOP


def test_a_leftover_batch_is_raised_to_its_floor_but_not_lowered_from_above_it():
    assert size({"review-leftover"}, by_name("sonnet-medium")) == by_name("opus-high")
    assert size({"review-leftover"}, TOP) == TOP


def test_an_unreviewed_batch_pins_its_rungs_model_effort_and_budget():
    argv = systemd_run_command("b", rung=TOP).split()
    assert _flag(argv, "--model") == "fable"
    assert _flag(argv, "--effort") == "xhigh"
    assert _flag(argv, "--max-budget-usd") == str(TOP.budget_usd)


def test_every_implementer_phase_runs_at_the_rung_and_resume_keeps_its_model(tmp_path):
    """A resume on another model would drop the implementer session's prompt cache."""
    pipeline, _ = _pipeline(tmp_path, [])
    pipeline.record.implementer = TOP
    pipeline.session = "sid-1"
    for argv in (pipeline._implementer(), pipeline._resume()):
        assert (_flag(argv, "--model"), _flag(argv, "--effort")) == ("fable", "xhigh")
        assert _flag(argv, "--max-budget-usd") == str(TOP.budget_usd)


def test_the_red_author_and_reviewer_stay_on_opus_medium_whatever_the_rung(tmp_path):
    pipeline, _ = _pipeline(tmp_path, [])
    pipeline.record.implementer = TOP
    for argv in (pipeline._red_author(), pipeline._reviewer()):
        assert (_flag(argv, "--model"), _flag(argv, "--effort")) == ("opus", "medium")


def test_the_review_unit_builds_its_pipeline_at_the_rung_it_was_launched_with(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    argv = ["--batch", "b", "--repo", "DanielH2018/server", "--rung", "fable-high"]
    assert review_unit.build(argv, "brief").record.implementer == by_name("fable-high")


def _launch(tmp_path, labels: tuple[str, ...], *flags: str) -> tuple[int, str]:
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)},
        issues=[Issue(1, "one", "body one", ("claude", *labels))],
    )
    argv = ["launch", "--batch", "1", "--host", "daniel-box", *flags]
    code = main([*argv, "--manifest-root", str(tmp_path)], tools)
    units = [c[1] for c in run.host_calls if "systemd-run" in c[1]]
    return code, units[0] if units else ""


def test_size_reaches_the_review_unit_as_its_rung(tmp_path):
    code, unit = _launch(tmp_path, (), "--size", "1=fable-high")
    assert code == 0
    assert (
        "fanout.py review --batch 1 --repo DanielH2018/server --rung fable-high "
        in unit
    )


def test_a_leftover_launch_runs_and_reports_its_floor_when_sized_below_it(
    tmp_path, capsys
):
    code, unit = _launch(tmp_path, ("review-leftover",), "--size", "1=sonnet-medium")
    assert code == 0
    assert "--rung opus-high" in unit
    assert "1 -> daniel-box (opus-high)" in capsys.readouterr().out


@pytest.mark.parametrize("spec", ["2=opus-high", "1=opus-max"])
def test_a_size_naming_no_batch_or_no_rung_launches_nothing(tmp_path, spec):
    code, unit = _launch(tmp_path, (), "--size", spec)
    assert code != 0
    assert unit == ""
