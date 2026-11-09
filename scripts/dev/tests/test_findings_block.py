"""A native "blocked by" relationship: `block` plans it, `open --blocked-by` files with it,
`next`, `claim` and `list` read it, and it frees the issue the moment its blocker closes.

Run: uv run pytest scripts/dev/tests/test_findings_block.py
"""

import json
from datetime import date

import pytest
from _findings_fakes import Fakes, facts, make_issue

from dev import findings
from lib.exit_codes import FINDINGS_BAD_ARGS
from dev.findings_lib.issue_model import open_blockers, pickable, waiting
from dev.findings_lib.plans import ClaimRefused, plan_block, plan_claim

TODAY = date(2026, 11, 9)
REPO_URL = "https://github.com/DanielH2018/server/issues"


def _blocker(number, state="OPEN", url=None):
    return {
        "number": number,
        "state": state,
        "title": f"blocker {number}",
        "url": url or f"{REPO_URL}/{number}",
    }


def blocked(number, *blockers, **kw):
    issue = make_issue(number, **kw)
    issue["blockedBy"] = {"nodes": list(blockers), "totalCount": len(blockers)}
    return issue


# --- reading the relationship -----------------------------------------------------------------


def test_an_open_blocker_withholds_the_issue():
    issue = blocked(7195, _blocker(7178))
    assert open_blockers(issue) == ["#7178"]
    assert pickable([issue], live_claims=set(), pr_refs=set(), today=TODAY) == []
    assert [r["number"] for r in waiting([issue], today=TODAY)] == [7195]


def test_a_closed_blocker_frees_the_issue_with_nothing_to_clear():
    issue = blocked(7195, _blocker(7178, state="CLOSED"))
    assert open_blockers(issue) == []
    rows = pickable([issue], live_claims=set(), pr_refs=set(), today=TODAY)
    assert [r["number"] for r in rows] == [7195]
    assert waiting([issue], today=TODAY) == []


def test_a_blocker_in_another_repository_is_spelled_with_its_slug():
    other = _blocker(558, url="https://github.com/DanielH2018/dotfiles/issues/558")
    assert open_blockers(blocked(1, other, _blocker(9))) == [
        "#9",
        "DanielH2018/dotfiles#558",
    ]


def test_an_open_blocker_still_withholds_once_the_date_has_passed():
    issue = blocked(1, _blocker(2), labels=["not-before:2026-11-01"])
    assert pickable([issue], live_claims=set(), pr_refs=set(), today=TODAY) == []


def test_claim_refuses_a_blocked_issue_and_names_the_way_out():
    with pytest.raises(ClaimRefused) as exc:
        plan_claim(
            blocked(7195, _blocker(7178)),
            worktree="wt",
            session=None,
            when="now",
            today=TODAY,
        )
    assert "blocked by #7178" in exc.value.reason
    assert "block 7195 --clear" in exc.value.reason


# --- the planner ------------------------------------------------------------------------------


def test_block_clear_without_on_removes_every_blocker_open_or_closed():
    other = _blocker(558, url="https://github.com/DanielH2018/dotfiles/issues/558")
    issue = blocked(1, _blocker(2, state="CLOSED"), other)
    assert plan_block(issue, on=[], clear=True) == [
        [
            "issue",
            "edit",
            "1",
            "--remove-blocked-by",
            "2,https://github.com/DanielH2018/dotfiles/issues/558",
        ]
    ]


@pytest.mark.parametrize(
    ("issue", "on", "clear", "reason"),
    [
        (make_issue(1, state="CLOSED"), ["2"], False, "closed"),
        (make_issue(1), ["1"], False, "cannot block itself"),
        (make_issue(1), [], True, "not blocked"),
    ],
    ids=["closed", "self", "clear-with-nothing-to-clear"],
)
def test_block_refuses(issue, on, clear, reason):
    with pytest.raises(ClaimRefused) as exc:
        plan_block(issue, on=on, clear=clear)
    assert reason in exc.value.reason


# --- the CLI ----------------------------------------------------------------------------------


def test_block_cli_writes_the_relationship(capsys, make_tools):
    tools, calls = make_tools(Fakes(view=make_issue(7195)))
    assert findings.main(["block", "7195", "--on", "7178,7180"], tools) == 0
    assert calls.gh == [["issue", "edit", "7195", "--add-blocked-by", "7178,7180"]]
    assert "#7195 blocked by 7178, 7180" in capsys.readouterr().out


def test_block_cli_needs_on_or_clear(make_tools):
    tools, calls = make_tools(Fakes(view=make_issue(7195)))
    assert findings.main(["block", "7195"], tools) == FINDINGS_BAD_ARGS
    assert calls.gh == []


def test_open_blocked_by_adds_the_relationship_after_create(tmp_path, make_tools):
    body = tmp_path / "body.md"
    body.write_text("details")
    tools, calls = make_tools(Fakes())
    argv = ["open", "--title", "T", "--body-file", str(body), "--severity", "low"]
    argv += ["--kind", "gap", "--blocked-by", "7178"]
    assert findings.main(argv, tools) == 0
    kinds = [c[:2] for c in calls.gh]
    assert kinds.index(["issue", "create"]) < kinds.index(["issue", "edit"])
    assert calls.gh[-1][-2:] == ["--add-blocked-by", "7178"]


def test_next_withholds_a_blocked_issue_and_names_its_blocker(capsys, make_tools):
    tools, _ = make_tools(
        Fakes(
            issues=[
                blocked(7195, _blocker(7178), title="retire the redaction"),
                make_issue(1140, title="free"),
            ],
            worktree_facts=facts(),
        )
    )
    assert findings.main(["next", "--json"], tools) == 0
    captured = capsys.readouterr()
    assert [r["number"] for r in json.loads(captured.out)] == [1140]
    assert "blocked: #7195 by #7178  retire the redaction" in captured.err


def test_list_marks_an_open_blocker_and_not_a_closed_one(capsys, make_tools):
    tools, _ = make_tools(
        Fakes(
            issues=[
                blocked(1, _blocker(9), title="waits"),
                blocked(2, _blocker(8, state="CLOSED"), title="free"),
            ]
        )
    )
    assert findings.main(["list"], tools) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "[blocked by #9]" in next(line for line in lines if "waits" in line)
    assert "blocked" not in next(line for line in lines if line.endswith("free"))
