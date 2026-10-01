"""terraria-stats runs exactly as many pods as the terraria server does.

The exporter tails the game server's console out of loki-homelab, so at
`terraria_k8s_replicas: 0` it polls for a server that cannot log. A count that is a terraria
role default does not cross a role boundary — a `terraria_k8s_replicas | default(1)` written
in game-stats would render 1 forever and say nothing. The value lives in `group_vars/all.yml`,
and three renders read it.

The scrape job is the third, and it is tested here rather than filed as a follow-up: a
Prometheus job whose Service has no endpoint reads `up == 0`, which is exactly the signal
observability's scrape-target check pages on. Scaling the exporter without gating the job trades
a pointless pod for a permanent page.
"""

from _helpers import ANSIBLE
from lib import yaml_fast


from validate.k8s_manifests import (
    ALL_VARS,
    BASE_CONTEXT,
    SHARED_TPL,
    k8s_entries,
    load_yaml,
    make_env,
    make_lookup,
    register_ansible_filters,
    resolve_vars,
    role_defaults,
)

K8S = ANSIBLE / "roles" / "k8s"


def _render(role: str, template: str, replicas: int) -> str:
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
    base = resolve_vars(base, base)
    ctx = {
        **base,
        **role_defaults(role, base),
        "container_item": k8s_entries()[role],
        "terraria_k8s_replicas": replicas,
    }
    env = make_env([K8S / role / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    return env.get_template(template).render(**ctx)


def _replicas(role: str, template: str, replicas: int) -> int:
    doc = next(
        d for d in yaml_fast.safe_load_all(_render(role, template, replicas)) if d
    )
    assert doc["kind"] == "Deployment", doc["kind"]
    return doc["spec"]["replicas"]


def _scrape_job_names(replicas: int) -> list[str]:
    doc = next(
        d
        for d in yaml_fast.safe_load_all(
            _render("observability", "prometheus.yaml.j2", replicas)
        )
        if d and d.get("kind") == "ConfigMap"
    )
    jobs = yaml_fast.safe_load(doc["data"]["prometheus.yml"])["scrape_configs"]
    return [job["job_name"] for job in jobs]


def test_the_exporter_matches_the_server_at_every_count() -> None:
    """Both halves of the pairing, against the two counts the server actually takes.

    Asserting equality alone would pass on a template that hardcodes the same literal in both
    files, so the counts are read separately and compared to the input.
    """
    for count in (0, 1):
        server = _replicas("terraria", "deployment.yaml.j2", count)
        exporter = _replicas("game-stats", "deployment-terraria.yaml.j2", count)
        assert (server, exporter) == (count, count), (
            f"at terraria_k8s_replicas: {count} the server renders {server} and the "
            f"exporter renders {exporter}"
        )


def test_the_scrape_job_is_absent_while_the_exporter_is_parked() -> None:
    """The reject half: no pod behind the Service means no job, so nothing reads `up == 0`."""
    assert "terraria-stats" not in _scrape_job_names(0)
    # The sibling exporter has no replicas knob and must not be gated by this change.
    assert "valheim-stats" in _scrape_job_names(0)


def test_the_scrape_job_returns_with_the_server() -> None:
    """The accept half. Restoring terraria must restore its series, not just its pods."""
    assert "terraria-stats" in _scrape_job_names(1)
