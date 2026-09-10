"""Step 6: the health gate, the half `ansible-playbook` exiting 0 cannot speak to.

Run: uv run pytest scripts/deploy_tools/tests/test_land_health_verdict.py
"""

import pytest

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.land_lib import health_verdict
from deploy_tools.land_lib.outcome import Outcome


# The deployer's record of a broad apply that CONTAINS this PR. A self-applied landing needs
# it before it may settle: `behind_since` empty proves only that local == origin (issue #1537).
APPLIED = {"broad_applied": f"{MERGE_SHA} ansible/initial_setup.yml renovate_agent"}


def _deployed(landing, fakes=None):
    ln, calls = landing(fakes)
    ln.merge_sha, ln.resolved_tags = MERGE_SHA, ["sonarr"]
    ln.plane = (fakes or Fakes()).plane
    ln.self_applied = (fakes or Fakes()).self_applied
    ln.self_applied_command = (fakes or Fakes()).self_applied_command
    ln.remaining_setup = (fakes or Fakes()).remaining_setup
    return ln, calls


def test_settled_after_a_healthy_deploy(landing, capsys):
    ln, calls = _deployed(landing)
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (0, "settled")
    assert exc.value.detail == f"PR #999, {MERGE_SHA}, tags: sonarr"
    assert next(c for c in calls if c[0] == "gate")[1] == (["sonarr"],)
    assert "sonarr: healthy" in capsys.readouterr().out


def test_unhealthy_when_the_gate_fails(landing):
    ln, _ = _deployed(landing, Fakes(gate=(False, ["sonarr: unhealthy"])))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "unhealthy")


def test_needs_manual_apply_when_a_plane_remains(landing, capsys):
    ln, _ = _deployed(landing, Fakes(plane="initial_setup.yml --tags k3s"))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert exc.value.verdict == "needs-manual-apply"
    assert "STILL UNAPPLIED" in capsys.readouterr().out


@pytest.mark.parametrize(
    "state, verdict, code, cause",
    [
        ({"hold_sha": "abc"}, "deploy-failed", 1, "tick-held"),
        ({"behind_since": "x"}, "deferred", 75, ""),
        (APPLIED, "settled", 0, ""),
    ],
)
def test_a_self_applied_half_reads_the_deployers_state(
    landing, state, verdict, code, cause
):
    ln, _ = _deployed(landing, Fakes(self_applied=True, state=state))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (code, verdict)
    assert ln.ledger.cause == cause


def test_an_ordinary_service_pr_ignores_the_deployers_state(landing):
    """behind_since is somebody else's pending merge when the tick does not apply this PR."""
    ln, _ = _deployed(landing, Fakes(state={"behind_since": "x"}))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert exc.value.verdict == "settled"


def test_a_self_applied_role_that_reaches_other_hosts_is_not_settled(landing, capsys):
    """Issue #1009: the services are live, the tick converged, and two hosts are still owed."""
    ln, _ = _deployed(
        landing,
        Fakes(
            self_applied=True,
            remaining_setup="daniel-server, daniel-pi",
            state=APPLIED,
        ),
    )
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "other hosts still need it" in exc.value.detail
    assert "it also reaches: daniel-server, daniel-pi" in capsys.readouterr().out


def test_no_remaining_hosts_still_settles(landing):
    """The reject half: the #723 shape, where the tick's host is the only one reached."""
    ln, _ = _deployed(
        landing, Fakes(self_applied=True, remaining_setup="", state=APPLIED)
    )
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (0, "settled")


# ── converged is not applied (issue #1537) ────────────────────────────────────────────────


def test_a_converged_tick_that_recorded_no_apply_is_not_settled(landing, capsys):
    """PR #1529's shape: something else fast-forwarded the checkout, so the tick applied
    nothing and will never see the range again — and every marker land.sh used to read is in
    the settled state."""
    ln, _ = _deployed(landing, Fakes(self_applied=True, state={}))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    out = capsys.readouterr().out
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "recorded no broad apply covering this PR" in out
    assert "`ansible-playbook ansible/initial_setup.yml --tags x`" in out


def test_an_apply_recorded_at_a_commit_without_this_pr_is_not_settled(landing):
    """The marker exists but names an EARLIER apply — coverage, not presence, decides."""
    ln, _ = _deployed(
        landing,
        Fakes(
            self_applied=True,
            state={"broad_applied": "0000000 ansible/deploy.yml "},
            is_ancestor_rc=1,
        ),
    )
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")


def test_an_ordinary_service_pr_never_asks_about_a_broad_apply(landing):
    """`broad_applied` speaks to a landing only when the tick applies part of THIS PR."""
    ln, _ = _deployed(landing, Fakes(self_applied=False, state={}))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (0, "settled")


def test_an_unreadable_deployer_state_is_not_settled(landing, capsys):
    """Finding 13's rejecting half at step 6: services live, the tick's own half unknown."""
    ln, _ = _deployed(landing, Fakes(self_applied=True))
    ln.tools.read_state = lambda root, name: None
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "could not be read" in capsys.readouterr().out
