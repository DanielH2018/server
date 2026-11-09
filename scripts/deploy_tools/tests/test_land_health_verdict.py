"""Step 6: the health gate, the half `ansible-playbook` exiting 0 cannot speak to.

Run: uv run pytest scripts/deploy_tools/tests/test_land_health_verdict.py
"""

from dataclasses import replace
from pathlib import Path

import pytest

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.deploy_lib.detach_notify import GateResult
from deploy_tools.land_lib import health_verdict, outcome
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.landing import Classification


def _deployed(landing, fakes=None):
    ln, calls = landing(fakes)
    ln.merge_sha, ln.flux_tags = MERGE_SHA, ["sonarr"]
    ln.classification = Classification(
        plane=(fakes or Fakes()).plane,
    )
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


def _gates(ln, *results):
    """Answer the landing's successive gates with `results`, which may be unreadable."""
    answers = iter(results)
    ln.tools = replace(ln.tools, gate=lambda tags, cwd=None: next(answers))


NOT_GRADED = ["sonarr: not graded -- probe.py health could not read its store"]


def test_an_unreadable_gate_defers(landing, capsys):
    """#5543: no reading is no failure."""
    ln, _ = _deployed(landing, Fakes())
    _gates(ln, GateResult(False, NOT_GRADED, unreadable=True))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "deferred")
    assert "could not read its store" in exc.value.detail
    out = capsys.readouterr().out
    assert outcome.UNREADABLE_GATE_NOTE in out
    assert outcome.STALE_GATE_NOTE not in out


def test_needs_manual_apply_when_a_plane_remains(landing, capsys):
    ln, _ = _deployed(landing, Fakes(plane="initial_setup.yml --tags k3s"))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert exc.value.verdict == "needs-manual-apply"
    assert "STILL UNAPPLIED" in capsys.readouterr().out


def test_the_gate_renders_the_tree_that_was_deployed(landing):
    """`probe.py health` enumerates workloads by rendering the role's manifests from its cwd.

    A landing that deployed a snapshot of its merge commit has not moved the primary checkout,
    so a role that commit ADDS enumerates nothing there and the whole gate reads `skipped` --
    green, on the first landing of a new service.
    """
    ln, calls = _deployed(landing)
    ln.deployed_at = MERGE_SHA
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    assert next(c for c in calls if c[0] == "snapshot")[1][1] == MERGE_SHA
    assert next(c for c in calls if c[0] == "gate")[2] == {"cwd": Path("/snap")}


TIP = "e" * 40


def test_the_gate_renders_origin_master_when_it_contains_the_deployed_commit(landing):
    """#7816: the probe refuses a role master changed after its tree's HEAD, so a gate pinned
    to the merge commit deferred on every re-run once a later commit touched a landed role."""
    ln, calls = _deployed(landing, Fakes(tip=TIP))
    ln.deployed_at = MERGE_SHA
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    assert next(c for c in calls if c[0] == "snapshot")[1][1] == TIP


def test_the_gate_keeps_the_deployed_commit_when_origin_master_lacks_it(landing):
    ln, calls = _deployed(landing, Fakes(tip=TIP, is_ancestor_of={TIP: 1}))
    ln.deployed_at = MERGE_SHA
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    assert next(c for c in calls if c[0] == "snapshot")[1][1] == MERGE_SHA


def test_a_landing_with_no_promoted_tag_gates_the_primary(landing):
    """CLEAN half: no snapshot, and the probe runs in the primary the tick fast-forwarded.

    Not in land.sh's own checkout, which may predate the merge and so see none of a role
    the PR adds: `check_one` would read `skipped` and the landing `settled` (#5754).
    """
    ln, calls = _deployed(landing)
    ln.flux_tags = []
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    assert "snapshot" not in [c[0] for c in calls]
    assert next(c for c in calls if c[0] == "gate")[2] == {"cwd": ln.opts.primary}


def test_a_snapshot_that_could_not_be_taken_fails_the_gate(landing, capsys):
    """FLAGGED half: no tree that can answer, so no verdict -- never a green one.

    Degrading to the primary is what the first cut did, and the primary is the one tree that
    cannot see a role the deployed commit ADDS: `check_one` returns `skipped` and the landing
    reads `settled` having gated nothing. That is the whole gap this path exists to close, so
    here it fails closed instead.
    """
    ln, _calls = _deployed(landing, Fakes(gate_snapshot=None, merge_applied_rc=1))
    ln.deployed_at = MERGE_SHA
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert exc.value.verdict == "unhealthy"
    assert "does not carry it yet" in capsys.readouterr().out


def test_a_snapshot_that_failed_over_a_primary_that_has_the_commit_gates_there(
    landing, capsys
):
    """CLEAN half: an equivalent-or-newer primary answers the same question, so it may.

    Without this arm a tick that converged the checkout a second before the gate would turn a
    healthy landing unhealthy over a worktree that was never needed.
    """
    ln, calls = _deployed(landing, Fakes(gate_snapshot=None, merge_applied_rc=0))
    ln.deployed_at = MERGE_SHA
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    # The primary itself, not land.sh's own checkout, which may predate the commit (#5744).
    assert next(c for c in calls if c[0] == "gate")[2] == {"cwd": ln.opts.primary}
    assert "already carries it" in capsys.readouterr().out


def test_a_failed_snapshot_asks_whether_the_primary_carries_the_retargeted_commit(
    landing,
):
    """After a retarget, `deployed_at` is newer than the merge commit, and a primary holding
    only the merge commit is not an equivalent tree to gate from."""
    ln, calls = _deployed(landing, Fakes(gate_snapshot=None, merge_applied_rc=1))
    ln.deployed_at = "d" * 40
    with pytest.raises(Outcome):
        health_verdict.health(ln)
    asked = [c[1] for c in calls if c[0] == "git" and c[1][:1] == ("merge-base",)]
    assert ("merge-base", "--is-ancestor", "d" * 40, "HEAD") in asked
