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

Every test builds its inventory and live-state dicts from the record builders in
`_infra_map.py`, so nothing here needs an age key, a cluster, or ssh. This file covers
the declared model and its reconciliation with live state; the parsers and collectors
are in `test_infra_map_live.py`, and the HTML page, diagram and SVG in
`test_infra_map_render.py`.

Run: uv run pytest scripts/infra_map/tests/test_gen_infra_map.py
"""

import pytest

import gen_infra_map as g

from _infra_map import (
    GLOBALS,
    PODS,
    ROLES,
    WORKLOADS,
    container,
    docker_host,
    live_ok,
    model_for,
    service,
)


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


def test_reconcile_k8s_looks_in_a_namespace_the_manifests_name_literally():
    """dri-device-plugin's DaemonSet is in kube-system on purpose; the inventory
    has no field for that, so it read as Missing behind 2/2 ready pods."""
    workloads = {
        ("kube-system", "dri-device-plugin"): {"ready": 2, "desired": 2, "image": "p:1"}
    }
    svc = service("dri-device-plugin", platform="k8s", namespace="homelab")
    assert g.reconcile_k8s(svc, workloads, ROLES)["status"] == "missing"
    roles = g.RoleIndex(
        container_owners={},
        batch_roles=frozenset(),
        manifest_namespaces={"dri-device-plugin": frozenset({"kube-system"})},
    )
    result = g.reconcile_k8s(svc, workloads, roles)
    assert result["status"] == "healthy"
    assert result["workloads"][0]["namespace"] == "kube-system"


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


#
# The inventory declares every k8s service under daniel-box, so a host panel
# built from `containers_list` rendered daniel-server as empty while it ran 47
# pods. These pin the panel to placement instead.


def test_services_on_host_lists_what_landed_there_not_what_declares_it():
    k8s = [
        {**service("sonarr", platform="k8s"), "nodes": ["daniel-server"]},
        {**service("traefik", platform="k8s"), "nodes": ["daniel-box"]},
    ]
    assert [s["name"] for s in g.services_on_host("daniel-server", [], k8s)] == [
        "sonarr"
    ]


def test_services_on_host_shows_a_spread_service_on_both():
    k8s = [
        {**service("loki", platform="k8s"), "nodes": ["daniel-box", "daniel-server"]}
    ]
    assert g.services_on_host("daniel-box", [], k8s)
    assert g.services_on_host("daniel-server", [], k8s)


def test_services_on_host_keeps_an_unplaced_service_with_its_declaring_host():
    """A one-shot job has no pods; it must not fall off the page entirely."""
    declared = [{**service("configarr", platform="k8s", status="job"), "nodes": []}]
    assert [s["name"] for s in g.services_on_host("daniel-box", declared, [])] == [
        "configarr"
    ]


def test_build_model_overlays_live_state_onto_declared_services():
    model = model_for(
        {
            "daniel-box": live_ok(
                {("homelab", "traefik"): {"ready": 1, "desired": 1, "image": "t:1"}}
            ),
            "daniel-pi": live_ok({"sonarr": container()}),
        }
    )
    assert model["totals"]["healthy"] == 2
    assert model["totals"]["down"] == 0


def test_build_model_degrades_an_unreachable_host_to_declared_only():
    """The missed-alarm case: stale data must never be painted as live."""
    model = model_for(
        {
            "daniel-box": {"ok": False, "data": {}, "error": "connection refused"},
            "daniel-pi": live_ok({"sonarr": container()}),
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


#
# These guard a bug that already happened once: cron runs with PATH=/usr/bin:/bin,
# which omits /usr/local/bin where kubectl lives. The run still exited 0 and wrote
# a page — it just quietly reported declared-only for every k8s service while the
# Docker half kept working. A healthy-looking, half-blind page that alerts nobody.


def test_main_leaves_the_previous_page_untouched_when_a_tool_is_missing(
    monkeypatch, tmp_path
):
    """Overwriting a real map with a declared-only render would hide the fault."""
    page = tmp_path / "map.html"
    page.write_text("PREVIOUS RENDER")
    monkeypatch.setattr(
        g,
        "collect_live",
        lambda *_: (_ for _ in ()).throw(g.MissingToolError("kubectl")),
    )
    assert g.main(["-o", str(page)]) == 2
    assert page.read_text() == "PREVIOUS RENDER"


def test_load_roles_derives_ownership_from_the_role_trees():
    """The whole companion/job distinction rests on this being repo-derived."""
    roles = g.load_roles()
    if not roles.container_owners:
        pytest.skip("role trees not present")
    # node-exporter was the fixture until its role archived (2026-08-14, Phase F drain —
    # it is a k8s DaemonSet now); autoheal is the same un-inventoried-companion shape.
    assert roles.container_owners.get("autoheal") == "autoheal"
    # Both are CronJob-only k8s roles. configarr used to qualify via its Docker
    # compose declaring no container_name; that compose was deleted with the rest of
    # the migrated roles' plumbing, so the classification is derived from the k8s
    # manifests now — and pi-peer-backup, which the compose rule never saw, qualifies too.
    assert "configarr" in roles.batch_roles
    assert "pi-peer-backup" in roles.batch_roles
    # Roles whose templates hold no long-running workload at all: Dockerfiles, a
    # route onto a chart-owned Deployment, a PVC, NetworkPolicies. All four sat
    # in the map as "Missing · no deployment found" until 2026-09-01.
    for role in ("n8n-images", "longhorn-ui", "media-volume", "netpol-baseline"):
        assert role in roles.batch_roles, role
    # And a DaemonSet-only role is a real workload the collector must find.
    assert "node-exporter" not in roles.batch_roles
    assert "dri-device-plugin" not in roles.batch_roles
    assert roles.manifest_namespaces["dri-device-plugin"] == {"kube-system"}
    # A role whose namespace is the resolved variable records nothing here.
    assert "freshrss" not in roles.manifest_namespaces


def _k8s_role(repo_root, name, *manifests):
    templates = repo_root / "ansible" / "roles" / "k8s" / name / "templates"
    templates.mkdir(parents=True)
    for index, kind in enumerate(manifests):
        (templates / f"{index}.yaml.j2").write_text(f"kind: {kind}\n")


def test_load_roles_excuses_a_role_with_no_long_running_workload(tmp_path):
    (tmp_path / "ansible" / "roles" / "containers").mkdir(parents=True)
    _k8s_role(tmp_path, "policies", "NetworkPolicy", "Job")
    _k8s_role(tmp_path, "route-only", "IngressRoute", "Middleware")
    roles = g.load_roles(tmp_path)
    assert roles.batch_roles == frozenset({"policies", "route-only"})


def test_load_roles_does_not_excuse_a_daemonset_or_statefulset_role(tmp_path):
    (tmp_path / "ansible" / "roles" / "containers").mkdir(parents=True)
    _k8s_role(tmp_path, "agent", "DaemonSet", "NetworkPolicy")
    _k8s_role(tmp_path, "db", "StatefulSet")
    _k8s_role(tmp_path, "web", "Deployment", "CronJob")
    assert g.load_roles(tmp_path).batch_roles == frozenset()


def test_load_roles_records_only_literal_manifest_namespaces(tmp_path):
    (tmp_path / "ansible" / "roles" / "containers").mkdir(parents=True)
    templates = tmp_path / "ansible" / "roles" / "k8s" / "plugin" / "templates"
    templates.mkdir(parents=True)
    (templates / "a.yaml.j2").write_text(
        "kind: DaemonSet\nmetadata:\n  namespace: kube-system\n"
    )
    (templates / "b.yaml.j2").write_text(
        "kind: Secret\nmetadata:\n  namespace: {{ k8s_namespace }}\n"
    )
    roles = g.load_roles(tmp_path)
    assert roles.manifest_namespaces == {"plugin": frozenset({"kube-system"})}


def test_place_on_nodes_collects_every_node_a_services_pods_landed_on():
    service = {"workloads": [{"name": "sonarr", "namespace": "homelab"}]}
    assert g.place_on_nodes(service, PODS)["nodes"] == ["daniel-box", "daniel-server"]


def test_place_on_nodes_does_not_cross_namespaces_or_names():
    service = {"workloads": [{"name": "other", "namespace": "homelab"}]}
    assert g.place_on_nodes(service, PODS)["nodes"] == ["daniel-box"]


def test_place_on_nodes_is_empty_for_a_service_with_no_workloads():
    assert g.place_on_nodes({"workloads": []}, PODS)["nodes"] == []


#
# The bug these guard: daniel-server's Docker was uninstalled on 2026-08-14 and
# its containers_list emptied, so inferring the plane from that list fell through
# to "docker". The map then ssh-ed a healthy k3s agent for a binary that is gone
# and rendered it as an unreachable Docker host, every 15 minutes.


def test_daniel_server_is_modelled_as_a_k3s_host_not_a_docker_one():
    server = next(h for h in model_for({})["hosts"] if h["name"] == "daniel-server")
    assert server["platform"] == "k8s"


def test_the_pi_is_covered_by_the_map():
    model = model_for({"daniel-pi": live_ok({"sonarr": container()})})
    pi = next(h for h in model["hosts"] if h["name"] == "daniel-pi")
    assert pi["platform"] == "docker"
    assert [s["name"] for s in pi["services"]] == ["sonarr"]


def test_build_model_carries_node_state_onto_its_host():
    cluster = {
        "ok": True,
        "error": "",
        "nodes": {
            "daniel-server": {
                "ready": True,
                "roles": [],
                "ip": "10.0.0.161",
                "version": "v1",
                "schedulable": True,
            }
        },
        "pods": [
            {
                "namespace": "homelab",
                "name": "x-1",
                "node": "daniel-server",
                "phase": "Running",
            }
        ],
        "volumes": 42,
        "backup_targets": [],
    }
    model = model_for({}, cluster)
    server = next(h for h in model["hosts"] if h["name"] == "daniel-server")
    assert server["node"]["pods"] == 1
    assert model["cluster"]["volumes"] == 42


def test_group_services_buckets_by_function_and_by_host():
    groups = {
        gr["name"]: [s["name"] for s in gr["services"]]
        for gr in g.group_services(model_for({}))
    }
    assert "traefik" in groups["Edge & identity"]
    assert "sonarr" in groups["Pi · LAN-only"]


def test_group_services_covers_every_service_exactly_once():
    """An unlisted service must surface as ungrouped, never drop off the page."""
    model = model_for({})
    grouped = [s["name"] for gr in g.group_services(model) for s in gr["services"]]
    declared = [s["name"] for h in model["hosts"] for s in h["services"]]
    assert sorted(grouped) == sorted(declared)


# ── standalone SVG (docs/assets/generated/infra-map.svg) ───────────────────────────────
