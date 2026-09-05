"""Slice 4: which staging verdict stops a prod deploy, and how the override is spent.

Every check here comes in a pair — one input the rule must accept, one it must reject — because
a gate that fires on everything and a gate that fires on nothing are indistinguishable from the
passing side alone.
"""

import dataclasses
import itertools

import pytest
import deploy_handlers
from deploy_staging import (
    STAGING_NO_VERDICT,
    STAGING_PASS,
    STAGING_REJECTED,
    STAGING_SKIPPED,
    staging_blocks,
    staging_verdict,
    staging_verdict_summary,
)

# Every exit code either child can produce: 0 pass, 1 rejected/failed, 2 no verdict. 3 stands in
# for a code neither script defines, which must still not read as a pass.
RCS = (0, 1, 2, 3)


def test_a_rejection_blocks_once_blocking_is_on() -> None:
    assert staging_blocks(STAGING_REJECTED, blocking=True)


def test_no_verdict_never_blocks() -> None:
    """The part-3 decision, pinned as a check rather than left in the doc.

    NO VERDICT means the gate could not be asked, which is never the change's fault. Blocking
    here would park prod behind one guest on a NAT network; passing through leaves prod
    exactly where it was before the gate existed.
    """
    assert not staging_blocks(STAGING_NO_VERDICT, blocking=True)


@pytest.mark.parametrize(
    "verdict", [STAGING_PASS, STAGING_SKIPPED, STAGING_NO_VERDICT, None]
)
def test_only_a_rejection_blocks(verdict: str | None) -> None:
    assert not staging_blocks(verdict, blocking=True)


@pytest.mark.parametrize(
    "verdict", [STAGING_PASS, STAGING_REJECTED, STAGING_NO_VERDICT, STAGING_SKIPPED]
)
def test_nothing_blocks_while_the_gate_is_advisory(verdict: str) -> None:
    """The rejecting half of the switch: with blocking off, even a rejection passes through."""
    assert not staging_blocks(verdict, blocking=False)


@pytest.mark.parametrize("deploy_rc,expect_rc", list(itertools.product(RCS, RCS)))
def test_the_verdict_word_and_the_summary_never_disagree(
    deploy_rc: int, expect_rc: int
) -> None:
    """The word main() branches on and the line an operator reads must be the same verdict.

    Two functions reading the same pair of exit codes in their own branch order is how a gate
    comes to block on a verdict whose journal line says something else — and the summary's own
    comments record that its ordering was got wrong once already.
    """
    gated, ungated = {"freshrss"}, set()
    summary = staging_verdict_summary(gated, ungated, deploy_rc, expect_rc)
    word = staging_verdict(deploy_rc, expect_rc)
    expected = {
        STAGING_NO_VERDICT: "NO VERDICT",
        STAGING_REJECTED: "REJECTED",
        STAGING_PASS: "PASS",
    }[word]
    assert expected in summary, f"verdict {word} against summary {summary!r}"


def test_the_override_is_spent_when_it_is_armed(gitops_deploy, state_dir) -> None:
    """Armed, it returns True once and removes itself — so it cannot become permanent."""
    marker = state_dir / "staging_gate_override"
    marker.touch()
    assert deploy_handlers.consume_staging_override(gitops_deploy.STATE)
    assert not marker.exists()
    assert not deploy_handlers.consume_staging_override(gitops_deploy.STATE)


def test_the_override_is_absent_by_default(gitops_deploy, state_dir) -> None:
    """The rejecting half: with nothing armed, nothing is spent and nothing is let through."""
    assert not (state_dir / "staging_gate_override").exists()
    assert not deploy_handlers.consume_staging_override(gitops_deploy.STATE)


_ARMED_BUT_EMPTY = "gate is ARMED but STAGING_SUBSET is empty"


def test_an_armed_gate_with_an_empty_subset_says_so(
    gitops_deploy, tick, settings, capsys
) -> None:
    """`Config.staging_subset` defaults to empty and `load_config` never parses it, so a Config
    built anywhere but `tick_config()` gates nothing while looking armed. The verdict it returns
    is the same SKIPPED an ordinary tick gets, which is why the journal has to tell them apart."""
    config = dataclasses.replace(settings, staging_subset=frozenset())
    verdict = deploy_handlers.consult_staging(
        tick.tools, gitops_deploy.STATE, config, {"sonarr"}, "c0ffee" * 6 + "abcd"
    )
    assert verdict == STAGING_SKIPPED
    assert _ARMED_BUT_EMPTY in capsys.readouterr().out


def test_an_armed_gate_with_a_real_subset_stays_quiet(
    gitops_deploy, tick, settings, capsys
) -> None:
    """The rejecting half. A tick that simply touched no staging service is the ordinary case
    and must not print the misconfiguration line — otherwise the line means nothing."""
    verdict = deploy_handlers.consult_staging(
        tick.tools, gitops_deploy.STATE, settings, {"grafana"}, "c0ffee" * 6 + "abcd"
    )
    out = capsys.readouterr().out
    assert verdict == STAGING_SKIPPED
    assert _ARMED_BUT_EMPTY not in out
    assert "nothing to gate" in out
