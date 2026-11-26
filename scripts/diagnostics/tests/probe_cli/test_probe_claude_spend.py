"""Tests for `probe.py claude-spend`: the per-kind split and the coordinator join."""

import re

from diagnostics.probe_lib import claude_spend


def _series(usd, **labels):
    return {"metric": labels, "value": [0, str(usd)]}


COSTS = claude_spend.parse_costs(
    [
        _series(30, app_entrypoint="claude-desktop", session_id="root"),
        _series(5, app_entrypoint="claude-desktop", session_id="operator"),
        _series(
            2,
            app_entrypoint="sdk-cli",
            session_id="batch",
            session_kind="fanout-review",
        ),
        _series(1, app_entrypoint="sdk-cli", session_id="headless"),
        _series(4, app_entrypoint="claude-desktop", session_id="worker"),
    ]
)


def test_only_an_unlabelled_desktop_session_is_split_by_the_coordinator_join():
    kinds = {
        c.session_id: claude_spend.session_kind(c, frozenset({"root", "batch"}))
        for c in COSTS
    }
    assert kinds == {
        "root": "desktop-coordinator",
        "operator": "desktop-interactive",
        "batch": "fanout-review",
        "headless": "unlabelled sdk-cli",
        "worker": "desktop-interactive",
    }


def test_the_report_sums_each_kind_and_lists_the_costliest_first():
    lines = claude_spend.format_spend(COSTS, frozenset({"root"}), "7d")
    assert lines[0] == "$42.00 over 7d across 5 sessions"
    rows = [line.split() for line in lines[2:]]
    assert [r[0] for r in rows] == [
        "desktop-coordinator",
        "desktop-interactive",
        "fanout-review",
        "unlabelled",
    ]
    assert rows[1][1:] == ["9.00", "21%", "2"]


def test_the_coordinator_ids_come_from_every_series_of_the_loki_response():
    data = {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"session_id": "root"}, "values": [[0, "3"]]},
                {"metric": {"session_id": "sub"}, "values": [[0, "1"]]},
            ]
        },
    }
    assert claude_spend.parse_session_ids(data) == frozenset({"root", "sub"})


def test_the_coordinator_filter_matches_the_skill_and_start_session_calls_only():
    pattern = re.search(r"tool_parameters=~`(.*)`", claude_spend.coordinator_logql(60))
    assert pattern
    matches = re.compile(pattern.group(1)).fullmatch
    assert matches('{"skill_name":"coordinate"}')
    assert matches('{"mcp_server_name":"ccd_session","mcp_tool_name":"start_session"}')
    assert not matches('{"skill_name":"pr-authoring"}')
    assert not matches('{"full_command":"grep -rn \\"start_session\\" .claude/skills"}')
    assert not matches('{"skill_name":"issue-fanout","args":"coordinate"}')
    assert not matches('{"mcp_server_name":"other","mcp_tool_name":"start_session"}')
    assert not matches(
        '{"mcp_server_name":"ccd_session_mgmt","mcp_tool_name":"list_sessions"}'
    )
