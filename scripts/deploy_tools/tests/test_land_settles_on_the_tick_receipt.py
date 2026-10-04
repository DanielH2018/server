"""A deploy-plane landing settles when the receipt covering it records an applied plane.

The deployer records each plane it applied in the receipt of the range it crossed, with the
`--tags` it narrowed to: a tag list, `[]` for the whole play, or `narrowed-to-nothing` when the
range moves no rendered output at all (#3391).

The verdict reads WHETHER a plane was applied, never WHICH tags. `handle_broad` scopes the tags
to the range it crossed, not to this PR's own roles, so a tag comparison would report
`needs-manual-apply` for a PR the tick applied alongside somebody else's merge. That is the
converged-but-unapplied failure arriving from the other side.

Run: uv run pytest scripts/deploy_tools/tests/test_land_settles_on_the_tick_receipt.py
"""

import pytest

from _land_fakes import MERGE_SHA, Fakes, receipt
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome


def _verdict(landing, state: dict, is_ancestor_rc: int = 0) -> Outcome:
    """The verdict for a deploy-plane PR against a deployer holding `state`."""
    ln, _ = landing(Fakes(state=state, is_ancestor_rc=is_ancestor_rc))
    ln.merge_sha = MERGE_SHA
    ln.plane = ""
    ln.self_applied = True
    ln.self_applied_command = "`ansible-playbook ansible/deploy.yml`"
    ln.remaining_setup = ""
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    return exc.value


@pytest.mark.parametrize(
    "tags",
    [[], ["radarr", "sonarr"], ["narrowed-to-nothing"]],
    ids=["whole-play", "narrowed", "narrowed-to-nothing"],
)
def test_land_settles_whatever_tags_the_tick_applied(landing, tags):
    state = receipt({"ansible/deploy.yml": tags})
    assert _verdict(landing, state).verdict == "settled"


@pytest.mark.parametrize(
    "state, is_ancestor_rc",
    [
        (receipt({"ansible/deploy.yml": ["radarr"]}), 1),
        (receipt({}, manual={"k3s": ["kubeconfig"]}), 0),
        ({}, 0),
    ],
    ids=["an-earlier-range", "only-a-role-left-to-a-hand", "no-receipt"],
)
def test_a_receipt_recording_no_apply_of_this_pr_still_needs_a_hand(
    landing, state, is_ancestor_rc
):
    """The rejecting half: a receipt is not a free pass, its `applied` half is.

    The second case is a range the tick crossed and recorded, but only to leave a setup role
    owed: a receipt exists and covers the merge commit, and nothing was applied.
    """
    outcome = _verdict(landing, state, is_ancestor_rc)
    assert outcome.verdict == "needs-manual-apply"
    assert "converged without recording an apply" in outcome.detail
