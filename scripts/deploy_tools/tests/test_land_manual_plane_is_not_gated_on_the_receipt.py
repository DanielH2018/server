"""Which planes the tick's receipt gates, and which it cannot reach.

WHY THIS TEST EXISTS. A hand-applied bring-up change gets no receipt, but that cannot make
land.sh report needs-manual-apply for a change that is already live. The receipt's `applied` half
is read from exactly two places, `land_lib/deploy.py` and `land_lib/health_verdict.py`, and BOTH sit behind
`ln.classification.self_applied`. A `_BROAD_MANUAL_PREFIXES` path and a setup role outside `initial_setup.yml` are
both `self_applied is False`, so the marker is never their gate: their `needs-manual-apply` comes
from `plane_note`, which says a HUMAN still owes the apply and is correct at landing time. The
`reaches no service tag, but is not done` line is `no_tag_outcome`'s `if ln.plane:` branch, which
returns before the receipt is read. Its absence on those planes is real and inert.

WHAT IS ASSERTED is therefore the boundary, both sides of it: the receipt decides for the plane
the deployer applies itself, and is never consulted for the planes it never applies. A future
change that widened `self_applied` to cover a manual plane would make the issue's claim true,
and fails here.

Run: uv run pytest scripts/deploy_tools/tests/test_land_manual_plane_is_not_gated_on_the_receipt.py
"""

import pytest


import land_tags
from _land_fakes import MERGE_SHA, RECEIPTS
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.landing import Classification

# The two planes the deployer never applies: a bring-up playbook, and a setup role that
# `initial_setup.yml` does not include (k3s lives in k3s-bringup.yml).
_BRINGUP = ["ansible/k3s-bringup.yml"]
_UNROUTABLE_ROLE = ["ansible/roles/setup/k3s/defaults/main.yml"]
# The plane it does apply itself, as `initial_setup.yml --tags renovate_agent`. This is the
# shape the receipt's `applied` half exists for.
_ROUTABLE_ROLE = ["ansible/roles/setup/renovate_agent/files/renovate_agent.py"]


def _state_reads(landing, files: list[str]) -> tuple[Outcome, list[str]]:
    """The verdict for a PR with this file list, and every deployer-state key it read."""
    ln, _ = landing(None)
    ln.merge_sha = MERGE_SHA
    ln.classification = Classification(
        plane=land_tags.plane_note(files),
        self_applied=land_tags.self_applied(files),
        self_applied_command=land_tags.self_applied_command(files),
        remaining_setup="",
    )
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
def test_a_manual_plane_never_reads_the_receipt(landing, files):
    outcome, read = _state_reads(landing, files)
    assert land_tags.self_applied(files) is False
    assert RECEIPTS not in read
    assert outcome.verdict == "needs-manual-apply"
    assert "is not done" in outcome.detail


def test_the_plane_the_deployer_applies_itself_is_gated_on_the_receipt(landing):
    """The other side of the boundary: here an absent receipt IS what decides."""
    outcome, read = _state_reads(landing, _ROUTABLE_ROLE)
    assert land_tags.self_applied(_ROUTABLE_ROLE) is True
    assert RECEIPTS in read
    assert outcome.verdict == "needs-manual-apply"
    assert "the tick converged without recording an apply" in outcome.detail


def test_an_unroutable_setup_role_is_told_to_clear_the_deployers_marker():
    """The tick merged this PR and recorded the role, so applying it is only half the job.

    A role applied by hand with its `manual_plane` line left behind pages **GitOps Deploy —
    Status** six hours later over work that is already live.
    """
    note = land_tags.plane_note(["ansible/roles/setup/k3s/defaults/main.yml"])
    assert "ansible/k3s-bringup.yml --tags k3s" in note
    assert "clear-owed manual_plane" in note


def test_a_bringup_playbook_is_not_told_to_clear_a_marker():
    """The rejecting half: the tick parks on these, so no marker was ever written.

    Printing a clear command here would send an operator after a file that does not exist.
    The range carries an unapplyable ROLE as well, which is the case that decides the rule:
    `deploy_defer.parks_the_tick` gives `cs.broad_manual` priority, so the whole range parks
    and `deploy_defer.record` never runs. A bring-up playbook ALONE names no role and would
    pass this assertion whatever the condition said.
    """
    note = land_tags.plane_note(
        ["ansible/k3s-bringup.yml", "ansible/roles/setup/k3s/defaults/main.yml"]
    )
    assert "ansible/k3s-bringup.yml --tags k3s" in note, (
        "the hand command is still printed"
    )
    assert "clear-owed manual_plane" not in note
