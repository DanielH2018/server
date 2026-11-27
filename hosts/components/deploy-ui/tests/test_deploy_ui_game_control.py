"""The Games panel's reads and writes, over a fake of the app's subprocess edge."""

import json
import threading
from pathlib import Path

import pytest

import deploy_ui
from deploy_ui_lib import deploy_ui_game_control as game_control
from deploy_ui_lib.deploy_ui_games import Game
from deploy_ui_lib.deploy_ui_reads import Unavailable

GAMES = (
    Game("valheim", ("valheim", "valheim-stats")),
    Game("terraria", ("terraria",), start_refused="issue #822"),
)
TOKEN = "games-token-value"
VIEW = ("kubectl", "config", "view", "--raw", "--minify", "-o")
APISERVER = {
    (*VIEW, "jsonpath={.clusters[0].cluster.server}"): ("https://127.0.0.1:6443", 0),
    (*VIEW, "jsonpath={.clusters[0].cluster.certificate-authority-data}"): (
        "Q0EK",
        0,
    ),
    ("curl",): ("{}", 0),
}
PODS = ("kubectl", "-n", "homelab", "get", "pods")


class FakeOut:
    """Answers each argv prefix with canned stdout, or raises for a nonzero rc."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv, timeout, ok_rcs=(0,), merge_stderr=False, stdin=None):
        self.calls.append((argv, stdin))
        for prefix, (out, rc) in self.table.items():
            if tuple(argv[: len(prefix)]) == prefix:
                if rc:
                    raise Unavailable(f"{argv[0]} exited {rc}: ")
                return out
        return ""


def _control(
    tmp_path: Path,
    table,
    token=True,
    loki_url="https://loki.example",
    restart_wait_s=300,
):
    creds = tmp_path / "creds"
    creds.mkdir(exist_ok=True)
    if token:
        (creds / game_control.GAMES_TOKEN).write_text(TOKEN + "\n")
    config = deploy_ui.Config(
        repo=tmp_path,
        log_dir=tmp_path / "logs",
        bind="",
        port=0,
        tree_lock=tmp_path / "lock",
        games=GAMES,
        credentials_dir=creds,
        loki_url=loki_url,
        restart_wait_s=restart_wait_s,
    )
    control = game_control.GameControl(config, FakeOut(table), sleep=lambda s: None)
    audited: list[str] = []
    control.audit = audited.append
    return control, audited


def _scale_calls(control):
    return [(argv, stdin) for argv, stdin in control._out.calls if argv[0] == "curl"]


def _act(control, game, action):
    return control.game_action({"game": game, "action": action})


@pytest.mark.parametrize(("action", "replicas"), [("start", 1), ("stop", 0)])
def test_start_and_stop_scale_every_deployment_of_the_game_is_clean(
    tmp_path, action, replicas
):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Starting and stopping a game
    control, audited = _control(tmp_path, APISERVER)
    assert _act(control, "valheim", action)[0] == 200
    calls = _scale_calls(control)
    assert [argv[-1] for argv, _ in calls] == [
        f"https://127.0.0.1:6443/apis/apps/v1/namespaces/homelab/deployments/{d}"
        f"/scale?fieldManager={game_control.GAMES_FIELD_MANAGER}"
        for d in ("valheim", "valheim-stats")
    ]
    for argv, stdin in calls:
        assert argv[argv.index("--data") + 1] == json.dumps(
            {"spec": {"replicas": replicas}}
        )
        assert stdin == f"Authorization: Bearer {TOKEN}\n"
        assert not any(TOKEN in arg for arg in argv)
    assert audited == [f"action=game-{action} game=valheim"]


@pytest.mark.parametrize("action", ["start", "restart"])
def test_a_refused_start_scales_nothing_is_flagged(tmp_path, action):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Starting and stopping a game
    control, audited = _control(tmp_path, APISERVER)
    status, text = _act(control, "terraria", action)
    assert (status, "issue #822" in text) == (409, True)
    assert _scale_calls(control) == [] and audited == []


def test_a_refused_game_can_still_be_stopped_is_clean(tmp_path):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Starting and stopping a game
    control, _ = _control(tmp_path, APISERVER)
    assert _act(control, "terraria", "stop")[0] == 200
    assert len(_scale_calls(control)) == 1


def test_an_unknown_action_scales_nothing_is_flagged(tmp_path):
    control, _ = _control(tmp_path, APISERVER)
    assert _act(control, "valheim", "delete")[0] == 400
    assert _scale_calls(control) == []


def test_a_write_without_the_token_is_unavailable_is_flagged(tmp_path):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Starting and stopping a game
    control, _ = _control(tmp_path, APISERVER, token=False)
    with pytest.raises(Unavailable, match="games token is not loaded"):
        _act(control, "valheim", "start")
    assert _scale_calls(control) == []


def test_a_restart_starts_the_game_once_its_pods_are_gone_is_clean(tmp_path):
    control, audited = _control(tmp_path, {**APISERVER, PODS: ("", 0)})
    control.finish_restart(GAMES[0])
    assert [argv[argv.index("--data") + 1] for argv, _ in _scale_calls(control)] == [
        json.dumps({"spec": {"replicas": 1}})
    ] * 2
    assert audited == ["action=game-restart game=valheim result=started"]


def test_a_restart_whose_pods_never_go_leaves_the_game_stopped_is_flagged(tmp_path):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Starting and stopping a game
    control, audited = _control(
        tmp_path, {**APISERVER, PODS: ("pod/valheim-abc\n", 0)}, restart_wait_s=0
    )
    control.finish_restart(GAMES[0])
    assert _scale_calls(control) == []
    assert "result=left-stopped" in audited[0]


def test_a_restart_stops_the_game_then_starts_it_again_is_clean(tmp_path):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Starting and stopping a game
    control, audited = _control(tmp_path, {**APISERVER, PODS: ("", 0)})
    restarted = threading.Event()

    def audit(line: str) -> None:
        audited.append(line)
        if "result=" in line:
            restarted.set()

    control.audit = audit
    status, text = _act(control, "valheim", "restart")
    # The restart's thread can end before this line, so wait on its last effect, not on it.
    assert restarted.wait(timeout=5)
    assert (status, "starts again" in text) == (200, True)
    replicas = [
        json.loads(argv[argv.index("--data") + 1])["spec"]["replicas"]
        for argv, _ in _scale_calls(control)
    ]
    assert replicas == [0, 0, 1, 1]
    assert audited == [
        "action=game-restart game=valheim",
        "action=game-restart game=valheim result=started",
    ]


def test_a_restart_that_cannot_scale_back_leaves_the_game_stopped_is_flagged(
    tmp_path,
):
    control, audited = _control(tmp_path, {**APISERVER, PODS: ("", 0)}, token=False)
    control.finish_restart(GAMES[0])
    assert _scale_calls(control) == []
    assert audited[0].startswith(
        "action=game-restart game=valheim result=left-stopped reason="
    )


def test_games_reads_the_deployments_and_their_pods_is_clean(tmp_path):
    deployments = {
        "items": [
            {"metadata": {"name": n}, "spec": {"replicas": 0}, "status": {}}
            for n in ("valheim", "valheim-stats", "terraria")
        ]
    }
    control, _ = _control(
        tmp_path,
        {
            ("kubectl", "-n", "homelab", "get", "deployments"): (
                json.dumps(deployments),
                0,
            ),
            PODS: ('{"items": []}', 0),
        },
    )
    rows = {g["name"]: g for g in control.game_servers()["games"]}
    assert rows["valheim"]["state"] == "stopped"
    assert rows["terraria"]["start_refused"] == "issue #822"
    out = control._out
    assert isinstance(out, FakeOut)
    pods_call = next(argv for argv, _ in out.calls if "pods" in argv)
    assert (
        pods_call[pods_call.index("-l") + 1]
        == "app in (valheim,valheim-stats,terraria)"
    )


def test_game_console_queries_loki_for_the_server_container_is_clean(tmp_path):
    loki = {"data": {"result": [{"values": [["2000000000", "Game server connected"]]}]}}
    control, _ = _control(tmp_path, {("curl",): (json.dumps(loki), 0)})
    assert control.game_console("valheim") == {
        "game": "valheim",
        "lines": [{"ts": 2, "line": "Game server connected"}],
    }
    out = control._out
    assert isinstance(out, FakeOut)
    [(argv, _)] = out.calls
    assert "https://loki.example/loki/api/v1/query_range" in argv
    assert 'query={namespace="homelab",container="valheim"}' in argv


@pytest.mark.parametrize(
    ("game", "loki_url", "reason"),
    [
        ("minecraft", "https://loki.example", "no game named"),
        ("valheim", "", "DEPLOY_UI_LOKI is not set"),
    ],
)
def test_game_console_refusals_are_unavailable_is_flagged(
    tmp_path, game, loki_url, reason
):
    control, _ = _control(tmp_path, {}, loki_url=loki_url)
    with pytest.raises(Unavailable, match=reason):
        control.game_console(game)
