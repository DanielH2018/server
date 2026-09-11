"""A narrowed `broad_applied` marker settles a deploy-plane landing, whatever its tag slot.

The deployer stopped running `ansible/deploy.yml` unscoped for every deploy-plane range: it
now records `<sha> ansible/deploy.yml <narrowed tags>`, or `<sha> ansible/deploy.yml
narrowed-to-nothing` when the range moves no rendered output at all.

`broad_applied_covers` reads only the SHA and asks whether this PR is an ancestor of it, so
both spellings already work — and the tag slot must STAY unread. Reading it would ask the
wrong question: `handle_broad` scopes the tags to the range it crossed, not to this PR's own
roles, so a tag-slot comparison would report `needs-manual-apply` for a PR the tick applied
alongside somebody else's merge. That is the failure issue #1537 is about, arriving from the
other side.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reads_a_narrowed_broad_applied_marker.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome

# The deploy plane: `self_applied` is True for it, so the marker is what decides.
APPLIED_AT = "f" * 40


def _verdict(landing, marker: str, is_ancestor_rc: int = 0) -> Outcome:
    """The verdict for a deploy-plane PR against a deployer that recorded `marker`."""
    fakes = Fakes(state={"broad_applied": marker}, is_ancestor_rc=is_ancestor_rc)
    ln, _ = landing(fakes)
    ln.merge_sha = MERGE_SHA
    ln.plane = ""
    ln.self_applied = True
    ln.self_applied_command = "`ansible-playbook ansible/deploy.yml`"
    ln.remaining_setup = ""
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    return exc.value


@pytest.mark.parametrize(
    "tag_slot",
    ["", "radarr,sonarr", "narrowed-to-nothing"],
    ids=["whole-play", "narrowed", "narrowed-to-nothing"],
)
def test_every_tag_slot_settles_when_the_apply_contains_the_pr(landing, tag_slot):
    marker = f"{APPLIED_AT} ansible/deploy.yml {tag_slot}".strip()
    assert _verdict(landing, marker).verdict == "settled"


def test_a_narrowed_marker_at_an_earlier_commit_still_needs_a_hand(landing):
    """The rejecting half: the SHA is what decides, so a narrowed marker is not a free pass."""
    outcome = _verdict(
        landing, f"{APPLIED_AT} ansible/deploy.yml radarr", is_ancestor_rc=1
    )
    assert outcome.verdict == "needs-manual-apply"
    assert "converged without recording an apply" in outcome.detail
