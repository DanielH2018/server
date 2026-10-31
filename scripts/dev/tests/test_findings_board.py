"""The "Not before" date on the Claude findings board follows the `not-before:` label.

Run: uv run pytest scripts/dev/tests/test_findings_board.py
"""

import json
import subprocess
from datetime import date

from _findings_fakes import (
    ADDED_ITEM,
    Fakes,
    board_answer,
    graphql_kind,
    make_issue,
)

from dev import findings
from dev.findings_lib.board import parse_board_state, plan_board_date

DAY = date(2026, 11, 7)


def _state(**kw):
    return parse_board_state(board_answer(**kw))


def _board_calls(calls):
    return [graphql_kind(c) for c in calls.gh if c[:2] == ["api", "graphql"]]


def _var(call, name):
    return next(a for a in call if a.startswith(f"{name}="))[len(name) + 1 :]


# --- the planner ------------------------------------------------------------------------------


def test_an_issue_off_the_board_is_added_then_dated():
    assert plan_board_date(_state(on_board=False), DAY) == ["add", "set"]


def test_a_board_date_that_already_matches_plans_nothing():
    assert plan_board_date(_state(current="2026-11-07"), DAY) == []


def test_a_moved_date_is_set_in_place():
    assert plan_board_date(_state(current="2026-10-01"), DAY) == ["set"]


def test_a_cleared_deferral_clears_the_board_date():
    assert plan_board_date(_state(current="2026-10-01"), None) == ["clear"]


def test_clearing_an_issue_off_the_board_plans_nothing():
    assert plan_board_date(_state(on_board=False), None) == []


def test_a_board_without_the_field_is_refused_with_the_command_that_creates_it():
    why = plan_board_date(_state(field=False), DAY)
    assert isinstance(why, str) and "gh project field-create" in why


def test_a_board_whose_title_only_contains_the_name_is_not_written():
    state = _state(title="Claude findings archive")
    assert state.project_id is None
    assert "no Project titled" in plan_board_date(state, DAY)


# --- the commands -----------------------------------------------------------------------------


def test_defer_writes_the_board_date_after_the_label(make_tools):
    fakes = Fakes(view=make_issue(1288), board={1288: board_answer(on_board=False)})
    tools, calls = make_tools(fakes)
    assert findings.main(["defer", "1288", "--until", "2026-11-07"], tools) == 0
    kinds = [c[:2] for c in calls.gh]
    assert kinds.index(["issue", "edit"]) < kinds.index(["api", "graphql"])
    assert _board_calls(calls) == ["read", "add", "set"]
    set_call = calls.gh[-1]
    assert _var(set_call, "date") == "2026-11-07"
    assert _var(set_call, "item") == ADDED_ITEM


def test_defer_clear_clears_the_board_date(make_tools):
    issue = make_issue(1288, labels=["not-before:2099-01-01"])
    fakes = Fakes(view=issue, board={1288: board_answer(current="2099-01-01")})
    tools, calls = make_tools(fakes)
    assert findings.main(["defer", "1288", "--clear"], tools) == 0
    assert _board_calls(calls) == ["read", "clear"]


def test_a_board_failure_warns_and_defer_still_succeeds(capsys, make_tools):
    scope = subprocess.CalledProcessError(
        1, ["gh"], stderr="missing required scopes [project]\n"
    )
    fakes = Fakes(
        view=make_issue(1288),
        board={1288: board_answer()},
        gh_errors={"api graphql": scope},
    )
    tools, calls = make_tools(fakes)
    assert findings.main(["defer", "1288", "--until", "2026-11-07"], tools) == 0
    err = capsys.readouterr().err
    assert "#1288 board date not written: missing required scopes" in err
    assert ["issue", "edit"] in [c[:2] for c in calls.gh]


def test_defer_dry_run_reads_the_board_and_writes_nothing(capsys, make_tools):
    fakes = Fakes(view=make_issue(1288), board={1288: board_answer(on_board=False)})
    tools, calls = make_tools(fakes)
    argv = ["defer", "1288", "--until", "2026-11-07", "--dry-run"]
    assert findings.main(argv, tools) == 0
    assert _board_calls(calls) == ["read"]
    out = capsys.readouterr().out
    assert "# add #1288" in out and "# set #1288 2026-11-07" in out


def test_an_aimed_register_reads_its_own_owner_and_repo(make_tools):
    fakes = Fakes(view=make_issue(7), board={7: board_answer(current="2026-11-07")})
    tools, calls = make_tools(fakes)
    argv = ["defer", "7", "--until", "2026-11-07", "--repo", "DanielH2018/dotfiles"]
    assert findings.main(argv, tools) == 0
    read = next(c for c in calls.gh if c[:2] == ["api", "graphql"])
    assert _var(read, "owner") == "DanielH2018" and _var(read, "name") == "dotfiles"
    assert "--repo" not in read


def test_board_dates_matches_every_open_issue_to_its_label(capsys, make_tools):
    issues = [
        make_issue(1, labels=["not-before:2099-01-01"]),
        make_issue(2),
        make_issue(3, labels=["not-before:2026-11-07"]),
    ]
    board = {
        1: board_answer(on_board=False),
        2: board_answer(current="2026-10-01"),
        3: board_answer(current="2026-11-07"),
    }
    tools, calls = make_tools(Fakes(issues=issues, board=board))
    assert findings.main(["board-dates"], tools) == 0
    assert _board_calls(calls) == ["read", "add", "set", "read", "clear", "read"]
    out = capsys.readouterr().out
    assert "1 set, 1 cleared, 1 unchanged, 0 failed" in out


def test_board_dates_exits_1_when_the_board_lacks_the_field(capsys, make_tools):
    issues = [make_issue(1, labels=["not-before:2099-01-01"])]
    tools, _ = make_tools(Fakes(issues=issues, board={1: board_answer(field=False)}))
    assert findings.main(["board-dates"], tools) == 1
    assert "0 set, 0 cleared, 0 unchanged, 1 failed" in capsys.readouterr().out


def test_the_fake_board_answer_parses_like_the_live_response():
    # Trimmed from the live read of #6710 on 2026-10-31, before the field existed.
    live = {
        "data": {
            "repository": {
                "issue": {
                    "id": "I_x",
                    "projectItems": {
                        "nodes": [
                            {
                                "id": "PVTI_x",
                                "project": {"id": "PVT_x"},
                                "fieldValueByName": None,
                            }
                        ]
                    },
                },
                "owner": {
                    "projectsV2": {
                        "nodes": [
                            {
                                "id": "PVT_x",
                                "title": "Claude findings",
                                "fields": {"nodes": [{"id": "F", "name": "Title"}, {}]},
                            }
                        ]
                    }
                },
            }
        }
    }
    state = parse_board_state(json.loads(json.dumps(live)))
    assert (state.item_id, state.field_id, state.current) == ("PVTI_x", None, None)
