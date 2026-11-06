"""Step 5: the fallback's derivation, the verdicts of a landing with no tag, the phase's stamps.

Run: uv run pytest scripts/deploy_tools/tests/test_land_deploy.py
"""

import pytest

from _land_fakes import MERGE_SHA, PRIMARY, Fakes, receipt
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.landing import Classification


# The deployer's record of a broad apply CONTAINING this PR. Converging with origin is not
# that: any session's `git merge --ff-only` produces it too.
_APPLIED = receipt({"ansible/initial_setup.yml": ["renovate_agent"]})


def _ready(landing, fakes=None, **opts):
    ln, calls = landing(fakes, **opts)
    ln.merge_sha = MERGE_SHA
    ln.ledger.t_ci = 2.0
    ln.ledger.t_tick = 3.0
    return ln, calls


@pytest.mark.parametrize(
    "fakes, verdict, code",
    [
        (Fakes(plane="initial_setup.yml --tags k3s"), "needs-manual-apply", 1),
        (Fakes(), "nothing-to-deploy", 0),
        (Fakes(self_applied=True, state={"hold_sha": "abc"}), "deploy-failed", 1),
        (Fakes(self_applied=True, state={"behind_since": "x"}), "deferred", 75),
        (Fakes(self_applied=True, state=_APPLIED), "settled", 0),
        (
            Fakes(
                self_applied=True,
                remaining_setup="daniel-server, daniel-pi",
                state=_APPLIED,
            ),
            "needs-manual-apply",
            1,
        ),
        # Converged is not applied: every marker land.sh reads says settled, and the tick
        # recorded no apply covering the PR.
        (Fakes(self_applied=True, state={}), "needs-manual-apply", 1),
    ],
)
def test_no_tag_outcomes(landing, fakes, verdict, code):
    ln, _ = _ready(landing, fakes)
    ln.classification = Classification(
        plane=fakes.plane,
        self_applied=fakes.self_applied,
        self_applied_command=fakes.self_applied_command,
        remaining_setup=fakes.remaining_setup,
    )
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    assert (exc.value.rc, exc.value.verdict) == (code, verdict)
    if verdict == "needs-manual-apply" and fakes.remaining_setup:
        assert "other hosts still need it" in exc.value.detail
    assert ln.ledger.cause == ("tick-held" if fakes.state.get("hold_sha") else "")


def test_a_held_tick_names_the_clear_command_with_the_full_sha(landing, capsys):
    held = "deadbeef" * 5
    ln, _ = _ready(landing, Fakes(self_applied=True, state={"hold_sha": held}))
    ln.classification = Classification(plane="", self_applied=True)
    with pytest.raises(Outcome):
        deploy.no_tag_outcome(ln)
    assert f"gitops_state.py clear-hold {held}`" in capsys.readouterr().out


def test_the_diff_fallback_derives_after_the_tick(landing):
    ln, calls = _ready(landing, Fakes(changed="sonarr"), since="abc")
    ln.needs_diff = True
    deploy.derive_from_diff(ln)
    assert ln.resolved_tags == ["sonarr"]
    assert ("deploy_tags", ("changed", "abc"), {"cwd": PRIMARY}) in calls


def test_the_diff_fallback_drops_an_empty_tag_element(landing):
    """A doubled or trailing comma is not a service, so it is dropped here rather than read
    as a tag miss that ends the landing."""
    ln, _ = _ready(landing, Fakes(changed="sonarr,,radarr,"), since="abc")
    ln.needs_diff = True
    deploy.derive_from_diff(ln)
    assert ln.resolved_tags == ["sonarr", "radarr"]


def test_deploy_phase_stamps_the_ledger(landing):
    ln, _ = _ready(landing)
    ln.flux_tags = ["sonarr"]
    deploy.deploy_phase(ln)
    assert ln.ledger.tags_label == "sonarr" and ln.ledger.t_deploy is not None


def test_an_unreadable_deployer_state_is_not_settled(landing, capsys):
    """Finding 13's rejecting half at the phase. `converged` here would print `settled`."""
    ln, _ = _ready(landing, Fakes(self_applied=True))
    ln.classification = Classification(
        self_applied=True,
    )
    ln.tools.read_state = lambda root, name: None
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "needs-manual-apply")
    assert "could not be read" in capsys.readouterr().out
