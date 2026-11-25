"""A not-before date: `defer` sets it, `open --not-before` files with it, `next` and `list`
show it, and it expires by comparison rather than by anyone clearing it.

The date is the Not Before issue field. A `not-before:` label still counts, for sessions on a
pre-field checkout, and the later of the two wins.

`main(...)` reads the real clock, so the end-to-end cases use dates far enough either side of
today that the verdict cannot flip: 2099 is still deferred, 2000 is long past.

Run: uv run pytest scripts/dev/tests/findings/test_findings_defer.py
"""

import json
import subprocess
from datetime import date

import pytest
from _findings_fakes import Fakes, facts, make_issue, typed

from dev import findings
from dev.findings_lib.plans import ClaimRefused, plan_defer


def _deferred(number, day, **kwargs):
    return {**make_issue(number, **kwargs), "fields": {"Not Before": day}}


def _field_writes(calls) -> list[str]:
    return [
        c[3]
        for c in calls.gh_json
        if c[:2] == ["api", "graphql"] and "setIssueFieldValue" in c[3]
    ]


# --- the reader -------------------------------------------------------------------------------


def test_a_later_label_from_a_pre_field_session_still_withholds_the_issue():
    issue = _deferred(1288, "2026-09-12", labels=["not-before:2026-09-20"])
    assert typed(issue).not_before == date(2026, 9, 20)


def test_an_unparseable_date_counts_as_no_deferral():
    assert (
        typed(_deferred(1288, "soon", labels=["not-before:later"])).not_before is None
    )


# --- the planner ------------------------------------------------------------------------------


def test_defer_removes_every_not_before_label_so_none_outlives_the_field():
    issue = make_issue(1288, labels=["not-before:2026-09-12", "not-before:2026-09-20"])
    plans = plan_defer(typed(issue), until=date(2026, 9, 1))
    assert plans[0] == [
        "issue",
        "edit",
        "1288",
        "--remove-label",
        "not-before:2026-09-12",
        "--remove-label",
        "not-before:2026-09-20",
    ]


def test_defer_clear_on_an_undeferred_issue_is_refused():
    with pytest.raises(ClaimRefused) as exc:
        plan_defer(typed(make_issue(1288)), until=None)
    assert "not deferred" in exc.value.reason


def test_defer_on_a_closed_issue_is_refused():
    with pytest.raises(ClaimRefused) as exc:
        plan_defer(typed(make_issue(1288, state="CLOSED")), until=date(2026, 9, 12))
    assert "closed" in exc.value.reason


# --- the CLI ----------------------------------------------------------------------------------


def test_defer_cli_sets_the_field_and_comments(capsys, make_tools):
    tools, calls = make_tools(Fakes(view=make_issue(1288)))
    assert findings.main(["defer", "1288", "--until", "2026-09-12"], tools) == 0
    [write] = _field_writes(calls)
    assert '"I_1288"' in write and 'dateValue: "2026-09-12"' in write
    assert [c[:2] for c in calls.gh] == [["issue", "comment"]]
    assert "#1288 deferred until 2026-09-12" in capsys.readouterr().out


def test_defer_cli_clear_deletes_the_field(make_tools):
    tools, calls = make_tools(Fakes(view=_deferred(1288, "2099-01-01")))
    assert findings.main(["defer", "1288", "--clear"], tools) == 0
    [write] = _field_writes(calls)
    assert "delete: true" in write


def test_defer_cli_clear_exits_3_when_nothing_is_deferred(capsys, make_tools):
    tools, calls = make_tools(Fakes(view=make_issue(1288)))
    assert findings.main(["defer", "1288", "--clear"], tools) == 3
    assert not calls.gh and not _field_writes(calls)
    assert "not deferred" in capsys.readouterr().err


def test_defer_cli_rejects_a_date_that_is_not_a_date(make_tools):
    tools, _ = make_tools(Fakes(view=make_issue(1288)))
    with pytest.raises(SystemExit) as exc:
        findings.main(["defer", "1288", "--until", "tomorrow"], tools)
    assert exc.value.code == 64


def test_open_cli_with_not_before_sets_both_fields_on_the_new_issue(
    tmp_path, make_tools
):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, calls = make_tools(Fakes())
    argv = ["open", "--title", "T", "--body-file", str(body), "--severity", "low"]
    argv += ["--kind", "gap", "--not-before", "2026-09-12"]
    assert findings.main(argv, tools) == 0
    create = next(c for c in calls.gh if c[:2] == ["issue", "create"])
    assert not any(a.startswith(("not-before:", "severity/")) for a in create)
    [write] = _field_writes(calls)
    assert '"o_Low"' in write and 'dateValue: "2026-09-12"' in write


def test_next_withholds_a_deferred_issue_and_names_it(capsys, make_tools):
    tools, _ = make_tools(
        Fakes(
            issues=[
                _deferred(1288, "2099-01-01", title="needs seven days of metric"),
                make_issue(1140, title="free"),
            ],
            worktree_facts=facts(),
        )
    )
    assert findings.main(["next", "--waiting"], tools) == 0
    out = capsys.readouterr().out
    assert "#1140" in out
    assert "deferred: #1288 until 2099-01-01  needs seven days of metric" in out
    assert out.index("deferred:") < out.index("#1140")


def test_next_offers_nothing_when_the_field_read_is_refused(capsys, make_tools):
    """A failed field read must not read as "nothing is deferred" and offer #1288."""
    refused = subprocess.CalledProcessError(1, "gh", stderr="API rate limit exceeded")
    tools, _ = make_tools(
        Fakes(
            issues=[_deferred(1288, "2099-01-01")],
            json_errors={"api graphql": refused},
            worktree_facts=facts(),
        )
    )
    assert findings.main(["next"], tools) == 1
    assert "#1288" not in capsys.readouterr().out


def test_next_counts_the_waiting_issues_and_names_the_flag_that_lists_them(
    capsys, make_tools
):
    tools, _ = make_tools(
        Fakes(
            issues=[_deferred(1288, "2099-01-01"), make_issue(1140, title="free")],
            worktree_facts=facts(),
        )
    )
    assert findings.main(["next"], tools) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "1 waiting (1 deferred); --waiting to list"
    assert "#1288" not in out


def test_next_offers_an_issue_whose_date_has_passed(capsys, make_tools):
    tools, _ = make_tools(
        Fakes(issues=[_deferred(1288, "2000-01-01")], worktree_facts=facts())
    )
    assert findings.main(["next"], tools) == 0
    out = capsys.readouterr().out
    assert "#1288" in out
    assert "deferred" not in out


def test_next_json_keeps_the_array_free_and_notes_the_deferral_on_stderr(
    capsys, make_tools
):
    """The array is what `issue-fanout` claims; a deferred row inside it would be claimed."""
    tools, _ = make_tools(
        Fakes(
            issues=[_deferred(1288, "2099-01-01"), make_issue(1140)],
            worktree_facts=facts(),
        )
    )
    assert findings.main(["next", "--json", "--waiting"], tools) == 0
    captured = capsys.readouterr()
    assert [r["number"] for r in json.loads(captured.out)] == [1140]
    assert "deferred: #1288 until 2099-01-01" in captured.err


def test_list_marks_a_live_deferral_and_not_an_expired_one(capsys, make_tools):
    tools, _ = make_tools(
        Fakes(
            issues=[
                _deferred(1288, "2099-01-01", title="later"),
                _deferred(1140, "2000-01-01", title="now"),
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
    tools, _ = make_tools(Fakes(issues=[_deferred(1288, "2099-01-01")]))
    assert findings.main(["list", "--json"], tools) == 0
    assert json.loads(capsys.readouterr().out)[0]["not_before"] == "2099-01-01"
