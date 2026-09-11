"""Which planes the `broad_applied` marker gates, and which it cannot reach.

WHY THIS TEST EXISTS. Issue #1705 reported that a hand-applied bring-up change leaves
`broad_applied` stale, "so land.sh can report needs-manual-apply for a change that is already
live". The first clause is true and the "so" is not. `broad_applied` is read from exactly two
places, `land_lib/deploy.py` and `land_lib/health_verdict.py`, and BOTH sit behind
`ln.self_applied`. A `_BROAD_MANUAL_PREFIXES` path and a setup role outside
`initial_setup.yml` are both `self_applied is False`, so the marker is never their gate: their
`needs-manual-apply` comes from `plane_note`, which says a HUMAN still owes the apply and is
correct at landing time. The evidence quoted in that issue -- `PR #1700 reaches no service tag,
but is not done` -- is `no_tag_outcome`'s `if ln.plane:` branch, which returns before the
marker is read. The marker's staleness on those planes is real and inert.

WHAT IS ASSERTED is therefore the boundary, both sides of it: the marker decides for the plane
the deployer applies itself, and is never consulted for the planes it never applies. A future
change that widened `self_applied` to cover a manual plane would make the issue's claim true,
and fails here.

Run: uv run pytest scripts/deploy_tools/tests/test_land_manual_plane_is_not_gated_on_broad_applied.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import land_tags
from _land_fakes import MERGE_SHA
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome

# The two planes the deployer never applies: a bring-up playbook, and a setup role that
# `initial_setup.yml` does not include (k3s lives in k3s-bringup.yml).
_BRINGUP = ["ansible/k3s-bringup.yml"]
_UNROUTABLE_ROLE = ["ansible/roles/setup/k3s/defaults/main.yml"]
# The plane it does apply itself, as `initial_setup.yml --tags renovate_agent`. This is the
# shape `broad_applied` was added for (issue #1537).
_ROUTABLE_ROLE = ["ansible/roles/setup/renovate_agent/files/renovate_agent.py"]


def _state_reads(landing, files: list[str]) -> tuple[Outcome, list[str]]:
    """The verdict for a PR with this file list, and every deployer-state key it read."""
    ln, _ = landing(None)
    ln.merge_sha = MERGE_SHA
    ln.plane = land_tags.plane_note(files)
    ln.self_applied = land_tags.self_applied(files)
    ln.self_applied_command = land_tags.self_applied_command(files)
    ln.remaining_setup = ""
    read: list[str] = []
    original = ln.tools.read_state

    def record(root, name):
        read.append(name)
        return original(root, name)

    ln.tools.read_state = record
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    return exc.value, read


@pytest.mark.parametrize("files", [_BRINGUP, _UNROUTABLE_ROLE])
def test_a_manual_plane_never_reads_the_marker(landing, files):
    outcome, read = _state_reads(landing, files)
    assert land_tags.self_applied(files) is False
    assert "broad_applied" not in read
    assert outcome.verdict == "needs-manual-apply"
    assert "is not done" in outcome.detail


def test_the_plane_the_deployer_applies_itself_is_gated_on_the_marker(landing):
    """The other side of the boundary: here an absent marker IS what decides."""
    outcome, read = _state_reads(landing, _ROUTABLE_ROLE)
    assert land_tags.self_applied(_ROUTABLE_ROLE) is True
    assert "broad_applied" in read
    assert outcome.verdict == "needs-manual-apply"
    assert "the tick converged without recording an apply" in outcome.detail
