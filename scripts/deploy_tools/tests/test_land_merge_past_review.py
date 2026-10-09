"""--arm-merge and --await-merge on a PR that can merge only through a ruleset bypass.

GitHub's auto-merge does not apply a ruleset bypass (github/docs#45265), so an armed PR in
that state sits BLOCKED until merge-timeout. arm_merge therefore leaves it unarmed, and
await_merge merges it through the REST endpoint once await_ci reads its head green. Each
behaviour is tested against its counterpart: a bypass-only PR against an ordinary armed one,
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


# REVIEW_REQUIRED needs the bypass for the review gate. APPROVED needs it for the agent branch
# fence, which restricts updates to master itself: armed, an approved agent PR sat BLOCKED
# until a hand merge (#3911).
BYPASS_ONLY = pytest.mark.parametrize("review", ["REVIEW_REQUIRED", "APPROVED"])


@BYPASS_ONLY
def test_a_pr_only_a_bypass_merges_is_left_for_a_direct_merge(landing, review):
    """Armed, it would sit BLOCKED: GitHub's auto-merge never applies a ruleset bypass."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title,body,reviewDecision": {
                    **_REVIEW_BOUND,
                    "reviewDecision": review,
                }
            }
        ),
        arm_merge=True,
        await_merge=True,
    )
    merge.arm_merge(ln)
    assert not [c for c in calls if c[0] == "gh"]
    assert ln.direct_merge_subject == "Bump vale to 3.19.0"


@BYPASS_ONLY
def test_a_pr_only_a_bypass_merges_dies_without_await_merge(landing, review):
    """Nothing else in the run would merge it, so leaving it unarmed would end silently."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title,body,reviewDecision": {
                    **_REVIEW_BOUND,
                    "reviewDecision": review,
                }
            }
        ),
        arm_merge=True,
    )
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert exc.value.rc == 1
    assert not [c for c in calls if c[0] == "gh"]
    # --await-merge alone only polls; the direct merge needs arm_merge in the same run (#3625).
    assert "re-run with both --arm-merge --await-merge" in exc.value.error


def test_a_pr_with_changes_requested_is_refused_before_any_merge(landing):
    """A direct merge would apply the operator's bypass to a PR someone asked to change."""
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,title,body,reviewDecision": {
                    **_REVIEW_BOUND,
                    "reviewDecision": "CHANGES_REQUESTED",
                }
            }
        ),
        arm_merge=True,
        await_merge=True,
    )
    with pytest.raises(Outcome) as exc:
        merge.arm_merge(ln)
    assert exc.value.rc == 1 and "changes requested" in exc.value.error
    assert not [c for c in calls if c[0] == "gh"]
    assert not ln.direct_merge_subject


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
