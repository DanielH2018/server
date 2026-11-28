"""Properties of the landing policy's approval gate over generated reviews and file lists.

`approval_problem` gives the same answer for every order of the same reviews, because GitHub's
listing order is not a verdict. `approval_hits` returns every listed path among the changed
files and their old names, once each, in the order the files name them.
The property tests run under the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/deploy_tools/tests/land/test_land_policy_properties.py
"""

from hypothesis import example, given
from hypothesis import strategies as st

from deploy_tools.land_lib.policy import (
    VERDICT_STATES,
    approval_hits,
    approval_problem,
    listed,
)
from deploy_tools.land_lib.pr_json import parse_file, parse_review

APPROVER = "DanielH2018"
HEAD, OTHER = "a" * 40, "b" * 40
_STAMPS = ["2026-11-26T10:00:00Z", "2026-11-26T11:00:00Z", "2026-11-26T12:00:00Z"]

_review = st.builds(
    lambda user, state, at, commit: parse_review(
        {
            "user": {"login": user},
            "state": state,
            "submitted_at": at,
            "commit_id": commit,
        }
    ),
    st.sampled_from([APPROVER, "someone-else"]),
    st.sampled_from([*VERDICT_STATES, "COMMENTED"]),
    st.sampled_from([*_STAMPS, None]),
    st.sampled_from([HEAD, OTHER]),
)


@st.composite
def _two_orders(draw):
    reviews = draw(st.lists(_review, max_size=6))
    return reviews, draw(st.permutations(reviews))


def _approval(state: str, commit: str, at: str | None = _STAMPS[0]):
    return parse_review(
        {
            "user": {"login": APPROVER},
            "state": state,
            "submitted_at": at,
            "commit_id": commit,
        }
    )


@given(_two_orders())
@example(([_approval("APPROVED", HEAD), _approval("CHANGES_REQUESTED", HEAD)],) * 2)
@example(
    ([_approval("APPROVED", HEAD, None), _approval("DISMISSED", OTHER, None)],) * 2
)
def test_the_approval_gate_ignores_the_order_github_lists_reviews_in(orders):
    reviews, shuffled = orders
    for order in (reviews, list(reversed(reviews))):
        assert approval_problem(order, APPROVER, HEAD) == approval_problem(
            shuffled, APPROVER, HEAD
        )


def test_tied_verdicts_pass_the_gate_only_when_each_approves_the_head():
    tied = [_approval("CHANGES_REQUESTED", HEAD), _approval("APPROVED", HEAD)]
    both = [_approval("APPROVED", HEAD), _approval("APPROVED", HEAD)]

    assert approval_problem(tied, APPROVER, HEAD) != ""
    assert approval_problem(both, APPROVER, HEAD) == ""


_PATHS = ["hosts/a.py", "hosts/b/conftest.py", "docs/c.md", "conftest.py", "", None]
_PREFIXES = ["hosts/", "**/conftest.py", "docs/c"]

_file = st.builds(
    lambda name, old: parse_file(
        {"filename": name, "status": "renamed", "previous_filename": old}
        if old is not None
        else {"filename": name, "status": "modified"}
    ),
    st.sampled_from(_PATHS),
    st.sampled_from(_PATHS),
)


@given(st.lists(_file, max_size=8), st.lists(st.sampled_from(_PREFIXES), max_size=3))
def test_approval_hits_are_each_listed_name_once_in_first_seen_order(files, prefixes):
    names = [n for f in files for n in (f.get("filename"), f.get("previous_filename"))]
    listed_names = [n for n in names if n and listed(n, prefixes)]

    assert approval_hits(files, prefixes) == list(dict.fromkeys(listed_names))
