"""Pre-flight (can the tick cross what is incoming?) and the master CI wait.

Run: uv run pytest scripts/deploy_tools/tests/test_land_ci.py
"""

import pytest

from _land_fakes import PRIMARY, Fakes
from deploy_tools.land_lib import ci
from deploy_tools.land_lib.outcome import Outcome


def test_preflight_passes_when_nothing_is_incoming(landing, capsys):
    ln, calls = landing()
    ci.preflight(ln)
    assert "nothing in the way" in capsys.readouterr().out
    assert calls[-1] == ("deploy_tags", ("blockers", "origin/master"), {"cwd": PRIMARY})


def test_blocked_when_an_incoming_change_needs_a_hand(landing):
    ln, _ = landing(Fakes(blockers=[3]))
    with pytest.raises(Outcome) as exc:
        ci.preflight(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "blocked")


def test_a_broken_preflight_is_a_plain_failure(landing):
    ln, _ = landing(Fakes(blockers=[1]))
    with pytest.raises(Outcome) as exc:
        ci.preflight(ln)
    assert exc.value.rc == 1 and exc.value.verdict is None


@pytest.mark.parametrize(
    "rc, verdict, code", [(1, "ci-red", 1), (75, "ci-timeout", 75)]
)
def test_red_and_no_verdict_each_end_the_landing_with_a_name(
    landing, rc, verdict, code
):
    ln, _ = landing(Fakes(await_ci=[(rc, "x")]))
    with pytest.raises(Outcome) as exc:
        ci.wait_master_ci(ln, "abc", "abc")
    assert (exc.value.rc, exc.value.verdict) == (code, verdict)


def test_step_3_names_the_sha_and_omits_the_on_clause_for_a_timeout(landing):
    """The unlabelled caller is step 3, waiting on the merge commit itself."""
    ln, _ = landing(Fakes(await_ci=[(1, "x")]))
    with pytest.raises(Outcome) as red:
        ci.wait_master_ci(ln, "abc123")
    assert red.value.error == "master CI is RED on abc123 — nothing deployed"

    ln, _ = landing(Fakes(await_ci=[(75, "x")]), ci_timeout=900)
    with pytest.raises(Outcome) as timeout:
        ci.wait_master_ci(ln, "abc123")
    assert timeout.value.error == "no CI verdict inside 900s — nothing deployed"


def test_a_labelled_wait_names_what_it_waited_on(landing):
    """The stale-retry caller waits on a tip that is not this PR's merge commit."""
    ln, _ = landing(Fakes(await_ci=[(75, "x")]), ci_timeout=900)
    with pytest.raises(Outcome) as exc:
        ci.wait_master_ci(ln, "def456", "the tip def456")
    assert (
        exc.value.error
        == "no CI verdict on the tip def456 inside 900s — nothing deployed"
    )


def test_a_labelled_red_names_that_the_tick_cannot_cross_it(landing):
    """bash's tip wait said "the tick cannot cross it" for a red CI on the tip; the
    unlabelled step-3 wait never did (#1085 item 6)."""
    ln, _ = landing(Fakes(await_ci=[(1, "x")]))
    with pytest.raises(Outcome) as exc:
        ci.wait_master_ci(ln, "def456", "the tip def456")
    assert exc.value.error == (
        "master CI is RED on the tip def456 — the tick cannot cross it; nothing deployed"
    )


def test_an_unrecognized_await_ci_exit_names_what_it_waited_on(landing):
    """The catch-all `await_ci failed` branch dropped `where`, so a labelled (tip) wait and
    an unlabelled (step 3) wait read identically on any exit other than 1 or 75 -- unlike
    every other branch in this function (#1085 item 6)."""
    ln, _ = landing(Fakes(await_ci=[(3, "x")]))
    with pytest.raises(Outcome) as unlabelled:
        ci.wait_master_ci(ln, "abc123")
    assert unlabelled.value.error == "await_ci failed (exit 3) — nothing deployed"

    ln, _ = landing(Fakes(await_ci=[(3, "x")]))
    with pytest.raises(Outcome) as labelled:
        ci.wait_master_ci(ln, "def456", "the tip def456")
    assert (
        labelled.value.error
        == "await_ci failed on the tip def456 (exit 3) — nothing deployed"
    )


def test_green_returns_and_prints_the_line(landing, capsys):
    ln, _ = landing(Fakes(await_ci=[(0, "0123456: CI green")]))
    ci.wait_master_ci(ln, "abc", "abc")
    assert "0123456: CI green" in capsys.readouterr().out
