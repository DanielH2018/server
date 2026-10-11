"""--arm-merge and --await-merge on a PR that land.sh merges directly.

arm_merge never arms GitHub's auto-merge, and await_merge merges the PR through the REST
endpoint once await_ci reads its head green, pinned to that head. Each behaviour is tested against its counterpart: a PR left for a direct merge against
one only polled, a green head against a pending one, and a merge GitHub accepts against one
it refuses.

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


# Every decision takes the direct merge. Armed for auto-merge, an approved agent PR once sat
# BLOCKED until a hand merge (#3911). An empty decision, which is every PR since the master
# review gate was retired on 2026-10-11, takes the same path rather than the auto-merge it once
# did (#4001). REVIEW_REQUIRED stays covered for a review rule added back.
EVERY_DECISION = pytest.mark.parametrize("review", ["", "REVIEW_REQUIRED", "APPROVED"])


@EVERY_DECISION
def test_every_pr_is_left_for_a_direct_merge(landing, review):
    """No PR is armed for auto-merge, which would merge whatever head is green, not the checked one."""
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


@EVERY_DECISION
def test_a_direct_merge_dies_without_await_merge(landing, review):
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
    """Someone asked for this PR to change, so no merge call is made."""
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


def test_a_pr_not_left_for_a_direct_merge_is_only_polled(landing):
    """--await-merge without --arm-merge waits for a merge made some other way."""
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


def test_a_disarmed_gate_ends_a_direct_merge_wait_naming_it(landing):
    """A direct merge fires only on await_ci's green, which a disarmed gate never gives (#4464)."""
    ln, calls = _left_for_a_direct_merge(
        landing, ["OPEN MERGEABLE aaaa"], [(2, "await_ci: no required CI contexts")]
    )
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.rc == 1 and exc.value.verdict == "refused"
    assert "disarmed" in exc.value.error and "REQUIRED_CONTEXTS" in exc.value.error
    assert not [c for c in calls if c[0] == "gh"]
