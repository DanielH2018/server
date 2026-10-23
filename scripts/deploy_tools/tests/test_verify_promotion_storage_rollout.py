"""`verify` during a Longhorn hop: a claim's failure is provisional while the manager rolls (#5997).

Longhorn's admission webhook is served by the longhorn-manager pods, so every Kustomization
holding a PersistentVolumeClaim fails its dry run while the manager DaemonSet replaces them,
then recovers on its next retry. Promote run 39954707757 held 61c16826 on that first
`failure`. The replay below is that commit's status timeline.

Run: uv run pytest scripts/deploy_tools/tests/test_verify_promotion_storage_rollout.py
"""

from _promote_fakes import bundle_files
from test_verify_promotion import PRIOR, T, Clock, digests, ks, ok

from deploy_tools.promote_lib import verify_promotion as vp

FLUX = {
    "longhorn": ks("longhorn"),
    "pi-peer-backup": ks("pi-peer-backup"),
    "mosquitto": ks("mosquitto"),
}
PROD = bundle_files(digests(longhorn="a", pi_peer_backup="p", mosquitto="m"), **FLUX)
# A Longhorn hop that also moves the two claim-holding tags, so all three are expected.
HOP = bundle_files(digests(longhorn="b", pi_peer_backup="q", mosquitto="n"), **FLUX)
# The same two tags moving with `longhorn` left as it was.
NO_HOP = bundle_files(digests(longhorn="a", pi_peer_backup="q", mosquitto="n"), **FLUX)

CLAIMS_FAILED = {
    **ok("homelab"),
    "flux/pi-peer-backup": "failure",
    "flux/mosquitto": "failure",
}
LONGHORN_UP = {**CLAIMS_FAILED, "flux/longhorn": "success"}
RECOVERED = ok("homelab", "longhorn", "pi-peer-backup", "mosquitto")


def timeline(clock: Clock, steps: list[tuple[float, dict[str, str]]]) -> vp.StatusReads:
    """Reads that answer the last of `steps` whose start time the clock has reached."""

    def statuses(sha, since):
        assert sha == T
        return dict([states for at, states in steps if clock.now >= at][-1])

    return vp.StatusReads(statuses, clock.sleep, clock)


def test_the_grace_and_the_rollout_bound_derive_from_the_bundle_on_a_hop_only():
    """`retryInterval: 2m` gives a 240s grace, and `longhorn`'s `10m` timeout adds 600s."""
    hop, no_hop = vp.expected_set(HOP, PROD), vp.expected_set(NO_HOP, PROD)
    assert (hop.storage_grace_s, hop.storage_rollout_s) == (240, 840)
    assert (no_hop.storage_grace_s, no_hop.storage_rollout_s) == (0, 0)


def test_the_61c16826_timeline_reads_good():
    """The claims fail at 0s, `longhorn` succeeds at 23s, the claims recover at 120s."""
    expected = vp.expected_set(HOP, PROD)
    clock = Clock()
    reads = timeline(clock, [(0, CLAIMS_FAILED), (23, LONGHORN_UP), (120, RECOVERED)])
    assert vp.verify(reads, T, PRIOR, expected) == vp.Verdict("good")


def test_a_claim_that_never_recovers_holds_once_the_grace_runs_out():
    expected = vp.expected_set(HOP, PROD)
    clock = Clock()
    reads = timeline(clock, [(0, CLAIMS_FAILED), (23, LONGHORN_UP)])
    verdict = vp.verify(reads, T, PRIOR, expected)
    assert verdict == vp.Verdict("failed", ("mosquitto", "pi-peer-backup"))
    # `longhorn` is first read as `success` at the poll at 30s.
    assert 30 + expected.storage_grace_s <= clock.now < expected.timeout_s


def test_a_claim_failure_holds_when_longhorn_never_reads_success():
    """A hung `longhorn` bounds the window at its own timeout, not at `verify`'s."""
    expected = vp.expected_set(HOP, PROD)
    clock = Clock()
    pending = {**CLAIMS_FAILED, "flux/longhorn": "pending"}
    verdict = vp.verify(timeline(clock, [(0, pending)]), T, PRIOR, expected)
    assert verdict == vp.Verdict("failed", ("mosquitto", "pi-peer-backup"))
    assert expected.storage_rollout_s <= clock.now < expected.timeout_s


def test_regression_guard_a_longhorn_failure_stays_final_at_the_first_read():
    """Passes on the code before #5997 too: it pins that `longhorn` never tolerates itself."""
    expected = vp.expected_set(HOP, PROD)
    clock = Clock()
    states = {**CLAIMS_FAILED, "flux/longhorn": "failure"}
    verdict = vp.verify(timeline(clock, [(0, states)]), T, PRIOR, expected)
    assert verdict == vp.Verdict("failed", ("longhorn", "mosquitto", "pi-peer-backup"))
    assert clock.sleeps == 0


def test_regression_guard_without_a_hop_a_claims_failure_holds_at_the_first_read():
    """Passes on the code before #5997 too: the tolerance needs a revision expecting `longhorn`."""
    expected = vp.expected_set(NO_HOP, PROD)
    clock = Clock()
    verdict = vp.verify(timeline(clock, [(0, CLAIMS_FAILED)]), T, PRIOR, expected)
    assert verdict == vp.Verdict("failed", ("mosquitto", "pi-peer-backup"))
    assert clock.sleeps == 0
