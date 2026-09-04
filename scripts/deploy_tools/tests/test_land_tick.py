"""The tick, retried while the unit's own flock gives up -- one implementation (#1013).

Run: uv run pytest scripts/deploy_tools/tests/test_land_tick.py
"""

from __future__ import annotations

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
