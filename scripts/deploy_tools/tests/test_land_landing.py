"""Landing's shared helpers: how it ends, how it reads the PR, git and the deployer's state.

Run: uv run pytest scripts/deploy_tools/tests/test_land_landing.py
"""

import json
import subprocess

import pytest

from _land_fakes import PRIMARY, Fakes
from deploy_tools.land_lib import tools as tools_mod
from deploy_tools.land_lib.landing import TickState
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


def test_view_dies_when_gh_times_out(landing):
    ln, _ = landing()
    ln.tools.gh_json = lambda *a, **k: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("gh", 30)
    )
    with pytest.raises(Outcome) as exc:
        ln.view("state")
    assert exc.value.rc == 1 and "gh timed out" in exc.value.error


def test_view_dies_when_gh_answers_with_something_that_is_not_json(landing):
    """An auth prompt or a proxy error page: gh exits 0 and the parse raises."""
    ln, _ = landing()
    ln.tools.gh_json = lambda *a, **k: (_ for _ in ()).throw(
        json.JSONDecodeError("Expecting value", "<html>", 0)
    )
    with pytest.raises(Outcome) as exc:
        ln.view("state")
    assert exc.value.rc == 1 and "unparseable gh output" in exc.value.error


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
        ({}, TickState.CONVERGED),
        ({"behind_since": "x"}, TickState.BEHIND),
        ({"hold_sha": "abc", "behind_since": "x"}, TickState.HELD),
    ],
)
def test_tick_state_reads_hold_before_behind(landing, state, expected):
    ln, _ = landing(Fakes(state=state))
    assert ln.tick_state() == expected


def test_an_unreadable_state_directory_is_unknown_not_converged(landing):
    """The reject half of `tick_state`, and the reason `read_state` distinguishes the two.

    `read_state` used to suppress every OSError and answer "", so a state directory this
    process could not read reported `converged` -- "the tick applied it" -- and a landing
    settled on the strength of a read that never happened.
    """
    ln, _ = landing()
    ln.tools.read_state = lambda root, name: None
    assert ln.tick_state() == TickState.UNKNOWN


def test_read_state_answers_empty_for_an_absent_marker(tmp_path):
    """The accept half: an absent marker is the ordinary state on every healthy tick."""
    assert tools_mod.read_state(tmp_path, "hold_sha") == ""
    assert tools_mod.read_state(tmp_path / "no-such-dir", "hold_sha") == ""


def test_read_state_strips_the_marker_it_finds(tmp_path):
    (tmp_path / "hold_sha").write_text("abc123\n")
    assert tools_mod.read_state(tmp_path, "hold_sha") == "abc123"


def test_read_state_answers_none_for_a_marker_it_cannot_read(tmp_path):
    """The rejecting half. A directory where a file is expected raises IsADirectoryError,
    which is an OSError that is NOT FileNotFoundError -- the exact split the fix turns on.
    """
    (tmp_path / "hold_sha").mkdir()
    assert tools_mod.read_state(tmp_path, "hold_sha") is None


def test_read_state_answers_none_for_an_unreadable_directory(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    (root / "hold_sha").write_text("abc")
    root.chmod(0o000)
    try:
        assert tools_mod.read_state(root, "hold_sha") is None
    finally:
        root.chmod(0o755)


def test_fakes_defaults_are_fresh_per_instance():
    """A list inside a tuple default is invisible to dataclasses and RUF012 alike."""
    assert Fakes().gate[1] is not Fakes().gate[1]
    assert Fakes().derived[0] is not Fakes().derived[0]
