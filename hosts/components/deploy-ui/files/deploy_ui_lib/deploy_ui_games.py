"""Readers behind the Games panel: the game registry, each game's state, its console tail.

Every function here is pure. `deploy_ui.App` runs `kubectl` and `curl` and hands their
stdout in, so the parsing is tested without a cluster or a Loki.
"""

import json
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Game:
    """One game server and every Deployment that runs only while it does.

    Attributes:
        name: the name the page shows and the API takes.
        deployments: the server's Deployment first, then the ones that follow it.
    """

    name: str
    deployments: tuple[str, ...]

    @property
    def server(self) -> str:
        return self.deployments[0]


def parse_games(spec: str) -> tuple[Game, ...]:
    """The registry from `DEPLOY_UI_GAMES`, written `name=server,dep;name=server,dep`.

    Raises:
        ValueError: an entry has no name or no Deployment.
    """
    games = []
    for entry in filter(None, (e.strip() for e in spec.split(";"))):
        name, _, deps = entry.partition("=")
        deployments = tuple(d.strip() for d in deps.split(",") if d.strip())
        if not name.strip() or not deployments:
            raise ValueError(
                f"game entry {entry!r} is not name=deployment[,deployment]"
            )
        games.append(Game(name.strip(), deployments))
    return tuple(games)


def pod_selector(games: tuple[Game, ...]) -> str:
    """The `kubectl -l` selector naming every game Deployment's pods by their `app` label."""
    return f"app in ({','.join(d for g in games for d in g.deployments)})"


def _epoch(stamp: str | None) -> int | None:
    return int(datetime.fromisoformat(stamp).timestamp()) if stamp else None


def _pod_row(pod: dict) -> dict:
    statuses = pod.get("status", {}).get("containerStatuses", [])
    return {
        "name": pod["metadata"]["name"],
        "phase": pod.get("status", {}).get("phase", "Unknown"),
        "ready": bool(statuses) and all(s.get("ready") for s in statuses),
        "restarts": sum(s.get("restartCount", 0) for s in statuses),
        "started": _epoch(pod.get("status", {}).get("startTime")),
    }


def _deployment_row(name: str, deployment: dict | None, pods: list[dict]) -> dict:
    if deployment is None:
        return {"name": name, "missing": True, "desired": 0, "ready": 0, "pods": []}
    return {
        "name": name,
        "missing": False,
        "desired": deployment["spec"].get("replicas", 1),
        "ready": deployment.get("status", {}).get("readyReplicas", 0),
        "pods": [_pod_row(p) for p in pods],
    }


def game_state(parts: list[dict]) -> str:
    """One word for a game from its Deployments' rows.

    `partial` means some Deployments are scaled up and others down, such as a server
    running without its stats exporter.
    """
    if any(p["missing"] for p in parts):
        return "missing"
    desired = {p["desired"] > 0 for p in parts}
    if desired == {False}:
        return "stopping" if any(p["pods"] for p in parts) else "stopped"
    if desired == {True}:
        return (
            "running" if all(p["ready"] >= p["desired"] for p in parts) else "starting"
        )
    return "partial"


def game_rows(
    games: tuple[Game, ...], deployments_json: str, pods_json: str
) -> list[dict]:
    """One row per game, with its state and each Deployment's replicas and pods.

    A pod belongs to the Deployment its `app` label names, the label `pod_selector` asks for.
    """
    deployments = {
        d["metadata"]["name"]: d for d in json.loads(deployments_json)["items"]
    }
    pods = json.loads(pods_json)["items"]
    rows = []
    for game in games:
        parts = [
            _deployment_row(
                name,
                deployments.get(name),
                [p for p in pods if p["metadata"].get("labels", {}).get("app") == name],
            )
            for name in game.deployments
        ]
        rows.append(
            {"name": game.name, "state": game_state(parts), "deployments": parts}
        )
    return rows


def console_query(namespace: str, game: Game) -> str:
    """The LogQL selector for the server container's console."""
    return f'{{namespace="{namespace}",container="{game.server}"}}'


def parse_console(loki_json: str, limit: int) -> list[dict]:
    """The newest `limit` console lines from a Loki query_range answer, oldest first.

    Loki splits one container's lines into several streams (stdout and stderr, and one per
    detected level), so the streams are merged on their nanosecond timestamps.
    """
    entries = [
        (int(ts), line)
        for stream in json.loads(loki_json).get("data", {}).get("result", [])
        for ts, line in stream.get("values", [])
    ]
    entries.sort(key=lambda e: e[0])
    return [{"ts": ts // 10**9, "line": line} for ts, line in entries[-limit:]]
