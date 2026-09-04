"""`annotation_line`, field by field, and the `cause` vocabulary the Ledger enforces.

The Landings board parses this line, so a field that changes shape or a `cause` nobody
expected is a dashboard change, not a refactor. `annotation_line` appeared in no test of its
own until this file existed.

Run: uv run pytest scripts/deploy_tools/tests/test_land_ledger.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.ledger import Ledger, annotation_line
from deploy_tools.land_lib.outcome import CAUSES, Cause, cause_for_deploy_exit

SHA = "0123456789abcdef0123456789abcdef01234567"


def _fields(line: str) -> dict[str, str]:
    """The logfmt line as a dict; `holder` keeps its quotes, which is what the board reads."""
    return dict(token.split("=", 1) for token in line.split(" ") if "=" in token)


def _filled() -> Ledger:
    return Ledger(
        pr="1031",
        t_start=100.0,
        t_merged=110.0,
        t_ci=140.0,
        t_tick=200.0,
        t_deploy=500.0,
        merge_sha=SHA,
        verdict="deploy-failed",
        cause="tag-miss",
        tags_label="sonarr,radarr",
        lock_waited=61,
        lock_holder="42 flock deploy",
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("event", "landing"),
        ("pr", "1031"),
        ("sha", SHA[:8]),
        ("verdict", "deploy-failed"),
        ("cause", "tag-miss"),
        ("exit", "1"),
        ("wait_merge", "10"),
        ("wait_ci", "30"),
        ("tick", "60"),
        ("deploy", "300"),
        ("total", "400"),
        ("tags", "sonarr,radarr"),
        ("lock", "61"),
    ],
)
def test_every_field_of_a_filled_ledger(field, value):
    assert _fields(annotation_line(_filled(), 1, 400.0))[field] == value


def test_the_holder_is_quoted_because_it_contains_spaces():
    """logfmt needs the quotes: `42 flock deploy` would otherwise parse as three fields."""
    assert 'holder="42 flock deploy"' in annotation_line(_filled(), 1, 400.0)


@pytest.mark.parametrize(
    "field, value",
    [
        ("pr", "unknown"),
        ("sha", ""),
        ("verdict", "aborted"),
        ("cause", ""),
        ("wait_merge", ""),
        ("wait_ci", ""),
        ("tick", ""),
        ("deploy", ""),
        ("tags", "none"),
        ("lock", "0"),
    ],
)
def test_an_unreached_stamp_leaves_its_field_empty(field, value):
    """A landing that died in step 1: every later stamp is None and must read as empty."""
    line = annotation_line(Ledger(pr="", t_start=0.0), 75, 5.0)
    assert _fields(line)[field] == value


def test_a_partial_landing_stamps_the_phases_it_reached_and_no_later_one():
    """Reaching the tick but not the deploy leaves `deploy=` empty, not 0."""
    ledger = Ledger(pr="7", t_start=0.0, t_merged=1.0, t_ci=5.0, t_tick=9.0)
    fields = _fields(annotation_line(ledger, 1, 9.0))
    assert (fields["wait_ci"], fields["tick"], fields["deploy"]) == ("4", "4", "")


def test_every_cause_a_phase_writes_is_in_the_vocabulary():
    for cause in Cause:
        assert cause in CAUSES


def test_an_unknown_cause_is_coerced_on_assignment(capsys):
    """The reject half. The board groups by this field, so an invented value would become
    a bar; it is coerced to one named bucket and warned, not raised, because the write
    happens after the deploy ran and a traceback there loses the `VERDICT:` line."""
    ledger = Ledger(pr="1", t_start=0.0)
    ledger.cause = "deploy-exit-9"
    assert ledger.cause == Cause.INVALID
    assert "unknown ledger cause 'deploy-exit-9'" in capsys.readouterr().err


def test_an_unknown_cause_is_coerced_at_construction_too():
    assert Ledger(pr="1", t_start=0.0, cause="whatever").cause == Cause.INVALID


def test_the_empty_cause_stays_legal():
    """An empty cause is the no-cause value beside every verdict but deploy-failed."""
    assert Ledger(pr="1", t_start=0.0).cause == ""


@pytest.mark.parametrize(
    "rc, expected",
    [
        (1, "deploy-exit-1"),
        (3, "deploy-exit-3"),
        (4, "deploy-exit-4"),
        (64, "deploy-exit-64"),
        (9, "deploy-exit-other"),
        (255, "deploy-exit-other"),
    ],
)
def test_an_unhandled_deploy_exit_maps_into_the_closed_vocabulary(rc, expected):
    """Every deploy.sh code keeps the label the board already groups by; the rest bucket."""
    cause = cause_for_deploy_exit(rc)
    assert cause == expected
    Ledger(pr="1", t_start=0.0).cause = cause  # must not raise
