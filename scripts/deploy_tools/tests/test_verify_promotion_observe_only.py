"""`verify`'s observe-only set (D5.3b2): `flux/soak` neither keeps it waiting nor holds.

T's bundle holds `flux/soak.yaml`, and the changed set holds `soak` beside the tag it
changes. A `good` verdict is what moves `last-good` and writes no hold: `settle` acts on it,
which `test_promote_settle.py::test_a_good_verdict_moves_last_good_to_sha_t_and_writes_no_hold`
covers. The red half empties the set, as D5.3b3 will, and the same two fixtures then hold.

Run: uv run pytest scripts/deploy_tools/tests/test_verify_promotion_observe_only.py
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
    assert (
        "soak"
        in vp.changed_set(
            vp.bundle_digests(TARGET), vp.bundle_digests(PROD), {"soak": []}
        ).names
    )


@pytest.mark.parametrize("states", SOAK_STATES.values(), ids=SOAK_STATES)
def test_every_other_status_success_is_good_whatever_flux_soak_reads(states):
    expected = vp.expected_set(TARGET, PROD)
    verdict = vp.verify(reads({T: states}), T, PRIOR, expected)
    assert verdict == vp.Verdict("good")


@pytest.mark.parametrize("states", SOAK_STATES.values(), ids=SOAK_STATES)
def test_with_the_set_empty_the_same_statuses_hold_naming_soak(states):
    """What `expected_set` returns once D5.3b3 deletes the set: `soak` expected like a tag."""
    observed = vp.expected_set(TARGET, PROD)
    expected = replace(observed, names=observed.names | vp.OBSERVE_ONLY)
    clock = Clock()
    verdict = vp.verify(reads({T: states}, clock), T, PRIOR, expected)
    assert verdict == vp.Verdict("failed", ("soak",))
