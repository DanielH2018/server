"""--arm-merge and --await-merge on a PR that waits on a review only a ruleset bypass clears.

GitHub's auto-merge does not apply a ruleset bypass (github/docs#45265), so an armed PR in
that state sits BLOCKED until merge-timeout. arm_merge therefore leaves it unarmed, and
await_merge merges it through the REST endpoint once await_ci reads its head green. Each
behaviour is tested against its counterpart: a review-bound PR against an ordinary armed one,
a green head against a pending one, and a merge GitHub accepts against one it refuses.

Run: uv run pytest scripts/deploy_tools/tests/test_land_merge_past_review.py
"""

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import merge
from deploy_tools.land_lib.outcome import Outcome

_REVIEW_BOUND = {
    "state": "OPEN",
    "title": "Bump vale to 3.19.0",
    "body": "Closes #12\n",
    "reviewDecision": "REVIEW_REQUIRED",
}


def _wait(states: list[str]):
    return [
        {"state": s, "mergeable": m, "headRefOid": h}
        for s, m, h in (x.split() for x in states)
    ]


def test_a_pr_waiting_on_a_review_is_left_for_a_direct_merge(landing):
    """Armed, it would sit BLOCKED: GitHub's auto-merge never applies a ruleset bypass."""
    ln, calls = landing(
        Fakes(gh_views={"state,title,body,reviewDecision": _REVIEW_BOUND}),
        arm_merge=True,
        await_merge=True,
    )
    merge.arm_merge(ln)
    assert not [c for c in calls if c[0] == "gh"]
    assert ln.direct_merge_subject == "Bump vale to 3.19.0"


def test_a_pr_waiting_on_a_review_dies_without_await_merge(landing):
    """Nothing else in the run would merge it, so leaving it unarmed would end silently."""
    ln, calls = landing(
        Fakes(gh_views={"state,title,body,reviewDecision": _REVIEW_BOUND}),
        arm_merge=True,
    )
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert exc.value.rc == 1
    assert not [c for c in calls if c[0] == "gh"]


def _left_for_a_direct_merge(landing, states, ci, merge_rc=(0,)):
    ln, calls = landing(
        Fakes(
            gh_views={"state,mergeable,headRefOid": _wait(states)},
            await_ci=ci,
            gh_merge_rc=list(merge_rc),
        )
    )
    ln.direct_merge_subject = "Bump vale to 3.19.0"
    return ln, calls


def test_a_review_bound_pr_merges_directly_once_its_head_is_green(landing):
    ln, calls = _left_for_a_direct_merge(
        landing,
        ["OPEN MERGEABLE aaaa", "OPEN MERGEABLE bbbb", "MERGED MERGEABLE bbbb"],
        [(75, "pending"), (0, "CI green")],
    )
    merge.await_merge(ln)
    assert [c[1] for c in calls if c[0] == "gh"] == [
        (
            "api",
            "-X",
            "PUT",
            "repos/{owner}/{repo}/pulls/999/merge",
            "-f",
            "merge_method=squash",
            "-f",
            "sha=bbbb",
            "-f",
            "commit_title=Bump vale to 3.19.0",
        )
    ]


def test_an_armed_pr_with_a_green_head_is_left_to_auto_merge(landing):
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": _wait(
                    ["OPEN MERGEABLE aaaa", "MERGED MERGEABLE aaaa"]
                )
            }
        )
    )
    merge.await_merge(ln)
    assert not [c for c in calls if c[0] == "gh"]


def test_a_refused_direct_merge_keeps_the_wait_going(landing):
    """A required check await_ci does not read, still running, refuses the merge for a poll."""
    ln, calls = _left_for_a_direct_merge(
        landing,
        ["OPEN MERGEABLE aaaa", "OPEN MERGEABLE aaaa", "MERGED MERGEABLE aaaa"],
        [(0, "CI green")],
        merge_rc=(1, 0),
    )
    merge.await_merge(ln)
    assert len([c for c in calls if c[0] == "gh"]) == 2
