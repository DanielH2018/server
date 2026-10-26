"""M6 batch B8's probe jobs reproduce Kuma's hand-written tiles and the Pi's published ports.

observability's `prometheus.yaml.j2` renders two scrape jobs for blackbox_exporter: `blackbox-extra`
(one target per hand-written non-push tile that has a probe) and `blackbox-pi-ports` (one
`tcp_connect` target per container the Pi publishes a port for). These guards render both from the
real inventory and hold each to the thing it replaces
(docs/target-architecture/monitoring/m6-retire-kuma.md, "B0, B5, B7 and B8").

The rejecting halves are `test_a_published_port_the_job_lacks_is_flagged` and
`test_a_retired_tile_missing_from_the_pager_guard_is_flagged`. The named members are the
Mosquitto tile, the keyword tile that expects `OK`, and `wg-easy` on 51821.

Run: uv run pytest ansible/tests/services/test_extra_probe_targets.py
"""

import copy
import re

from _helpers import K8S_ROLES
from _k8s_render import host_context, render_pod_env, render_role_template
from _kuma_entities import _entities
from entry_fields import published_ports
from lib import yaml_fast

OBS = K8S_ROLES / "observability"
BLACKBOX = K8S_ROLES / "blackbox-exporter"
PI_PORTS_JOB = "blackbox-pi-ports"
EXTRA_JOB = "blackbox-extra"

# Kuma tile name -> the (service, module) its replacement probe carries. Loki's /ready has no
# probe (M5's LokiHomelabDown covers it) and the Pi ping is `PiHostDown`, so both sit in
# RETIRED_WITHOUT_A_TARGET.
TILE_PROBES = {
    "Daniel Pi WG Easy": ("wg-easy", "http_2xx"),
    "Email-to-RSS (Worker)": ("email-to-rss", "http_2xx"),
    "iCal Proxy k8s": ("ical-proxy", "http_2xx"),
    "k3s LiveSync": ("livesync", "http_401"),
    "k3s Scrutiny": ("scrutiny", "http_2xx"),
    "k3s homelab-mcp (edge gate)": ("homelab-mcp", "http_body_ok"),
    "Homelab Edge (all-clear)": ("traefik", "http_body_OK"),
    "Pi-hole k8s DNS": ("pihole", "dns_a_homepage"),
    "k3s Mosquitto (VIP)": ("mosquitto", "tcp_connect"),
    "k3s Jellyfin (VIP)": ("jellyfin", "tcp_connect"),
    "SLZB-06M Coordinator": ("zigbee2mqtt", "tcp_connect"),
    "k3s nut (upsd)": ("nut", "tcp_connect"),
}
RETIRED_WITHOUT_A_TARGET = {"Daniel Pi Host", "Loki"}
# The `pi_pressure` row's tile retires with the 14, and the guard lists it too.
PRESSURE_TILE = "Pi Pressure"
KEYWORDS = {"http_body_ok": "ok", "http_body_OK": "OK"}


def rendered_jobs(containers_list: list[dict] | None = None) -> dict[str, dict]:
    """Every scrape job of the rendered `prometheus.yml`, by name."""
    overrides = None
    if containers_list is not None:
        hostvars = copy.deepcopy(host_context()["hostvars"])
        # The job reads both service keys, so the override replaces both.
        hostvars["daniel-pi"]["containers_list"] = containers_list
        hostvars["daniel-pi"]["compose_services"] = []
        overrides = {"hostvars": hostvars}
    text = render_role_template("observability", "prometheus.yaml.j2", overrides)
    configmap = next(
        doc
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") == "ConfigMap"
    )
    config = yaml_fast.safe_load(configmap["data"]["prometheus.yml"])
    return {job["job_name"]: job for job in config["scrape_configs"]}


def targets(job: str, containers_list: list[dict] | None = None) -> dict[str, dict]:
    """A job's targets, each mapped to its labels."""
    return {
        target: item["labels"]
        for item in rendered_jobs(containers_list)[job]["static_configs"]
        for target in item["targets"]
    }


def pi_containers() -> list[dict]:
    """Every Pi service entry, from `containers_list` and the package's `compose_services`."""
    pi = host_context()["hostvars"]["daniel-pi"]
    return copy.deepcopy([*pi["containers_list"], *pi["compose_services"]])


def tile_address(tile: dict) -> str:
    """Where a Kuma tile probes: its URL, or `host:port` for a port tile or the DNS server."""
    if tile["type"] in ("http", "keyword"):
        return tile["url"]
    if tile["type"] == "dns":
        return f"{tile['dns_resolve_server']}:{tile['port']}"
    return f"{tile['hostname']}:{tile['port']}"


def kuma_probe_tiles() -> dict[str, dict]:
    """The rendered static monitors that are not push monitors, notifications or tags."""
    return {
        entity["name"]: entity
        for entity in _entities().values()
        if entity["type"] in ("http", "keyword", "dns", "port", "ping")
    }


def unguarded_tiles(cutover_text: str, tiles: set[str]) -> list[str]:
    """The tiles `CheckWithoutPager` has no `absent_over_time` term for."""
    return sorted(
        name
        for name in tiles
        if f'absent_over_time(monitor_status{{monitor_name="{name}"}}[2h])'
        not in cutover_text
    )


def test_the_14_hand_written_tiles_are_exactly_the_ones_b8_replaces():
    assert set(kuma_probe_tiles()) == (set(TILE_PROBES) | RETIRED_WITHOUT_A_TARGET), (
        "a hand-written tile was added or removed without B8 deciding what replaces it"
    )
    assert len(kuma_probe_tiles()) == 14


def test_each_probe_target_matches_the_tile_it_replaces():
    probes = targets(EXTRA_JOB)
    tiles = kuma_probe_tiles()
    assert len(probes) == len(TILE_PROBES)
    for name, (service, module) in TILE_PROBES.items():
        labels = probes[tile_address(tiles[name])]
        assert (labels["service"], labels["module"]) == (service, module), name
    assert probes[tile_address(tiles["k3s LiveSync"])]["module"] == "http_401"
    assert tiles["k3s LiveSync"]["accepted_statuscodes"] == ["401"]
    for module, keyword in KEYWORDS.items():
        [tile] = [t for t in tiles.values() if t.get("keyword") == keyword]
        assert probes[tile["url"]]["module"] == module


def test_the_home_critical_tiles_are_the_critical_tier():
    probes = targets(EXTRA_JOB)
    tiles = kuma_probe_tiles()
    critical = {
        name
        for name, tile in tiles.items()
        if any(t.get("value") == "critical" for t in tile.get("tag_names", []))
    }
    paged_critical = {
        name
        for name in TILE_PROBES
        if probes[tile_address(tiles[name])]["page_tier"] == "critical"
    }
    assert paged_critical == critical - RETIRED_WITHOUT_A_TARGET
    assert "Homelab Edge (all-clear)" in paged_critical


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


def test_pi_port_dead_holds_the_bridges_down_streak():
    bridge_interval_s = int(render_pod_env("monitor-bridge")["INTERVAL"])
    config = K8S_ROLES / "monitor-bridge" / "files" / "bridge" / "config_host.py"
    [default] = re.findall(r'"PI_PORTS_CONSECUTIVE",\s*"(\d+)"', config.read_text())
    rules = yaml_fast.safe_load(
        (OBS / "files" / "rules" / "extra-probes.yaml").read_text()
    )
    [rule] = [r for r in rules["groups"][0]["rules"] if r["alert"] == "PiPortDead"]
    assert rule["for"] == f"{int(default) * bridge_interval_s // 60}m"


def test_the_pager_guard_lists_every_retired_tile():
    text = (OBS / "files" / "rules" / "kuma-cutover.yaml").read_text()
    retired = set(TILE_PROBES) | RETIRED_WITHOUT_A_TARGET | {PRESSURE_TILE}
    assert unguarded_tiles(text, retired) == []


def test_a_retired_tile_missing_from_the_pager_guard_is_flagged():
    text = (OBS / "files" / "rules" / "kuma-cutover.yaml").read_text()
    text = text.replace('monitor_name="Daniel Pi Host"', 'monitor_name="Daniel Pi Hos"')
    assert unguarded_tiles(text, {"Daniel Pi Host"}) == ["Daniel Pi Host"]
