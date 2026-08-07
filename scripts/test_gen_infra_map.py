#!/usr/bin/env python3
"""Tests for gen_infra_map — the declared-vs-live infrastructure map.

The failure this suite is really guarding against is a *quietly wrong* page. The
map is regenerated on a timer and read at a glance, so a mis-classification does
not look like a bug — it looks like news. Two directions matter:

* A false alarm (a one-shot job like configarr, or a role companion like
  node-exporter, rendered as "missing"/"undeclared") trains the reader to ignore
  red, which is worse than not drawing it.
* A missed alarm (a stopped container rendered as healthy, or an unreachable
  host silently showing stale declared state as if it were live).

Every test below builds its own inventory and live-state dicts, so nothing here
needs an age key, a cluster, or ssh.

Run: uv run pytest scripts/test_gen_infra_map.py
"""

import json

import pytest

import gen_infra_map as g

GLOBALS = {
    "k8s_hostname_suffix": "-k8s",
    "k8s_namespace": "homelab",
    "k8s_observability_namespace": "observability",
}

ROLES = g.RoleIndex(
    container_owners={
        "node-exporter": "prometheus",
        "cadvisor": "prometheus",
        "prometheus": "prometheus",
        "unbound": "pihole",
    },
    batch_roles=frozenset({"configarr", "n8n-images"}),
)


def docker_host(containers_list):
    return {"server_ip": "10.0.0.161", "containers_list": containers_list}


def live_ok(data):
    return {"ok": True, "data": data, "error": ""}


def container(state="running", status="Up 2 days (healthy)", image="img:1"):
    return {
        "state": state,
        "status": status,
        "image": image,
        "healthy": "(healthy)" in status,
        "unhealthy": "(unhealthy)" in status,
    }


# --- resolve_vars -----------------------------------------------------------


def test_resolve_vars_substitutes_known_names():
    assert g.resolve_vars("auth{{ k8s_hostname_suffix }}", GLOBALS) == "auth-k8s"


def test_resolve_vars_leaves_unknown_names_visible():
    """Blanking an unresolved name would hide the gap; leaving it shows it."""
    assert g.resolve_vars("{{ mystery }}", GLOBALS) == "{{ mystery }}"


def test_resolve_vars_passes_through_non_strings():
    assert g.resolve_vars(8080, GLOBALS) == 8080
    assert g.resolve_vars(None, GLOBALS) is None


def test_resolve_vars_resolves_nested_references():
    variables = {"outer": "{{ inner }}", "inner": "done"}
    assert g.resolve_vars("{{ outer }}", variables) == "done"


def test_resolve_vars_survives_a_reference_cycle():
    variables = {"a": "{{ b }}", "b": "{{ a }}"}
    assert "{{" in g.resolve_vars("{{ a }}", variables)


# --- declared_services ------------------------------------------------------


def test_declared_services_defaults_to_docker_platform():
    hv = docker_host([{"name": "sonarr", "port": 8989, "networks": ["media"]}])
    (service,) = g.declared_services("daniel-server", hv, GLOBALS)
    assert service["platform"] == "docker"
    assert service["networks"] == ["media"]
    assert service["namespace"] is None


def test_declared_services_resolves_k8s_hostname_and_namespace():
    hv = docker_host(
        [
            {
                "name": "authelia",
                "platform": "k8s",
                "hostname": "auth{{ k8s_hostname_suffix }}",
                "port": 9091,
            }
        ]
    )
    (service,) = g.declared_services("daniel-box", hv, GLOBALS)
    assert service["hostname"] == "auth-k8s"
    assert service["namespace"] == "homelab"


def test_declared_services_gives_namespace_owner_its_own_namespace():
    """claude-otel is one entry that owns the whole observability namespace."""
    hv = docker_host([{"name": "claude-otel", "platform": "k8s", "port": 3000}])
    (service,) = g.declared_services("daniel-box", hv, GLOBALS)
    assert service["namespace"] == "observability"


def test_declared_services_omits_hostname_for_unrouted_services():
    """No port means no Traefik route — showing a hostname would invent one."""
    hv = docker_host([{"name": "autoheal"}])
    (service,) = g.declared_services("daniel-server", hv, GLOBALS)
    assert service["hostname"] is None


def test_declared_services_tolerates_a_missing_containers_list():
    assert g.declared_services("daniel-pi", {}, GLOBALS) == []


def test_declared_services_skips_entries_without_a_name():
    hv = docker_host([{"port": 80}, {"name": "real"}])
    assert [s["name"] for s in g.declared_services("h", hv, GLOBALS)] == ["real"]


# --- parse_docker_ps --------------------------------------------------------


def test_parse_docker_ps_reads_health_from_the_status_string():
    out = "a\trunning\tUp 2 days (healthy)\timg:1\nb\trunning\tUp 1 day (unhealthy)\timg:2\n"
    parsed = g.parse_docker_ps(out)
    assert parsed["a"]["healthy"] is True
    assert parsed["b"]["unhealthy"] is True


def test_parse_docker_ps_ignores_malformed_and_blank_lines():
    assert g.parse_docker_ps("garbage\n\n\tx\t\t\n") == {}


# --- parse_kubectl_deployments ---------------------------------------------


def deployment(name, ns="homelab", ready=1, desired=1, image="img:1"):
    status = {"readyReplicas": ready} if ready else {}
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "replicas": desired,
            "template": {"spec": {"containers": [{"image": image}]}},
        },
        "status": status,
    }


def test_parse_kubectl_deployments_extracts_replica_counts():
    payload = json.dumps({"items": [deployment("traefik", ready=1, desired=2)]})
    parsed = g.parse_kubectl_deployments(payload)
    assert parsed[("homelab", "traefik")]["ready"] == 1
    assert parsed[("homelab", "traefik")]["desired"] == 2


def test_parse_kubectl_deployments_treats_absent_ready_replicas_as_zero():
    """kubectl omits readyReplicas entirely at zero — None would break the sum."""
    payload = json.dumps({"items": [deployment("down", ready=0)]})
    assert g.parse_kubectl_deployments(payload)[("homelab", "down")]["ready"] == 0


def test_parse_kubectl_deployments_returns_empty_on_bad_json():
    assert g.parse_kubectl_deployments("not json") == {}


# --- match_k8s_workloads ----------------------------------------------------


WORKLOADS = {
    ("homelab", "n8n"): {"ready": 1, "desired": 1, "image": "n8n:1"},
    ("homelab", "n8n-runners"): {"ready": 1, "desired": 1, "image": "n8n:1"},
    ("homelab", "n8nother"): {"ready": 1, "desired": 1, "image": "x:1"},
    ("observability", "loki"): {"ready": 1, "desired": 1, "image": "loki:1"},
    ("observability", "tempo"): {"ready": 1, "desired": 1, "image": "tempo:1"},
}


def test_match_k8s_workloads_takes_the_service_and_its_helpers():
    service = {"name": "n8n", "namespace": "homelab"}
    assert [w["name"] for w in g.match_k8s_workloads(service, WORKLOADS)] == [
        "n8n",
        "n8n-runners",
    ]


def test_match_k8s_workloads_requires_a_hyphen_not_a_bare_prefix():
    """`n8nother` is a different service; prefix matching must not swallow it."""
    service = {"name": "n8n", "namespace": "homelab"}
    assert "n8nother" not in [
        w["name"] for w in g.match_k8s_workloads(service, WORKLOADS)
    ]


def test_match_k8s_workloads_gives_a_namespace_owner_everything_in_it():
    service = {"name": "claude-otel", "namespace": "observability"}
    assert [w["name"] for w in g.match_k8s_workloads(service, WORKLOADS)] == [
        "loki",
        "tempo",
    ]


def test_match_k8s_workloads_does_not_cross_namespaces():
    service = {"name": "loki", "namespace": "homelab"}
    assert g.match_k8s_workloads(service, WORKLOADS) == []


# --- reconcile_docker -------------------------------------------------------


def service(name="sonarr", **overrides):
    base = {
        "name": name,
        "platform": "docker",
        "hostname": None,
        "port": None,
        "authelia": False,
        "networks": [],
        "namespace": None,
        "declared": True,
        "status": "unknown",
        "detail": "",
        "image": "",
        "replicas": None,
    }
    return {**base, **overrides}


def test_reconcile_docker_marks_a_running_healthy_container_healthy():
    result = g.reconcile_docker(service(), {"sonarr": container()}, ROLES)
    assert result["status"] == "healthy"


def test_reconcile_docker_marks_an_unhealthy_container_degraded():
    live = {"sonarr": container(status="Up 3 hours (unhealthy)")}
    assert g.reconcile_docker(service(), live, ROLES)["status"] == "degraded"


def test_reconcile_docker_marks_a_stopped_container_down():
    """The missed-alarm case: a container present but not running is not healthy."""
    live = {"sonarr": container(state="exited", status="Exited (1) 2 hours ago")}
    assert g.reconcile_docker(service(), live, ROLES)["status"] == "down"


def test_reconcile_docker_marks_an_absent_container_missing():
    assert g.reconcile_docker(service(), {}, ROLES)["status"] == "missing"


def test_reconcile_docker_calls_a_batch_role_a_job_not_missing():
    """configarr runs via `compose run --rm`; absence is correct, not a fault."""
    result = g.reconcile_docker(service("configarr"), {}, ROLES)
    assert result["status"] == "job"


# --- reconcile_k8s ----------------------------------------------------------


def test_reconcile_k8s_is_healthy_when_all_replicas_are_ready():
    svc = service("n8n", platform="k8s", namespace="homelab")
    assert g.reconcile_k8s(svc, WORKLOADS, ROLES)["status"] == "healthy"


def test_reconcile_k8s_is_degraded_on_partial_readiness():
    workloads = {("homelab", "n8n"): {"ready": 1, "desired": 3, "image": "n8n:1"}}
    svc = service("n8n", platform="k8s", namespace="homelab")
    result = g.reconcile_k8s(svc, workloads, ROLES)
    assert result["status"] == "degraded"
    assert result["replicas"] == (1, 3)


def test_reconcile_k8s_is_down_when_nothing_is_ready():
    workloads = {("homelab", "n8n"): {"ready": 0, "desired": 2, "image": "n8n:1"}}
    svc = service("n8n", platform="k8s", namespace="homelab")
    assert g.reconcile_k8s(svc, workloads, ROLES)["status"] == "down"


def test_reconcile_k8s_calls_a_build_role_a_job():
    svc = service("n8n-images", platform="k8s", namespace="homelab")
    assert g.reconcile_k8s(svc, {}, ROLES)["status"] == "job"


def test_reconcile_k8s_marks_a_genuinely_absent_deployment_missing():
    svc = service("freshrss", platform="k8s", namespace="homelab")
    assert g.reconcile_k8s(svc, {}, ROLES)["status"] == "missing"


# --- find_extra_containers --------------------------------------------------


def test_find_extra_containers_attributes_a_companion_to_its_role():
    """node-exporter has no inventory entry; the prometheus role creates it."""
    live = {"prometheus": container(), "node-exporter": container()}
    extras = g.find_extra_containers(live, {"prometheus"}, ROLES)
    assert [e["name"] for e in extras] == ["node-exporter"]
    assert extras[0]["status"] == "companion"
    assert "prometheus" in extras[0]["detail"]


def test_find_extra_containers_flags_a_container_the_repo_does_not_know():
    extras = g.find_extra_containers({"mystery": container()}, {"prometheus"}, ROLES)
    assert extras[0]["status"] == "undeclared"


def test_find_extra_containers_does_not_excuse_a_companion_of_an_undeployed_role():
    """unbound's owner (pihole) is not on this host, so it is still drift."""
    extras = g.find_extra_containers({"unbound": container()}, {"sonarr"}, ROLES)
    assert extras[0]["status"] == "undeclared"


def test_find_extra_containers_marks_a_stopped_extra_down():
    live = {"mystery": container(state="exited", status="Exited (0)")}
    assert g.find_extra_containers(live, set(), ROLES)[0]["status"] == "down"


# --- classify_migration -----------------------------------------------------


def test_classify_migration_splits_cutover_dual_and_docker_only():
    box = [
        service("freshrss", platform="k8s"),
        service("traefik", platform="k8s"),
    ]
    server = [service("traefik"), service("sonarr")]
    result = g.classify_migration(box, server)
    assert result["cutover"] == ["freshrss"]
    assert result["dual"] == ["traefik"]
    assert result["docker_only"] == ["sonarr"]


def test_classify_migration_ignores_undeclared_docker_containers():
    """A companion container is not an un-migrated service."""
    server = [service("node-exporter", declared=False, status="companion")]
    assert g.classify_migration([], server)["docker_only"] == []


# --- build_model ------------------------------------------------------------


def model_for(live):
    host_vars = {
        "daniel-box": docker_host(
            [{"name": "traefik", "platform": "k8s", "port": 8080}]
        ),
        "daniel-server": docker_host([{"name": "sonarr", "port": 8989}]),
    }
    return g.build_model(GLOBALS, host_vars, live, "2026-08-07 03:00 CDT", ROLES)


def test_build_model_overlays_live_state_onto_declared_services():
    model = model_for(
        {
            "daniel-box": live_ok(
                {("homelab", "traefik"): {"ready": 1, "desired": 1, "image": "t:1"}}
            ),
            "daniel-server": live_ok({"sonarr": container()}),
        }
    )
    assert model["totals"]["healthy"] == 2
    assert model["totals"]["down"] == 0


def test_build_model_degrades_an_unreachable_host_to_declared_only():
    """The missed-alarm case: stale data must never be painted as live."""
    model = model_for(
        {
            "daniel-box": {"ok": False, "data": {}, "error": "connection refused"},
            "daniel-server": live_ok({"sonarr": container()}),
        }
    )
    box = next(h for h in model["hosts"] if h["name"] == "daniel-box")
    assert box["reachable"] is False
    assert box["error"] == "connection refused"
    assert all(s["status"] == "unknown" for s in box["services"])


def test_build_model_reports_both_hosts_even_with_no_live_data():
    model = model_for({})
    assert [h["name"] for h in model["hosts"]] == list(g.HOSTS)


def test_build_model_counts_routed_and_sso_gated_services():
    host_vars = {
        "daniel-box": docker_host([]),
        "daniel-server": docker_host(
            [
                {"name": "sonarr", "port": 8989, "use_authelia": True},
                {"name": "autoheal"},
            ]
        ),
    }
    model = g.build_model(GLOBALS, host_vars, {}, "now", ROLES)
    server = next(h for h in model["hosts"] if h["name"] == "daniel-server")
    assert server["routed_count"] == 1
    assert server["authelia_count"] == 1


# --- render_html ------------------------------------------------------------


def rendered():
    return g.render_html(
        model_for(
            {
                "daniel-box": live_ok(
                    {("homelab", "traefik"): {"ready": 1, "desired": 1, "image": "t:1"}}
                ),
                "daniel-server": live_ok({"sonarr": container()}),
            }
        )
    )


def test_render_html_is_self_contained():
    """It is opened over file://, so any external asset is a broken page."""
    page = rendered()
    for marker in ("http://", "https://", "<script", "src="):
        assert marker not in page, f"page must not reference {marker}"


def test_render_html_includes_both_hosts_and_their_services():
    page = rendered()
    for expected in ("daniel-box", "daniel-server", "sonarr", "traefik"):
        assert expected in page


def test_render_html_escapes_values_from_the_inventory():
    host_vars = {
        "daniel-box": docker_host([]),
        "daniel-server": docker_host([{"name": "<script>evil</script>", "port": 1}]),
    }
    page = g.render_html(g.build_model(GLOBALS, host_vars, {}, "now", ROLES))
    assert "<script>evil" not in page
    assert "&lt;script&gt;evil" in page


def test_render_html_names_an_unreachable_host_in_the_page():
    model = model_for(
        {
            "daniel-box": {"ok": False, "data": {}, "error": "ssh timed out"},
            "daniel-server": live_ok({"sonarr": container()}),
        }
    )
    assert "ssh timed out" in g.render_html(model)


# --- load_roles (reads the real repo) ---------------------------------------


def test_load_roles_derives_ownership_from_the_role_trees():
    """The whole companion/job distinction rests on this being repo-derived."""
    roles = g.load_roles()
    if not roles.container_owners:
        pytest.skip("role trees not present")
    assert roles.container_owners.get("node-exporter") == "prometheus"
    assert "configarr" in roles.batch_roles
