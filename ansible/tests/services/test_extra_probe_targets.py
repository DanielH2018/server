"""M6 batch B8's probe jobs reproduce Kuma's 14 hand-written tiles and the Pi's published ports.

observability's `prometheus.yaml.j2` renders two scrape jobs for blackbox_exporter: `blackbox-extra`
(one target per hand-written non-push tile that has a probe) and `blackbox-pi-ports` (one
`tcp_connect` target per container the Pi publishes a port for). These guards render both from the
real inventory and hold each to the thing it replaces
(docs/target-architecture/monitoring/m6-retire-kuma.md, "B0, B5, B7 and B8"). Cutover-a1 deleted
the tiles, so the target addresses, modules and tiers are literals here, copied from the deleted
tiles' declarations (`git show 50f2b2c410:ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2`).

The rejecting halves are `test_a_published_port_the_job_lacks_is_flagged`,
`test_an_all_clear_pointed_at_an_app_route_is_flagged` and
`test_an_untiered_service_probe_leaves_the_critical_tier`. The named members are the Mosquitto
target, the keyword target that expects `OK`, and `wg-easy` on 51821.

Run: uv run pytest ansible/tests/services/test_extra_probe_targets.py
"""

import copy

from _helpers import K8S_ROLES
from _k8s_render import host_context, render_role_template
from entry_fields import published_ports
from lib import yaml_fast
from service_tier import in_service_tier

OBS = K8S_ROLES / "observability"
BLACKBOX = K8S_ROLES / "blackbox-exporter"
PI_PORTS_JOB = "blackbox-pi-ports"
EXTRA_JOB = "blackbox-extra"

# service -> (module, a substring of the target address) for the 12 tiles that became a probe. The
# substring is the port or path the deleted tile named. Loki's /ready has no probe (M5's
# `ScrapeTargetDown` covers job loki-homelab) and the Pi ping is `PiHostDown`.
PROBES = {
    "wg-easy": ("http_2xx", ":51821"),
    "email-to-rss": ("http_2xx", "/admin/login"),
    "ical-proxy": ("http_2xx", ":5000/calendar1.ics"),
    "livesync": ("http_401", "livesync.local."),
    "scrutiny": ("http_2xx", "/api/health"),
    "homelab-mcp": ("http_body_ok", "mcp.local."),
    "traefik": ("http_body_OK", "/.well-known/traefik-edge-selfcheck"),
    "pihole": ("dns_a_homepage", ":53"),
    "mosquitto": ("tcp_connect", ":1883"),
    "jellyfin": ("tcp_connect", ":8096"),
    "zigbee2mqtt": ("tcp_connect", ":6638"),
    "nut": ("tcp_connect", ":3493"),
}
# The services whose tile took `severity: critical` from the home-critical tier, plus the edge
# all-clear, which was always critical.
TIERED_PROBES = {"mosquitto", "pihole"}
EDGE_SELFCHECK_PATH = "/.well-known/traefik-edge-selfcheck"
KEYWORDS = {"http_body_ok": "ok", "http_body_OK": "OK"}


def rendered_jobs(
    containers_list: list[dict] | None = None,
    box_containers: list[dict] | None = None,
) -> dict[str, dict]:
    """Every scrape job of the rendered `prometheus.yml`, by name.

    `containers_list` replaces the Pi's services; `box_containers` replaces the cluster's.
    """
    overrides = {}
    if containers_list is not None:
        hostvars = copy.deepcopy(host_context()["hostvars"])
        # The job reads both service keys, so the override replaces both.
        hostvars["daniel-pi"]["containers_list"] = containers_list
        hostvars["daniel-pi"]["compose_services"] = []
        overrides["hostvars"] = hostvars
    if box_containers is not None:
        overrides["containers_list"] = box_containers
    overrides = overrides or None
    text = render_role_template("observability", "prometheus.yaml.j2", overrides)
    configmap = next(
        doc
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") == "ConfigMap"
    )
    config = yaml_fast.safe_load(configmap["data"]["prometheus.yml"])
    return {job["job_name"]: job for job in config["scrape_configs"]}


def targets(
    job: str,
    containers_list: list[dict] | None = None,
    box_containers: list[dict] | None = None,
) -> dict[str, dict]:
    """A job's targets, each mapped to its labels."""
    return {
        target: item["labels"]
        for item in rendered_jobs(containers_list, box_containers)[job][
            "static_configs"
        ]
        for target in item["targets"]
    }


def pi_containers() -> list[dict]:
    """Every Pi service entry, from `containers_list` and the package's `compose_services`."""
    pi = host_context()["hostvars"]["daniel-pi"]
    return copy.deepcopy([*pi["containers_list"], *pi["compose_services"]])


def probe_by_service() -> dict[str, tuple[str, str, str]]:
    """The `blackbox-extra` targets: service -> (module, address, page_tier)."""
    return {
        labels["service"]: (labels["module"], address, labels["page_tier"])
        for address, labels in targets(EXTRA_JOB).items()
    }


def test_each_probe_target_matches_the_tile_it_replaces():
    probes = probe_by_service()
    assert probes.keys() == PROBES.keys()
    for service, (module, fragment) in PROBES.items():
        got_module, address, _tier = probes[service]
        assert got_module == module, service
        assert fragment in address, service
    for module, keyword in KEYWORDS.items():
        assert module in {m for m, _a, _t in probes.values()}, keyword


def test_the_home_critical_tiles_are_the_critical_tier():
    probes = probe_by_service()
    box = host_context()["containers_list"]
    expected = {"traefik"} | {
        service
        for service in TIERED_PROBES
        if in_service_tier(box, service, "home-critical")
    }
    assert {s for s, (_m, _a, tier) in probes.items() if tier == "critical"} == expected
    assert "traefik" in expected


def test_an_untiered_service_probe_leaves_the_critical_tier():
    # The two tiered probes take their tier from containers_list `tier`, not from a literal.
    # Untier mosquitto and its target must drop to the warning tier.
    box = host_context()["containers_list"]
    untiered = [
        {k: v for k, v in entry.items() if k != "tier"}
        if entry.get("name") == "mosquitto"
        else entry
        for entry in box
    ]
    assert probe_by_service()["mosquitto"][2] == "critical"
    [tier] = [
        labels["page_tier"]
        for labels in targets(EXTRA_JOB, box_containers=untiered).values()
        if labels["service"] == "mosquitto"
    ]
    assert tier == "warning"


def all_clear_is_app_coupled(address: str) -> bool:
    """True when the edge all-clear probes anything but Traefik's own self-check route."""
    return EDGE_SELFCHECK_PATH not in address


def test_an_all_clear_on_the_edge_selfcheck_path_is_clean():
    assert not all_clear_is_app_coupled(
        "https://edge-selfcheck.local.example.com" + EDGE_SELFCHECK_PATH
    )


def test_an_all_clear_pointed_at_an_app_route_is_flagged():
    assert all_clear_is_app_coupled("https://mcp.local.example.com/health")


def test_the_edge_all_clear_probes_the_selfcheck_route_and_checks_the_body():
    # A status code alone reads green through a total-404 edge, so the body is checked.
    module, address, _tier = probe_by_service()["traefik"]
    assert not all_clear_is_app_coupled(address), address
    assert module == "http_body_OK"


def test_the_edge_all_clear_host_is_pinned_by_the_exporter():
    """An unrouted name resolves nowhere unless the probing pod pins it."""
    ctx = host_context("daniel-box")
    hostvars = {ctx["k8s_primary_node"]: {"containers_list": ctx["containers_list"]}}
    doc = yaml_fast.safe_load(
        render_role_template(
            "blackbox-exporter", "deployment.yaml.j2", {"hostvars": hostvars}
        )
    )
    pinned = [
        name
        for alias in doc["spec"]["template"]["spec"].get("hostAliases") or []
        for name in alias.get("hostnames", [])
        if name.startswith("edge-selfcheck.local.")
    ]
    assert len(pinned) == 1, pinned
    assert probe_by_service()["traefik"][1].startswith(f"https://{pinned[0]}/")


def test_every_module_a_target_names_is_in_blackbox_yml():
    config = yaml_fast.safe_load((BLACKBOX / "files" / "blackbox.yml").read_text())
    used = {
        labels["module"]
        for job in (EXTRA_JOB, PI_PORTS_JOB)
        for labels in targets(job).values()
    }
    assert used <= config["modules"].keys(), sorted(used - config["modules"].keys())
    for module, keyword in KEYWORDS.items():
        regexp = config["modules"][module]["http"]["fail_if_body_not_matches_regexp"]
        assert regexp == [keyword], module


def test_the_rendered_configmap_names_the_lan_domain_in_the_dns_module():
    domain = host_context()["domain"]
    rendered = render_role_template("blackbox-exporter", "configmap.yaml.j2")
    modules_text = yaml_fast.safe_load(rendered)["data"]["blackbox.yml"]
    config = yaml_fast.safe_load(modules_text)
    assert "__DOMAIN__" not in modules_text
    assert (
        config["modules"]["dns_a_homepage"]["dns"]["query_name"]
        == f"homepage.local.{domain}"
    )


def test_the_pi_job_has_one_target_per_published_port():
    containers = pi_containers()
    address = host_context()["hostvars"]["daniel-pi"]["server_ip"]
    expected = {
        f"{address}:{port}": name
        for name, port in (pair.split(":") for pair in published_ports(containers))
    }
    got = {
        target: labels["service"] for target, labels in targets(PI_PORTS_JOB).items()
    }
    assert got == expected
    assert got[f"{address}:51821"] == "wg-easy"
    assert {labels["module"] for labels in targets(PI_PORTS_JOB).values()} == {
        "tcp_connect"
    }


def test_a_published_port_the_job_lacks_is_flagged():
    containers = [*pi_containers(), {"name": "newpub", "port": 9999}]
    address = host_context()["hostvars"]["daniel-pi"]["server_ip"]
    assert f"{address}:9999" in targets(PI_PORTS_JOB, containers)
    assert f"{address}:9999" not in targets(PI_PORTS_JOB)


def pi_port_dead_rule() -> dict:
    rules = yaml_fast.safe_load(
        (OBS / "files" / "rules" / "extra-probes.yaml").read_text()
    )
    [rule] = [r for r in rules["groups"][0]["rules"] if r["alert"] == "PiPortDead"]
    return rule


def test_pi_port_dead_holds_the_retired_rows_down_streak():
    # The deleted pi_pressure row held a dead port for PI_PORTS_CONSECUTIVE (2) bridge cycles of
    # INTERVAL (300s), so a Pi deploy that recreates a container for a few seconds pages nobody.
    assert pi_port_dead_rule()["for"] == f"{2 * 300 // 60}m"


def recreate_hint_problems(rule: dict) -> list[str]:
    """Why `rule` would page a dead Pi port without telling the reader what to do about it."""
    problems = []
    if rule["expr"] != 'probe_success{job="blackbox-pi-ports"} == 0':
        problems.append(
            "does not fire on a refused connect to a blackbox-pi-ports target"
        )
    description = rule["annotations"]["description"]
    for needle in ("RECREATE", "docker ps"):
        if needle not in description:
            problems.append(f"description lacks {needle!r}")
    return problems


def test_pi_port_dead_fires_on_a_refused_connect_and_names_the_recreate_hint():
    # Failure class 6 ("restart is not recreate"): a Pi container that comes back attached to no
    # network keeps `Up (healthy)` while its port is closed, and only a recreate restores it.
    assert recreate_hint_problems(pi_port_dead_rule()) == []


def test_a_pi_port_rule_that_loses_its_hint_or_its_selector_is_flagged():
    rule = copy.deepcopy(pi_port_dead_rule())
    rule["annotations"]["description"] = "A Pi port is closed."
    rule["expr"] = 'probe_success{job="blackbox-extra"} == 0'
    assert recreate_hint_problems(rule) == [
        "does not fire on a refused connect to a blackbox-pi-ports target",
        "description lacks 'RECREATE'",
        "description lacks 'docker ps'",
    ]
