"""The Games panel's reads and writes: each game's state and console, and its start and stop.

`deploy_ui.App` owns one `GameControl` and routes `/api/games`, `/api/game-console` and
`/api/game` to it. Every command runs through the app's `out`, so the panel shares its
subprocess edge, and a test swaps `audit` and `sleep` by assigning them.
"""

import base64
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from deploy_ui_lib import deploy_ui_games as game_reads
from deploy_ui_lib import deploy_ui_writes as writes
from deploy_ui_lib.deploy_ui_reads import Unavailable
from host_lib import logfmt

# The credential the unit imports: the `homelab-games` ServiceAccount's token (ADR-0033).
GAMES_TOKEN = "games_panel_token"
# Flux strips only kubectl's and its own field managers, so a count under this name survives.
GAMES_FIELD_MANAGER = "deploy-ui-games"
GAME_ACTIONS = ("start", "stop", "restart")


class GameControl:
    """The game registry's reads and writes over the app's config and subprocess edge."""

    def __init__(self, cfg, out: Callable[..., str], sleep=time.sleep) -> None:
        self.cfg, self._out, self.sleep = cfg, out, sleep
        self.audit: Callable[[str], None] = writes.audit
        self._api_lock = threading.Lock()
        self._api: tuple[str, Path] | None = None

    def game_servers(self) -> dict:
        """Each registered game's state, from its Deployments and their pods."""
        if not self.cfg.games:
            return {"games": []}
        kubectl = ["kubectl", "-n", self.cfg.namespace, "get"]
        names = [d for g in self.cfg.games for d in g.deployments]
        deployments = self._out(
            [*kubectl, "deployments", *names, "--ignore-not-found", "-o", "json"], 15
        )
        pods = self._out(
            [
                *kubectl,
                "pods",
                "-l",
                game_reads.pod_selector(self.cfg.games),
                "-o",
                "json",
            ],
            15,
        )
        return {"games": game_reads.game_rows(self.cfg.games, deployments, pods)}

    def game_console(self, name: str) -> dict:
        """The newest console lines of one game's server container, read from Loki.

        Raises:
            Unavailable: `name` is not a registered game, no Loki URL is set, or the
                query failed.
        """
        game = game_reads.find_game(self.cfg.games, name)
        if game is None:
            raise Unavailable(f"no game named {name!r}")
        if not self.cfg.loki_url:
            raise Unavailable("DEPLOY_UI_LOKI is not set")
        start_ns = int(time.time() - self.cfg.console_window_s) * 10**9
        raw = self._out(
            [
                "curl",
                "-fsS",
                "--max-time",
                "10",
                "-G",
                f"{self.cfg.loki_url}/loki/api/v1/query_range",
                "--data-urlencode",
                f"query={game_reads.console_query(self.cfg.namespace, game)}",
                "--data-urlencode",
                f"limit={self.cfg.console_lines}",
                "--data-urlencode",
                "direction=backward",
                "--data-urlencode",
                f"start={start_ns}",
            ],
            15,
        )
        return {
            "game": name,
            "lines": game_reads.parse_console(raw, self.cfg.console_lines),
        }

    def _games_token(self) -> str:
        """The `homelab-games` token systemd imported for the unit.

        Raises:
            Unavailable: the unit imported no such credential.
        """
        missing = (
            "the games token is not loaded: the operator stores `games_panel_token` "
            "and runs `host_secrets.py sync daniel-box`"
        )
        if self.cfg.credentials_dir is None:
            raise Unavailable(missing)
        try:
            return (self.cfg.credentials_dir / GAMES_TOKEN).read_text().strip()
        except OSError as exc:
            raise Unavailable(missing) from exc

    def _apiserver(self) -> tuple[str, Path]:
        """The API server's URL and a file holding its CA, read once from the read-only kubeconfig."""
        with self._api_lock:
            if self._api is None:
                view = ["kubectl", "config", "view", "--raw", "--minify", "-o"]
                server = self._out(
                    [*view, "jsonpath={.clusters[0].cluster.server}"], 10
                ).strip()
                ca = self._out(
                    [
                        *view,
                        "jsonpath={.clusters[0].cluster.certificate-authority-data}",
                    ],
                    10,
                ).strip()
                if not server or not ca:
                    raise Unavailable("the read-only kubeconfig names no API server")
                ca_file = self.cfg.log_dir / "apiserver-ca.crt"
                ca_file.parent.mkdir(parents=True, exist_ok=True)
                ca_file.write_bytes(base64.b64decode(ca))
                self._api = (server, ca_file)
            return self._api

    def _scale(self, deployment: str, replicas: int) -> None:
        """Set one game Deployment's replica count through its `scale` subresource.

        The token travels on stdin as a header file, never in argv.

        Raises:
            Unavailable: no token, no API server, or the API refused the write.
        """
        token = self._games_token()
        server, ca = self._apiserver()
        url = (
            f"{server}/apis/apps/v1/namespaces/{self.cfg.namespace}/deployments/"
            f"{deployment}/scale?fieldManager={GAMES_FIELD_MANAGER}"
        )
        self._out(
            [
                "curl",
                "-fsS",
                "--max-time",
                "10",
                "--cacert",
                str(ca),
                "-X",
                "PATCH",
                "-H",
                "Content-Type: application/merge-patch+json",
                "-H",
                "@-",
                "--data",
                json.dumps({"spec": {"replicas": replicas}}),
                url,
            ],
            15,
            stdin=f"Authorization: Bearer {token}\n",
        )

    def _game_pods(self, game: game_reads.Game) -> list[str]:
        out = self._out(
            [
                "kubectl",
                "-n",
                self.cfg.namespace,
                "get",
                "pods",
                "-l",
                game_reads.pod_selector((game,)),
                "-o",
                "name",
            ],
            15,
        )
        return out.split()

    def finish_restart(self, game: game_reads.Game) -> None:
        """Start `game` again once its pods are gone, or leave it stopped after the wait.

        Runs on its own thread after `game_action` scaled the game to 0. Every outcome is one
        audit line, because no request is left to answer.
        """
        deadline = time.monotonic() + self.cfg.restart_wait_s
        try:
            while self._game_pods(game):
                if time.monotonic() >= deadline:
                    self.audit(
                        f"action=game-restart game={game.name} result=left-stopped "
                        f"reason=pods-still-running-after-{self.cfg.restart_wait_s}s"
                    )
                    return
                self.sleep(5)
            for deployment in game.deployments:
                self._scale(deployment, 1)
        except Unavailable as exc:
            self.audit(
                f"action=game-restart game={game.name} result=left-stopped "
                + logfmt({"reason": str(exc)[:200]})
            )
            return
        self.audit(f"action=game-restart game={game.name} result=started")

    def game_action(self, body: dict) -> tuple[int, str]:
        """Start, stop or restart one game: every Deployment it lists, together.

        A restart scales the game to 0 here and starts it from `finish_restart`'s thread once
        its pods are gone, because these Deployments are `Recreate` over RWO claims.
        """
        name, action = str(body.get("game", "")), str(body.get("action", ""))
        game = game_reads.find_game(self.cfg.games, name)
        if game is None:
            return 404, f"no game named {name!r}"
        if action not in GAME_ACTIONS:
            return 400, f"action must be one of {', '.join(GAME_ACTIONS)}"
        if action != "stop" and game.start_refused:
            return 409, f"{name} cannot be started: {game.start_refused}"
        replicas = 1 if action == "start" else 0
        for deployment in game.deployments:
            self._scale(deployment, replicas)
        self.audit(f"action=game-{action} game={name}")
        if action == "restart":
            threading.Thread(
                target=self.finish_restart,
                args=(game,),
                name=f"game-restart-{name}",
                daemon=True,
            ).start()
            return 200, f"stopping {name}; it starts again once its pods are gone"
        return 200, f"{'starting' if replicas else 'stopping'} {name}"
