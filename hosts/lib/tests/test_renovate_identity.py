import pytest
import renovate_identity


@pytest.mark.parametrize("login", ["app/renovate", "renovate[bot]"])
def test_both_of_githubs_spellings_identify_renovate(login):
    assert renovate_identity.is_renovate({"login": login, "is_bot": True})


@pytest.mark.parametrize(
    "account",
    [
        {"login": "DanielH2018"},
        {"login": "renovate"},
        {"login": "app/renovate-bot"},
        {},
        None,
    ],
)
def test_any_other_account_or_a_missing_one_is_not_renovate(account):
    assert not renovate_identity.is_renovate(account)


def test_gh_and_rest_payloads_parse_to_the_same_pr():
    gh = {"number": 7, "author": {"login": "app/renovate"}, "headRefName": "renovate/a"}
    rest = {
        "number": 7,
        "user": {"login": "renovate[bot]"},
        "head": {"ref": "renovate/a"},
    }
    for pr in (
        renovate_identity.RenovatePR.from_gh(gh),
        renovate_identity.RenovatePR.from_rest(rest),
    ):
        assert (pr.number, pr.head_ref, pr.is_renovate) == (7, "renovate/a", True)
        assert pr.on_renovate_branch


def test_a_renovate_branch_pushed_by_anyone_else_is_not_renovate():
    pr = renovate_identity.RenovatePR.from_rest(
        {"number": 8, "user": {"login": "renovate-fan"}, "head": {"ref": "renovate/a"}}
    )
    assert pr.on_renovate_branch and not pr.is_renovate
