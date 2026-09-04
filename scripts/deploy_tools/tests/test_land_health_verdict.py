"""Step 6: the health gate, the half `ansible-playbook` exiting 0 cannot speak to.

Run: uv run pytest scripts/deploy_tools/tests/test_land_health_verdict.py
"""

from __future__ import annotations

import pytest

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.land_lib import health_verdict
from deploy_tools.land_lib.outcome import Outcome


def _deployed(landing, fakes=None):
    ln, calls = landing(fakes)
    ln.merge_sha, ln.tags = MERGE_SHA, "sonarr"
    ln.plane = (fakes or Fakes()).plane
    ln.self_applied = (fakes or Fakes()).self_applied
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
        ({}, "settled", 0, ""),
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
        landing, Fakes(self_applied=True, remaining_setup="daniel-server, daniel-pi")
    )
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "other hosts still need it" in exc.value.detail
    assert "it also reaches: daniel-server, daniel-pi" in capsys.readouterr().out


def test_no_remaining_hosts_still_settles(landing):
    """The reject half: the #723 shape, where the tick's host is the only one reached."""
    ln, _ = _deployed(landing, Fakes(self_applied=True, remaining_setup=""))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (0, "settled")
