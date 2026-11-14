"""Landing's shared helpers: how it ends, how it reads the PR, and git.

Run: uv run pytest scripts/deploy_tools/tests/test_land_landing.py
"""

import json
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
    assert exc.value.error == "" and exc.value.rc == 0


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


def test_view_dies_when_a_field_has_the_wrong_type(landing):
    ln, _ = landing()
    ln.tools.gh_json = lambda *a, **k: {"headRefOid": ["not", "a", "sha"]}
    with pytest.raises(Outcome) as exc:
        ln.view("headRefOid")
    # The field name is the one detail that says what broke (#4516).
    assert exc.value.rc == 1 and "headRefOid: expected a string" in exc.value.error


def test_git_runs_in_the_primary_checkout_without_raising(landing):
    ln, calls = landing(Fakes(fetch_rc=1))
    assert ln.git("fetch", "-q", "origin", "master").returncode == 1
    assert calls[-1] == ("git", ("fetch", "-q", "origin", "master"), {"cwd": PRIMARY})


def test_fakes_defaults_are_fresh_per_instance():
    """A list inside a tuple default is invisible to dataclasses and RUF012 alike."""
    assert Fakes().gate[1] is not Fakes().gate[1]
    assert Fakes().derived[0] is not Fakes().derived[0]


def test_die_takes_no_default_exit_code():
    """A default of 2 collided with three other meanings of 2 (#4451): every caller names one."""
    import inspect

    from deploy_tools.land_lib.landing import Landing

    rc = inspect.signature(Landing.die).parameters["rc"]
    assert rc.default is inspect.Parameter.empty
