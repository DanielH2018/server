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
    ln, _ = landing(Fakes(tick=[1]))
    with pytest.raises(Outcome) as exc:
        tick.run_tick(ln)
    assert exc.value.rc == 1 and "gitops tick failed (exit 1)" in exc.value.error
