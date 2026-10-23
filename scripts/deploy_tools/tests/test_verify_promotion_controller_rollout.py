"""`verify` during a Flux hop: kustomize-controller's own replacement posts failures (#6013).

When a promotion changes kustomize-controller's Deployment, the old pod posts `failure` for
`flux` and `soak` as it is replaced, and the new pod posts `success` for `flux` 12 seconds
later. Promote run 39977616897 held 627fd395 `failed=flux,soak` on the first failure. The
replay below is that commit's status timeline.

Run: uv run pytest scripts/deploy_tools/tests/test_verify_promotion_controller_rollout.py
"""

from _promote_fakes import bundle_files
from test_verify_promotion import PRIOR, T, Clock, digests, ks, ok
from test_verify_promotion_storage_rollout import timeline

from deploy_tools.promote_lib import verify_promotion as vp

FLUX = {"flux": ks("flux"), "soak": ks("soak"), "sonarr": ks("sonarr")}
PROD = bundle_files(digests(flux="a", soak="s", sonarr="r"), **FLUX)
# A Flux hop that moves all three tags.
HOP = bundle_files(digests(flux="b", soak="t", sonarr="q"), **FLUX)
# The same three tags moving with `flux` left as it was.
NO_HOP = bundle_files(digests(flux="a", soak="t", sonarr="q"), **FLUX)

REPLACED = {**ok("homelab", "sonarr"), "flux/flux": "failure", "flux/soak": "failure"}
FLUX_UP = {**REPLACED, "flux/flux": "success"}
RECOVERED = ok("homelab", "flux", "soak", "sonarr")


def test_the_627fd395_timeline_reads_good():
    """Both fail at 0s, `flux` succeeds at 12s, the soak re-runs and succeeds at 90s."""
    expected = vp.expected_set(HOP, PROD)
    clock = Clock()
    reads = timeline(clock, [(0, REPLACED), (12, FLUX_UP), (90, RECOVERED)])
    assert vp.verify(reads, T, PRIOR, expected) == vp.Verdict("good")


def test_the_soaks_parting_failure_waits_for_its_fresh_success():
    """`flux` is already `success`; only the soak's failure from the old pod stands."""
    expected = vp.expected_set(HOP, PROD)
    clock = Clock()
    reads = timeline(clock, [(0, FLUX_UP), (45, RECOVERED)])
    assert vp.verify(reads, T, PRIOR, expected) == vp.Verdict("good")
    assert clock.now >= 45


def test_regression_guard_without_flux_in_the_changed_set_the_same_failure_holds():
    """Passes on the code before #6013 too: the tolerance needs a revision expecting `flux`."""
    expected = vp.expected_set(NO_HOP, PROD)
    clock = Clock()
    verdict = vp.verify(timeline(clock, [(0, REPLACED)]), T, PRIOR, expected)
    assert "flux" not in expected.names
    assert verdict == vp.Verdict("failed", ("soak",))
    assert clock.sleeps == 0


def test_a_flux_failure_no_success_follows_holds_at_the_timeout():
    expected = vp.expected_set(HOP, PROD)
    clock = Clock()
    verdict = vp.verify(timeline(clock, [(0, REPLACED)]), T, PRIOR, expected)
    assert verdict == vp.Verdict("failed", ("flux", "soak"))
    assert expected.timeout_s <= clock.now < expected.timeout_s + vp.POLL_S


def test_regression_guard_another_names_failure_stays_final_in_a_flux_hop():
    """Passes on the code before #6013 too: only `flux` and `soak` are tolerated."""
    expected = vp.expected_set(HOP, PROD)
    clock = Clock()
    states = {**FLUX_UP, "flux/sonarr": "failure"}
    verdict = vp.verify(timeline(clock, [(0, states)]), T, PRIOR, expected)
    assert verdict == vp.Verdict("failed", ("soak", "sonarr"))
    assert clock.sleeps == 0
