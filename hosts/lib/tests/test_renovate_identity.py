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
