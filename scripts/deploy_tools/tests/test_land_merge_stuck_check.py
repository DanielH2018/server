"""A check GitHub leaves pending under a completed workflow run ends the merge wait (#7996).

PR #7975's required image-smoke check stayed `queued` after its run completed, GitHub refused
every direct merge, and the landing polled for 75 minutes towards merge-timeout. The fixtures
are the real readings: check run 130783792832 `queued` in suite 116713431402, which read
`completed` / `failure`.

Run: uv run pytest scripts/deploy_tools/tests/test_land_merge_stuck_check.py
"""

import subprocess

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import merge, stuck_checks
from deploy_tools.land_lib.outcome import Outcome

_SUITE = 116713431402
_STUCK = [{"name": "pull + boot changed images", "suite": _SUITE}]


def _refused_green_head(landing, pending_checks, completed_suites):
    """A green head GitHub refuses to merge on every poll, waited on for 3 * AFTER_S."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": [
                    {
                        "state": "OPEN",
                        "mergeable": "MERGEABLE",
                        "headRefOid": "c592b53c",
                    }
                ],
                "commits": {"commits": []},
            },
            await_ci=[(0, "c592b53c: CI green")],
            gh_merge_rc=[1],
            pending_checks=pending_checks,
            completed_suites=completed_suites,
        ),
        merge_timeout=stuck_checks.AFTER_S * 3,
        merge_poll=stuck_checks.AFTER_S // 2,
    )
    ln.direct_merge_subject = "Bump vale to 3.19.0"
    return ln, calls


def test_a_check_pending_under_a_completed_run_ends_the_wait_with_the_remedy(landing):
    ln, calls = _refused_green_head(landing, _STUCK, [_SUITE])
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert (exc.value.rc, exc.value.verdict) == (75, "merge-timeout")
    assert "git commit --allow-empty" in exc.value.error
    assert "'pull + boot changed images'" in exc.value.error
    # One read of each listing, at AFTER_S, rather than one per poll.
    assert [c[0] for c in calls if c[0].startswith("gh:check-")] == [
        "gh:check-runs",
        "gh:check-suites",
    ]


@pytest.mark.parametrize(
    "pending_checks, completed_suites",
    [
        (_STUCK, []),
        ([], [_SUITE]),
        (subprocess.CalledProcessError(1, "gh", stderr="HTTP 404: Not Found"), []),
        (["not a run"], [_SUITE]),
    ],
    ids=["run-still-in-progress", "nothing-pending", "gh-fails", "not-a-run"],
)
def test_a_check_still_running_or_an_unreadable_list_keeps_the_wait(
    landing, pending_checks, completed_suites
):
    """A pending check under a suite still running is ordinary CI, and a failed read names
    nothing, so the wait runs out its budget and names no empty commit."""
    ln, _calls = _refused_green_head(landing, pending_checks, completed_suites)
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.verdict == "merge-timeout"
    assert "still OPEN after" in exc.value.error
    assert "allow-empty" not in exc.value.error


def test_a_head_ci_has_not_read_green_is_never_read_for_stuck_checks(landing):
    """The extra reads belong only after a green head stays unmerged, not on every pending poll."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": [
                    {
                        "state": "OPEN",
                        "mergeable": "MERGEABLE",
                        "headRefOid": "c592b53c",
                    }
                ]
            },
            await_ci=[(75, "c592b53c: pending")],
            pending_checks=_STUCK,
            completed_suites=[_SUITE],
        ),
        merge_timeout=stuck_checks.AFTER_S * 3,
        merge_poll=stuck_checks.AFTER_S // 2,
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert "still OPEN after" in exc.value.error
    assert not [c for c in calls if c[0].startswith("gh:check-")]
