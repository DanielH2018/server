"""terraria-stats and valheim-stats run exactly as many pods as their game server does.

Each exporter tails its game server's console out of loki-homelab, so at
`terraria_k8s_replicas: 0` or `valheim_k8s_replicas: 0` it polls for a server that cannot log.
A count that is a game role default does not cross a role boundary — a
`terraria_k8s_replicas | default(1)` written in game-stats would render 1 forever and say
nothing. Each value lives in `group_vars/all.yml`, and three renders read it.

The scrape job is the third, and it is tested here rather than filed as a follow-up: a
Prometheus job whose Service has no endpoint reads `up == 0`, which is exactly the signal
observability's scrape-target check pages on. Scaling the exporter without gating the job trades
a pointless pod for a permanent page.

The file keeps its terraria name: the pairing began there, and valheim was added to it when it
was parked the same way (2026-11-13).
"""

import pytest

from _k8s_render import render_role_template
from lib import yaml_fast

# game -> (server role, exporter template, scrape job); the replicas variable is `<game>_k8s_replicas`
GAMES = {
    "terraria": ("terraria", "deployment-terraria.yaml.j2", "terraria-stats"),
    "valheim": ("valheim", "deployment-valheim.yaml.j2", "valheim-stats"),
}
BOTH_RUNNING = {"terraria_k8s_replicas": 1, "valheim_k8s_replicas": 1}


def _render(role: str, template: str, overrides: dict) -> str:
    # daniel-box's whole context, containers_list included: the scrape job renders from the
    # game-stats entry's `metrics` item, and the exporter reads its port from the same item.
    return render_role_template(role, template, overrides)


def _replicas(role: str, template: str, replicas: int, game: str) -> int:
    overrides = {**BOTH_RUNNING, f"{game}_k8s_replicas": replicas}
    doc = next(
        d for d in yaml_fast.safe_load_all(_render(role, template, overrides)) if d
    )
    assert doc["kind"] == "Deployment", doc["kind"]
    return doc["spec"]["replicas"]


def _scrape_job_names(**overrides: int) -> list[str]:
    doc = next(
        d
        for d in yaml_fast.safe_load_all(
            _render(
                "observability", "prometheus.yaml.j2", {**BOTH_RUNNING, **overrides}
            )
        )
        if d and d.get("kind") == "ConfigMap"
    )
    jobs = yaml_fast.safe_load(doc["data"]["prometheus.yml"])["scrape_configs"]
    return [job["job_name"] for job in jobs]


@pytest.mark.parametrize("game", GAMES)
def test_the_exporter_matches_the_server_at_every_count(game: str) -> None:
    """Both halves of the pairing, against the two counts the server actually takes.

    Asserting equality alone would pass on a template that hardcodes the same literal in both
    files, so the counts are read separately and compared to the input.
    """
    server_role, exporter_template, _ = GAMES[game]
    for count in (0, 1):
        server = _replicas(server_role, "deployment.yaml.j2", count, game)
        exporter = _replicas("game-stats", exporter_template, count, game)
        assert (server, exporter) == (count, count), (
            f"at {game}_k8s_replicas: {count} the server renders {server} and the "
            f"exporter renders {exporter}"
        )


@pytest.mark.parametrize("game", GAMES)
def test_the_scrape_job_follows_the_server(game: str) -> None:
    """The reject half: no pod behind the Service means no job, so nothing reads `up == 0`.

    The accept half: restoring the server restores its series, not just its pods. The other
    game stays running throughout, so a gate that dropped both jobs would fail here.
    """
    job = GAMES[game][2]
    other = next(g[2] for name, g in GAMES.items() if name != game)
    parked = _scrape_job_names(**{f"{game}_k8s_replicas": 0})
    assert job not in parked
    assert other in parked
    assert job in _scrape_job_names(**{f"{game}_k8s_replicas": 1})
