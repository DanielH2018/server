"""`flux/soak` gates `verify` (D5.3b3): no status keeps it waiting, and a failure holds.

T's bundle holds `flux/soak.yaml`, and the changed set holds `soak` beside the tag it
changes. D5.3b2's observe-only set left `soak` out of what `verify` expected, and these two
fixtures then read `good`. The red half rebuilds that set's subtraction and shows it.

Run: uv run pytest scripts/deploy_tools/tests/test_verify_promotion_soak_gates.py
"""

from dataclasses import replace

import pytest
from _promote_fakes import bundle_files
from test_verify_promotion import PRIOR, T, Clock, digests, ks, ok, reads

from deploy_tools.promote_lib import verify_promotion as vp


def _with_soak(**footprints: str) -> dict:
    found = digests(**footprints)
    found["bento-pdf"]["suspended"] = False
    return found


FLUX = {"bento-pdf": ks("bento-pdf"), "soak": ks("soak", timeout="13m")}
PROD = bundle_files(_with_soak(bento_pdf="a", soak="k"), **FLUX)
TARGET = bundle_files(_with_soak(bento_pdf="b", soak="k"), **FLUX)
SOAK_STATES = {
    "no flux/soak status": ok("homelab", "bento-pdf"),
    "a flux/soak failure": {**ok("homelab", "bento-pdf"), "flux/soak": "failure"},
}


def test_the_fixture_changed_set_holds_soak():
    """Without this the pair below would pass on a set the soak never joined."""
    assert "soak" in vp.expected_set(TARGET, PROD).names


@pytest.mark.parametrize("states", SOAK_STATES.values(), ids=SOAK_STATES)
def test_every_other_status_success_holds_naming_soak(states):
    clock = Clock()
    verdict = vp.verify(
        reads({T: states}, clock), T, PRIOR, vp.expected_set(TARGET, PROD)
    )
    assert verdict == vp.Verdict("failed", ("soak",))


def test_a_flux_soak_success_is_good():
    states = ok("homelab", "bento-pdf", "soak")
    verdict = vp.verify(reads({T: states}), T, PRIOR, vp.expected_set(TARGET, PROD))
    assert verdict == vp.Verdict("good")


@pytest.mark.parametrize("states", SOAK_STATES.values(), ids=SOAK_STATES)
def test_with_soak_left_out_the_same_statuses_read_good(states):
    """The red half: D5.3b2's observe-only set, which subtracted `soak` from the set."""
    gating = vp.expected_set(TARGET, PROD)
    observed = replace(gating, names=gating.names - {"soak"})
    verdict = vp.verify(reads({T: states}), T, PRIOR, observed)
    assert verdict == vp.Verdict("good")
