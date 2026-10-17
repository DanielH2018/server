"""`land-wait`'s deadline: a substituting promotion (#5155), and an earlier commit's
verification in flight (#5164).

`Sim` is in `_land_wait_fakes.py`; the other outcomes are tested in
test_promote_land_wait.py.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_land_wait_deadline.py
"""

import pytest

import forge
from flux_host import FluxHost
from lib.repo_paths import REPO
from deploy_tools.promote_lib import verify_promotion as vp
from deploy_tools.promote_lib import wait as promote_wait
from _land_wait_fakes import (
    D,
    M,
    P,
    Sim,
    at,
    digests,
    kustomization,
    publish_pipeline_s,
    substituting_promote_timeout,
)
from _promote_fakes import FakeTransport, digest_of

# -- the timeout against a substituting bundle (#5155) --------------------------------------


def test_the_land_wait_timeout_covers_a_substituting_promotion_plus_the_publish_pipeline():
    """The Verify-by of #5155, against the default `land.sh` reaches through `run_land_wait`."""
    margin = promote_wait.LAND_WAIT_TIMEOUT_S - substituting_promote_timeout()
    assert margin >= publish_pipeline_s(), (
        f"land-wait gives up {margin}s after verify would, inside the "
        f"{publish_pipeline_s()}s render and publish may take"
    )


def test_a_substituting_promotion_verify_would_pass_settles_rather_than_gives_up():
    """M is promoted after the publish pipeline, and `verify`'s full wait, have both run out.

    `land_wait` runs at its default timeout, as `land.sh` calls it, and must not report
    `gave-up` on a promotion that `verify` still counts as on time.
    """
    covered_at = substituting_promote_timeout() + publish_pipeline_s()
    polls_before = -(-covered_at // promote_wait.POLL_S)  # the first poll at or past it
    sim = Sim(polls=[at(P)] * polls_before + [at(M)])
    result = sim.wait()
    assert (result.verdict, result.revision) == ("settled", M), result.detail


def test_the_promote_timeout_constant_is_verifys_substituting_wait():
    """`wait.py` cannot import `promote_timeout`, so its constant restates the figure.

    A change to the renderer's default timeout or to the `cluster-vars` hop moves `verify`'s
    wait, and this fails until the constant moves with it.
    """
    assert promote_wait.PROMOTE_TIMEOUT_S == substituting_promote_timeout()


# -- the soak's timeout, which grows with the bundle (D5.3b3) -------------------------------

# M changes bento-pdf, so its changed set holds `soak`.
_SOAK_DIGESTS = {
    M: digests(bento_pdf="b", soak="k"),
    P: digests(bento_pdf="a", soak="k"),
}
for _found in _SOAK_DIGESTS.values():
    _found["bento-pdf"]["suspended"] = False
# The soak's `timeout` two queueing rounds past the nine tags published on 2026-10-17, whose
# bundle rendered 2910s. The constant, derived from a one-tag bundle, no longer covers it.
_GROWN_SOAK = "4110s"


def _with_soak(commit: str, timeout: str | None) -> list[dict]:
    soak = kustomization("soak", f"master@sha1:{commit}", ready=False)
    if timeout is not None:
        soak["spec"]["timeout"] = timeout
    return [*at(commit, "bento-pdf"), soak]


def test_the_soak_deadline_is_twice_verifys_wait_on_the_listed_soak():
    assert promote_wait.soak_deadline_s(_with_soak(M, _GROWN_SOAK)) == 2 * (4110 + 180)
    assert promote_wait.soak_deadline_s(_with_soak(M, "soon")) == 0
    assert promote_wait.soak_deadline_s(at(M, "bento-pdf")) == 0


@pytest.mark.parametrize("listed", [_GROWN_SOAK, None])
def test_a_promotion_verify_still_counts_on_time_settles_only_with_the_soak_deadline(
    listed,
):
    """M promotes after the publish pipeline and `verify`'s full wait on a grown soak.

    The red half lists `soak` with no `timeout`, which leaves the one-tag constant: the wait
    gives up first, the #5155 shape.
    """
    covered_at = 4110 + 180 + publish_pipeline_s()
    assert covered_at > promote_wait.LAND_WAIT_TIMEOUT_S
    polls = [_with_soak(P, listed)] * _polls_until(covered_at) + [_with_soak(M, listed)]
    sim = Sim(polls=polls, published=_SOAK_DIGESTS, soak={M: ["success"]})
    result = sim.wait()
    expected = "settled" if listed else "gave-up"
    assert result.verdict == expected, result.detail


# -- an earlier commit's verification in flight (#5164) -------------------------------------


def _polls_until(seconds: int) -> int:
    """The number of polls before the first one at or past `seconds`."""
    return -(-seconds // promote_wait.POLL_S)


def test_the_in_flight_wait_is_decides():
    """`verify_promotion` imports `wait`, so `wait` restates the constant."""
    assert promote_wait.IN_FLIGHT_WAIT_S == vp.IN_FLIGHT_WAIT_S


def test_a_promotion_after_decides_full_in_flight_wait_settles():
    """The Verify-by of #5164: M's `decide` waits out P's verification, then M promotes.

    P stays in flight for `IN_FLIGHT_WAIT_S`, and M is covered a substituting promotion and
    the publish pipeline after that.
    """
    in_flight_polls = _polls_until(vp.IN_FLIGHT_WAIT_S)
    covered_at = (
        vp.IN_FLIGHT_WAIT_S + substituting_promote_timeout() + publish_pipeline_s()
    )
    sim = Sim(
        polls=[at(P)] * _polls_until(covered_at) + [at(M)],
        in_flight=[P] * in_flight_polls + [None],
    )
    result = sim.wait()
    assert (result.verdict, result.revision) == ("settled", M), result.detail


def test_an_in_flight_wait_that_never_ends_stops_moving_the_deadline():
    """`decide` gives up at IN_FLIGHT_WAIT_S, so the wait gives up a timeout after that."""
    sim = Sim(polls=[at(P)], in_flight=[P])
    result = sim.wait()
    assert (result.verdict, result.cause) == ("gave-up", "promote-timeout")
    waited = sim.sleeps * promote_wait.POLL_S
    assert (
        vp.IN_FLIGHT_WAIT_S
        < waited
        <= vp.IN_FLIGHT_WAIT_S + promote_wait.LAND_WAIT_TIMEOUT_S
    )


@pytest.mark.parametrize(
    "in_flight",
    [M, D, promote_wait.WaitReadError("prod's manifest has no revision")],
    ids=["the-merge-itself", "a-later-commit", "an-unreadable-prod"],
)
def test_only_an_earlier_commit_in_flight_moves_the_deadline(in_flight):
    """The base timeout already covers a verification of M or of a commit after it."""
    sim = Sim(polls=[at(P)], in_flight=[in_flight])
    assert sim.wait().verdict == "gave-up"
    assert sim.sleeps == promote_wait.LAND_WAIT_TIMEOUT_S // promote_wait.POLL_S


def _prod_transport(prod: str, last_good: str, revision: str | None) -> FakeTransport:
    transport = FakeTransport()
    transport.tags["prod"] = digest_of(prod)
    transport.tags["last-good"] = digest_of(last_good)
    annotations = (
        {} if revision is None else {promote_wait.REVISION_ANNOTATION: revision}
    )
    transport.manifests["prod"] = {"annotations": annotations}
    return transport


def _forge(transport: FakeTransport) -> forge.Forge:
    return forge.Forge(
        forge.Registry(transport, None), forge.GitHub(transport, None), forge.Cosign()
    )


def test_prod_ahead_of_last_good_reads_as_its_commit_in_flight():
    transport = _prod_transport("t", "g", f"master@sha1:{P}")
    assert promote_wait._in_flight(_forge(transport)) == P


def test_prod_at_last_good_reads_as_nothing_in_flight():
    transport = _prod_transport("g", "g", f"master@sha1:{P}")
    assert promote_wait._in_flight(_forge(transport)) is None


def test_prod_without_a_revision_is_a_read_error():
    transport = _prod_transport("t", "g", None)
    with pytest.raises(promote_wait.WaitReadError, match="revision"):
        promote_wait._in_flight(_forge(transport))


def test_the_live_reads_wire_the_in_flight_read_to_prod():
    transport = _prod_transport("t", "g", f"master@sha1:{P}")
    reads = promote_wait.live_wait_reads(_forge(transport), REPO, host=FluxHost())
    assert reads.in_flight() == P


@pytest.mark.parametrize(
    "in_flight", [P, None], ids=["an-earlier-commit-in-flight", "nothing-in-flight"]
)
def test_the_cap_ends_the_wait_whatever_is_in_flight(in_flight):
    """A lander killed at a fixed time passes what its deadline leaves (#5199)."""
    sim = Sim(polls=[at(P)], in_flight=[in_flight])
    result = promote_wait.land_wait(sim.reads(), M, [], cap_s=1800)
    assert (result.verdict, result.cause) == ("gave-up", "promote-timeout")
    assert sim.sleeps * promote_wait.POLL_S == 1800
