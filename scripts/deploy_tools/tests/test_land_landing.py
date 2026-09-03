"""Landing's shared helpers: how it ends, how it reads the PR, git and the deployer's state.

Run: uv run pytest scripts/deploy_tools/tests/test_land_landing.py
"""

from __future__ import annotations

import subprocess

import pytest

from _land_fakes import PRIMARY, Fakes
from deploy_tools.land_lib.outcome import Outcome


def test_die_carries_the_message_on_both_streams(landing):
    ln, _ = landing()
    with pytest.raises(Outcome) as exc:
        ln.die("CI is red", 1, "ci-red")
    assert exc.value.error == "CI is red" and exc.value.detail == "PR #999 — CI is red"
    assert (exc.value.rc, exc.value.verdict) == (1, "ci-red")


def test_finish_carries_no_error(landing):
    ln, _ = landing()
    with pytest.raises(Outcome) as exc:
        ln.finish("settled", 0, "PR #999, abc")
    assert exc.value.error is None and exc.value.rc == 0


def test_view_dies_when_gh_fails(landing):
    ln, _ = landing()
    ln.tools.gh_json = lambda *a, **k: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, "gh", stderr="HTTP 404")
    )
    with pytest.raises(Outcome) as exc:
        ln.view("state")
    assert exc.value.rc == 1 and "HTTP 404" in exc.value.error


def test_git_runs_in_the_primary_checkout_without_raising(landing):
    ln, calls = landing(Fakes(fetch_rc=1))
    assert ln.git("fetch", "-q", "origin", "master").returncode == 1
    assert calls[-1] == ("git", ("fetch", "-q", "origin", "master"), {"cwd": PRIMARY})


def test_lock_contention_is_booked_and_the_holder_named_once(landing, capsys):
    ln, _ = landing()
    ln.note_lock_contention(60)
    ln.note_lock_contention(60)
    assert ln.ledger.lock_waited == 120 and ln.ledger.lock_holder == "42 flock deploy"
    assert capsys.readouterr().out.count("lock held by") == 1


@pytest.mark.parametrize(
    "state, expected",
    [
        ({}, "converged"),
        ({"behind_since": "x"}, "behind"),
        ({"hold_sha": "abc", "behind_since": "x"}, "held"),
    ],
)
def test_tick_state_reads_hold_before_behind(landing, state, expected):
    ln, _ = landing(Fakes(state=state))
    assert ln.tick_state() == expected


def test_fakes_defaults_are_fresh_per_instance():
    """A list inside a tuple default is invisible to dataclasses and RUF012 alike."""
    assert Fakes().gate[1] is not Fakes().gate[1]
    assert Fakes().derived[0] is not Fakes().derived[0]
