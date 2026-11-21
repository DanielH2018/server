"""`remedy <n>`: records a ruled remedy and removes the `no-vetted-remediation` label.

Run: uv run pytest scripts/dev/tests/test_findings_remedy.py
"""

import pytest
from _findings_fakes import Fakes, make_issue, typed

from dev import findings
from dev.fanout_lib.brief import operator_comments
from dev.findings_lib.plans import ClaimRefused, plan_remedy

_LABEL = "no-vetted-remediation"


def test_remedy_removes_the_label_and_posts_the_remedy():
    plans = plan_remedy(typed(make_issue(9116, labels=[_LABEL])), "Cap the retries.\n")
    assert plans == [
        ["issue", "edit", "9116", "--remove-label", _LABEL],
        ["issue", "comment", "9116", "--body", "Vetted remedy: Cap the retries."],
    ]


@pytest.mark.parametrize(
    ("issue", "remedy", "reason"),
    [
        (typed(make_issue(9116, state="CLOSED", labels=[_LABEL])), "x", "closed"),
        (typed(make_issue(9116)), "x", "no `no-vetted-remediation` label"),
        (typed(make_issue(9116, labels=[_LABEL])), " \n", "empty"),
    ],
)
def test_remedy_refuses_when_there_is_nothing_to_record(issue, remedy, reason):
    with pytest.raises(ClaimRefused) as exc:
        plan_remedy(issue, remedy)
    assert reason in exc.value.reason


def test_the_remedy_comment_reaches_a_fanout_brief():
    """The session that picks the issue up has to read what the operator ruled."""
    plans = plan_remedy(typed(make_issue(9116, labels=[_LABEL])), "Cap the retries.")
    issue = typed(make_issue(9116, comments=[plans[1][-1]]))
    assert [c.body for c in operator_comments(issue)] == [plans[1][-1]]


def test_remedy_cli_writes_the_label_removal_and_the_comment(
    tmp_path, capsys, make_tools
):
    body = tmp_path / "remedy.md"
    body.write_text("Cap the retries.")
    tools, calls = make_tools(Fakes(view=make_issue(9116, labels=[_LABEL])))
    assert findings.main(["remedy", "9116", "--body-file", str(body)], tools) == 0
    assert ["issue", "edit", "9116", "--remove-label", _LABEL] in calls.gh
    comment = ["issue", "comment", "9116", "--body", "Vetted remedy: Cap the retries."]
    assert comment in calls.gh
    assert "#9116 remedy recorded" in capsys.readouterr().out


def test_remedy_cli_exits_3_when_the_issue_lacks_the_label(
    tmp_path, capsys, make_tools
):
    body = tmp_path / "remedy.md"
    body.write_text("Cap the retries.")
    tools, calls = make_tools(Fakes(view=make_issue(9116)))
    assert findings.main(["remedy", "9116", "--body-file", str(body)], tools) == 3
    assert not calls.gh
    assert "no `no-vetted-remediation` label" in capsys.readouterr().err
