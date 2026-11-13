"""Only the operator's issues reach a session as work (dotfiles#967 slice 1).

Any collaborator can open a `claude`-labelled issue, and `next`, `show` and `claim` hand its
body to a session. The two loaders in `gh_calls` drop or refuse an issue another account
opened.
"""

import pytest
from _findings_fakes import FOREIGNER, make_issue
from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.gh_calls import _load_issue, load_issues


def _answering(answer):
    return FindingsTools(gh_json=lambda *argv, **kwargs: answer)


def test_the_list_keeps_the_operators_issues_and_names_the_skipped_ones(capsys):
    issues = [make_issue(1), make_issue(2, author=FOREIGNER)]
    kept = load_issues("open", _answering(issues))
    assert [i["number"] for i in kept] == [1]
    assert f"skipped 1 issue(s) not opened by DanielH2018: #2 by {FOREIGNER}" in (
        capsys.readouterr().err
    )


def test_an_issue_with_no_author_is_skipped():
    issue = make_issue(3)
    del issue["author"]
    assert load_issues("open", _answering([issue])) == []


def test_reading_one_issue_another_account_opened_is_refused():
    tools = _answering(make_issue(4, author=FOREIGNER))
    with pytest.raises(SystemExit, match=f"#4 was opened by {FOREIGNER}"):
        _load_issue(4, tools)


def test_reading_one_operator_issue_returns_it():
    assert _load_issue(5, _answering(make_issue(5)))["number"] == 5
