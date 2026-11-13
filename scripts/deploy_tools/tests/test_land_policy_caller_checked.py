"""The two settings a lander sets after its own checks: the head it read and an approval.

renovate-agent's lander runs its floor check before land.sh, so land.sh must merge exactly the
head that check read, and must need the operator's approval whenever the check says so.

Run: uv run pytest scripts/deploy_tools/tests/test_land_policy_caller_checked.py
"""

import pytest

from deploy_tools.land_lib import merge
from deploy_tools.land_lib.outcome import Outcome
from test_land_policy import APPROVER, HEAD, _fakes, _review


def _arm(landing, fakes, **opts):
    ln, calls = landing(fakes, arm_merge=True, await_merge=True, **opts)
    merge.arm_merge(ln)
    return ln, calls


def test_the_head_the_caller_checked_is_pinned(landing):
    ln, _ = _arm(landing, _fakes(), require_head=HEAD)
    assert ln.pinned_head == HEAD


def test_a_head_other_than_the_one_the_caller_checked_is_refused(landing):
    with pytest.raises(Outcome) as exc:
        _arm(landing, _fakes(), require_head="d" * 40)
    assert "not the dddddddd the caller checked" in exc.value.error


def test_a_required_approval_of_the_head_lands(landing):
    fakes = _fakes(pr_reviews=[_review("APPROVED")])
    ln, calls = _arm(landing, fakes, require_approval=True, approver=APPROVER)
    assert ln.pinned_head == HEAD
    assert [c for c in calls if c[0] == "gh:reviews"]


@pytest.mark.parametrize(
    "reviews, approver, names",
    [
        ([], APPROVER, "has not approved it"),
        ([_review("APPROVED", commit="a" * 40)], APPROVER, "approved aaaaaaaa"),
        ([_review("APPROVED")], "", "no approver is set"),
    ],
)
def test_a_required_approval_that_is_missing_is_refused(
    landing, reviews, approver, names
):
    fakes = _fakes(pr_reviews=reviews)
    with pytest.raises(Outcome) as exc:
        _arm(landing, fakes, require_approval=True, approver=approver)
    assert "needs the operator's approval" in exc.value.error
    assert names in exc.value.error
