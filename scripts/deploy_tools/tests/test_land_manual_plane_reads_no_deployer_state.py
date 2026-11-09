"""A landing reads none of the deployer's markers, whichever plane the PR reaches.

WHY THIS TEST EXISTS. land.sh starts no tick and reads none of its state (H7.49a). A plane the
deployer never applies, a bring-up playbook or a setup role outside `initial_setup.yml`, still
ends `needs-manual-apply`, because `plane_note` says a HUMAN owes the apply. A setup role the
deployer's timer applies ends `nothing-to-deploy`, and the PR's author reads the host for the
outcome. Neither path asks the deployer's state directory anything.

Run: uv run pytest scripts/deploy_tools/tests/test_land_manual_plane_reads_no_deployer_state.py
"""

import pytest

from deploy_tools.land_lib import deploy, land_tags
from deploy_tools.land_lib.landing import Classification
from deploy_tools.land_lib.outcome import Outcome
from _land_fakes import MERGE_SHA

# The two planes the deployer never applies: a bring-up playbook, and a setup role that
# `initial_setup.yml` does not include (k3s lives in k3s-bringup.yml).
_BRINGUP = ["ansible/k3s-bringup.yml"]
_UNROUTABLE_ROLE = ["ansible/roles/setup/k3s/tasks/node.yml"]
# The plane the deployer's timer applies itself, as `initial_setup.yml --tags renovate_agent`.
_ROUTABLE_ROLE = ["ansible/roles/setup/renovate_agent/files/renovate_agent.py"]


def _verdict(landing, files: list[str]) -> tuple[Outcome, list[str]]:
    """The verdict for a PR with this file list, and every deployer-state key it read."""
    ln, _ = landing(None)
    ln.merge_sha = MERGE_SHA
    ln.classification = Classification(
        plane=land_tags.plane_note(files, owed=land_tags.forecast_owed(files))
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
def test_a_manual_plane_still_needs_a_hand_and_reads_no_state(landing, files):
    outcome, read = _verdict(landing, files)
    assert outcome.verdict == "needs-manual-apply"
    assert "is not done" in outcome.detail
    assert read == []


def test_a_setup_role_the_timer_applies_is_nothing_to_deploy_and_reads_no_state(
    landing,
):
    outcome, read = _verdict(landing, _ROUTABLE_ROLE)
    assert outcome.verdict == "nothing-to-deploy"
    assert outcome.rc == 0
    assert read == []


def test_an_unroutable_setup_role_is_told_to_clear_the_deployers_marker():
    """The tick merged this PR and recorded the role, so applying it is only half the job.

    A role applied by hand with its `manual_plane` line left behind pages **GitOps Deploy —
    Status** six hours later over work that is already live.
    """
    note = land_tags.plane_note(
        ["ansible/roles/setup/k3s/defaults/main.yml"],
        owed=land_tags.forecast_owed(["ansible/roles/setup/k3s/defaults/main.yml"]),
    )
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
        ["ansible/k3s-bringup.yml", "ansible/roles/setup/k3s/defaults/main.yml"],
        owed=frozenset({"k3s"}),
    )
    assert "ansible/k3s-bringup.yml --tags k3s" in note, (
        "the hand command is still printed"
    )
    assert "clear-owed manual_plane" not in note
