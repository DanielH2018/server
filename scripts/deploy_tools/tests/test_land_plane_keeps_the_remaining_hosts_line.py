"""A hand-applied plane beside a self-applied role that reaches other hosts (issue #2569).

Both `needs-manual-apply` sites end at their plane arm, which sits above the branch that owns
the remaining-hosts verdict. PR #2568 landed that way on 2026-09-25: the `k3s-bringup.yml`
line printed, the `initial_setup` library's did not, and daniel-server and daniel-pi kept the
old `kuma-push-lib.sh`. One file for both sites, because the bug is the arm ordering they
share.

Run: uv run pytest scripts/deploy_tools/tests/test_land_plane_keeps_the_remaining_hosts_line.py
"""

import pytest

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.land_lib import deploy, health_verdict
from deploy_tools.land_lib.outcome import Outcome


_PLANE = "`ansible-playbook ansible/k3s-bringup.yml --tags backup-health`"
_REMAINING = (
    "`initial_setup` also reaches daniel-server: `cmd-server`; "
    "`initial_setup` also reaches daniel-pi: `cmd-pi`"
)
# The deployer's record of a broad apply containing this PR, so the tick's own half settles
# and only the two remediations are left open.
_APPLIED = {"broad_applied": f"{MERGE_SHA} ansible/initial_setup.yml initial_setup"}


def _landing_at(landing, fakes):
    ln, _ = landing(fakes)
    ln.merge_sha, ln.resolved_tags = MERGE_SHA, ["sonarr"]
    ln.plane = fakes.plane
    ln.self_applied = fakes.self_applied
    ln.self_applied_command = fakes.self_applied_command
    ln.remaining_setup = fakes.remaining_setup
    return ln


@pytest.mark.parametrize("site", [health_verdict.health, deploy.no_tag_outcome])
def test_a_plane_does_not_swallow_the_remaining_hosts_line(landing, capsys, site):
    """PR #2568's shape: both halves are owed a hand, so both commands print."""
    ln = _landing_at(
        landing,
        Fakes(
            plane=_PLANE,
            self_applied=True,
            remaining_setup=_REMAINING,
            state=_APPLIED,
        ),
    )
    with pytest.raises(Outcome) as exc:
        site(ln)
    assert exc.value.verdict == "needs-manual-apply"
    out = capsys.readouterr().out
    assert "k3s-bringup.yml" in out
    assert "cmd-server" in out
    assert "cmd-pi" in out


@pytest.mark.parametrize("site", [health_verdict.health, deploy.no_tag_outcome])
def test_a_plane_with_no_remaining_hosts_prints_only_the_plane(landing, capsys, site):
    """The reject half: no host is owed the role, so no second remediation appears -- an
    empty `remaining_setup` must not print a remediation naming nothing."""
    ln = _landing_at(landing, Fakes(plane=_PLANE))
    with pytest.raises(Outcome):
        site(ln)
    out = capsys.readouterr().out
    assert "k3s-bringup.yml" in out
    assert "hosts this tick never touched" not in out
