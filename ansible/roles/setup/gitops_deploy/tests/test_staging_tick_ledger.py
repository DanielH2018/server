"""What a real gated tick records, and — the half that matters — what it must not record.

Every check is a pair: one verdict the recorder must write, one it must refuse. A recorder that
writes on every verdict and one that writes on none are indistinguishable from the writing side
alone, and the refusing side is the load-bearing one here. `consult_staging` returns SKIPPED on
two paths that measured nothing, and it runs every ten minutes, so a recorder that wrote those
would bury the real samples under thousands of rows saying only that the gate did not run.
"""

import inspect
import json
from types import ModuleType

import pytest
from deploy_staging import (
    STAGING_NO_VERDICT,
    STAGING_PASS,
    STAGING_REJECTED,
    STAGING_SKIPPED,
    TICK_FALSE_FAILURE,
    TICK_NEEDS_TRIAGE,
    TICK_OK,
    staging_tick_outcome,
)
from deploy_toolbox import DeployTools

# The real clock: the recorder only stamps `at`, which these tests assert is non-empty.
TOOLS = DeployTools()


def test_a_pass_is_recorded_as_a_pass() -> None:
    assert staging_tick_outcome(STAGING_PASS) == TICK_OK


def test_a_skipped_verdict_has_no_outcome_at_all() -> None:
    assert staging_tick_outcome(STAGING_SKIPPED) is None


def test_no_verdict_is_a_false_failure_because_the_gate_could_not_be_asked() -> None:
    assert staging_tick_outcome(STAGING_NO_VERDICT) == TICK_FALSE_FAILURE


def test_a_rejection_is_never_attributed_automatically() -> None:
    """A rejection is the gate misfiring OR a real defect, and only an operator can say which."""
    assert staging_tick_outcome(STAGING_REJECTED) == TICK_NEEDS_TRIAGE


def test_an_unknown_word_records_nothing_rather_than_guessing() -> None:
    assert staging_tick_outcome("something-new") is None


def _ledger(gitops_deploy: ModuleType) -> list[dict]:
    path = gitops_deploy.STAGING_TICK_LEDGER
    try:
        with open(path) as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []


@pytest.mark.parametrize(
    "verdict, outcome",
    [
        (STAGING_PASS, TICK_OK),
        (STAGING_REJECTED, TICK_NEEDS_TRIAGE),
        (STAGING_NO_VERDICT, TICK_FALSE_FAILURE),
    ],
)
def test_every_real_verdict_appends_one_row(
    gitops_deploy: ModuleType, state_dir, verdict: str, outcome: str
) -> None:
    gitops_deploy.record_staging_tick(
        TOOLS, "c0ffee1234", {"freshrss", "ical-proxy"}, verdict
    )
    rows = _ledger(gitops_deploy)
    assert len(rows) == 1
    assert rows[0]["sha"] == "c0ffee1234"
    assert rows[0]["verdict"] == verdict
    assert rows[0]["outcome"] == outcome
    # Comma-joined and sorted, so the ledger reads the same way --tags does.
    assert rows[0]["tags"] == "freshrss,ical-proxy"
    assert rows[0]["at"]


def test_a_skipped_tick_appends_nothing(gitops_deploy: ModuleType, state_dir) -> None:
    """The reject half. A row marked skipped would be worse than no row: the tick fires every
    ten minutes and almost never reaches the gate."""
    gitops_deploy.record_staging_tick(TOOLS, "c0ffee1234", set(), STAGING_SKIPPED)
    assert _ledger(gitops_deploy) == []


def test_rows_accumulate_rather_than_replacing_each_other(
    gitops_deploy: ModuleType, state_dir
) -> None:
    gitops_deploy.record_staging_tick(TOOLS, "aaaaaaaa", {"freshrss"}, STAGING_PASS)
    gitops_deploy.record_staging_tick(TOOLS, "bbbbbbbb", {"freshrss"}, STAGING_REJECTED)
    assert [r["sha"] for r in _ledger(gitops_deploy)] == ["aaaaaaaa", "bbbbbbbb"]


def test_an_unwritable_ledger_costs_the_measurement_and_not_the_deploy(
    gitops_deploy: ModuleType, state_dir, monkeypatch
) -> None:
    """`consult_staging` may not break a prod deploy for any reason, this recorder included."""
    monkeypatch.setattr(
        gitops_deploy, "STAGING_TICK_LEDGER", str(state_dir / "no-such-dir" / "t.jsonl")
    )
    gitops_deploy.record_staging_tick(TOOLS, "c0ffee1234", {"freshrss"}, STAGING_PASS)


def test_consult_staging_records_the_verdict_it_returns(
    gitops_deploy: ModuleType,
) -> None:
    """Textual, because the alternative is scripting a whole staging round trip. The recorder
    has to see the SAME word consult_staging returns — recording one verdict and returning
    another is the defect a separate call site invites."""
    body = inspect.getsource(gitops_deploy.consult_staging)
    assert "verdict = staging_verdict(deploy_rc, expect_rc)" in body
    assert "record_staging_tick(tools, origin, gated, verdict)" in body
    assert "return verdict" in body
