#!/usr/bin/env python3
"""A `behind` read taken after this landing stopped watching a tick names the race (#1607).

`land.sh` printed `VERDICT: deferred` and "the next tick crosses it" during the very apply
that failed and set `hold_sha`, parking the deployer for every session. The read was racing
the apply: the tick wrapper had exited 75 ("still running, stopped watching") 540s in, and at
that moment `hold_sha` was legitimately empty while `behind_since` was set -- the exact
signature of an ordinary deferral. The correct verdict, `deploy-failed`, appeared only on the
NEXT invocation, minutes later.

`deferred` and `deploy-failed` call for opposite actions: wait for the next tick, versus stop
because a hold blocks the whole fleet. This pins the third answer -- the landing saying it does
not know, because it stopped watching.

Both halves per CLAUDE.md: a `behind` read AFTER an abandoned watch must name the race (the
half the bug got wrong), and a `behind` read after a tick that actually finished must keep the
ordinary "the next tick crosses it" prose (the half a blanket rewrite would delete, making
every deferral read as a possible hold).

Run: uv run pytest scripts/deploy_tools/tests/test_land_abandoned_tick_watch.py
"""

import pytest

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.land_lib import deploy, health_verdict, tick
from deploy_tools.land_lib.outcome import Outcome

_BEHIND = {"behind_since": "93eda4d2 1789043217.0696108"}
_NEXT_TICK = "the next tick crosses it"


# ── the flag itself: only an abandoned watch sets it ────────────────────────────────────


def test_a_tick_still_running_marks_the_watch_abandoned(landing):
    """Exit 75 is the wrapper giving up on a run in flight, not the run finishing."""
    ln, _ = landing(Fakes(tick=[75]))
    tick.run_tick(ln)
    assert ln.tick_watch_abandoned is True


def test_a_tick_that_finished_does_not_mark_it(landing):
    """The must-not-fire half: a flag set on every tick would rewrite every deferral."""
    ln, _ = landing(Fakes(tick=[0]))
    tick.run_tick(ln)
    assert ln.tick_watch_abandoned is False


def test_a_later_tick_that_finished_clears_an_earlier_abandoned_watch(landing):
    """`run_tick` runs twice -- step 4, then the deploy phase's stale retry.

    A retry that returns 0 watched a tick to completion, so the markers it leaves are settled
    and the race note no longer applies. Left sticky, the landing would send an operator back
    for a second look at a state that is already final.
    """
    ln, _ = landing(Fakes(tick=[75]))
    tick.run_tick(ln)
    assert ln.tick_watch_abandoned is True
    ln.tools.tick = lambda: 0
    tick.run_tick(ln)
    assert ln.tick_watch_abandoned is False


# ── the no-tag verdict (deploy.no_tag_outcome) ─────────────────────────────────────────


def _self_applied(landing, fakes):
    ln, _ = landing(fakes)
    ln.merge_sha, ln.self_applied = MERGE_SHA, True
    return ln


def test_behind_after_an_abandoned_watch_names_the_race(landing, capsys):
    ln = _self_applied(landing, Fakes(self_applied=True, state=_BEHIND))
    ln.tick_watch_abandoned = True
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    out = capsys.readouterr().out
    assert _NEXT_TICK not in out
    assert "read mid-apply" in out
    assert "stopped watching a tick still applying" in exc.value.detail
    # Still exit 75 + deferred: the landing is unsettled, not failed, and every consumer of
    # the code path (land.sh's own retry, the board) reads 75 as "come back to it".
    assert (exc.value.rc, exc.value.verdict) == (75, "deferred")


def test_behind_after_a_completed_tick_keeps_the_ordinary_prose(landing, capsys):
    ln = _self_applied(landing, Fakes(self_applied=True, state=_BEHIND))
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    assert _NEXT_TICK in capsys.readouterr().out
    assert (exc.value.rc, exc.value.verdict) == (75, "deferred")
    assert "not yet applied by the tick" in exc.value.detail


def test_a_readable_hold_still_wins_over_an_abandoned_watch(landing):
    """A hold that IS readable is settled fact; the race note must not soften it."""
    ln = _self_applied(landing, Fakes(self_applied=True, state={"hold_sha": "abc"}))
    ln.tick_watch_abandoned = True
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "deploy-failed")


# ── the health verdict (health_verdict.health), the other site reading the same markers ──


def _deployed(landing, fakes):
    ln, _ = landing(fakes)
    ln.merge_sha, ln.resolved_tags, ln.self_applied = MERGE_SHA, ["sonarr"], True
    return ln


def test_the_health_verdict_names_the_race_too(landing, capsys):
    ln = _deployed(landing, Fakes(self_applied=True, state=_BEHIND))
    ln.tick_watch_abandoned = True
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert "read mid-apply" in capsys.readouterr().out
    assert "stopped watching a tick still applying" in exc.value.detail
    assert (exc.value.rc, exc.value.verdict) == (75, "deferred")


def test_the_health_verdict_after_a_completed_tick_is_unchanged(landing):
    ln = _deployed(landing, Fakes(self_applied=True, state=_BEHIND))
    with pytest.raises(Outcome) as exc:
        health_verdict.health(ln)
    assert "the tick's half not yet" in exc.value.detail
    assert (exc.value.rc, exc.value.verdict) == (75, "deferred")
