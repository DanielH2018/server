"""A not-before date: `defer` plans it, `open --not-before` files with it, `next` and `list`
show it, and it expires by comparison rather than by anyone clearing it (#1739).

`main(...)` reads the real clock, so the end-to-end cases use dates far enough either side of
today that the verdict cannot flip: 2099 is still deferred, 2000 is long past.

Run: uv run pytest scripts/dev/tests/test_findings_defer.py
"""

import json
from datetime import date

import pytest
from _findings_fakes import Fakes, facts, make_issue

from dev import findings
from dev.findings_lib.issue_model import LABELS, NOT_BEFORE_STYLE
from dev.findings_lib.plans import (
    ClaimRefused,
    plan_defer,
    plan_ensure_label,
    plan_open,
)

FUTURE = "not-before:2099-01-01"
PAST = "not-before:2000-01-01"
ALL_LABELS = set(LABELS)


# --- the planners -----------------------------------------------------------------------------


def test_ensure_label_creates_a_dated_label_the_repo_lacks():
    plans = plan_ensure_label("not-before:2026-09-12", ALL_LABELS)
    colour, desc = NOT_BEFORE_STYLE
    assert plans == [
        [
            "label",
            "create",
            "not-before:2026-09-12",
            "--color",
            colour,
            "--description",
            desc,
        ]
    ]


def test_ensure_label_plans_nothing_for_a_label_the_repo_has():
    assert plan_ensure_label("not-before:2026-09-12", {"not-before:2026-09-12"}) == []


def test_defer_creates_the_label_then_adds_it_and_comments():
    plans = plan_defer(
        make_issue(1288), until=date(2026, 9, 12), existing_labels=ALL_LABELS
    )
    assert plans[0][:3] == ["label", "create", "not-before:2026-09-12"]
    assert plans[1] == ["issue", "edit", "1288", "--add-label", "not-before:2026-09-12"]
    assert plans[2] == [
        "issue",
        "comment",
        "1288",
        "--body",
        "Deferred until 2026-09-12.",
    ]


def test_defer_to_a_new_date_replaces_the_old_label_in_one_edit():
    """One label per issue, so `not_before` never has to pick between two dates."""
    issue = make_issue(1288, labels=["not-before:2026-09-12"])
    plans = plan_defer(
        issue,
        until=date(2026, 9, 20),
        existing_labels=ALL_LABELS | {"not-before:2026-09-20"},
    )
    assert plans[0] == [
        "issue",
        "edit",
        "1288",
        "--add-label",
        "not-before:2026-09-20",
        "--remove-label",
        "not-before:2026-09-12",
    ]


def test_defer_to_the_same_date_again_plans_no_label_churn():
    issue = make_issue(1288, labels=["not-before:2026-09-12"])
    plans = plan_defer(
        issue,
        until=date(2026, 9, 12),
        existing_labels=ALL_LABELS | {"not-before:2026-09-12"},
    )
    assert plans[0] == ["issue", "edit", "1288", "--add-label", "not-before:2026-09-12"]


def test_defer_clear_removes_every_not_before_label():
    issue = make_issue(1288, labels=["not-before:2026-09-12", "not-before:2026-09-20"])
    plans = plan_defer(issue, until=None, existing_labels=ALL_LABELS)
    assert plans[0] == [
        "issue",
        "edit",
        "1288",
        "--remove-label",
        "not-before:2026-09-12",
        "--remove-label",
        "not-before:2026-09-20",
    ]
    assert plans[1][:3] == ["issue", "comment", "1288"]


def test_defer_clear_on_an_undeferred_issue_is_refused():
    with pytest.raises(ClaimRefused) as exc:
        plan_defer(make_issue(1288), until=None, existing_labels=ALL_LABELS)
    assert "not deferred" in exc.value.reason


def test_defer_on_a_closed_issue_is_refused():
    with pytest.raises(ClaimRefused) as exc:
        plan_defer(
            make_issue(1288, state="CLOSED"),
            until=date(2026, 9, 12),
            existing_labels=ALL_LABELS,
        )
    assert "closed" in exc.value.reason


def test_open_with_a_date_adds_the_label_to_the_create():
    _, _, plans = plan_open(
        None,
        title="T",
        body="B",
        labels=["claude"],
        fp="0" * 12,
        source="session",
        defer_until=date(2026, 9, 12),
    )
    assert "not-before:2026-09-12" in plans[0]


def test_open_without_a_date_adds_no_such_label():
    _, _, plans = plan_open(
        None, title="T", body="B", labels=["claude"], fp="0" * 12, source="session"
    )
    assert not any(a.startswith("not-before:") for a in plans[0])


# --- the CLI ----------------------------------------------------------------------------------


def test_defer_cli_creates_the_label_once(capsys, make_tools):
    tools, calls = make_tools(Fakes(view=make_issue(1288)))
    argv = ["defer", "1288", "--until", "2026-09-12"]
    assert findings.main(argv, tools) == 0
    assert [c[:2] for c in calls.gh] == [
        ["label", "create"],
        ["issue", "edit"],
        ["issue", "comment"],
    ]
    assert "#1288 deferred until 2026-09-12" in capsys.readouterr().out


def test_defer_cli_skips_the_create_when_the_label_exists(make_tools):
    fakes = Fakes(view=make_issue(1288), labels=ALL_LABELS | {"not-before:2026-09-12"})
    tools, calls = make_tools(fakes)
    assert findings.main(["defer", "1288", "--until", "2026-09-12"], tools) == 0
    assert [c[:2] for c in calls.gh] == [["issue", "edit"], ["issue", "comment"]]


def test_defer_cli_clear_exits_3_when_nothing_is_deferred(capsys, make_tools):
    tools, calls = make_tools(Fakes(view=make_issue(1288)))
    assert findings.main(["defer", "1288", "--clear"], tools) == 3
    assert not calls.gh
    assert "not deferred" in capsys.readouterr().out


def test_defer_cli_rejects_a_date_that_is_not_a_date(make_tools):
    tools, _ = make_tools(Fakes(view=make_issue(1288)))
    with pytest.raises(SystemExit) as exc:
        findings.main(["defer", "1288", "--until", "tomorrow"], tools)
    assert exc.value.code == 2


def test_open_cli_with_not_before_creates_the_label_before_the_issue(
    tmp_path, make_tools
):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, calls = make_tools(Fakes())
    argv = [
        "open",
        "--title",
        "T",
        "--body-file",
        str(body),
        "--severity",
        "low",
        "--kind",
        "gap",
        "--not-before",
        "2026-09-12",
    ]
    assert findings.main(argv, tools) == 0
    kinds = [c[:2] for c in calls.gh]
    assert kinds.index(["label", "create"]) < kinds.index(["issue", "create"])
    create = next(c for c in calls.gh if c[:2] == ["issue", "create"])
    assert "not-before:2026-09-12" in create


def test_next_withholds_a_deferred_issue_and_names_it(capsys, make_tools):
    tools, _ = make_tools(
        Fakes(
            issues=[
                make_issue(1288, labels=[FUTURE], title="needs seven days of metric"),
                make_issue(1140, title="free"),
            ],
            worktree_facts=facts(),
        )
    )
    assert findings.main(["next"], tools) == 0
    out = capsys.readouterr().out
    assert "#1140" in out
    assert "deferred: #1288 until 2099-01-01  needs seven days of metric" in out
    assert out.index("#1140") < out.index("deferred:")


def test_next_offers_an_issue_whose_date_has_passed(capsys, make_tools):
    tools, _ = make_tools(
        Fakes(issues=[make_issue(1288, labels=[PAST])], worktree_facts=facts())
    )
    assert findings.main(["next"], tools) == 0
    out = capsys.readouterr().out
    assert "#1288" in out
    assert "deferred" not in out


def test_next_json_keeps_the_array_free_and_notes_the_deferral_on_stderr(
    capsys, make_tools
):
    """The array is what `issue-fanout` claims; a deferred row inside it is #1739 inverted."""
    tools, _ = make_tools(
        Fakes(
            issues=[make_issue(1288, labels=[FUTURE]), make_issue(1140)],
            worktree_facts=facts(),
        )
    )
    assert findings.main(["next", "--json"], tools) == 0
    captured = capsys.readouterr()
    assert [r["number"] for r in json.loads(captured.out)] == [1140]
    assert "deferred: #1288 until 2099-01-01" in captured.err


def test_list_marks_a_live_deferral_and_not_an_expired_one(capsys, make_tools):
    tools, _ = make_tools(
        Fakes(
            issues=[
                make_issue(1288, labels=[FUTURE], title="later"),
                make_issue(1140, labels=[PAST], title="now"),
            ]
        )
    )
    assert findings.main(["list"], tools) == 0
    lines = capsys.readouterr().out.splitlines()
    later = next(line for line in lines if "later" in line)
    now = next(line for line in lines if line.endswith("now"))
    assert "[deferred until 2099-01-01]" in later
    assert "deferred" not in now


def test_list_json_carries_the_date_for_the_docs_generator(capsys, make_tools):
    tools, _ = make_tools(Fakes(issues=[make_issue(1288, labels=[FUTURE])]))
    assert findings.main(["list", "--json"], tools) == 0
    assert json.loads(capsys.readouterr().out)[0]["not_before"] == "2099-01-01"
