"""`land-wait`'s deadline: a substituting promotion (#5155), and an earlier commit's
verification in flight (#5164).

`Sim` is in `_land_wait_fakes.py`; the other outcomes are tested in
test_promote_land_wait.py.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_land_wait_deadline.py
"""

import pytest

import forge
from deploy_tools.promote_lib import verify_promotion as vp
from deploy_tools.promote_lib import wait as promote_wait
from _land_wait_fakes import (
    D,
    M,
    P,
    Sim,
    at,
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
