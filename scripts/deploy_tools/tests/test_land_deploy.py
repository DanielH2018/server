"""Step 4: the fallback's derivation, the verdicts of a landing with no tag, the phase's stamps.

Run: uv run pytest scripts/deploy_tools/tests/test_land_deploy.py
"""

import pytest

from _land_fakes import MERGE_SHA, PRIMARY, Fakes
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.landing import Classification


def _ready(landing, fakes=None, **opts):
    ln, calls = landing(fakes, **opts)
    ln.merge_sha = MERGE_SHA
    ln.ledger.t_ci = 2.0
    return ln, calls


@pytest.mark.parametrize(
    "fakes, verdict, code",
    [
        (Fakes(plane="initial_setup.yml --tags k3s"), "needs-manual-apply", 1),
        (Fakes(), "nothing-to-deploy", 0),
    ],
)
def test_no_tag_outcomes(landing, fakes, verdict, code):
    ln, _ = _ready(landing, fakes)
    ln.classification = Classification(plane=fakes.plane)
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    assert (exc.value.rc, exc.value.verdict) == (code, verdict)
    assert ln.ledger.cause == ""


def test_the_diff_fallback_derives_in_the_primary_checkout(landing):
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


def test_deploy_phase_stamps_the_ledger_and_the_gated_tree(landing):
    ln, _ = _ready(landing)
    ln.flux_tags = ["sonarr"]
    deploy.deploy_phase(ln)
    assert ln.ledger.tags_label == "sonarr" and ln.ledger.t_deploy is not None
    assert ln.deployed_at == MERGE_SHA
