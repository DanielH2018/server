"""A hand-applied plane beside a tick half that recorded no apply (issue #2579).

Both `needs-manual-apply` sites end at their plane arm, which sits above the branch that
reads the deployer's own state. A PR carrying both therefore never printed `Apply it:` for
the tick's half, even in the #1537 shape: the tick converged, so no later tick applies that
range and nothing else says so. One file for both sites, because the bug is the arm ordering
they share -- the same shape as `test_land_plane_keeps_the_remaining_hosts_line.py`.

Run: uv run pytest scripts/deploy_tools/tests/test_land_plane_keeps_the_tick_half_line.py
"""

import pytest

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.land_lib import deploy, health_verdict
from deploy_tools.land_lib.outcome import Outcome


_PLANE = "`ansible-playbook ansible/k3s-bringup.yml --tags backup-health`"
_COMMAND = "`ansible-playbook ansible/initial_setup.yml --tags renovate_agent`"
# The deployer's record of a broad apply containing this PR: the tick's half is settled, so
# the reject half below must print no apply command for it.
_APPLIED = {"broad_applied": f"{MERGE_SHA} ansible/initial_setup.yml renovate_agent"}


def _landing_at(landing, fakes):
    ln, _ = landing(fakes)
    ln.merge_sha, ln.resolved_tags = MERGE_SHA, ["sonarr"]
    ln.plane = fakes.plane
    ln.self_applied = fakes.self_applied
    ln.self_applied_command = fakes.self_applied_command
    return ln


@pytest.mark.parametrize("site", [health_verdict.health, deploy.no_tag_outcome])
def test_a_plane_does_not_swallow_the_tick_half_command(landing, capsys, site):
    """The #1537 shape under a plane: the tick converged with no apply recorded."""
    ln = _landing_at(
        landing,
        Fakes(plane=_PLANE, self_applied=True, self_applied_command=_COMMAND),
    )
    with pytest.raises(Outcome) as exc:
        site(ln)
    assert exc.value.verdict == "needs-manual-apply"
    out = capsys.readouterr().out
    assert "k3s-bringup.yml" in out
    assert f"Apply it: {_COMMAND}" in out


@pytest.mark.parametrize("site", [health_verdict.health, deploy.no_tag_outcome])
def test_a_plane_beside_an_applied_tick_half_prints_only_the_plane(
    landing, capsys, site
):
    """The reject half: a recorded broad apply covers this PR, so its half is done."""
    ln = _landing_at(
        landing,
        Fakes(
            plane=_PLANE,
            self_applied=True,
            self_applied_command=_COMMAND,
            state=_APPLIED,
        ),
    )
    with pytest.raises(Outcome):
        site(ln)
    out = capsys.readouterr().out
    assert "k3s-bringup.yml" in out
    assert "Apply it:" not in out
