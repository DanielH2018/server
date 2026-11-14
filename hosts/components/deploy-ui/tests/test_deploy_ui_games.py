"""Readers behind the Games panel. Each parser gets an accept and a reject case."""

import json

import pytest

from deploy_ui_lib import deploy_ui_games as games

GAMES = games.parse_games("valheim=valheim,valheim-stats")


def _deployment(name, replicas, ready=0):
    return {
        "metadata": {"name": name},
        "spec": {"replicas": replicas},
        "status": {"readyReplicas": ready} if ready else {},
    }


def _pod(app, phase="Running", ready=True):
    return {
        "metadata": {"name": f"{app}-abc", "labels": {"app": app}},
        "status": {
            "phase": phase,
            "startTime": "2026-11-14T10:00:00Z",
            "containerStatuses": [{"ready": ready, "restartCount": 2}],
        },
    }


def _rows(deployments, pods):
    return games.game_rows(
        GAMES, json.dumps({"items": deployments}), json.dumps({"items": pods})
    )


def test_parse_games_reads_each_game_and_its_server_first_is_clean():
    parsed = games.parse_games(
        "terraria=terraria,terraria-stats; valheim=valheim,valheim-stats"
    )
    assert [g.name for g in parsed] == ["terraria", "valheim"]
    assert parsed[1].server == "valheim"
    assert parsed[1].deployments == ("valheim", "valheim-stats")


def test_parse_games_entry_without_deployments_is_flagged():
    with pytest.raises(ValueError, match="valheim"):
        games.parse_games("terraria=terraria;valheim=")


def test_running_game_reports_each_deployment_and_its_pod_is_clean():
    [row] = _rows(
        [_deployment("valheim", 1, 1), _deployment("valheim-stats", 1, 1)],
        [_pod("valheim"), _pod("valheim-stats")],
    )
    assert row["state"] == "running"
    server = row["deployments"][0]
    assert (server["desired"], server["ready"]) == (1, 1)
    assert server["pods"][0]["restarts"] == 2
    assert server["pods"][0]["started"] == 1794650400


@pytest.mark.parametrize(
    ("deployments", "pods", "state"),
    [
        (
            [_deployment("valheim", 1), _deployment("valheim-stats", 1, 1)],
            [_pod("valheim", "Pending", ready=False), _pod("valheim-stats")],
            "starting",
        ),
        (
            [_deployment("valheim", 0), _deployment("valheim-stats", 0)],
            [_pod("valheim")],
            "stopping",
        ),
        (
            [_deployment("valheim", 1, 1), _deployment("valheim-stats", 0)],
            [_pod("valheim")],
            "partial",
        ),
        ([_deployment("valheim", 0)], [], "missing"),
    ],
)
def test_game_between_states_is_flagged(deployments, pods, state):
    assert _rows(deployments, pods)[0]["state"] == state


def test_pod_of_another_app_is_not_counted_is_flagged():
    [row] = _rows(
        [_deployment("valheim", 0), _deployment("valheim-stats", 0)], [_pod("terraria")]
    )
    assert row["state"] == "stopped"


def test_parse_console_merges_streams_newest_last_is_clean():
    loki = {
        "data": {
            "result": [
                {"values": [["3000000000", "c"], ["1000000000", "a"]]},
                {"values": [["2000000000", "b"]]},
            ]
        }
    }
    assert games.parse_console(json.dumps(loki), 2) == [
        {"ts": 2, "line": "b"},
        {"ts": 3, "line": "c"},
    ]


def test_parse_console_without_results_is_empty_is_flagged():
    assert games.parse_console('{"status":"success","data":{"result":[]}}', 10) == []
