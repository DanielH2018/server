"""What a landing prints when a suspended promotion wait leaves the health gate no tags.

Every boundary but the gate is a fake from `_land_fakes.py`. The gate is `Tools()`'s real one,
because `build_tools`'s fake returns fixed lines and never prints the gate's own reason. With no
tags it reaches no probe.

Run: uv run pytest scripts/deploy_tools/tests/test_land_suspended_gate_reason.py
"""

from dataclasses import replace

import pytest

from _land_fakes import MERGE_SHA, Fakes, receipt
from deploy_tools.land_lib import health_verdict, promotion
from deploy_tools.land_lib.host_wait import HostTarget
from deploy_tools.land_lib.landing import Classification
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.tools import Tools
from deploy_tools.promote_lib.wait import WaitResult

SUSPENDED = WaitResult(
    "suspended",
    "promote-suspended",
    "bento-pdf is suspended",
    suspended=("bento-pdf",),
    tags=("bento-pdf",),
)

# The two ways a suspended wait goes on to the health step: `wait_on_promotion` returns for a
# changed host package and for a PR the tick applies part of.
_REACHES_THE_GATE = [
    pytest.param(Classification(), [HostTarget("daniel-pi", armed=True)], id="host"),
    pytest.param(Classification(self_applied=True), [], id="tick-half"),
]


def _suspended_landing(
    landing, capsys, classification, host_targets
) -> tuple[Outcome, str]:
    """Run the promotion wait and the health step; the outcome, and everything printed."""
    ln, _ = landing(
        Fakes(
            owners={MERGE_SHA: {"bento-pdf": "flux"}},
            land_wait=[SUSPENDED],
            state=receipt({"ansible/initial_setup.yml": ["renovate_agent"]}),
        )
    )
    ln.merge_sha = MERGE_SHA
    ln.classification = classification
    ln.host_targets = host_targets
    ln.tools = replace(ln.tools, gate=Tools().gate)
    promotion.wait_on_promotion(ln)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    exc.value.emit()
    cap = capsys.readouterr()
    return exc.value, cap.out + cap.err


@pytest.mark.parametrize("classification, host_targets", _REACHES_THE_GATE)
def test_a_suspended_wait_settles_with_no_missing_tags_reason(
    landing, capsys, classification, host_targets
):
    """#7380: no `--tags` flag was missing here, and the empty gate list still reads settled."""
    outcome, out = _suspended_landing(landing, capsys, classification, host_targets)
    assert (outcome.rc, outcome.verdict) == (0, "settled"), out
    assert "no --tags given" not in out, out


@pytest.mark.parametrize("classification, host_targets", _REACHES_THE_GATE)
def test_a_suspended_wait_credits_no_ansible_exit_code(
    landing, capsys, classification, host_targets
):
    """#7380: no Ansible ran on this path, so nothing reads its exit code."""
    _, out = _suspended_landing(landing, capsys, classification, host_targets)
    assert "ansible exit code" not in out, out
