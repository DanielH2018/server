"""The `manual` label: `manual <n>` and `open --manual` set it, `manual <n> --clear` removes it.

`next` withholding a `manual` issue and `claim` refusing one are covered in
`test_findings_next.py` and `test_findings_claim_plans.py`; these tests cover only the writes.

Run: uv run pytest scripts/dev/tests/test_findings_manual.py
"""

import pytest
from _findings_fakes import Fakes, make_issue

from dev import findings
from dev.findings_lib.issue_model import claim_comment, fingerprint
from dev.findings_lib.plans import ClaimRefused, plan_manual

_HOLDER = "worktree-issue-2978"


# --- the planner ------------------------------------------------------------------------------


def test_manual_adds_the_label_and_comments():
    plans = plan_manual(make_issue(2978), clear=False)
    assert plans[0] == ["issue", "edit", "2978", "--add-label", "manual"]
    assert plans[1][:3] == ["issue", "comment", "2978"]


def test_manual_clear_removes_the_label_and_comments():
    plans = plan_manual(make_issue(2978, labels=["manual"]), clear=True)
    assert plans[0] == ["issue", "edit", "2978", "--remove-label", "manual"]
    assert plans[1][:3] == ["issue", "comment", "2978"]


@pytest.mark.parametrize(
    ("issue", "clear", "reason"),
    [
        (make_issue(2978, state="CLOSED"), False, "closed"),
        (make_issue(2978, labels=["manual"]), False, "already manual"),
        (make_issue(2978), True, "not manual"),
    ],
)
def test_manual_refuses_when_there_is_nothing_to_change(issue, clear, reason):
    with pytest.raises(ClaimRefused) as exc:
        plan_manual(issue, clear=clear)
    assert reason in exc.value.reason


# --- the CLI ----------------------------------------------------------------------------------


def test_manual_cli_releases_a_claim_the_issue_carries(capsys, make_tools):
    """`reap` skips `manual` issues, so a claim not released here would never be cleared."""
    held = make_issue(
        2978, labels=["claimed"], comments=[claim_comment(_HOLDER, None, "t")]
    )
    tools, calls = make_tools(Fakes(view=held))
    assert findings.main(["manual", "2978"], tools) == 0
    assert ["issue", "edit", "2978", "--add-label", "manual"] in calls.gh
    assert any(f"Released: `{_HOLDER}`" in a for c in calls.gh for a in c)
    assert ["issue", "edit", "2978", "--remove-label", "claimed"] in calls.gh
    assert "#2978 marked manual" in capsys.readouterr().out


def test_manual_cli_clear_exits_3_when_the_issue_is_not_manual(capsys, make_tools):
    tools, calls = make_tools(Fakes(view=make_issue(2978)))
    assert findings.main(["manual", "2978", "--clear"], tools) == 3
    assert not calls.gh
    assert "not manual" in capsys.readouterr().out


def _open_argv(tmp_path):
    body = tmp_path / "b.md"
    body.write_text("B")
    return [
        "open",
        "--title",
        "T",
        "--body-file",
        str(body),
        "--severity",
        "low",
        "--kind",
        "gap",
        "--file",
        "a.py:1",
        "--manual",
    ]


def test_open_cli_with_manual_puts_the_label_on_the_create(tmp_path, make_tools):
    tools, calls = make_tools(Fakes())
    assert findings.main(_open_argv(tmp_path), tools) == 0
    create = next(c for c in calls.gh if c[:2] == ["issue", "create"])
    assert "manual" in create


def test_open_cli_with_manual_marks_an_issue_the_dedup_matched(tmp_path, make_tools):
    """A touch would otherwise drop the flag without a word."""
    match = make_issue(3, fp=fingerprint("T", "a.py:1"))
    tools, calls = make_tools(Fakes(issues=[match]))
    assert findings.main(_open_argv(tmp_path), tools) == 0
    assert ["issue", "edit", "3", "--add-label", "manual"] in calls.gh
