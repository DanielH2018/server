"""The tick, retried while the unit's own flock gives up -- one implementation (#1013).

Run: uv run pytest scripts/deploy_tools/tests/test_land_tick.py
"""

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import tick
from deploy_tools.land_lib.outcome import Outcome


def test_lock_busy_after_every_attempt_is_skipped(landing):
    ln, calls = landing(Fakes(tick=[3]))
    with pytest.raises(Outcome) as exc:
        tick.run_tick(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "lock-busy")
    assert [c[0] for c in calls].count("tick") == 5


def test_a_contended_tick_is_booked_and_then_succeeds(landing, capsys):
    ln, calls = landing(Fakes(tick=[3, 0]))
    tick.run_tick(ln)
    assert [c[0] for c in calls].count("tick") == 2
    assert ln.ledger.lock_waited >= ln.opts.lock_backoff
    assert ln.ledger.lock_holder == "42 flock deploy"
    assert "retrying in 60s" in capsys.readouterr().out


def test_exit_75_is_not_a_failure(landing, capsys):
    """The wrapper stopped watching a run still in flight; the ff-merge is done or retryable."""
    ln, _ = landing(Fakes(tick=[75]))
    tick.run_tick(ln)
    assert "tick exit 75" in capsys.readouterr().out


def test_an_outright_failure_dies(landing):
    """#1031: the tick failing is deploy-failed, not the verdict-less `aborted` bucket."""
    ln, _ = landing(Fakes(tick=[1]))
    with pytest.raises(Outcome) as exc:
        tick.run_tick(ln)
    assert exc.value.rc == 1 and "gitops tick failed (exit 1)" in exc.value.error
    assert exc.value.verdict == "deploy-failed"
    assert ln.ledger.cause == "tick-failed"


def test_the_lock_holder_is_sampled_before_the_attempt(landing):
    """#1031: read after the losing attempt, the holder has usually already released.

    Two halves, because the recorded value alone cannot tell the orders apart -- a
    post-attempt read consumes the same first fake answer. The ORDER assertion is the one
    that goes red without the pre-sample: `lock_holder` must be called before the tick it
    is a sample for. The value assertion then proves the sample is the one that was booked,
    with the fake going empty afterwards the way a released holder does.
    """
    ln, calls = landing(Fakes(tick=[3, 0], lock_holder=["42 flock deploy", ""]))
    tick.run_tick(ln)
    names = [c[0] for c in calls]
    assert names.index("lock_holder") < names.index("tick")
    assert ln.ledger.lock_holder == "42 flock deploy"


def test_the_tick_lets_the_landing_book_a_wait_it_reports_itself(landing):
    """Joining a tick already in flight exits 0, so `retry_while_locked` books nothing.

    The wrapper reports that wait on its own stderr instead, and the phase must hand the
    boundary somewhere to put it.
    """
    ln, calls = landing()
    tick.run_tick(ln)
    observe = next(c[2]["observe"] for c in calls if c[0] == "tick")
    observe(47, "pid 8: gitops-deploy")
    assert ln.ledger.lock_waited == 47
    assert ln.ledger.lock_holder == "pid 8: gitops-deploy"


def test_a_kick_does_not_wait_for_the_tick(landing, capsys):
    """`--no-wait`: the landing deploys the merge commit itself and needs no fast-forward."""
    ln, calls = landing()
    tick.kick_tick(ln)
    kick = next(c for c in calls if c[0] == "tick")
    assert kick[2] == {"wait": False}
    assert "tick kicked, not awaited" in capsys.readouterr().out
    assert ln.ledger.lock_waited == 0
    assert ln.ledger.kick == "started"


def test_a_failed_kick_does_not_end_the_landing(landing, capsys):
    """The rejecting half of `run_tick`'s own failure arm: a kick applies nothing, so a
    landing whose deploy is still to come must not die on one. The deployer's timer
    converges the primary checkout whether the request landed or not."""
    ln, _ = landing(Fakes(tick=[1]))
    tick.kick_tick(ln)
    assert "tick kick failed (exit 1)" in capsys.readouterr().out
    assert ln.ledger.kick == "failed"


def test_a_kick_that_joined_a_run_in_flight_is_not_booked_as_started(landing, capsys):
    """Issue #1843: exit 4 is "a tick was already running, none was started". That run fetched
    before the merge, so it does not carry this landing's commit; reading it as `started` is
    what let the primary sit behind until the timer."""
    ln, _ = landing(Fakes(tick=[4]))
    tick.kick_tick(ln)
    assert ln.ledger.kick == "joined"
    out = capsys.readouterr().out
    assert "joined a run already in flight" in out
    assert "kick failed" not in out


def test_a_joined_kick_is_re_armed_after_the_gate(landing, capsys):
    """The second request starts a tick of its own once the joined run has ended."""
    ln, calls = landing(Fakes(tick=[4, 0]))
    tick.kick_tick(ln)
    tick.rearm_tick(ln)
    assert [c[2] for c in calls if c[0] == "tick"] == [{"wait": False}, {"wait": False}]
    assert ln.ledger.kick == "rearmed"
    assert "tick re-armed after the gate" in capsys.readouterr().out


def test_a_second_join_is_booked_as_not_converged_by_this_landing(landing, capsys):
    """REJECTING half: the joined run is still in flight after the gate. Said and booked, not
    retried -- the deployer's timer converges the checkout, and the deploy is already live."""
    ln, _ = landing(Fakes(tick=[4, 4]))
    tick.kick_tick(ln)
    tick.rearm_tick(ln)
    assert ln.ledger.kick == "joined"
    assert "did NOT converge the primary checkout" in capsys.readouterr().out


@pytest.mark.parametrize("first", [0, 1])
def test_only_a_joined_kick_is_re_armed(landing, first):
    """A kick that started its own tick, or that systemd refused, is not asked twice."""
    ln, calls = landing(Fakes(tick=[first, 0]))
    tick.kick_tick(ln)
    tick.rearm_tick(ln)
    assert [c[0] for c in calls].count("tick") == 1


def test_a_landing_that_kicked_nothing_re_arms_nothing(landing):
    """The fallback path awaited its tick; there is no kick to re-arm."""
    ln, calls = landing()
    tick.rearm_tick(ln)
    assert [c[0] for c in calls].count("tick") == 0
    assert ln.ledger.kick == ""
